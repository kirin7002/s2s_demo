"""Failure-path guards with fake feedback; no sockets or hardware."""
import tempfile
from pathlib import Path
import time
import unittest

from scripts.stackchan_hardware import DEFAULT_DIRECTORY
from scripts.verify_stackchan_motion import MotionFailure, MotionSuite


class FakeMotion(MotionSuite):
    def __init__(self, directory):
        super().__init__({}, directory)
        self.attempted = []
        self.dispatched = []

    async def rpc(self, op, **fields):
        if op == 'status':
            return {'voice_session_active': False, 'control_session_active': False}
        return {'motion_enabled': True}

    async def tool(self, name, arguments):
        self.dispatched.append((name, arguments))
        if name == 'stackchan_get_state':
            return {'online': True, 'fresh': True, 'simulated': False, 'state': {'yaw_deg': 0, 'pitch_deg': 0},
                    'capabilities': {'head': {'enabled': True, 'calibrated': True, 'yaw_range_deg': [-5, 5],
                                              'pitch_range_deg': [-5, 5], 'max_speed_deg_s': 10, 'tolerance_deg': 1.5}}}
        if name == 'stackchan_stop':
            return {'ok': True, 'status': 'stopped', 'simulated': False}
        return {'ok': True, 'status': 'completed', 'fresh': True, 'simulated': False, 'command_id': 'fixture_only'}

    async def single(self, label, yaw, pitch, previous):
        self.attempted.append(label)
        raise MotionFailure('MOTION_TIMEOUT', {'status': 'failed', 'error_code': 'MOTION_TIMEOUT', 'partial_applied': True})


class FakeInterrupt(MotionSuite):
    def __init__(self, directory, changed):
        super().__init__({}, directory)
        self.changed = changed
        self.interrupted = False

    async def pose(self, label, target=None):
        return {'yaw_deg': 0 if 'baseline' in label or not self.changed else .3, 'pitch_deg': 0}

    async def execute(self, arguments, label):
        self.report['commands'].append({'label': label, 'arguments': arguments})
        # No real waits or devices; the no-motion fixture is already at deadline.
        return 'synthetic-command', time.monotonic() - (.6 if self.changed else 1.21)

    async def until(self, deadline):
        pass

    async def rpc(self, op, **fields):
        if op == 'interrupt':
            self.interrupted = True
        return {'voice_events': {'interrupted': int(self.interrupted)}}

    async def tool(self, name, arguments):
        return {'status': 'cancelled', 'fresh': True, 'simulated': False}


class MotionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        DEFAULT_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='motion-selftest-', dir=DEFAULT_DIRECTORY)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_failure_stops_and_never_dispatches_remaining_head_frames(self):
        suite = FakeMotion(Path(self.temp.name))
        report = await suite.run()
        self.assertTrue(report['aborted'])
        self.assertEqual(report['error']['code'], 'MOTION_TIMEOUT')
        self.assertEqual(suite.attempted, ['yaw_positive_3'])
        names = [name for name, _ in suite.dispatched]
        self.assertIn('stackchan_stop', names)
        for name, args in suite.dispatched:
            if name == 'stackchan_perform':
                self.assertTrue(all('head' not in frame for frame in args['frames']))
        self.assertTrue(report['cleanup']['control_released'])

    async def test_outside_authorized_target_and_short_duration_reject_before_dispatch(self):
        suite = FakeMotion(Path(self.temp.name))
        suite.armed = True
        for head in ({'yaw_deg': 3.1, 'pitch_deg': 0, 'move_ms': 1200},
                     {'yaw_deg': 3, 'pitch_deg': 0, 'move_ms': 1199}):
            with self.assertRaises(MotionFailure):
                await suite.execute({'duration_ms': 1500, 'frames': [{'at_ms': 0, 'head': head}]}, 'invalid')
        self.assertFalse(suite.dispatched)

    async def test_no_motion_is_unverified_and_cannot_pass_interrupt_stage(self):
        suite = FakeInterrupt(Path(self.temp.name), changed=False)
        with self.assertRaises(MotionFailure) as caught:
            await suite.interrupt_motion()
        self.assertEqual(caught.exception.code, 'MOTION_IN_PROGRESS_UNVERIFIED')
        self.assertFalse(suite.interrupted)
        self.assertFalse(suite.report['stages'])

    async def test_motion_baseline_is_required_before_interrupt_and_cancel_check(self):
        suite = FakeInterrupt(Path(self.temp.name), changed=True)
        await suite.interrupt_motion()
        self.assertTrue(suite.interrupted)
        observed = suite.report['stages'][0]['movement_observation']
        self.assertGreaterEqual(observed['observed_change_deg'], .25)
        self.assertLessEqual(observed['observed_elapsed_ms'], 1200)
        self.assertEqual(suite.report['stages'][0]['queued_frame_at_ms'], 3000)
