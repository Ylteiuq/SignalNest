# changedetection.io 经验映射到 SignalNest

调研日期：2026-09-29（Asia/Shanghai）  
依据固定版本：[`v0.60.7` / `593e9cc48c2b475dacbc3dbdebe2f5136bef8efa`](https://github.com/dgtlmoon/changedetection.io/commit/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa)。这里的“上游事实”来自该版本源码；“决定”是结合 SignalNest 单用户、1～3 个 HTTP 可取校园源、同步 HTTPX 和 SQLite 的建议。调用链见 [架构](changedetection-io-architecture.md)，故障细节见 [可靠性报告](changedetection-io-reliability.md)。

## 现在借鉴

| SignalNest 决定 | 上游事实 / 依据 | 对当前实现的落点 |
| --- | --- | --- |
| 保持普通 HTTP 为默认通路；每个请求有有限的 connect/read/整体预算、明确的 User-Agent、受限 redirect，并保留 TLS 证书校验。 | 上游默认 HTTP 简单直接；但该版本 HTTP 调用显式 `verify=False`，Session 在单次 fetch 中创建，redirect 由应用自行跟随。[Requests fetcher](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/content_fetchers/requests.py#L58-L125) | 适配现有 HTTPX；不要照搬关闭 TLS 验证。把 redirect 限制和超时作为源抓取配置的一部分。 |
| 对短暂传输故障做有限、可观察的请求内重试；把 HTTP 状态错误和解析错误记录为本次失败，不立即伪装成成功。针对 429 读取 Retry-After 并设上限。 | 上游 Retry 覆盖 connect/read，而 `status=0` 不重试 HTTP 状态；worker 失败后靠正常周期，不做失败退避。[Retry 配置](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/content_fetchers/requests.py#L58-L79) · [失败后 next due](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/flask_app.py#L1499-L1559) | 设置较小次数和带上限退避+jitter；避免把 403/429/5xx 错页写成成功通知。持久化错误类型、HTTP 状态和下一次可尝试时间。 |
| SQLite 中明确分开 `last_attempt_at`、`last_success_at`、`next_due_at`、连续失败/错误摘要；失败不能覆盖最近成功 notice version。启动时从数据库重建 due 工作，不依赖内存队列恢复。 | 上游 `last_checked` 是尝试开始；`last_error` 单独存在；队列在内存，重启后重新计算是否到期，不 replay 每次遗漏的周期。[worker](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/worker.py#L145-L263) · [重启加载和 ticker](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/store/__init__.py#L194-L252) | 让队列只是唤醒/加速层，SQLite 的 due 状态才是事实来源。单进程运行可先用简单扫描/claim，不引入 Redis 或 broker。 |
| 一个通知抓取周期写入时，以短数据库事务一起提交成功尝试、抽取到的 notice/version 和“待发提醒”记录；通知发送器处理后标记完成。 | 上游先提交 watch JSON，再写快照/index，之后才投内存通知队列；通知发送失败不会重入。[worker 写入顺序](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/worker.py#L535-L603) · [通知 runner](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/flask_app.py#L1453-L1524) | 未来启用个性化通知时使用本地 transactional outbox；发送可能“至少一次”，用稳定 event key 去重，避免声称 exactly-once。 |
| 保存原始响应摘要与抽取后内容指纹时，只有解析/验证成功后才推进“最近成功版本”标记；记录 selector/parser 版本，配置改变时可重跑同一份 raw snapshot。 | 上游 raw checksum 先于抽取写入；processed md5/filter hash 后续才随 watch metadata 写入。该顺序存在失败后 shortcut 漏处理风险。[processor 写 checksum 与处理](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/processors/text_json_diff/processor.py#L419-L467) | 把 `raw_response` / checksum 当采集证据；解析成功才提交 source cursor 或 notice 状态。对 HTTP 200 空页、模板错误页和关键字段缺失定义源级验证。 |
| 调度器持久化的是下一次到期时间和任务 claim/lease，而不是长期保留一个易丢队列；失败周期与正常周期分开计算。 | 上游每秒 tick，以 `last_checked` + interval 判定；慢任务被运行态 UUID 阻止重叠，失败没有专用 backoff，重启后只按状态重新评估。[Ticker](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/flask_app.py#L1564-L1705) | 1～3 个站点先由单一协调器按 `next_due_at` 取工作；claim 设租期/超时，进程重启后租期过期可重领。先保证同一 source 不并行，不建线程池。 |

## 以后有真实需求再考虑

| 机制 | 触发条件 | 为什么现在不需要 |
| --- | --- | --- |
| Playwright/Selenium browser fetcher | HTTP 返回的页面确实缺少公告正文，且确认内容由 JavaScript 加载。 | WHU 样本已能由普通 HTTP 获取；浏览器带来独立运行时、超时和资源回收问题。上游把浏览器作为可选 fetcher，并有额外 CDP timeout 与 close 生命周期。[选择入口](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/content_fetchers/__init__.py#L177-L191) · [Playwright 生命周期](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/content_fetchers/playwright.py#L249-L321) |
| 多 worker 公平调度、每站点并发额度/令牌桶 | 站点数、任务耗时或源端限频确实要求并发。 | 上游多个 worker 与进程内优先队列是大规模 watch 用法的一部分；对 1～3 个低频源，串行通常更容易推理。其代理间隔不等于全局域名限速。[worker pool](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/worker_pool.py#L39-L97) · [Ticker 的 proxy 间隔](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/flask_app.py#L1517-L1538) |
| 外部任务 broker 或多进程协调 | 多实例部署、任务量或可用性目标证明单进程不够。 | 上游 file datastore 明确按单 app 实例工作；直接多进程共享目录不安全。[file datastore 注释](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/store/file_saving_datastore.py#L35-L47) |
| 大型通用 watcher 插件系统、请求 browser-step 交互 DSL | 用户开始维护多类异构源或需要浏览器交互。 | 当前只有少数校园官网，已有站点配置/解析边界更轻。不要因为上游提供这些能力就预先抽象。 |
| 快照归档、去重内容寻址或数据库外存储 | 历史体积成为实际磁盘问题。 | SQLite 可先保存 notice/version 元数据及必要原文；按使用量设计归档。上游的 JSON、raw checksum、快照和历史索引多文件组合不提供跨文件一致性。[history 文件写入](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/model/Watch.py#L709-L783) |

## 不采用或不照搬

- 不照搬上游 `verify=False`。这会绕过证书校验；SignalNest 用 HTTPX 默认 TLS 验证，只有经过具体源证据支持才讨论例外。[上游参数](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/content_fetchers/requests.py#L96-L103)
- 不用 `last_checked` 兼任上次成功状态，不用原始 response checksum 单独充当解析游标。上游写入顺序展示了失败后 raw-shortcut 的风险；应用应保留最近成功版本，并让“抓到但未解析成功”保持可重试。[checksum 顺序](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/processors/text_json_diff/processor.py#L464-L559)
- 不照搬多文件 watch JSON + raw checksum + snapshot + history index 的持久化组合。SignalNest 已采用 SQLAlchemy/SQLite：有关联的状态尽量以事务提交，原文文件存储若保留则先写完并校验，再短事务登记引用；需要补偿清理孤儿文件。
- 不假设 queue item、成功记录和通知天然原子，不宣称 exactly-once。历史版本与提醒状态分别建模；未来通知通过 outbox、稳定事件标识和发送结果状态管理。
- 不照搬完整 changedetection.io 或增加框架依赖。它是 Apache License 2.0；若未来复制或改写其具体代码并分发，应附许可证、标记修改文件、保留适用版权/专利/商标/归属声明；若上游分发含 NOTICE 还须带上适用声明。依赖许可证需要另行逐项检查，仓库许可证不覆盖依赖。当前建议只借鉴工程行为，不复用代码。[固定版本 LICENSE](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/LICENSE#L590-L607) · [再分发条件](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/LICENSE#L727-L791)

## 对下一阶段设计的直接影响

下一阶段可基于现有 SQLite 模型先定义每个 source 的 `last_attempt_at`、`last_success_at`、`next_due_at` 与错误摘要，随后实现单进程、串行、重启可重建的轮询协调器。一次轮询应将成功抓取记录、parser 输出、notice/version 与 source 的成功游标一起提交；失败只更新 attempt/error/下次重试时间，不推进成功游标。收到 429 时服从有上限的 Retry-After；传输异常少量请求内重试，随后进入由持久 `next_due_at` 控制的调度。通知功能启用时再增加同事务 outbox。当前不需要浏览器后端、外部 broker、通用抓取插件框架。
