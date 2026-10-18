"""Deterministic model-interface checks; no provider connection or hardware."""
import copy
import json
import unittest

from gateway.action_audit import target_summary
from gateway.stackchan import PARAMETERS, schema_error
from gateway.target_interface import (adapt_target_steps, flat_target_schema, normalize_model_targets,
                                      target_input_validator)
from gateway.target_planner import compile_targets
from gateway.timing_planner import TimingPlanError

CANONICAL = PARAMETERS['stackchan_perform_targets'].schema
CAPS = {'head': {'enabled': True, 'calibrated': True, 'max_speed_deg_s': 30,
                 'yaw_range_deg': [-60, 60], 'pitch_range_deg': [-25, 25]},
        'max_frames': 16, 'max_duration_ms': 8000}
POSE = {'yaw_deg': 6, 'pitch_deg': 7}


def compile_request(arguments, pose=POSE):
    normalized, container = normalize_model_targets(arguments)
    errors = list(target_input_validator(CANONICAL, normalized).iter_errors(normalized))
    if errors:
        raise schema_error(errors)
    targets, adaptation = adapt_target_steps(normalized)
    assert not list(PARAMETERS['stackchan_perform_targets'].iter_errors(targets))
    wire, timing = compile_targets(targets, CAPS, pose)
    return targets, wire, {**container, **adaptation, **timing}


