"""Ephemeral TLS + synthetic device; never connects to the hardware daemon."""
import asyncio
import copy
import json
from pathlib import Path
import socket
import ssl
import stat
import subprocess
import tempfile
import unittest

from aiohttp import ClientConnectorCertificateError, ClientSession, WSMsgType

from scripts.stackchan_hardware import DEFAULT_DIRECTORY, HardwareHarness, certificate_names, migrate_address, prepare, request_local
from tests.test_stackchan import CAPABILITIES, until


class HardwareHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        DEFAULT_DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temp = tempfile.TemporaryDirectory(prefix='selftest-', dir=DEFAULT_DIRECTORY)
        self.root = Path(self.temp.name)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        config_file = await asyncio.to_thread(prepare, self.root, '127.0.0.1', port)
        self.config = json.loads(config_file.read_text())
        self.h = HardwareHarness(self.config)
        self.server = asyncio.create_task(self.h.serve(install_signals=False))
        await until(lambda: Path(self.config['control_socket']).exists() or self.server.done())
        if self.server.done():
            self.server.result()
        self.tls = ssl.create_default_context(cafile=self.config['ca_certificate_file'])
        self.client = ClientSession()
        self.base = f'https://127.0.0.1:{port}'
        self.device, self.reader = None, None
        self.wire = []
        self.caps = copy.deepcopy(CAPABILITIES)
        self.caps['head']['enabled'] = False
        self.caps['head']['calibrated'] = False

    async def asyncTearDown(self):
        self.h.shutdown.set()
        await asyncio.wait_for(self.server, 8)
        if self.device:
            await self.device.close()
        if self.reader:
            await self.reader
        await self.client.close()
        self.temp.cleanup()

    async def attach_synthetic_device(self):
        self.device = await self.client.ws_connect(self.base + '/device/v1/ws', ssl=self.tls,
                                                   headers={'Authorization': 'Bearer ' + self.config['device_token']})
        await self.device.send_json({'v': 1, 'type': 'device.hello', 'device_boot_id': 'synthetic_tls_device',
                                      'firmware_version': 'test-only', 'capabilities': self.caps})
        ready = await self.device.receive_json(timeout=2)
        connection_id = ready['connection_id']
        async def reader():
            async for packet in self.device:
                if packet.type != WSMsgType.TEXT:
                    break
                event = json.loads(packet.data)
                self.wire.append(event)
                if event['type'] == 'control.sync':
                    await self.device.send_json({**event, 'type': 'control.synced'})
                elif event['type'] == 'command.execute':
                    reply = {k: event[k] for k in ('connection_id', 'control_session_id', 'generation', 'command_id')}
                    await self.device.send_json({**reply, 'v': 1, 'type': 'command.status',
                                                 'status': 'accepted', 'partial_applied': False})
                    await self.device.send_json({**reply, 'v': 1, 'type': 'command.status',
                                                 'status': 'completed', 'partial_applied': False})
                elif event['type'] == 'device.query':
                    await self.device.send_json({'v': 1, 'type': 'device.state', 'connection_id': connection_id,
                                                 'request_id': event['request_id'], 'capabilities': self.caps,
                                                 'active_command_id': None, 'state': {}})
        self.reader = asyncio.create_task(reader())

    async def test_private_files_and_tls_require_issuing_ca(self):
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for path in self.root.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        async with self.client.get(self.base + '/login', ssl=self.tls) as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(ClientConnectorCertificateError):
            await self.client.get(self.base + '/login')

    async def test_preflight_uses_login_binding_and_registered_tools_without_commands(self):
        result = await request_local(self.config, {'op': 'preflight'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['result'], {'login_and_binding': True, 'registered_tool_count': 4,
                                          'command_count': 0, 'motion_enabled': False})
        self.assertFalse(self.h.motion_enabled)

    async def test_head_request_is_rejected_before_session_or_dispatch(self):
        request = {'op': 'tool', 'calls': [{'name': 'stackchan_perform', 'arguments': {
            'duration_ms': 200, 'frames': [{'at_ms': 0, 'head': {'yaw_deg': 0, 'pitch_deg': 0, 'move_ms': 100}}]}}]}
        response = await request_local(self.config, request)
        self.assertFalse(response['ok'])
        self.assertFalse(self.h.models or self.h.hub.records)

    async def test_bonjour_migration_preserves_ca_and_credentials(self):
        ca_before = Path(self.config['ca_certificate_file']).read_bytes()
        key_before = Path(self.config['server_private_key_file']).read_bytes()
        await asyncio.to_thread(migrate_address, self.root, 'stable-test.local', '127.0.0.1')
        updated = json.loads((self.root / 'server-config.json').read_text())
        device = json.loads((self.root / 'device-config.json').read_text())
        self.assertTrue(updated['device_token'] == self.config['device_token'] == device['device_token'])
        self.assertTrue(updated['demo_password'] == self.config['demo_password'])
        self.assertTrue(Path(updated['ca_certificate_file']).read_bytes() == ca_before)
        self.assertTrue(Path(updated['server_private_key_file']).read_bytes() == key_before)
        self.assertEqual(updated['listen_host'], '0.0.0.0')
        self.assertEqual(device['host'], 'stable-test.local')
        result = await asyncio.to_thread(subprocess.run, ['openssl', 'x509', '-in', updated['server_certificate_file'],
                                                          '-noout', '-text'], capture_output=True, text=True, check=True)
        self.assertIn('DNS:stable-test.local', result.stdout)
        for bad in ('evil.example', 'evil.local\nsubjectAltName=DNS:evil.example'):
            with self.assertRaises(ValueError):
                certificate_names(bad, '127.0.0.1')

    async def test_device_log_omits_authorization_and_secret_values(self):
        marker = 'synthetic-header-that-must-not-be-logged'
        async with self.client.get(self.base + '/device/v1/ws', ssl=self.tls,
                                   headers={'Authorization': 'Bearer ' + marker}) as response:
            self.assertEqual(response.status, 401)
        log = (self.root / 'gateway-events.jsonl').read_text()
        self.assertFalse(marker in log or self.config['device_token'] in log or self.config['demo_password'] in log)
        entries = [json.loads(line) for line in log.splitlines()]
        self.assertTrue(any(e.get('http_status') == 401 and e.get('peer_ip') == '127.0.0.1' for e in entries))

    async def test_closed_tls_transport_still_records_only_fixed_diagnostic(self):
        class DetachedTransport:
            def get_extra_info(self, name):
                raise AttributeError('synthetic-private-error-text')

        self.h.transport_error(None, {'exception': ConnectionResetError('synthetic-private-error-text'),
                                     'transport': DetachedTransport()})
        log = (self.root / 'gateway-events.jsonl').read_text()
        self.assertNotIn('synthetic-private-error-text', log)
        entry = json.loads(log.splitlines()[-1])
        self.assertEqual(entry['category'], 'PEER_CLOSED_DURING_HANDSHAKE')
        self.assertIsNone(entry['peer_ip'])

    async def test_screen_light_state_and_stop_work_before_servo_readiness(self):
        await self.attach_synthetic_device()
        state = await self.h.dispatch({'op': 'tool', 'calls': [{'name': 'stackchan_get_state', 'arguments': {}}]})
        self.assertTrue(state[0]['online'] and state[0]['fresh'] and state[0]['simulated'])
        scene = {'duration_ms': 200, 'frames': [{'at_ms': 0,
                 'screen': {'page': 'face', 'expression': 'happy'},
                 'light': {'rgb': [0, 12, 0], 'brightness': .1, 'fade_ms': 0}}]}
        result = await self.h.dispatch({'op': 'tool', 'duplicate': True,
                                        'calls': [{'name': 'stackchan_perform', 'arguments': scene}]})
        self.assertEqual(result[0]['status'], 'accepted')
        self.assertIs(result[0]['simulated'], True)  # This unit fixture is not hardware.
        commands = [m for m in self.wire if m['type'] == 'command.execute']
        self.assertEqual(len(commands), 1)
        self.assertNotIn('head', commands[0]['args']['frames'][0])
        stop = await self.h.dispatch({'op': 'tool', 'calls': [{'name': 'stackchan_stop', 'arguments': {}}]})
        self.assertEqual(stop[0]['status'], 'stopped')
        self.assertFalse(self.h.motion_enabled)


if __name__ == '__main__':
    unittest.main()
