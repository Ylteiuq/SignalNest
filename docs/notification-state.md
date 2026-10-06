# N1 通知状态与成功事务接口稿

2026-10-06。**本文是下一交付的接口稿，尚未实现。** 当前交付只实现 N0 的本地 Profile、事实提取、规则决策与预览；没有新增通知表、迁移、启用命令、邮件计划或发送能力。以下函数、表名与约束均为拟议接口，不可作为当前可调用能力。

本稿结合当前 `schema.py`、`ingestion.py`、`crawling.py` 与[通知决策调研](../research/notification-decisions.md)、[邮件设计](../research/email-delivery-design.md)。调研建议不是已有实现；最新决策使用 `PUSH_NOW / DIGEST / STORE_ONLY / IGNORE`，核对需求由独立 `needs_review` 表达，旧稿中的 `IMMEDIATE / REVIEW` 不作为新的 Action 枚举。

## 复用的事实与缺口

| 已有记录 | 继续承担的职责 | 不能替代的通知事实 |
| --- | --- | --- |
| `documents` | 稳定 `(source_id, source_document_id)`、首次发现时间及 origin、当前成功版本、详情 due | `last_success_at` 可能来自离线操作；`current_version_id` 也可被历史 reparse 改写，均不是 live 比较基线。 |
| `notice_versions` | 不可变规范内容与 Parser 版本；唯一 `(document_id, content_sha256, parser_version)` | A→B→A 复用旧 A，不代表没有第二次内容变化；首次 `raw_response_id` 也不是本次实际正文证据。 |
| `raw_responses` / `http_resources` | 独立获取证据、完整 200 原文、绑定 304、请求 profile 与最新传输基线 | 最新 200 可解析失败，不能成为业务成功或通知已登记的证明。 |
| `ingestion_runs` / `source_ingestion_state` | 发现来源、整次运行结果、完整列表覆盖证据、持久冷却 | 运行成功不是单篇成功的必要条件；详情已提交而运行收尾失败，不能撤销其内容事件。 |

目前 `ListEntry.published_date` 是 Parser 提取的真实站点列表日期，但 `discover_page_in_transaction` **没有把日期写入 documents**。N1 不凭 `discovered_at`、当前时钟、整数 ID、URL 或 run origin 补造发布日期，也不改 Parser 的提取规则。

初次启用仍要求来源已有一次可信完整列表扫描；它用于避免把尚未发现的整站旧历史都当新发布，不能证明详情全部成功或所有近期机会已经处理。详情 due 继续只来自 `documents.next_due_at`；通知模块不建立第二套网站复查计划。

## 显式处理来源

拟新增 `ProcessingOrigin = live | offline | maintenance`，与已有发现 `Origin = unknown | bootstrap | regular | historical` 分开：

- `live`：生产 HTTP 协调器的本次成功处理，提供真实 `ingestion_run_id` 和本次观察响应。200 可作为 body 与 observed 两者；304 的 observed 指向本次 304，body 指向其验证的完整 200。
- `offline`：本地 import-page；即使输入有 ETag、profile 或成功内容，也不建立 live 通知基线，不生成通知事件。
- `maintenance`：显式历史 reparse、规则维护。允许更新业务当前版本，独立 live 基线保持原样。

`automatic=True` 只检查缓存原文是否仍为最新匹配的完整正文，不表明其业务来源。生产协调器必须显式传 `live`；既有离线入口默认 `offline`，CLI reparse 显式传 `maintenance`。不得因存在 run_id、请求 profile 或 304 就自动推断 live。服务接入验证 live run 的 source、固定 Parser 版本和有效时间；来源标记由受信入口提供，不承诺抵挡直接篡改数据库的外部程序。

通知未启用时，三个来源都保留既有入库行为，**不创建 observation、event、decision 或投递意图**。启用后暂停发送只改变通道状态，live 事件与选中决策继续登记；暂停不重置启用边界或去重身份。

## 固定启用边界与真实日期证据

