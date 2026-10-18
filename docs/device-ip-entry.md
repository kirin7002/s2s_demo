# 专用 IP 设备入口

目标为 `wss://47.116.102.49:8443/device/v1/ws`，仅用于已配置的 StackChan。
IP 与端口是否能通过当前云侧入口，必须由设备实测，不能由本机健康检查推断。
用户已在轻量服务器防火墙放行 IPv4 TCP8443；无需新增 UDP、80、443 或管理端口。

## 隔离方式

只新增 `s2s-device-ip` 侧车，使用运行中主 Caddy 的精确镜像 ID，不拉取或升级镜像。
主 Caddy、S2S 容器、既有站点配置及网页 Origin 均不修改、不重启或 reload。
侧车加入主 Caddy 已有的用户定义 bridge 网络，发布 `0.0.0.0:8443:8443/tcp`。
精确 GET `/device/v1/ws` 经 HTTPS 转发到 `caddy:443`，设置
`Host: s2s.kirinxu.cn` 与 `tls_server_name s2s.kirinxu.cn`，验证上游公有证书。
其他路径一律404，管理API关闭、无HTTP重定向或HTTP/3端口。
原 S2S 继续检查设备 token 和无浏览器 Origin，模型Key、演示口令不进入侧车。
版本检查、配置预检和正式侧车均显式调用`/usr/bin/caddy`，使用相同UID0与权限配置：
drop ALL后只保留`NET_BIND_SERVICE`，兼容镜像二进制的file capability；启用no-new-privileges。
预检仅在服务器私密临时目录复制CA供同权限验证容器读取，完成即清理，不导出私钥。

独立私有CA的根与私钥位于服务器 `shared/device-ip-entry/ca/`；私钥不导出。
公共根导出为 `shared/device-ip-root-ca.pem`，固件配置为
`shared/stackchan-device-ip-config.json`，原设备token不变；原域名版配置保留。
侧车持久化 `/data` 与 `/config`，由Caddy自动签发、续期IP SAN叶证书及中间证书。
根证书固定、有效期10年，根到期前需单独规划更换；叶证书默认12小时、中间证书默认7天。
`default_sni`仅在侧车内设为IP，兼容不发送SNI的IP客户端，不影响主Caddy。
不向浏览器/系统全局安装此CA，不关闭主机名/IP校验。

## 发布与回滚

`scripts/prepare_device_ip_release.py`要求干净提交，生成仅含部署脚本、Caddyfile和清单的包。
包内没有token、模型Key、口令或任何私钥；公共配置0644，目录0700。
服务器必须先准备专用CA及IP配置。精确包路径、SHA和管理员命令以
`.run/s2s-cloud/device-ip-release-status.json`为准。

管理员执行 `device_ip_entry.py` 默认仅预检：核对现有容器身份、共享网络、端口空闲、
同token与CA、实际Caddy版本，并用相同镜像在无网络临时容器验证配置。
临时验证不修改生产配置或持久CA。`--apply`才创建独立入口。
验证失败自动移除本次已识别侧车，并恢复之前的同名侧车（如果存在）。
`--rollback`按保存的容器ID核验后停用新侧车，保留容器和CA数据，并恢复之前的侧车。
不会删除或停用身份不符的容器。默认无任何主机防火墙或云安全组变更。

健康检查覆盖：以IP为`server_hostname`、无SNI且可信根验证成功；TLS1.2；
错误CA/IP拒绝；缺失/错误token401；其他路径404；合法token WebSocket升级101后立即关闭，
不发送hello、不注册假设备、不获取控制权、不调用模型或动作。
合法升级不代表真实设备已上线，设备能力和控制仍须固件侧真实连接后确认。

参考：[Caddy独立PKI和default_sni](https://caddyserver.com/docs/caddyfile/options)、
[内部证书的自动续期](https://caddyserver.com/docs/caddyfile/directives/tls#internal)。
