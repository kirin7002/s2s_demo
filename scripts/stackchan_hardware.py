"""Private LAN TLS harness using the production gateway and an in-memory Qwen.

Never reads .env, opens serial ports, or connects to a model provider. Startup
only listens. Tool injection is through a mode-0600 Unix socket, not a LAN API.
Head motion remains gated until a separate servo-ready report is supplied.
Read-only tools and screen/light-only scenes work with motion disabled.
"""
import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import time

from aiohttp import ClientSession, CookieJar, WSMessage, WSMsgType, web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gateway.config import Config
from gateway.server import DEVICES, create_app

DEFAULT_DIRECTORY = ROOT / '.run/stackchan-hardware'


def private_write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)
    os.replace(temporary, path)


def private_json(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def certificate_names(host, lan_ip=None):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.local', host):
            raise ValueError('Only a local Bonjour name or private IPv4 address is allowed') from None
        if lan_ip is None:
            candidates = [item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_INET)]
            lan_ip = next((value for value in candidates if not ipaddress.ip_address(value).is_loopback), None)
        address = ipaddress.ip_address(lan_ip)
    if address.version != 4 or not address.is_private or address.is_unspecified:
        raise ValueError('A specific private LAN IPv4 address is required')
    names = [f'IP:{address}', 'IP:127.0.0.1', 'DNS:localhost']
    if host.lower().endswith('.local'):
        names.insert(0, 'DNS:' + host)
    return list(dict.fromkeys(names)), str(address)


def server_certificate_config(names):
    return f'''[req]
prompt=no
distinguished_name=dn
req_extensions=server
[dn]
CN=StackChan LAN Hardware Test
[server]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName={','.join(names)}
subjectKeyIdentifier=hash
'''


def prepare(directory, host, port, lan_ip=None):
    names, address = certificate_names(host, lan_ip)
    if not 1024 <= port <= 65535:
        raise ValueError('Invalid test port')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    config_file = directory / 'server-config.json'
    if config_file.exists():
        raise ValueError('Existing private configuration preserved; use serve to reuse it')
    ca_key, ca_cert = directory / 'ca.key.pem', directory / 'ca.crt.pem'
    server_key, server_cert = directory / 'server.key.pem', directory / 'server.crt.pem'
    ca_conf, server_conf = directory / 'ca.cnf', directory / 'server.cnf'
    private_write(ca_conf, '''[req]
prompt=no
distinguished_name=dn
x509_extensions=ca
[dn]
CN=StackChan Local Hardware Test CA
[ca]
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
''')
    private_write(server_conf, server_certificate_config(names))
    commands = [
        ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3',
         '-config', str(ca_conf), '-keyout', str(ca_key), '-out', str(ca_cert)],
        ['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-config', str(server_conf),
         '-keyout', str(server_key), '-out', str(directory / 'server.csr.pem')],
        ['openssl', 'x509', '-req', '-in', str(directory / 'server.csr.pem'), '-CA', str(ca_cert),
         '-CAkey', str(ca_key), '-set_serial', str(secrets.randbits(128)), '-days', '3',
         '-extfile', str(server_conf), '-extensions', 'server', '-out', str(server_cert)],
    ]
    previous_mask = os.umask(0o077)
    try:
        for command in commands:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        os.umask(previous_mask)
    token = secrets.token_urlsafe(48)
    origin = f'https://{host}:{port}'
    config = {'host': host, 'port': port, 'origin': origin, 'device_id': 'stackchan_k151',
              'listen_host': '0.0.0.0' if host.lower().endswith('.local') else host, 'lan_ip_at_issue': address,
              'device_token': token, 'demo_password': secrets.token_urlsafe(32),
              'ca_certificate_file': str(ca_cert), 'server_certificate_file': str(server_cert),
              'server_private_key_file': str(server_key),
              'control_socket': str(directory / 'control.sock'), 'state_file': str(directory / 'state.json'),
              'model_mode': 'in_memory_mock_no_provider_network'}
    private_json(config_file, config)
    private_json(directory / 'device-config.json', {
        'host': host, 'port': port, 'path': '/device/v1/ws', 'device_id': config['device_id'],
        'url': f'wss://{host}:{port}/device/v1/ws', 'device_token': token,
        'ca_certificate_file': str(ca_cert), 'root_ca_pem': ca_cert.read_text(),
        'expected_simulated': False,
    })
    return config_file


