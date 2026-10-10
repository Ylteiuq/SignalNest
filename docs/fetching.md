# 有界 HTTP Fetcher

同步、串行获取器现已由 `crawl_once` 与 `signalnest crawl-once` 接入。Fetcher 本身不承担列表遍历或业务提交；构造/导入不会发送请求。默认测试使用模拟传输；后续已完成真实子进程恢复实验和少量实采，实际结果及限制见 [恢复验证记录](recovery-validation.md)。

## 交接与职责

来源许可由显式 `source_parser` 绑定，省略时兼容 UC；CS 使用 `cs-undergrad-notices`，有限 HTTPS/443 主机、列表路由、1074 栏目和同身份 JSP/静态详情跳转。协调器从 `[source].parser` 同时绑定 Fetcher 与 Parser，不根据 URL 猜来源。具体配置和独立实例边界见[CS 采集](cs-collection.md)。

`HttpFetcher` 在一次运行内复用一个 Client、物理请求计数、monotonic 预算和请求间隔。调用者提供已初始化的 Engine、RawStore、source_id 和 HttpSettings，并持有该实例的 writer_lock。可显式提供固定 RequestProfile；默认 User-Agent 来自配置、Accept 为 text/html、Accept-Encoding 为 identity。实际发送的这三个字段与 profile 完全一致。每个请求重新构造，清空 Cookie，不继承 Auth、环境代理或自动重定向。

`fetch(FetchTarget(...)) -> FetchResult` 区分 complete（完整、非空 200 HTML）、bodyless（304 或只有元数据的状态/拒绝）、transport_failure（连接/读取失败）、deferred（冷却或预算阻止继续）。这些均不是业务成功。结果保留按发送顺序排列的 FetchAttempt：实际开始/结束 UTC 时间，收到响应时的 ResponseInput，完整 bytes 或 None，本次选中的具体 304 candidate，有限错误代码及 not-before。没有收到响应就没有 ResponseInput，不伪造状态或 fetched_at。收到头的客户端 UTC 时间作为 fetched_at，与服务器 Date 无关。

Fetcher 只读缓存原文并持久化服务端冷却，不归档响应、不调用 Parser、不提交业务状态。`crawl_once` 按顺序将每个有元数据的 attempt 交给 record_response（包括跳转、重试和异常 304），仅传完整 bytes，304 传 candidate；正常 200/304 再调用 process_cached_response。传输失败没有 HTTP 状态，由协调器登记目标失败。不能只登记最终成功响应。公开入口还有 default_profile、make_client、FetchLimits；fetch(..., unconditional=True) 显式禁止条件头，不绕过冷却或运行预算。

`fetch(..., revalidate=True)` 在所有跳转、重试及修复请求中发送 `Cache-Control: no-cache`，要求重新验证表示；仍可发送与精确 URI/profile 对应的条件头。该字段不改变表示 profile，User-Agent、Accept 与 Accept-Encoding 始终与 RequestProfile 完全一致。协调器用此选项复核首页。

`repair(result)` 处理返回有效 304 后、业务读取前原文丢失或损坏的窗口。它只接受本实例最近一次成功 bodyless 304 的同一个结果对象，且只能调用一次；新的 fetch 会使旧结果失效。它在最终实际 URI 上继续无条件获取，复用原单资源期限、已消耗的重试/跳转额度以及运行请求预算，修复本身消耗一次重试。返回值只包含新增的物理请求，不能重复登记原结果。重试额度用尽返回 cache_repair_required；期限或请求额度用尽返回 deferred。不能在外层新建无限重试循环。

构造参数 `before_request(target, intent_at)` 是可选的发送前回调；协调器用短事务保存列表尝试意图。回调必须先结束事务，HTTP 随后才开始；回调失败直接传播，不发送、不增加请求计数。`intent_at` 是回调前的 UTC 意图时间，FetchAttempt.started_at 是回调返回后实际发送的起点，ResponseInput.fetched_at 是实际收到响应的时间。回调耗时也计入预算；若它耗尽期限，保留已提交的尝试意图，但不制造物理请求或响应证据。

| outcome | 正文与证据 | 后续动作 |
| --- | --- | --- |
| complete | 最后一条是非空完整 200、HTML 类型；不证明站点模板有效 | 登记所有响应，解析并提交业务 |
| bodyless，error_code=None | 校验可复用的 304、candidate 指定 200，无新 bytes | 登记绑定，处理原 200，保留原 fetched_at |
| bodyless，有错误 | 状态拒绝、非法跳转、不可修复 304 或正文规则拒绝 | 仅登记取得的元数据及有限失败，不解析 |
| transport_failure | 未收到头时无 metadata；读失败可有 metadata，永无部分 bytes | 登记已有证据及目标失败 |
| deferred | 可能已取得重试/跳转证据，也可能未发送；not_before_at 仅服务器冷却有值 | 登记已有证据，协调器决定受限/中断与待办 |

