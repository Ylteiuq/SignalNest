# 单次采集协调器

`crawling.crawl_once(settings, CrawlOptions(...)) -> CrawlSummary` 连接真实 Fetcher、RawStore、缓存、Parser 和 SQLite 服务。它持有整个运行的 POSIX `writer_lock`，要求数据库已显式 `storage-init` 至最新迁移，自行关闭 Client/Engine。构造或导入模块、CLI 帮助和配置检查不获取锁或发送 HTTP。

协调器实现先用 MockTransport、真实临时文件/SQLite、假时钟及 SQL 故障注入验证；后续已补 5 项真实子进程 SIGKILL/重启实验及三轮有限实采，见 [恢复验证记录](recovery-validation.md)。默认测试保持离线，没有断电实验。Parser 提取、分页规则、版本 `whu-student-notices-v2`、内容摘要、schema 和历史迁移均未改。

## 入口与预算

```sh
signalnest crawl-once --config signalnest.toml --scan limited --max-pages 1 --max-details 2
signalnest crawl-once --config signalnest.toml --scan full --max-pages 64 --max-details 20
```

这些命令会发送真实 HTTP；离线导入仍使用 `import-page` / `reparse`。CLI 的 `--scan {full,limited}` 必填，不按通知是否已知或日期决定覆盖模式。

| 选项 | 默认与含义 |
| --- | --- |
| `--max-pages` | full 默认 64、limited 默认 2；遍历页上限，首页复核另需请求额度 |
| `--max-details` | 20；独立详情逻辑尝试上限，可为 0 |
| `--max-requests` | 120；所有实际 GET，含跳转、重试、首页复核和完整获取回退 |
| `--run-seconds` | 600；整次共享 monotonic 预算，解析/归档/提交耗时也消耗它 |
| `--resource-seconds` | 60；资源的重试、跳转、等待与修复共用 |
| `--max-body-bytes` | 2 MiB；每份完整 200 正文上限 |

HTTP 超时、请求间隔与 User-Agent 来自 TOML。唯一重试层在 Fetcher；没有协调器外层重试循环。预算在操作之间检查，不能强制打断同步 DNS/read、Parser、文件 I/O 或 SQLite 提交；因此不承诺严格墙钟上限。

## 运行、响应与事务

获取实例锁后创建运行记录；第一次完整扫描成功前 origin 为 bootstrap，之后为 regular。旧发现来源 unknown 不补猜，明确历史导入仍与常规发现区分；任何入口都不发送邮件。

每次从配置首页开始，没有分页续扫游标。Fetcher 的 `before_request(target, intent_at)` 在发送前用短事务登记列表尝试意图；回调失败不发送请求，事务不包住 HTTP。极端情况下预算在回调期间耗尽，可能有尝试意图而未真正发送；实际请求数另由 `physical_requests` 记录，`FetchAttempt.started_at` 在回调返回后记录实际发送起点。有效响应证据、整页登记和完整扫描时间仍各自独立。

每个有响应元数据的 attempt 按顺序登记，包括跳转和重试失败；未收到响应不伪造状态或 fetched_at。原文文件发布及响应证据事务先完成，随后在事务外读取、验证和解析，再调用现有短事务提交列表/版本与资源处理标记。业务成功不是 HTTP 200 或 Parser 返回的同义词。

正常 304 绑定请求前选定的具体 200，自动处理该原文；原始 fetched_at、正文文件和获取证据保持。最新完整 200 即使解析失败也优先重处理，不能回退旧成功正文。登记后到业务处理前文件再失效时，`process_cached_response` 返回 full_fetch_required；协调器最多调用一次 `fetcher.repair(result)`，复用同一个资源截止时间、额外尝试和运行额度，且保留已有 304。没有生成空基线、伪造原获取时间或重新开启一套重试预算。完整获取的新 bytes 若仍对应损坏的已有摘要文件，RawStore 拒绝覆盖，明确终止而不假称修复成功。

## 完整覆盖的实际证据

`ScanCompletion` 保留旧声明兼容；生产调用另外提供原响应绑定、有序通知/引用行及首页复核记录，提交时验证这些行已登记。协调器在构造它前实际验证：

1. 每次请求目标来自上页 next，维护跨页请求/最终 URI 集合，包含重定向路径；重复目标或落回已访问路径失败。
2. 首页 current=1，后续页连续增加；total 和非末页活动尾页 URL 保持一致，不从文件名计算。
3. 每页 Parser 验证成功且全部条目事务已提交；明确末页具有禁用 next/last 证据，实际末页请求路径覆盖最初尾页目标。
4. full 模式重新请求首页，发送 `Cache-Control: no-cache`，可携带精确 profile 对应的条件头。复核也必须解析、整页提交成功，并与最初首页的最终 URI 和完整 `ListPage` 相等：本站有序身份及未适配引用目标/href/位置、标题、日期、详情 URL、next 及分页证据均比较。

全部通过后，用独立短事务记录 coverage=complete，将完整响应链、实际 next、分页、有序混合行及独立首页复核保存到 coverage_evidence，并推进来源完整扫描/bootstrap 成功时间；两者一起提交。跨页漂移检测发生在页提交之后，已登记的有效条目保留；它们不证明 complete。首页变化即使带来新通知也会发现入库，但覆盖仍 interrupted。复核不等于站点原子快照，也不能识别其他页在扫描期间发生后又恢复的变化。