def migrate_address(directory, host, lan_ip=None):
    """Reissue only the server leaf; preserve the root CA and all credentials."""
    directory = Path(directory).resolve()
    config_file = directory / 'server-config.json'
    config = json.loads(config_file.read_text())
    device_file = directory / 'device-config.json'
    device = json.loads(device_file.read_text())
    names, address = certificate_names(host, lan_ip)
    root_before = Path(config['ca_certificate_file']).read_bytes()
    token_before = config['device_token']
    private_json(directory / 'server-config.before-address-change.json', config)
    private_json(directory / 'device-config.before-address-change.json', device)
    private_write(directory / 'server.before-address-change.crt.pem', Path(config['server_certificate_file']).read_text())
    conf = directory / 'server.cnf'
    private_write(conf, server_certificate_config(names))
    csr, leaf = directory / 'server.next.csr.pem', directory / 'server.next.crt.pem'
    commands = [
        ['openssl', 'req', '-new', '-key', config['server_private_key_file'], '-config', str(conf), '-out', str(csr)],
        ['openssl', 'x509', '-req', '-in', str(csr), '-CA', config['ca_certificate_file'],
         '-CAkey', str(directory / 'ca.key.pem'), '-set_serial', str(secrets.randbits(128)), '-days', '3',
         '-extfile', str(conf), '-extensions', 'server', '-out', str(leaf)],
        ['openssl', 'verify', '-CAfile', config['ca_certificate_file'], str(leaf)],
    ]
    previous_mask = os.umask(0o077)
    try:
        for command in commands:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        leaf.chmod(0o600)
        os.replace(leaf, config['server_certificate_file'])
    finally:
        os.umask(previous_mask)
    config.update(host=host, origin=f"https://{host}:{config['port']}", listen_host='0.0.0.0', lan_ip_at_issue=address)
    device.update(host=host, url=f"wss://{host}:{config['port']}/device/v1/ws")
    assert config['device_token'] == token_before and device['device_token'] == token_before
    assert Path(config['ca_certificate_file']).read_bytes() == root_before
    private_json(config_file, config)
    private_json(device_file, device)
    return config_file


class MockQwen:
    """Provider socket interface; no DNS, HTTP client or paid-model fallback."""
    def __init__(self):
        self.incoming, self.results = asyncio.Queue(), asyncio.Queue()
        self.closed, self.close_code = False, None
        self.tools, self.audio_packets, self.continuations = [], 0, 0
        self.last_response = None
        self.push({'type': 'session.created'})

    def push(self, event):
        if event.get('type') == 'response.created':
            self.last_response = event['response']['id']
        self.incoming.put_nowait(WSMessage(WSMsgType.TEXT, json.dumps(event), ''))

    async def send_json(self, event):
        if self.closed:
            raise ConnectionError('Local mock closed')
        kind = event['type']
        if kind == 'session.update':
            self.tools = event['session'].get('tools', [])
            self.push({'type': 'session.updated'})
        elif kind == 'conversation.item.create':
            self.results.put_nowait(event['item'])
        elif kind == 'response.create':
            self.continuations += 1
        elif kind == 'input_audio_buffer.append':
            self.audio_packets += 1
        elif kind == 'response.cancel':
            self.push({'type': 'response.done', 'response': {'id': self.last_response, 'status': 'cancelled'}})

    async def receive(self, timeout=None):
        return await asyncio.wait_for(self.incoming.get(), timeout)

    async def close(self):
        self.closed, self.close_code = True, 1000
        self.incoming.put_nowait(WSMessage(WSMsgType.CLOSED, None, None))


