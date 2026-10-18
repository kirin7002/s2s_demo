"""Explicit local real-provider TLS entrypoint; secrets come from root .env.

Uses production login, DeviceHub, Session and ToolCalls. No simulated provider
or function injection endpoint. Existing hardware TLS/device identity is reused
so firmware does not need reflashing. Starting the service incurs no model call.
"""
import argparse
import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import ssl
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp import WSMsgType, web
from gateway.config import Config
from gateway.action_audit import action_summary
from gateway.server import ACTIVE, ATTEMPTS, CLIENT, DEVICES, LOGINS, OWNERS, create_app
from scripts.stackchan_hardware import private_json, private_write

DEFAULT_DIRECTORY = ROOT / '.run/s2s-live'


class TrackedProvider:
    def __init__(self, socket, runtime):
        self.socket, self.runtime = socket, runtime
        self.events, self.sent = Counter(), Counter()
        self.calls, self.outputs, self.responses = [], [], []
        self.audio_bytes = 0
        self.saved = False

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def receive(self, **kwargs):
        packet = await self.socket.receive(**kwargs)
        if packet.type == WSMsgType.TEXT:
            event = json.loads(packet.data)
            kind = event.get('type', '')
            if isinstance(kind, str) and re.fullmatch(r'[a-z_.]{1,90}', kind):
                self.events[kind] += 1
            if kind == 'response.function_call_arguments.done':
                name = event.get('name')
                args = event.get('arguments', '')
                self.calls.append({'name': name if name in {
                    'stackchan_get_state', 'stackchan_perform', 'stackchan_get_command', 'stackchan_stop'} else 'unknown',
                    'call_id': event.get('call_id'),
                    'arguments_sha256': hashlib.sha256(args.encode()).hexdigest()})
                if name == 'stackchan_perform':
                    try:
                        self.calls[-1]['action_summary'] = action_summary(json.loads(args))
                    except (ValueError, TypeError):
                        self.calls[-1]['action_summary'] = {'valid': False}
            elif kind == 'response.audio.delta':
                # Count encoded length only; audio and transcripts are not logged.
                self.audio_bytes += len(event.get('delta', '')) * 3 // 4
            elif kind == 'response.done':
                response = event.get('response', {})
                self.responses.append({'status': response.get('status'), 'usage': response.get('usage')})
        return packet

    async def send_json(self, event):
        kind = event.get('type')
        self.sent[kind] += 1
        if kind == 'conversation.item.create' and event.get('item', {}).get('type') == 'function_call_output':
            item = event['item']
            value = json.loads(item['output'])
            self.outputs.append({'call_id': item['call_id'], **{key: value[key] for key in (
                'ok', 'status', 'command_id', 'error_code', 'online', 'fresh', 'age_ms', 'simulated', 'state'
            ) if key in value}})
        await self.socket.send_json(event)

    async def close(self):
        await self.socket.close()
        if not self.saved:
            self.saved = True
            self.runtime.record('provider_session_closed', real_provider=True,
                model=self.runtime.config.model, events=dict(self.events), sent=dict(self.sent),
                calls=self.calls, outputs=self.outputs, responses=self.responses,
                output_audio_bytes_estimate=self.audio_bytes)