拟议启用预览在事务外校验 Profile、读取完整扫描事实、获取可用的已解析列表/详情日期证据，展示近期候选与未知数；它不下载所有历史详情。正式启用持现有实例锁，在短事务中保存一次 `installation_id`、`activation_at`、政策 revision 及当时已有的稳定来源身份集合。

成员唯一键为 `(installation_id, source_id, source_document_id)`，不用 `MAX(documents.id)`。当前 SQLite DDL 没有 `AUTOINCREMENT` 关键字；不为通知边界重建 documents，也不把删除后整数 ID 复用当新身份。空集合可以合法启用；同一启用操作重放复用结果，另一次 activate 拒绝悄悄重置集合/时钟。

首启回顾窗口固定为 activation_at 在上海的自然日及之前 6 个自然日。候选状态拟为 `unknown / selected / not_recent / disabled / generated`：

- 启用时无可靠日期为 unknown；不能先认定是旧历史并宣称候选完成。
- 启用预览或之后成功列表登记将真实 `ListPage` 与正文/观察响应 ID 一起传入通知模块，更新**已在集合中**成员的日期证据，不把之后新发现的身份补入启用集合。
- 证据最少保存日期、种类 `list_entry / notice_version`、来源身份、body/observed response ID、Parser 版本及相应版本 ID（详情来源才有）。列表成员以来源身份匹配，不按条目位置推断；位置可用于诊断。
- 详情先 live 成功且列表日期仍未知，可用其可靠站点日期补分类。selected 的固定选择不因积压拖到第 8 天而消失；是否仍开放由 N0 使用本次明确时间判断。
- 列表与详情日期矛盾时保留两份证据及冲突，不覆盖成假一致；传给 N0 的未知项必须可见。未来日期不证明已近期发布。
- generated 只在成功正文、相应事件与决策一起提交后标记。解析失败不标完成，重启后仍可恢复。

集合内首次 live 的近期候选至多一个 `activation_recent`，不同时生成 new。老历史首次成功只建比较基线；集合外第一次 live 是否为 new 还需可靠的首次发现来源依据，bootstrap/historical/unknown 不因当前运行 regular 就改写历史。N0 决策及 route 合成决定提醒方式；近期日期本身不构成邮件资格。

## 独立 live 比较与事件序号

每篇通知保存独立 observation：实际成功的 live 版本、实际 body response、最后 observed response、观察时间及事件序号。它不跟随 offline/maintenance current 指针，也不跟随解析失败的最新传输指针。

| 情况 | N1 处理 |
| --- | --- |
| 同 Parser、相同规范内容摘要（重复 200 或绑定 304） | 更新可追溯的 live 观察证据，不产生事件。仍使用实际 body response，不能替换为去重版本首次 raw。 |
| 同 Parser、内容不同 | 产生一次 update，序号递增；A→B→A 得到两次不同 update，即使 A 的 version_id 被复用。 |
| Parser 改版，原字节摘要相同 | 静默更新 live 基线与规则口径，不把纯规则变化当网站内容更新。 |
| Parser 与原字节都不同 | 在事务外使用当前 Parser 重解析旧 live body，再与新产物按同一规则比较；确认不同才生成 update。 |
| 旧原文丢失/损坏，或当前 Parser 无法解析旧模板 | 持久 `comparison_unknown` 及有限错误分类，静默推进到新成功 live 基线，避免重复错误事件；不声称已经证明无变化。可能漏掉恰好与规则升级同时发生的真实变化。 |
| 新正文获取或解析失败 | 不推进 live 基线、current 成功版本或事件序号；既有获取/失败诊断保留，后续仍可重试。 |

事件键拟为 `installation_id / source_id / source_document_id / event_seq`；数据库唯一 `(installation_id, document_id, event_seq)`。不以 raw hash、内容 hash、version ID、run ID 或时间戳作内容事件身份。首次静默历史观察 seq=0；首次 new/activation_recent 与真实 update 各占下一个序号。所有确认的真实变化都保存事件，即使本次 Action 是 IGNORE。

新事件保存 previous/current version 和 body/observed response，决策使用不可变内容及其事实；展示、渲染不读取当时可能已变化的 documents.current_version_id。曾登记邮件资格的机会条件变化由 N0 明确受支持规则处理；不扩大当前提取器覆盖，不用裸“取消”词猜全机会撤销。

