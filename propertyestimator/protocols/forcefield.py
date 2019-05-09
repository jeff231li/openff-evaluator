"""
A collection of protocols for assigning force field parameters to molecular systems.
"""

import logging
from os import path

import numpy as np
from simtk import unit
from simtk.openmm import app

from propertyestimator.substances import Substance
from propertyestimator.utils.exceptions import PropertyEstimatorException
from propertyestimator.workflow.decorators import protocol_input, protocol_output
from propertyestimator.workflow.plugins import register_calculation_protocol
from propertyestimator.workflow.protocols import BaseProtocol


@register_calculation_protocol()
class BuildSmirnoffSystem(BaseProtocol):
    """Parametrise a set of molecules with a given smirnoff force field.
    """

    @protocol_input(str)
    def force_field_path(self, value):
        """The file path to the force field parameters to assign to the system."""
        pass

    @protocol_input(str)
    def coordinate_file_path(self, value):
        """The file path to the coordinate file which defines the system to which the
        force field parameters will be assigned."""
        pass

    @protocol_input(dict)
    def charged_molecule_paths(self):
        """File paths to mol2 files which contain the charges assigned to molecules
        in the system. This input is helpful when dealing with large molecules (such
        as hosts in host-guest binding calculations) whose charges may by needed
        in multiple places, and hence should only be calculated once. The key should
        be the smiles or unique label of the molecule"""
        pass

    @protocol_input(Substance)
    def substance(self):
        """The composition of the system."""
        pass

    @protocol_output(str)
    def system_path(self):
        """The assigned system."""
        pass

    def __init__(self, protocol_id):

        super().__init__(protocol_id)

        # inputs
        self._force_field_path = None
        self._coordinate_file_path = None
        self._substance = None

        self._charged_molecule_paths = {}

        # outputs
        self._system_path = None

    @staticmethod
    def _generate_known_charged_molecules():
        """Generates a set of molecules whose charges are known a priori,
        such as ions, for use in parameterised systems.

        Notes
        -----
        These are solely to be used as a work around until library charges
        are fully implemented in the openforcefield toolkit.

        Returns
        -------
        list of openforcefield.topology.Molecule
            The molecules with assigned charges.
        """
        from openforcefield.topology import Molecule

        sodium = Molecule.from_smiles('[Na+]')
        sodium.partial_charges = np.array([1.0]) * unit.elementary_charge

        potassium = Molecule.from_smiles('[K+]')
        potassium.partial_charges = np.array([1.0]) * unit.elementary_charge

        calcium = Molecule.from_smiles('[Ca+2]')
        calcium.partial_charges = np.array([2.0]) * unit.elementary_charge

        chlorine = Molecule.from_smiles('[Cl-]')
        chlorine.partial_charges = np.array([-1.0]) * unit.elementary_charge

        return [sodium, potassium, calcium, chlorine]

    def execute(self, directory, available_resources):

        from openforcefield.typing.engines.smirnoff import ForceField
        from openforcefield.topology import Molecule, Topology

        logging.info('Generating topology: ' + self.id)

        pdb_file = app.PDBFile(self._coordinate_file_path)

        force_field = None

        try:

            force_field = ForceField(self._force_field_path, allow_cosmetic_attributes=True)

        except Exception as e:

            return PropertyEstimatorException(directory=directory,
                                              message='{} could not load the ForceField: {}'.format(self.id, e))

        unique_molecules = []

        charged_molecules = self._generate_known_charged_molecules()

        for component in self._substance.components:

            molecule = Molecule.from_smiles(smiles=component.smiles)

            if molecule is None:

                return PropertyEstimatorException(directory=directory,
                                                  message='{} could not be converted to a Molecule'.format(component))

            unique_molecules.append(molecule)

            if (self._charged_molecule_paths is not None and
                component.label in self._charged_molecule_paths):

                molecule_path = self._charged_molecule_paths[component.label]
                charged_molecule = Molecule.from_file(molecule_path, 'MOL2')

                charged_molecules.append(charged_molecule)

        topology = Topology.from_openmm(pdb_file.topology, unique_molecules=unique_molecules)

        system = force_field.create_openmm_system(topology, charge_from_molecules=charged_molecules)

        if system is None:

            return PropertyEstimatorException(directory=directory,
                                              message='Failed to create a system from the'
                                                       'provided topology and molecules')

        from simtk.openmm import XmlSerializer
        system_xml = XmlSerializer.serialize(system)

        self._system_path = path.join(directory, 'system.xml')

        with open(self._system_path, 'wb') as file:
            file.write(system_xml.encode('utf-8'))

        logging.info('Topology generated: ' + self.id)

        return self._get_output_dictionary()
