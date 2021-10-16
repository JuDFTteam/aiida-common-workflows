# -*- coding: utf-8 -*-
"""Implementation of `aiida_common_workflows.common.bands.generator.CommonBandsInputGenerator` for SIESTA."""
import os

import yaml

from aiida import engine
from aiida import orm
from aiida import plugins
from aiida.common import exceptions
from aiida_common_workflows.common import ElectronicType, SpinType, seekpath_explicit_kp_path
from aiida_common_workflows.generators import ChoiceType, CodeType
from ..generator import CommonBandsInputGenerator

__all__ = ('SiestaCommonBandsInputGenerator',)

StructureData = plugins.DataFactory('structure')


class SiestaCommonBandsInputGenerator(CommonBandsInputGenerator):
    """Generator of inputs for the SiestaCommonBandsWorkChain"""

    _default_protocol = 'moderate'

    def __init__(self, *args, **kwargs):
        """Construct an instance of the input generator, validating the class attributes."""

        self._initialize_protocols()

        super().__init__(*args, **kwargs)

        def raise_invalid(message):
            raise RuntimeError('invalid protocol registry `{}`: '.format(self.__class__.__name__) + message)

        for k, v in self._protocols.items():  # pylint: disable=invalid-name

            if 'parameters' not in v:
                raise_invalid('protocol `{}` does not define the mandatory key `parameters`'.format(k))
            if 'mesh-cutoff' in v['parameters']:
                try:
                    float(v['parameters']['mesh-cutoff'].split()[0])
                    str(v['parameters']['mesh-cutoff'].split()[1])
                except (ValueError, IndexError):
                    raise_invalid(
                        'Wrong format of `mesh-cutoff` in `parameters` of protocol '
                        '`{}`. Value and units are required'.format(k)
                    )

            if 'basis' not in v:
                raise_invalid('protocol `{}` does not define the mandatory key `basis`'.format(k))

            if 'pseudo_family' not in v:
                raise_invalid('protocol `{}` does not define the mandatory key `pseudo_family`'.format(k))

    def _initialize_protocols(self):
        """Initialize the protocols class attribute by parsing them from the configuration file."""
        _filepath = os.path.join(os.path.dirname(__file__), 'protocol.yml')

        with open(_filepath) as _thefile:
            self._protocols = yaml.full_load(_thefile)

    @classmethod
    def define(cls, spec):
        """Define the specification of the input generator.

        The ports defined on the specification are the inputs that will be accepted by the ``get_builder`` method.
        """
        super().define(spec)
        spec.inputs['spin_type'].valid_type = ChoiceType((SpinType.NONE, SpinType.COLLINEAR))
        spec.inputs['electronic_type'].valid_type = ChoiceType((ElectronicType.METAL, ElectronicType.INSULATOR))
        spec.inputs['engines']['bands']['code'].valid_type = CodeType('siesta.siesta')

    def _construct_builder(self, **kwargs) -> engine.ProcessBuilder:
        """Construct a process builder based on the provided keyword arguments.

        The keyword arguments will have been validated against the input generator specification.
        """
        # pylint: disable=too-many-branches,too-many-statements,too-many-locals
        structure = kwargs['structure']
        engines = kwargs['engines']
        protocol = kwargs['protocol']
        spin_type = kwargs['spin_type']
        magnetization_per_site = kwargs.get('magnetization_per_site', None)
        seekpath_parameters = kwargs['seekpath_parameters']
        parent_folder = kwargs.get('parent_folder', None)
        bands_kpoints = kwargs.get('bands_kpoints', None)

        # Checks
        if protocol not in self.get_protocol_names():
            import warnings
            warnings.warn('no protocol implemented with name {protocol}, using default moderate')
            protocol = self.get_default_protocol_name()
        if 'bands' not in engines:
            raise ValueError('The `engines` dictionaly must contain "bands" as outermost key')

        pseudo_family = self._protocols[protocol]['pseudo_family']
        try:
            orm.Group.objects.get(label=pseudo_family)
        except exceptions.NotExistent as exc:
            raise ValueError(
                f'protocol `{protocol}` requires `pseudo_family` with name {pseudo_family} '
                'but no family with this name is loaded in the database'
            ) from exc

        # K points for bands, possible change of structure due to SeeK-Path
        if bands_kpoints is None:
            res = seekpath_explicit_kp_path(structure, orm.Dict(dict=seekpath_parameters))
            structure = res['structure']
            bandskpoints = res['kpoints']
            if parent_folder is not None:
                raise ValueError(
                    'A parent folder has been passed to trigger a restart, but SeeK-Path modified the '
                    'structure and the old DM is unusable. Define `bands_kpoints` explicitly '
                    'in order to not trigger SeeK-Path or remove the `parent_folder`.'
                )
        else:
            bandskpoints = bands_kpoints

        # K points
        kpoints_mesh = self._get_kpoints(protocol, structure)

        # Parameters, including scf ...
        parameters = self._get_param(protocol, structure)
        #... spin options (including initial magentization) ...
        if spin_type == SpinType.COLLINEAR:
            parameters['spin'] = 'polarized'
        if magnetization_per_site is not None:
            if spin_type == SpinType.NONE:
                import warnings
                warnings.warn('`magnetization_per_site` will be ignored as `spin_type` is set to SpinType.NONE')
            if spin_type == SpinType.COLLINEAR:
                in_spin_card = '\n'
                for i, magn in enumerate(magnetization_per_site):
                    in_spin_card += f' {i+1} {magn} \n'
                in_spin_card += '%endblock dm-init-spin'
                parameters['%block dm-init-spin'] = in_spin_card

        # Basis
        basis = self._get_basis(protocol, structure)

        # Pseudo fam
        pseudo_family = self._get_pseudo_fam(protocol)

        builder = self.process_class.get_builder()
        builder.structure = structure
        builder.basis = orm.Dict(dict=basis)
        builder.parameters = orm.Dict(dict=parameters)
        if kpoints_mesh:
            builder.kpoints = kpoints_mesh
        builder.pseudo_family = pseudo_family
        builder.options = orm.Dict(dict=engines['bands']['options'])
        builder.code = orm.load_code(engines['bands']['code'])
        builder.bandskpoints = bandskpoints
        if parent_folder is not None:
            builder.parent_calc_folder = parent_folder
            #Maybe impose just one scf step if this happens??????

        return builder

    def _get_param(self, key, structure):  # pylint: disable=too-many-branches,too-many-locals
        """
        Method to construct the `parameters` input. Heuristics are applied, a dictionary
        with the parameters is returned.
        """
        parameters = self._protocols[key]['parameters'].copy()

        if 'atomic_heuristics' in self._protocols[key]:  # pylint: disable=too-many-nested-blocks
            atomic_heuristics = self._protocols[key]['atomic_heuristics']

            if 'mesh-cutoff' in parameters:
                meshcut_glob = parameters['mesh-cutoff'].split()[0]
                meshcut_units = parameters['mesh-cutoff'].split()[1]
            else:
                meshcut_glob = None

            # Run through heuristics
            for kind in structure.kinds:
                need_to_apply = False
                try:
                    cust_param = atomic_heuristics[kind.symbol]['parameters']
                    need_to_apply = True
                except KeyError:
                    pass
                if need_to_apply:
                    if 'mesh-cutoff' in cust_param:
                        try:
                            cust_meshcut = float(cust_param['mesh-cutoff'].split()[0])
                        except (ValueError, IndexError) as exc:
                            raise RuntimeError(
                                'Wrong `mesh-cutoff` value for heuristc '
                                '{0} of protocol {1}'.format(kind.symbol, key)
                            ) from exc
                        if meshcut_glob is not None:
                            if cust_meshcut > float(meshcut_glob):
                                meshcut_glob = cust_meshcut
                        else:
                            meshcut_glob = cust_meshcut
                            try:
                                meshcut_units = cust_param['mesh-cutoff'].split()[1]
                            except (ValueError, IndexError) as exc:
                                raise RuntimeError(
                                    'Wrong `mesh-cutoff` units for heuristc '
                                    '{0} of protocol {1}'.format(kind.symbol, key)
                                ) from exc

            if meshcut_glob is not None:
                parameters['mesh-cutoff'] = '{0} {1}'.format(meshcut_glob, meshcut_units)

        return parameters

    def _get_basis(self, key, structure):  #pylint: disable=too-many-branches
        """
        Method to construct the `basis` input.
        Heuristics are applied, a dictionary with the basis is returned.
        """
        basis = self._protocols[key]['basis'].copy()

        if 'atomic_heuristics' in self._protocols[key]:  # pylint: disable=too-many-nested-blocks
            atomic_heuristics = self._protocols[key]['atomic_heuristics']

            pol_dict = {}
            size_dict = {}
            pao_block_dict = {}

            # Run through all the heuristics
            for kind in structure.kinds:
                need_to_apply = False
                try:
                    cust_basis = atomic_heuristics[kind.symbol]['basis']
                    need_to_apply = True
                except KeyError:
                    pass
                if need_to_apply:
                    if 'split-tail-norm' in cust_basis:
                        basis['pao-split-tail-norm'] = True
                    if 'polarization' in cust_basis:
                        pol_dict[kind.name] = cust_basis['polarization']
                    if 'size' in cust_basis:
                        size_dict[kind.name] = cust_basis['size']
                    if 'pao-block' in cust_basis:
                        pao_block_dict[kind.name] = cust_basis['pao-block']
                        if kind.name != kind.symbol:
                            pao_block_dict[kind.name] = pao_block_dict[kind.name].replace(kind.symbol, kind.name)

            if pol_dict:
                card = '\n'
                for k, value in pol_dict.items():
                    card = card + f'  {k}  {value} \n'
                card = card + '%endblock paopolarizationscheme'
                basis['%block pao-polarization-scheme'] = card
            if size_dict:
                card = '\n'
                for k, value in size_dict.items():
                    card = card + f'  {k}  {value} \n'
                card = card + '%endblock paobasissizes'
                basis['%block pao-basis-sizes'] = card
            if pao_block_dict:
                card = '\n'
                for k, value in pao_block_dict.items():
                    card = card + f'{value} \n'
                card = card + '%endblock pao-basis'
                basis['%block pao-basis'] = card

        return basis

    def _get_kpoints(self, key, structure):
        """
        Method to construct the kpoints mesh
        """
        from aiida.orm import KpointsData
        if 'kpoints' in self._protocols[key]:
            kpoints_mesh = KpointsData()
            kpoints_mesh.set_cell_from_structure(structure)
            kp_dict = self._protocols[key]['kpoints']
            if 'offset' in kp_dict:
                kpoints_mesh.set_kpoints_mesh_from_density(distance=kp_dict['distance'], offset=kp_dict['offset'])
            else:
                kpoints_mesh.set_kpoints_mesh_from_density(distance=kp_dict['distance'])
            return kpoints_mesh

        return None

    def _get_pseudo_fam(self, key):
        from aiida.orm import Str
        return Str(self._protocols[key]['pseudo_family'])
