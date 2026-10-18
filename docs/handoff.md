# s2s_demo 开发交接

更新日期：2026-10-17。仓库：`https://github.com/kirin7002/s2s_demo.git`。
本地根目录：`/Users/kirin/go_path/s2s_demo`。
用户希望后续直接在 Codex 的正式 `s2s_demo` 项目中聊天开发，无需经 Dot 中转。

## 最新状态：2026-10-18

**最新联调入口改为Mac本机，云端不再是前置：`http://localhost:8080/login`。**
`scripts/serve_s2s_tls.py`一个进程同时监听127.0.0.1:8080的电脑HTTP和0.0.0.0:8443的设备TLS，
两个Web app共享同一个DeviceHub、登录/绑定/控制状态，各自保留精确Origin与相应Cookie策略；不要另启第二个网关。
电脑浏览器已确认安全上下文、mediaDevices存在、登录/chat/退出200、异源403，不需安装全局CA。
localhost入口额外精确校验Host=localhost:8080，错误Host403；WS错误Origin403、同源未登录401，均未连接模型。
尚未请求真实电脑麦克风，也没有在本轮接通模型或发动作，等待用户真实登录/关联/接通。
实际设备已上线并公布yaw±60°、pitch±25°、max_speed30°/s、tolerance1.5°；read_only=false、test_max=None。
本机使用根目录.env的S2S_DEMO_PASSWORD，不使用旧hardware随机测试口令。
私有状态继续通过`.venv/bin/python scripts/s2s_status.py --op get_state`读取，不连接模型。
手机同LAN的HTTPS仍使用私密CA，未信任时有ERR_CERT_AUTHORITY_INVALID；当前按用户要求先电脑联调，不要求安装全局CA。
云端包与原值设备配置已准备/上传，明确目的地授权已取得，0600及字节校验通过；尚未执行管理员容器更新。

**新增用户授权：已批准少量真实模型联调与必要公网更新；不要再套用下文旧的“禁止真实模型”阶段限制。**
**用户最新改为直接最终手机联调，取消逐项/合成/电池动作补测。**电池补测在连接模型前取消，新增连接/动作均为0。
正常服务已去除临时read_only、±3°服务端上限、单会话90秒/静默30秒和额外8工具/6续答限制；
恢复应用默认2并发、600秒会话、60秒静默，保留鉴权、绑定、协议边界和停止/断线保护。
设备正常动作范围由固件公布；目前运行的仍是小角度固件，正常行程固件由设备侧负责，不由服务端伪报能力。
不要再运行自动动作或细项测试，等待手机用户真实发起。
真实付费测试仅3次连接：一次18秒以内配置/四工具注册检查，两次45秒上限的合成语音闭环，均已结束。
`.run/s2s-live/provider-registration.json`确认Flash接受当前配置并回显4工具；
`real-audio-readonly.json`确认真实模型自主调用get_state，真实设备online/fresh/simulated=false后返回语音；
`real-audio-action.json`确认真实模型get_state→perform→两次get_command，最终fresh completed及PCM回传。
这些输入是Mac离线合成语音，不是用户手机麦克风；未注入函数调用，真实输入转写已匹配测试句。
早期perform参数只保留哈希，不补造轨迹；后续新增无文本动作摘要（帧数、头部轴范围、move_ms/duration、表情枚举）与终态审计。
保持个人助理定位，电源读数是带sample_age_ms的缓存，单次百分比不等于精确容量，也不能据旧值判断USB已拔除。

当前本机监听入口由`scripts/serve_s2s_tls.py`运行，仍使用原设备TLS地址/CA/凭据，
但模型与登录凭据来自根目录`.env`。使用正常应用配置，启动本身不连模型或发动作。
旧模拟进程已停，回退可在协调确认空闲后停止真实服务并运行`scripts/stackchan_hardware.py serve`，不能同时启动。
新只读Unix诊断通道`.run/s2s-live/control.sock`支持status和get_state；快捷命令
`.venv/bin/python scripts/s2s_status.py --op get_state`直接读设备，不连接模型、不占用控制。
180秒只读电池/重启监测由设备侧完成，确认VBUS下降到0与重启后自动WSS重连，不是模型动作测试。
活动设备连接配置为`.run/s2s-live/device-config.json`，公网候选独立配置为`.run/s2s-cloud/device-config.json`，后者尚未激活。

