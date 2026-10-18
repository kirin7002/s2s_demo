"""Acceptance logic only: never calls a model or moves hardware."""
import unittest
from argparse import Namespace
from pathlib import Path
import tempfile
from unittest.mock import patch

from scripts.verify_real_model_basics import (assess, correction_evidence, one_parameter_correction,
                                             prior_diagnostic, run_isolated)


class BasicAcceptanceTests(unittest.TestCase):
    def test_only_one_explicit_preflight_error_can_be_corrected(self):
        speed = {'error_code': 'OUT_OF_RANGE', 'details': {'reason': 'speed'}}
        self.assertTrue(one_parameter_correction([speed]))
        self.assertFalse(one_parameter_correction([speed, speed]))
        self.assertFalse(one_parameter_correction([{'error_code': 'MOTION_TIMEOUT'}]))
        self.assertFalse(one_parameter_correction([{'error_code': 'OUT_OF_RANGE'}]))
        invalid = {'error_code': 'INVALID_ARGUMENT',
                   'details': {'reason': 'additionalProperties', 'field': '$', 'misplaced_fields': ['hold_ms']}}
        self.assertTrue(one_parameter_correction([invalid]))
        self.assertFalse(one_parameter_correction([invalid], execution_started=True))
        self.assertFalse(one_parameter_correction([{**invalid, 'command_id': 'already-dispatched'}]))
        self.assertFalse(one_parameter_correction([{'error_code': 'ACK_TIMEOUT', 'command_id': 'unknown'}]))

    def test_preflight_correction_retains_reject_and_counts_only_one_model_attempt(self):
        reject = {'stage': 'perform_result', 'error_code': 'INVALID_ARGUMENT',
                  'details': {'reason': 'additionalProperties', 'field': '$', 'misplaced_fields': ['hold_ms']}}
        diagnostic = {k: v for k, v in reject.items() if k != 'stage'}
        accepted = {'stage': 'perform_result', 'command_id': 'corrected', 'status': 'accepted'}
        completed = {'stage': 'device_command_status', 'command_id': 'corrected', 'status': 'completed'}
        for audits, diagnostics in (([], [diagnostic]), ([reject], [diagnostic]),
                                    ([reject, accepted, completed], [diagnostic])):
            result = correction_evidence(audits, diagnostics)
            self.assertTrue(result['allowed'])
            self.assertEqual(result['attempts'], int(accepted in audits))
            self.assertEqual(result['rejection']['error_code'], 'INVALID_ARGUMENT')
        self.assertFalse(correction_evidence([reject, reject], [diagnostic, diagnostic])['allowed'])
        self.assertFalse(correction_evidence([accepted, reject], [diagnostic])['allowed'])
        self.assertFalse(correction_evidence([reject, accepted, accepted], [diagnostic])['allowed'])
        for code in ('MOTION_TIMEOUT', 'ACK_TIMEOUT', 'SCHEDULER_OVERRUN'):
            fault = {'stage': 'device_command_status', 'command_id': 'corrected', 'error_code': code}
            result = correction_evidence([reject, accepted, fault], [diagnostic])
            self.assertFalse(result['allowed'])
            self.assertEqual(result['attempts'], 1)
            self.assertEqual(result['rejection'], reject)
    def test_forward_pose_tolerance_does_not_hide_absent_motion(self):
        checks = assess({'head_axis': 'pitch', 'returns_to_center': True}, [{'head_frames': [
            {'pitch_deg': .3, 'yaw_deg': -.3}, {'pitch_deg': .3, 'yaw_deg': -.3}]}],
            {'pitch_deg': .3, 'yaw_deg': -.3})
        self.assertTrue(checks['planned_center'])
        self.assertTrue(checks['measured_center'])
        self.assertFalse(checks['requested_axis_moves'])
    def test_delayed_prior_command_error_is_not_the_next_case_failure(self):
        self.assertTrue(prior_diagnostic({'command_id': 'old'}, {'old'}))
        self.assertFalse(prior_diagnostic({'command_id': 'new'}, {'old'}))
        self.assertFalse(prior_diagnostic({'error_code': 'OUT_OF_RANGE'}, {'old'}))
    def test_separate_commands_cannot_claim_simultaneous_combination(self):
        expected = {'head_axis': 'pitch', 'expression': 'happy', 'light_color': 'green'}
        summaries = [
            {'head_frames': [{'at_ms': 0, 'pitch_deg': 5}]},
            {'screen_frames': [{'at_ms': 0, 'expression': 'happy'}]},
            {'light_frames': [{'at_ms': 0, 'rgb': [0, 255, 0], 'brightness': .1}]}]
        self.assertFalse(assess(expected, summaries, {})['combined_same_frame_time'])
        combined = {key: value for summary in summaries for key, value in summary.items()}
        self.assertTrue(assess(expected, [combined], {})['combined_same_frame_time'])

    def test_one_sided_yaw_and_single_cycle_are_not_a_double_shake(self):
        def check(values):
            return assess({'head_axis': 'yaw', 'cycles': 2}, [{'head_frames': [
                {'at_ms': i * 1000, 'yaw_deg': v} for i, v in enumerate(values)]}], {})
        self.assertFalse(check([0, 5, 0])['both_yaw_sides'])
        self.assertFalse(check([-5, 5, 0])['requested_cycles'])
        self.assertTrue(check([-5, 5, -5, 5, 0])['requested_cycles'])

    def test_rgb_holds_exclude_fades_and_end_off(self):
        frames = [{'at_ms': i * 500, 'rgb': rgb, 'brightness': .1, 'fade_ms': 100}
                  for i, rgb in enumerate(([255, 0, 0], [0, 255, 0], [0, 0, 255], [0, 0, 0]))]
        expected = {'light_colors': ['red', 'green', 'blue'], 'ends_light_off': True}
        checks = assess(expected, [{'light_frames': frames}], {})
        self.assertTrue(checks['rgb_order'])
        self.assertTrue(checks['ends_light_off'])
        self.assertFalse(checks['half_second_holds'])
        for i, frame in enumerate(frames):
            frame['at_ms'] = i * 600
        self.assertTrue(assess(expected, [{'light_frames': frames}], {})['half_second_holds'])


class IsolatedSessionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_intent_uses_a_separate_completed_child_session(self):
        with tempfile.TemporaryDirectory() as directory:
            args = Namespace(directory=Path(directory), report_name='suite.json')
            cases = [{'id': name, 'expected': {'head_axis': 'pitch'}} for name in ('nod', 'shake', 'combined')]
            seen = []
            async def child(single):
                self.assertEqual(len(single.case_ids), 1)
                seen.append((single.case_ids[0], single.report_name))
                return {'cases': [{'id': single.case_ids[0], 'passed': True}],
                        'provider_audit': [{'outputs': []}], 'control_released': True}
            with patch('scripts.verify_real_model_basics.run', side_effect=child):
                report = await run_isolated(args, cases)
            self.assertTrue(report['passed'])
            self.assertEqual(report['real_provider_connections_actual'], 3)
            self.assertEqual(seen, [('nod', 'suite-nod.json'), ('shake', 'suite-shake.json'),
                                    ('combined', 'suite-combined.json')])

    async def test_physical_failure_skips_further_head_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            args = Namespace(directory=Path(directory), report_name='suite.json')
            cases = [{'id': name, 'expected': {'head_axis': 'pitch'}} for name in ('nod', 'shake', 'combined')]
            seen = []
            async def child(single):
                seen.append(single.case_ids[0])
                return {'cases': [{'id': single.case_ids[0], 'passed': False}], 'control_released': True,
                        'provider_audit': [{'outputs': [{'error_code': 'MOTION_TIMEOUT'}]}]}
            with patch('scripts.verify_real_model_basics.run', side_effect=child):
                report = await run_isolated(args, cases)
            self.assertEqual(seen, ['nod'])
            self.assertEqual(report['real_provider_connections_actual'], 1)
            self.assertIn('not_executed', report['cases'][1])
