# 通知政策更新与有界重评

N4 提供显式、本地的政策更新和事件重评。它不建立新的内容事件、不重新采集、不发送邮件，也不修改已冻结邮件。所有写入由调用者持有实例写入锁；CLI 已统一加锁。预览只读，不获取写入锁。

先检查画像，再预览政策差异；`--at` 是显式 UTC Unix 秒。操作标识使用 1–64 个 ASCII 字母、数字或 `_.:-`，同一标识不可改参数，也不可同时用于政策更新和重评。

```sh
signalnest profile-check --profile profile.example.toml
signalnest notifications-policy-update --config config.toml \
  --profile profile.toml --operation-id policy-20261008 --at 1791421200 --preview
signalnest notifications-policy-update --config config.toml \
  --profile profile.toml --operation-id policy-20261008 --at 1791421200
```

政策更新保存 `policy_manifest(profile)` 的不可变快照，复用相同摘要的既有 revision，并更新通道的后续决策政策。它不会扫描历史、创建投递资格或更改已有决策。政策 revision、通道指针和操作结果一起提交；其中任一步失败则一起回滚。重放已完成的更新返回原结果，不会把更晚的政策指针恢复成旧政策。代码规则升级后，可显式更新政策，以替换已经不能由当前代码执行的旧 manifest。

重评采用明确的事件 ID，最多 100 个输入 ID，排序去重后固定集合。事件 ID 可从数据库事件记录读取，不能按文章数值 ID 猜测。先预览原因，再决定是否登记新的投递资格：

```sh
signalnest notifications-reevaluate --config config.toml \
  --operation-id review-20261008 --event-id 17 --event-id 23 --at 1791421200 --preview
signalnest notifications-reevaluate --config config.toml \
  --operation-id review-20261008 --event-id 17 --event-id 23 --at 1791421200
# 恢复原操作：省略事件集合与时间，保持原政策、原时钟和原输入。
signalnest notifications-reevaluate --config config.toml --operation-id review-20261008
```

当前持久化重评只接受以下事件：属于本次启用实例；对应通知当前版本和最后一次 live 内容事件；最终路线为 `none`；投递资格时间、立即意图和冻结邮件成员均为空；按固定评估时钟，仍在最近七个上海自然日内，或是具有可信期限且已经开放的机会。近期不是邮件资格本身，仍须由纯规则判断。近期但已经截止的事件可得到新的 `STORE_ONLY` 决策；很久以前且已经截止的事件不进入持久重评。已有 `immediate` 或 `digest` 资格的当前事件可以预览，不能重新登记资格；Digest 没有立即 outbox 也已经锁定。

首次创建操作时，读取确定的事件、正文版本、当前 live 观察证据和选中决策，关闭读取事务后提取事实并运行规则。政策、评估时钟、上海 Digest 时间、事件上下文、事实及完整决策一并冻结。正文全文不额外写入操作或决策；已有版本保存正文，快照保留有限原文证据。规则提取、决策与文件 I/O 不在业务写入事务中执行。

`notification_operations` 是一个小批操作日志，复用既有政策、事件和决策表，不是后台队列。它保存规范参数及摘要、带摘要的不可变输入/决策快照、成员结果和完成时间。每个成员各用一个短事务提交新的决策、选中决策、最终路线、必要立即意图和操作进展。决策的 `evaluation_key` 使用 `operation:<operation_id>`，避免与 N1 的 `initial` 保留值冲突；邮件身份仍属于原事件，不属于操作 ID。

恢复先返回已经完成成员的原结果；尚未完成的成员继续原集合、原政策和原时钟，不重新运行筛选或规则。显式重放参数不同返回 `notification_operation_parameters_mismatch`。未完成项的内容/观察/选中决策变化记为 `stale_input`；政策、模式、Digest 日历或地址变化记为 `stale_context`。发送暂停及跨过自然日或截止时间本身不会改变原快照。过期时间需重新判断时，使用新操作 ID 和新显式评估时间；不能改写原操作。

`evaluated_at` 始终是固定的决策时钟；重评操作的 `completed_at` 是实际完成观察时间，二者独立。可组合接口允许注入完成时钟做测试，时钟回退到评估时间之前会明确失败并回滚该成员。已完成成员不再次读取时钟。

`complete=true` 表示所有原成员已有终结结果，并不表示全部获准登记。`stale_input` / `stale_context` 需要查看原因并另建操作，CLI 返回非零退出码。数据库写入失败返回 `notification_database_write_failed`，未完成成员与操作保留用于恢复；不将它伪装成“没有变化”。成员在 SQL 故障前成功提交的结果保持有效，恢复只处理剩余成员。

可独立调用的入口位于 `signalnest.notifications.maintenance`：

- `preview_policy_update` / `update_policy`：只读政策比较与显式不可变政策更新。
- `prepare_reevaluation`：纯本地准备固定集合、证据、规则决策；不写数据库。
- `register_reevaluation_in_transaction`：使用调用者的 Connection 注册准备结果并验证输入 token。
- `reevaluate_events`：有界预览、创建并逐项应用、或只凭 ID 恢复。

限制：只重评明确选择的当前 live 事件；不提供候选查询语言、自动全历史重评、资格撤销、取消后改投或持续时钟驱动重评。旧政策可审计，不保证当前代码能执行任意旧引擎；已经冻结的决策和邮件不重新计算。测试覆盖真实 SQLite 事务故障和关闭重开后的操作恢复，这部分不宣称完成断电验证。

## 政策 v3 的显式升级

事实提取/规则/引擎已更新为 v3，路线仍为 v1；v3 区分实际报名机会与顺带提及，保留 v2 跨主题优先级和时间/申报主体修正。已有实例先保留备份，用新代码预览画像和真实样本，再显式 `notifications-policy-update --profile ... --operation-id ... --at ...`，使用新的 operation ID；不通过初始化或修改原 revision 自动更新。旧政策期间生产 live 处理会明确返回过期政策错误，应先更新后恢复采集。升级不补发历史、不改已有投递资格或冻结 bytes；旧误命中的 Digest 资格不会自动撤回。

已持久登记的 v1/v2 维护操作按原 operation ID 单独恢复，复用原固定快照/时钟/决策，不调用新版规则；需要新版评估时另建明确操作。Profile/Facts 的新增可空字段未提供时省略，包括 TopicMatch.context，原摘要继续可校验。规则升级步骤、样本结果和未知范围见 [政策 v3](notifications.md)。
