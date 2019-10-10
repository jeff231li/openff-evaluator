"""
A collection of protocols for running molecular simulations.
"""
import logging
import math
import os
import shutil
import threading
import traceback
from enum import Enum

import numpy as np
import yaml
from simtk import openmm, unit as simtk_unit
from simtk.openmm import app

from propertyestimator import unit
from propertyestimator.thermodynamics import ThermodynamicState, Ensemble
from propertyestimator.utils.exceptions import PropertyEstimatorException
from propertyestimator.utils.openmm import setup_platform_with_resources, openmm_quantity_to_pint, \
    pint_quantity_to_openmm, disable_pbc
from propertyestimator.utils.quantities import EstimatedQuantity
from propertyestimator.utils.statistics import StatisticsArray, ObservableType
from propertyestimator.utils.utils import temporarily_change_directory, safe_unlink
from propertyestimator.workflow.decorators import protocol_input, protocol_output, InequalityMergeBehaviour
from propertyestimator.workflow.plugins import register_calculation_protocol
from propertyestimator.workflow.protocols import BaseProtocol


@register_calculation_protocol()
class RunEnergyMinimisation(BaseProtocol):
    """A protocol to minimise the potential energy of a system.
    """

    input_coordinate_file = protocol_input(docstring='The coordinates to minimise.',
                                           type_hint=str,
                                           default_value=protocol_input.UNDEFINED)

    tolerance = protocol_input(docstring='The energy tolerance to which the system should be minimized.',
                               type_hint=unit.Quantity,
                               default_value=10*unit.kilojoules / unit.mole)

    max_iterations = protocol_input(docstring='The maximum number of iterations to perform.  If this is 0, '
                                              'minimization is continued until the results converge without regard to '
                                              'how many iterations it takes.',
                                    type_hint=int,
                                    default_value=10)

    system_path = protocol_input(docstring='The path to the XML system object which defines the forces present '
                                           'in the system.',
                                 type_hint=str,
                                 default_value=protocol_input.UNDEFINED)

    enable_pbc = protocol_input(docstring='If true, periodic boundary conditions will be enabled.',
                                type_hint=bool,
                                default_value=protocol_input.UNDEFINED)

    output_coordinate_file = protocol_output(docstring='The file path to the minimised coordinates.',
                                             type_hint=str)

    def __init__(self, protocol_id):

        super().__init__(protocol_id)

        self._input_coordinate_file = None

        self._system_path = None
        self._system = None

        self._enable_pbc = True

        self._tolerance = 10*unit.kilojoules / unit.mole
        self._max_iterations = 0

        self._output_coordinate_file = None

    def execute(self, directory, available_resources):

        logging.info('Minimising energy: ' + self.id)

        platform = setup_platform_with_resources(available_resources)

        input_pdb_file = app.PDBFile(self._input_coordinate_file)

        with open(self._system_path, 'rb') as file:
            self._system = openmm.XmlSerializer.deserialize(file.read().decode())

        if not self._enable_pbc:

            for force_index in range(self._system.getNumForces()):

                force = self._system.getForce(force_index)

                if not isinstance(force, openmm.NonbondedForce):
                    continue

                force.setNonbondedMethod(0)  # NoCutoff = 0, NonbondedMethod.CutoffNonPeriodic = 1

        # TODO: Expose the constraint tolerance
        integrator = openmm.VerletIntegrator(0.002 * simtk_unit.picoseconds)
        simulation = app.Simulation(input_pdb_file.topology, self._system, integrator, platform)

        box_vectors = input_pdb_file.topology.getPeriodicBoxVectors()

        if box_vectors is None:
            box_vectors = simulation.system.getDefaultPeriodicBoxVectors()

        simulation.context.setPeriodicBoxVectors(*box_vectors)
        simulation.context.setPositions(input_pdb_file.positions)

        simulation.minimizeEnergy(pint_quantity_to_openmm(self._tolerance), self._max_iterations)

        positions = simulation.context.getState(getPositions=True).getPositions()

        self._output_coordinate_file = os.path.join(directory, 'minimised.pdb')

        with open(self._output_coordinate_file, 'w+') as minimised_file:
            app.PDBFile.writeFile(simulation.topology, positions, minimised_file)

        logging.info('Energy minimised: ' + self.id)

        return self._get_output_dictionary()