class TargetInterfaceTests(unittest.TestCase):
    def test_flat_voice_schema_is_derived_and_has_no_nested_unions_or_wrappers(self):
        saved = copy.deepcopy(CANONICAL)
        flat = flat_target_schema(CANONICAL)
        item = flat['properties']['steps']['items']
        self.assertEqual(set(item['properties']), {'yaw_deg', 'pitch_deg', 'expression', 'text', 'rgb', 'brightness', 'hold_ms'})
        self.assertFalse(any(k in json.dumps(item) for k in ('"anyOf"', '"oneOf"', '"const"')))
        self.assertEqual(item['properties']['yaw_deg']['maximum'], 60)
        self.assertEqual(item['properties']['pitch_deg']['minimum'], -25)
        self.assertEqual(CANONICAL, saved)

    def test_flat_outputs_start_together_and_independent_pause_only_extends_previous_target(self):
        args = {'steps': [{'yaw_deg': 12, 'expression': 'laughing', 'rgb': [0, 255, 0], 'brightness': .1,
                           'hold_ms': 500}, {'hold_ms': 1000}, {'pitch_deg': 2}]}
        saved = copy.deepcopy(args)
        targets, wire, meta = compile_request(args)
        first, second = wire['frames']
        self.assertEqual((first['head']['yaw_deg'], first['head']['pitch_deg']), (12, 7))
        self.assertEqual(first['screen'], {'page': 'face', 'expression': 'laughing'})
        self.assertEqual(first['light'], {'rgb': [0, 255, 0], 'brightness': .1, 'fade_ms': 150})
        self.assertEqual(second['at_ms'] - first['head']['move_ms'], 1500)
        self.assertEqual((second['head']['yaw_deg'], second['head']['pitch_deg']), (12, 2))
        self.assertEqual(meta['input_step_mapping'], [0, 0, 1])
        self.assertEqual(meta['merged_holds'], [{'step_index': 1, 'target_step_index': 0, 'hold_ms': 1000}])
        self.assertEqual(targets['steps'][0]['hold_ms'], 1500)
        self.assertEqual(args, saved)

    def test_observed_nested_target_plus_hold_failure_now_has_one_unmodified_goal(self):
        args = {'steps': [{'head': {'yaw_deg': 0, 'pitch_deg': 0},
                           'screen': {'page': 'face', 'expression': 'happy'}}, {'hold_ms': 1000}]}
        targets, wire, meta = compile_request(args)
        self.assertEqual(len(targets['steps']), 1)
        self.assertEqual(len(wire['frames']), 1)
        self.assertEqual(targets['steps'][0], {**args['steps'][0], 'hold_ms': 1000})
        self.assertEqual(meta['effective_duration_ms'], 1750)
        self.assertFalse(meta['neutral_frame_added'])

    def test_flat_text_selects_page_without_logging_content_or_moving_head(self):
        secret = 'synthetic-private-screen-text'
        targets, wire, _ = compile_request({'steps': [{'text': secret}, {'hold_ms': 1500}]}, pose=None)
        self.assertEqual(wire['frames'][0]['screen'], {'page': 'text', 'text': secret})
        self.assertNotIn('head', wire['frames'][0])
        self.assertEqual(wire['duration_ms'], 1750)
        self.assertNotIn(secret, json.dumps(target_summary({'steps': [{'text': secret}]})))
        self.assertNotIn(secret, json.dumps(target_summary(targets)))

    def test_first_hold_and_conflicting_or_incomplete_outputs_are_precise_rejections(self):
        cases = [({'steps': [{'hold_ms': 1000}]}, 'hold_without_previous_target'),
                 ({'steps': [{'expression': 'happy', 'text': 'private-content'}]}, 'mutually_exclusive'),
                 ({'steps': [{'rgb': [0, 255, 0]}]}, 'required'),
                 ({'steps': [{'brightness': .1}]}, 'required')]
        for args, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(TimingPlanError) as raised:
                compile_request(args)
            self.assertEqual(raised.exception.details['reason'], reason)
            self.assertNotIn('private-content', json.dumps(raised.exception.details))

    def test_original_types_unknown_keys_and_mixed_wrappers_still_reject(self):
        secret = 'synthetic-private-screen-text'
        for step in ({'yaw_deg': '12'}, {'yaw_deg': True}, {'expression': 'happy', secret: secret},
                     {'head': {'yaw_deg': 12}, 'expression': 'happy'}, {}, {'text': ''}):
            with self.subTest(keys=list(step)):
                normalized, _ = normalize_model_targets({'steps': [step]})
                errors = list(target_input_validator(CANONICAL, normalized).iter_errors(normalized))
                self.assertTrue(errors)
                self.assertNotIn(secret, json.dumps(schema_error(errors).result()))

    def test_array_and_singleton_containers_share_strict_non_recursive_decoding(self):
        for raw in ({'yaw_deg': 12}, '{"yaw_deg":12}', '[{"yaw_deg":12}]'):
            _, wire, _ = compile_request({'steps': raw})
            self.assertEqual(wire['frames'][0]['head']['yaw_deg'], 12)
            self.assertEqual(wire['frames'][0]['head']['pitch_deg'], 7)
        for raw in ("[{'yaw_deg':12}]", '[{"yaw_deg":1,"yaw_deg":2}]', '[{"yaw_deg":NaN}]',
                    '[{"yaw_deg":1e999}]', json.dumps('[{"yaw_deg":12}]')):
            with self.subTest(raw=raw), self.assertRaises(TimingPlanError):
                compile_request({'steps': raw})

    def test_merged_dwell_is_budgeted_exactly_and_never_clipped(self):
        args = {'steps': [{'expression': 'happy', 'hold_ms': 3000}, {'hold_ms': 4750}]}
        _, wire, _ = compile_request(args)
        self.assertEqual(wire['duration_ms'], 8000)
        args['steps'][1]['hold_ms'] = 4751
        with self.assertRaises(TimingPlanError) as raised:
            compile_request(args)
        self.assertEqual(raised.exception.details['reason'], 'generated_duration')
        args['steps'][1]['hold_ms'] = 6000
        with self.assertRaises(TimingPlanError) as raised:
            compile_request(args)
        self.assertEqual(raised.exception.details['reason'], 'merged_hold_duration')

    def test_source_count_and_physical_limits_still_bound_folded_plans(self):
        for args in ({'steps': [{'expression': 'happy'}] + [{'hold_ms': 0}]*16},
                     {'steps': [{'yaw_deg': 61}]}, {'steps': [{'pitch_deg': -26}]}):
            with self.subTest(args=args):
                normalized, _ = normalize_model_targets(args)
                errors = list(target_input_validator(CANONICAL, normalized).iter_errors(normalized))
                self.assertTrue(errors)
                self.assertEqual(schema_error(errors).code, 'OUT_OF_RANGE')


if __name__ == '__main__':
    unittest.main()
