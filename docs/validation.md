# 验证记录

## 已确认的当前连接阻塞（2026-10-18）

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