@register_calculation_protocol()
class RunOpenMMSimulation(BaseProtocol):
    """Performs a molecular dynamics simulation in a given ensemble using
    an OpenMM backend.
    """

    steps = protocol_input(docstring='The number of timesteps to evolve the system by.',
                           type_hint=int, merge_behavior=InequalityMergeBehaviour.LargestValue,
                           default_value=1000000)

    thermostat_friction = protocol_input(docstring='The thermostat friction coefficient.',
                                         type_hint=unit.Quantity, merge_behavior=InequalityMergeBehaviour.SmallestValue,
                                         default_value=1.0 / unit.picoseconds)

    timestep = protocol_input(docstring='The timestep to evolve the system by at each step.',
                              type_hint=unit.Quantity, merge_behavior=InequalityMergeBehaviour.SmallestValue,
                              default_value=2.0*unit.femtosecond)

    output_frequency = protocol_input(docstring='The frequency with which to write to the output statistics and '
                                                'trajectory files.',
                                      type_hint=int, merge_behavior=InequalityMergeBehaviour.SmallestValue,
                                      default_value=3000)

    ensemble = protocol_input(docstring='The thermodynamic ensemble to simulate in.',
                              type_hint=Ensemble,
                              default_value=Ensemble.NPT)

    thermodynamic_state = protocol_input(docstring='The thermodynamic conditions to simulate under',
                                         type_hint=ThermodynamicState,
                                         default_value=protocol_input.UNDEFINED)

    input_coordinate_file = protocol_input(docstring='The file path to the starting coordinates.',
                                           type_hint=str,
                                           default_value=protocol_input.UNDEFINED)

    system_path = protocol_input(docstring='A path to the XML system object which defines the forces present '
                                           'in the system.',
                                 type_hint=str,
                                 default_value=protocol_input.UNDEFINED)

    enable_pbc = protocol_input(docstring='If true, periodic boundary conditions will be enabled.',
                                type_hint=bool,
                                default_value=True)

    save_rolling_statistics = protocol_input(docstring='If True, the statistics file will be written to every '
                                                       '`output_frequency` number of steps, rather than just once at '
                                                       'the end of the simulation.',
                                             type_hint=bool,
                                             default_value=True)

    allow_gpu_platforms = protocol_input(docstring='If true, OpenMM will be allowed to run using a GPU if available, '
                                                   'otherwise it will be constrained to only using CPUs.',
                                         type_hint=bool,
                                         default_value=True)

    high_precision = protocol_input(docstring='If true, OpenMM will be run using a platform with high precision '
                                              'settings. This will be the Reference platform when only a CPU is '
                                              'available, or double precision mode when a GPU is available.',
                                    type_hint=bool,
                                    default_value=False)

    output_coordinate_file = protocol_output(docstring='The file path to the coordinates of the final system '
                                                       'configuration.',
                                             type_hint=str)

    trajectory_file_path = protocol_output(docstring='The file path to the trajectory sampled during the simulation.',
                                           type_hint=str)

    statistics_file_path = protocol_output(docstring='The file path to the statistics sampled during the simulation.',
                                           type_hint=str)

    def __init__(self, protocol_id):

        super().__init__(protocol_id)

        self._steps = 1000000

        self._thermostat_friction = 1.0 / unit.picoseconds
        self._timestep = 0.002 * unit.picoseconds

        self._output_frequency = 3000

        self._ensemble = Ensemble.NPT

        self._input_coordinate_file = None
        self._thermodynamic_state = None

        self._system_path = None

        self._enable_pbc = True

        self._save_rolling_statistics = True

        self._allow_gpu_platforms = True
        self._high_precision = False

        self._output_coordinate_file = None

        self._trajectory_file_path = None
        self._statistics_file_path = None

        # Keep a track of the file names used for temporary working files.
        self._temporary_statistics_path = None
        self._temporary_trajectory_path = None

        self._checkpoint_path = None

        self._context = None
        self._integrator = None

    def execute(self, directory, available_resources):

        # We handle most things in OMM units here.
        temperature = pint_quantity_to_openmm(self._thermodynamic_state.temperature)
        pressure = pint_quantity_to_openmm(None if self._ensemble == Ensemble.NVT else
                                           self._thermodynamic_state.pressure)

        if temperature is None:

            return PropertyEstimatorException(directory=directory,
                                              message='A temperature must be set to perform '
                                                      'a simulation in any ensemble')

        if Ensemble(self._ensemble) == Ensemble.NPT and pressure is None:

            return PropertyEstimatorException(directory=directory,
                                              message='A pressure must be set to perform an NPT simulation')

        if Ensemble(self._ensemble) == Ensemble.NPT and self._enable_pbc is False:

            return PropertyEstimatorException(directory=directory,
                                              message='PBC must be enabled when running in the NPT ensemble.')

        logging.info('Performing a simulation in the ' + str(self._ensemble) + ' ensemble: ' + self.id)

        # Clean up any temporary files from previous (possibly failed)
        # simulations.
        self._temporary_statistics_path = os.path.join(directory, 'temp_statistics.csv')
        self._temporary_trajectory_path = os.path.join(directory, 'temp_trajectory.dcd')

        safe_unlink(self._temporary_statistics_path)
        safe_unlink(self._temporary_trajectory_path)

        self._checkpoint_path = os.path.join(directory, 'checkpoint.xml')

        # Set up the output file paths
        self._trajectory_file_path = os.path.join(directory, 'trajectory.dcd')
        self._statistics_file_path = os.path.join(directory, 'statistics.csv')

        # Set up the simulation objects.
        if self._context is None or self._integrator is None:

            self._context, self._integrator = self._setup_simulation_objects(temperature,
                                                                             pressure,
                                                                             available_resources)

        result = self._simulate(directory, temperature, pressure, self._context, self._integrator)
        return result

    def _setup_simulation_objects(self, temperature, pressure, available_resources):
        """Initializes the objects needed to perform the simulation.
        This comprises of a context, and an integrator.

        Parameters
        ----------
        temperature: simtk.unit.Quantity
            The temperature to run the simulation at.
        pressure: simtk.unit.Quantity
            The pressure to run the simulation at.
        available_resources: ComputeResources
            The resources available to run on.

        Returns
        -------
        simtk.openmm.Context
            The created openmm context which takes advantage
            of the available compute resources.
        openmmtools.integrators.LangevinIntegrator
            The Langevin integrator which will propogate
            the simulation.
        """

        import openmmtools
        from simtk.openmm import XmlSerializer

        # Create a platform with the correct resources.
        if not self._allow_gpu_platforms:

            from propertyestimator.backends import ComputeResources
            available_resources = ComputeResources(available_resources.number_of_threads)

        platform = setup_platform_with_resources(available_resources, self._high_precision)

        # Load in the system object from the provided xml file.
        with open(self._system_path, 'r') as file:
            system = XmlSerializer.deserialize(file.read())

        # Disable the periodic boundary conditions if requested.
        if not self._enable_pbc:

            disable_pbc(system)
            pressure = None

        # Use the openmmtools ThermodynamicState object to help
        # set up a system which contains the correct barostat if
        # one should be present.
        openmm_state = openmmtools.states.ThermodynamicState(system=system,
                                                             temperature=temperature,
                                                             pressure=pressure)

        system = openmm_state.get_system(remove_thermostat=True)

        # Set up the integrator.
        thermostat_friction = pint_quantity_to_openmm(self._thermostat_friction)
        timestep = pint_quantity_to_openmm(self._timestep)

        integrator = openmmtools.integrators.LangevinIntegrator(temperature=temperature,
                                                                collision_rate=thermostat_friction,
                                                                timestep=timestep)

        # Create the simulation context.
        context = openmm.Context(system, integrator, platform)

        # Initialize the context with the correct positions etc.
        if os.path.isfile(self._checkpoint_path):

            # Load the simulation state from a checkpoint file.
            logging.info(f'Loading the checkpoint from {self._checkpoint_path}.')

            with open(self._checkpoint_path, 'r') as file:
                checkpoint_state = XmlSerializer.deserialize(file.read())

            context.setState(checkpoint_state)

        else:

            logging.info(f'No checkpoint file was found at {self._checkpoint_path}.')

            # Populate the simulation object from the starting input files.
            input_pdb_file = app.PDBFile(self._input_coordinate_file)

            if self._enable_pbc:

                # Optionally set up the box vectors.
                box_vectors = input_pdb_file.topology.getPeriodicBoxVectors()

                if box_vectors is None:

                    raise ValueError('The input file must contain box vectors '
                                     'when running with PBC.')

                context.setPeriodicBoxVectors(*box_vectors)

            context.setPositions(input_pdb_file.positions)
            context.setVelocitiesToTemperature(temperature)

        return context, integrator

    def _write_statistics_array(self, raw_statistics, current_step, temperature, pressure,
                                degrees_of_freedom, total_mass):
        """Appends a set of statistics to an existing `statistics_array`.
        Those statistics are potential energy, kinetic energy, total energy,
        volume, density and reduced potential.

        Parameters
        ----------
        raw_statistics: dict of ObservableType and numpy.ndarray
            A dictionary of potential energies (kJ/mol), kinetic
            energies (kJ/mol) and volumes (angstrom**3).
        current_step: int
            The index of the current step.
        temperature: simtk.unit.Quantity
            The temperature the system is being simulated at.
        pressure: simtk.unit.Quantity
            The pressure the system is being simulated at.
        degrees_of_freedom: int
            The number of degrees of freedom the system has.
        total_mass: simtk.unit.Quantity
            The total mass of the system.

        Returns
        -------
        StatisticsArray
            The statistics array with statistics appended.
        """
        temperature = openmm_quantity_to_pint(temperature)
        pressure = openmm_quantity_to_pint(pressure)

        beta = 1.0 / (unit.boltzmann_constant * temperature)

        raw_potential_energies = raw_statistics[ObservableType.PotentialEnergy][0:current_step + 1]
        raw_kinetic_energies = raw_statistics[ObservableType.KineticEnergy][0:current_step + 1]
        raw_volumes = raw_statistics[ObservableType.Volume][0:current_step + 1]

        potential_energies = raw_potential_energies * unit.kilojoules / unit.mole
        kinetic_energies = raw_kinetic_energies * unit.kilojoules / unit.mole
        volumes = raw_volumes * unit.angstrom ** 3

        # Calculate the instantaneous temperature, taking account the
        # systems degrees of freedom.
        temperatures = 2.0 * kinetic_energies / (degrees_of_freedom * unit.molar_gas_constant)

        # Calculate the systems enthalpy and reduced potential.
        total_energies = potential_energies + kinetic_energies
        enthalpies = None

        reduced_potentials = potential_energies / unit.avogadro_number

        if pressure is not None:

            pv_terms = pressure * volumes

            reduced_potentials += pv_terms
            enthalpies = total_energies + pv_terms * unit.avogadro_number

        reduced_potentials = (beta * reduced_potentials) * unit.dimensionless

        # Calculate the systems density.
        densities = total_mass / (volumes * unit.avogadro_number)

        statistics_array = StatisticsArray()

        statistics_array[ObservableType.PotentialEnergy] = potential_energies
        statistics_array[ObservableType.KineticEnergy] = kinetic_energies
        statistics_array[ObservableType.TotalEnergy] = total_energies
        statistics_array[ObservableType.Temperature] = temperatures
        statistics_array[ObservableType.Volume] = volumes
        statistics_array[ObservableType.Density] = densities
        statistics_array[ObservableType.ReducedPotential] = reduced_potentials

        if enthalpies is not None:
            statistics_array[ObservableType.Enthalpy] = enthalpies

        statistics_array.to_pandas_csv(self._temporary_statistics_path)

    def _simulate(self, directory, temperature, pressure, context, integrator):
        """Performs the simulation using a given context
        and integrator.

        Parameters
        ----------
        directory: str
            The directory the trajectory is being run in.
        temperature: simtk.unit.Quantity
            The temperature to run the simulation at.
        pressure: simtk.unit.Quantity
            The pressure to run the simulation at.
        context: simtk.openmm.Context
            The OpenMM context to run with.
        integrator: simtk.openmm.Integrator
            The integrator to evolve the simulation with.
        """

        # Build the reporters which we will use to report the state
        # of the simulation.
        input_pdb_file = app.PDBFile(self._input_coordinate_file)
        topology = input_pdb_file.topology

        with open(os.path.join(directory, 'input.pdb'), 'w+') as configuration_file:
            app.PDBFile.writeFile(input_pdb_file.topology, input_pdb_file.positions, configuration_file)

        # Make a copy of the existing trajectory to append to if one already exists.
        append_trajectory = False

        if os.path.isfile(self._trajectory_file_path):

            shutil.copyfile(self._trajectory_file_path, self._temporary_trajectory_path)
            append_trajectory = True

        elif os.path.isfile(self._temporary_trajectory_path):
            os.unlink(self._temporary_trajectory_path)

        if append_trajectory:
            trajectory_file_object = open(self._temporary_trajectory_path, 'r+b')
        else:
            trajectory_file_object = open(self._temporary_trajectory_path, 'w+b')

        trajectory_dcd_object = app.DCDFile(trajectory_file_object,
                                            topology,
                                            integrator.getStepSize(),
                                            0,
                                            self._output_frequency,
                                            append_trajectory)

        expected_number_of_statistics = math.ceil(self.steps / self.output_frequency)

        raw_statistics = {
            ObservableType.PotentialEnergy: np.zeros(expected_number_of_statistics),
            ObservableType.KineticEnergy: np.zeros(expected_number_of_statistics),
            ObservableType.Volume: np.zeros(expected_number_of_statistics),
        }

        # Define any constants needed for extracting system statistics
        # Compute the instantaneous temperature of degrees of freedom.
        # This snipped is taken from the build in OpenMM `StateDataReporter`
        system = context.getSystem()

        degrees_of_freedom = sum([3 for i in range(system.getNumParticles()) if
                                  system.getParticleMass(i) > 0 * simtk_unit.dalton])

        degrees_of_freedom -= system.getNumConstraints()

        if any(type(system.getForce(i)) == openmm.CMMotionRemover for i in range(system.getNumForces())):
            degrees_of_freedom -= 3

        total_mass = 0.0 * simtk_unit.dalton

        for i in range(system.getNumParticles()):
            total_mass += system.getParticleMass(i)

        total_mass = openmm_quantity_to_pint(total_mass)

        # Perform the simulation.
        current_step_count = 0
        current_step = 0

        result = None

        try:

            while current_step_count < self.steps:

                steps_to_take = min(self._output_frequency, self.steps - current_step_count)
                integrator.step(steps_to_take)

                state = context.getState(getPositions=True,
                                         getEnergy=True,
                                         getVelocities=False,
                                         getForces=False,
                                         getParameters=False,
                                         enforcePeriodicBox=self.enable_pbc)

                # Write out the current frame of the trajectory.
                trajectory_dcd_object.writeModel(positions=state.getPositions(),
                                                 periodicBoxVectors=state.getPeriodicBoxVectors())

                # Write out the energies and system statistics.
                raw_statistics[ObservableType.PotentialEnergy][current_step] = \
                    state.getPotentialEnergy().value_in_unit(simtk_unit.kilojoules_per_mole)
                raw_statistics[ObservableType.KineticEnergy][current_step] = \
                    state.getKineticEnergy().value_in_unit(simtk_unit.kilojoules_per_mole)
                raw_statistics[ObservableType.Volume][current_step] = \
                    state.getPeriodicBoxVolume().value_in_unit(simtk_unit.angstrom ** 3)

                if self._save_rolling_statistics:

                    self._write_statistics_array(raw_statistics, current_step, temperature,
                                                 pressure, degrees_of_freedom, total_mass)

                current_step_count += steps_to_take
                current_step += 1

        except Exception as e:

            formatted_exception = f'{traceback.format_exception(None, e, e.__traceback__)}'

            result = PropertyEstimatorException(directory=directory,
                                                message=f'The simulation failed unexpectedly: '
                                                        f'{formatted_exception}')

        # Create a checkpoint file.
        state = context.getState(getPositions=True,
                                 getEnergy=True,
                                 getVelocities=True,
                                 getForces=True,
                                 getParameters=True,
                                 enforcePeriodicBox=self.enable_pbc)

        state_xml = openmm.XmlSerializer.serialize(state)

        with open(self._checkpoint_path, 'w') as file:
            file.write(state_xml)

        # Make sure to close the open trajectory stream.
        trajectory_file_object.close()

        # Save the final statistics
        self._write_statistics_array(raw_statistics, current_step, temperature,
                                     pressure, degrees_of_freedom, total_mass)

        if isinstance(result, PropertyEstimatorException):
            return result

        # Move the trajectory and statistics files to their
        # final location.
        os.replace(self._temporary_trajectory_path, self._trajectory_file_path)

        if not os.path.isfile(self._statistics_file_path):
            os.replace(self._temporary_statistics_path, self._statistics_file_path)
        else:

            existing_statistics = StatisticsArray.from_pandas_csv(self._statistics_file_path)
            current_statistics = StatisticsArray.from_pandas_csv(self._temporary_statistics_path)

            concatenated_statistics = StatisticsArray.join(existing_statistics,
                                                           current_statistics)

            concatenated_statistics.to_pandas_csv(self._statistics_file_path)

        # Save out the final positions.
        final_state = context.getState(getPositions=True)
        positions = final_state.getPositions()
        topology.setPeriodicBoxVectors(final_state.getPeriodicBoxVectors())

        self._output_coordinate_file = os.path.join(directory, 'output.pdb')

        with open(self._output_coordinate_file, 'w+') as configuration_file:
            app.PDBFile.writeFile(topology, positions, configuration_file)

        logging.info(f'Simulation performed in the {str(self._ensemble)} ensemble: {self._id}')
        return self._get_output_dictionary()


