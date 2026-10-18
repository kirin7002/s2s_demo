"""Local transport/fault fixtures; no model, device credentials or hardware."""
import asyncio
import copy
import json
import unittest

from aiohttp import ClientSession, WSServerHandshakeError

from gateway.config import Config
from gateway.server import DEVICES
from gateway.stackchan import DeviceError, PARAMETERS, QWEN_TOOLS, encode, schema_error, validate_frames
from gateway.tool_calls import ToolCalls, ToolProtocolError
from tests.test_gateway import Harness


TOKEN = 'local-synthetic-device-token-00000001'
CAPABILITIES = {
    'simulated': True, 'max_frames': 16, 'max_duration_ms': 8000,
    'head': {'enabled': True, 'calibrated': True, 'yaw_range_deg': [-60, 60],
             'pitch_range_deg': [-25, 25], 'max_speed_deg_s': 120},
    'light': {'enabled': True, 'brightness_limit': .25},
    'screen': {'enabled': True, 'pages': ['face', 'text'], 'expressions': ['neutral', 'happy']},
}
SCENE = {'duration_ms': 200, 'frames': [{'at_ms': 0, 'screen': {'page': 'face', 'expression': 'happy'}}]}


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


class DeviceFixture:
    """Deliberately simple ACK fault injector, not a firmware scheduler."""
    def __init__(self, harness):
        self.h = harness
        self.events = []
        self.commands = {}
        self.ack = True
        self.capabilities = copy.deepcopy(CAPABILITIES)
        self.pose = {'yaw_deg': 0, 'pitch_deg': 0}
        self.query_enabled = True
        self.query_gate = None
        self.sync = asyncio.Event()
        self.sync.set()
        self.jobs = set()

    async def open(self):
        self.ws = await self.h.client.ws_connect('/device/v1/ws', headers={'Authorization': 'Bearer ' + TOKEN})
        await self.ws.send_json({'v': 1, 'type': 'device.hello', 'device_boot_id': 'fixture_boot',
                                 'firmware_version': 'test-only', 'capabilities': self.capabilities})
        self.connection_id = (await self.ws.receive_json(timeout=2))['connection_id']
        self.reader = asyncio.create_task(self.run())
        return self

    async def status(self, command, status, **fields):
        message = {key: command[key] for key in ('connection_id', 'control_session_id', 'generation', 'command_id')}
        message.update(v=1, type='command.status', status=status, partial_applied=False, **fields)
        self.commands[command['command_id']] = message
        await self.ws.send_json(message)

    async def sync_reply(self, message):
        await self.sync.wait()
        for record in list(self.commands.values()):
            if record['status'] in {'accepted', 'running'}:
                await self.status(record, 'cancelled')
        await self.ws.send_json({**message, 'type': 'control.synced'})

    async def run(self):
        async for frame in self.ws:
            if frame.type.name != 'TEXT':
                break
            message = json.loads(frame.data)
            self.events.append(message)
            if message['type'] == 'control.sync':
                job = asyncio.create_task(self.sync_reply(message))
                self.jobs.add(job)
                job.add_done_callback(self.jobs.discard)
            elif message['type'] == 'command.execute':
                if self.ack:
                    await self.status(message, 'accepted')
            elif message['type'] == 'device.query':
                if self.query_gate:
                    await self.query_gate.wait()
                if not self.query_enabled:
                    continue
                record = self.commands.get(message.get('command_id'), {})
                await self.ws.send_json({**message, 'type': 'device.state', 'capabilities': self.capabilities,
                                         'state': self.pose,
                                         'status': 'unknown', **record,
                                         'request_id': message['request_id'], 'type': 'device.state'})

    async def close(self):
        for job in list(self.jobs):
            job.cancel()
        await asyncio.gather(*list(self.jobs), return_exceptions=True)
        await self.ws.close()
        await self.reader


