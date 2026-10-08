# 含邮件状态的备份与恢复

备份必须同时保留 SQLite、完整 `raw/`、配置、锁定依赖和对应源码版本。停止采集与邮件 timer，等待正在运行的服务退出，再用实例 `writer_lock` 覆盖 SQLite backup API 与 raw 复制；一致快照步骤见 [运维说明](operations.md)。仅复制运行中的 SQLite 主文件不能代替这个步骤。冻结 MIME、成员、投递状态、尝试和通道暂停都在同一数据库中，没有独立邮件目录。

## 只读专项校验

用备份对应代码版本，在独立恢复目录执行：

```sh
/opt/signalnest/.venv/bin/python /opt/signalnest/deploy/verify_backup.py \
  --database /var/lib/signalnest-restore/signalnest.sqlite3 \
  --data-dir /var/lib/signalnest-restore
```

校验器使用 SQLite `mode=ro` 与 `query_only`；不会初始化、升级、修补、恢复 `sending` 或发送邮件。它先检查 revision、SQLite 完整性与外键，再验证通知成功指针及每个被引用原文的安全路径和 SHA。邮件专项验证包括：

- 冻结 bytes 的 SHA、大小、CRLF、纯文本 UTF-8 MIME，以及 From、To、Subject、Message-ID 和 Date 与保存字段一致。不会用当前渲染器重新生成旧邮件。
- 成员顺序、精确成员摘要、事件与决策归属，冻结快照与历史版本、原文响应和政策摘要的绑定。200 观察证据必须就是该正文响应；304 必须无正文，绑定同来源、同通知、同 URI/profile 的具体 200，并满足当时响应的保守缓存资格。检查历史绑定时不要求原文仍是资源最新响应。使用冻结决策对应的历史政策，不以当前 Profile、当前通知版本或现行规则重评。
- 每封邮件唯一的投递状态，累计尝试与从 1 开始的连续编号、起止时间、有限结果和错误分类。`accepted` 必须对应最后一次已完成尝试的最终 250；`sending` 必须对应最后一次未完成尝试；`uncertain` 保留至少 30 分钟冷却且 due 不早于该冷却。
- pending、retry、blocked、人工一次重试的状态关系，以及通道暂停原因。旧迁移保留的未知暂停原因/时间允许同时为空，不猜测。

SQLite 类型亲和不能保证数字列保存的就是整数；校验器显式检查邮件额度、时间、投递计数、尝试与布尔字段的类型，损坏文本仍返回上述有限错误，不以宽泛异常捕获隐藏程序缺陷。

成功 JSON 包含 `mail_messages`、`mail_message_members`、`mail_attempts`、`mail_pending` / `mail_sending` / `mail_retry` / `mail_uncertain` / `mail_accepted` / `mail_blocked` 和 `mail_paused_channels`。有效 `sending` 与 `uncertain` 不是备份损坏，仍需要恢复诊断。错误以有限分类返回非零码，例如 `backup_mail_payload`、`backup_mail_members`、`backup_mail_evidence`、`backup_mail_attempts`、`backup_mail_delivery`、`backup_mail_channel_pause`，不输出邮件或异常正文。孤立原文与未完成临时文件只计数、不删除。

## 恢复副本先暂停

恢复环境不安装/启动采集与邮件 timer，不注入 SMTP 凭据。先校验副本，再建立专用 `restore.toml`，其 data_dir 和 database 指向副本，并保持 `[mail_runtime].enabled = false`。若需升级，保留原始备份，另复制一个副本显式升级并重新校验；不要在唯一备份上升级。

```sh
signalnest config-check --config /etc/signalnest/restore.toml
signalnest mail-pause --config /etc/signalnest/restore.toml
signalnest mail-status --config /etc/signalnest/restore.toml
signalnest mail-preview --config /etc/signalnest/restore.toml --mail-id 1
```

`mail-pause` 取得副本实例锁，只修改通道暂停；不会接触 SMTP。即使快照原本已暂停，也显式执行这一步。若从未启用通知，`mail-pause` 会明确报未启用，此时没有冻结投递任务，仍不要为恢复校验启用邮件。

暂停状态下可先人工核对 pending / accepted / sending / uncertain、累计未知尝试和服务端日志。发送协调器在持锁的首轮将遗留 `sending` 一次性登记为 `uncertain`，保留原尝试编号，持久保存恢复时间起的冷却；暂停仍阻止所有 SMTP。再次启动不会反复延后这个 due。只读 `mail-status` 与本校验器都不会执行此恢复。

正式切换前停止原实例所有写入者和 timer，保留其数据库/raw，确认原实例不会再次发送，然后才向恢复实例注入凭据、人工恢复通道和启用邮件定时入口。已提交的 `accepted` 不自动重发；仍有 `sending`/`uncertain` 时，不确定结果可能重复发送。快照之后发生的外部接受事实也可能不在备份中，不能因为本地 pending 就直接补发。Message-ID 不提供通用服务器去重，不删除投递状态、尝试或成员来清理积压。

## 实际验证范围

离线测试通过真实原文归档、Parser、通知成功事务、冻结邮件和投递服务，建立 pending、accepted、sending、uncertain 及暂停样本。用真正的 SQLite backup API 与 raw 复制生成快照，逐字段核对 frozen bytes、成员、投递和尝试；验证校验前后所有副本文件摘要不变。故障样本在外键与 SQLite 完整性仍成立时破坏邮件 SHA/头部、成员/决策绑定、尝试连续性、接受证据、未知 due 或暂停字段，必须拒绝且不修补。

恢复测试先暂停副本，证明不接触 SMTP；遗留 sending 仅恢复一次，重新打开数据库保持 due；人工恢复后只对其余任务注入模拟结果，accepted 不再次尝试，原实例未被修改。补充验证真实 304 所生成冻结事件在后续传输基线推进后仍可校验，并拒绝来源、目标、URI、profile 或绑定错配。旧 v1 序列化证据经真实 N2 冻结、显式 v2 政策更新和 SQLite backup 后仍按历史摘要验证，不执行现行规则；此案例使用一致的合成 v1 保存表示，不声称执行旧规则引擎或恢复外部旧数据库文件。这些结果是离线 SQLite、文件与事务验证，模拟接受不等于真实邮件发送。没有验证真实断电、异地介质损坏、目标 Linux 服务启动或真实 SMTP 收件箱投递；SHA/一致性检查也不是对恶意全面改写备份的密码学认证。
