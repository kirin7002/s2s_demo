"""No Docker or secrets: verify update isolation and rollback decisions."""
import contextlib
import io
import unittest
from unittest.mock import patch

from deploy import update_ui as update


class UIDeployTests(unittest.TestCase):
    def test_runtime_values_never_appear_in_arguments(self):
        args = update.launch_args('s2s-demo:ui-test', 'a' * 40, 'proxy-id', {'KEY': 'fake-sensitive'})
        self.assertNotIn('fake-sensitive', repr(args))
        self.assertIn('--read-only', args)
        self.assertIn('container:proxy-id', args)

    def test_failure_restores_old_container_and_removes_only_owned_replacement(self):
        calls = []
        def docker(*args, **kwargs):
            calls.append(args)
            return b''
        candidate = {'Id': 'new-id', 'Config': {'Labels': {
            'com.kirin.project': 's2s_demo', 'com.kirin.ui_revision': 'a' * 40}}}
        def fail():
            raise update.UpdateError('synthetic health failure')
        with patch.object(update, 'docker', docker), patch.object(update, 'inspect', return_value=candidate), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(update.UpdateError):
            update.switch({'Id': 'old-id'}, 'new-image', 'a' * 40, 'proxy-id', {}, fail)
        self.assertIn(('rm', '-f', 'new-id'), calls)
        self.assertEqual(calls[-2:], [('rename', 'old-id', 's2s-demo'), ('start', 'old-id')])

    def test_unknown_container_is_never_removed(self):
        calls = []
        def docker(*args, **kwargs):
            calls.append(args)
            return b''
        def fail():
            raise update.UpdateError('synthetic health failure')
        with patch.object(update, 'docker', docker), \
                patch.object(update, 'inspect', return_value={'Id': 'unknown', 'Config': {'Labels': {}}}), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(update.UpdateError):
            update.switch({'Id': 'old-id'}, 'new-image', 'a' * 40, 'proxy-id', {}, fail)
        self.assertFalse(any(args[0] == 'rm' for args in calls))


if __name__ == '__main__':
    unittest.main()
