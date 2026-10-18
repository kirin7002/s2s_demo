# 个人助理与 StackChan 接入约定

产品身份是个人助理 Agent，手机负责收音和播放；StackChan 是可接入的动作、表情、灯光和显示工具。
首版网关与函数调用已实现并通过本机模拟联调，默认关闭；这不代表真实设备已经连接。持久记忆仍未接入。

协议源为 StackChan Wall-E 仓库的 `docs/control-protocol.md`，能力源为 `protocol/tools.json`，
设备命令为 `protocol/command.schema.json`；Qwen声明由同一能力源转换，不另行定义一套动作语义。
本机仓库为 `/Users/kirin/Documents/ChatGPT/stackchan-wall-e`，当前工具目录0.3.0。
对方契约检查数量不是S2S端测试结果；自由目标真实模型/真机组合结果见文末。

- 设备以STA Wi-Fi主动建立到现有网关的WSS连接。设备路由放在现有Python进程，首版原生Function Calling，MCP后续复用目录。
- 正常语音注册 `stackchan_get_state`、`stackchan_perform_targets`、`stackchan_get_command`、`stackchan_stop`。
  完整目录另保留`stackchan_perform`兼容路由。自由目标工具只接收steps、pace及return_to_neutral，
  不让模型填写时间轴；代码计算预算、共线中间目标与显式0/0回正，设备继续做20ms插值。
  设备由授权语音会话绑定，模型参数不接受设备ID或凭据。
- wire最多16帧、每段move最多4000ms、总时长100–8000ms，均包含边界；超限整体拒绝，不裁剪或拆成多个命令。
  低层`perform`缺省auto仅延长不足预算、顺延后帧/总时长，保留目标/输出/非负停顿；strict原样严格校验。
  编译后仍验证原wire schema和实际完整UTF-8字节数，记录requested/effective安全摘要，终态关联effective命令。
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

临时TLS端点为`wss://kirindeMacBook-Air.local:8443/device/v1/ws`，监听`0.0.0.0:8443`。
主机与设备均已验证Bonjour解析，设备以该DNS名进行TLS主机名校验；最近核验IP为192.168.31.102，
Wi-Fi切换可能改变IP。未安装系统级信任或修改云资源，登录与设备Bearer鉴权保留。
临时CA与服务器证书有效期3天，SAN包含Bonjour名、准备时的LAN IP、127.0.0.1和localhost。
固件须信任配置中的CA，不使用跳过校验。只换同Mac的LAN地址时保留CA和设备凭据；
`migrate-address`会备份配置并只更新叶证书及地址，必须在服务停止且协调允许后使用，不自动重启。
证书过期需另行协调更新。Bonjour仅适合同一LAN，跨网络仍需稳定云域名。

