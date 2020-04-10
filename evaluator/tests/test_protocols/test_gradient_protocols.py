import random
import tempfile

from evaluator import unit
from evaluator.backends import ComputeResources
from evaluator.forcefield import ParameterGradientKey
from evaluator.protocols.gradients import CentralDifferenceGradient


def test_central_difference_gradient():

    with tempfile.TemporaryDirectory() as directory:

        gradient_key = ParameterGradientKey("vdW", "[#1]-[#8X2H2+0:1]-[#1]", "epsilon")

        reverse_parameter = -random.random() * unit.kelvin
        reverse_observable = -random.random() * unit.kelvin

        forward_parameter = random.random() * unit.kelvin
        forward_observable = random.random() * unit.kelvin

        central_difference = CentralDifferenceGradient("central_difference")
        central_difference.parameter_key = gradient_key
        central_difference.reverse_observable_value = reverse_observable
        central_difference.reverse_parameter_value = reverse_parameter
        central_difference.forward_observable_value = forward_observable
        central_difference.forward_parameter_value = forward_parameter

        central_difference.execute(directory, ComputeResources())

        assert central_difference.gradient.value == (
            (forward_observable - reverse_observable)
            / (forward_parameter - reverse_parameter)
        )