@register_calculation_protocol()
class BaseYankProtocol(BaseProtocol):
    """An abstract base class for protocols which will performs a set of alchemical
    free energy simulations using the YANK framework.

    Protocols which inherit from this base must implement the abstract `_get_yank_options`
    methods.
    """

    thermodynamic_state = protocol_input(docstring='The state at which to run the calculations.',
                                         type_hint=ThermodynamicState,
                                         default_value=protocol_input.UNDEFINED)

    number_of_iterations = protocol_input(docstring='The number of YANK iterations to perform.',
                                          type_hint=int, merge_behavior=InequalityMergeBehaviour.LargestValue,
                                          default_value=protocol_input.UNDEFINED)

    steps_per_iteration = protocol_input(docstring='The number of steps per YANK iteration to perform.',
                                         type_hint=int, merge_behavior=InequalityMergeBehaviour.LargestValue,
                                         default_value=500)

    checkpoint_interval = protocol_input(docstring='The number of iterations between saving YANK checkpoint files.',
                                         type_hint=int, merge_behavior=InequalityMergeBehaviour.SmallestValue,
                                         default_value=50)

    timestep = protocol_input(docstring='The length of the timestep to take.',
                              type_hint=unit.Quantity, merge_behavior=InequalityMergeBehaviour.SmallestValue,
                              default_value=2*unit.femtosecond)

    force_field_path = protocol_input(docstring='The path to the force field to use for the calculations',
                                      type_hint=str,
                                      default_value=protocol_input.UNDEFINED)

    verbose = protocol_input(docstring='Controls whether or not to run YANK at high verbosity.',
                             type_hint=bool,
                             default_value=False)

    estimated_free_energy = protocol_output(docstring='The estimated free energy value and its uncertainty '
                                                      'returned by YANK.',
                                            type_hint=EstimatedQuantity)

    def __init__(self, protocol_id):
        """Constructs a new BaseYankProtocol object."""

        super().__init__(protocol_id)

        self._thermodynamic_state = None
        self._timestep = 2 * unit.femtosecond

        self._number_of_iterations = 1

        self._steps_per_iteration = 500
        self._checkpoint_interval = 50

        self._force_field_path = None

        self._verbose = False

        self._estimated_free_energy = None

    def _get_options_dictionary(self, available_resources):
        """Returns a dictionary of options which will be serialized
        to a yaml file and passed to YANK.

        Parameters
        ----------
        available_resources: ComputeResources
            The resources available to execute on.

        Returns
        -------
        dict of str and Any
            A yaml compatible dictionary of YANK options.
        """

        from openforcefield.utils import quantity_to_string

        platform_name = 'CPU'

        if available_resources.number_of_gpus > 0:

            # A platform which runs on GPUs has been requested.
            from propertyestimator.backends import ComputeResources
            toolkit_enum = ComputeResources.GPUToolkit(available_resources.preferred_gpu_toolkit)

            # A platform which runs on GPUs has been requested.
            platform_name = 'CUDA' if toolkit_enum == ComputeResources.GPUToolkit.CUDA else \
                                                      ComputeResources.GPUToolkit.OpenCL

        return {
            'verbose': self._verbose,
            'output_dir': '.',

            'temperature': quantity_to_string(pint_quantity_to_openmm(self._thermodynamic_state.temperature)),
            'pressure': quantity_to_string(pint_quantity_to_openmm(self._thermodynamic_state.pressure)),

            'minimize': True,

            'default_number_of_iterations': self._number_of_iterations,
            'default_nsteps_per_iteration': self._steps_per_iteration,
            'checkpoint_interval': self._checkpoint_interval,

            'default_timestep': quantity_to_string(pint_quantity_to_openmm(self._timestep)),

            'annihilate_electrostatics': True,
            'annihilate_sterics': False,

            'platform': platform_name
        }

    def _get_solvent_dictionary(self):
        """Returns a dictionary of the solvent which will be serialized
        to a yaml file and passed to YANK. In most cases, this should
        just be passing force field settings over, such as PME settings.

        Returns
        -------
        dict of str and Any
            A yaml compatible dictionary of YANK solvents.
        """
        from openforcefield.typing.engines.smirnoff.forcefield import ForceField
        force_field = ForceField(self._force_field_path)

        charge_method = force_field.get_parameter_handler('Electrostatics').method

        if charge_method.lower() != 'pme':
            raise ValueError('Currently only PME electrostatics are supported.')

        return {'default': {
            'nonbonded_method': charge_method,
        }}

    def _get_system_dictionary(self):
        """Returns a dictionary of the system which will be serialized
        to a yaml file and passed to YANK. Only a single system may be
        specified.

        Returns
        -------
        dict of str and Any
            A yaml compatible dictionary of YANK systems.
        """
        raise NotImplementedError()

    def _get_protocol_dictionary(self):
        """Returns a dictionary of the protocol which will be serialized
        to a yaml file and passed to YANK. Only a single protocol may be
        specified.

        Returns
        -------
        dict of str and Any
            A yaml compatible dictionary of a YANK protocol.
        """
        raise NotImplementedError()

    def _get_experiments_dictionary(self):
        """Returns a dictionary of the experiments which will be serialized
        to a yaml file and passed to YANK. Only a single experiment may be
        specified.

        Returns
        -------
        dict of str and Any
            A yaml compatible dictionary of a YANK experiment.
        """

        system_dictionary = self._get_system_dictionary()
        system_key = next(iter(system_dictionary))

        protocol_dictionary = self._get_protocol_dictionary()
        protocol_key = next(iter(protocol_dictionary))

        return {
            'system': system_key,
            'protocol': protocol_key
        }

    def _get_full_input_dictionary(self, available_resources):
        """Returns a dictionary of the full YANK inputs which will be serialized
        to a yaml file and passed to YANK

        Parameters
        ----------
        available_resources: ComputeResources
            The resources available to execute on.

        Returns
        -------
        dict of str and Any
            A yaml compatible dictionary of a YANK input file.
        """

        return {
            'options': self._get_options_dictionary(available_resources),

            'solvents': self._get_solvent_dictionary(),

            'systems': self._get_system_dictionary(),
            'protocols': self._get_protocol_dictionary(),

            'experiments': self._get_experiments_dictionary()
        }

    @staticmethod
    def _extract_trajectory(checkpoint_path, output_trajectory_path):
        """Extracts the stored trajectory of the 'initial' state from a
        yank `.nc` checkpoint file and stores it to disk as a `.dcd` file.

        Parameters
        ----------
        checkpoint_path: str
            The path to the yank `.nc` file
        output_trajectory_path: str
            The path to store the extracted trajectory at.
        """

        from yank.analyze import extract_trajectory

        mdtraj_trajectory = extract_trajectory(checkpoint_path, state_index=0, image_molecules=True)
        mdtraj_trajectory.save_dcd(output_trajectory_path)

    @staticmethod
    def _run_yank(directory, available_resources):
        """Runs YANK within the specified directory which contains a `yank.yaml`
        input file.

        Parameters
        ----------
        directory: str
            The directory within which to run yank.

        Returns
        -------
        simtk.unit.Quantity
            The free energy returned by yank.
        simtk.unit.Quantity
            The uncertainty in the free energy returned by yank.
        """

        from yank.experiment import ExperimentBuilder
        from yank.analyze import ExperimentAnalyzer

        with temporarily_change_directory(directory):

            # Set the default properties on the desired platform
            # before calling into yank.
            setup_platform_with_resources(available_resources)

            exp_builder = ExperimentBuilder('yank.yaml')
            exp_builder.run_experiments()

            analyzer = ExperimentAnalyzer('experiments')
            output = analyzer.auto_analyze()

            free_energy = output['free_energy']['free_energy_diff_unit']
            free_energy_uncertainty = output['free_energy']['free_energy_diff_error_unit']

        return free_energy, free_energy_uncertainty

    @staticmethod
    def _run_yank_as_process(queue, directory, available_resources):
        """A wrapper around the `_run_yank` method which takes
        a `multiprocessing.Queue` as input, thereby allowing it
        to be launched from a separate process and still return
        it's output back to the main process.

        Parameters
        ----------
        queue: multiprocessing.Queue
            The queue object which will communicate with the
            launched process.
        directory: str
            The directory within which to run yank.

        Returns
        -------
        simtk.unit.Quantity
            The free energy returned by yank.
        simtk.unit.Quantity
            The uncertainty in the free energy returned by yank.
        str, optional
            The stringified errors which occurred on the other process,
            or `None` if no exceptions were raised.
        """

        free_energy = None
        free_energy_uncertainty = None

        error = None

        try:
            free_energy, free_energy_uncertainty = BaseYankProtocol._run_yank(directory, available_resources)
        except Exception as e:
            error = traceback.format_exception(None, e, e.__traceback__)

        queue.put((free_energy, free_energy_uncertainty, error))

    def execute(self, directory, available_resources):

        yaml_filename = os.path.join(directory, 'yank.yaml')

        # Create the yank yaml input file from a dictionary of options.
        with open(yaml_filename, 'w') as file:
            yaml.dump(self._get_full_input_dictionary(available_resources), file)

        # Yank is not safe to be called from anything other than the main thread.
        # If the current thread is not detected as the main one, then yank should
        # be spun up in a new process which should itself be safe to run yank in.
        if threading.current_thread() is threading.main_thread():
            logging.info('Launching YANK in the main thread.')
            free_energy, free_energy_uncertainty = self._run_yank(directory, available_resources)
        else:

            from multiprocessing import Process, Queue

            logging.info('Launching YANK in a new process.')

            # Create a queue to pass the results back to the main process.
            queue = Queue()
            # Create the process within which yank will run.
            process = Process(target=BaseYankProtocol._run_yank_as_process, args=[queue, directory,
                                                                                  available_resources])

            # Start the process and gather back the output.
            process.start()
            free_energy, free_energy_uncertainty, error = queue.get()
            process.join()

            if error is not None:
                return PropertyEstimatorException(directory, error)

        self._estimated_free_energy = EstimatedQuantity(openmm_quantity_to_pint(free_energy),
                                                        openmm_quantity_to_pint(free_energy_uncertainty),
                                                        self._id)

        return self._get_output_dictionary()