本轮 N0 的 `EventContext` 是预览输入：`kind`、`notification_mode`、`previous_facts`、`previous_action`、`previous_effective_route`、`comparison_known`、`activation_date_conflict`，以及必须带明确时区的 `next_digest_at`。N1 根据持久启用状态、真实观察与上次选中决策构造这些值，不能相信 CLI 用户声明的“这次是 new”或“上次已提醒”。数据库 UTC 秒在调用 N0 时显式转换为带时区 datetime，`next_digest_at` 由冻结的政策与显式 evaluated_at 计算，不由纯函数读取系统时钟。

## 最小新增持久记录（拟议，不是当前 schema）

N1 拟新增 7 张窄表；不改 0001–0003，不新建通用任务、订阅或多渠道平台。N2 补冻结 payload 字段/行为；尝试记录及发送恢复留 N4。确切 SQL 名称可在 N1 实现时调整，但下面的恢复与约束语义必须保留。

| 表 | 字段与实际用途 | 必要约束 |
| --- | --- | --- |
| `notification_policy_revisions` | `decision.policy_manifest(profile)` 的不可变快照，含规范 Profile、事实规则、决策阈值/顺序、路线规则与全部版本；以 `policy_sha256` 校验，用于重放原输入而非读现配置 | policy 摘要唯一；不存凭据。Parser 版本属于内容版本，不冒充规则 revision；数据快照不承诺执行任意旧引擎，历史重放需保存的引擎版本可用。 |
| `notification_channel_state` | 单实例 primary：installation_id、source、activation_at、active policy revision、固定近期回顾政策、模式、recipient_key，以及后续计划所需的显式冻结 from/to 和发送暂停原因 | 单行主键；未启用没有该行；policy FK；激活身份、时间不可静默重置。地址不是新的收件人身份。 |
| `notification_activation_members` | 稳定身份、候选状态、有限日期/引用证据、冲突及 generated event 引用；恢复延期未成功候选 | 上述稳定身份唯一；FK installation；不依赖 document_id 水位。 |
| `notification_observations` | installation/document、live version/body/observed response、observed_at、event_seq、comparison_unknown 错误及时间 | 每 installation/document 唯一；version 的同 document 复合 FK；response 归属、200/304绑定由服务显式校验。 |
| `notification_events` | 独立序号/kind、previous/current version、body/observed response、发生时间、selected decision、最终 route、可空 outbox_id | seq 唯一；版本同 document；event key 唯一；route 仅 immediate/digest/none。 |
| `notification_decisions` | event/policy/evaluation_key、固定 evaluated_at、N0 Profile/content/facts/policy/input 摘要、有限事实/context 与原文短证据、Action、needs_review、reasons/rule IDs/unknowns、effective route 及路线理由 | `(event_id, policy_revision_id, evaluation_key)` 唯一；复合引用保证 selected decision 属于本事件；内容和证据不可原地覆盖。原始正文由版本/响应引用重建，不另复制到日志。 |
| `email_outbox`（N1 只建最小 planned 意图） | 稳定 delivery_key、immediate 类型、primary 收件人、选中 event/decision、固定政策/渲染 revision/Date 与地址、planned 状态；N2 后续冻结精确 MIME 和成员 | delivery key 唯一；单事件唯一分配；planned 不允许发送；无 SMTP attempts 或发送状态循环。 |

immediate 事件与 planned 意图在同成功事务创建；digest 事件已登记投递资格但允许尚未分配 outbox；none 没有意图。Action/needs_review/effective_route 都保存，**N2 不再读取当前 Profile 或模式重路由**。不存在 REVIEW Action 专用路线；待核对展示属于 N2 渲染。

跨表语义不能假装由一个普通 FK 全部证明。实现应使用现有 `(document_id, version_id)` 复合唯一键，新增必要的同事件 decision 引用，并在事务内校验 raw response 与目标/来源/完整 200、observed 304 绑定、route/意图一致性。event→decision→outbox 存在插入顺序：同一事务先创建事件、决策/意图，最后选中引用，任何异常整体回滚；临时未选中状态不允许由公共 service 返回成功。数据库不支持的跨表 CHECK 不伪造为约束，关键服务核验须有故障与错误归属测试。

