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
            self.runtime = Runtime(hardware, self.directory, read_only=True)

    async def asyncTearDown(self):
        await self.runtime.hub.close()
        self.temp.cleanup()

    async def test_startup_uses_root_login_but_no_provider_connection(self):
        self.assertEqual(self.runtime.config.origin, 'https://mixed-host.local:8443')
        self.assertTrue(self.runtime.config.stackchan_read_only)
        self.assertEqual(self.runtime.config.stackchan_test_max_deg, 3)
        self.assertEqual(self.runtime.status()['provider_connection_attempts'], 0)
        self.assertEqual(self.runtime.status()['active_sessions'], 0)

    async def test_tool_failure_closes_provider_without_continuation_or_retry(self):
        socket = Socket()
        tracked = TrackedProvider(socket, self.runtime)
        with self.assertRaises(RuntimeError):
            await tracked.send_json({'type': 'conversation.item.create', 'item': {
                'type': 'function_call_output', 'call_id': 'fixture',
                'output': json.dumps({'ok': False, 'error_code': 'MOTION_TIMEOUT', 'simulated': False})}})
        self.assertTrue(socket.closed)
        self.assertFalse(socket.sent)

    async def test_tool_budget_stops_before_a_ninth_call_can_execute(self):
        socket = Socket({'type': 'response.function_call_arguments.done', 'name': 'stackchan_get_state',
                         'call_id': 'fixture', 'arguments': '{}'})
        tracked = TrackedProvider(socket, self.runtime)
        for _ in range(8):
            await tracked.receive()
        with self.assertRaises(RuntimeError):
            await tracked.receive()
        self.assertTrue(socket.closed)

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
             'screen': {'page': 'text', 'text': 'synthetic-private-text'}},
            {'at_ms': 2000, 'head': {'yaw_deg': 3, 'pitch_deg': 0, 'move_ms': 1200},
             'screen': {'expression': 'happy'}}]})
        self.assertEqual(value['head_axes']['yaw_deg'], {'min': -3, 'max': 3})
        self.assertEqual(value['move_ms'], [1200, 1200])
        self.assertEqual(value['expressions'], ['happy'])
        self.assertNotIn('synthetic-private-text', json.dumps(value))

    async def test_battery_only_requires_fresh_power_and_zero_vbus(self):
        sample = {'online': True, 'fresh': True, 'simulated': False, 'state': {
            'power': {'available': True, 'sample_age_ms': 1000, 'vbus_voltage_mv': 0}}}
        self.assertTrue(battery_sample_valid(sample))
        sample['state']['power']['vbus_voltage_mv'] = 5000
        self.assertFalse(battery_sample_valid(sample))
        sample['state']['power'].update(vbus_voltage_mv=0, sample_age_ms=6000)
        self.assertFalse(battery_sample_valid(sample))
