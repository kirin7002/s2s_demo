"""IP TLS boundary and isolated container rollback; no cloud/model/device calls."""
import contextlib
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from deploy import device_ip_entry as entry


class EntryTests(unittest.TestCase):
    def test_sidecar_has_only_ip_device_path_and_verified_https_upstream(self):
        config = Path('deploy/device-ip.Caddyfile').read_text()
        for expected in ['admin off', 'default_sni 47.116.102.49', 'path /device/v1/ws', 'respond 404',
                         'https://caddy:443', 'header_up Host s2s.kirinxu.cn',
                         'tls_server_name s2s.kirinxu.cn', 'issuer internal', 'skip_install_trust']:
            self.assertIn(expected, config)
        for forbidden in ['tls_insecure_skip_verify', 'on_demand', 'insecure_secrets_log', '/api/', 'log {']:
            self.assertNotIn(forbidden, config)

    def test_launch_reuses_exact_image_and_has_no_gateway_credentials(self):
        plan = {'revision': 'a' * 40, 'image': 'sha256:' + 'b' * 64, 'network': 'caddy_default',
                'token': 'synthetic-secret', 'upstream_ip': '172.18.0.2', 'upstream_resolution': 'docker_dns'}
        args = entry.launch_args(Path('/release'), plan)
        self.assertIn(plan['image'], args)
        self.assertEqual(args[args.index('--entrypoint') + 1], '/usr/bin/caddy')
        self.assertEqual(args[args.index('--cap-add') + 1], 'NET_BIND_SERVICE')
        self.assertEqual(args[args.index('--cap-drop') + 1], 'ALL')
        self.assertIn('no-new-privileges:true', args)
        self.assertIn('0.0.0.0:8443:8443/tcp', args)
        self.assertIn('caddy_default', args)
        self.assertIn('--read-only', args)
        self.assertNotIn(plan['token'], repr(args))
        self.assertNotIn('DASHSCOPE', repr(args))
        self.assertNotIn('/opt/caddy', repr(args))
        self.assertNotIn('80:80', repr(args))
        self.assertNotIn('--add-host', args)

    def test_failed_health_removes_only_new_entry_and_restores_prior(self):
        calls = []
        candidate = {'Id': 'new', 'Config': {'Labels': {'com.kirin.project': entry.PROJECT,
                     'com.kirin.ip_entry_revision': 'a' * 40}}}
        plan = {'current': {'Id': 'old'}, 'revision': 'a' * 40}
        def docker(*args):
            calls.append(args)
            return b'new' if args[0] == 'run' else b''
        def failed(_):
            raise entry.EntryError('synthetic health failure')
        with patch.object(entry, 'launch_args', return_value=['run', 'synthetic-image']), \
                patch.object(entry, 'docker', docker), patch.object(entry, 'inspect', return_value=candidate):
            with self.assertRaises(entry.EntryError):
                entry.switch(Path('/release'), plan, failed)
        self.assertIn(('rm', '-f', 'new'), calls)
        self.assertEqual(calls[-2:], [('rename', 'old', entry.NAME), ('start', 'old')])
        self.assertFalse(any('caddy' in c or 's2s-demo' in c for c in calls))

    def test_failed_health_never_removes_unknown_container(self):
        calls = []
        plan = {'current': None, 'revision': 'a' * 40}
        with patch.object(entry, 'launch_args', return_value=['run', 'image']), \
                patch.object(entry, 'docker', side_effect=lambda *a: calls.append(a) or b'new'), \
                patch.object(entry, 'inspect', return_value={'Id': 'unknown', 'Config': {'Labels': {}}}):
            with self.assertRaises(entry.EntryError):
                entry.switch(Path('/release'), plan, lambda _: (_ for _ in ()).throw(entry.EntryError('failure')))
        self.assertFalse(any(c[0] == 'rm' for c in calls))

    def test_rename_failure_restarts_same_previous_entry(self):
        calls = []
        old = {'Id': 'old'}
        def docker(*args):
            calls.append(args)
            if args[0] == 'rename':
                raise entry.EntryError('synthetic rename failure')
            return b''
        with patch.object(entry, 'docker', docker), patch.object(entry, 'inspect', return_value=old):
            with self.assertRaises(entry.EntryError):
                entry.switch(Path('/release'), {'current': old, 'revision': 'a' * 40})
        self.assertEqual(calls[-1], ('start', 'old'))
        self.assertFalse(any(c[0] == 'rm' for c in calls))

    def test_empty_or_malformed_status_is_controlled_failure(self):
        for response in [b'', b'bad response\r\n', b'HTTP/1.1 nope\r\n']:
            ctx = MagicMock()
            ctx.wrap_socket.return_value.__enter__.return_value.recv.return_value = response
            with patch.object(entry.socket, 'create_connection', return_value=MagicMock()):
                with self.assertRaises(entry.EntryError):
                    entry.tls_request(ctx)

    def test_manifest_checks_scripts_and_configuration(self):
        import hashlib
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            files = {}
            for file in ['device_ip_entry.py', 'device-ip.Caddyfile']:
                (root / file).write_text('synthetic')
                (root / file).chmod(0o644)
                files[file] = hashlib.sha256(b'synthetic').hexdigest()
            (root / 'ip-entry-manifest.json').write_text(json.dumps({'revision': 'a' * 40, 'files': files}))
            self.assertEqual(entry.verify_bundle(root), 'a' * 40)
            (root / 'device-ip.Caddyfile').write_text('changed')
            with self.assertRaises(entry.EntryError):
                entry.verify_bundle(root)


