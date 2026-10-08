# N1 通知启用、事件与成功事务

2026-10-07。**N1 已实现**：显式启用边界、真实列表日期证据、独立 live 比较基线、内容事件、完整决策快照和立即投递的 planned 意图。它接入现有生产采集成功事务。N1 模块不冻结或发送邮件。现已另行实现 [N2 本地邮件计划与冻结](mail-planning.md)；N3 的 [SMTP 适配器](smtp.md)已实现；政策更新、重评、暂停命令和发送恢复已由 [N4](mail-sending.md)另行完成。

## 复用与新增状态

复用 documents 的稳定 `(source_id, source_document_id)`、成功指针与唯一详情 due；复用 notice_versions 的规范内容、Parser 版本和幂等唯一键；复用 raw_responses/http_resources 的实际完整 200 与绑定 304；复用 ingestion_runs/source_ingestion_state 的运行来源、覆盖证明和冷却。通知模块不新增网站复查计划、任务平台或收件人系统。

迁移 `0004_notification_state` 只新建以下八张窄表，不修改 0001–0003 或重建已有表。升级没有自动启用、事件或历史投递：

| 表 | 实际用途与恢复约束 |
| --- | --- |
| notification_listing_evidence | 每篇最新可信列表日期及 body/observed 响应、Parser、处理时间/来源；另保存实际由生产列表插入的新身份的首次 run/time。日期不能用 discovered_at 代替。这张表可在启用前写入，为首启预览提供证据。 |
| notification_policy_revisions | `policy_manifest(profile)` 的不可变规范快照和唯一摘要，包含画像、事实规则、规则顺序/阈值、路由与版本。数据库 JSON 往返后按规范摘要校验。 |
| notification_channel_state | 单行 primary：安装身份、来源、启用操作 ID/时间、政策 revision、模式、固定近期政策、上海每日 Digest 时分及明确 from/to 地址；paused 为后续发送层保留状态，本轮无暂停命令。 |
| notification_activation_members | 启用时的稳定来源身份集合，不依赖整数 document ID 水位。候选状态、最新列表/详情日期、最初选择证据、首对冲突证据与首次生成事件引用。 |
| notification_observations | 每安装/通知独立 live version、**实际 body response**、本次 observed response、观察时间、事件序号，以及最近一次比较未知的有限代码/时间和前后原文/版本证据。 |
| notification_events | new/update/activation_recent、独立序号、前后版本及实际正文/观察证据、选中决策、最终路线和资格登记时间；事件不以内容 hash/version/run 为身份。 |
| notification_decisions | 每事件/政策/initial 唯一评估，完整 N0 Decision、有限 facts/context 短证据、明确评估时钟。完整正文仍引用版本/原文，不复制进日志。 |
| email_outbox | 每立即事件唯一 planned 意图、固定选中决策、primary 收件人、from/to、创建时间和稳定 delivery_key。没有 MIME、Message-ID、发送尝试或可发送状态。 |

比接口稿多出的列表证据表用于保存现有 documents 未持久化的真实 `ListEntry.published_date`；避免为这一项重建 SQLite 的原有循环外键图。首启证据保存最初选择和首对冲突两个有界快照，后来的日期一致不能抹掉已有不确定性。

## 启用与处理来源

首次启用要求来源已有一次生产完整列表覆盖事实；校验对应 run 的 source、coverage/time，bootstrap/regular 可用，historical 不作为资格。详情部分失败不阻止启用。完整覆盖仍由已有协调器实际证明，activation 不重新遍历网站。

`notifications-preview` 只读已初始化数据库，显示 ready/blocker、候选计数及最多 50 个近期身份与选择证据；不读 raw、不取锁、不联网。`notifications-activate` 持现有实例锁，在一笔短事务内冻结政策、安装/启用时间与全部已有身份。必须明确提供 Profile、启用操作 ID、sender/recipient。相同 ID 和完全相同参数重放复用原边界，即使默认当前时间已经改变；不同 ID/参数明确冲突，不重置边界。空身份集合合法。`notifications-status` 只读状态，不输出 Profile 或邮箱地址。

首启窗口固定为 activation_at 的上海自然日及之前 6 日：

- 无可靠日期为 unknown；只有未来日期也保持 unknown。
- 任一真实日期落在窗口内为 selected；此选择不会因为积压到第 8 天而消失。当前是否仍开放由 N0 的本次显式时钟判断。
- 窗口外已知日期为 not_recent；关闭首启回顾则 disabled。
- 当前列表日期、详情日期分别保留；最初选择证据和第一对矛盾证据保留，冲突传入 N0 的未知项。
- 首次实际生成事件及决策后为 generated，首次事件引用不被后续 update 改写。已有真实 update 也会消费尚未生成的回顾候选，避免之后再发首启提醒。静默历史基线 seq=0 后补齐近期日期，仍可以产生一次 activation_recent；失败不消费候选。

ProcessingOrigin 与发现 Origin 分开，不能从 304、profile、run_id 或 automatic=True 猜测：

| 处理来源 | 入口与效果 |
| --- | --- |
| live | `crawl_once` 显式传入，必须给实际有效 run。成功后可更新 live 基线、创建事件与决策。200 必须完整，304 只观察绑定的 200。 |
| offline | `import_page/process_response` 默认；更新既有业务 current，保持 live 基线、事件和投递资格。 |
| maintenance | CLI reparse 显式传入；允许历史/规则重解析，但不制造 live 更新提醒。 |