@register_calculation_protocol()
class LigandReceptorYankProtocol(BaseYankProtocol):
    """An abstract base class for protocols which will performs a set of
    alchemical free energy simulations using the YANK framework.

    Protocols which inherit from this base must implement the abstract
    `_get_*_dictionary` methods.
    """

    class RestraintType(Enum):
        """The types of ligand restraints available within yank.
        """
        Harmonic = 'Harmonic'
        FlatBottom = 'FlatBottom'

    ligand_residue_name = protocol_input(docstring='The residue name of the ligand.',
                                         type_hint=str,
                                         default_value=protocol_input.UNDEFINED)

    receptor_residue_name = protocol_input(docstring='The residue name of the receptor.',
                                           type_hint=str,
                                           default_value=protocol_input.UNDEFINED)

    solvated_ligand_coordinates = protocol_input(docstring='The file path to the solvated ligand coordinates.',
                                                 type_hint=str,
                                                 default_value=protocol_input.UNDEFINED)

    solvated_ligand_system = protocol_input(docstring='The file path to the solvated ligand system object.',
                                            type_hint=str,
                                            default_value=protocol_input.UNDEFINED)

    solvated_complex_coordinates = protocol_input(docstring='The file path to the solvated complex coordinates.',
                                                  type_hint=str,
                                                  default_value=protocol_input.UNDEFINED)

    solvated_complex_system = protocol_input(docstring='The file path to the solvated complex system object.',
                                             type_hint=str,
                                             default_value=protocol_input.UNDEFINED)

    apply_restraints = protocol_input(docstring='Determines whether the ligand should be explicitly restrained to the '
                                                'receptor in order to stop the ligand from temporarily unbinding.',
                                      type_hint=bool,
                                      default_value=True)

    restraint_type = protocol_input(docstring='The type of ligand restraint applied, provided that `apply_restraints` '
                                              'is `True`',
                                    type_hint=RestraintType,
                                    default_value=RestraintType.Harmonic)

    solvated_ligand_trajectory_path = protocol_output(docstring='The file path to the generated ligand trajectory.',
                                                      type_hint=str)

    solvated_complex_trajectory_path = protocol_output(docstring='The file path to the generated ligand trajectory.',
                                                       type_hint=str)

    def __init__(self, protocol_id):
        """Constructs a new LigandReceptorYankProtocol object."""

        super().__init__(protocol_id)

        self._ligand_residue_name = None
        self._receptor_residue_name = None

        self._solvated_ligand_coordinates = None
        self._solvated_ligand_system = None

        self._solvated_complex_coordinates = None
        self._solvated_complex_system = None

        self._local_ligand_coordinates = 'ligand.pdb'
        self._local_ligand_system = 'ligand.xml'

        self._local_complex_coordinates = 'complex.pdb'
        self._local_complex_system = 'complex.xml'

        self._solvated_ligand_trajectory_path = None
        self._solvated_complex_trajectory_path = None

        self._apply_restraints = True
        self._restraint_type = LigandReceptorYankProtocol.RestraintType.Harmonic

    def _get_system_dictionary(self):

        solvent_dictionary = self._get_solvent_dictionary()
        solvent_key = next(iter(solvent_dictionary))

        host_guest_dictionary = {
            'phase1_path': [self._local_complex_system, self._local_complex_coordinates],
            'phase2_path': [self._local_ligand_system, self._local_ligand_coordinates],

            'ligand_dsl': f'resname {self._ligand_residue_name}',
            'solvent': solvent_key
        }

        return {'host-guest': host_guest_dictionary}

    def _get_protocol_dictionary(self):

        absolute_binding_dictionary = {
            'complex': {'alchemical_path': 'auto'},
            'solvent': {'alchemical_path': 'auto'}
        }

        return {'absolute_binding_dictionary': absolute_binding_dictionary}

    def _get_experiments_dictionary(self):

        experiments_dictionary = super(LigandReceptorYankProtocol, self)._get_experiments_dictionary()

        if self._apply_restraints:

            experiments_dictionary['restraint'] = {
                'restrained_ligand_atoms': f'(resname {self._ligand_residue_name}) and (mass > 1.5)',
                'restrained_receptor_atoms': f'(resname {self._receptor_residue_name}) and (mass > 1.5)',

                'type': self._restraint_type.value
            }

        return experiments_dictionary

    def execute(self, directory, available_resources):

        # Because of quirks in where Yank looks files while doing temporary
        # directory changes, we need to copy the coordinate files locally so
        # they are correctly found.
        shutil.copyfile(self._solvated_ligand_coordinates, os.path.join(directory, self._local_ligand_coordinates))
        shutil.copyfile(self._solvated_ligand_system, os.path.join(directory, self._local_ligand_system))

        shutil.copyfile(self._solvated_complex_coordinates, os.path.join(directory, self._local_complex_coordinates))
        shutil.copyfile(self._solvated_complex_system, os.path.join(directory, self._local_complex_system))

        result = super(LigandReceptorYankProtocol, self).execute(directory, available_resources)

        if isinstance(result, PropertyEstimatorException):
            return result

        ligand_yank_path = os.path.join(directory, 'experiments', 'solvent.nc')
        complex_yank_path = os.path.join(directory, 'experiments', 'complex.nc')

        self._solvated_ligand_trajectory_path = os.path.join(directory, 'ligand.dcd')
        self._solvated_complex_trajectory_path = os.path.join(directory, 'complex.dcd')

        self._extract_trajectory(ligand_yank_path, self._solvated_ligand_trajectory_path)
        self._extract_trajectory(complex_yank_path, self._solvated_complex_trajectory_path)

        return self._get_output_dictionary()
