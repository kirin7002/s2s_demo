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
LEGACY_DISK = DISK.replace(b'            dir https://', b'            ca https://', 1)
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

    def test_original_invalid_ca_field_is_corrected_only_in_target_site(self):
        base, corrected = repair.approved_config(LEGACY_DISK, SNIPPET)
        self.assertEqual(base, BASE)
        self.assertEqual(corrected, DISK)
        self.assertEqual(repair.prepare(LEGACY_DISK, SNIPPET, ACTIVE, adapter), CANDIDATE)

    def exercise_main(self, fail_reload):
        state = {'mounted': BASE, 'active': ACTIVE, 'failed': False, 'temp': b''}
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / 'Caddyfile'
            host.write_bytes(LEGACY_DISK)
            backup = Path(directory) / 'backup'
            backup.mkdir()

            def docker(*args, data=None):
                if args[1] == 's2s-demo':
                    return b's2s_demo\n'
                return json.dumps({'State': {'Running': True}, 'Mounts': [
                    {'Source': str(host), 'Destination': repair.MOUNT, 'RW': True}]}).encode()

            def execute(*args, data=None):
                if args[0] == 'mktemp':
                    return b'/tmp/s2s-caddy-fixture\n'
                if args[0] == 'sh' and args[2] == 'cat > "$1"':
                    state['temp'] = data
                    return b''
                if args[0] == 'cat':
                    return state['mounted']
                if args[0] == 'wget':
                    return json.dumps(state['active']).encode()
                if args[:2] == ('caddy', 'adapt'):
                    return json.dumps(adapter(state['temp'])).encode()
                if args[0] == 'sh':
                    state['mounted'] = data
                if args[:2] == ('caddy', 'reload'):
                    if fail_reload and not state['failed']:
                        state['failed'] = True
                        raise repair.RepairError('Synthetic failure')
                    state['active'] = json.loads(state['temp'])
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
            self.assertEqual(host.read_bytes(), LEGACY_DISK if fail_reload else DISK)
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

    def test_real_file_path_replaces_stdin_for_older_caddy(self):
        calls = []
        def execute(*args, data=None):
            calls.append((args, data))
            return b'/tmp/s2s-caddy-fixture\n' if args[0] == 'mktemp' else b'{}'
        with patch.object(repair, 'execute', execute):
            repair.config_command('adapt', b'sensitive-config', 'caddyfile')
        self.assertIn((('caddy', 'adapt', '--config', '/tmp/s2s-caddy-fixture', '--adapter', 'caddyfile'), None), calls)
        self.assertEqual(calls[-1], (('rm', '-f', '/tmp/s2s-caddy-fixture'), None))

    def test_temporary_config_is_removed_after_command_failure(self):
        calls = []
        def execute(*args, data=None):
            calls.append(args)
            if args[0] == 'caddy':
                raise repair.RepairError('Synthetic failure')
            return b'/tmp/s2s-caddy-fixture\n'
        with patch.object(repair, 'execute', execute), self.assertRaises(repair.RepairError):
            repair.config_command('adapt', b'sensitive-config', 'caddyfile')
        self.assertEqual(calls[-1], ('rm', '-f', '/tmp/s2s-caddy-fixture'))

    def test_command_error_reports_fixed_category_without_stderr_secrets(self):
        result = type('Result', (), {'returncode': 1, 'stderr': b'open -: no such file; fake-secret'})()
        with patch.object(repair.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(repair.RepairError, 'STDIN_CONFIG_UNSUPPORTED') as caught:
                repair.execute('caddy', 'adapt', '--config', '-')
        self.assertIn('stage=adapt', str(caught.exception))
        self.assertNotIn('fake-secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
