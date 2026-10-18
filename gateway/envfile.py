"""Small literal .env reader: no shell, expansion, execution or env mutation."""
from pathlib import Path
import re

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
ALLOWED_KEYS = frozenset({
    "DASHSCOPE_API_KEY", "S2S_DEMO_PASSWORD", "S2S_ENABLE_LIVE", "S2S_ORIGIN",
    "QWEN_REGION", "QWEN_MODEL", "QWEN_WORKSPACE_ID", "QWEN_VOICE",
    "S2S_STACKCHAN_ENABLED", "S2S_STACKCHAN_DEVICE_ID", "S2S_STACKCHAN_DEVICE_TOKEN",
    "S2S_STACKCHAN_TEST_MAX_DEG",
    "S2S_STACKCHAN_AUDIT",
})


def read_env_file(path=DEFAULT_ENV_FILE):
    if path is None:
        return {}
    try:
        content = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        raise ValueError("无法读取本地 .env；请检查文件权限和 UTF-8 编码。") from None
    values = {}
    for number, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()

        def invalid():
            # Never include the input line, key or value in diagnostics.
            return ValueError(f"本地 .env 第 {number} 行格式无效，请检查赋值和引号。")

        if not separator or key not in ALLOWED_KEYS or key in values:
            raise invalid()
        if value.startswith(("'", '"')):
            match = re.fullmatch(r'''(?:'([^']*)'|"([^"]*)")(?:\s+#.*)?''', value)
            if not match:
                raise invalid()
            value = match.group(1) if value.startswith("'") else match.group(2)
        else:
            value = "" if value.startswith("#") else re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            if "'" in value or '"' in value:
                raise invalid()
        if "\x00" in value:
            raise invalid()
        values[key] = value
    return values
