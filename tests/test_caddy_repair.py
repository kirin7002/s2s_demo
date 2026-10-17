"""Repair safeguards with synthetic configuration and mocked Docker only."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy import repair_caddy_s2s as repair


SNIPPET = Path('deploy/Caddyfile.s2s').read_bytes()
BASE = b'old.example {\n respond "old"\n}\n'
DISK = BASE + SNIPPET
ACTIVE = {'old': True}
CANDIDATE = {'old': True, 'apps': {
    'http': {'servers': {'srv0': {'routes': [{'match': [{'host': ['s2s.kirinxu.cn']}]}]}}},
    'tls': {'automation': {'policies': [{'subjects': ['s2s.kirinxu.cn'], 'issuers': [
        {'module': 'acme', 'ca': 'https://acme-v02.api.letsencrypt.org/directory'}]}]}}}}


def adapter(data):
    return ACTIVE if data == BASE else CANDIDATE


class CaddyRepairTests(unittest.TestCase):
    def test_only_approved_site_can_be_added(self):
        self.assertEqual(repair.prepare(DISK, SNIPPET, ACTIVE, adapter), CANDIDATE)

    def test_unrelated_difference_is_rejected(self):
        with self.assertRaisesRegex(repair.RepairError, 'outside S2S'):
            repair.prepare(DISK, SNIPPET, {'different': True}, adapter)

    def test_duplicate_or_modified_site_is_rejected(self):
        for disk in [DISK + SNIPPET, DISK.replace(b'8080', b'8081')]:
            with self.assertRaises(repair.RepairError):
                repair.prepare(disk, SNIPPET, ACTIVE, adapter)

    def exercise_main(self, fail_reload):
        state = {'mounted': BASE, 'active': ACTIVE, 'failed': False}
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / 'Caddyfile'
            host.write_bytes(DISK)
            backup = Path(directory) / 'backup'
            backup.mkdir()

            def docker(*args, data=None):
                if args[1] == 's2s-demo':
                    return b's2s_demo\n'
                return json.dumps({'State': {'Running': True}, 'Mounts': [
                    {'Source': str(host), 'Destination': repair.MOUNT, 'RW': True}]}).encode()

            def execute(*args, data=None):
                if args[0] == 'cat':
                    return state['mounted']
                if args[0] == 'wget':
                    return json.dumps(state['active']).encode()
                if args[:2] == ('caddy', 'adapt'):
                    return json.dumps(adapter(data)).encode()
                if args[0] == 'sh':
                    state['mounted'] = data
                if args[:2] == ('caddy', 'reload'):
                    if fail_reload and not state['failed']:
                        state['failed'] = True
                        raise repair.RepairError('Synthetic failure')
                    state['active'] = json.loads(data)
                return b''

            output = io.StringIO()
            with patch.object(repair, 'HOST', host), patch.object(repair, 'docker', docker), \
                    patch.object(repair, 'execute', execute), patch.object(repair.os, 'geteuid', return_value=0), \
                    patch.object(repair.os, 'umask'), patch.object(repair.sys, 'argv', ['repair', '--apply-approved-site']), \
                    patch.object(repair.tempfile, 'mkdtemp', return_value=str(backup)), \
                    contextlib.redirect_stdout(output):
                if fail_reload:
                    with self.assertRaises(repair.RepairError):
                        repair.main()
                else:
                    repair.main()
            self.assertEqual(host.read_bytes(), DISK)
            self.assertEqual((backup / 'mounted.Caddyfile').read_bytes(), BASE)
        return state, output.getvalue()

    def test_stale_mount_is_synchronized_and_active_config_is_checked(self):
        state, output = self.exercise_main(False)
        self.assertEqual(state['mounted'], DISK)
        self.assertEqual(state['active'], CANDIDATE)
        self.assertIn('repair_applied=true', output)

    def test_failed_reload_restores_mounted_and_active_config(self):
        state, output = self.exercise_main(True)
        self.assertEqual(state['mounted'], BASE)
        self.assertEqual(state['active'], ACTIVE)
        self.assertIn('repair_rolled_back=true', output)


if __name__ == '__main__':
    unittest.main()
