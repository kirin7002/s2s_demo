"""Interactive localhost launcher. Never reads credentials from files or Vault."""
import argparse
import getpass
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import warnings

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8080"


def process_field(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def stop_preview(pid):
    """Only stop the explicitly named Python gateway in this checkout."""
    if pid <= 1:
        raise RuntimeError("预览 PID 无效。")
    command = process_field("/bin/ps", "-p", str(pid), "-o", "comm=")
    args = process_field("/bin/ps", "-p", str(pid), "-o", "args=").split()
    cwd = process_field("/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn")
    listener = process_field("/usr/sbin/lsof", "-nP", "-a", "-p", str(pid),
                             "-iTCP:8080", "-sTCP:LISTEN", "-Fn")
    if (Path(command).name.lower() not in ("python", "python3", "python3.12")
            or len(args) < 3 or args[-2:] != ["-m", "gateway.server"]
            or f"n{ROOT}" not in cwd.splitlines()
            or "n127.0.0.1:8080" not in listener.splitlines()):
        raise RuntimeError("无法确认指定 PID 是本目录的本地预览；未停止任何进程。")
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not process_field("/usr/sbin/lsof", "-nP", "-a", "-p", str(pid),
                             "-iTCP:8080", "-sTCP:LISTEN", "-Fn"):
            return
        time.sleep(0.1)
    raise RuntimeError("预览尚未释放端口；请稍后重试，不会强制终止。")


def check_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 8080))
        except OSError:
            raise RuntimeError("无法使用 127.0.0.1:8080；请检查端口占用和本机权限，未读取凭据。") from None


def read_secret(prompt):
    # getpass otherwise falls back to echoed stdin when no terminal is available.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(prompt)
    if not value.strip():
        raise RuntimeError("输入不能为空；启动已取消。")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="无回显输入凭据，启动本地真实 S2S。")
    parser.add_argument("--stop-preview-pid", type=int,
                        help="切换前停止指定的本项目预览 PID；身份检查不通过则退出")
    options = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise RuntimeError("请在自己的交互终端运行，勿使用管道或重定向。")
    if options.stop_preview_pid is not None:
        stop_preview(options.stop_preview_pid)
    check_port()
    print("真实模式：点击网页‘开始’后将连接模型，可能收费。Ctrl+C 停止服务。")
    api_key = read_secret("DashScope API Key（北京地域，输入不显示）: ")
    password = read_secret("自定演示登录口令（输入不显示，稍后网页登录使用）: ")
    sys.path.insert(0, str(ROOT))
    from aiohttp import web
    from gateway.config import Config
    from gateway.server import create_app

    # Secrets remain in this process's memory, not argv, environment or files.
    os.environ["S2S_ENABLE_LIVE"] = "1"
    os.environ["S2S_ORIGIN"] = ORIGIN
    config = Config(api_key=api_key, demo_password=password, live_enabled=True, origin=ORIGIN)
    print(f"打开 {ORIGIN}，用刚输入的演示口令登录。尚未验证 Key 或模型权限。")
    web.run_app(create_app(config), host="127.0.0.1", port=8080, access_log=None)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"启动取消：{error}", file=sys.stderr)
        sys.exit(1)
    except (getpass.GetPassWarning, EOFError):
        print("启动取消：请确认端口可用，并在交互终端输入非空凭据。", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
