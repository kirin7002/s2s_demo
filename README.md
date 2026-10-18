# S2S 个人助理 Agent

通过手机持续语音与个人助理交流，帮助解答问题、思考和规划日常事务。StackChan 是可接入的设备工具，
可用于动作、表情、灯光与屏幕显示。网关已实现可选设备绑定、设备WebSocket路由和原生函数调用闭环，
默认关闭，已通过模拟器、真实设备WSS及真实Flash工具/音频联调；手机真实麦克风尚未验收，持久记忆未接入。
能力与执行结果必须依据实际可用工具和真实设备回执，接纳命令不等于执行完成。

当前默认 **`qwen-audio-3.0-realtime-flash`**，移动网页 → 同源 Python 网关 → 阿里云 Qwen-Audio Realtime WebSocket。已用授权合成语音完成真实模型工具与PCM回传闭环；手机麦克风联调待用户参与。公网设备功能更新待管理员执行。

## 本地启动

Python 3.11+：

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m gateway.server
```

访问 http://127.0.0.1:8080 。默认关闭真实模型连接，页面会明确提示未启用；不会用模拟回复伪装真实模型。无 Key 时仍可运行全部本地测试。

Mac 已准备项目的启动命令与本次验证结果见 [Mac 本地启动](docs/local-macos.md)。

## 后续真实接入配置

仅在用户确认联调预算、地域和模型权限后启用真实连接。Mac 本地可在已忽略的 `.env` 中亲自填写 `DASHSCOPE_API_KEY` 和 `S2S_DEMO_PASSWORD`，也可使用运行时环境变量；不要通过聊天提供或提交仓库。配置示例见 [.env.example](.env.example)，使用方式见 [Mac 本地启动](docs/local-macos.md)。

服务启动时自动读取项目根目录 `.env`；优先级为进程环境变量（包括空字符串）→ `.env` → 默认值。文件只解析字面赋值，不执行 shell 或变量替换。没有 `.env` 且未显式启用时，真实连接仍默认关闭。

| 运行时环境变量 | 默认值 / 说明 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 未设置；北京（或所选地域）的百炼 DashScope API Key，服务端 Bearer 鉴权，浏览器不接收 |
| `QWEN_WORKSPACE_ID` | 可选，默认留空使用 Audio 公共端点；显式设置才改用对应业务空间专属域名 |
| `QWEN_REGION` | `cn-beijing`；亦支持 `ap-southeast-1`，Key 必须同地域 |
| `QWEN_MODEL` | `qwen-audio-3.0-realtime-flash`；无自动升级或型号回退 |
| `QWEN_VOICE` | 留空按型号选择默认音色；3.0 为 `longanqian` |
| `S2S_ENABLE_LIVE` | 默认关闭；显式 `1` 才允许出站模型连接 |
| `S2S_DEMO_PASSWORD` | 未设置；真实模式必需的演示登录口令，运行时注入 |
| `S2S_ORIGIN` | `http://127.0.0.1:8080`；必须精确匹配访问网页的 Origin |
| `S2S_STACKCHAN_ENABLED` | 默认 `0`；设为 `1` 且下两项有效才开放设备路由 |
| `S2S_STACKCHAN_DEVICE_ID` | 单个演示设备的固定ID；1–64个字母、数字、下划线或短横线 |
| `S2S_STACKCHAN_DEVICE_TOKEN` | 独立设备Bearer凭据，32–256个字母、数字、下划线或短横线；只在服务端/设备端安全配置 |
| `S2S_STACKCHAN_TEST_MAX_DEG` | 可选联测绝对目标角度上限；正常使用不设置，仍受设备自身限位和速度限制 |
| `S2S_STACKCHAN_AUDIT` | 默认0；1只记录动作参数摘要、枚举表情及结果，不记录语音或屏幕文本 |

默认完整地址（无需 Workspace ID）：

```text
wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-flash
```

地域与型号的非秘密配置示例（仅配置，不启用模型）：

```sh
export QWEN_REGION=cn-beijing
export QWEN_MODEL=qwen-audio-3.0-realtime-flash
# QWEN_WORKSPACE_ID 默认不设置；DASHSCOPE_API_KEY 由安全运行环境单独注入。
```

新加坡 Audio 公共域名为 `dashscope-intl.aliyuncs.com`。显式设置有效 `QWEN_WORKSPACE_ID` 后，使用 `wss://{WorkspaceId}.{region}.maas.aliyuncs.com/api-ws/v1/realtime?model=...`。域名只能由固定地域映射或受限空间 ID 构造，浏览器不能提交任意上游地址。已有其他显式型号配置仍保留，不会自动切换型号；Omni 分支仍要求专属空间地址。旧 `qwen3-omni-flash-realtime-2025-09-15` 不使用新版配置，当前直接拒绝。