公网`s2s.kirinxu.cn`TLS/登录可用，Chrome实测安全上下文和mediaDevices存在，但无手机授权/录音验证。
仍运行旧UI/车控定位，`/api/stackchan`404，公开tools=disabled。SSH账号无Docker权限、sudo -n不可用；
不使用历史聊天密码、不修改无关服务/防火墙、不复制本机模型密钥或登录口令上云。
`deploy/update_gateway.py`准备仅更新现有S2S容器，沿用云端原登录/模型/反代网络，离线wheel构建，
加入设备鉴权和无文本动作审计，移除临时角度限制；失败自动恢复旧容器，支持显式手动回滚。
云端配置token/CA必须与设备侧已读的`.run/s2s-cloud/device-config.json`保持一致，不生成替代身份。
自动审批最初因特定目的地授权不足拒绝上传设备Bearer；用户随后明确批准47.116.102.49的
`/home/kirin/apps/s2s_demo/shared/stackchan-device-config.json`，已上传且0600/原字节哈希验证一致。
最终代码release为`/home/kirin/apps/s2s_demo/releases/gateway-128386686b86`，远端包SHA、清单、
Linux依赖和正常会话配置验证通过；精确sudo更新命令在`.run/s2s-cloud/deploy-command.txt`，手动回滚命令在`rollback-command.txt`。
当前Docker和sudo权限仍阻断助手切换容器，需要用户亲自在终端执行；不使用历史聊天密码。
旧fc759e8准备包已被最新无临时限制版本替代，不要激活。手机URL为https://s2s.kirinxu.cn/login，
先完成管理员更新与设备侧最终固件切换，再使用原云端演示口令关联设备并真实通话。
管理员发布包/命令的实际路径见`.run/s2s-cloud/`及部署文档；必须完成管理员更新后再将固件改为公网配置。
全量本机125项（含Chrome）与JS4项通过，后续新增2项显式回滚保护也通过；未执行线上容器切换或Docker构建。

以下是本次真实模型阶段之前的历史记录：

入口区分：常规S2S通过根目录`.env`配置，演示口令为`S2S_DEMO_PASSWORD`；
`.run/stackchan-hardware/`属于本仓库内的隔离机器人联测材料，8443为模拟模型测试入口，不能当作真实语音主入口。
用户浏览器登录时发现Bonjour配置含大写、浏览器Origin转小写引发403；现已在联测入口统一Origin小写，
保留精确同源校验。登录页支持纯文本HTTP错误，避免显示JSON解析底层错误。
11项隔离/浏览器检查通过；空闲时重启联测服务后，登录、chat、退出均HTTP200。
这次重启使运动门槛恢复默认关闭，旧内存命令历史不再保留；原始报告和凭据/CA未变。
没有打开语音控制、发送设备命令、调用真实模型或修改正式`.env`。常规8080服务尚未启动。

本机K151 TLS联测网关已启动，设备端点改为
`wss://kirindeMacBook-Air.local:8443/device/v1/ws`，监听`0.0.0.0:8443`。
同一CA、设备令牌和登录凭据保留，叶证书含Bonjour DNS SAN，真实设备已通过主机名校验上线。
最近核验LAN IP为`192.168.31.102`；用户正在切换Wi-Fi频段，不能把历史IP当作当前地址。服务入口
`scripts/stackchan_hardware.py`使用真实网关/设备/工具代码和进程内模拟Qwen，不读取.env或访问付费模型。
私密配置、临时CA/服务器私钥及状态仅在已忽略的`.run/stackchan-hardware/`，目录0700、文件0600；
固件侧已安全读取`device-config.json`，不要再输出或传输其中凭据。运行状态查看该目录`state.json`或脚本status。

当前阶段：**HeadFollower修复固件的第五轮10个运动阶段全部通过，最终清理完成，网关保持监听，不再自动动作。**
`hardware-motion-follower-results.json`记录六个单轴步骤、停顿摇头/点头、运动中打断及软件回零。
限位±5°、目标±3°、最高10°/s、容差1.5°及既有时间参数不变；没有操作USB/EEPROM或真实模型。
新增打断真实性检查：先读baseline，600ms开始采样，最迟1200ms须观察到至少0.25°变化，否则未验证并停止。
本轮实测回执返回后约700ms时yaw从-1.3°变为0°，观察到约1.3°运动后打断；
命令fresh cancelled，经过3000ms后续帧时点仍稳定，未把accepted当成正在运动。
软件回零fresh completed，最终真实pose为yaw0°/pitch约-0.9°，处于1.5°容差内，不声称机械绝对零误差。
中性/熄灯fresh completed，语音/控制释放；独立`hardware-motion-follower-final-readonly.json`确认
online/fresh=true、simulated=false且无活动命令，前四轮原报告哈希未变。
新增真假运动打断保护的4项纯模拟回归通过。本轮不代表真实模型/手机语音的组合验收。

