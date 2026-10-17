# 个人助理与 StackChan 接入约定

产品身份是个人助理 Agent，手机负责收音和播放；StackChan 是可接入的动作、表情、灯光和显示工具。
本文件记录接入约定，不代表工具已实现或设备已连接。持久记忆仍未接入。

协议源为 StackChan Wall-E 仓库的 `docs/control-protocol.md`，能力源为 `protocol/tools.json`，
设备命令为 `protocol/command.schema.json`；Qwen声明由同一能力源转换，不另行定义一套动作语义。
本机仓库为 `/Users/kirin/Documents/ChatGPT/stackchan-wall-e`。对方报告24项契约检查通过，
尚无真机或真实模型联调；此数量不是S2S端测试结果。

- 设备以STA Wi-Fi主动建立到现有网关的WSS连接。设备路由放在现有Python进程，首版原生Function Calling，MCP后续复用目录。
- 四个工具为 `stackchan_get_state`、`stackchan_perform`、`stackchan_get_command`、`stackchan_stop`。
  设备由授权语音会话绑定，模型参数不接受设备ID或凭据。
- `perform`最多16帧、总时长100–8000ms，均包含边界；超限整体拒绝，不截短拆分。
  Agent自行选择姿态/灯光/表情/文字序列，不定义话术映射，不强制预置gesture。
  摇头示例为`protocol/examples/shake-with-pause.json`（左转1秒、保持0.5秒、右转1秒、回中1秒），
  点头示例为`protocol/examples/nod.json`。示例不等于可直接驱动未标定硬件。
- 表情目录新增`laughing`（大笑）、`scared`（害怕）；`happy`表示微笑。设备只公布已实现子集。
- `accepted`须来自设备确认，之后回报`running/completed/failed/cancelled`；ACK超时为未知，不自动重试或重放。
- 每连接有`connection_id`，控制有`control_session_id`与递增`generation`。停止/打断后等待`control.synced`，再下新动作；断线本地停。控制keepalive/租约覆盖手机离线。
- 设备`generation`与模型`response_epoch`分离。模型调用stop不取消自身调用或作废当前响应；用户打断使旧响应失效。
  同响应工具按`output_index`串行，stop后的perform等sync确认后使用新generation；不阻塞音频接收。
- 按provider `call_id`去重，每个调用单独结算；全部工具结果已回填、旧响应结束且smart_turn状态允许，才可协调一次续答。
- 对完整JSON envelope序列化后的实际UTF-8字节数校验，超过8192返回`MESSAGE_TOO_LARGE`，不截断或拆分。

接入后，提示词和界面须从当前授权绑定、在线状态及实际工具注册情况描述能力。
没有回执不能声称执行完成；不能声称未接入的外部工具或跨会话记忆已可用。
