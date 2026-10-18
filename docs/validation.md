# 验证记录

## 2026-10-18授权真实模型增补

本机明确授权后只建立3次真实Flash连接：一次配置/四工具注册检查（不发音频/生成），
两次单连接、45秒上限的授权合成中文语音测试，无自动重试。默认型号、longanqian、smart_turn、16k输入/24k输出不变。
四工具真实回显通过。只读测试自主调用get_state，回填真实online/fresh=true、simulated=false并收到138240字节PCM。
动作测试自主调用get_state→perform→get_command→get_command，真实设备fresh completed，收到2568960字节PCM；
服务端硬限制头部目标±3°，无脚本注入函数调用，两次结束均释放控制。PCM收到不等于手机扬声器已验收。
初次动作原参数只存SHA256，不补造具体轨迹；下次启用无文本摘要审计，便于检查模型实际轴范围和时间参数。
证据在私密`.run/s2s-live/`；模型Key与登录口令未输出或迁移，输入不是用户录音。

本机全量Python125项（含Chrome合成麦克风）及JS4项通过；新增2项显式回滚身份/保留检查也通过。
公网真实TLS、登录页和Chrome安全上下文/mediaDevices可用；公网设备接口仍404，需管理员更新。
手机真实麦克风、真实语音→公网→机器人、连续通话、后台/蓝牙/回声尚未参与，不能宣称全链路手机验收通过。
以下为先前阶段的历史记录。

## StackChan网关首版本机验证（2026-10-18）

新增`tests/test_stackchan.py`24项，覆盖登录/Origin/设备Bearer边界、授权后注册工具、设备能力与参数约束、
完整UTF-8 envelope 8192字节上限、真实设备ACK与fresh查询、模拟标记、错误白名单、ACK丢失不重试、
停止确认栅栏与超时恢复、控制独占/keepalive/撤权、重连新上下文不重放、终态不回退、
等待ACK时音频继续、语音打断取消、挂断/退出释放、未确认挂断断开设备、call_id去重与冲突、
output_index顺序、完整参数边界、逐项回填与一次续答。全部只使用本机fault fixture或模拟上游。

浏览器新增设备主动关联/解除、通话中禁止切换、320×568和390×844按钮可见性测试。
最终`.venv/bin/python -m unittest discover -v`完整100项通过，包含15项Mac Chrome浏览器测试和24项StackChan专项测试。
已查看390×844模拟设备在线关联截图，小屏布局压缩身份区以保留圆形通话按钮；未改变音频链路。
六个Audio/Omni配置对比确认，除提示词及Audio显式注册的工具外，型号、音色、轮次和音频字段保持一致。
JS采集算法4项、三个JS语法检查、依赖检查和git diff空白检查通过。

另实际运行StackChan仓库`python scripts/verify_s2s.py --s2s-project /Users/kirin/go_path/s2s_demo`，
用其`firmware/.pio/build/simulator/program`编译后C++控制核心最终通过11项端到端检查：
登录绑定、工具注册、能力发现、accepted、动作期间音频转发、stop→perform与旧动作cancelled、
fresh完成查询、断线拒绝不排队、重连旧历史unknown；新增两项核验模拟标记贯穿工具结果和真实单调时钟下多帧点头完成。
模拟器明确标记simulated=true；
未使用native单元测试二进制代替控制核心模拟器，也未重复实现Python调度器。

本轮未部署、改云资源、连接真实模型或驱动物理设备。可信公网WSS、物理硬件标定/限位、
真实手机语音与模型调用的组合仍待单独授权验收；当前没有接口契约阻塞。

## 移动通话界面本地验证（2026-10-18）

对话页改为浅色通话布局：对话记录在上方独立滚动，底部中央圆形接通/挂断按钮、
两侧静音与打断；显示通话时长。静音会禁用音轨并停止音频包上传，挂断释放全部资源。
等待麦克风期间可用同一位置的取消按钮；挂断后记录保留在本页，可清空，无持久存储。
保留登录、原Flash配置、自动轮次和原有音频协议。

