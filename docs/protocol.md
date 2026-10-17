# 协议选择与接口边界

核对日期：2026-10-17。本仓库初始为空工作树和无提交 `work` 分支；远端没有 heads，`git fetch origin` 返回无 HEAD；没有 README、设计、checkout `.agents/skills` 或可识别的供应商配置。未输出任何配置值。

| 项目 | 本轮专用 Audio Realtime | 后续可选 Omni Realtime |
| --- | --- | --- |
| 显式默认型号 | `qwen-audio-3.0-realtime-plus` | 不自动启用；可显式选择 `qwen3.8-omni-flash-realtime` |
| 本轮匹配 | 纯语音连续对话 | 未来图像/视频场景 |
| 轮次字段 | `smart_turn` | `semantic_vad` |
| 格式配置 | 顶层 `input_audio_format` / `output_audio_format` | 嵌套 `audio.input.format` / `audio.output.format` |
| 默认音色配置 | 顶层 `voice=longanqian` | `audio.output.voice=Tina` |
| 音频链路 | 输入16k / 输出24k PCM16单声道 | 本项目也显式设为16k / 24k PCM |
| 自动打断 | Audio服务端自动取消旧响应，网关只清播放；手动按钮发cancel | 网关清播放并在有活动响应时发cancel |

依据：[Audio 指南](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)、[Audio 客户端事件](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)、[Omni 指南](https://help.aliyun.com/zh/model-studio/realtime)、[Omni 客户端事件](https://help.aliyun.com/zh/model-studio/client-events)。这是协议字段对照，不代表两个系列可互换会话配置。

Audio 默认使用北京公共端点 `wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-plus`，无需 Workspace ID；新加坡公共域名为 `dashscope-intl.aliyuncs.com`。显式提供空间 ID 时才使用该地域业务空间域名；Omni 分支仍要求空间地址。Key 必须与地域一致。公共域名沿用官方保留地址，未改变 Audio 的 smart_turn 或 PCM 协议。参见 [Audio WebSocket 地址与鉴权](https://help.aliyun.com/zh/model-studio/fun-audiochat-realtime-websocket-api)。

推荐依据是用户确定的3.0方向与本轮纯语音范围，不是性能排名。没有同条件端到端延迟实测，也没有可用于本账号、地域与计费方式的预算核验，因此不能声称 Audio 比 Omni 更快、更便宜或效果更好。正式对比需相同设备、网络、音频与轮次条件，测首音频时间、打断尾音、错误率和实际用量；须先取得测试预算授权。

## 本应用内部协议

浏览器连接同源 `/ws`；这只是本项目内部传输，不冒充供应商API。

- 浏览器二进制：PCM16 LE 单声道16k，每包3200字节；网关转为官方 `input_audio_buffer.append` 的base64字段。
- 浏览器控制：只接收 `{"type":"interrupt"}` 和 `{"type":"stop"}`，不能提交系统提示词、工具、上游URL或供应商会话配置。
- 网关先等待 `session.created`，发送对应系列 `session.update`，收到 `session.updated` 后才发内部 `ready`。
- 上游 `response.audio.delta` 解码后二进制回传；内部JSON事件包括 `ready`、`responding`、`listening`、`interrupted`、`transcript`、`turn_done`、`notice`、`error`。
- 停止采用官方允许的断开WebSocket方式；取消后残余音频抑制到新 `response.created`，无活动响应时不盲发cancel。
- 不发送未经该官方文档确认的 `conversation.item.truncate`；不保证模型上下文与客户端实际听到的每毫秒音频完全一致。新会话不恢复旧上下文。

## 独立接口

`gateway/ports.py` 定义 MemoryHub / ToolExecutor 类型契约。没有运行时实现、数据库、同步、离线任务、车控、MCP或工具声明。未来接入前须明确用户身份、跨端授权、事件幂等与错误语义；本轮不调用这些接口，也不把模型会话上下文包装为统一 Memory Hub。