## 接口与短事务

拟议 `notifications/service.py` 提供以下接口；类型字段以本轮实际 N0 契约为基础，不另建规则 DSL：

```python
# I/O、旧文重解析、事实提取与决策全部在事务外。
prepare_notification(
    engine, raw_store, *, notice, body_response_id, observed_response_id,
    processing_origin, ingestion_run_id, evaluated_at, next_digest_at,
) -> PreparedNotification

# 已解析 ListPage；在现有整页事务内登记启用成员真实日期证据。
register_listing_evidence_in_transaction(
    connection, *, source_id, page, body_response_id,
    observed_response_id, processed_at, processing_origin,
) -> None

# 仅数据库校验/写入；不 begin/commit，不读文件，不调用 Parser/N0。
commit_notification_in_transaction(
    connection, prepared, *, version_id,
) -> NotificationCommitResult
```

`PreparedNotification` 保存来源与原文/观察身份、NoticeContent 摘要/Parser、明确 evaluated_at、政策 token、通道/路线 token、启用成员分类 token、旧 observation 的 version/body/seq token，以及上次事件的 selected decision/effective_route/投递资格 token、比较结果、拟议事件 context 与已计算 N0 Decision。未启用/offline/maintenance 返回明确不登记原因，不能返回假“已通知”。`NotificationCommitResult` 返回 baseline 是否推进、event/decision/outbox ID 或明确静默原因；不代表邮件发送成功。

政策 token 核对 active revision 与 `policy_sha256`，通道/路线 token 另保存实际 installation/activation、notification_mode、initial_recent_review、Digest 日历参数和固定收件人等通道值；不引入通用配置版本系统。N0 的 policy_manifest 不包含具体 mode/next_digest_at，不能只检查 Profile 摘要就允许提交旧路线。evaluated_at 与由当时固定日历算出的 next_digest_at 一起留在 prepared context；提交核对原通道参数仍相同，不按提交时钟重算下一档，参数变化返回 stale 错误后重新准备。

上次选中决策与资格也是独立 token：显式重评可将旧事件从 none 改为 digest，而不改变 observation 的 version/body/seq。若只检查 observation，已准备的更新会误用“此前未提醒”并吞掉收紧条件的提醒。提交须核对上次 selected decision、effective_route 与资格登记状态仍一致；任何变化重新准备，不能在事务内临时重算规则。

在 `process_response` 的 Parser 成功之后、`save_notice` 的 `engine.begin()` **之前**调用 prepare。未来最小接入是在现有 `save_notice_in_transaction` 增加 `processing_origin` 与 `prepared_notification`：

1. 重读政策、通道/路线参数、成员、旧 live observation、上次选中决策/资格、来源/目标/响应，核验 prepare tokens、内容摘要与 Parser。live 且已激活但没有有效 prepared 必须拒绝；不能借省略参数绕过通知登记。
2. 原有幂等版本、current/成功/due 写入保持不变；得到 version ID。
3. 同 Connection 调用 `commit_notification_in_transaction`，提交基线、序号、事件、N0 决策、已选路线、必要 planned 意图/成员完成标记。
4. 原有 body/observed 处理状态和资源成功标记也在该事务；对外只在整组提交成功后返回。

列表证据钩子在 `discover_page_in_transaction` 同 Connection 中执行，因此条目、资源处理标记及成员日期不能只提交半组。获取证据与 raw 文件归档仍独立；网络、Parser、文件读取、邮件渲染或等待不进入业务事务。

token 不符返回有限 `notification_prepare_stale`，整组回滚；调用者在原有预算内重新准备，不能在短事务里临时读旧文件或重算策略。实例锁减少竞争，但不取代 token 校验。通知登记 SQL/归属错误属于系统性失败：协调器终止本次写入，不将其转换成普通“无变化”；原有失败登记不可用仍报 `failure_state_unavailable`。