limited 始终 coverage=limited，即使预算内看见末页；无普通失败时 run=succeeded。full 被页上限截断是 limited 覆盖、interrupted 运行，仍可独立处理详情。循环、跳页、总数/尾页漂移、首页变化或普通列表失败为 interrupted 覆盖；请求/时间预算限制为 limited 覆盖，均不推进完整扫描时间。覆盖与运行结果独立，列表 complete 后详情失败可以得到 partial_failure/failed 的运行结果。

## 独立详情与恢复

运行开始事务中，将本来源 processed 且 next_due_at=NULL 的旧记录明确设为 started_at，加入首次联网复查，计入 legacy_rechecks_scheduled；不清除成功状态/版本，不以发布日期推断历史来源。这一步即使本次遇到冷却也保留，下次可继续。

列表 304、全已知页或普通列表中断后，仍从 SQLite 查询发现、失败和到期复查的详情。到期门控后按 F/H/R 分组，保留 12/4/4、借槽并轮转；组内 next_due_at 非空时用它，否则用 discovered_at，ID 仅用于平局。前景仅来自本次成功登记的可见前两页且从未成功处理的身份，不能按发布日期或发现来源猜测；后续旧页归历史，失败但有成功基线归复查。每篇本次最多一次，批次最多 max_details。

默认成功根据站点发布日期相对本次上海日历日期的年龄，分为 24 小时/7 天/30 天复查；失败按有限错误类别延期并服从 not-before。唯一持久 due 仍是 documents.next_due_at。`notice_due/error_due` 在 Parser 后、事务外计算，交给既有业务服务，成功/due/资源标记一起提交。CrawlOptions 显式固定间隔覆盖保持支持，默认使用 `[runtime]`；离线入口不自动应用政策。详见 [运行政策](runtime-policy.md)。

普通 HTTP/解析/身份校验失败可靠登记后继续其他详情；首次失败没有成功版本，复查失败保留最近成功版本。服务端持久冷却及全局请求/运行预算停止所有进一步网络请求，不因为换详情 URI 绕过。文件归档、数据库或失败登记异常属于系统错误，终止运行，不转换成“没有变化”。

预期系统错误或用户中断尝试独立收尾；收尾也不能登记时抛 run_finalization_unavailable，不能声称已保存恢复状态。程序缺陷继续传播，CLI 最终边界返回非零且不输出任意异常文本；强制终止或缺陷可遗留 running，下一次持锁 start_run 将其记为 interrupted。待办从持久状态重建，不依赖内存游标。完整覆盖已提交后运行失败，不撤销该覆盖事实。

## 摘要与退出码

CrawlSummary 输出本次运行事实，未新增持久计数表：

| 字段 | 精确语义 |
| --- | --- |
| pages_committed / scanned_entries | 已提交遍历页及其全部条目行；重叠行重复计数，不含首页复核 |
| new_documents | 锁内本来源 documents 总数增量；包含首页漂移复核实际发现的新身份 |
| home_rechecked | 成功处理过首页复核；true 本身不证明 unchanged/complete，比较前也可能耗尽预算 |
| details_attempted / succeeded / failed | 逻辑详情尝试与最终结果；普通目标拒绝可计尝试却没有物理请求，冷却/全局预算在发送前阻止则不计详情失败 |
| scanned_references / new_references / remaining_unadapted_references | 未适配行观察数 / 本轮新增唯一引用 / 来源待适配引用总数；不抓外部正文，不进入详情待办 |
| physical_requests | 真实发送的全部 GET，不等于尝试意图或条目数 |
| remaining_due | 收尾时数据库中当前到期/可处理的详情数 |
| remaining_unprocessed | discovered/failed 的总数，含未到重试时间和保留旧成功版本的复查失败；不是无成功版本数 |
| coverage / result | 列表覆盖与整次运行结果，原因分别为 coverage_error_code / error_code |

succeeded 退出 0；partial_failure、failed、interrupted 或系统错误退出 1；配置/预算/命令用法错误退出 2；用户中断退出 130。stderr 含结构化事件及有限错误诊断，stdout 为 JSON 摘要；系统异常不能生成可信完整摘要时不输出伪成功结果。

真实子进程终止/重启验证及少量低频实采已完成；实际 Vary 变化导致完整回退，预算中断后原库可以继续处理。结果和限制见 [恢复验证记录](recovery-validation.md)。`scheduled-run` 使用配置预算复用本入口，外部 timer 触发，不在 Python 中启动后台调度；部署/日志/备份见 [运维文档](operations.md)。当前没有邮件、分页并发、任务队列、LLM 或 Agent 能力。

有效但未适配的列表行由整页事务登记，不阻断本站通知及后续分页；坏字段/身份/分页仍整页失败。完整列表覆盖与外链正文准备分别表达；同一外链重复出现的观察行照计，但按 source/完整 URI 去重。规则与恢复见[未适配引用](list-references.md)。