Mac Chrome实际运行14项浏览器测试（模拟麦克风、环回WebSocket），覆盖接通/挂断、
静音停止上传、计时、记录保留/清空、双击、权限拒绝/迟到授权、断线/后台释放、打断、登录退出。
320×568、390×844、1440×1000布局无水平溢出，主要按钮在视口内且触摸宽度至少44px。
已查看390×844及1440×1000截图。浏览器路径可由S2S_TEST_BROWSER指定，默认支持Mac Chrome。
完整Python回归72项、音频算法4项通过；后新增UI更新保护测试3项通过。
本地预览127.0.0.1:8081使用显式禁用模型、无Key配置，不读取.env；未做真实音频/模型调用。
远端页面尚未更新，部署完成后须独立核验新版公开资源与登录边界。

## 阿里云HTTPS入口验证通过（2026-10-18）

用户授权执行受保护的代理修复。服务器Caddy v2.11.7诊断确认：宿主与容器挂载文件内容不同；
移除S2S块后的配置适配成功，完整配置在第31行拒绝ACME issuer中的`ca`子指令。
已按[官方ACME issuer语法](https://caddyserver.com/docs/caddyfile/directives/tls#acme)改为`dir`，
验证其他站点配置与运行配置完全一致后，备份、同步两个文件并重载，运行JSON与候选一致。
本地17项诊断/修复测试及激活脚本bash语法检查通过；覆盖原错误字段迁移、差异拒绝、回滚和脱敏。

过滤日志确认签发成功。Mac直连47.116.102.49、SNI为s2s.kirinxu.cn，使用系统CA并校验证书主机名：
TLS可信，证书到期2027-01-15 16:26:37 UTC；/login及login.js/style.css/app.js/capture.js均200，
/api/config的ready/password_required均true、型号为Flash；未登录/chat返回302至/login，
带标准Upgrade及正确Origin的未登录/ws返回401。kirinxu.cn主站TLS也通过。
HTTPS登录响应不含此前HTTP80观察到的ICP拦截页面；不能把旧HTTP观察当作当前HTTPS阻塞。
没有提交演示口令或建立已认证WSS，没有触发云端模型、音频或生成。
真实登录后的模型握手、手机麦克风及完整语音仍未在云部署验收。

## 最新 Key 的配置握手已通过（2026-10-18）

用户重新授权并更换 Key 后，重启加载准确项目 `.env`，保持 Flash / 北京公共端点 / live模式。
本机登录页HTTP200；一次严格超时、无音频诊断取得HTTP101、`session.created`，发送应用当前完整
`session.update` 后收到`session.updated`，随即关闭。没有发送音频、`response.create`、自动重试，
没有改变计费设置。这验证初始鉴权和会话配置确认，完整语音输入/回复仍待用户验收。
当前PID67839，仅监听127.0.0.1:8080；以下配额阻塞为旧Key的历史诊断，不代表当前结果。

## 旧 Key 的连接阻塞（2026-10-18）

在授权范围内只执行一次空 `session.update.session={}` 对照，无需第二次连接：北京公共端点、
Flash、同一 Key，正常 TLS 验证；HTTP 101，收到 `session.created`。随后服务端发出
opcode=8、FIN=1、RSV=0、无 mask、声明长度 178 的关闭帧，帧内关闭码为 **1007**。
限量读取并脱敏后的关闭原因明确表示：**模型免费额度已耗尽，需关闭仅使用免费额度模式才可付费继续**。
这与[百炼官方错误说明](https://help.aliyun.com/zh/model-studio/error-code)中的对应错误一致。

因此当前业务阻塞是账户的免费额度/仅免费调用限制，不是 Key 为空或无效，也不能通过删除
`smart_turn`、音色等会话字段解决。完整音频兼容性仍未验收；账户限制解除前无需继续试字段。

协议错误的第二层原因也已确认：服务端把长业务错误放进了超限关闭帧。RFC 6455 控制帧最多
125 字节，aiohttp 正确拒绝长度 178，转为客户端解析错误 1002，所以页面此前看不到实际配额原因。
诊断器仅在有界内存中读取了这一个异常关闭帧用于查因，没有存储帧/凭据/完整请求头，也没有让应用
接受违规帧或继续会话。生产客户端仍严格校验。

本地 fixture 已对齐观察到的 1007 / 178 字节关闭帧形状，复现客户端 1002 和内容脱敏；
28 项网关测试通过。没有发送音频或 `response.create`，没有自动重试、启用付费、改套餐或模型权限。
下一步由用户在百炼确认该模型的免费额度与“免费额度用完即停/仅使用免费额度”设置；若选择付费继续，
由用户自行更改计费开关并确认预算后，再做真实语音验收。以下先前的未决假设已由本节结果收敛。

## Mac 当前增量：Flash 与故障诊断

用户明确选择 `qwen-audio-3.0-realtime-flash`，当前默认型号和本地 `.env` 普通模型字段已切换。
北京公共端点与 `longanqian` 音色不变，字段兼容性已按官方 Audio 指南与客户端事件文档核对。
前端型号显示取自 `/api/config`，不写死型号。以下旧记录中的 Plus 是当时基线，不代表当前默认。

网关现在给出固定诊断码和阶段，区分上游 HTTP 拒绝、网络错误、等待会话创建、等待配置确认和语音阶段。
日志只记录本地固定分类和数字 HTTP 状态；不记录供应商原始错误、请求头、凭据、音频或转写。
本地回归：27 项网关、13 项配置/启动、4 项音频采集测试通过；包括 HTTP 401、配置拒绝、
会话等待超时及日志/网页脱敏断言。没有为这批测试连接真实模型或发送音频。
Mac 浏览器测试仍因固定 Linux Chromium 路径未运行；网页实际报错需结合新诊断码定位。

### Flash 配置握手断开诊断（2026-10-18）

用户报告 `session_updated / PROTOCOL_ERROR`。一次受限的 Flash 配置诊断得到 HTTP 101、
`session.created`；仅发送当前 `session.update` 后，aiohttp 返回 `WSMsgType.ERROR` 和关闭码 1002。
未发送音频、`response.create` 或重试。这证实故障发生在上游帧读取阶段，尚不能证明某个配置字段或
某种压缩协商是根因。应用此前把所有非文本事件误归为同一 `PROTOCOL_ERROR`，丢失了这一层信息。

现在从 `msg.data` 识别 aiohttp 帧解析错误（此时 `ws.exception()` 可能为空），只输出固定诊断分类
与数字关闭码，区分保留位、非法关闭码、控制帧、UTF-8 与普通关闭；不输出原始异常或帧载荷。
本地异常帧 fixture 及脱敏分类测试通过。真实根因仍待新分类证据，不将可登录页面当成完整 S2S 已修复。

### 本地传输对照排查

在 aiohttp 3.14.5 上分别使用已安装的 Cython 解析器和其 Python 实现，共跑 12 个纯环回实验。
使用 `Config()` 的默认 Flash 会话配置，不读取 `.env`；原始 TCP fixture 验证客户端 mask 和
JSON 载荷相符，并构造以下返回帧。两种解析器结果一致：

| 返回帧 | Cython / Python 结果 |
| --- | --- |
| 超过 64 KiB 的普通 UTF-8 配置确认 | `TEXT / session.updated` |
| 分片配置确认，中间插入正常 ping | `TEXT / session.updated` |
| 双方协商 permessage-deflate 后压缩配置确认 | `TEXT / session.updated` |
| 未协商扩展却设置 RSV1 的压缩帧 | `ERROR / 1002 / non-zero reserved bits` |
| 关闭帧内包含非法状态 1005 | `ERROR / 1002 / Invalid close code` |
| 服务端发送合法关闭帧，状态为 1002 | `CLOSE / 1002` |

这排除了当前配置的 JSON 编码、客户端 mask、正常长帧/分片以及单独 C 扩展差异在本地对照中的故障。
真实诊断收到的是 `ERROR` 而非 `CLOSE`，结合安装版 `ClientWebSocketResponse.receive()` 源码，
可确认 1002 来自本地 `WebSocketError` 解析路径，不是直接收到服务端正常 1002 关闭帧。
但至少两种异常输入均能产生同一观测，无法只凭 1002 判定是压缩或会话字段造成。

已确认实现差异：本项目 `ws_connect()` 默认 `compress=0`；官方示例使用的 websockets
默认协商压缩（本机未安装该库，没有可比较的安装版本）。参考
[aiohttp 参数](https://docs.aiohttp.org/en/stable/client_reference.html#aiohttp.ClientSession.ws_connect)、
[websockets 压缩默认值](https://websockets.readthedocs.io/en/stable/topics/compression.html)。
官方 Audio schema 中，当前 `modalities`、`voice=longanqian`、PCM 字段和 `smart_turn` 均有定义；
这一静态核对不代表当前真实会话已接受配置。

本次未追加真实请求、未换依赖或放宽协议检查。若授权追加最小对照，只建立一次 Flash 连接、
将唯一传输变量改为 `compress=15`，记录扩展是否协商成功和安全帧分类，发送相同 `session.update`
后立即关闭；不发音频、不生成、不自动重试。当前仍缺真实失败帧的具体解析类别。

### 获授权的单变量压缩对照结果

一次 `compress=15` 对照已完成：HTTP 101、压缩协商成功、收到 `session.created`；发送相同配置后
仍返回 `WSMsgType.ERROR / WebSocketError / 1002`，具体固定分类为 `UPSTREAM_WS_CONTROL_LENGTH`。
对应安装版解析器的 `Control frame payload cannot be larger than 125 bytes` 分支，不能通过
改为协商压缩解决。未发送音频、`response.create` 或重试，也未更改应用的压缩设置。

新增合成本地超长关闭帧 fixture，可重现相同的 `session_updated / UPSTREAM_WS_CONTROL_LENGTH / 1002`，
并确认敏感帧原因不进入日志或页面。应用保持严格帧校验，不以忽略 RFC 6455 长度限制掩盖问题。
尚缺违规控制帧的 opcode、声明长度与内部原因，当前不能判断是配置字段引发服务端错误关闭，
还是其他控制帧/中间链路问题。

若继续获准最小真实对照：同一 Flash/端点/鉴权，只将 `session.update.session` 改为空对象，
并在内存中观测限长的帧头和关闭原因的安全分类；不保存或输出原始帧、凭据和请求头，不发送音频或生成。
只做一次并立即断开，借此区分空配置也失败的传输问题与当前完整配置触发的问题，不循环删字段试错。

日期：2026-10-17。运行环境：Python 3.12、aiohttp 3.14.4、系统 Chromium，390×844 移动视口与触控模式。

## 已通过

```sh
python -m unittest tests.test_gateway tests.test_browser -v
# Ran 36 tests ... OK
node --test tests/test_capture.js
# 4 passed, 0 failed
python -m compileall -q gateway tests
node --check web/app.js
node --check web/capture.js
node --check web/login.js
```

24项Python配置/网关/认证验证：Audio/Omni配置隔离、非法型号/地域/空间拒绝、同会话双轮双向PCM流、手动取消及残余抑制、Audio自动打断不重复取消、错误脱敏、配置不泄露Key/空间、Origin拒绝、未登录拒绝、退出后拒绝WebSocket、登录错误与频率限制、Cookie属性、连接数量上限、坏包清理、未启用不访问上游、握手超时、握手期间取消与资源释放，跨标签退出撤销、过期会话和错误响应防缓存、缺Key阻塞、HTTPS Secure Cookie回归，以及下述6项公共端点配置回归。

12项Chromium浏览器验证：390像素无横向溢出、合成麦克风实际AudioWorklet上传和回复PCM播放、连续轮次、反复点击不重复建连、停止释放后重连、权限拒绝、等待权限时取消与迟到流释放、离线、上游断开、音频暂停、页面后台、登录失败/成功与退出，新增旧Socket迟到回调不污染新会话、离开页面后可重连、计入新包的输出积压拦截、打断立即停止并断开旧音源。相关场景组合在12个用例中执行。

4项实际worklet算法验证：16k / 44.1k / 48k设备采样率，每秒输出10个3200字节包，双声道混合、PCM限幅与小端格式；44.1k变化信号在不规则处理块与整段处理下逐字节一致。

## 收尾审查修复

1. 退出登录原先仅撤销Cookie，其他标签页已打开的模型连接仍会继续；现按会话Cookie关联并关闭所有已打开连接，且在WebSocket握手完成后复核认证，防止退出与升级交错。
2. 页面离开原先只释放资源但未清除当前会话引用；现同步重置界面，使浏览器返回缓存页面后仍能重新开始。
3. 输出背压原先仅比较已有队列；现计入新包时长，分配/调度前拒绝超过8秒的队列。上传也计入即将发送的新包。
4. 打断原先停止音源但未立即断开其节点；现同时停止、断开并清空队列。
5. 认证错误与跳转原先绕过响应防缓存/安全头；现统一添加并按aiohttp要求重新抛出HTTP异常。

重新核对官方Audio客户端事件与WebSocket指南：3.0使用顶层PCM字段、`voice=longanqian`、`smart_turn`，未混入Omni嵌套音频字段或3.1专属语言配置。默认型号仍为`qwen-audio-3.0-realtime-plus`。这属于代码/文档核对，不能代替真实服务验收。

## 公共端点小步变更

Audio 默认北京公共地址，无需 Workspace ID。6项新增回归覆盖：准确默认URL与3.0 Plus型号、运行时缺省空间配置、新加坡公共域名与显式型号保持、可选Audio专属地址/Omni空间约束、缺Key及非法host输入拦截，以及无Workspace配置的本地握手。全部使用配置断言或本机fixture，没有连接真实公共模型；没有申请临时Token或配置真实秘密。

曾发现建连阶段取消造成上游WebSocket未释放；现将限时建连任务独立托管，在finally收回并关闭，相关回归通过。浏览器测试保留严格CSP，没有为测试启用unsafe-eval。

## 验证边界

- 上游全部为本地WebSocket协议fixture，按官方事件构造；这证明本地实现处理这些事件，不能证明供应商实际接受配置。
- 浏览器麦克风为Chromium合成设备；权限拒绝/等待及系统中断/后台状态由测试注入。没有上传真实用户音频，没有真实API Key，没有连接阿里云模型，没有付费测试。
- 未使用真机Safari/iOS/Android。首次用户手势启音、回声、音色、真实网络延迟、锁屏、来电、蓝牙设备切换和模型配额需后续授权验收。
- Dockerfile仅准备，未构建或部署；无云资源、安全组、持久凭据、远端推送操作。

## 页面截图

截图展示默认未启用模型的实际页面，不包含模拟模型回复：

- [移动登录页](screenshots/mobile-login.png)
- [移动对话页](screenshots/mobile-chat.png)

## 可执行下一步

先按README启动本地页面。用户确认预算、已有阿里云资源与地域后，通过部署环境的秘密管理注入Key和演示访问口令，配置工作空间/精确HTTPS Origin，才启用真实模型。按 [部署准备](deployment.md) 的真机清单完成模型握手、至少三轮、打断/停止与弱网验收。用户批准部署前不推送、不创建资源。
