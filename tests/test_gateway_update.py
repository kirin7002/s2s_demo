"""No Docker execution: exercise rollback, isolation, and offline bundle hashes."""
import contextlib
import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy import update_gateway as update
from deploy.gateway_checks import OFFLINE_CHECK, health_check_code


class GatewayUpdateTests(unittest.TestCase):
    def test_manual_rollback_preserves_new_container_and_restores_previous(self):
        calls = []
        current = {'Id': 'new', 'Config': {'Labels': {'com.kirin.project': 's2s_demo',
                                                   'com.kirin.gateway_revision': 'a' * 40}}}
        previous = {'Id': 'old', 'Config': {'Labels': {'com.kirin.project': 's2s_demo'}}}
        with patch.object(update, 'inspect', side_effect=[current, previous]), \
                patch.object(update, 'docker', lambda *a, **k: calls.append(a) or b''), \
                contextlib.redirect_stdout(io.StringIO()):
            update.rollback_gateway('a' * 40)
        self.assertEqual(calls[-2:], [('rename', 'old', 's2s-demo'), ('start', 'old')])
        self.assertFalse(any(c[0] == 'rm' for c in calls))

    def test_manual_rollback_refuses_unknown_current_container(self):
        with patch.object(update, 'inspect', return_value={'Id': 'other', 'Config': {'Labels': {}}}), \
                patch.object(update, 'docker') as docker:
            with self.assertRaises(update.UpdateError):
                update.rollback_gateway('a' * 40)
        docker.assert_not_called()

    def test_values_stay_out_of_arguments_and_proxy_is_not_changed(self):
        calls = []
        def docker(*args, **kwargs):
            calls.append((args, kwargs))
            return b''
        with patch.object(update, 'docker', docker):
            update.switch_gateway({'Id': 'old'}, 'image', 'a' * 40, 'proxy',
                                  {'DASHSCOPE_API_KEY': 'synthetic-secret'}, lambda: None)
        run, kwargs = next(c for c in calls if c[0][0] == 'run')
        self.assertNotIn('synthetic-secret', repr(run))
        self.assertEqual(kwargs['environment']['DASHSCOPE_API_KEY'], 'synthetic-secret')
        self.assertIn('container:proxy', run)
        self.assertIn('--read-only', run)
        self.assertFalse(any(c[0][0] in ('restart', 'exec', 'cp') for c in calls))

    def test_failed_health_restores_old_and_only_removes_our_new_container(self):
        calls = []
        candidate = {'Id': 'new', 'Config': {'Labels': {'com.kirin.project': 's2s_demo',
                                                     'com.kirin.gateway_revision': 'a' * 40}}}
        def fail():
            raise update.UpdateError('fixture failure')
        with patch.object(update, 'docker', lambda *a, **k: calls.append(a) or b''), \
                patch.object(update, 'inspect', return_value=candidate), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(update.UpdateError):
                update.switch_gateway({'Id': 'old'}, 'image', 'a' * 40, 'proxy', {}, fail)
        self.assertIn(('rm', '-f', 'new'), calls)
        self.assertEqual(calls[-2:], [('rename', 'old', 's2s-demo'), ('start', 'old')])

    def test_unknown_replacement_is_not_removed(self):
        calls = []
        def fail():
            raise update.UpdateError('fixture failure')
        with patch.object(update, 'docker', lambda *a, **k: calls.append(a) or b''), \
                patch.object(update, 'inspect', return_value={'Id': 'other', 'Config': {'Labels': {}}}), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(update.UpdateError):
                update.switch_gateway({'Id': 'old'}, 'image', 'a' * 40, 'proxy', {}, fail)
        self.assertFalse(any(c[0] == 'rm' for c in calls))

    def test_bundle_hash_and_path_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {'revision': 'a' * 40, 'files': {}}
            for folder in ('gateway', 'web', 'wheelhouse', 'deploy'):
                (root / folder).mkdir()
                name = folder + '/fixture'
                (root / name).write_text('fixture')
                manifest['files'][name] = hashlib.sha256(b'fixture').hexdigest()
            (root / 'gateway-manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(update.verified_manifest(root), 'a' * 40)
            (root / 'deploy/fixture').write_text('tampered updater')
            with self.assertRaises(update.UpdateError):
                update.verified_manifest(root)
            (root / 'deploy/fixture').write_text('fixture')
            (root / 'gateway/fixture').write_text('tampered')
            with self.assertRaises(update.UpdateError):
                update.verified_manifest(root)
            manifest['files'] = {'../outside': 'ignored'}
            (root / 'gateway-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(update.UpdateError):
                update.verified_manifest(root)

    def test_current_catalog_and_flat_voice_registration_pass_offline_check(self):
        exec(compile(OFFLINE_CHECK, '<offline-release-check>', 'exec'), {})


class GatewayHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_health_checks_login_binding_assets_and_auth_without_model(self):
        from aiohttp.test_utils import TestServer
        from gateway.config import Config
        from gateway.server import create_app, ACTIVE, DEVICES, ROOT
        cfg = Config(live_enabled=True, api_key='synthetic-key', demo_password='synthetic-password',
                     origin='https://s2s.kirinxu.cn', stackchan_enabled=True,
                     stackchan_device_id='synthetic-device', stackchan_device_token='x' * 48)
        async def no_model():
            raise AssertionError('Release health must not open a model connection')
        app = create_app(cfg, connector=no_model)
        manifest = {'files': {'web/' + p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in ROOT.iterdir() if p.is_file()}}
        async with TestServer(app) as server:
            code = health_check_code(manifest).replace('http://127.0.0.1:8080', str(server.make_url('')).rstrip('/'))
            with patch.object(Config, 'from_env', return_value=cfg):
                await asyncio.to_thread(exec, compile(code, '<release-health>', 'exec'), {})
            self.assertFalse(app[ACTIVE])
            self.assertFalse(app[DEVICES].bindings)
            self.assertFalse(app[DEVICES].controllers)