高层 `record_response/discover_page/save_notice/process_response/import_page` 保留便利性。没有通知参数的既有离线调用保持当前效果；生产 `crawl_once` 的 `obtain` 显式提供 live 上下文。304 使用绑定 200 的 notice/body/fetched_at，本次 observed 响应及处理时钟分别保存。不能在 `process_response` 已成功返回之后追加事件。

## 决策锁定、迁移与后续交付

policy revision 冻结 Profile、规则/事实/引擎版本；`evaluation_key=initial` 的重复处理复用已保存的决策与原 evaluated_at，不能按本次新时钟改写。显式重评使用持久操作 ID 和新时刻，追加决策，不制造内容 update。

仅当前 live 候选且**尚无投递资格**允许切换选中决策。digest 已选中虽然 outbox_id 为空，也已经是投递资格；immediate planned、已分配 Digest、accepted/uncertain 都不能因 Profile 修改再发一封。已锁路线可做新规则预览；真实新内容仍有独立事件。模式或 Profile 更新默认只影响以后新事件，不全量扫描历史制造补发。

新增 Alembic revision 只创建所需新表，不改历史迁移，不为旧通知建立 observation/event/outbox，不把旧 origin=unknown 补成 regular。升级结果仍未启用；首次启用需明确预览、Profile 和固定边界，不存在迁移自动历史补邮件。验证 SQLite foreign_keys、循环引用插入顺序和升级保留现有数据；不以 create_all 代替迁移。

N1/N2 由同一实现 agent 顺序负责事务/schema：N1 原子登记与 planned 意图；N2 消费持久 effective_route，事务外渲染、短事务冻结邮件及精确成员。N3 可在输入为已冻结 bytes、输出有限 `accepted / retryable / uncertain / permanent` 的契约确定后并行实现 SMTP，适配器自身不重试；该结果不是收件箱送达证明。本轮不实现 N2/N3/N4，也不读取邮件凭据或发送网络请求。

## N1 必须补的离线验收（本轮未验证持久化）

| 研究场景 | 实现阶段与检查 |
| --- | --- |
| D6 / E1：历史与近期首启 | N0 可预览相应 context；N1 证明稳定集合、静默历史、一次 activation_recent、无 new 双事件。 |
| D7：延迟、未知列表日期、304 | N1 保留固定窗口；详情日期可补分类；重复列表/activate/304 不重复候选，失败不标 generated。 |
| D8：整数 ID 复用 | N1 以独立样例模拟删除/复用，验证成员按稳定身份而非 MAX(id)；不删除真实业务版本。 |
| D9 / E6：原子回滚与收尾失败 | 在真实临时 SQLite 对 event/decision/intent 各插入点故障注入，业务成功/baseline/资源标记一起回滚；已提交内容不被 run 收尾失败抹去。 |
| D10：重放/政策重评 | 相同 evaluation_key 复用原时刻/决策；显式操作 ID 新决策；不凭重评生成内容 update；投递资格只能登记一次。 |
| D11：已选路线后改 Profile | N1 锁定选中决策/路线；N2 再验证冻结 bytes、成员和 Message-ID 不变。 |
| D12 / E3：条件变化与 A→B→A | N0 只对已支持的事实提供透明预览；N1 两个 update/seq，回退复用 A version 但不复用旧事件。 |
| E2：重复 200/304 | 实际 archive/cache/Parser/SQLite 全链路，只有一次 new；304 body/observed 分开，重复内容不建第二事件。 |
| E4：Parser 改版 | 同 raw 静默；不同 raw 事务外旧文同口径重解析；缺失/损坏/旧模板失败持久 comparison_unknown。 |
| E5：离线污染隔离 | offline current B→A 后 live B，无 update，live baseline 不被改写。 |
| E15 及升级 | 旧库数据保留、默认未启用无邮件记录；暂停仍登记事件；复用现有跨进程写锁。 |

上述为下一批测试要求，不是本轮已执行实验。N0 的纯函数测试不能替代 N1 事务/迁移验收，也不能证明 SMTP 去重、崩溃或断电恢复。
