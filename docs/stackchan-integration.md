# 个人助理与 StackChan 接入约定

产品身份是个人助理 Agent，手机负责收音和播放；StackChan 是可接入的动作、表情、灯光和显示工具。
首版网关与函数调用已实现并通过本机模拟联调，默认关闭；这不代表真实设备已经连接。持久记忆仍未接入。

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

## 首版本地实现（2026-10-18）

现有Python进程内已实现`gateway/stackchan.py`（设备路由、ACK、控制代次与租约）和
`gateway/tool_calls.py`（函数调用协调）。契约快照位于`gateway/stackchan_contract/`，
与上述协议源逐项一致；目录更新时须同时检查schema和Qwen声明，不能独立修改动作含义。
Memory Hub和MCP仍未实现。本轮不部署、不更改云资源、不连接真实模型。

### 最小绑定与配置

服务端由用户在本地安全配置以下三项，示例文件只提供空秘密字段；不要把真实值发送聊天：

- `S2S_STACKCHAN_ENABLED=1`。
- `S2S_STACKCHAN_DEVICE_ID`：唯一演示设备的固定ID，1–64位字母/数字/下划线/短横线。
- `S2S_STACKCHAN_DEVICE_TOKEN`：该设备独立随机Bearer凭据，32–256位相同安全字符集；与模型API Key、登录口令分开。

设备使用`Authorization: Bearer <设备凭据>`主动连接`/device/v1/ws`；不接受URL令牌或浏览器Origin。
公网必须使用可信WSS，不能把网关绑定到公网来省略TLS。localhost测试可用WS。
本轮没有把现有公网代理配置扩展为新版本，也没有同步任何凭据。

浏览器先使用已有演示登录，再通过`GET /api/stackchan`查看配置设备，
`POST /api/stackchan/bind`提交`{"device_id":"配置中的唯一ID"}`主动关联；`null`解除关联。
POST要求精确同源Origin。界面不提供任意设备输入，模型工具参数也没有设备ID或凭据。
关联后下次接通才注册四工具；未关联或非Audio分支不注册。设备离线时查询可说明离线，动作直接拒绝，不排队。
通话期间界面禁止切换关联；服务端解除关联/退出登录仍可立即撤销权限并停止控制。

这是**单设备、共享演示登录**：任何获准使用演示口令的人都能主动关联该配置设备。
不提供多用户设备所有权体系。动作控制由一个语音会话独占，其他会话收到`DEVICE_BUSY`；不抢占。
绑定、命令缓存和去重只在进程内，重启需重新登录/关联，不重放历史命令。

### 回执、取消与模拟语义

- 所有工具结果带`simulated`：已知模拟器为`true`，固件明确声明真实硬件时为`false`，离线或旧固件未声明时为`null`。
  命令查询保留原命令标记。提示词要求模拟结果明确说“模拟执行”，标记未知时也不能宣称真实机器人已动。
- `accepted`必须来自设备，完成状态另查询；ACK超时返回`ACK_TIMEOUT/status=unknown`，保留command_id，不重试。
  错误白名单包含`SCHEDULER_OVERRUN`（固件错过帧超过50ms后取消剩余帧）、`LEASE_EXPIRED`、`LOCAL_STOP`。
  不把设备原始错误文字返回模型或日志。
- 用户语音/手动打断提升`response_epoch`，取消旧调用任务，同时同步提升设备generation并发送控制栅栏。
  `stackchan_stop`只提升设备generation，不作废调用自身或当前响应；后续perform须等待`control.synced`。
- 挂断/退出登录释放控制，确认停止后才让出设备；停止未确认则关闭设备连接，交由固件断线本地停。
  控制keepalive每10秒，设备本地租约30秒；WebSocket心跳10秒、无通信30秒关闭。断线不自动回中或恢复旧动作。
- 协调器最多保留每语音会话64个provider call_id、128个response；超限以固定协议错误结束会话，要求重新接通。
  相同call_id相同调用只结算一次，冲突拒绝；动作不根据转写文字匹配。
- 完整命令envelope按真实UTF-8序列化检查8192字节，超限整体拒绝，不截断拆分。
  首帧实际姿态速度、安全限位、亮度上限及硬件故障仍由设备核心最终校验。

### 可重复的无费用验证

S2S独立故障注入与浏览器回归：

```sh
cd /Users/kirin/go_path/s2s_demo
.venv/bin/python -m unittest tests.test_stackchan -v
.venv/bin/python -m unittest tests.test_browser -v
```

