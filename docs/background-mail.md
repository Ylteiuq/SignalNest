# 后台邮件执行

外部 timer 触发有界单次命令，不在 Python 内增加常驻调度器。默认 `[mail_runtime].enabled = false`，旧配置和仅采集实例继续原有行为；本次没有安装服务或发送真实邮件。

## 显式启用

先备份、升级数据库、核对画像并通过 `notifications-activate` 启用通知。已有实例升级政策 v2 还须显式执行 `notifications-policy-update`，步骤见 [规则说明](notifications.md)。通知未启用时，邮件入口明确失败，不自动启用或补发全部历史。

在生产 TOML 中保留正确的 `[smtp]`（TLS、服务器、地址、凭据环境变量名），再增加：

```toml
[mail_runtime]
enabled = true
max_plan_messages = 5
max_events = 50
max_bytes = 131072

[mail_sending]
max_messages = 5
run_seconds = 300
```

计划限制为每轮 1–20 封、每封 1–100 事件、完整 MIME 1 KiB–1 MiB；SMTP 尝试/退避/due 继续由 `[mail_sending]` 和既有投递状态控制。启用但没有 `[smtp]` 为配置错误。配置检查只校验参数，不读环境凭据、不访问数据库、不联网。

```sh
signalnest config-check --config /etc/signalnest/signalnest.toml
signalnest mail-status --config /etc/signalnest/signalnest.toml
# enabled=true 时会计划并发送，运行前先核对 mail-preview 与暂停状态。
signalnest scheduled-mail --config /etc/signalnest/signalnest.toml
```

`scheduled-mail` 每轮先计划，再发送到期冻结邮件，输出一个 JSON 摘要。正常退出 0，单项阻断/投递失败/暂停等需处理结果为 1，配置错误为 2，中断为 130；disabled 只返回跳过摘要，不建库、不取锁或连接 SMTP。

## 阶段与失败边界

库入口 `run_mail_pass(settings, ...)` 在同一实例写锁内执行 N2 计划与 N4 发送。Parser、文件读取、渲染及 SMTP 均不放入业务写事务。`run_scheduled_cycle(settings, mode, ...)` 先调用既有采集协调器，再调用邮件阶段；`scheduled-run` 仅在显式 enabled 时使用这个组合入口。

| 情况 | 本轮行为 |
| --- | --- |
| 普通 HTTP/解析失败、受限覆盖或采集预算不足，已可靠登记 | 继续邮件阶段；摘要保留采集失败，退出非零。 |
| 单项邮件过大或有限渲染阻断，诊断已可靠保存 | 保留未分配项；其他项可计划，已有冻结邮件继续发送。 |
| 通道暂停 | 可计划；恢复遗留 sending；不发任何 SMTP。 |
| 凭据缺失/认证永久错误 | 有限通道错误，持久暂停，结束发送阶段。 |
| 数据库、归档、失败登记、计划或投递登记等系统错误 | 停止本轮，不越过该错误继续发送。 |
| 实例锁被另一个进程持有 | 立即拒绝，不计划或发送；下次 timer 再试。 |

采集释放锁后，邮件重新取得同一数据库锁；两阶段之间存在正常争锁窗口，拒绝仍是显式失败，不隐含重试。独立邮件 timer 保证已有冻结邮件无需等待一次成功采集；每轮仍先检查数据库与冻结证据。程序缺陷不会被宽泛异常捕获转换为普通失败。

事件包含 `mail_background_started`、`mail_planned`、`mail_background_finished`；有限错误分别标记 `mail_plan`、`mail_send`、`mail_storage`、`background_abort`。使用 source_id/run_id/mail_id/attempt_no 定位，无邮件正文、地址或凭据。`mail-status` 与日志分别用于积压事实和本轮阶段诊断。

## systemd 与凭据

沿用 [运维模板](operations.md) 的 Linux/systemd 252+、服务账号、数据库路径和 journal namespace。新增 `signalnest-mail.service` / `.timer`：启动后两分钟检查一次，此后上海时间每小时 02、07、12…57 分钟执行；持久 timer 停机后至多补一次触发，积压由多轮额度推进。regular/full 的 `scheduled-run` 也在采集后计划/发送；独立 timer 在采集未正常收尾时继续提供邮件入口。不同服务共用写锁，没有并行发送。

两个服务均读取可选 `/etc/signalnest/smtp.env`。由管理员在目标机创建，root 拥有、权限 0600，变量名与 SMTP 配置一致；文件使用 `NAME=value`，不要写 `export` 或 shell 命令。真实密码不要写进 TOML、仓库、unit 或命令行。文件缺失不会阻止仅采集实例启动；邮件已启用但凭据缺失时会暂停，修复后须核对 `mail-status` 并显式 `mail-resume`。

EnvironmentFile 由服务管理器读取后注入进程，适配器只在真正发送时取值；它不是加密秘密仓库，目标机仍须限制管理员与进程环境读取权限。[systemd 执行环境](https://github.com/systemd/systemd/blob/v252/man/systemd.exec.xml)

组合采集服务为 `TimeoutStartSec=20min`，邮件服务为 `10min`，停止宽限均 30 秒。默认采集 600 秒加发送 300 秒是协作预算；计划开销、阻塞连接等仍由服务上限兜底。修改应用预算时同步检查服务上限。oneshot 的启动超时约束整个命令，超时先终止、宽限后可强杀；原定时器不设自动重启，下一固定周期恢复。[systemd 服务超时](https://github.com/systemd/systemd/blob/v252/man/systemd.service.xml)

SMTP 已接受但本地结果尚未提交时终止，仍可能重复发送；后续持锁运行把遗留 sending 一次登记为 uncertain 并保存冷却。已提交 accepted 不重发，Message-ID 不证明外部去重。恢复副本先暂停且不注入凭据，见 [邮件备份恢复](mail-backup.md)。

## 实际验证范围

离线集成测试连接真实通知/冻结/发送/SQLite，验证自动计划、分批积压、普通采集失败、持久单项阻断、系统错误停止和暂停。真实子进程验证统一争锁及释放、环境凭据被实际 SMTP 适配器读取、接受后 supervisor SIGTERM/宽限/SIGKILL 与下一进程暂停恢复；SMTP 服务为本地 socketpair，相关进程测试用透明 TLS wrapper，真实本地 TLS 握手另由 N3 测试覆盖。

当前验证主机为 macOS，unit 的路径/入口/时间预算/凭据声明做静态校验；supervisor 实验不是实际 systemd 超时实验。目标 Linux 仍须执行 `systemd-analyze verify/calendar`、受控服务启动与超时演练，再启用生产 timer。没有真实邮箱、校园请求、断电或长期无人值守运行验证。
