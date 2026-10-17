# s2s_demo 开发交接

更新日期：2026-10-17。仓库：`https://github.com/kirin7002/s2s_demo.git`。
本地根目录：`/Users/kirin/go_path/s2s_demo`。
用户希望后续直接在 Codex 的正式 `s2s_demo` 项目中聊天开发，无需经 Dot 中转。

## 最新状态：2026-10-18

用户已授权阿里云部署。无秘密代码已准备在服务器`47.116.102.49`的
`/home/kirin/apps/s2s_demo/releases/3f670692f010`；禁用模型的服务器启动检查通过。
拟用`s2s.kirinxu.cn`，DNS已指向服务器，证书尚未开通。kirin不能免密sudo/管理Docker，
远端`shared/.env`秘密留空；未复制本地秘密、未修改Caddy/安全组/防火墙。
需用户确认新增公开站点并由管理员完成安全凭据交接与激活，见[部署说明](deployment.md)。

用户再次重新授权并更换 Key 后，已重启加载最新 `.env`，当前 PID67839，仅监听127.0.0.1:8080。
登录页HTTP200；一次无音频验证取得HTTP101、`session.created`、当前完整配置的`session.updated`。
因此最新Key的初始鉴权与配置确认已通过，下面旧Key的额度问题不再代表当前状态。
尚未做真实音频/生成验收；没有修改计费设置，不读取或输出秘密。后续操作前仍须重新核验PID。

当前模型为 `qwen-audio-3.0-realtime-flash`。用户已自行填写本地 `.env`；不要读取、打印或迁移秘密。
真实连接初始鉴权可通过，但账户免费额度已耗尽且仍启用仅免费调用模式。一次空会话配置诊断确认
服务端关闭码 1007，178 字节关闭原因说明配额问题；该帧超过控制帧上限125字节，导致客户端显示1002。
见 [当前根因及证据](validation.md)。停止参数试错，不擅自切换付费开关、套餐或型号。
当时本机服务 PID 为60701，仅作历史记录，当前PID见本节顶部；操作前重新核验进程身份。
真实音频与生成均未做验收。以下启动准备、旧PID和“未配置”等描述是历史记录。

## 代码和本地环境

- `main` 的上游代码基线：`01bfd3e934de8b6d907fc2cfa5fc07046fa3020c`，Audio 公共端点版。
- 本地启动文档提交：`77a348b57352f50793d8e671ac5b90ea822463e0`，只修改 README 并增加 `docs/local-macos.md`；未推送。
- 本文为后续交接文档，不更改 Demo 实现。接续时先执行 `git status --short --branch` 和 `git log -3 --oneline` 确认最新状态，勿覆盖用户更改。
- 项目内 `.venv` 已用 Python 3.12.14 准备，已安装 `requirements-dev.txt`；Mac 默认系统 Python 3.9.6 不适用。

## 预览启动

```sh
cd /Users/kirin/go_path/s2s_demo
S2S_ENABLE_LIVE=0 S2S_ORIGIN=http://127.0.0.1:8080 .venv/bin/python -m gateway.server
```

用户自行访问 <http://127.0.0.1:8080>，点击“进入本地预览”。仅监听环回地址，按 `Ctrl+C` 停止。
本次交接未再次启动服务或模型。此前启动检查结束后已停止进程并确认 8080 无监听。

## 已测与未测

- 网关、认证及配置测试 24 项通过：`.venv/bin/python -m unittest tests.test_gateway -v`。
- 音频采集算法测试 4 项通过：`node --test tests/test_capture.js`。
- 三个前端 JS 语法检查和 `pip check` 通过。
- localhost 实际进程的页面、静态资源和预览登录通过；配置 `ready=false`，登录后 `/ws` 返回 503，模型连接保持禁用。
- 本机浏览器测试未运行。现有测试写死 Linux `/usr/bin/chromium`；Mac 需另准备测试浏览器路径。没有真实麦克风、手机或模型验证。
- 真实供应商握手和音频兼容性尚未验证；本地 fixture 通过不能代替真实联调。

## 配置与后续边界

上游交接报告称原云环境 `DASHSCOPE_API_KEY` 非空；这里只记录状态，没有重新检查云环境或读取秘密。
该状态不代表 Mac 已配置：此前本机检查中 `DASHSCOPE_API_KEY` 和 `S2S_DEMO_PASSWORD` 均未设置。
真实 Key、独立演示口令须由用户自行填写本地 `.env` 或安全注入运行环境；不要从 Vault、聊天或云环境迁移秘密。
应用现在自动读取项目根目录 `.env`，进程环境变量（包括空值）优先。文件只解析字面值，不执行 shell 或插值；本地准备文件秘密字段留空，权限 `0600`，Git 忽略。默认北京地域、`qwen-audio-3.0-realtime-flash`，Audio 公共端点无需 Workspace ID。
仅在用户确认权限和预算后才启用 `S2S_ENABLE_LIVE=1`。没有持久凭据、付费模型调用或云部署。

## Codex 项目注册状态

本次尝试通过受支持的 Computer Use 入口选择 Codex 应用时，工具拒绝访问 `com.openai.codex`（安全限制）；尚未成功添加正式项目，无法提供有效 projectId。
未编辑应用数据库、私有注册表或内部配置，也未尝试绕过该限制。后续需由可用的受支持项目添加入口或用户在桌面中添加上述已有目录，再通过 `list_projects` 核验，避免重复。
