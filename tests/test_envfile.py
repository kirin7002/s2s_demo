"""Only temporary fake .env files are read; no real project credentials or network."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gateway.config import Config
from gateway.envfile import read_env_file


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, content):
        self.path.write_text(content, encoding="utf-8")

    def test_missing_and_empty_file_keep_live_disabled(self):
        self.assertFalse(Config.from_env(self.path).live_enabled)
        self.write("# nothing set\n\n")
        self.assertEqual(read_env_file(self.path), {})
        self.assertFalse(Config.from_env(self.path).live_enabled)

    def test_empty_secrets_block_live_and_quotes_preserve_literal_values(self):
        self.write('DASHSCOPE_API_KEY=\nS2S_DEMO_PASSWORD=""\nS2S_ENABLE_LIVE=1\n')
        self.assertIsNotNone(Config.from_env(self.path).problem())
        self.write('DASHSCOPE_API_KEY="fake # value" # note\n'
                   "S2S_DEMO_PASSWORD=' fake $VALUE `literal` \\n ' # note\n"
                   "QWEN_WORKSPACE_ID= # empty\n")
        values = read_env_file(self.path)
        self.assertEqual(values["DASHSCOPE_API_KEY"], "fake # value")
        self.assertEqual(values["S2S_DEMO_PASSWORD"], " fake $VALUE `literal` \\n ")
        self.assertEqual(values["QWEN_WORKSPACE_ID"], "")
        self.assertNotIn("DASHSCOPE_API_KEY", os.environ)

    def test_environment_wins_including_explicit_empty_values(self):
        self.write("DASHSCOPE_API_KEY=fake-file-key\nS2S_DEMO_PASSWORD=fake-file-pass\n"
                   "S2S_ENABLE_LIVE=1\nQWEN_REGION=cn-beijing\n")
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "", "S2S_ENABLE_LIVE": "0",
                                     "QWEN_REGION": "ap-southeast-1"}):
            cfg = Config.from_env(self.path)
        self.assertEqual(cfg.api_key, "")
        self.assertEqual(cfg.demo_password, "fake-file-pass")
        self.assertFalse(cfg.live_enabled)
        self.assertEqual(cfg.region, "ap-southeast-1")

    def test_bad_lines_fail_without_exposing_any_input(self):
        for line in ['fake-sensitive-line', 'DASHSCOPE_API_KEY="fake-sensitive-line',
                     'DASHSCOPE_API_KEY="fake-sensitive-line" trailing',
                     'DASHSCOPE_API_KEY="fake-sensitive-line" "extra"',
                     'export DASHSCOPE_API_KEY=fake-sensitive-line',
                     'UNKNOWN=fake-sensitive-line', 'DASHSCOPE_API_KEY=fake-sensitive-line\x00',
                     'DASHSCOPE_API_KEY=fake-sensitive-line\nDASHSCOPE_API_KEY=again']:
            with self.subTest(line=line):
                self.write(line)
                with self.assertRaises(ValueError) as error:
                    read_env_file(self.path)
                self.assertNotIn("fake-sensitive-line", str(error.exception))
                self.assertIn("行格式无效", str(error.exception))

    def test_shell_payloads_are_literal_and_never_executed(self):
        marker = Path(self.temp.name) / "must-not-exist"
        payload = f"$(touch {marker})"
        self.write(f"DASHSCOPE_API_KEY={payload}\nS2S_DEMO_PASSWORD=`touch {marker}`\n")
        with patch("subprocess.run", side_effect=AssertionError("no execution")), \
                patch("os.system", side_effect=AssertionError("no execution")):
            self.assertEqual(read_env_file(self.path)["DASHSCOPE_API_KEY"], payload)
        self.assertFalse(marker.exists())

    def test_config_repr_and_loading_do_not_print_secrets(self):
        self.write("DASHSCOPE_API_KEY=fake-private-key\nS2S_DEMO_PASSWORD=fake-private-pass\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            cfg = Config.from_env(self.path)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("fake-private", repr(cfg))

    def test_invalid_encoding_is_redacted(self):
        self.path.write_bytes(b"DASHSCOPE_API_KEY=fake-private\xff")
        with self.assertRaises(ValueError) as error:
            read_env_file(self.path)
        self.assertNotIn("fake-private", str(error.exception))


if __name__ == "__main__":
    unittest.main()