时间字段均为客户端 UTC 整数秒。有限 FetchCode 区分 connect/read/write/pool、证书错误、协议错误、正文超限/短读/空/type/encoding/header、跳转目标/循环/次数、异常 304、状态拒绝、冷却及资源/运行/请求预算。证书校验错误不重试；其他无法识别的 ConnectError 只有共同的有限额度，不根据异常字符串放宽 TLS。SQL 读取/冷却写入异常抛 IngestError（database_read_failed/cooldown_state_unavailable）；程序缺陷继续抛出，不能变成可忽略的资源失败。事件为 fetch_started/fetch_retried/fetch_finished，仅含来源、run 和阶段/错误代码。

## 边界与恢复

完整正文在内存中有界累积（默认 2 MiB），随后由既有 RawStore 一次发布。无需再造临时文件/对象存储接口；限制保留的正文，不承诺整个进程内存的硬上限。只接受 identity 实体，通过 iter_raw 读取、不执行解压；超过 Content-Length 上限提前拒绝，仍对实际流计数，校验短读/空正文/HTML 类型。部分 bytes 丢弃，元数据保持 unavailable。非 200/304、跳转与错误响应不保存诊断正文。

头字段长度受限，重复 singleton 头明确拒绝。使用 HTTPX 公开 response hook 留下实际接收时间及元数据；即使关闭自动跳转，HTTPX 构造 next_request 时仍可能拒绝坏 Location，此路径保留跳转证据并关闭响应，不调用其 next_request。Fetcher 不修复归档文件：若重新获取的 bytes 摘要与损坏文件相同，既有 RawStore 仍拒绝覆盖，调用者必须明确处理存储损坏，不能报告恢复成功。

唯一重试层在 Fetcher：默认两个额外尝试，传输暂时失败、408/500/502/503/504 与异常 304 完整回退共用；跳转另限三跳，次数跨重试累计。无条件 304 或收到 304 后基线文件/验证头失效，最多一次完整 GET 修复；第二次异常 304 失败。每跳仅允许本站 HTTPS 443、支持的列表路由或同身份详情新旧路由；不携带上一步验证器，重新查目标的精确 URI/profile。拒绝循环、跨域、降级、凭据、片段、栏目外目标与身份变化。

请求间隔覆盖所有发送及相邻 fetch 调用；退避为指数等待加非负 jitter。默认单资源 60 秒、整次实例 600 秒/120 个物理请求；等待/跳转/重试共享预算，各 HTTPX timeout 在发送前按剩余预算收紧。预算为协作期限：同步 DNS/一次阻塞 read 不能在循环内精确取消，不承诺硬墙钟终止。配置的间隔在本实例内执行；跨重启保留服务端冷却，不新增持久间隔字段。

Retry-After 支持非负 ASCII 秒数与 UTC HTTP-date。429 总是持久延期；缺失/无效值保守等待 30 分钟。带 Retry-After 的重试状态或跳转同样保存完整 not-before；最多在本次等 15 秒，超过等待/剩余预算即延期，不截短服务器要求。不可表示的巨大值将冷却置为 SQLite 最大时间，明确报告，须人工核实；冷却登记失败抛系统错误，不返回假装已登记的延期。每次发送前读取持久冷却；重启和更换同来源 URL 不能绕过。

`crawl_once` 负责运行记录、列表尝试/覆盖证明、每个 attempt 的证据登记、详情 due 与失败状态。Fetcher 不能证明列表完整扫描；首页 304 也不消除详情待办。记录后至处理前原文再损坏的竞争窗口由 process_cached_response 的 full_fetch_required 表达，协调器调用上述 repair 接口继续同一次有界获取。

本交付借鉴 research/ingestion-design 的表示键、单层重试、每跳重选与持久等待边界；研究探针不替代应用验证。不增加响应诊断正文、持久 request-start 字段、传输任务表或新的数据库迁移。规则版本/内容摘要仍由 Parser 负责，完全未改。MockTransport + 自定义原始流 + 假时钟连接真实 RawStore/缓存/SQLite，验证策略和关闭路径；后续真实 GET 保持 TLS 校验开启，SIGKILL 实验中的 HTTP 仍为模拟。没有真实 slow-drip、硬内存/墙钟限制或断电验证，少量 GET 也不能代表所有网络故障。
