"""Strict version-one analysis configuration; no optional numerical imports."""
import copy
import math
from pathlib import Path
import re
import yaml

from .expressions import FIELDS, FUNCTIONS, expression_names, variable_order


class _Loader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError('Analysis YAML keys must be unique strings')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def mapping(value, allowed, location):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f'{location}: expected a string-keyed mapping')
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f'{location}: unknown keys: ' + ', '.join(sorted(unknown)))
    return value


def name(value, location):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value)
            or value in FUNCTIONS or value in FIELDS or value == 'initial'):
        raise ValueError(f'{location}: invalid or reserved name')
    return value


def _finite(value):
    return (type(value) in (int, float)
            and not (type(value) is int and value.bit_length() > 128) and math.isfinite(value))


def validate_analysis_config(data):

    data = copy.deepcopy(data)
    mapping(data, ('schema_version', 'input', 'objects', 'variables', 'cuts', 'histograms'), 'analysis')
    if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise ValueError('schema_version: expected integer 1')

    source = data.setdefault('input', {})
    mapping(source, ('format', 'tree', 'weights', 'normalization'), 'input')
    if source.get('format') not in (None, 'lhe', 'root', 'lhco'):
        raise ValueError('input.format: expected lhe, root or lhco')
    source.setdefault('tree', 'Delphes')
    if source['tree'] != 'Delphes':
        raise ValueError('input.tree: only Delphes is supported')
    weights = source.setdefault('weights', {'mode': 'nominal'})
    mapping(weights, ('mode', 'branch'), 'input.weights')
    if weights.get('mode') not in ('nominal', 'unit'):
        raise ValueError('input.weights.mode: expected nominal or unit')
    if 'branch' in weights and (weights['mode'] != 'nominal' or not isinstance(weights['branch'], str)
                                or not weights['branch'].strip()):
        raise ValueError('input.weights.branch: requires nominal weights and a nonempty branch name')
    normalization = source.setdefault('normalization', {'mode': 'raw'})
    mapping(normalization, ('mode', 'cross_section_pb', 'sumw_generated', 'luminosity_pb_inverse',
                            'weight_convention'), 'input.normalization')
    mode = normalization.get('mode')
    if mode not in ('raw', 'cross_section', 'luminosity'):
        raise ValueError('input.normalization.mode: expected raw, cross_section or luminosity')
    if mode == 'raw':
        if set(normalization) != {'mode'}:
            raise ValueError('Raw normalization accepts only mode')
    else:
        for key in ('cross_section_pb', 'sumw_generated'):
            if not _finite(normalization.get(key)) or not normalization[key]:
                raise ValueError(f'input.normalization.{key}: expected a finite nonzero number')
        if normalization['cross_section_pb'] < 0:
            raise ValueError('input.normalization.cross_section_pb: expected a positive number')
        if normalization.get('weight_convention') != 'dimensionless':
            raise ValueError('Physical normalization requires explicit weight_convention: dimensionless')
        if mode == 'luminosity':
            if not _finite(normalization.get('luminosity_pb_inverse')) or normalization['luminosity_pb_inverse'] <= 0:
                raise ValueError('input.normalization.luminosity_pb_inverse: expected a positive number')
        elif 'luminosity_pb_inverse' in normalization:
            raise ValueError('luminosity_pb_inverse requires mode: luminosity')
    objects = data.setdefault('objects', {})
    if not isinstance(objects, dict):
        raise ValueError('objects: expected a mapping')
    for key, obj in objects.items():
        name(key, 'objects')
        mapping(obj, ('source', 'select', 'sort_by', 'descending'), f'objects.{key}')
        if obj.get('source') not in ('particles', 'Electron', 'Muon', 'Photon', 'Jet', 'MissingET', 'Particle', 'Tau'):
            raise ValueError(f'objects.{key}.source: unsupported collection')
        if 'select' in obj:
            expression_names(obj['select'], set(), f'objects.{key}.select', object_filter=True)
        if obj.get('sort_by', 'pt') not in FIELDS or type(obj.get('descending', True)) is not bool:
            raise ValueError(f'objects.{key}: invalid sorting options')
        obj.setdefault('sort_by', 'pt')
        obj.setdefault('descending', True)
    variables = data.get('variables')
    if not isinstance(variables, dict) or not variables:
        raise ValueError('variables: expected a nonempty mapping')
    for key in variables:
        name(key, 'variables')
        if key in objects:
            raise ValueError(f'variables.{key}: duplicates an object name')
    order = variable_order(variables, objects)
    symbols = set(variables) | set(objects)
    cuts = data.setdefault('cuts', [])
    if not isinstance(cuts, list):
        raise ValueError('cuts: expected an ordered list')

    stages = {'initial'}
    for cut in cuts:
        mapping(cut, ('name', 'expression'), 'cuts')
        key = name(cut.get('name'), 'cuts.name')
        if key in stages:
            raise ValueError(f'cuts: duplicate stage {key}')
        expression_names(cut.get('expression'), symbols, f'cuts.{key}.expression')
        stages.add(key)

    histograms = data.get('histograms')
    if not isinstance(histograms, list) or not histograms:
        raise ValueError('histograms: expected a nonempty list')

    names = set()
    for hist in histograms:
        mapping(hist, ('name', 'variable', 'stage', 'bins', 'label', 'fill'), 'histograms')
        key = name(hist.get('name'), 'histograms.name')
        if key in names:
            raise ValueError(f'histograms: duplicate name {key}')
        names.add(key)
        if hist.get('variable') not in variables:
            raise ValueError(f'histograms.{key}.variable: expected a declared variable')
        hist.setdefault('stage', cuts[-1]['name'] if cuts else 'initial')
        hist.setdefault('fill', 'events')
        if hist['stage'] not in stages or hist['fill'] not in ('events', 'objects'):
            raise ValueError(f'histograms.{key}: invalid cut stage or fill mode')
        if 'label' in hist and not isinstance(hist['label'], str):
            raise ValueError(f'histograms.{key}.label: expected text')
        bins = hist.get('bins')
        if isinstance(bins, list):
            if len(bins) < 2 or not all(_finite(x) for x in bins) or any(a >= b for a, b in zip(bins, bins[1:])):
                raise ValueError(f'histograms.{key}.bins: edges must be finite and strictly increasing')
        else:
            mapping(bins, ('count', 'min', 'max'), f'histograms.{key}.bins')
            if (type(bins.get('count')) is not int or not 1 <= bins['count'] <= 100000
                    or not _finite(bins.get('min')) or not _finite(bins.get('max')) or bins['min'] >= bins['max']):
                raise ValueError(f'histograms.{key}.bins: invalid count or range')

    return data, order


def load_analysis_config(source):

    if isinstance(source, dict):
        return validate_analysis_config(source)
    path = Path(source).expanduser().resolve(strict=True)
    try:
        data = yaml.load(path.read_text(encoding='utf-8'), Loader=_Loader)
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid analysis YAML: {exc}') from exc

    return validate_analysis_config(data)