以下四轮失败记录保留供追溯：
第四轮经设备侧明确通知，在舍入修复固件boot=`esp32_9cdc77a2_b6f7843c`上通过WSS执行相同测试。
`hardware-motion-rounding-results.json`记录首个yaw+3/pitch0再次真实failed/MOTION_TIMEOUT，
partial_applied=true，立即stop并中止全部后续head，没有调整参数或操作USB。
起始、途中及独立停止后只读姿态均约yaw1.9°/pitch-1.6°，终态online/fresh=true、simulated=false，无活动命令。
中性/熄灯fresh completed，语音与控制均释放；独立证据`hardware-motion-rounding-final-readonly.json`。
前三轮原报告哈希全部保持不变；停止后姿态不代表故障瞬时值。
第三轮已在设备侧修复USB诊断阻塞、增加预算内稳定期后按明确通知执行，
结果另存`hardware-motion-settling-results.json`。运动参数未变，仅RGB提高至同色相255级，brightness仍0.08。
首个yaw+3/pitch0步骤即收到真实`failed/MOTION_TIMEOUT/partial_applied=true`，后续步骤全部中止。
已stop，中性/熄灯fresh completed，释放控制；没有失败后回中。
独立`hardware-motion-settling-final-readonly.json`补读online/fresh=true、simulated=false，
最终yaw约1.8°、pitch约-1.6°，active_command_id为空；这是停止后终态，不是故障瞬时反馈。
前两轮原报告哈希核对未变。用户已切换5GHz，Bonjour自动重连，无需更换设备配置。
首次原报告`hardware-motion-results.json`保留`MOTION_TIMEOUT`失败及只读终态，哈希已独立保存。
固件侧修复双层轨迹后，第二轮`hardware-motion-direct-results.json`中六个单轴步骤和摇头通过；
点头第三hold实测pitch=-1.9°，超过1.5°容差，本地`FEEDBACK_OUT_OF_TOLERANCE`保护触发，
已stop、中性表情/熄灯completed、释放控制，后续运动打断与回软件零位未执行。
第二轮原报告保持不变；独立`hardware-motion-direct-timing-analysis.json`记录源码时序、采样和证据缺口。
该hold按ACK返回后4800ms采样，晚于第三帧3400+1200ms计划结束，但早于整条5000ms结束；
没有保存设备时钟或本地ACK时间锚点，不能重建设备端精确时间，也没有采到停止前瞬时命令状态。
第二轮网络恢复后的补查曾遇设备离线，旧nod返回unknown/fresh=false，未取得其停止后姿态；
独立`hardware-motion-direct-post-stop-readonly.json`保留证据，新连接/会话不能据此确认旧历史。
运动门槛此前经明确授权已开启，不能写成门槛关闭；最后测试报告voice/control均已释放。
测试不操作USB、固件或付费模型；不调整±3°、1200ms、10°/s、1.5°等既定条件。

此前已完成禁动真机20项检查，20通过、0失败。
真实WSS已确认HTTP101、peer=192.168.31.112、simulated=false；设备修正了认证头重复CRLF。
检查覆盖7表情、中文、RGB、停顿、stop、provider call_id去重、BUSY、手动/模拟VAD打断及断线重连。
测试使用进程内模拟Qwen，回执来自真实设备；未用摄像头验收视觉效果，也未声称覆盖原始设备帧重放。
结束中性表情/熄灯已有fresh completed回执，后续只读状态确认voice/control均无活动会话。
原`.run/stackchan-hardware/hardware-no-head-results.json`保留首次关闭后的`control_released=false`快照不变；
独立`hardware-no-head-final-verification.json`记录后续实际状态、复核时间和原报告哈希。首次观察先于异步清理完成，
不能把原false改写为一次测试通过。测试脚本已增加等待实际清理结束的逻辑。
启动时所有head命令默认禁用；运动测试前已取得设备侧就绪确认及真实已启用/标定能力，
本轮测试只用±3°，设备限位±5°。
已通过LAN/回环TLS及CA/主机名、私密权限、禁动门槛自检，另28项隔离TLS/StackChan回归通过。
没有操作USB、改变Wi-Fi NVS或部署线上；舵机供电与轨迹修复由设备侧负责，运动问题仍待分析。
隔离TLS及失败保护回归9项通过。持久脱敏日志为`.run/stackchan-hardware/gateway-events.jsonl`，
人可读快照为`gateway-log-view.txt`。阿里云生产Docker日志读取因权限不足未成功，未使用聊天口令；
快照内保留用户自行输入sudo的只读日志命令。本机模拟Qwen日志不能冒充生产日志。
启动及控制入口详见[接入文档](stackchan-integration.md)。以下是此前首版集成和部署的历史状态。

