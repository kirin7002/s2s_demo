# Mac 本地启动

准备日期：2026-10-17。项目目录：`/Users/kirin/go_path/s2s_demo`。

从 `https://github.com/kirin7002/s2s_demo.git` 的 `main` 新克隆，代码基线为
`01bfd3e934de8b6d907fc2cfa5fc07046fa3020c`。保持原 Demo 实现；本次只补充启动文档。

## 直接启动已准备好的项目

```sh
cd /Users/kirin/go_path/s2s_demo
S2S_ENABLE_LIVE=0 S2S_ORIGIN=http://127.0.0.1:8080 .venv/bin/python -m gateway.server
```

手动打开 <http://127.0.0.1:8080>，点击“进入本地预览”。本机当前未配置演示口令，预览无需填写密码。模型保持关闭，不会产生真实语音回复。终端按 `Ctrl+C` 停止。

必须使用上述 `127.0.0.1` 地址，Origin 默认按它校验。服务固定只监听 `127.0.0.1:8080`；当前实现不读取 `PORT` 或 `HOST` 环境变量。不要用 `localhost` 或局域网 IP 替换访问地址。

## 重新安装依赖时

本机默认 `/usr/bin/python3` 是 Python 3.9.6，不满足项目 Python 3.11+ 要求。
已使用 `/opt/homebrew/bin/python3.12`（3.12.14）创建项目内 `.venv` 并安装运行与测试依赖。

在新目录重新克隆后，可执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

仅运行页面可将最后一行改为 `-r requirements.txt`。不需要激活虚拟环境，不需要 Node 来运行页面；Node 用于采集算法测试。

## 本次实际验证

- Python 3.12.14，aiohttp 3.14.5，Node v26.4.0；`pip check` 通过。
- `.venv/bin/python -m unittest tests.test_gateway -v`：24 项通过，上游均为本机 fixture。
- `node --test tests/test_capture.js`：4 项通过。
- `node --check web/app.js`、`web/capture.js`、`web/login.js`：全部通过。
- 启动真实网关进程并以 HTTP 客户端验证：入口跳转、登录页、本地预览登录、对话页和四个静态资源均正常；`/api/config` 返回 `ready=false`、`password_required=false`；登录后 `/ws` 返回 503，阻止真实模型连接。
- `lsof` 确认仅监听 `127.0.0.1:8080`；检查后进程退出码 0，已停止。
- 本次遵循仅操作项目文件和命令的范围，没有运行浏览器测试。现有 `tests/test_browser.py` 写死 Linux `/usr/bin/chromium`，在 Mac 直接运行需先调整测试浏览器路径与准备对应浏览器。浏览器 UI、真实麦克风、手机和真实模型均未在本机验证。

## 真实对话仍需用户自行配置

### 终端交互启动（无需把秘密写进命令）

在自己的终端执行（已有 `.venv` 无需重新安装）：

```sh
/Users/kirin/go_path/s2s_demo/.venv/bin/python /Users/kirin/go_path/s2s_demo/scripts/start_live.py
```

按无回显提示亲自输入北京地域的 DashScope API Key 和自定演示登录口令；然后打开
<http://127.0.0.1:8080>，使用该口令登录。脚本固定北京公共端点、
`qwen-audio-3.0-realtime-plus`、`S2S_ENABLE_LIVE=1`，只监听 `127.0.0.1:8080`。
凭据只保留在进程内存，不写文件、命令行参数、环境变量或日志，不从 Vault 读取。
输入无需显示出来；不要通过管道、重定向或聊天传入。

如果预览仍占用端口，脚本会在询问凭据前退出，不自动终止进程。
可在预览原终端按 `Ctrl+C`，或在上述命令末尾加 `--stop-preview-pid <预览PID>`。
这个显式选项只对命令、工作目录和环回监听端口均匹配本项目的指定 Python 进程发送
`SIGTERM`；检查不通过即退出，不会强杀。PID 应使用当前预览的实际 PID。
新的真实模式在当前终端前台运行，按 `Ctrl+C` 可靠停止；没有注册自启动。

启动和登录不验证 Key 的真实性或模型权限；点击页面“开始”才发起真实模型连接，
可能产生费用。请在接受费用风险后操作。脚本未代用户进行任何真实握手或付费测试。

本次检查只确认本地终端未设置 `DASHSCOPE_API_KEY`、`S2S_DEMO_PASSWORD`，没有读取任何秘密值。
用户自行在安全的运行环境中注入这两个变量；Key 应属于所选地域且有目标模型权限。
默认 `QWEN_REGION=cn-beijing`、`QWEN_MODEL=qwen-audio-3.0-realtime-plus`，Audio 公共端点无需 `QWEN_WORKSPACE_ID`。
确认预算和权限后才将启动命令的 `S2S_ENABLE_LIVE=0` 改为 `1`，重新启动。
`S2S_ORIGIN` 保持 `http://127.0.0.1:8080`。

程序直接读取进程环境，**不会自动加载 `.env`**。不要将真实 Key 或口令放入命令行参数、仓库、聊天或网页。
本次未创建 `.env`、持久凭据或云资源，未执行付费模型调用。