class HardwareHarness:
    def __init__(self, config):
        self.config = config
        self.started = time.monotonic()
        self.motion_enabled = False
        self.models = []
        self.voice, self.http, self.reader = None, None, None
        self.voice_events = {}
        self.connection_events = []
        self.operation = asyncio.Lock()
        self.shutdown = asyncio.Event()
        # Explicit Config: no env file and no real model credentials.
        gateway_config = Config(api_key='local-mock-no-provider-access',
                                demo_password=config['demo_password'], live_enabled=True,
                                origin=config['origin'], stackchan_enabled=True,
                                stackchan_device_id=config['device_id'],
                                stackchan_device_token=config['device_token'])
        self.app = create_app(gateway_config, connector=self.connect_mock)
        self.hub = self.app[DEVICES]
        self.app.on_response_prepare.append(self.observe_response)

    def observe(self, stage, peer=None, **fields):
        # Deliberately omit request headers, URLs, bodies and exception text.
        address = peer[0] if isinstance(peer, tuple) and peer else peer
        try:
            address = str(ipaddress.ip_address(address)) if address else None
        except ValueError:
            address = None
        self.connection_events.append({'at_unix': round(time.time(), 3), 'stage': stage,
                                       'peer_ip': address, **fields})
        event = self.connection_events[-1]
        log_path = Path(self.config['state_file']).with_name('gateway-events.jsonl')
        if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
            os.replace(log_path, log_path.with_name('gateway-events.previous.jsonl'))
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        self.connection_events = self.connection_events[-32:]

    async def observe_response(self, request, response):
        if request.path == '/device/v1/ws':
            self.observe('device_http_response', request.remote, http_status=response.status)

    def transport_error(self, loop, context):
        error = context.get('exception')
        transport = context.get('transport')
        # asyncio may already have detached a failed TLS transport's protocol.
        # Diagnostics must still avoid falling back to raw exception output.
        try:
            peer = transport.get_extra_info('peername') if transport else None
        except (AttributeError, RuntimeError, OSError):
            peer = None
        if isinstance(error, ssl.SSLError):
            category = 'TLS_HANDSHAKE_FAILED'
            reason = getattr(error, 'reason', '')
            if reason in {'TLSV1_ALERT_UNKNOWN_CA', 'SSLV3_ALERT_BAD_CERTIFICATE',
                          'SSLV3_ALERT_CERTIFICATE_EXPIRED', 'TLSV1_ALERT_CERTIFICATE_UNKNOWN'}:
                category = 'CLIENT_REJECTED_CERTIFICATE'
        elif isinstance(error, ConnectionResetError):
            category = 'PEER_CLOSED_DURING_HANDSHAKE'
        else:
            category = 'LOCAL_TRANSPORT_ERROR'
        self.observe('transport_error', peer, category=category)

    async def connect_mock(self):
        model = MockQwen()
        self.models.append(model)
        return model

    def status(self):
        connection = self.hub.connection
        caps = connection.capabilities if connection else {}
        head = caps.get('head', {})
        return {'host': self.config['host'], 'listen_host': self.config.get('listen_host', self.config['host']),
                'port': self.config['port'], 'pid': os.getpid(),
                'mode': self.config['model_mode'], 'motion_enabled': self.motion_enabled,
                'device_connected': bool(connection and connection.online),
                'connection_id': connection.id if connection else None,
                'simulated': caps.get('simulated'),
                'head': head if isinstance(head, dict) else {},
                'command_count': len(self.hub.records),
                'model_session_count': len(self.models),
                'voice_session_active': bool(self.voice and not self.voice.closed),
                'control_session_active': self.hub.control is not None,
                'connection_events': self.connection_events,
                'voice_events': self.voice_events, 'uptime_seconds': round(time.monotonic() - self.started)}

    async def persist(self):
        previous_connection = None
        while True:
            connection = self.hub.connection
            current_id = connection.id if connection else None
            if current_id != previous_connection:
                self.observe('device_online' if current_id else 'device_offline', connection_id=current_id,
                             simulated=connection.capabilities.get('simulated') if connection else None)
                previous_connection = current_id
            private_json(self.config['state_file'], self.status())
            await asyncio.sleep(1)

    async def open_voice(self):
        if self.voice is not None and not self.voice.closed:
            return
        if self.http:
            await self.http.close()
        tls = ssl.create_default_context(cafile=self.config['ca_certificate_file'])
        self.http = ClientSession(cookie_jar=CookieJar(unsafe=True))
        base = f"https://127.0.0.1:{self.config['port']}"
        headers = {'Origin': self.config['origin']}
        async with self.http.post(base + '/api/login', json={'password': self.config['demo_password']},
                                  headers=headers, ssl=tls) as response:
            if response.status != 200:
                raise ValueError('Local test login failed')
        async with self.http.post(base + '/api/stackchan/bind', json={'device_id': self.config['device_id']},
                                  headers=headers, ssl=tls) as response:
            if response.status != 200:
                raise ValueError('Local device binding failed')
        self.voice = await self.http.ws_connect(base + '/ws', headers=headers, ssl=tls)
        if (await self.voice.receive_json(timeout=5)).get('type') != 'ready':
            raise ValueError('Local mock session did not become ready')
        self.reader = asyncio.create_task(self.drain_voice())

    async def drain_voice(self):
        async for message in self.voice:
            if message.type == WSMsgType.TEXT:
                kind = json.loads(message.data).get('type', 'unknown')
                self.voice_events[kind] = self.voice_events.get(kind, 0) + 1
            elif message.type == WSMsgType.BINARY:
                self.voice_events['audio'] = self.voice_events.get('audio', 0) + 1

    async def close_voice(self):
        if self.voice:
            await self.voice.close()
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.http:
            await self.http.close()
        self.voice, self.http, self.reader = None, None, None

    async def tool_calls(self, calls, duplicate=False):
        if not isinstance(calls, list) or not 1 <= len(calls) <= 8:
            raise ValueError('Invalid tool batch')
        names = {'stackchan_get_state', 'stackchan_get_command', 'stackchan_perform', 'stackchan_stop'}
        for call in calls:
            if not isinstance(call, dict) or call.get('name') not in names or not isinstance(call.get('arguments'), dict):
                raise ValueError('Invalid tool call')
            for frame in call['arguments'].get('frames', []):
                if 'head' in frame and not self.motion_enabled:
                    raise ValueError('Servo readiness has not been reported; head motion is gated')
                if 'head' in frame and any(abs(frame['head'][axis]) > 5 for axis in ('yaw_deg', 'pitch_deg')):
                    raise ValueError('Hardware harness limits tests to five degrees')
        await self.open_voice()
        model = self.models[-1]
        response_id = 'hw_' + secrets.token_hex(8)
        model.push({'type': 'response.created', 'response': {'id': response_id}})
        items = []
        for index, call in enumerate(calls):
            item = {'type': 'function_call', 'call_id': response_id + '_' + str(index),
                    'name': call['name'], 'arguments': json.dumps(call['arguments'], ensure_ascii=False)}
            items.append(item)
            model.push({'type': 'response.output_item.added', 'response_id': response_id,
                        'output_index': index, 'item': item})
            complete = {'type': 'response.function_call_arguments.done', 'response_id': response_id,
                        'output_index': index, **{k: item[k] for k in ('call_id', 'name', 'arguments')}}
            model.push(complete)
            if duplicate:
                model.push(complete)
        model.push({'type': 'response.done', 'response': {'id': response_id, 'status': 'completed', 'output': items}})
        results = []
        async with asyncio.timeout(25):
            for item in items:
                output = await model.results.get()
                if output.get('call_id') != item['call_id']:
                    raise ValueError('Unexpected local mock result ordering')
                results.append(json.loads(output['output']))
        return results

    async def dispatch(self, request):
        operation = request.get('op')
        if operation == 'status':
            return self.status()
        async with self.operation:
            if operation == 'preflight':
                await self.open_voice()
                count = len(self.models[-1].tools)
                await self.close_voice()
                return {'login_and_binding': True, 'registered_tool_count': count,
                        'command_count': len(self.hub.records), 'motion_enabled': self.motion_enabled}
            if operation == 'tool':
                return await self.tool_calls(request.get('calls'), request.get('duplicate') is True)
            if operation == 'servo_ready':
                # Called only after the human/firmware coordinator reports readiness.
                report_path = Path(request['report_file']).resolve()
                if report_path.parent != Path(self.config['state_file']).parent:
                    raise ValueError('Readiness report must be in the private test directory')
                report = json.loads(report_path.read_text())
                connection = self.hub.connection
                caps = connection.capabilities if connection else {}
                if (report.get('servo_ready') is not True or not connection or caps.get('simulated') is not False
                        or caps.get('head', {}).get('calibrated') is not True or caps.get('head', {}).get('enabled') is not True):
                    raise ValueError('Physical servo readiness is not confirmed')
                self.motion_enabled = True
                return {'motion_enabled': True}
            if operation in {'interrupt', 'speech_interrupt', 'disconnect'}:
                if operation == 'disconnect':
                    if self.hub.connection:
                        await self.hub.connection.socket.close(code=1001, message=b'Local test disconnect')
                elif operation == 'interrupt':
                    await self.open_voice()
                    await self.voice.send_json({'type': 'interrupt'})
                else:
                    await self.open_voice()
                    self.models[-1].push({'type': 'input_audio_buffer.speech_started'})
                    self.models[-1].push({'type': 'input_audio_buffer.speech_stopped'})
                return {'requested': operation}
            if operation == 'close_voice':
                await self.close_voice()
                return {'voice_closed': True}
        raise ValueError('Unknown local harness operation')

    async def control(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError('Invalid local request')
            result = {'ok': True, 'result': await self.dispatch(request)}
        except Exception:
            # Request arguments and exception text may contain private material.
            result = {'ok': False, 'error': 'LOCAL_TEST_REQUEST_REJECTED'}
        writer.write((json.dumps(result, ensure_ascii=False) + '\n').encode())
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def serve(self, install_signals=True):
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(self.config['server_certificate_file'], self.config['server_private_key_file'])
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        loop = asyncio.get_running_loop()
        previous_debug, previous_handler = loop.get_debug(), loop.get_exception_handler()
        # asyncio reports failures before the HTTP layer only in debug mode.
        # Our handler saves a fixed classification rather than raw TLS errors.
        loop.set_debug(True)
        loop.set_exception_handler(self.transport_error)
        control = None
        persist = None
        try:
            bind = self.config.get('listen_host', self.config['host'])
            hosts = {bind} if bind == '0.0.0.0' else {bind, '127.0.0.1'}
            for host in hosts:
                await web.TCPSite(runner, host, self.config['port'], ssl_context=tls).start()
            socket_path = Path(self.config['control_socket'])
            if socket_path.exists():
                raise ValueError('An existing control socket must be inspected before restart')
            control = await asyncio.start_unix_server(self.control, path=socket_path)
            socket_path.chmod(0o600)
            persist = asyncio.create_task(self.persist())
            self.observe('gateway_started', host=self.config['host'], listen_host=bind, port=self.config['port'],
                         model_mode=self.config['model_mode'], motion_enabled=False)
            print('Local TLS hardware gateway listening; motion gated; no model provider network.', flush=True)
            if install_signals:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, self.shutdown.set)
            await self.shutdown.wait()
        finally:
            if persist:
                persist.cancel()
                await asyncio.gather(persist, return_exceptions=True)
            if control:
                control.close()
                await control.wait_closed()
                Path(self.config['control_socket']).unlink(missing_ok=True)
            await self.close_voice()
            await runner.cleanup()
            self.observe('gateway_stopped')
            private_json(self.config['state_file'], {**self.status(), 'stopped': True})
            loop.set_debug(previous_debug)
            loop.set_exception_handler(previous_handler)


