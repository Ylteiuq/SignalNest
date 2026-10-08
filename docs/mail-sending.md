# N4：发送协调、诊断与恢复

发送只消费 N2 已冻结的邮件，不改变正文成功状态、事件成员、正文、地址或 Message-ID。`mail_messages.state=pending` 是冻结产物的旧标记；SMTP 状态的唯一来源是新增 `mail_delivery`。N1 的立即意图与 Digest 资格继续由原表管理。

## 持久事实与事务

- `mail_delivery`：每封冻结邮件的状态、累计尝试数、一次人工许可、下次尝试时间、接受时间及阻断理由。
- `mail_attempts`：每次尝试的起止时间、有限结果/阶段/错误类别、SMTP 数字回复、清理失败、恢复事实与未知最小冷却。不保存服务器任意文本或凭据。
- 原通道的 `paused` 复用为发送暂停，加暂停原因和时间；暂停不阻止内容事件或本地邮件计划。
- `notification_operations`：明确政策更新或受限重评的固定参数、输入快照和进度，政策 manifest 仍复用已有 revision。

0006 只增加表和通道字段；既有冻结邮件得到 pending 投递状态，不补建历史事件、不发送邮件。新计划将邮件、成员与 pending 状态一起提交。

调用方跨整个写入操作持实例 writer lock。网络前短事务提交 sending 与 attempt；冻结邮件读取和 SMTP 在事务外；网络后另一个短事务一起提交尝试结果和投递状态。任何登记或结果保存失败立即终止本次 drain，不能继续发送下一封。SMTP 不重试，重试只由协调器跨运行安排。

## 重试与中断

默认最多六次自动尝试，失败后的退避为 5 分钟、15 分钟、1 小时、6 小时、24 小时；不确定结果至少等待 30 分钟。每次 drain 至多五封，每封一次，默认协作预算 300 秒。预算在各邮件开始前检查，不强行取消已进入 SMTP 的调用，socket timeout 也不是整个运行的墙钟保证。

pending/retry/uncertain 到期才可发送。明确永久失败或额度耗尽保留为 blocked；永久通道错误暂停整个通道，暂时通道错误把本次 drain 提前结束，避免对同一故障连续发送积压。accepted 表示 SMTP 最终 250 已观察且本地提交，并不证明进入收件箱。

获得写锁后，遗留 sending 一次性转为 uncertain，结束旧 attempt，不增加次数。恢复后 due 持久化，再启动不顺延；降低尝试上限会把已耗尽的待办登记为 blocked，不阻塞其他邮件；耗尽额度时 blocked 仍保留 uncertain 诊断。人工 retry 只授予原邮件一次额外尝试，不清零次数，不改变内容；不确定邮件仍遵守冷却。accepted 禁止重发。

SMTP 已接受但本地结果未提交时，恢复可能重发同一 Message-ID，外部可能收到两封；不能以 Message-ID 宣称服务端去重。恢复旧备份也可能失去接受事实，必须先暂停发送并人工核对。

## 接口

`drain_mail`、`recover_sending`、`register_attempt`、`finish_attempt`、`retry_mail`、`set_sending_paused` 的写入调用方持实例锁；`mail_status` 只读。`DrainOptions` 固定本轮参数；`[mail_sending]` 配置数量、预算及有限退避，不单独启用 SMTP。策略维护见 [notification-maintenance.md](notification-maintenance.md)。

```sh
signalnest storage-init --config signalnest.toml
signalnest mail-status --config signalnest.toml
signalnest mail-pause --config signalnest.toml
signalnest mail-resume --config signalnest.toml
signalnest mail-retry --config signalnest.toml --mail-id 1
# 配置 SMTP 并核对 mail-preview 后，显式发送已经冻结的到期邮件。
signalnest mail-drain --config signalnest.toml --max-messages 5 --run-seconds 300
```

status/retry/pause/resume 可显式给 UTC Unix 秒 `--at`；drain 使用实际运行时钟。retry 只适用于 blocked 邮件，重复授予尚未消费的同一许可不叠加；retry/uncertain 的自动 due 不能被此入口提前。暂停期间仍可恢复遗留 attempt、查看状态、积压事件和计划。修复通道并 resume 不会自动恢复单封永久失败；需要独立人工 retry。

`mail-status` 计数覆盖全部冻结邮件，默认至多展示 100 封并报告 `messages_truncated`，优先展示未接受任务；不给出正文、地址、环境值或任意服务器回复。`uncertain_count` 为当前 sending 或最近结果未知的邮件数，`uncertain_attempt_count` 为累计未知尝试次数；后来 accepted 不会擦掉历史不确定性。还显示未计划立即意图、未分配 Digest、到期数、最早未接受邮件冻结时间、暂停原因和有限最近尝试诊断。

普通单封暂时/永久失败继续其他待办；通道暂时失败结束本轮而保留该邮件 due，永久失败持久暂停。仅已提交的 accepted 禁止后续发送。drain 失败/未知/暂停/阻断需处理时返回 1，正常有界执行返回 0；输入或缺失 SMTP 配置返回 2。status 的 0 表示诊断读取成功，不意味着全部邮件健康。未启用渠道、存储/锁故障均明确非零。

## 验证与限制

2026-10-08，macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1、OpenSSL 3.5.8；现有依赖和锁文件不变。全套 **1435 项离线测试通过**，新增 N4 198 项：发送政策/事务/积压 43、配置 31、CLI 39、迁移 6、政策维护 72、真实进程终止 7。Ruff、格式检查（src/tests 与既有备份校验程序共 90 文件）及 diff 检查通过。

真实临时 SQLite 验证尝试/结果整体回滚、登记失败不发送、结果失败停止后续发送、失败保留正文成功、六次自动与一次人工许可、due 与未知冷却跨重启、降低额度、时钟倒退、损坏冻结内容、12 封分轮发送和 101 封有界状态诊断。真实 0005 旧库包含 N1 事件/N2 冻结 bytes 与成员；0006 升级保留全部旧事实，五个 DDL/回填故障点回滚，重复初始化安全。政策维护固定输入恢复另有事务故障与关闭重开验证。

七项 POSIX SIGKILL 实验使用实际发送协调器、SQLite、归档/Parser 形成的冻结邮件及父进程 socketpair SMTP 模拟服务。覆盖尝试事务提交前、提交后网络前、服务接受但不给最终回复、适配器观察最终 250、结果事务提交前、结果提交后，以及人工许可已消费时终止。父进程独立持久接受日志证实未知窗口可产生两次接受；冻结内容/MID/成员不变，accepted 提交后无重发。不是 MockTransport 代替的进程终止实验，也不是断电或公网 SMTP 验证。

支持范围沿用 POSIX 单实例写锁；实际测试为 macOS，未在 Linux/Windows 部署发送。同步 socket timeout 和协作预算不能保证硬墙钟上限。没有真实账户兼容、退信/垃圾箱/收件箱验证、服务端去重或 exactly-once 保证。后续 [后台邮件](background-mail.md) 提供显式启用的定时模板，尚未部署。

帮助、示例配置检查从另一工作目录通过，不取锁、不创建存储；缺少 SMTP 的 drain 明确返回 2。75 份已跟踪 research/fixture、Parser、原契约/N0/N1、rawstore/ingestion、0001–0004 与依赖文件逐字节未变；0005 保持原样。保留已有 N2/N3 修改，无提交、推送或 PR。

本批只做离线模拟 SMTP 与真实子进程中断验证，不测试真实账户、不安排生产定时发送。
