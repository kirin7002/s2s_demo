"""Real-runtime plumbing with synthetic sockets only; no paid requests."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import WSMessage, WSMsgType
from gateway.config import Config
from scripts.serve_s2s_tls import Runtime, TrackedProvider
from gateway.action_audit import action_summary
from scripts.verify_s2s_live import battery_sample_valid
from gateway.server import ACTIVE, CONFIG, DEVICES, LOGINS, OWNERS


class Socket:
    def __init__(self, event=None):
        self.event = event
        self.closed = False
        self.sent = []

    async def receive(self, **kwargs):
        return WSMessage(WSMsgType.TEXT, json.dumps(self.event), None)

    async def send_json(self, event):
        self.sent.append(event)

    async def close(self):
        self.closed = True


class LiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        config = Config(api_key='synthetic-secret', demo_password='synthetic-login', live_enabled=True)
        hardware = {'origin': 'https://Mixed-Host.local:8443', 'port': 8443,
                    'device_id': 'fixture', 'device_token': 'synthetic-device-token-' * 3}
        with patch('scripts.serve_s2s_tls.Config.from_env', return_value=config):
            self.runtime = Runtime(hardware, self.directory)

    async def asyncTearDown(self):
        await self.runtime.hub.close()
        self.temp.cleanup()

    async def test_startup_uses_root_login_but_no_provider_connection(self):
        self.assertEqual(self.runtime.config.origin, 'https://mixed-host.local:8443')
        self.assertFalse(self.runtime.config.stackchan_read_only)
        self.assertIsNone(self.runtime.config.stackchan_test_max_deg)
        self.assertEqual(self.runtime.config.max_seconds, 600)
        self.assertEqual(self.runtime.config.idle_seconds, 60)
        self.assertEqual(self.runtime.config.max_sessions, 2)
        self.assertEqual(self.runtime.status()['provider_connection_attempts'], 0)
        self.assertEqual(self.runtime.status()['active_sessions'], 0)
        self.assertEqual(self.runtime.local_app[CONFIG].origin, 'http://localhost:8080')
        for key in (ACTIVE, DEVICES, LOGINS, OWNERS):
            self.assertIs(self.runtime.local_app[key], self.runtime.app[key])

    async def test_tool_failure_is_returned_under_normal_coordinator_policy(self):
        socket = Socket()
        tracked = TrackedProvider(socket, self.runtime)
        await tracked.send_json({'type': 'conversation.item.create', 'item': {
            'type': 'function_call_output', 'call_id': 'fixture',
            'output': json.dumps({'ok': False, 'error_code': 'MOTION_TIMEOUT', 'simulated': False})}})
        self.assertFalse(socket.closed)
        self.assertEqual(len(socket.sent), 1)

    async def test_temporary_eight_call_budget_removed(self):
        socket = Socket({'type': 'response.function_call_arguments.done', 'name': 'stackchan_get_state',
                         'call_id': 'fixture', 'arguments': '{}'})
        tracked = TrackedProvider(socket, self.runtime)
        for _ in range(9):
            await tracked.receive()
        self.assertFalse(socket.closed)
        self.assertEqual(len(tracked.calls), 9)

    async def test_audit_omits_audio_transcript_and_secrets(self):
        socket = Socket({'type': 'response.audio_transcript.delta', 'delta': 'synthetic-private-text'})
        tracked = TrackedProvider(socket, self.runtime)
        await tracked.receive()
        await tracked.close()
        log = (self.directory / 'events.jsonl').read_text()
        self.assertNotIn('synthetic-private-text', log)
        self.assertNotIn('synthetic-secret', log)
        self.assertIn('provider_session_closed', log)

    async def test_action_summary_keeps_motion_parameters_but_never_text(self):
        value = action_summary({'duration_ms': 4000, 'frames': [
            {'at_ms': 0, 'head': {'yaw_deg': -3, 'pitch_deg': 0, 'move_ms': 1200},
             'light': {'rgb': [0, 255, 0], 'brightness': .1, 'fade_ms': 100},
             'screen': {'page': 'text', 'text': 'synthetic-private-text'}},
            {'at_ms': 2000, 'head': {'yaw_deg': 3, 'pitch_deg': 0, 'move_ms': 1200},
             'screen': {'page': 'face', 'expression': 'happy'}}]})
        self.assertEqual(value['head_axes']['yaw_deg'], {'min': -3, 'max': 3})
        self.assertEqual(value['move_ms'], [1200, 1200])
        self.assertEqual(value['head_frames'], [
            {'frame_index': 0, 'at_ms': 0, 'yaw_deg': -3, 'pitch_deg': 0, 'move_ms': 1200},
            {'frame_index': 1, 'at_ms': 2000, 'yaw_deg': 3, 'pitch_deg': 0, 'move_ms': 1200}])
        self.assertEqual(value['expressions'], ['happy'])
        self.assertEqual(value['light_frames'], [{'frame_index': 0, 'at_ms': 0,
            'rgb': [0, 255, 0], 'brightness': .1, 'fade_ms': 100}])
        self.assertEqual(value['screen_frames'], [
            {'frame_index': 0, 'at_ms': 0, 'page': 'text'},
            {'frame_index': 1, 'at_ms': 2000, 'page': 'face', 'expression': 'happy'}])
        self.assertNotIn('synthetic-private-text', json.dumps(value))

    async def test_battery_only_requires_fresh_power_and_zero_vbus(self):
        sample = {'online': True, 'fresh': True, 'simulated': False, 'state': {
            'power': {'available': True, 'sample_age_ms': 1000, 'vbus_voltage_mv': 0}}}
        self.assertTrue(battery_sample_valid(sample))
        sample['state']['power']['vbus_voltage_mv'] = 5000
        self.assertFalse(battery_sample_valid(sample))
        sample['state']['power'].update(vbus_voltage_mv=0, sample_age_ms=6000)
        self.assertFalse(battery_sample_valid(sample))
