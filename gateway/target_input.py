"""Bounded, non-semantic container compatibility for target tool inputs.

No defaults for angles, no unknown-key removal, and no executable parsing.
The complete schema must still validate the returned object before planning.
"""
import copy
import json
import math

from .timing_planner import TimingPlanError


def input_type(value):
    if value is None:
        return 'null'
    return {dict: 'object', list: 'array', str: 'string', bool: 'boolean',
            int: 'integer', float: 'number'}.get(type(value), 'unsupported')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('nonfinite number')
    return value


def _constant(_):
    raise ValueError('nonfinite constant')


def normalize_target_input(arguments):
    """Return (unchanged-or-normalized copy, content-free metadata).

    Accepted adaptations: one explicit target step object -> singleton array;
    one strictly encoded JSON array/step object string -> its container. Array
    items, scalar fields, and unknown fields are never coerced or discarded.
    """
    if not isinstance(arguments, dict):
        raise TimingPlanError('INVALID_ARGUMENT', reason='type', field='$',
                              expected_type='object', received_type=input_type(arguments))
    try:
        size = len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise TimingPlanError('INVALID_ARGUMENT', reason='invalid_json_value', field='$') from None
    if size > 8192:
        raise TimingPlanError('MESSAGE_TOO_LARGE', reason='tool_arguments', max_bytes=8192)
    result = copy.deepcopy(arguments)
    if 'steps' not in result:
        # Leave missing properties to the common schema diagnostics.
        return result, {'input_normalized': False, 'normalizations': [], 'received_steps_type': 'missing'}
    steps = result['steps']
    received = input_type(steps)
    changes = []
    if isinstance(steps, str):
        try:
            steps = json.loads(steps, object_pairs_hook=_object, parse_float=_float, parse_constant=_constant)
        except (ValueError, TypeError, RecursionError):
            raise TimingPlanError('INVALID_ARGUMENT', reason='steps_json_encoding', field='$.steps',
                                  expected_type='array', received_type='string') from None
        changes.append('decode_steps_json_once')
    if isinstance(steps, dict) and any(key in steps for key in ('head', 'screen', 'light')):
        steps = [steps]
        changes.append('single_step_to_array')
    if not isinstance(steps, list):
        raise TimingPlanError('INVALID_ARGUMENT', reason='type', field='$.steps', expected_type='array',
                              received_type=received, decoded_type=input_type(steps))
    result['steps'] = steps
    return result, {'input_normalized': bool(changes), 'normalizations': changes,
                    'received_steps_type': received, 'normalized_steps_type': 'array'}