async def request_local(config, request):
    reader, writer = await asyncio.open_unix_connection(config['control_socket'])
    writer.write((json.dumps(request) + '\n').encode())
    await writer.drain()
    response = json.loads(await asyncio.wait_for(reader.readline(), 30))
    writer.close()
    await writer.wait_closed()
    return response


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'migrate-address', 'serve', 'status', 'preflight', 'request'])
    parser.add_argument('--directory', type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument('--host', default='kirindeMacBook-Air.local')
    parser.add_argument('--lan-ip')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--request-file', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'init':
            path = prepare(args.directory, args.host, args.port, args.lan_ip)
            print(json.dumps({'host': args.host, 'port': args.port, 'config_file': str(path)}))
            return 0
        if args.action == 'migrate-address':
            path = migrate_address(args.directory, args.host, args.lan_ip)
            print(json.dumps({'host': args.host, 'config_file': str(path)}))
            return 0
        config = json.loads((args.directory / 'server-config.json').read_text())
        if args.action == 'serve':
            asyncio.run(HardwareHarness(config).serve())
            return 0
        request = json.loads(args.request_file.read_text()) if args.action == 'request' else {'op': args.action}
        print(json.dumps(asyncio.run(request_local(config, request)), ensure_ascii=False))
        return 0
    except Exception:
        print('Local hardware harness failed; private configuration or request was not printed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