使用另一仓库已经编译的**相同C++固件控制核心**端到端检查：

```sh
.venv/bin/python /Users/kirin/Documents/ChatGPT/stackchan-wall-e/scripts/verify_s2s.py \
  --s2s-project /Users/kirin/go_path/s2s_demo
```

该入口内部创建临时本机网关、模拟Qwen和合成设备凭据，不读取项目.env或真实API Key；
退出时关闭全部本轮连接。模拟器二进制需先由StackChan仓库构建`firmware/.pio/build/simulator/program`，
不会使用native测试程序代替。最终11项检查已通过：登录绑定、工具注册、能力查询、accepted、
执行中音频转发、stop→perform与旧命令cancelled、fresh完成查询、断线拒绝、重连旧历史unknown，
以及模拟标记贯穿工具结果、真实单调时钟下多帧点头完成。
S2S自己的故障fixture只注入传输故障，不重复实现固件调度。
真实手机、物理机器人、可信公网WSS和真实模型的组合仍未验证，不能用这些本机结果代替。

## 本机 TLS 真机联测准备（2026-10-18）

`scripts/stackchan_hardware.py`为独立的本地测试入口，使用原有`create_app`、登录/绑定、
DeviceHub和ToolCalls；模型连接器固定为进程内模拟Qwen，不读取项目.env、不连接模型供应商、
不操作串口。常规网关的监听范围和默认配置不变。此入口只用于明确授权的局域网真机测试。

当前en0为`192.168.31.111`，临时TLS端点为`wss://192.168.31.111:8443/device/v1/ws`；
旧`192.168.0.117`不再适用。只监听此LAN地址与127.0.0.1，未安装系统级信任或修改云资源。
临时CA与服务器证书有效期3天，SAN包含当前LAN IP、127.0.0.1和localhost。
固件须信任配置中的CA，不使用跳过校验。地址变化或证书过期后应建立新私密目录重新准备，
不要覆盖正在联调的配置。

```sh
# 仅首次准备：私密材料不会输出到终端。
.venv/bin/python scripts/stackchan_hardware.py init --host 192.168.31.111 --port 8443
.venv/bin/python scripts/stackchan_hardware.py serve
# 只查看状态，不开启语音或控制会话。
.venv/bin/python scripts/stackchan_hardware.py status
```

运行目录`.run/stackchan-hardware/`已被Git忽略，目录0700、文件及本地控制socket为0600：
`device-config.json`供固件侧安全读取host/port/path、设备随机凭据和CA PEM；
`server-config.json`含独立测试登录口令和服务端证书路径；`state.json`是脱敏运行状态；
`preparation-checks.json`记录启动自检。不要把这些文件内容贴到聊天或提交仓库。
Wi-Fi NVS、舵机零位/符号/使能和固件PSRAM配置均由设备侧负责，本脚本不改动。

启动仅监听，不自动创建语音会话、绑定控制或下发命令。`preflight`会临时走真实登录、关联和工具注册，
随后关闭会话，不调用perform；当前该预检已结束，设备命令数为0。
`model_session_count`是累计模拟会话数，包含已经关闭的预检，并不表示活动控制。
USB只读舵机探测期间只使用`status`，等待设备侧报告结束；不要运行preflight或tool操作。

私密Unix socket提供测试RPC，不在LAN增设注入API。`request --request-file <私密JSON路径>`可调用
`tool`（`calls:[{name,arguments}]`）、`close_voice`、`interrupt`、`speech_interrupt`或`disconnect`。
头部禁动时允许查询、纯屏幕/灯光perform和stop，不要求servo-ready；带任意head字段的命令
在创建会话和下发前拒绝。后续只有收到舵机就绪报告后才能提供同目录`servo_ready:true`记录并调用
`servo_ready`，且实际设备必须声明`simulated=false`、head已启用且已标定；测试角度另限±5°。
当前门槛仍关闭，没有创建就绪记录或启用舵机。

已通过LAN/回环地址的CA与主机名验证、未信任CA拒绝、私密文件权限、登录/绑定/四工具注册、
含head调用拒绝自检。另4项隔离TLS回归验证上述边界，以及禁动时屏幕/灯光/stop可经过真实工具链；
其中设备为合成fixture，明确返回`simulated=true`，不是K151的硬件验收结果。
真机连接后的读状态、屏幕灯光和路由测试待USB探测结束；舵机动作继续等待设备侧确认。
