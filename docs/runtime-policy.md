# 处理政策与只读诊断

实现依据为 [运行策略研究](../research/runtime-policy.md)，没有修改研究或站点 Parser。政策在 `[runtime]` 固定为本次运行的配置快照，不是插件系统；外部 timer 负责触发，Python 仍只执行一次有界串行采集。没有新的表、任务队列、租约或通知发送状态。

## 待办分组与公平性

先按 `documents.next_due_at` 筛选到期候选。未到期失败不能凭优先级重新入选，成功但 due=NULL 的旧记录由现有首次联网登记纳入；同一 run 每个身份最多尝试一次。

| 组 | 互斥判定 | 默认保留逻辑尝试 |
| --- | --- | --- |
| foreground / F | 本次成功登记的可见第 1、2 页身份，且 last_success_at=NULL | 12 |
| history / H | 其余 last_success_at=NULL 的到期候选 | 4 |
| recheck / R | last_success_at 非空的到期候选，包括保留旧成功版本的失败记录 | 4 |

前景仅表示当前入口可见且尚未成功处理，不表示刚发布。置顶旧文也可进入 F，后续旧页进入 H；首页 304 仍解析绑定原文，坏页不产生前景集合。集合每次重建、不持久化，不从 discovery_origin 或日期猜测。

组内按 `(next_due_at 或 discovered_at, id)` 排序。先保留各组槽，不足释放，空槽按 F→H→R 借用；执行按 F/H/R 一条一条轮转，跳过空组。总数不超过 max_details，小于保留槽总数的批次也按轮转截断，例如三组都有工作时上限 1/2/3 对应 F/FH/FHR。失败消耗一个槽，不在本 run 立即补选失败身份。

只有足够批额、网络/时间预算且没有全局冷却或系统故障时，三组饱和的完整批次才提供 12/4/4。扫描先消耗共用预算，分配不等于实际服务；不为凑容量另建 Fetcher 或绕过冷却。

## 成功与失败 due

年龄是本次处理时间在 Asia/Shanghai 的日历日期减去站点发布日期。成功 due 为**本次处理 UTC 秒 + 间隔**，与版本、current 指针及资源处理标记同一事务提交；有效 304 也按现在处理时间计算，不用原 200 fetched_at 起算。

| 年龄 | 默认间隔 |
| --- | --- |
| 0–7 天 | 24 小时 |
| 8–30 天 | 7 天 |
| 超过 30 天 | 30 天 |
| 未来日期 | 按近期档；成功后计入 future_dates，不删除有效通知 |

失败默认：短暂传输、可重试 5xx 及其他未专门分类错误 30 分钟；401/403/TLS 校验 24 小时；Parser、身份、目标/正文类型/模板相关校验 6 小时；404/410 为 7 天。没有撤回/删除推断。详情失败 due 至少为来源 not-before，服务端冷却依然控制全部请求。系统性数据库、原文存储或失败登记错误继续明确终止，不能仅延期后忽略。

配置见 [示例](../config.example.toml)：配额、日期边界、三个成功间隔及四个失败间隔均校验。旧 TOML 可以省略整个 runtime，获得这些新默认值。库 `CrawlOptions.success_recheck_seconds/failure_retry_seconds` 的显式固定间隔覆盖仍支持，默认 None 使用配置政策；CLI 使用配置政策。手动 `crawl-once` 的预算仍来自已有参数；`scheduled-run --mode regular/full` 则分别从 runtime.regular/full 读取预算。

`process_response/process_cached_response` 新增窄的 notice_due/error_due 回调：输入已解析通知或有限错误和处理时间，事务外计算，结果在既有成功/失败事务中持久化。未提供回调时离线入库/历史 reparse 的行为不变，仍可直接传 next_due_at/failure_due_at。

政策变更不自动重写旧 due。显式 `apply-recheck-policy --config ...` 持同一实例锁，在短事务中用当前成功版本日期与 last_success_at 重算，仅 processed 成功记录参与；取旧 due 与新 due 较早者，NULL 则立即纳入，不推迟逾期、不清除失败退避，不改原文/版本/获取时间。命令可重复，先按运维文档备份。

## 摘要、日志与 status

采集摘要保留原字段，新增 `detail_groups` 的 allocated/attempted/succeeded/failed/unserved/remaining_due/oldest_overdue_seconds、foreground_pages、future_dates 和 remaining_first_processing。分配/尝试是本次选择时的组，剩余 due 按收尾时事实重新分组；历史积压总数以 last_success_at=NULL 计算，包含尚未到期的首次失败。

每组 `detail_group_finished` 事件只增加有限非负整数统计，仍不输出 URL、HTML、配置或异常正文。运行中被终止可能没有这些收尾事件；业务待办从 SQLite 重建。计数不新增数据库字段，日志保留策略见 [运维文档](operations.md)。

`status --config ...` / `inspect_status(settings, at=...)` 输出 JSON，只读 SQLite mode=ro + query_only，在一致读事务校验迁移 head。它不联网、不建库、不迁移、不读 raw、不获取锁、不修复 running 行或 due。正常读到诊断警告退出 0，数据库不可用退出 1，配置错误退出 2；警告不表示已发送告警。

报告区分列表尝试、有效响应、条目登记与完整扫描，以及首次积压、成功基线复查、未安排的成功 due、冷却、最近 10 个运行和未收尾运行。last_list_response_at 是已登记完整 200 或兼容绑定 304 的传输事实，仍可能在 Parser 中失败；last_list_registered_at 才表示条目事务成功，两者都不证明完整覆盖。提示阈值为完整扫描超过 26 小时、首次最老等待超过 72 小时、复查逾期超过 24 小时，以及最近三次已结束运行预算中止。由于未持久化 scan_mode，不宣称这三次都是普通运行；status 也不能还原 F/H 分组或证明 running 行对应活进程，须结合 journal/systemd/锁实际情况诊断。

## 触发边界

交付一个 Linux/systemd 252+ 模板：普通每半小时 limited 两页、20 详情、64 请求；每日 01:15 full、64 页、0 详情、96 请求；共享 600 秒协作预算。模板不在当前机器启用，部署级 15 分钟终止保护仍需在目标机验证。

首次/至少 24 小时停机恢复先人工执行一次有界 full，再恢复普通 timer；没有从有限旧 run 猜测 full 尝试或增加自动重试事实。错过普通周期不排队重放，日常 full 至多补一次；重叠由同一实例锁明确拒绝，不自动高频重跑。具体安装、独立日志保留、一致备份和恢复校验见 [运维文档](operations.md)。

半小时 × 20 详情的理论上限为 960 次逻辑尝试/日；三组持续饱和且完整服务时历史和复查各 192 次/日。改成每小时 × 20 则只有 480，若 600 篇均每天复查显然不足（480 < 600），更未计新文/失败。实际还有扫描、重试/回退、预算与冷却成本，不把配额当成功容量，也不拉未到期任务填空槽。status 展示积压事实而不猜外部 timer 的实际运行频率。

这些数值是保守起步政策，不是源站公布的负载许可或已测得的长期容量。后续先观察一周，再决定是否调整；本交付不进行新的实采、不增加 Email 或后台 Python 调度器。