未启用时 live 不创建 observation/event/decision/outbox；仍登记真实列表日期。响应缺失的便利 `discover_page` 只登记通知，不能供首启日期证据。列表证据钩子再次校验 source、页面类型、200/正文、304 绑定及获取时间。列表回放不能用较旧证据覆盖较新的日期或改变候选。集合外的 new 还须证明身份确实由启用后的 live regular 列表首次插入；已有离线身份、bootstrap/historical/unknown 不因后来 regular 运行就变成新发布。

## live 比较与事件

notice_versions.raw_response_id 是该去重版本**首次**保存的原文；observation/event 使用本次实际正文响应。离线 current、最新传输 200 和 live 比较基线互不替代。

| 比较情况 | 行为 |
| --- | --- |
| 同 Parser、相同规范内容 | 更新实际观察证据，不追加事件/决策、不按新时钟改旧评估。重复 200 和绑定 304 一样。 |
| 同 Parser、不同内容 | 追加 update 和序号；A→B→A 两次变化，A 可复用原 version ID。 |
| Parser 不同、原字节相同 | 静默切换规则口径，不当作网站更新。 |
| Parser 和原字节都不同 | 事务外用当前 Parser 重新解析旧 live body，确认同口径内容不同才追加 update。 |
| 旧原文缺失、损坏或新 Parser 无法识别旧模板 | 静默建立新成功基线，保存有限 comparison_unknown；不宣称无变化。后续相同响应仍保留最近的未知代码、时间和具体前后原文/版本证据。 |
| 新获取/解析/通知登记失败 | 不推进成功版本、live 基线或序号；保留独立原文证据与失败诊断。 |

首次静默历史观察 seq=0；new、activation_recent、真实 update 各占一个序号，唯一 `(installation_id, document_id, event_seq)`。确认内容变化仍保存事件，即使 Action=IGNORE。没有邮件资格的事件 route=none；Digest 事件已登记资格但尚未分配邮件；immediate 同事务登记唯一 planned 意图。N2 消费这个冻结的选中路线，不再用当前 Profile 重路由。

规则升级的比较失败可能漏掉恰好同时发生的真实网站变化，这是保守取舍，不叫“未变化”。政策快照与当前代码版本不匹配时返回 notification_policy_outdated，不自动改 Profile 或对历史补发；政策更新/重评留 N4。存储快照不保证当前程序能执行任意旧引擎。

## 可组合事务接口

```python
prepare_notification(
    engine, raw_store, *, notice, body_response_id, observed_response_id,
    processing_origin, ingestion_run_id, evaluated_at, notice_parser=parse_notice,
) -> PreparedNotification

register_listing_evidence_in_transaction(
    connection, *, source_id, page, body_response_id, observed_response_id,
    processed_at, parser_version, processing_origin, ingestion_run_id,
    new_document_ids,
) -> None

commit_notification_in_transaction(connection, prepared, *, version_id)
    -> NotificationCommitResult
```

prepare 只读数据库；旧文读取/重新解析、facts/decision 全在业务事务外。next_digest_at 从冻结的上海日历和显式 evaluated_at 计算，不读取系统时钟。prepared 保存不可变 JSON token，包含未启用这一状态、政策/通道参数、成员日期、旧 observation、上次选中决策/路线/资格，以及新发现证据。

现有 save_notice_in_transaction 增加 processing_origin 与 prepared_notification。live 不允许省略准备结果；核对正文身份/hash/Parser/time、200/304 归属与 run。同一个 Connection 一起提交版本、current/成功/due、原文和资源处理标记、live 基线、事件、决策、选中路线、planned 意图及成员完成。复合外键保证版本属于同通知、selected decision/outbox 属于同事件；跨表响应归属由服务校验。

commit 不 begin/commit、不做文件或网络 I/O、不运行 Parser/N0。token 改变返回 notification_prepare_stale 并整体回滚；process_response 在事务外最多重新准备一次。SQL/归属/政策错误属于系统性失败，协调器终止，不伪装普通单篇无变化。失败登记也失败仍报告 failure_state_unavailable。原文证据继续独立提交，run 收尾失败不能抹去已经成功提交的通知事件。

历史详情的日期证据引用去重版本首次 200 body；它不是最后一次 live observed 的声明。live 详情证据明确区分 body 与 observed，获取时间保持原始值；处理和决策时间分别记录。事实快照省略正文全文，重放需结合事件引用的不可变版本/原文及相应 Parser/规则代码；新版 Parser 重解析旧文所得 previous facts 的版本由当前事件版本与上下文指明。

## 验证与下一步

全量测试包括原归档、缓存、Parser、入库、CLI、备份和采集回归；N1 新测试用真实 fixture、临时原文和 SQLite 验证去重、A→B→A、离线隔离、近期延期、绑定 304、规则升级、损坏原文、token 竞争、归属外键、迁移保留和事件/决策/意图故障回滚。事务监听器确认 raw 读取、Parser 与 N0 在业务事务外。这些故障注入及重开库验证不等于 N1 新的进程终止、断电或 SMTP 实验。

N2 已另行交付事务外纯文本渲染、短事务冻结、数量/字节上限、分片、幂等分配和积压补计划，见 [实现说明](mail-planning.md)。N1 planned 继续作为立即意图，实际邮件和成员在新增表中保存；N3 适配器已另行实现，N4 发送恢复尚未实现。