```sh
# 仅首次准备：私密材料不会输出到终端。
.venv/bin/python scripts/stackchan_hardware.py init --host kirindeMacBook-Air.local --port 8443
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
随后关闭会话，不调用perform；此预检本身不生成设备命令。
`model_session_count`是累计模拟会话数，包含已经关闭的预检，并不表示活动控制。
USB只读舵机探测期间只使用`status`，等待设备侧报告结束；不要运行preflight或tool操作。

私密Unix socket提供测试RPC，不在LAN增设注入API。`request --request-file <私密JSON路径>`可调用
`tool`（`calls:[{name,arguments}]`）、`close_voice`、`interrupt`、`speech_interrupt`或`disconnect`。
头部禁动时允许查询、纯屏幕/灯光perform和stop，不要求servo-ready；带任意head字段的命令
在创建会话和下发前拒绝。后续只有收到舵机就绪报告后才能提供同目录`servo_ready:true`记录并调用
`servo_ready`，且实际设备必须声明`simulated=false`、head已启用且已标定；测试角度另限±5°。
门槛只表示测试入口授权状态，不等于有活动控制会话。当前已完成授权运动测试并暂停，详见下文。

已通过LAN/回环地址的CA与主机名验证、未信任CA拒绝、私密文件权限、登录/绑定/四工具注册、
含head调用拒绝自检。隔离TLS与失败保护回归共9项，覆盖CA/凭据迁移、脱敏诊断、禁动边界及动作失败后停止；
其中设备为合成fixture，明确返回`simulated=true`，不是K151的硬件验收结果。
之后已收到设备侧明确开始通知，实际运行`verify_stackchan_hardware.py --run`，20项禁动真机检查全部通过，
包含真实能力、7表情、中文、RGB四色、带停顿组合序列、provider call_id去重、BUSY与stop、
手动/模拟VAD打断、失联拒绝不排队、重连旧历史unknown及重连后执行。全部含设备反馈的证据为simulated=false。
该禁动套件没有head帧，没有启用servo_ready；原始设备帧重放/过期拒绝不在本次硬件结果覆盖范围内。
最终恢复中性表情和熄灯取得fresh completed。首次关闭后立即检查得到control_released=false，
后续独立只读查询实际voice_session_active=false、control_session_active=false。
原报告不变；独立`.run/stackchan-hardware/hardware-no-head-final-verification.json`保留原哈希、首次快照及后续状态。
脚本已修正为等待异步清理完成后再记录释放结果。

### 授权小角度运动结果与暂停

设备侧修复供电后明确确认真实head已启用/标定，限位±5°、最高10°/s、容差1.5°。
`verify_stackchan_motion.py`只在显式`--run`且私密servo-ready报告核验通过后执行±3°，
单轴move至少1200ms、duration1500ms；软件pitch零位对应真实BSP30°。默认只展示计划。
现有报告禁止覆盖；`--report-name`可指定经另行授权的复测文件，不构成自动重跑许可。
任意failed或反馈异常立即stop并中止后续head，失败后只恢复中性表情/熄灯并释放控制，不回中。

首次`hardware-motion-results.json`第二个yaw步骤收到真实`MOTION_TIMEOUT`，原始失败记录及只读终态保留。
设备侧修复双层插值后，第二轮使用相同阈值与序列，`hardware-motion-direct-results.json`记录六个单轴步骤和摇头通过；
点头第三hold实测pitch=-1.9°超过1.5°，触发本地`FEEDBACK_OUT_OF_TOLERANCE`，并非已记录的固件failed。
已stop，中性/熄灯fresh completed，语音与控制会话释放；后续运动中打断及最终回零尚未验证。
第三hold计划在本地ACK返回后4800ms读取，第三帧计划3400+1200ms结束；
采样晚于该帧计划move窗口，但早于整条5000ms结束。原报告缺少ACK时间锚点与设备时钟，
也未记录停止前瞬时命令状态；不能补造completed或把最后hold称作停止后终态。
独立`hardware-motion-direct-timing-analysis.json`保留原报告哈希和证据缺口。
后续网络恢复补查时设备又离线，旧nod命令返回unknown/fresh=false；记录于
`hardware-motion-direct-post-stop-readonly.json`，不能把unknown当作取消或完成。

第三轮经设备侧明确通知开始，固件增加move预算内稳定期并修复USB诊断阻塞；
测试脚本只提高RGB值至同色相255级，brightness仍0.08，其余运动参数与序列未变。
`hardware-motion-settling-results.json`首个yaw+3/pitch0命令即收到真实failed/MOTION_TIMEOUT，
partial_applied=true；立即stop，后续运动全部中止，没有回中。中性/熄灯fresh completed，控制释放。
独立`hardware-motion-settling-final-readonly.json`记录停止后的fresh真实pose：yaw约1.8°、pitch约-1.6°，
active_command_id为空；不能将这个稍后姿态当作故障时瞬时值。前两轮报告哈希不变。
当前等待进一步分析，不自动重跑，不改变阈值。
随后经明确通知，在舍入修复固件上执行第四轮，测试参数与配色均未变。
`hardware-motion-rounding-results.json`首个yaw+3/pitch0再次收到真实failed/MOTION_TIMEOUT，
立即stop，未执行后续运动或回中；中性/熄灯completed，控制释放。
独立`hardware-motion-rounding-final-readonly.json`记录终态yaw约1.9°/pitch约-1.6°，
online/fresh=true、simulated=false且无活动命令；起始和途中样本也是该姿态。
前三轮原报告哈希不变，仍不据停止后样本推定故障时瞬时姿态，不自动重跑。

HeadFollower修复经设备侧明确通知后，第五轮相同参数10个阶段全部通过，原始记录
`hardware-motion-follower-results.json`，包括六个单轴步骤、停顿摇头/点头、运动中打断及软件回零。
本轮补充打断真实性门槛：下发前baseline，600ms采样，最迟1200ms观察到至少0.25°变化后才打断；
若未观察到则记MOTION_IN_PROGRESS_UNVERIFIED并停止，不把命令接纳视为运动证据。
实际约700ms观察yaw变化1.3°，随后fresh cancelled，3000ms后续帧未执行且姿态稳定。
回软件零位取得fresh completed，真实终态yaw0°/pitch约-0.9°，满足1.5°容差；
中性/熄灯completed、控制释放。独立`hardware-motion-follower-final-readonly.json`确认无活动命令，
前四轮原报告哈希保持不变。仅模拟Qwen+真实设备WSS，不代表真实语音/模型组合验收。
本机网关保持监听，没有真实麦克风、付费模型或摄像头视觉验收。

### 日志

本机联测脱敏日志`.run/stackchan-hardware/gateway-events.jsonl`持续追加启动、TLS固定错误分类、
设备HTTP状态与上下线事件，1MB轮换；不记录Authorization、凭据、请求正文或原始异常。
`gateway-log-view.txt`是人可读快照，不代表实时状态。握手结束后transport已释放时也只写固定分类。
阿里云已部署网关是另一个服务：当前SSH可连接，但Docker日志读取权限不足，尚未取得生产日志。
用户可在自己的终端执行以下只读命令并自行输入sudo口令：

```sh
ssh -t -F /Users/kirin/go_path/jev_demo/.run/server-management/ssh_config aliyun-kirin \
  'sudo docker logs --timestamps --tail 100 -f s2s-demo'
```