这里的凭据准确名称是 **DashScope API Key**，不是阿里云 AccessKey ID/Secret、NLS Access Token 或 OAuth Access Token。官方另提供临时 API Key（默认60秒，最长1800秒）；当前程序只透传安全注入的值，不申请、刷新或验证其有效期，也未做3.0真实兼容性联调。首次服务端联调建议使用北京地域的 DashScope API Key；不要在聊天、网页表单或仓库中填写。参见 [官方地址与鉴权](https://help.aliyun.com/zh/model-studio/fun-audiochat-realtime-websocket-api)、[临时 API Key](https://help.aliyun.com/zh/model-studio/generate-temporary-api-key)。

## 使用与移动端行为

- 点击开始即解锁 AudioContext，再请求麦克风权限；模型确认配置前不发送音频。
- AudioWorklet 采集并重采样为 16 kHz、16-bit LE 单声道 PCM，每 100 ms 上传；24 kHz 回复按到达顺序流式播放。
- 连续说话与停顿由 `smart_turn` 判断；可语音打断或手动打断，清空本地播放并抑制残余回复。
- 停止、断线、离线、设备断开、系统音频暂停、进入后台均释放音轨和播放资源。返回后点击重新连接，建立新上下文，不重放旧音频。
- 拒绝权限、等待权限时取消、反复点击、连接超时均可恢复。迟到的授权流会立即关闭。
- 仅本次页面显示转写，最多 100 条；没有 localStorage、音频落盘、服务端转写日志或跨会话记忆。清空显示不清除模型当前会话上下文；停止/重连才创建新会话。

手机必须使用可信 HTTPS；手机访问开发机 HTTP IP 不满足麦克风安全上下文。页面在 Chromium 移动视口验证；真实 iOS Safari / Android Chrome 的麦克风、锁屏、电话打断、蓝牙切换与回声仍需设备验收。重采样使用有状态面积平均，生产级抗混叠/回声效果尚未实测，建议耳机测试。

## 验证

```sh
pip install -r requirements-dev.txt
python -m unittest tests.test_gateway -v
python -m unittest tests.test_stackchan -v
python -m unittest tests.test_browser -v
node --test tests/test_capture.js
```

浏览器测试优先使用Mac已安装的Chrome或Linux Chromium，可用`S2S_TEST_BROWSER`指定路径。所有上游均为仅绑定本机的测试 WebSocket，麦克风是 Chromium 合成音频；权限/系统中断情境由测试注入，不代表真机验收。详见 [验证记录](docs/validation.md)。

## 边界与设计

每浏览器 WebSocket 对应一个上游会话；默认最多 2 个同时连接、最长 600 秒；建连 15 秒、协议静默 60 秒、单次发送 5 秒超时。浏览器有 30 秒启动超时、上传和播放积压上限。没有后台自动重连和重试付费请求。

服务仅绑定 `127.0.0.1:8080`，默认关闭真实模型；入口为 `/login` 和 `/chat` 两页。登录使用演示口令与内存 Cookie 会话，未配置口令且未启用模型时明确显示本地预览。Origin 校验用于浏览器同源约束。当前适合本机开发，不能直接作为公网多用户服务。后续部署前需加入正式账户鉴权、按用户预算/限流与可信 HTTPS 反代，不修改云安全组来绕过约束。

StackChan配置完成后，登录用户须在通话页主动关联唯一配置设备，下次语音会话才注册
`get_state`、`perform_targets`、`get_command`、`stop`四个工具（统一`stackchan_`前缀）。
模型只生成自由姿态/表情/灯光目标，代码计算时序与中间目标，设备做20ms轨迹插值。
完整目录另保留低层`perform`的auto/strict时序接口，正常语音不注册该接口。
未关联时模型没有设备工具。一个语音会话独占动作控制，其他会话收到`DEVICE_BUSY`，不抢占。
设备主动连接`/device/v1/ws`，公网须经可信TLS反代使用WSS；本轮没有部署该版本或调整反代。
所有工具结果区分`simulated=true/false/null`；模拟执行不能声称真实机器人已动作，未知标记也不能作为真实执行证据。
详情及本机模拟联调命令见[StackChan接入约定](docs/stackchan-integration.md)。

外部Memory Hub仍仅有[Python接口](gateway/ports.py)，没有记忆存储/同步或离线任务；MCP尚未实现。
[协议选型](docs/protocol.md) · [部署准备](docs/deployment.md)。
