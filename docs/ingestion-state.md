# 单次采集的持久状态与事务接口

本节点只实现离线状态/证据接口，不发送 HTTP、决定分页遍历、重试或复查周期。研究探针不是这些生产接口的验证结果。继续单实例、串行写入；没有任务队列、租约或 outbox。

## 最小数据

- `documents` 继续是详情的唯一业务成功与到期真相：current_version_id、status、last_success_at、next_due_at。增加首次 discovery_origin（unknown/bootstrap/regular/historical）与 first_discovery_run_id；只在首次登记赋值，旧记录 unknown，不按发布日期猜测。
- `raw_responses` 复用来源、实际 requested_url、final_url、获取时间、头、原文引用、目标与最近处理错误。增加 body_state（unknown/complete/unavailable）、resource_id、validated_response_id、Vary/Cache-Control/Content-Encoding。旧响应保持 unknown、无 resource/binding；可以显式 reparse，不凭已有 ETag 推测缓存资格。
- `http_resources` 按 source + 实际 GET URI + 请求 profile 摘要唯一；保存固定白名单 profile、最新完整 200 指针与最后成功处理的原文/Parser/时间。最新完整 200 即使解析失败也替换传输指针；不回退到更旧的成功原文。没有第二套详情 due 或业务成功状态。
- `source_ingestion_state` 保存列表尝试、完整 200/可绑定 304 证据、整页登记三个时间；完整扫描成功与首次历史扫描完成时间独立；not_before_at 保存服务端冷却。列表时间指该来源任意列表页，不能表示首页或整个扫描成功。不预设列表/扫描调度周期。
- `ingestion_runs` 保存来源、起止时间、发现 origin、固定 Parser 版本、coverage（pending/complete/limited/interrupted）与整次 result（running/succeeded/partial_failure/failed/interrupted），覆盖与运行错误分开。启动下一次同来源运行时将遗留 running 标为 interrupted；已提交的 complete 覆盖保留。

分页 visited 集合、预期页号/总数、首页复核指纹、请求预算、monotonic deadline、重试计数、正文 buffer 只属于一次运行。本节点不持久化页码续扫游标或完整待办队列。重启查询 documents 的待处理/失败/到期事实，以及资源的最新原文与成功处理标记差异。

## 条件验证

RequestProfile 固定 User-Agent、Accept、Accept-Encoding=identity；拒绝控制字符，没有 Cookie/Auth。URI 保留完整查询顺序和参数，不按文章身份合并。每跳的实际请求分别登记；requested/final 不同的响应不具有本节点的条件复用资格。

完整非空 200、明确 profile、已发布正文引用、已知支持的 Vary（User-Agent/Accept/Accept-Encoding）、无 no-store、未压缩及有效验证器，才可选作条件候选。private/no-cache 不禁止条件验证。本节点不实现通用 HTTP 缓存；未知 Vary、Vary:*、不支持的编码、无效验证器或表示配置变化要求完整获取。新的不合格完整 200 也阻止退回旧基线。200 原始头保持不变；304 的缺省头可沿用，明确冲突的验证头/Vary 或 no-store 保守要求完整获取，不合并更新验证器。

资源另保存 blocked_by_response_id：不兼容的已绑定 304 会持久化阻断后续条件选择，重启也不会继续发送旧验证器；新的完整 200 清除阻断。该指针只影响传输复用，不删除归档、不冒充业务处理成功。

`select_cache_candidate` 在请求前选择具体候选并读取校验原文。`record_response(..., candidate=...)` 只将 304 绑定到该候选，校验最新指针、source、实际 URI、profile、目标、200 状态与完整引用；不从收到 304 后的“最新响应”猜测候选。无候选 304 仍可保存无绑定证据。304 永无正文，处理时再次读取绑定的 200 并验证摘要。

`process_cached_response` 用最新且匹配的原文处理 200/绑定 304；缺失/损坏或绑定不再可用返回 full_fetch_required，HTTP 层再执行受预算约束的完整获取。解析失败仍明确抛错，不能作为无变化或回退理由。对详情还检查目标最新完整原文，防止不同 URI/profile 的旧缓存将当前内容回退。显式 `process_response`/CLI reparse 是历史操作，可以有意选择旧原文；不会将旧原文提升为传输基线。

