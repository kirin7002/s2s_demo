"""Small model-facing target format, compiled into the canonical contract.

The device catalog, poses, outputs and limits remain authoritative. This layer
only derives container fields and folds explicit pauses; it never selects goals.
"""
import copy
import json

from jsonschema import Draft7Validator

from .target_input import normalize_target_input, _object, _float, _constant
from .timing_planner import TimingPlanError

FLAT_OUTPUTS = ('yaw_deg', 'pitch_deg', 'expression', 'text', 'rgb', 'brightness')
LEGACY_OUTPUTS = ('head', 'screen', 'light')


def flat_target_schema(canonical):
    """Derive scalar rules from the canonical schema, without nested unions."""
    schema = copy.deepcopy(canonical)
    source = canonical['properties']['steps']['items']['properties']
    fields = {axis: copy.deepcopy(source['head']['properties'][axis]) for axis in ('yaw_deg', 'pitch_deg')}
    fields.update({k: copy.deepcopy(source['screen']['properties'][k]) for k in ('expression', 'text')})
    fields.update({k: copy.deepcopy(source['light']['properties'][k]) for k in ('rgb', 'brightness')})
    fields['hold_ms'] = copy.deepcopy(source['hold_ms'])
    fields['expression']['description'] = 'Optional expression. Code selects the face page. Do not combine with text in this step.'
    fields['text']['description'] = 'Optional screen text. Code selects the text page. Do not combine with expression in this step.'
    fields['rgb']['description'] = 'Optional RGB target; provide brightness in the same step.'
    fields['brightness']['description'] = 'Optional brightness target; provide rgb in the same step.'
    fields['hold_ms']['description'] = ('Pause after this target settles. A separate hold-only step adds a pause to '
                                      'the preceding target without changing any output. Cannot be the first step.')
    schema['properties']['steps']['items'] = {
        'type': 'object', 'properties': fields, 'additionalProperties': False, 'minProperties': 1}
    schema['properties']['steps']['description'] = (
        'Direct JSON array of flat target objects, never a JSON-encoded string. '
        'Use yaw_deg/pitch_deg, expression OR text, rgb with brightness, and optional hold_ms. '
        'Outputs in a step start together; omitted outputs keep their targets. '
        'There are no head/screen/light wrapper objects in this model-facing format.')
    return schema


def voice_target_tool(tool):
    result = copy.deepcopy(tool)
    function = result.get('function', {})
    if function.get('name') == 'stackchan_perform_targets':
        function['parameters'] = flat_target_schema(function['parameters'])
        function['description'] = (
            'Execute freely chosen target steps on the bound robot. Use flat step fields: '
            'yaw_deg/pitch_deg, expression OR text, rgb with brightness, hold_ms. '
            'No wrapper objects or model-authored clocks. Code computes timing, page selection and interpolation. '
            'A hold-only step pauses the preceding target; no first-step pause. '
            'Choose poses, expressions, light and order freely; no fixed gesture templates. '
            'For a shake use alternating yaw targets, for a nod use pitch round trips. '
            'Directional looks retain their final pose; return_to_neutral defaults false. '
            'Only explicitly choose true when calibrated forward return is intended. '
            'Mechanical and resource limits still apply atomically. Accepted is not completed; '
            'query get_command for the terminal result. Correct rejected parameters at most once '
            'and only when retryable=true, preserving intended goals and values.')
    return result


def normalize_model_targets(arguments):
    """Keep canonical strict JSON diagnostics, also recognize a flat singleton.

    A probe uses the exact same strict decoder and never recursively decodes.
    Non-flat containers and all failures still use the canonical normalizer.
    """
    try:
        if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 8192:
            return normalize_target_input(arguments)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return normalize_target_input(arguments)
    received = arguments.get('steps') if isinstance(arguments, dict) else None
    singleton = received
    if isinstance(received, str) and len(received.encode('utf-8')) <= 8192:
        try:
            singleton = json.loads(received, object_pairs_hook=_object, parse_float=_float, parse_constant=_constant)
        except (ValueError, TypeError, RecursionError):
            return normalize_target_input(arguments)
    if isinstance(singleton, dict) and any(k in singleton for k in (*FLAT_OUTPUTS, 'hold_ms')):
        prepared = {**arguments, 'steps': [singleton]}
        normalized, metadata = normalize_target_input(prepared)
        metadata.update(input_normalized=True, received_steps_type='string' if isinstance(received, str) else 'object',
                        normalizations=(['decode_steps_json_once'] if isinstance(received, str) else []) + ['single_step_to_array'])
        return normalized, metadata
    return normalize_target_input(arguments)


def target_input_validator(canonical, arguments):
    """Choose a known schema per step, retaining legacy calls and exact paths."""
    schema = copy.deepcopy(canonical)
    flat = flat_target_schema(canonical)['properties']['steps']['items']
    nested = canonical['properties']['steps']['items']
    items = arguments.get('steps', []) if isinstance(arguments, dict) else []
    if isinstance(items, list):
        schema['properties']['steps']['items'] = [
            nested if isinstance(step, dict) and any(k in step for k in LEGACY_OUTPUTS) else flat
            for step in items[:16]]
    return Draft7Validator(schema)


def adapt_target_steps(arguments):
    """Translate validated flat outputs and merge pauses without choosing values."""
    result = copy.deepcopy(arguments)
    compiled, mapping, pauses, flat_indices = [], [], [], []
    for index, step in enumerate(arguments['steps']):
        if set(step) == {'hold_ms'}:
            if not compiled:
                raise TimingPlanError('INVALID_ARGUMENT', reason='hold_without_previous_target',
                                      field='$.steps[0]', step_index=0)
            previous = compiled[-1]
            hold = previous.get('hold_ms', 0) + step['hold_ms']
            if hold > 8000:
                raise TimingPlanError('OUT_OF_RANGE', reason='merged_hold_duration',
                                      step_index=index, required_hold_ms=hold, max_hold_ms=8000)
            previous['hold_ms'] = hold
            mapping.append(len(compiled) - 1)
            pauses.append({'step_index': index, 'target_step_index': len(compiled) - 1, 'hold_ms': step['hold_ms']})
            continue
        if any(k in step for k in LEGACY_OUTPUTS):
            target = copy.deepcopy(step)
        else:
            flat_indices.append(index)
            if 'expression' in step and 'text' in step:
                raise TimingPlanError('INVALID_ARGUMENT', reason='mutually_exclusive',
                                      field=f'$.steps[{index}]', step_index=index, fields=['expression', 'text'])
            missing = [k for k, other in (('rgb', 'brightness'), ('brightness', 'rgb'))
                       if other in step and k not in step]
            if missing:
                raise TimingPlanError('INVALID_ARGUMENT', reason='required', field=f'$.steps[{index}]',
                                      step_index=index, missing_fields=missing)
            target = {k: step[k] for k in ('hold_ms',) if k in step}
            if any(k in step for k in ('yaw_deg', 'pitch_deg')):
                target['head'] = {k: step[k] for k in ('yaw_deg', 'pitch_deg') if k in step}
            if 'expression' in step:
                target['screen'] = {'page': 'face', 'expression': step['expression']}
            if 'text' in step:
                target['screen'] = {'page': 'text', 'text': step['text']}
            if 'rgb' in step:
                target['light'] = {k: copy.deepcopy(step[k]) for k in ('rgb', 'brightness')}
        compiled.append(target)
        mapping.append(len(compiled) - 1)
    result['steps'] = compiled
    return result, {'source_input_steps': len(arguments['steps']), 'input_step_mapping': mapping,
                    'merged_holds': pauses, 'flat_step_indices': flat_indices}
