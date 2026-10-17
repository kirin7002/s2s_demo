"""Launcher checks with fake credentials; no sockets, processes or provider calls."""
import contextlib
import io
import os
import unittest
from unittest.mock import patch, Mock

from scripts import start_live


class LiveLauncherTests(unittest.TestCase):
    def test_occupied_port_aborts_before_prompt_or_server(self):
        with patch.object(start_live.sys.stdin, "isatty", return_value=True), \
                patch.object(start_live.sys.stderr, "isatty", return_value=True), \
                patch.object(start_live.socket, "socket") as sock, \
                patch.object(start_live, "read_secret") as prompt:
            sock.return_value.__enter__.return_value.bind.side_effect = OSError("busy")
            with self.assertRaisesRegex(RuntimeError, "8080"):
                start_live.main([])
            prompt.assert_not_called()

    def test_noninteractive_input_rejected(self):
        with patch.object(start_live.sys.stdin, "isatty", return_value=False), \
                patch.object(start_live, "read_secret") as prompt:
            with self.assertRaisesRegex(RuntimeError, "交互终端"):
                start_live.main([])
            prompt.assert_not_called()

    def test_no_echo_fallback_or_empty_secret(self):
        def fallback(_):
            import warnings
            warnings.warn("no terminal", start_live.getpass.GetPassWarning)
            self.fail("must abort before reading echoed input")
        with patch.object(start_live.getpass, "getpass", side_effect=fallback):
            with self.assertRaises(start_live.getpass.GetPassWarning):
                start_live.read_secret("fake prompt")
        with patch.object(start_live.getpass, "getpass", return_value=" "):
            with self.assertRaises(RuntimeError):
                start_live.read_secret("fake prompt")

    def test_foreign_process_or_checkout_never_killed(self):
        cases = [
            ["/bin/sleep", "sleep 30", f"p123\nn{start_live.ROOT}", "p123\nn127.0.0.1:8080"],
            ["/usr/bin/Python", "Python -m gateway.server", "p123\nn/tmp/other", "p123\nn127.0.0.1:8080"],
            ["/usr/bin/Python", "Python -m gateway.server", f"p123\nn{start_live.ROOT}", "p123\nn*:8080"],
        ]
        for fields in cases:
            with self.subTest(fields=fields), \
                    patch.object(start_live, "process_field", side_effect=fields), \
                    patch.object(start_live.os, "kill") as kill:
                with self.assertRaises(RuntimeError):
                    start_live.stop_preview(123)
                kill.assert_not_called()

    def test_verified_preview_receives_only_term(self):
        fields = ["/usr/bin/Python", "Python -m gateway.server",
                  f"p123\nn{start_live.ROOT}", "p123\nn127.0.0.1:8080", ""]
        with patch.object(start_live, "process_field", side_effect=fields), \
                patch.object(start_live.os, "kill") as kill:
            start_live.stop_preview(123)
            kill.assert_called_once_with(123, start_live.signal.SIGTERM)

    def test_launch_uses_memory_only_and_loopback_without_connecting(self):
        output = io.StringIO()
        app = Mock()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(start_live.sys.stdin, "isatty", return_value=True), \
                patch.object(start_live.sys.stderr, "isatty", return_value=True), \
                patch.object(start_live, "check_port"), \
                patch.object(start_live, "read_secret", side_effect=["fake-key-only", "fake-password-only"]), \
                patch("gateway.server.create_app", return_value=app) as create, \
                patch("aiohttp.web.run_app") as run, \
                patch.object(start_live.socket, "socket", side_effect=AssertionError("network forbidden")), \
                contextlib.redirect_stdout(output):
            start_live.main([])
            config = create.call_args.args[0]
            self.assertTrue(config.live_enabled)
            self.assertEqual(config.api_key, "fake-key-only")
            self.assertEqual(config.demo_password, "fake-password-only")
            self.assertEqual(config.region, "cn-beijing")
            self.assertEqual(config.workspace, "")
            self.assertNotIn("DASHSCOPE_API_KEY", os.environ)
            self.assertNotIn("S2S_DEMO_PASSWORD", os.environ)
            self.assertEqual(os.environ["S2S_ENABLE_LIVE"], "1")
            run.assert_called_once_with(app, host="127.0.0.1", port=8080, access_log=None)
        self.assertNotIn("fake-key-only", output.getvalue())
        self.assertNotIn("fake-password-only", output.getvalue())


if __name__ == "__main__":
    unittest.main()