非完整读取、失败或重定向允许 content=None，只存元数据；显式 body_state=unavailable 禁止附带部分 bytes，complete 必须有 bytes。提供 bytes 的调用者须已确认读取完整，存储服务不能从 HTML 自行证明 HTTP 完整性。带 profile 的空 200 拒绝发布，须以 unavailable 元数据登记失败；无 profile 的既有离线空文件仍可保留解析失败证据，但没有缓存资格。

## 短事务

原文归档/读取与 Parser 在业务事务外。响应登记和传输指针独立短事务，不能证明业务成功。

`discover_page_in_transaction`、`save_notice_in_transaction` 使用调用者已有 Connection/事务，不提交或回滚。前者提交全部条目、原响应处理状态、资源成功处理标记及来源登记时间；后者提交幂等版本、当前版本、成功状态、调用者给出的 next_due_at 和资源标记。未提供 due 的显式离线操作保持原值，HTTP 协调器必须给出本次成功/失败的策略时间，不由这些函数推导周期。高层 discover_page/save_notice/process_response/import_page 保留便利封装。

`record_failure_in_transaction(connection, IngestError, processed_at, failure_due_at=...)` 可将元数据读取失败、冷却和必要运行状态组合提交；由 response_id 验证并取得目标，拒绝错误代码/目标/时间，不清成功指针。`record_failure(engine, ...)` 是独立短事务封装；失败登记本身失败返回 failure_state_unavailable。Connection 入口出现任何错误都必须由调用者回滚，不能捕获后继续提交半个操作。

304 的观察 response_id 与 body_response_id 分开：业务使用 200 的 final_url/原获取时间，版本指向真实 200；本次处理时间与 304 证据保留，不创建假正文或新获取时间。

来源/运行状态的 `*_in_transaction` 接口可组合进同一短事务。完整扫描提交必须由协调器先证明：从首页按实际 next 连续覆盖全部页，URI 链无循环/跳页、总页数一致、明确末页、每页事务已提交、首页复核一致。接口验证提供的分页序列/复核声明，但不替协调器证明网络链或数据库每页登记；不是遍历器。limited/interrupted 不推进完整扫描或 bootstrap 完成；扫描 complete 与运行 partial_failure 可以同时成立。

| 状态入口 | 运行事实 |
| --- | --- |
| start_run_in_transaction | 显式 origin 与固定 parser_version，恢复同来源遗留 running；业务提交拒绝混用规则版本。日志 run_id 与这里的 ingestion_run_id 独立，CLI 离线日志 UUID 不凭空建立运行行。 |
| record_list_attempt_in_transaction | 网络发送前记录尝试；读取本地文件不调用它。 |
| set_cooldown_in_transaction / read_source_state | 保存完整绝对 UTC not-before，不能缩短已有冷却；重启后读取。 |
| record_coverage_in_transaction | complete 必须提供 ScanCompletion：有序全部分页证据与显式 unchanged 首页复核声明；limited/interrupted 保留旧完整成功时间。仅是受信协调器的提交接口。 |
| finish_run_in_transaction | 独立记录 succeeded/partial_failure/failed/interrupted；未完成的 coverage 记 interrupted，已 complete 保持。 |
| pending_documents / pending_resources | 数据库查询重建待处理/失败/到期，以及最新原文或 Parser 版本尚未成功处理的资源；没有持久队列、批次配额或重试循环。 |

下一轮顺序：持有 writer_lock → start_run → attempt/读取冷却 → 选择候选 → 有界 HTTP（事务外，每跳重新选择）→ record_response → process_cached_response 或明确 record_failure → 所有列表事务及复核成立后记录 coverage → 独立处理详情 → finish_run。304/首页成功不跳过数据库待办。

## 写入保护与迁移

`writer_lock(database)` 用解析后数据库旁的固定 `.lock` 文件及 POSIX flock 非阻塞锁。锁文件不删除；存在不代表占用。数据库符号链接别名解析到同一锁，数据库硬链接和锁文件符号链接拒绝，避免路径别名绕过保护。初始化/升级、CLI import-page/reparse 统一遵守，库调用者覆盖整个写入运行使用此锁。帮助/config-check 不获取锁。支持 macOS/Linux POSIX，Windows 未支持；外部不遵守 advisory lock 的程序不受此锁保护。

0003 只用 ADD COLUMN 与新表，不重建被 notice_versions 引用且启用外键的 raw_responses/documents；0001/0002 冻结。旧数据/引用不变，未知字段保持 unknown/NULL，不自动填充缓存或历史来源。原文与 SQLite 仍不是跨介质事务；正常异常回滚和进程退出释放锁，不等于断电耐久性保证。
