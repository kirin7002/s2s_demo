# S2S 持续语音对话网关

本轮默认 **`qwen-audio-3.0-realtime-plus`**，移动网页 → 同源 Python 网关 → 阿里云 Qwen-Audio Realtime WebSocket。代码已实现，尚未用真实模型或真实手机麦克风联调。没有自动部署或付费调用。

## 本地启动

Python 3.11+：

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m gateway.server
```

访问 http://127.0.0.1:8080 。默认关闭真实模型连接，页面会明确提示未启用；不会用模拟回复伪装真实模型。无 Key 时仍可运行全部本地测试。

## 后续真实接入配置

仅在用户确认联调预算、地域和模型权限后，由用户通过其部署环境的秘密管理安全注入 `DASHSCOPE_API_KEY`，不用聊天提供或写入仓库。本轮没有创建、保存或配置真实凭据。

| 运行时环境变量 | 默认值 / 说明 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 未设置；服务端 Bearer 鉴权，浏览器不接收 |
| `QWEN_WORKSPACE_ID` | 未设置；百炼业务空间 ID |
| `QWEN_REGION` | `cn-beijing`；亦支持 `ap-southeast-1`，Key 必须同地域 |
| `QWEN_MODEL` | `qwen-audio-3.0-realtime-plus`；无自动升级或型号回退 |
| `QWEN_VOICE` | 留空按型号选择默认音色；3.0 为 `longanqian` |
| `S2S_ENABLE_LIVE` | 默认关闭；显式 `1` 才允许出站模型连接 |
| `S2S_DEMO_PASSWORD` | 未设置；真实模式必需的演示登录口令，运行时注入 |
| `S2S_ORIGIN` | `http://127.0.0.1:8080`；必须精确匹配访问网页的 Origin |

模型 URL 由受限空间 ID、地域与型号构造，不能从浏览器提交任意上游地址。支持的其他显式型号：`qwen-audio-3.0-realtime-flash`、`qwen-audio-3.1-realtime-plus` 和独立 Omni 配置分支（3.8 Flash、3.5 Plus/Flash）。旧 `qwen3-omni-flash-realtime-2025-09-15` 不使用新版配置，当前直接拒绝。

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
python -m unittest tests.test_browser -v
node --test tests/test_capture.js
```

浏览器测试使用本环境 `/usr/bin/chromium`；其他环境可调整 `tests/test_browser.py` 的 `executable_path`。所有上游均为仅绑定本机的测试 WebSocket，麦克风是 Chromium 合成音频；权限/系统中断情境由测试注入，不代表真机验收。详见 [验证记录](docs/validation.md)。

## 边界与设计

每浏览器 WebSocket 对应一个上游会话；默认最多 2 个同时连接、最长 600 秒；建连 15 秒、协议静默 60 秒、单次发送 5 秒超时。浏览器有 30 秒启动超时、上传和播放积压上限。没有后台自动重连和重试付费请求。

服务仅绑定 `127.0.0.1:8080`，默认关闭真实模型；入口为 `/login` 和 `/chat` 两页。登录使用演示口令与内存 Cookie 会话，未配置口令且未启用模型时明确显示本地预览。Origin 校验用于浏览器同源约束。当前适合本机开发，不能直接作为公网多用户服务。后续部署前需加入正式账户鉴权、按用户预算/限流与可信 HTTPS 反代，不修改云安全组来绕过约束。

工具、外部 Memory Hub 仅有 [Python 接口](gateway/ports.py)，运行时不调用。没有车控、Memory Hub 存储/同步、离线任务或新云资源。[协议选型](docs/protocol.md) · [部署准备](docs/deployment.md)。
