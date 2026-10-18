"""Explicitly authorized +/-3 degree hardware checks. No USB or model network."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.stackchan_hardware import DEFAULT_DIRECTORY, private_json, request_local


class MotionFailure(Exception):
    def __init__(self, code, evidence=None):
        super().__init__(code)
        self.code, self.evidence = code, evidence


def require(condition, code, evidence=None):
    if not condition:
        raise MotionFailure(code, evidence)


def frame(at, yaw, pitch, expression='happy', rgb=None, move=1200):
    return {'at_ms': at, 'head': {'yaw_deg': yaw, 'pitch_deg': pitch, 'move_ms': move},
            'screen': {'page': 'face', 'expression': expression},
            'light': {'rgb': rgb or [0, 12, 12], 'brightness': .08, 'fade_ms': 100}}


class MotionSuite:
    def __init__(self, config, directory, report_name='hardware-motion-results.json'):
        self.config, self.root = config, Path(directory)
        require(Path(report_name).name == report_name and report_name.endswith('.json'), 'INVALID_REPORT_NAME')
        self.output = self.root / report_name
        self.report = {'started_utc': datetime.now(timezone.utc).isoformat(),
                       'model_mode': 'in_memory_mock', 'expected_simulated': False,
                       'usb_operations': 0, 'real_model_calls': 0, 'stages': [], 'feedback': [],
                       'commands': [], 'limits': {'target_abs_deg': 3, 'advertised_abs_deg': 5,
                                                'max_speed_deg_s': 10, 'tolerance_deg': 1.5},
                       'software_zero': {'yaw_deg': 0, 'pitch_deg': 0, 'bsp_pitch_center_deg': 30},
                       'aborted': False, 'cleanup': {}}
        self.armed = False
        self.tolerance = 1.5
        self.tool_count = 0

    def save(self):
        private_json(self.output, self.report)

    async def rpc(self, op, **fields):
        response = await request_local(self.config, {'op': op, **fields})
        require(response.get('ok') is True, 'LOCAL_RPC_REJECTED', {'operation': op})
        return response['result']

    async def tool(self, name, arguments):
        self.tool_count += 1
        values = await self.rpc('tool', calls=[{'name': name, 'arguments': arguments}])
        result = values[0]
        if name == 'stackchan_get_state' and result.get('online') is False:
            raise MotionFailure('DEVICE_OFFLINE_PRECONDITION' if not self.armed else 'DEVICE_DISCONNECTED_DURING_TEST', result)
        require(result.get('simulated') is False, 'NOT_REAL_DEVICE_FEEDBACK', result)
        if result.get('ok') is False or result.get('status') in {'failed', 'rejected', 'unknown'}:
            raise MotionFailure(result.get('error_code', 'DEVICE_RESULT_UNCONFIRMED'), result)
        return result

    async def pose(self, label, target=None):
        result = await self.tool('stackchan_get_state', {})
        require(result.get('online') and result.get('fresh'), 'STATE_NOT_FRESH', result)
        state = result.get('state', {})
        pose = {axis: state.get(axis) for axis in ('yaw_deg', 'pitch_deg')}
        require(all(type(v) in (int, float) and math.isfinite(v) and abs(v) <= 5 for v in pose.values()),
                'INVALID_OR_OUT_OF_RANGE_POSE', result)
        sample = {'label': label, 'at_monotonic': time.monotonic(), 'pose': pose,
                  'fresh': result['fresh'], 'age_ms': result.get('age_ms'), 'simulated': result['simulated']}
        if target is not None:
            sample['target'] = target
            sample['absolute_error_deg'] = {axis: abs(pose[axis] - target[axis]) for axis in pose}
        self.report['feedback'].append(sample)
        self.save()
        if target is not None:
            require(all(value <= self.tolerance for value in sample['absolute_error_deg'].values()),
                    'FEEDBACK_OUT_OF_TOLERANCE', sample)
        return pose

    async def execute(self, arguments, label):
        require(self.armed, 'SERVO_NOT_READY')
        require(self.tool_count < 59, 'TOOL_BUDGET_EXHAUSTED')
        for item in arguments['frames']:
            if 'head' in item:
                require(all(abs(item['head'][axis]) <= 3 for axis in ('yaw_deg', 'pitch_deg')),
                        'TEST_TARGET_LIMIT_EXCEEDED')
                require(item['head']['move_ms'] >= 1200, 'TEST_MOVE_TOO_FAST')
        result = await self.tool('stackchan_perform', arguments)
        require(result.get('status') in {'accepted', 'running', 'completed'}, 'COMMAND_NOT_ACCEPTED', result)
        self.report['commands'].append({'label': label, 'arguments': arguments, 'accepted': result})
        self.save()
        return result['command_id'], time.monotonic()

    async def completed(self, command_id):
        deadline = time.monotonic() + 2
        while True:
            result = await self.tool('stackchan_get_command', {'command_id': command_id})
            if result.get('status') == 'completed' and result.get('fresh'):
                self.report['commands'][-1]['completion'] = result
                self.save()
                return result
            require(result.get('status') in {'accepted', 'running'}, 'COMMAND_NOT_COMPLETED', result)
            require(time.monotonic() < deadline, 'COMPLETION_DEADLINE', result)
            await asyncio.sleep(.2)

    async def until(self, deadline):
        await asyncio.sleep(max(0, deadline - time.monotonic()))

    async def single(self, label, yaw, pitch, previous):
        target = {'yaw_deg': yaw, 'pitch_deg': pitch}
        arguments = {'duration_ms': 1500, 'frames': [frame(0, yaw, pitch)]}
        command, started = await self.execute(arguments, label)
        await self.until(started + .65)
        await self.pose(label + '_during')
        await self.until(started + 1.7)
        await self.completed(command)
        final = await self.pose(label + '_settled', target)
        expected_change = max(abs(target[axis] - previous[axis]) for axis in target)
        observed_change = max(abs(final[axis] - previous[axis]) for axis in target)
        require(expected_change <= self.tolerance or observed_change >= .25, 'NO_PHYSICAL_POSITION_CHANGE',
                {'before': previous, 'after': final, 'target': target})
        self.report['stages'].append({'name': label, 'passed': True, 'before': previous, 'after': final,
                                      'observed_change_deg': observed_change})
        self.save()
        return final

    async def sequence(self, axis):
        poses = [(3, 0), (-3, 0), (0, 0)] if axis == 'yaw' else [(0, 3), (0, -3), (0, 0)]
        label = 'shake_with_half_second_holds' if axis == 'yaw' else 'nod_with_half_second_holds'
        arguments = {'duration_ms': 5000, 'frames': [
            frame(0, *poses[0], 'happy', [0, 12, 12]),
            frame(1700, *poses[1], 'laughing', [12, 8, 0]),
            frame(3400, *poses[2], 'scared', [0, 0, 12])]}
        command, started = await self.execute(arguments, label)
        for index, (when, target) in enumerate(zip((1.4, 3.1, 4.8), poses)):
            await self.until(started + when)
            await self.pose(f'{label}_hold_{index}', {'yaw_deg': target[0], 'pitch_deg': target[1]})
        await self.until(started + 5.15)
        await self.completed(command)
        self.report['stages'].append({'name': label, 'passed': True, 'hold_ms': 500})
        self.save()

    async def interrupt_motion(self):
        arguments = {'duration_ms': 5500, 'frames': [frame(0, 3, 0, move=1600),
                                                     frame(3000, -3, 0, 'scared', move=1600)]}
        command, started = await self.execute(arguments, 'interrupt_motion_cancel_future_frame')
        await self.until(started + .6)
        before = await self.pose('motion_before_interrupt')
        count = (await self.rpc('status'))['voice_events'].get('interrupted', 0)
        await self.rpc('interrupt')
        deadline = time.monotonic() + 4
        while (await self.rpc('status'))['voice_events'].get('interrupted', 0) <= count:
            require(time.monotonic() < deadline, 'INTERRUPT_NOT_OBSERVED')
            await asyncio.sleep(.02)
        result = await self.tool('stackchan_get_command', {'command_id': command})
        require(result.get('status') == 'cancelled' and result.get('fresh'), 'MOTION_NOT_CANCELLED', result)
        self.report['commands'][-1]['cancelled'] = result
        await asyncio.sleep(.3)
        stopped = await self.pose('after_motion_interrupt')
        await asyncio.sleep(.4)
        stable = await self.pose('motion_stop_stability')
        require(all(abs(stable[axis] - stopped[axis]) <= self.tolerance for axis in stable), 'STOP_FEEDBACK_UNSTABLE',
                {'first': stopped, 'later': stable})
        await self.until(started + 4.9)
        later = await self.pose('after_cancelled_future_frame_deadline')
        require(all(abs(later[axis] - stopped[axis]) <= self.tolerance for axis in later),
                'CANCELLED_FRAME_APPLIED_OR_FEEDBACK_DRIFT', {'stopped': stopped, 'later': later})
        self.report['stages'].append({'name': 'interrupt_motion_cancel_future_frame', 'passed': True,
                                      'before_stop': before, 'stopped': stopped, 'later': later,
                                      'queued_frame_at_ms': 3000, 'result': result})
        self.save()

    async def run(self):
        self.save()
        try:
            await self.rpc('close_voice')
            initial = await self.tool('stackchan_get_state', {})
            caps = initial.get('capabilities', {}).get('head', {})
            require(initial.get('online') and initial.get('fresh'), 'INITIAL_STATE_NOT_FRESH', initial)
            require(caps.get('enabled') is True and caps.get('calibrated') is True
                    and caps.get('yaw_range_deg') == [-5, 5] and caps.get('pitch_range_deg') == [-5, 5]
                    and caps.get('max_speed_deg_s') == 10 and caps.get('tolerance_deg') == 1.5,
                    'CAPABILITIES_DIFFER_FROM_CONFIRMED_LIMITS', initial)
            self.report['initial_state'] = initial
            previous = await self.pose('before_arming')
            ready = self.root / 'servo-ready.json'
            private_json(ready, {'servo_ready': True, 'confirmed_utc': datetime.now(timezone.utc).isoformat(),
                                'source': 'Explicit device-coordinator confirmation and fresh physical WSS feedback',
                                'initial_protocol_pose': previous, 'capabilities': caps,
                                'nvs_zero_from_coordinator': {'yaw_raw': 468, 'pitch_raw': 573},
                                'bsp_pitch_center_deg': 30, 'authorized_test_target_abs_deg': 3})
            armed = await self.rpc('servo_ready', report_file=str(ready.resolve()))
            require(armed.get('motion_enabled') is True, 'ARM_NOT_CONFIRMED')
            self.armed = True
            self.report['servo_ready_enabled'] = True
            for label, yaw, pitch in [('yaw_positive_3', 3, 0), ('yaw_negative_3', -3, 0), ('yaw_zero', 0, 0),
                                      ('pitch_positive_3', 0, 3), ('pitch_negative_3', 0, -3), ('pitch_zero', 0, 0)]:
                previous = await self.single(label, yaw, pitch, previous)
            await self.sequence('yaw')
            await self.sequence('pitch')
            await self.interrupt_motion()
            previous = await self.pose('before_final_software_zero')
            await self.single('restore_software_zero', 0, 0, previous)
        except Exception as error:
            self.report['aborted'] = True
            self.report['outcome_classification'] = 'motion_test_aborted' if self.armed else 'precondition_not_met_no_motion'
            self.report['error'] = {'code': error.code, 'evidence': error.evidence} if isinstance(error, MotionFailure) else {
                'code': 'LOCAL_MOTION_TEST_ERROR', 'exception_type': type(error).__name__}
            try:
                self.report['emergency_stop'] = await self.tool('stackchan_stop', {})
            except Exception:
                self.report['emergency_stop'] = {'confirmed': False}
            self.report['cleanup']['further_head_commands_aborted'] = True
            self.save()
        finally:
            try:
                # Cleanup is deliberately head-free after any failure.
                arguments = {'duration_ms': 300, 'frames': [{'at_ms': 0,
                    'screen': {'page': 'face', 'expression': 'neutral'},
                    'light': {'rgb': [0, 0, 0], 'brightness': 0, 'fade_ms': 0}}]}
                result = await self.tool('stackchan_perform', arguments)
                await asyncio.sleep(.45)
                done = await self.tool('stackchan_get_command', {'command_id': result['command_id']})
                require(done.get('status') == 'completed' and done.get('fresh'), 'IDLE_RESTORE_NOT_CONFIRMED', done)
                self.report['cleanup']['neutral_lights_off'] = done
            except Exception:
                self.report['cleanup']['neutral_lights_off'] = {'confirmed': False}
            try:
                await self.rpc('close_voice')
                deadline = time.monotonic() + 5
                while True:
                    status = await self.rpc('status')
                    if not status['voice_session_active'] and not status['control_session_active']:
                        break
                    require(time.monotonic() < deadline, 'RELEASE_NOT_CONFIRMED')
                    await asyncio.sleep(.03)
                self.report['cleanup']['final_gateway_status'] = status
                self.report['cleanup']['control_released'] = True
            except Exception:
                self.report['cleanup']['control_released'] = False
            self.report['tool_calls'] = self.tool_count
            self.report['finished_utc'] = datetime.now(timezone.utc).isoformat()
            self.save()
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--directory', type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument('--report-name', default='hardware-motion-results.json')
    parser.add_argument('--wait-device-seconds', type=float, default=0)
    args = parser.parse_args()
    if not args.run:
        print('Plan only: fresh pose, +/-3 degree axis checks, paused shake/nod, motion interruption, software zero.')
        return 0
    if Path(args.report_name).name != args.report_name or not args.report_name.endswith('.json'):
        print('Invalid report name.', file=sys.stderr)
        return 2
    if (args.directory / args.report_name).exists():
        print('Existing motion report preserved. No repeat run is permitted before separate review.', file=sys.stderr)
        return 2
    config = json.loads((args.directory / 'server-config.json').read_text())
    if args.wait_device_seconds > 0:
        async def wait_for_device():
            deadline = time.monotonic() + args.wait_device_seconds
            while True:
                response = await request_local(config, {'op': 'status'})
                state = response['result']
                head = state.get('head', {})
                if (state['device_connected'] and state['simulated'] is False
                        and head.get('enabled') is True and head.get('calibrated') is True):
                    return True
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(.5)
        if not asyncio.run(wait_for_device()):
            print('Waiting for physical device; no motion test started and no action failure recorded.')
            return 2
    report = asyncio.run(MotionSuite(config, args.directory, args.report_name).run())
    print(json.dumps({'aborted': report['aborted'], 'passed_stages': len(report['stages']),
                      'error': report.get('error'), 'control_released': report['cleanup'].get('control_released'),
                      'report_file': str((args.directory / args.report_name).resolve())}, ensure_ascii=False))
    return int(report['aborted'] or not report['cleanup'].get('control_released'))


if __name__ == '__main__':
    raise SystemExit(main())
