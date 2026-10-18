# Mac 本地启动

准备日期：2026-10-17。项目目录：`/Users/kirin/go_path/s2s_demo`。

## 当前真实电脑联调入口（2026-10-18）

已运行的真实网关入口为 **http://localhost:8080/login**，仅监听127.0.0.1，不需安装CA。
使用根目录`.env`的`S2S_DEMO_PASSWORD`登录，在页面关联StackChan，再点击接通并允许麦克风。
设备仍以可信WSS连接`kirindeMacBook-Air.local:8443`，两个入口共享同一DeviceHub与授权控制，
各自精确校验Origin。`http://127.0.0.1:8080`不是此入口的配置Origin，请使用完整localhost链接。
电脑入口同时精确校验`Host: localhost:8080`；错误Host或WS Origin返回403，同源未登录WS返回401。
当前进程由`.venv/bin/python scripts/serve_s2s_tls.py`启动；不要再运行下文基础模式命令争用8080。
正常会话/协议限制、鉴权和停止保护保留，临时只读/±3°/90秒测试配置已移除。
只读诊断：`.venv/bin/python scripts/s2s_status.py --op get_state`，无需模型调用。
已确认电脑Chrome安全上下文与麦克风API存在、登录与绑定接口可用、真机在线；实际用户麦克风与连续通话待用户发起。
手机访问局域网HTTPS需另有可信证书，当前不要求安装全局CA，先按用户要求完成电脑真实语音。

以下为基础模式与早期准备说明；运行前先确认没有现存联调服务。

常规S2S配置位于此项目根目录`.env`，登录口令为`S2S_DEMO_PASSWORD`；不在聊天中提供或展示秘密。
另有`.run/stackchan-hardware/`私密目录用于机器人真机联测，包含临时TLS证书、设备凭据及独立测试口令。
它仍属于同一个仓库，但8443联测入口固定使用模拟模型，不是日常真实语音入口；
不要拿该目录的`demo_password`替代根目录正式配置。常规本机服务使用下述8080入口，需单独启动。

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

### 推荐：本地 `.env`

项目根目录 `.env` 已准备，权限为 `0600`（仅本人读写），并已被 Git 忽略。
用户只需用自己的编辑器填写 `DASHSCOPE_API_KEY` 和 `S2S_DEMO_PASSWORD`，不要把值发到聊天。
如果文件已存在，准备过程保留原文件。`.env.example` 只包含空白秘密字段和普通配置，可以提交。

当前准备的普通配置为真实模式、北京公共端点、`qwen-audio-3.0-realtime-flash`、
`http://127.0.0.1:8080`，Workspace ID 留空。填好后再停止旧预览并启动：

```sh
cd /Users/kirin/go_path/s2s_demo
.venv/bin/python -m gateway.server
```

无需再通过终端提示输入秘密，也无需操作终端应用 UI；可由 Codex 后台启动和维护进程。
加载配置、启动和登录都不主动连接模型；网页点击“开始”后才可能产生模型费用。
当前预览不会因为编辑文件而自动切换，必须重启才使用新配置。

配置优先级：进程环境变量（即使为空）高于项目根目录 `.env`，再高于代码默认值。
因此旧命令中的 `S2S_ENABLE_LIVE=0` 始终强制预览，适合继续无付费检查。
文件位置固定于项目根目录，不随执行命令的工作目录改变。
允许 `KEY=value`、空值、整行注释、值后空白加 `#` 注释以及单行单/双引号字面值。
引号内不转义；`$VAR`、`${VAR}`、反引号和 `$()` 都是普通文字，不替换、不执行。
不支持 `export`、重复字段、多行值或未知字段；格式错误只报告行号，不打印原行或凭据。
有空格、`#` 或另一种引号的口令建议用单/双引号包裹；不要在值中使用包裹它的同种引号。

### 终端交互启动（无需把秘密写进命令）

在自己的终端执行（已有 `.venv` 无需重新安装）：

```sh
/Users/kirin/go_path/s2s_demo/.venv/bin/python /Users/kirin/go_path/s2s_demo/scripts/start_live.py
```

按无回显提示亲自输入北京地域的 DashScope API Key 和自定演示登录口令；然后打开
<http://127.0.0.1:8080>，使用该口令登录。脚本固定北京公共端点、
`qwen-audio-3.0-realtime-flash`、`S2S_ENABLE_LIVE=1`，只监听 `127.0.0.1:8080`。
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
默认 `QWEN_REGION=cn-beijing`、`QWEN_MODEL=qwen-audio-3.0-realtime-flash`，Audio 公共端点无需 `QWEN_WORKSPACE_ID`。
确认预算和权限后才将启动命令的 `S2S_ENABLE_LIVE=0` 改为 `1`，重新启动。
`S2S_ORIGIN` 保持 `http://127.0.0.1:8080`。

程序现在支持自动加载本地 `.env`。不要将真实 Key 或口令放入命令行参数、仓库或聊天；网页登录仅输入演示口令。
本次仅创建秘密字段为空的本地配置，没有创建云资源或执行付费模型调用。

## 通话界面预览与浏览器回归（2026-10-18）

新的通话界面已在独立`http://127.0.0.1:8081`启动禁用模型的本地预览，可点击“进入本地预览”。
此进程由显式无Key、live=false的Config创建，不读取项目.env，不影响原8080进程；
后续接续时先核验进程仍在，勿把端口可用等同于真实模型已验证。
Mac浏览器回归现在优先使用已安装Chrome；可通过`S2S_TEST_BROWSER`指定其他Chromium路径。
`.venv/bin/python -m unittest tests.test_browser -v`已通过14项实际浏览器测试，
麦克风和上游均为本地fixture，不验证真实Safari/Android设备。
