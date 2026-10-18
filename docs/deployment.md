# 阿里云隔离部署

## 当前StackChan网关更新准备（2026-10-18）

用户已批准真实手机/模型/设备联调及必要更新。现有公网TLS与登录可用，但仍是旧实现：
`/api/stackchan`404、tools=disabled、个人助理提示词尚未部署。不要将旧站的可访问等同于机器人已接入。
本机真实Flash已通过四工具注册、合成中文音频→模型自主工具→真实设备→回填→PCM回传；手机真实输入仍待验收。

本次只更换现有`s2s-demo`容器的代码与必要依赖，沿用原云端Key、演示口令、型号、Caddy网络与域名。
不改Caddy、不加公网端口或管理API、不改防火墙及其他应用。设备独立凭据在
`/home/kirin/apps/s2s_demo/shared/stackchan-device-config.json`（0600），不包含模型Key或登录口令。
本机固件侧读取候选配置`.run/s2s-cloud/device-config.json`；仅管理员激活后才能切换固件。
ISRG Root X1与现有站点主机名已验证；不能关闭设备TLS校验。

`deploy/update_gateway.py --approve-gateway-update`必须由管理员执行。当前SSH用户无法访问Docker socket，
`sudo -n`也不可用，因此助手无法代执行。准备完成后的**精确一条命令**位于私密目录
`.run/s2s-cloud/deploy-command.txt`；用户在自己的终端执行并亲自输入sudo密码，禁止使用聊天密码。
发布包含源码校验清单和Linux Python3.12依赖wheel，复用现有镜像、无网络构建；
先检查导入，再切换S2S容器，健康失败自动回滚，旧容器保留为`s2s-demo.pre-gateway-<revision前12位>`。
显式回退使用同一脚本`--rollback-gateway-update`，核验容器身份且保留新容器，不删除未知容器。
更新命令不主动调用模型或设备。发布后须再次确认公网绑定接口、WSS鉴权及手机安全上下文。

初期服务端额外设置`S2S_STACKCHAN_TEST_MAX_DEG=3`，保持设备±5°/10°s/1.5°；
`S2S_STACKCHAN_AUDIT=1`只记录帧数、头部轴min/max、move_ms、duration、枚举表情和完成/取消/错误结果。
不记录屏幕文本、用户语音或凭据。个人助理必须先读当前能力，回执accepted不能当作完成。

手机步骤：打开`https://s2s.kirinxu.cn/login`，使用**原云端演示口令**，关联已配置StackChan设备，再点击接通并允许麦克风。
先问“查询机器人是否在线”，再在协调确认无人控制后请求小角度动作；设备离线时不尝试补发。
实际手机输入、扬声器/回声、连续三轮、语音打断、锁屏及网络切换尚未验证，不以合成音频测试替代。

## 当前已完成：2026-10-18

**入口：https://s2s.kirinxu.cn/login**。可信TLS与登录页/全部前端资源已从公网验证；
公开配置ready=true、Flash、要求演示口令。未登录/chat跳转/login，未登录WSS返回401。
原主站TLS正常。没有云端模型握手或音频/生成验证；下一步由用户手机登录并验收语音。
不要重复运行下文历史激活/修复命令。