class ContractTests(unittest.TestCase):
    def test_schema_diagnostics_identify_fields_without_rejected_content(self):
        secret = 'synthetic-private-screen-text'
        cases = [({**SCENE, 'duration_ms': secret}, 'type', '$.duration_ms'),
                 ({**SCENE, secret: secret}, 'additionalProperties', '$'),
                 ({'duration_ms': 500, 'frames': [{'at_ms': 0, 'head': {
                     'yaw_deg': 0, 'pitch_deg': 0}}]}, 'required', '$.frames[0].head')]
        for args, reason, field in cases:
            with self.subTest(reason=reason):
                result = schema_error(list(PARAMETERS['stackchan_perform'].iter_errors(args))).result()
                self.assertEqual(result['error_code'], 'INVALID_ARGUMENT')
                self.assertEqual(result['details']['reason'], reason)
                self.assertEqual(result['details']['field'], field)
                self.assertNotIn(secret, json.dumps(result))

    def test_cross_field_invalid_argument_identifies_frame_and_order(self):
        scene = {'duration_ms': 1000, 'frames': [SCENE['frames'][0], SCENE['frames'][0]]}
        with self.assertRaises(DeviceError) as raised:
            validate_frames(scene, CAPABILITIES)
        result = raised.exception.result()
        self.assertEqual(result['details']['reason'], 'frame_order')
        self.assertEqual(result['details']['frame_index'], 1)
        self.assertEqual(result['details']['field'], 'at_ms')

    def test_speed_diagnostics_use_adjacent_targets_and_max_axis(self):
        caps = copy.deepcopy(CAPABILITIES)
        caps['head']['max_speed_deg_s'] = 30
        args = {'duration_ms': 3000, 'frames': [
            {'at_ms': 0, 'head': {'yaw_deg': -20, 'pitch_deg': 0, 'move_ms': 1000}},
            {'at_ms': 1000, 'head': {'yaw_deg': 20, 'pitch_deg': 5, 'move_ms': 500}}]}
        original = copy.deepcopy(args)
        with self.assertRaises(DeviceError) as raised:
            validate_frames(args, caps, initial_pose={'yaw_deg': 0, 'pitch_deg': 0})
        result = raised.exception.result()
        self.assertEqual(result['error_code'], 'OUT_OF_RANGE')
        self.assertEqual(result['details']['reason'], 'speed')
        self.assertEqual(result['details']['frame_index'], 1)
        self.assertEqual(result['details']['minimum_move_ms'], 1334)
        self.assertEqual(result['details']['recommended_move_ms'], 1584)
        self.assertEqual(args, original)
        args['frames'][1]['head']['move_ms'] = 1334
        validate_frames(args, caps, initial_pose={'yaw_deg': 0, 'pitch_deg': 0})
        args['frames'][1]['head']['move_ms'] = 1333
        with self.assertRaises(DeviceError):
            validate_frames(args, caps)

    def test_boundary_sequences_and_schema_rejection(self):
        validator = PARAMETERS['stackchan_perform']
        for duration in (100, 8000):
            args = {**SCENE, 'duration_ms': duration}
            self.assertTrue(validator.is_valid(args))
            validate_frames(args, CAPABILITIES)
        for args in ({**SCENE, 'duration_ms': 99}, {**SCENE, 'duration_ms': 8001},
                     {**SCENE, 'frames': SCENE['frames'] * 17}, {**SCENE, 'device_id': 'arbitrary'}):
            self.assertFalse(validator.is_valid(args))

    def test_channel_overlap_calibration_and_expression_subset(self):
        head = {'yaw_deg': 0, 'pitch_deg': 0, 'move_ms': 200}
        bad = [
            ({**SCENE, 'frames': [{**SCENE['frames'][0], 'at_ms': 1}]}, CAPABILITIES, 'INVALID_ARGUMENT'),
            ({'duration_ms': 500, 'frames': [{'at_ms': 0, 'head': head}, {'at_ms': 100, 'head': head}]},
             CAPABILITIES, 'INVALID_ARGUMENT'),
            ({**SCENE, 'frames': [{'at_ms': 0, 'head': head}]},
             {**CAPABILITIES, 'head': {**CAPABILITIES['head'], 'calibrated': False}}, 'NOT_CALIBRATED'),
            ({**SCENE, 'frames': [{'at_ms': 0, 'screen': {'page': 'face', 'expression': 'scared'}}]},
             CAPABILITIES, 'UNSUPPORTED_CAPABILITY'),
        ]
        for arguments, caps, code in bad:
            with self.subTest(code=code), self.assertRaises(DeviceError) as raised:
                validate_frames(arguments, caps)
            self.assertEqual(raised.exception.code, code)

    def test_actual_utf8_budget_and_nonfinite_numbers(self):
        self.assertEqual(len(encode({'x': '😀' * 2046}).encode()), 8192)
        with self.assertRaises(DeviceError) as raised:
            encode({'x': '😀' * 2047})
        self.assertEqual(raised.exception.code, 'MESSAGE_TOO_LARGE')
        with self.assertRaises(DeviceError):
            encode({'x': float('nan')})

    def test_tools_do_not_change_audio_profile_or_expose_secrets(self):
        config = Config(stackchan_device_token=TOKEN)
        base, tools = config.session_update()['session'], config.session_update(QWEN_TOOLS)['session']
        for key in base:
            if key != 'instructions':
                self.assertEqual(base[key], tools[key])
        self.assertEqual(len(tools['tools']), 4)
        self.assertEqual({tool['function']['name'] for tool in tools['tools']}, {
            'stackchan_get_state', 'stackchan_perform_targets', 'stackchan_get_command', 'stackchan_stop'})
        self.assertEqual(len(QWEN_TOOLS), 5)
        self.assertIn('模拟', tools['instructions'])
        for text in ('stackchan_perform_targets', 'return_to_neutral=true', '不计算速度', '20个中文字符', '不可猜成锁定'):
            self.assertIn(text, tools['instructions'])
        for text in ('定向看/转向默认保持最终目标', '明确设置return_to_neutral=false',
                     'hold_ms结束不意味着恢复', '允许自由选择上下文恰当的头部姿态配合表情与灯光'):
            self.assertIn(text, tools['instructions'])
        self.assertNotIn('仅表情或灯光不得添加head', tools['instructions'])
        self.assertNotIn('未要求改变的输出不要擅自改动', tools['instructions'])
        perform = next(t for t in QWEN_TOOLS if t['function']['name'] == 'stackchan_perform')
        self.assertIn('timing_mode defaults to auto', perform['function']['description'])
        self.assertNotIn(TOKEN, repr(config) + json.dumps(tools))


class DeviceGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = await Harness(stackchan_enabled=True, stackchan_device_id='demo_robot',
                               stackchan_device_token=TOKEN, device_ack_seconds=.15,
                               device_keepalive_seconds=.05).open()
        self.hub = self.h.app[DEVICES]
        self.device = await DeviceFixture(self.h).open()
        await self.h.client.post('/api/stackchan/bind', json={'device_id': 'demo_robot'},
                                 headers={'Origin': self.h.cfg.origin})
        self.owner = next(iter(self.hub.bindings))
        self.control = self.hub.for_owner(self.owner)

    async def asyncTearDown(self):
        await self.control.close()
        await self.device.close()
        await self.h.close()

    async def test_read_only_probe_rejects_physical_perform_before_control(self):
        object.__setattr__(self.h.cfg, 'stackchan_read_only', True)
        result = await self.control.execute('stackchan_perform', SCENE)
        self.assertEqual(result['error_code'], 'NOT_READY')
        self.assertIsNone(self.hub.control)
        self.assertFalse(self.hub.records)

    async def test_auto_compiles_reported_speed_and_duration_failures_before_wire(self):
        object.__setattr__(self.h.cfg, 'stackchan_audit_enabled', True)
        self.device.capabilities['head']['max_speed_deg_s'] = 30
        args = {'duration_ms': 2500, 'frames': [
            {'at_ms': 0, 'head': {'yaw_deg': 0, 'pitch_deg': 0, 'move_ms': 1000}},
            {'at_ms': 1000, 'head': {'yaw_deg': 20, 'pitch_deg': 0, 'move_ms': 900}},
            {'at_ms': 1900, 'head': {'yaw_deg': -20, 'pitch_deg': 0, 'move_ms': 900},
             'screen': {'page': 'text', 'text': 'synthetic-private-screen-text'}}]}
        saved = copy.deepcopy(args)
        with self.assertLogs('gateway.stackchan', level='WARNING') as audit:
            result = await self.control.execute('stackchan_perform', args)
            command = next(e for e in self.device.events if e['type'] == 'command.execute')
            await self.device.status(command, 'completed')
            await until(lambda: self.hub.records[result['command_id']]['status'] == 'completed')
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['timing_mode'], 'auto')
        self.assertTrue(result['timing_adjusted'])
        self.assertEqual(result['effective_duration_ms'], 3501)
        wire = command['args']
        self.assertEqual(wire['frames'][2]['head']['move_ms'], 1584)
        self.assertEqual([f['at_ms'] for f in wire['frames']], [0, 1000, 1917])
        self.assertNotIn('timing_mode', wire)
        self.assertEqual(args, saved)
        record = self.hub.records[result['command_id']]
        self.assertEqual(record['effective_args'], wire)
        self.assertEqual(record['action_summary']['duration_ms'], 3501)
        self.assertEqual(record['requested_summary']['duration_ms'], 2500)
        self.assertIn('requested_summary', '\n'.join(audit.output))
        self.assertIn('effective_summary', '\n'.join(audit.output))
        self.assertNotIn('synthetic-private-screen-text', '\n'.join(audit.output))
        query = await self.control.execute('stackchan_get_command', {'command_id': result['command_id']})
        self.assertEqual(query['status'], 'completed')
        self.assertEqual(query['frame_timing'], result['frame_timing'])

    async def test_auto_repairs_frames_starting_beyond_original_duration(self):
        args = {'duration_ms': 1500, 'frames': [
            {'at_ms': 0, 'screen': {'page': 'face', 'expression': 'happy'}},
            {'at_ms': 2000, 'screen': {'page': 'face', 'expression': 'neutral'}}]}
        result = await self.control.execute('stackchan_perform', args)
        self.assertEqual(result['status'], 'accepted')
        self.assertTrue(result['timing_adjusted'])
        self.assertEqual(result['effective_duration_ms'], 2001)
        await self.control.execute('stackchan_stop', {})
        strict = await self.control.execute('stackchan_perform', {**args, 'timing_mode': 'strict'})
        self.assertEqual(strict['error_code'], 'INVALID_ARGUMENT')
        self.assertEqual(strict['details']['reason'], 'frame_outside_duration')
        self.assertEqual(strict['details']['frame_index'], 1)

    async def test_auto_resource_failure_is_atomic_and_does_not_clip_targets(self):
        self.device.capabilities['head']['max_speed_deg_s'] = 30
        args = {'duration_ms': 8000, 'frames': [{'at_ms': 0,
                 'head': {'yaw_deg': 20, 'pitch_deg': 0, 'move_ms': 100}}]}
        saved = copy.deepcopy(args)
        result = await self.control.execute('stackchan_perform', args)
        self.assertEqual(result['error_code'], 'OUT_OF_RANGE')
        self.assertEqual(result['details']['reason'], 'planned_duration')
        self.assertEqual(args, saved)
        self.assertFalse(self.hub.records)
        self.assertFalse(any(e['type'] == 'command.execute' for e in self.device.events))

    async def test_auto_uses_fresh_start_and_preserves_same_frame_outputs(self):
        self.device.capabilities['head']['max_speed_deg_s'] = 30
        self.device.pose = {'yaw_deg': -20, 'pitch_deg': 0}
        args = {'duration_ms': 500, 'frames': [{'at_ms': 0,
            'head': {'yaw_deg': 20, 'pitch_deg': 0, 'move_ms': 500},
            'screen': {'page': 'face', 'expression': 'happy'},
            'light': {'rgb': [0, 255, 0], 'brightness': .2, 'fade_ms': 100}}]}
        result = await self.control.execute('stackchan_perform', args)
        self.assertEqual(result['effective_duration_ms'], 1584)
        command = next(e for e in self.device.events if e['type'] == 'command.execute')
        frame = command['args']['frames'][0]
        self.assertEqual(frame['at_ms'], 0)
        self.assertEqual(frame['screen'], args['frames'][0]['screen'])
        self.assertEqual(frame['light'], args['frames'][0]['light'])

    async def test_free_targets_share_wire_path_and_append_exact_neutral(self):
        self.device.capabilities['head']['max_speed_deg_s'] = 30
        self.device.pose = {'yaw_deg': -.3, 'pitch_deg': -2.2}
        args = {'steps': [
            {'head': {'yaw_deg': -17, 'pitch_deg': 3}, 'hold_ms': 500,
             'screen': {'page': 'face', 'expression': 'happy'},
             'light': {'rgb': [0, 255, 0], 'brightness': .2}},
            {'head': {'yaw_deg': 12, 'pitch_deg': 9}}], 'return_to_neutral': True}
        saved = copy.deepcopy(args)
        result = await self.control.execute('stackchan_perform_targets', args)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['source_tool'], 'stackchan_perform_targets')
        self.assertEqual(result['planning_mode'], 'targets')
        self.assertTrue(result['neutral_frame_added'])
        command = next(e for e in self.device.events if e['type'] == 'command.execute')
        first, second, last = command['args']['frames']
        self.assertEqual(second['at_ms'] - first['head']['move_ms'], 500)
        self.assertEqual(first['screen'], args['steps'][0]['screen'])
        self.assertEqual(first['light']['rgb'], [0, 255, 0])
        self.assertEqual((last['head']['yaw_deg'], last['head']['pitch_deg']), (0, 0))
        self.assertNotIn('steps', command['args'])
        self.assertEqual(args, saved)
        record = self.hub.records[result['command_id']]
        self.assertEqual(record['effective_args'], command['args'])
        self.assertNotIn('at_ms', record['requested_summary']['steps'][0])
        self.assertEqual(record['action_summary']['head_frames'][-1]['yaw_deg'], 0)

    async def test_free_targets_long_leg_inserts_collinear_frames_in_one_command(self):
        self.device.capabilities['head']['max_speed_deg_s'] = 30
        self.device.pose = {'yaw_deg': -60, 'pitch_deg': -20}
        result = await self.control.execute('stackchan_perform_targets', {
            'pace': 'brisk', 'steps': [{'head': {'yaw_deg': 60, 'pitch_deg': 20}}]})
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['generated_frames'], 2)
        commands = [e for e in self.device.events if e['type'] == 'command.execute']
        self.assertEqual(len(commands), 1)
        first, last = commands[0]['args']['frames']
        self.assertEqual((first['head']['yaw_deg'], first['head']['pitch_deg']), (0, 0))
        self.assertEqual((last['head']['yaw_deg'], last['head']['pitch_deg']), (60, 20))
        self.assertEqual(result['effective_duration_ms'], 4500)

    async def test_free_targets_hold_over_two_seconds_and_atomic_budget_rejection(self):
        args = {'steps': [{'screen': {'page': 'face', 'expression': 'happy'}, 'hold_ms': 3000}]}
        result = await self.control.execute('stackchan_perform_targets', args)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['effective_duration_ms'], 3250)
        await self.control.execute('stackchan_stop', {})
        too_long = {'steps': [{**args['steps'][0], 'hold_ms': 8000}]}
        result = await self.control.execute('stackchan_perform_targets', too_long)
        self.assertEqual(result['error_code'], 'OUT_OF_RANGE')
        self.assertEqual(result['details']['reason'], 'generated_duration')
        self.assertEqual(sum(e['type'] == 'command.execute' for e in self.device.events), 1)

    async def test_free_targets_still_reject_unknown_fields_without_echo(self):
        secret = 'synthetic-private-screen-text'
        result = await self.control.execute('stackchan_perform_targets', {'steps': [{
            'head': {'yaw_deg': 0, 'pitch_deg': 1, secret: secret}}]})
        self.assertEqual(result['error_code'], 'INVALID_ARGUMENT')
        self.assertEqual(result['details']['field'], '$.steps[0].head')
        self.assertEqual(result['details']['step_index'], 0)
        self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(any(e['type'] == 'command.execute' for e in self.device.events))

    async def test_first_head_frame_uses_new_query_not_cached_pose(self):
        self.device.pose = {'yaw_deg': -50, 'pitch_deg': 0}
        scene = {'timing_mode': 'strict', 'duration_ms': 1000, 'frames': [{'at_ms': 0,
                 'head': {'yaw_deg': 50, 'pitch_deg': 0, 'move_ms': 200}}]}
        result = await self.control.execute('stackchan_perform', scene)
        self.assertEqual(result['details']['reason'], 'speed')
        self.assertEqual(result['details']['frame_index'], 0)
        self.assertEqual(result['details']['minimum_move_ms'], 834)
        self.assertFalse(any(e['type'] == 'command.execute' for e in self.device.events))
        self.device.pose = {'yaw_deg': 49, 'pitch_deg': 0}
        result = await self.control.execute('stackchan_perform', scene)
        self.assertEqual(result['status'], 'accepted')

    async def test_head_needs_fresh_pose_but_screen_does_not(self):
        self.device.query_enabled = False
        scene = {'duration_ms': 1000, 'frames': [{'at_ms': 0,
                 'head': {'yaw_deg': 1, 'pitch_deg': 0, 'move_ms': 500}}]}
        result = await self.control.execute('stackchan_perform', scene)
        self.assertEqual(result['error_code'], 'NOT_READY')
        self.assertEqual(result['details']['reason'], 'fresh_pose_unavailable')
        self.assertFalse(any(e['type'] == 'command.execute' for e in self.device.events))
        self.assertEqual((await self.control.execute('stackchan_perform', SCENE))['status'], 'accepted')

    async def test_hardware_fault_blocks_model_head_retry_even_after_stop(self):
        scene = {'duration_ms': 1000, 'frames': [{'at_ms': 0,
                 'head': {'yaw_deg': 1, 'pitch_deg': 0, 'move_ms': 500}}]}
        first = await self.control.execute('stackchan_perform', scene)
        command = next(e for e in self.device.events if e.get('command_id') == first['command_id'])
        await self.device.status(command, 'failed', error_code='SCHEDULER_OVERRUN')
        await until(lambda: self.control.motion_fault == 'SCHEDULER_OVERRUN')
        await self.control.execute('stackchan_stop', {})
        retry = await self.control.execute('stackchan_perform', scene)
        self.assertEqual(retry['error_code'], 'NOT_READY')
        self.assertEqual(retry['details']['reason'], 'head_motion_fault_latched')
        target_retry = await self.control.execute('stackchan_perform_targets', {
            'steps': [{'head': {'yaw_deg': 1, 'pitch_deg': 0}}]})
        self.assertEqual(target_retry['details']['reason'], 'head_motion_fault_latched')
        self.assertEqual(sum(e['type'] == 'command.execute' for e in self.device.events), 1)
        state = await self.control.execute('stackchan_get_state', {})
        self.assertEqual(state['head_motion_fault'], 'SCHEDULER_OVERRUN')
        self.assertEqual((await self.control.execute('stackchan_perform', SCENE))['status'], 'accepted')

    async def test_stop_during_pose_query_fences_preflight_action(self):
        await self.control.ensure()
        self.device.query_gate = asyncio.Event()
        scene = {'duration_ms': 1000, 'frames': [{'at_ms': 0,
                 'head': {'yaw_deg': 1, 'pitch_deg': 0, 'move_ms': 500}}]}
        task = asyncio.create_task(self.control.execute('stackchan_perform', scene))
        await until(lambda: any(e['type'] == 'device.query' for e in self.device.events))
        fence = asyncio.create_task(self.control.fence())
        self.device.query_gate.set()
        result = await task
        await fence
        self.assertEqual(result['error_code'], 'STALE_CONTEXT')
        self.assertFalse(any(e['type'] == 'command.execute' for e in self.device.events))

    async def test_resource_target_and_timeline_errors_are_distinct(self):
        cases = [({**SCENE, 'duration_ms': 8001}, 'duration', None),
                 ({**SCENE, 'frames': SCENE['frames'] * 17}, 'frame_count', None),
                 ({'duration_ms': 1000, 'frames': [{'at_ms': 0, 'head': {
                     'yaw_deg': 61, 'pitch_deg': 0, 'move_ms': 500}}]}, 'target', 0),
                 ({'timing_mode': 'strict', 'duration_ms': 200, 'frames': [{'at_ms': 0, 'head': {
                     'yaw_deg': 1, 'pitch_deg': 0, 'move_ms': 500}}]}, 'duration', 0)]
        for args, reason, frame in cases:
            result = await self.control.execute('stackchan_perform', args)
            self.assertEqual(result['error_code'], 'OUT_OF_RANGE')
            self.assertEqual(result['details']['reason'], reason)
            self.assertEqual(result['details']['frame_index'], frame)
        malformed = await self.control.execute('stackchan_perform', {**SCENE, 'extra': 'private'})
        self.assertEqual(malformed['error_code'], 'INVALID_ARGUMENT')
        self.assertFalse(any(e['type'] == 'command.execute' for e in self.device.events))

    async def test_live_test_target_limit_rejects_above_three_degrees(self):
        object.__setattr__(self.h.cfg, 'stackchan_test_max_deg', 3)
        scene = {'duration_ms': 1500, 'frames': [{'at_ms': 0,
                 'head': {'yaw_deg': 3.1, 'pitch_deg': 0, 'move_ms': 1200}}]}
        result = await self.control.execute('stackchan_perform', scene)
        self.assertEqual(result['error_code'], 'OUT_OF_RANGE')
        self.assertIsNone(self.hub.control)
        self.assertFalse(self.hub.records)

    async def test_action_audit_records_completion_without_screen_text(self):
        object.__setattr__(self.h.cfg, 'stackchan_audit_enabled', True)
        scene = {'duration_ms': 200, 'frames': [{'at_ms': 0,
                 'screen': {'page': 'text', 'text': 'synthetic-private-screen-text'}}]}
        with self.assertLogs('gateway.stackchan', level='WARNING') as audit:
            result = await self.control.execute('stackchan_perform', scene)
            command = next(e for e in self.device.events if e['type'] == 'command.execute')
            await self.device.status(command, 'completed')
            await until(lambda: self.hub.records[result['command_id']]['status'] == 'completed')
        log = '\n'.join(audit.output)
        self.assertIn('completed', log)
        self.assertIn('frame_count', log)
        self.assertNotIn('synthetic-private-screen-text', log)
        self.assertNotIn(TOKEN, log)

    async def test_binding_login_csrf_and_device_auth(self):
        async with ClientSession() as visitor:
            result = await visitor.get(self.h.client.make_url('/api/stackchan'))
            self.assertEqual(result.status, 401)
        for body, origin, status in [({'device_id': 'other'}, self.h.cfg.origin, 403),
                                      ({'device_id': 'demo_robot'}, 'https://untrusted.invalid', 403)]:
            result = await self.h.client.post('/api/stackchan/bind', json=body, headers={'Origin': origin})
            self.assertEqual(result.status, status)
        for headers, status in [({}, 401), ({'Authorization': 'Bearer ' + TOKEN, 'Origin': self.h.cfg.origin}, 403)]:
            with self.assertRaises(WSServerHandshakeError) as raised:
                await self.h.client.ws_connect('/device/v1/ws', headers=headers)
            self.assertEqual(raised.exception.status, status)
        body = await (await self.h.client.get('/api/stackchan')).text()
        self.assertNotIn(TOKEN, body)
        self.assertTrue(json.loads(body)['bound'])

    async def test_real_ack_fresh_query_and_simulated_on_all_results(self):
        accepted = await self.control.execute('stackchan_perform', SCENE)
        self.assertEqual(accepted['status'], 'accepted')
        command = next(m for m in self.device.events if m['type'] == 'command.execute')
        await self.device.status(command, 'completed')
        result = await self.control.execute('stackchan_get_command', {'command_id': accepted['command_id']})
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['fresh'])
        for name, args in [('stackchan_get_state', {}), ('stackchan_stop', {}),
                           ('stackchan_perform', {'duration_ms': 1}),
                           ('stackchan_get_command', {'command_id': 'unknown'})]:
            with self.subTest(name=name):
                self.assertIs((await self.control.execute(name, args))['simulated'], True)
        self.assertIs(accepted['simulated'], True)

    async def test_ack_timeout_is_unknown_and_never_retried(self):
        self.device.ack = False
        result = await self.control.execute('stackchan_perform', SCENE)
        self.assertEqual((result['status'], result['error_code']), ('unknown', 'ACK_TIMEOUT'))
        self.assertIs(result['simulated'], True)
        await asyncio.sleep(.2)
        self.assertEqual(sum(m['type'] == 'command.execute' for m in self.device.events), 1)
        await self.device.status(next(m for m in self.device.events if m['type'] == 'command.execute'), 'completed')
        query = await self.control.execute('stackchan_get_command', {'command_id': result['command_id']})
        self.assertEqual(query['status'], 'completed')

    async def test_reconnect_uses_fresh_context_without_action_replay(self):
        accepted = await self.control.execute('stackchan_perform', SCENE)
        old_connection = self.device.connection_id
        old_control = self.control.id
        await self.device.close()
        await until(lambda: self.hub.connection is None)
        offline = await self.control.execute('stackchan_perform', SCENE)
        self.assertEqual(offline['error_code'], 'DEVICE_OFFLINE')
        self.assertIsNone(offline['simulated'])
        self.device = await DeviceFixture(self.h).open()
        self.assertNotEqual(self.device.connection_id, old_connection)
        query = await self.control.execute('stackchan_get_command', {'command_id': accepted['command_id']})
        self.assertEqual(query['status'], 'unknown')
        self.assertIs(query['simulated'], True)
        self.assertFalse(any(m['type'] == 'command.execute' for m in self.device.events))
        fresh = await self.control.execute('stackchan_perform', SCENE)
        self.assertEqual(fresh['status'], 'accepted')
        self.assertNotEqual(self.control.id, old_control)
        self.assertEqual(self.control.generation, 1)

    async def test_unconfirmed_hangup_closes_device_transport(self):
        await self.control.ensure()
        self.device.sync.clear()
        await self.control.close()
        await until(lambda: self.hub.connection is None)
        self.assertIsNone(self.hub.control)

    async def test_stop_gate_and_generation_do_not_allow_old_dispatch(self):
        await self.control.ensure()
        generation = self.control.generation
        self.device.sync.clear()
        stop = asyncio.create_task(self.control.execute('stackchan_stop', {}))
        await until(lambda: self.control.generation > generation)
        denied = await self.control.execute('stackchan_perform', SCENE)
        self.assertEqual(denied['error_code'], 'NOT_READY')
        self.assertFalse(any(m['type'] == 'command.execute' for m in self.device.events))
        self.device.sync.set()
        self.assertEqual((await stop)['status'], 'stopped')
        self.assertEqual((await self.control.execute('stackchan_perform', SCENE))['status'], 'accepted')
        self.assertEqual(self.device.events[-1]['generation'], generation + 1)

    async def test_stop_timeout_requires_new_confirmation(self):
        await self.control.ensure()
        self.device.sync.clear()
        self.assertEqual((await self.control.execute('stackchan_stop', {}))['error_code'], 'ACK_TIMEOUT')
        self.assertEqual((await self.control.execute('stackchan_perform', SCENE))['error_code'], 'NOT_READY')
        self.device.sync.set()
        self.assertTrue((await self.control.execute('stackchan_stop', {}))['ok'])

    async def test_device_errors_and_cancel_reasons_are_preserved(self):
        for status, error in [('failed', 'SCHEDULER_OVERRUN'), ('cancelled', 'LEASE_EXPIRED'), ('cancelled', 'LOCAL_STOP')]:
            accepted = await self.control.execute('stackchan_perform', SCENE)
            command = next(m for m in self.device.events if m.get('command_id') == accepted['command_id'])
            await self.device.status(command, status, error_code=error)
            result = await self.control.execute('stackchan_get_command', {'command_id': accepted['command_id']})
            self.assertEqual(result['error_code'], error)
            self.assertEqual(result['status'], status)

    async def test_exclusive_control_revoke_and_keepalive(self):
        await self.control.ensure()
        second = self.hub.for_owner(self.owner)
        self.assertEqual((await second.execute('stackchan_perform', SCENE))['error_code'], 'DEVICE_BUSY')
        await until(lambda: any(m['type'] == 'control.keepalive' for m in self.device.events))
        await self.hub.revoke(self.owner)
        self.assertTrue(self.control.closed)
        self.assertFalse(self.device.events[-1]['active'])
        self.assertEqual((await self.control.execute('stackchan_perform', SCENE))['error_code'], 'NOT_READY')

    async def test_full_envelope_limit_rejects_without_dispatch(self):
        # Find a schema-valid body that fits alone but exceeds with routing IDs.
        arguments = None
        for length in range(1, 81):
            candidate = {'timing_mode': 'strict', 'duration_ms': 8000, 'frames': [
                {'at_ms': i * 500, 'head': {'yaw_deg': .123456789012345, 'pitch_deg': .123456789012345, 'move_ms': 100},
                 'light': {'rgb': [0, 0, 0], 'brightness': .123456789012345, 'fade_ms': 0},
                 'screen': {'page': 'text', 'text': '😀' * length}} for i in range(16)]}
            size = len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':')).encode())
            if 8050 < size <= 8192:
                arguments = candidate
                break
        self.assertIsNotNone(arguments)
        self.assertTrue(PARAMETERS['stackchan_perform'].is_valid(arguments))
        result = await self.control.execute('stackchan_perform', arguments)
        self.assertEqual(result['error_code'], 'MESSAGE_TOO_LARGE')
        self.assertFalse(any(m['type'] == 'command.execute' for m in self.device.events))

    async def test_malformed_status_and_late_terminal_do_not_corrupt_state(self):
        accepted = await self.control.execute('stackchan_perform', SCENE)
        command = next(m for m in self.device.events if m['type'] == 'command.execute')
        await self.device.ws.send_json({**command, 'type': 'command.status', 'status': {}, 'partial_applied': False})
        await self.device.status(command, 'completed')
        await until(lambda: self.hub.records[accepted['command_id']]['status'] == 'completed')
        await self.device.status(command, 'running')
        await asyncio.sleep(.02)
        self.assertEqual(self.hub.records[accepted['command_id']]['status'], 'completed')

    async def test_tools_registered_only_after_authorized_binding(self):
        voice = await self.h.socket()
        self.assertEqual((await voice.receive_json(timeout=2))['type'], 'ready')
        session = (await self.h.events.get())['session']
        self.assertEqual({t['function']['name'] for t in session['tools']}, {
            'stackchan_get_state', 'stackchan_perform_targets', 'stackchan_get_command', 'stackchan_stop'})
        await voice.close()
        await self.h.client.post('/api/stackchan/bind', json={'device_id': None},
                                 headers={'Origin': self.h.cfg.origin})
        voice = await self.h.socket()
        self.assertEqual((await voice.receive_json(timeout=2))['type'], 'ready')
        self.assertNotIn('tools', (await self.h.events.get())['session'])
        await voice.close()

    async def test_pending_ack_does_not_block_audio_and_speech_cancels_old_call(self):
        self.device.ack = False
        voice = await self.h.socket()
        await voice.receive_json(timeout=2)
        await self.h.events.get()
        provider = self.h.upstreams[-1]
        item = {'type': 'function_call', 'call_id': 'voice_call', 'name': 'stackchan_perform',
                'arguments': json.dumps(SCENE)}
        await provider.send_json({'type': 'response.created', 'response': {'id': 'voice_response'}})
        await provider.send_json({'type': 'response.output_item.added', 'response_id': 'voice_response',
                                   'output_index': 0, 'item': item})
        await provider.send_json({'type': 'response.function_call_arguments.done', 'response_id': 'voice_response',
                                   'output_index': 0, **{k: item[k] for k in ('call_id', 'name', 'arguments')}})
        await provider.send_json({'type': 'response.done', 'response': {
            'id': 'voice_response', 'status': 'completed', 'output': [item]}})
        await until(lambda: any(m['type'] == 'command.execute' for m in self.device.events))
        await voice.send_bytes(b'\x00\x00' * 1600)
        self.assertEqual((await asyncio.wait_for(self.h.events.get(), 1))['type'], 'input_audio_buffer.append')
        generation = self.hub.control.generation
        await provider.send_json({'type': 'input_audio_buffer.speech_started'})
        await until(lambda: self.hub.control.generation > generation)
        command = next(m for m in self.device.events if m['type'] == 'command.execute')
        await self.device.status(command, 'accepted')  # Delayed old ACK cannot resume the response.
        await asyncio.sleep(.05)
        self.assertTrue(self.h.events.empty())
        await voice.close()
        await until(lambda: self.hub.control is None)

    async def test_hangup_and_logout_release_device(self):
        voice = await self.h.socket()
        await voice.receive_json(timeout=2)
        await self.h.events.get()
        # The same owner cannot acquire concurrently; this test explicitly uses
        # the voice session's controller to exercise its hangup path.
        voice_control = next(c for c in self.hub.controllers if c is not self.control)
        await voice_control.ensure()
        await voice.send_json({'type': 'stop'})
        await voice.receive(timeout=2)
        await until(lambda: self.hub.control is None)
        self.assertTrue(voice_control.closed)
        self.assertFalse(self.device.events[-1]['active'])
        await self.h.client.post('/api/logout', headers={'Origin': self.h.cfg.origin})
        self.assertFalse(self.hub.bindings)


