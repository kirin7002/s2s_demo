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


def _syntax_details(text, error):
    """Describe grammar only; never echo source text, keys, or string values."""
    value = text.strip()
    if not value:
        form = 'empty'
    elif value.startswith('```'):
        form = 'code_fence'
    elif (value.startswith(('[', '{')) and isinstance(error, json.JSONDecodeError)
          and error.msg == 'Expecting property name enclosed in double quotes'
          and text[error.pos:error.pos+1] == "'"):
        form = 'single_quoted_keys'
    elif (value.startswith(('[', '{')) and isinstance(error, json.JSONDecodeError)
          and error.msg == 'Expecting property name enclosed in double quotes'
          and text[error.pos:error.pos+2] == '\\"'):
        form = 'escaped_property_quotes'
    elif value.startswith(('[', '{')):
        form = 'json_like_container'
    else:
        form = 'non_container_text'
    details = {'string_form': form}
    if isinstance(error, json.JSONDecodeError):
        reasons = {'Expecting property name enclosed in double quotes': 'property_quotes',
                   'Expecting value': 'expected_value', 'Extra data': 'extra_data',
                   "Expecting ',' delimiter": 'comma_delimiter',
                   "Expecting ':' delimiter": 'colon_delimiter',
                   'Unterminated string starting at': 'unterminated_string'}
        issue = reasons.get(error.msg, 'invalid_json_syntax')
        if error.pos < len(text) and text[error.pos] in ']}' and text[:error.pos].rstrip().endswith(','):
            issue = 'trailing_comma'
        details.update(parse_issue=issue, position=error.pos)
    elif isinstance(error, RecursionError):
        details['parse_issue'] = 'excessive_nesting'
    else:
        details['parse_issue'] = {'duplicate key': 'duplicate_key', 'nonfinite number': 'nonfinite_number',
                                  'nonfinite constant': 'nonfinite_number'}.get(str(error), 'invalid_json_value')
    return details


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
        except (ValueError, TypeError, RecursionError) as error:
            raise TimingPlanError('INVALID_ARGUMENT', reason='steps_json_encoding', field='$.steps',
                                  expected_type='array', received_type='string',
                                  **_syntax_details(steps, error)) from None
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
