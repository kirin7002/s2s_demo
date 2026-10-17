"""Local transport/fault fixtures; no model, device credentials or hardware."""
import asyncio
import json
import unittest

from aiohttp import ClientSession, WSServerHandshakeError

from gateway.config import Config
from gateway.server import DEVICES
from gateway.stackchan import DeviceError, PARAMETERS, QWEN_TOOLS, encode, validate_frames
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
        self.sync = asyncio.Event()
        self.sync.set()
        self.jobs = set()

    async def open(self):
        self.ws = await self.h.client.ws_connect('/device/v1/ws', headers={'Authorization': 'Bearer ' + TOKEN})
        await self.ws.send_json({'v': 1, 'type': 'device.hello', 'device_boot_id': 'fixture_boot',
                                 'firmware_version': 'test-only', 'capabilities': CAPABILITIES})
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
                record = self.commands.get(message.get('command_id'), {})
                await self.ws.send_json({**message, 'type': 'device.state', 'capabilities': CAPABILITIES,
                                         'state': {'head': {'yaw_deg': 0, 'pitch_deg': 0}},
                                         'status': 'unknown', **record,
                                         'request_id': message['request_id'], 'type': 'device.state'})

    async def close(self):
        for job in list(self.jobs):
            job.cancel()
        await asyncio.gather(*list(self.jobs), return_exceptions=True)
        await self.ws.close()
        await self.reader


class ContractTests(unittest.TestCase):
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
        self.assertIn('模拟', tools['instructions'])
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
            candidate = {'duration_ms': 8000, 'frames': [
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
        self.assertEqual(session['tools'], QWEN_TOOLS)
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