class NetworkSelectionTests(unittest.TestCase):
    def caddy(self, names, project='caddy'):
        return {'Config': {'Labels': {'com.docker.compose.project': project} if project else {}},
                'NetworkSettings': {'Networks': {
                    name: {'IPAddress': ip, 'NetworkID': name + '-id'} for name, ip in names}}}

    def network(self, name, owner=None, compose_name=None):
        return {'Id': name + '-id', 'Driver': 'bridge', 'Options': None,
                'Labels': {'com.docker.compose.project': owner,
                           'com.docker.compose.network': compose_name} if owner else {}}

    def test_compose_default_selected_among_two_bridges_not_external_ai_gateway(self):
        caddy = self.caddy([('ai-gateway', '172.19.0.2'), ('caddy_default', '172.18.0.2')])
        networks = {'ai-gateway': self.network('ai-gateway', 'ai-gateway', 'default'),
                    'caddy_default': self.network('caddy_default', 'caddy', 'default')}
        result = entry.select_network(caddy, networks.__getitem__)
        self.assertEqual(result['network'], 'caddy_default')
        self.assertEqual(result['selection_reason'], 'compose_project_default')
        self.assertEqual(result['upstream_resolution'], 'docker_dns')
        self.assertEqual(result['upstream_ip'], '172.18.0.2')

    def test_multiple_owned_network_selection_is_deterministic(self):
        networks = {n: self.network(n, 'caddy', n) for n in ['z-backend', 'a-frontend']}
        for names in [[('z-backend', '172.19.0.2'), ('a-frontend', '172.18.0.2')],
                      [('a-frontend', '172.18.0.2'), ('z-backend', '172.19.0.2')]]:
            self.assertEqual(entry.select_network(self.caddy(names), networks.__getitem__)['network'], 'a-frontend')

    def test_legacy_bridge_uses_explicit_current_proxy_ip(self):
        result = entry.select_network(self.caddy([('bridge', '172.17.0.2')]),
                                      lambda _: self.network('bridge'))
        self.assertEqual(result['upstream_resolution'], 'extra_host')
        args = entry.launch_args(Path('/release'), {**result, 'revision': 'a' * 40,
                                                  'image': 'sha256:' + 'b' * 64})
        self.assertEqual(args[args.index('--add-host') + 1], 'caddy:172.17.0.2')

    def test_invalid_or_absent_endpoint_is_not_selected(self):
        for ip in ['', '0.0.0.0', '::1', '172.17.0.2;other', '127.0.0.1']:
            with self.assertRaises(entry.EntryError):
                entry.select_network(self.caddy([('bridge', ip)]), lambda _: self.network('bridge'))

    def test_another_projects_external_network_is_not_a_fallback(self):
        with self.assertRaises(entry.EntryError):
            entry.select_network(self.caddy([('ai-gateway', '172.19.0.2')]),
                                 lambda _: self.network('ai-gateway', 'ai-gateway', 'default'))

    def plan(self, resolution='extra_host'):
        network = 'bridge' if resolution == 'extra_host' else 'caddy_default'
        image = 'sha256:' + 'b' * 64
        current = {'Id': 'c' * 64, 'Image': image, 'State': {'Running': True},
                   'Config': {'Labels': {'com.kirin.project': entry.PROJECT,
                              'com.kirin.ip_entry_revision': 'a' * 40,
                              'com.kirin.ip_entry_upstream_ip': '172.18.0.2'}},
                   'HostConfig': {'NetworkMode': network,
                                  'ExtraHosts': ['caddy:172.18.0.2'] if resolution == 'extra_host' else []}}
        return {'current': current, 'revision': 'a' * 40, 'network': network,
                'network_id': network + '-id', 'upstream_ip': '172.18.0.2',
                'upstream_resolution': resolution, 'image': image}

    def test_reapply_refreshes_changed_mapping_only_when_needed(self):
        plan = self.plan()
        self.assertFalse(entry.needs_replacement(plan))
        plan['upstream_ip'] = '172.18.0.3'
        self.assertTrue(entry.needs_replacement(plan))
        plan['current']['Config']['Labels']['com.kirin.ip_entry_upstream_ip'] = '172.18.0.3'
        self.assertTrue(entry.needs_replacement(plan))  # stale /etc/hosts still triggers rebuild
        plan['current']['HostConfig']['ExtraHosts'] = ['caddy:172.18.0.3']
        self.assertFalse(entry.needs_replacement(plan))

    def test_dns_mode_tracks_proxy_ip_changes_and_unique_backup_name(self):
        plan = self.plan('docker_dns')
        self.assertFalse(entry.needs_replacement(plan))
        plan['upstream_ip'] = '172.18.0.4'
        self.assertTrue(entry.needs_replacement(plan))
        self.assertTrue(entry.backup_name(plan).endswith('-' + 'c' * 12))


class GatewayHelloBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_upgrade_without_hello_does_not_replace_device(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from gateway.config import Config
        from gateway.server import DEVICES, create_app
        from tests.test_stackchan import CAPABILITIES
        token = 'synthetic-token-for-device-0000000001'
        app = create_app(Config(stackchan_enabled=True, stackchan_device_id='test-device',
                                stackchan_device_token=token))
        async with TestServer(app) as server, ClientSession() as client:
            headers = {'Authorization': 'Bearer ' + token}
            first = await client.ws_connect(server.make_url('/device/v1/ws'), headers=headers)
            await first.send_json({'v': 1, 'type': 'device.hello', 'device_boot_id': 'fixture_boot',
                                   'firmware_version': 'fixture', 'capabilities': CAPABILITIES})
            await first.receive_json(timeout=2)
            connection = app[DEVICES].connection
            probe = await client.ws_connect(server.make_url('/device/v1/ws'), headers=headers)
            await probe.close()
            self.assertIs(app[DEVICES].connection, connection)
            self.assertTrue(connection.online)
            await first.close()


class IPTLSBoundaryTests(unittest.TestCase):
    def test_trusted_ip_without_sni_wrong_ca_wrong_ip_and_auth_boundaries(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            def openssl(*args):
                r = subprocess.run(['openssl', *args], cwd=root, capture_output=True)
                self.assertEqual(r.returncode, 0, 'Synthetic TLS fixture creation failed')
            openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=Test root',
                    '-keyout', 'root.key', '-out', 'root.crt', '-addext', 'basicConstraints=critical,CA:TRUE')
            openssl('req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=IP fixture',
                    '-keyout', 'leaf.key', '-out', 'leaf.csr')
            (root / 'ext').write_text('subjectAltName=IP:47.116.102.49\nbasicConstraints=CA:FALSE\n')
            openssl('x509', '-req', '-in', 'leaf.csr', '-CA', 'root.crt', '-CAkey', 'root.key', '-CAcreateserial',
                    '-days', '1', '-extfile', 'ext', '-out', 'leaf.crt')
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(root / 'leaf.crt', root / 'leaf.key')
            names = []
            ctx.set_servername_callback(lambda sock, name, initial: names.append(name))
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0));listener.listen();listener.settimeout(.2)
            port = listener.getsockname()[1]
            stopped = threading.Event()
            def serve():
                while not stopped.is_set():
                    try:
                        conn, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    try:
                        with conn, ctx.wrap_socket(conn, server_side=True) as tls:
                            data = b''
                            while b'\r\n\r\n' not in data:
                                chunk = tls.recv(2048)
                                if not chunk:break
                                data += chunk
                            if not data:continue
                            path = data.split(b' ')[1]
                            status = 404 if path != b'/device/v1/ws' else 101 if b'Bearer synthetic-token' in data else 401
                            tls.sendall(('HTTP/1.1 ' + str(status) + ' Fixture\r\nContent-Length: 0\r\n\r\n').encode())
                    except (ssl.SSLError, OSError):
                        conn.close()
            thread = threading.Thread(target=serve, daemon=True);thread.start()
            actual_request = entry.tls_request
            try:
                with patch.object(entry, 'tls_request', side_effect=lambda ctx, **kw: actual_request(ctx, port=port, **kw)):
                    results = entry.health({'root': (root / 'root.crt').read_text(), 'token': 'synthetic-token'})
                self.assertTrue(all(v is True for k, v in results.items() if k not in ['real_model_calls', 'device_actions']))
                self.assertTrue(names and all(name is None for name in names))
            finally:
                stopped.set();listener.close();thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
