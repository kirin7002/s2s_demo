"""Acceptance logic only: never calls a model or moves hardware."""
import unittest

from scripts.verify_real_model_basics import assess, prior_diagnostic


class BasicAcceptanceTests(unittest.TestCase):
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
