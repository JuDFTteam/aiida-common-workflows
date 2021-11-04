# -*- coding: utf-8 -*-
"""Implementation of `aiida_common_workflows.common.bands.generator.CommonBandsInputGenerator` for Fleur."""

from aiida import engine
from aiida import orm
from aiida.common import LinkType
from aiida_common_workflows.generators import CodeType
from ..generator import CommonBandsInputGenerator

__all__ = ('FleurCommonBandsInputGenerator',)


class FleurCommonBandsInputGenerator(CommonBandsInputGenerator):
    """Generator of inputs for the FleurCommonBandsWorkChain"""

    @classmethod
    def define(cls, spec):
        """Define the specification of the input generator.

        The ports defined on the specification are the inputs that will be accepted by the ``get_builder`` method.
        """
        super().define(spec)
        spec.inputs['engines']['bands']['code'].valid_type = CodeType('fleur.fleur')

    def _construct_builder(self, **kwargs) -> engine.ProcessBuilder:
        """Construct a process builder based on the provided keyword arguments.

        The keyword arguments will have been validated against the input generator specification.
        """
        # pylint: disable=too-many-branches,too-many-statements,too-many-locals
        engines = kwargs.get('engines', None)
        parent_folder = kwargs['parent_folder']
        bands_kpoints = kwargs['bands_kpoints']

        # From the parent folder, we retrieve the calculation that created it. Note
        # that we are sure it exists (it wouldn't be the same for WorkChains). We then check
        # that it is a FleurCalculation and create the builder.
        parent_fleur_calc = parent_folder.get_incoming(link_type=LinkType.CREATE).one().node
        if parent_fleur_calc.process_type != 'aiida.calculations:fleur.fleur':
            raise ValueError('The `parent_folder` has not been created by a FleurCalculation')
        builder_parent_fleur_calc = parent_fleur_calc.get_builder_restart()

        builder_common_bands_wc = self.process_class.get_builder()
        
        builder_common_bands_wc.options = orm.Dict(dict=builder_parent_fleur_calc._data['metadata']['options'])
        builder_common_bands_wc.fleur = builder_parent_fleur_calc._data['code']
        builder_common_bands_wc.kpoints = bands_kpoints
        builder_common_bands_wc.remote = parent_folder

        wf_parameters = {
            'kpath': 'skip'
        }

        builder_common_bands_wc.wf_parameters = orm.Dict(dict=wf_parameters)

        # Update the code and computational options if `engines` is specified
        try:
            engb = engines['bands']
        except KeyError:
            raise ValueError('The `engines` dictionaly must contain "bands" as outermost key')
        if 'code' in engb:
            builder_common_bands_wc.code = orm.load_code(engines['bands']['code'])
        if 'options' in engb:
            builder_common_bands_wc.options = orm.Dict(dict=engines['bands']['options'])

        return builder_common_bands_wc