已修复旧单文件挂载与宿主不一致，以及原片段ACME issuer误写`ca`的问题。
正确语法为`issuer acme { dir <directory_url> }`，见[官方文档](https://caddyserver.com/docs/caddyfile/directives/tls#acme)。
受保护修复确认其他站点与运行配置一致，备份到`/opt/caddy/s2s-config-backup-miiiwzul`后，
仅改目标字段并同步已有挂载、重载、核验实际运行JSON。日志确认签发成功，证书到期2027-01-15。
17项本地测试通过。后续激活脚本已加入挂载一致性检查与运行配置验收。
应用release保留，实际代理修复来自staging；旧release内错误部署片段不要再次执行。

以下是准备、激活及故障诊断的历史过程；其中“未通过/待执行”不代表当前状态。

## 通话界面更新（待管理员发布）

用户已要求把通话界面更新到现有站点。UI提交`3f6501e9e9d9164c9f357681e55511bd35be4215`，
发布包目录`/home/kirin/apps/s2s_demo/releases/ui-3f6501e9e9d9`。
代码已推送GitHub main，发布包已上传并通过远端文件校验和Python语法检查。
非交互sudo检查为false；尚未执行容器切换，公网仍是旧UI。
包中仅6个web文件、[更新脚本](../deploy/update_ui.py)及无秘密的版本/文件校验清单。
脚本验证包、现有应用身份/只读根目录/共享Caddy网络及本地基础镜像；只在原镜像上COPY新的web，
构建无网络、无依赖更新。新容器沿用现有容器的运行配置，值只在服务器子进程环境传递，
不输出、不写入文件或argv，不从本机复制任何秘密。
保留原应用容器为`s2s-demo.pre-ui-3f6501e9e9d9`，只更新S2S容器，
不改Caddy/其他应用/证书。切换短暂中断现有S2S通话；内部登录页、Flash就绪及新CSS校验失败会回滚。
新版本生效不主动连接模型。备份容器保留为手动停止状态，以便后续回滚。

服务器kirin无免密sudo；用户须在自己的终端输入管理员密码，禁止使用任何聊天中曾贴出的密码：

```sh
ssh -t -F /Users/kirin/go_path/jev_demo/.run/server-management/ssh_config aliyun-kirin 'sudo python3 /home/kirin/apps/s2s_demo/releases/ui-3f6501e9e9d9/deploy/update_ui.py --approve-ui-update'
```

本地更新保护测试3项通过：环境值不出现在argv、健康失败回滚原容器、不删除未知容器。
管理员执行后再校验公网CSS/JS内容、新版页面和未认证WSS401；当前尚未宣称远端新版已生效。

2026-10-18 已只读核验现有服务器 `47.116.102.49`，SSH别名 `aliyun-kirin`、用户 `kirin`。
代码准备放在 `/home/kirin/apps/s2s_demo/releases/<commit>/`，秘密单独位于
`/home/kirin/apps/s2s_demo/shared/.env`（0600），不从本机复制任何 Key 或口令。

## 已有入口与待批准变更

服务器现有 Docker Caddy 使用 `/opt/caddy/Caddyfile`，80/443已开放并监听；既有主站等保持原样。
`s2s.kirinxu.cn` 的现有DNS已解析到该服务器，目前没有可用证书。
用户已明确确认使用此域名，不再重复确认。准备的 [站点配置](../deploy/Caddyfile.s2s) 只新增此子域名，
仅对此站点显式配置一个ACME issuer：`https://acme-v02.api.letsencrypt.org/directory`。
因此该站点只向Let's Encrypt申请/续期，不回退ZeroSSL，不修改其他站点的CA策略。
管理员激活前应审阅并接受[Let's Encrypt当前订户协议](https://letsencrypt.org/documents/LE-SA-v1.8-July-06-2026.pdf)；
此链接已通过官方ACME目录的`termsOfService`字段核对。用户已执行激活，内部登录页HTTP200、
Flash配置就绪；Caddy配置验证和reload命令已完成。公网TLS仍失败，不能视为部署验收通过。
不买域名、不开新公网端口、不改防火墙或安全组。Caddy的WebSocket代理默认支持升级，无默认流超时：
[官方反代文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。

无秘密代码已上传到`/home/kirin/apps/s2s_demo/releases/3f670692f010`，空白远端.env已创建为0600。
该目录保留为旧版；固定LE的最终准备版使用独立目录
`/home/kirin/apps/s2s_demo/releases/20261018-le-only`，激活请仅使用下方新命令。
服务器Python3.12.3、独立check-venv中的aiohttp3.14.5；系统ensurepip不可用，已用现有pip的
`--python`选项安装到`--without-pip`用户venv，没有修改系统包。禁用模型的真实进程检查通过：
登录页、预览登录、对话页和全部静态资源HTTP200，`ready=false`，已认证`/ws`返回503。
本次专属检查进程已停止，没有启动公网容器或修改Caddy。

应用容器使用现有Caddy的网络命名空间，仍只监听该命名空间的127.0.0.1:8080；不映射主机端口。
因此无需把Python绑定改成0.0.0.0。应用具有只读根文件系统、无额外capability和资源上限。
若以后重建Caddy容器，需相应重建s2s容器以共享新的命名空间。

当前kirin用户不能免密sudo，也无Docker socket写权限。已准备
[管理员激活脚本](../deploy/activate-aliyun.sh)，需要明确批准新增公开站点，并由管理员执行。
该脚本先校验远端.env、现有Caddy挂载和内部端口，构建新应用、验证本机登录页，再备份原Caddyfile、
追加新站点、验证并reload；失败时恢复原配置并移除本次新建的应用容器，不停止未知或既有应用。
reload可能关闭既有WebSocket连接，需要接受一次配置重载的影响；不重启Caddy容器。

## 秘密交接和最终启动

由用户在服务器编辑`shared/.env`，只填写`DASHSCOPE_API_KEY`和`S2S_DEMO_PASSWORD`；
普通配置已按 [远端模板](../deploy/aliyun.env.example) 固定为Flash、北京、`https://s2s.kirinxu.cn`。
文件沿用本项目字面解析规则，不执行shell；脚本不打印值，不把值放进argv或镜像。
不要把本机.env传上去，不在聊天中粘贴秘密。激活脚本先清除继承的模型配置，使用远端文件。

域名已获确认。用户安全填写远端秘密、管理员审阅LE证书条款后，在已登录的服务器终端运行：

```sh
sudo bash /home/kirin/apps/s2s_demo/releases/20261018-le-only/deploy/activate-aliyun.sh --approve-public-site
```

管理员密码只在用户自己的终端输入，不交给Codex。脚本不会自动进行模型握手、音频或生成。
完成后仍须从外网核验可信证书、登录、静态资源和WSS边界，再由用户手机接受麦克风权限并验收语音。

## 只读证书日志交接

当前应用内部检查已通过，但新站公网TLS尚未通过，HTTP外网有`Non-compliance ICP Filing`拦截。
需要Caddy证书日志进一步确定签发原因；不要重复运行激活脚本。
辅助脚本位于`/home/kirin/apps/s2s_demo/staging/tls-diagnostics/`。
用户在Mac自己的终端执行一条命令并输入服务器sudo密码：

```sh
ssh -t -F /Users/kirin/go_path/jev_demo/.run/server-management/ssh_config aliyun-kirin 'sudo bash /home/kirin/apps/s2s_demo/staging/tls-diagnostics/collect-caddy-tls.sh'
```

首次执行返回`matched_s2s_certificate_events=0`，仅表示没有匹配日志，不能推断证书成功。
已核验磁盘配置没有关闭自动HTTPS，原主站TLS正常，新域名仍报`TLSV1_ALERT_INTERNAL_ERROR`。
更新后的脚本读取已有Caddy最近24小时、最多10000行日志，输出最新20条本域名TLS事件的
`msg/error/identifier/ca`及日志行数；过滤其他站点与HTTP请求日志、常见秘密格式。
同时只读查询容器运行状态、日志驱动及Caddy运行配置，仅输出目标路由/证书策略是否存在等布尔值，
不输出原始配置。不读取.env、请求头或证书私钥，不改文件、不reload/restart、不签发证书、不调用模型。
管理员权限仅用于此次状态与日志读取。

## 新站点未加载时的修复

用户回传诊断确认`active_config_readable=true`，但目标路由、显式证书策略均不存在，
49条JSON日志中目标域名出现0次；磁盘Caddyfile则有且只有一个目标站点。
因此当前首先需要解决磁盘配置与运行配置不一致，再验收证书；尚不能据此认定ACME失败原因。
修复输出`mounted_file_matches_host=false`确认容器挂载内容与宿主文件不同。
首次管理员修复在配置适配阶段退出，尚未输出`only_s2s_change_verified`，没有修改配置。
原错误提示未保留阶段，已增加固定阶段/退出码/错误类别，仍不输出原始stderr。

已准备[管理员修复脚本](../deploy/repair_caddy_s2s.py)，上传到同一staging目录。
它先验证“磁盘配置移除原样S2S站点后”与当前运行配置完全一致，否则停止；
然后备份宿主文件、容器挂载文件及运行JSON到root专用0700目录，文件0600。
同步已有挂载文件，通过容器内0600临时文件加载已验证的配置，并核验实际运行JSON。
临时文件使用后删除；避免依赖已安装Caddy对`--config -`的支持。
后续实际Caddy诊断确认是目标块`ca`字段错误，非stdin兼容问题；当前保留临时文件方式。
失败时恢复原挂载内容和原运行配置。无需重新构建或重启任何容器；
重载仍可能关闭既有WebSocket连接，并启动此前已批准的目标站点LE证书申请。
不读取应用.env、不调用模型。命令由用户在自己的终端输入sudo密码执行：

```sh
ssh -t -F /Users/kirin/go_path/jev_demo/.run/server-management/ssh_config aliyun-kirin 'sudo python3 /home/kirin/apps/s2s_demo/staging/tls-diagnostics/repair_caddy_s2s.py --apply-approved-site && sudo bash /home/kirin/apps/s2s_demo/staging/tls-diagnostics/collect-caddy-tls.sh'
```

本地16项诊断/修复测试通过，包括拒绝其他站点差异、同步旧挂载、重载失败回滚、
临时文件清理和错误信息不暴露stderr秘密。远端首次管理员修复已安全停止，
更新版尚未执行；证书及公网访问仍待验证。

## 先前准备与验收边界

Dockerfile 只准备运行镜像，尚未构建验证；不创建云资源，不推镜像，不更改安全组。镜像内服务仍只监听 `127.0.0.1:8080`，没有公开端口。后续需结合实际反代拓扑审阅绑定方式，不能直接以 `-p` 期待访问环回服务。

1. 用户在阿里云账户确认北京（当前默认）或新加坡地域、`qwen-audio-3.0-realtime-flash`权限与预算。Audio 公共端点不再要求提供 Workspace ID。
2. 在部署环境秘密管理中注入对应地域的 DashScope API Key（变量名 `DASHSCOPE_API_KEY`）与独立 `S2S_DEMO_PASSWORD`，不在聊天、Dockerfile、构建参数或仓库里录入。配置地域、精确 HTTPS `S2S_ORIGIN`；默认不设置 `QWEN_WORKSPACE_ID`，仅显式选专属地址时才设置。凭据由运行时进程读取，修改后需重启；单独保存到 Vault 不代表已绑定任务。不要把 AccessKey 或 NLS Token 代替 DashScope API Key。临时 API Key 的申请/续期不在本轮实现范围。
3. 用已有可信HTTPS反代提供 `/login`、`/chat`、静态资源与 `/ws`。反代须支持WebSocket Upgrade，空闲超时大于会话上限；不缓存API/登录响应，不记录口令请求体、Cookie、音频或转写。不依赖客户端伪造的转发头。
4. 当前单口令仅供受控演示；公开多人使用前需真实身份认证、按用户限流与预算。Cookie为随机服务端内存会话、HttpOnly、SameSite Strict，HTTPS Origin时Secure，1小时过期，重启失效。退出将撤销同Cookie下所有已打开WebSocket。登录每IP最多每分钟5次，最多256个登录会话。无找回密码、账号注册或持久账号库。
5. 后续明确预算后才启用 `S2S_ENABLE_LIVE=1`，执行少量真实握手/音频验证。个人助理与StackChan接入已在本地实现但未部署；正式用户身份、Memory Hub与其他工具仍待独立设计。

真机验收：iOS Safari与Android Chrome分别验证HTTPS麦克风授权/拒绝、首次触控启音、至少3轮、说话/按钮打断、停止/重连、网络切换、锁屏/后台、来电/系统音频暂停、耳机/蓝牙切换。记录首包、首音频、打断尾音及配额用量；使用授权测试音频，不默认上传真实用户音频。当前 Chromium 合成音频测试不替代这些步骤。