class Runtime:
    def __init__(self, hardware, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.hardware = hardware
        base = Config.from_env()
        self.config = replace(base, live_enabled=True, origin=hardware['origin'].lower(),
            stackchan_enabled=True, stackchan_device_id=hardware['device_id'],
            stackchan_device_token=hardware['device_token'], stackchan_read_only=False,
            stackchan_test_max_deg=None)
        self.config = replace(self.config, stackchan_audit_enabled=True)
        if self.config.problem():
            raise ValueError('Real-provider root configuration unavailable')
        self.stop = asyncio.Event()
        self.attempts = 0
        self.providers = []
        self.app = create_app(self.config, connector=self.connect)
        self.hub = self.app[DEVICES]
        self.local_origin = 'http://localhost:8080'
        self.local_app = create_app(replace(self.config, origin=self.local_origin), connector=self.connect)
        # One device/controller and one account/session store. Separate origins
        # retain exact CSRF/WS checks and the appropriate Secure cookie policy.
        for key in (ACTIVE, ATTEMPTS, DEVICES, LOGINS, OWNERS):
            self.local_app[key] = self.app[key]
        self.control_socket = self.directory / 'control.sock'

    def record(self, stage, **fields):
        event = {'recorded_utc': datetime.now(timezone.utc).isoformat(), 'stage': stage, **fields}
        fd = os.open(self.directory / 'events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')

    async def connect(self):
        self.attempts += 1
        self.record('provider_connect', attempt=self.attempts, model=self.config.model)
        socket = await self.app[CLIENT].ws_connect(self.config.url,
            headers={'Authorization': 'Bearer ' + self.config.api_key},
            heartbeat=20, max_msg_size=2 * 1024 * 1024)
        tracked = TrackedProvider(socket, self)
        self.providers.append(tracked)
        self.providers = self.providers[-8:]
        return tracked

    def status(self):
        connection = self.hub.connection
        return {'pid': os.getpid(), 'mode': 'real_provider', 'model': self.config.model,
            'origin': self.config.origin, 'device_connected': bool(connection and connection.online),
            'computer_origin': self.local_origin,
            'connection_id': connection.id if connection else None,
            'simulated': connection.capabilities.get('simulated') if connection else None,
            'capabilities': connection.capabilities if connection else {},
            'active_sessions': len(self.app[ACTIVE]), 'control_session_active': self.hub.control is not None,
            'read_only': self.config.stackchan_read_only, 'max_head_target_deg': self.config.stackchan_test_max_deg,
            'max_sessions': self.config.max_sessions, 'max_seconds': self.config.max_seconds,
            'idle_seconds': self.config.idle_seconds,
            'provider_connection_attempts': self.attempts,
            'latest_tool_call_names': [c['name'] for c in self.providers[-1].calls] if self.providers else [],
            'latest_tool_output_count': len(self.providers[-1].outputs) if self.providers else 0,
            'latest_completed_tool_outputs': sum(o.get('status') == 'completed' and o.get('fresh') is True
                for o in self.providers[-1].outputs) if self.providers else 0,
            'nonterminal_commands': sum(r['status'] not in {'completed', 'failed', 'cancelled', 'rejected'}
                for r in self.hub.records.values()),
            'command_count': len(self.hub.records)}

    async def persist(self):
        while True:
            private_json(self.directory / 'state.json', self.status())
            await asyncio.sleep(.5)

    async def control(self, reader, writer):
        try:
            value = json.loads(await asyncio.wait_for(reader.readline(), 3))
            # Read-only local diagnostics. No function/model injection over LAN.
            if value == {'op': 'status'}:
                observed = self.status()
            elif value == {'op': 'get_state'}:
                connection = self.hub.connection
                if not connection or not connection.online:
                    observed = {'online': False, 'fresh': False, 'simulated': None, 'state': {}}
                else:
                    identifier = secrets.token_hex(12)
                    event = await connection.request(('query', identifier), {
                        'v': 1, 'type': 'device.query', 'connection_id': connection.id,
                        'request_id': identifier})
                    observed = {'online': self.hub.connection is connection, 'fresh': self.hub.connection is connection,
                        'age_ms': round((time.monotonic() - connection.observed_at) * 1000),
                        'state': event.get('state', {}), 'active_command_id': event.get('active_command_id'),
                        'simulated': connection.capabilities.get('simulated')}
            else:
                raise ValueError()
            result = {'ok': True, 'result': observed}
        except Exception:
            result = {'ok': False, 'error': 'INVALID_LOCAL_STATUS_REQUEST'}
        writer.write((json.dumps(result) + '\n').encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def serve(self):
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(self.hardware['server_certificate_file'], self.hardware['server_private_key_file'])
        if self.control_socket.exists():
            raise ValueError('Existing local status socket requires review')
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        local_runner = web.AppRunner(self.local_app, access_log=None)
        await local_runner.setup()
        control = None
        task = None
        try:
            await web.TCPSite(runner, '0.0.0.0', self.hardware['port'], ssl_context=tls).start()
            await web.TCPSite(local_runner, '127.0.0.1', 8080).start()
            control = await asyncio.start_unix_server(self.control, path=self.control_socket)
            self.control_socket.chmod(0o600)
            task = asyncio.create_task(self.persist())
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig, self.stop.set)
            self.record('gateway_started', real_provider=True, read_only=self.config.stackchan_read_only)
            print('S2S listening: http://localhost:8080/login; device TLS preserved; no provider connection until a call.', flush=True)
            await self.stop.wait()
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if control:
                control.close()
                await control.wait_closed()
                self.control_socket.unlink(missing_ok=True)
            await local_runner.cleanup()
            await runner.cleanup()
            private_json(self.directory / 'state.json', {**self.status(), 'stopped': True})
            self.record('gateway_stopped')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument('--hardware-config', type=Path, default=ROOT / '.run/stackchan-hardware/server-config.json')
    args = parser.parse_args()
    hardware = json.loads(args.hardware_config.read_text())
    asyncio.run(Runtime(hardware, args.directory).serve())


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('S2S TLS startup stopped; private configuration and error details withheld.', file=sys.stderr)
        raise SystemExit(1) from None