产品定位已调整为个人助理 Agent，定位提交`7e558d4`保存在本地。随后按用户批准完成StackChan网关首版：
`gateway/stackchan.py`负责设备Bearer认证、单配置设备与登录会话绑定、WebSocket路由、ACK/状态/租约；
`gateway/tool_calls.py`负责四个原生函数、完整参数、call_id去重、output_index串行、独立response_epoch与设备generation，
stop/perform栅栏、用户打断及挂断取消。默认关闭，须由用户安全配置设备凭据并在页面主动关联。
所有工具结果均保留simulated；模拟器结果不得声称真实机器人已动。新增错误白名单包括
SCHEDULER_OVERRUN、LEASE_EXPIRED、LOCAL_STOP。设备控制工具、能力边界和本机联调入口见
[StackChan接入约定](stackchan-integration.md)。持久记忆和MCP尚未实现。

本轮没有部署、推送、修改云资源、读取/复制真实凭据或调用真实模型；公网仍是此前用户部署的版本。
与StackChan仓库的编译后C++核心端到端11项检查通过；这是本机WS+模拟Qwen，不是物理机器人或公网WSS验收。
本机协议与故障回归新增24项，浏览器新增1项主动关联测试；完整检查记录见[验证记录](validation.md)。
最终完整Python回归100项（含15项Chrome浏览器）、JS采集4项和编译后C++模拟器端到端11项均通过。
六个模型配置除提示词和明确授权后的工具声明外保持不变，默认Flash、longanqian、smart_turn与16k/24kPCM不变。
没有启动新的常驻预览进程，旧PID和下文旧测试数量均为历史记录，操作前须重新核验。

用户要求Apple/Skype风格移动通话页，并要求发布到现有站点。UI提交`3f6501e`：
上方本次对话记录、底部圆形接通/挂断、静音/打断、通话计时，保留登录及Flash。
已看手机/桌面截图，完整Python回归72项（14项Mac Chrome浏览器）和音频4项通过；
新增更新保护测试3项通过。本地禁用模型预览`http://127.0.0.1:8081`已启动并请求在Codex打开。
用户已自行执行UI更新，报告`DEPLOYED_UI_REVISION=3f6501e9e9d9164c9f357681e55511bd35be4215`，
内部登录、Flash配置与资源检查通过；旧容器为`s2s-demo.pre-ui-3f6501e9e9d9`。
公网二次复核当时因本机执行连接中断未完成。发布包与历史命令见[部署说明](deployment.md)。
禁止再次使用聊天中曾贴出的密码，不做新模型调用或凭据更换。

用户已授权部署及管理员修复，公网入口 **https://s2s.kirinxu.cn/login 已验证可用**。
可信TLS通过，证书到期2027-01-15；Caddy日志确认`certificate obtained successfully`。
登录页、全部4个前端资源HTTP200，公开配置ready=true/password_required=true/Flash；
未登录/chat跳转/login，未登录WSS返回401。原主站TLS正常，HTTPS登录页无ICP拦截。
没有云端真实模型、音频或生成测试；手机麦克风和完整语音仍待用户验收。

根因有两项：容器单文件挂载内容落后于宿主文件；原新站片段误把ACME issuer字段写成`ca`，
正确字段是`dir`。旧挂载让最初validate/reload实际读取了旧配置，漏掉了错误。
已仅修正S2S字段、同步宿主与挂载，验证其他站点无差异后重载，并核验实际运行JSON。
Caddy版本v2.11.7；17项本地诊断/修复测试通过。备份目录
`/opt/caddy/s2s-config-backup-miiiwzul`（root0700，内文件0600）。不要重复激活/修复。
后续激活脚本已加入挂载一致性预检和实际运行配置验收，详见[部署说明](deployment.md)。

应用源码仍来自`/home/kirin/apps/s2s_demo/releases/20261018-le-only`；旧准备目录
`releases/3f670692f010`保留。实际代理修复脚本位于`staging/tls-diagnostics/`。
旧release内部署片段保留为历史，不要重复运行其中激活脚本；仓库最新片段已修正。
用户已亲自填写远端`shared/.env`；未复制本地API Key、未修改安全组/防火墙。
本次sudo授权会话已执行sudo -k并退出；不在文件或文档记录管理员密码。

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