class FakeControl:
    simulated = True
    def __init__(self):
        self.calls, self.generation = [], 1
        self.gate = asyncio.Event()
        self.gate.set()
    async def execute(self, name, args):
        self.calls.append((name, args))
        await self.gate.wait()
        return {'ok': True, 'simulated': True}
    def fence(self):
        self.generation += 1
        return self.confirm()
    async def confirm(self):
        return None
    async def close(self):
        pass


class ToolCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.control = FakeControl()
        self.output, self.notices = [], []
        async def send(event):
            self.output.append(event)
        async def notice(text):
            self.notices.append(text)
        self.tools = ToolCalls(self.control, send, notice)

    async def asyncTearDown(self):
        await self.tools.close()
        self.assertFalse(self.notices)

    def response(self, calls, *, done=True, reverse=False):
        self.tools.handle({'type': 'response.created', 'response': {'id': 'r1'}})
        items = []
        for i, (name, arguments) in enumerate(calls):
            items.append({'type': 'function_call', 'call_id': f'call_{i}', 'name': name,
                          'arguments': arguments})
        for i in (reversed(range(len(items))) if reverse else range(len(items))):
            self.tools.handle({'type': 'response.output_item.added', 'response_id': 'r1', 'output_index': i, 'item': items[i]})
            self.tools.handle({'type': 'response.function_call_arguments.done', 'response_id': 'r1',
                               'output_index': i, **{k: items[i][k] for k in ('call_id', 'name', 'arguments')}})
        if done:
            self.tools.handle({'type': 'response.done', 'response': {'id': 'r1', 'status': 'completed', 'output': items}})
        return items

    async def test_output_index_order_dedup_and_single_continuation(self):
        items = self.response([('stackchan_stop', '{}'), ('stackchan_perform', json.dumps(SCENE))], reverse=True)
        duplicate = {'type': 'response.function_call_arguments.done', 'response_id': 'r1', 'output_index': 0, **{k: items[0][k] for k in ('call_id', 'name', 'arguments')}}
        self.tools.handle(duplicate)
        await self.tools.responses['r1'].task
        self.tools.handle({'type': 'response.done', 'response': {'id': 'r1', 'status': 'completed'}})
        self.tools.handle(duplicate)
        self.assertEqual([name for name, _ in self.control.calls], ['stackchan_stop', 'stackchan_perform'])
        self.assertEqual([x['item']['call_id'] for x in self.output if 'item' in x], ['call_0', 'call_1'])
        self.assertEqual(sum(x['type'] == 'response.create' for x in self.output), 1)
        self.assertEqual(self.tools.epoch, 0)

    async def test_error_diagnostics_are_separate_from_spoken_output(self):
        diagnostics = []
        async def diagnostic(name, result, call_id):
            diagnostics.append((name, result))
            self.assertEqual(call_id, 'call_0')
        async def reject(name, args):
            return DeviceError('OUT_OF_RANGE', details={'reason': 'speed', 'frame_index': 1,
                                                      'minimum_move_ms': 1334}).result()
        self.control.execute = reject
        self.tools.diagnostic = diagnostic
        self.response([('stackchan_perform', json.dumps(SCENE))])
        await self.tools.responses['r1'].task
        self.assertEqual(diagnostics[0][1]['details']['reason'], 'speed')
        self.assertEqual([event['type'] for event in self.output], ['conversation.item.create', 'response.create'])
        self.assertEqual(self.output[0]['item']['type'], 'function_call_output')

    async def test_incomplete_arguments_and_interrupted_response_never_execute(self):
        self.response([('stackchan_perform', json.dumps(SCENE))], done=False)
        await asyncio.sleep(.01)
        self.assertFalse(self.control.calls)
        self.tools.invalidate()
        self.tools.handle({'type': 'response.done', 'response': {'id': 'r1', 'status': 'completed'}})
        await asyncio.sleep(.01)
        self.assertFalse(self.control.calls or self.output)
        self.assertEqual(self.control.generation, 2)

    async def test_inflight_interrupt_suppresses_late_output_and_remaining_calls(self):
        self.control.gate.clear()
        self.response([('stackchan_perform', '{}'), ('stackchan_get_state', '{}')])
        await until(lambda: self.control.calls)
        self.tools.invalidate()
        self.control.gate.set()
        await asyncio.sleep(.02)
        self.assertEqual(len(self.control.calls), 1)
        self.assertFalse(self.output)

    async def test_invalid_json_yields_one_simulated_error_result(self):
        self.response([('stackchan_perform', '{')])
        await self.tools.responses['r1'].task
        result = json.loads(self.output[0]['item']['output'])
        self.assertEqual(result, {'ok': False, 'error_code': 'INVALID_ARGUMENT', 'simulated': True})
        self.assertFalse(self.control.calls)

    async def test_speech_state_and_new_response_prevent_extra_continuation(self):
        self.control.gate.clear()
        self.response([('stackchan_get_state', '{}')])
        await until(lambda: self.control.calls)
        self.tools.handle({'type': 'input_audio_buffer.speech_started'})
        self.control.gate.set()
        await self.tools.responses['r1'].task
        self.assertEqual([e['type'] for e in self.output], ['conversation.item.create'])

    async def test_conflicting_call_and_output_are_rejected(self):
        items = self.response([('stackchan_get_state', '{}')], done=False)
        with self.assertRaises(ToolProtocolError):
            self.tools.handle({'type': 'response.function_call_arguments.done', 'response_id': 'r1',
                               'output_index': 0, **{k: items[0][k] for k in ('call_id', 'arguments')}, 'name': 'stackchan_stop'})
        items[0]['call_id'] = 'other_call'
        with self.assertRaises(ToolProtocolError):
            self.tools.handle({'type': 'response.done', 'response': {'id': 'r1', 'status': 'completed', 'output': items}})
        self.assertFalse(self.control.calls)
