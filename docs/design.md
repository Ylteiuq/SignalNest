# SignalNest 设计与离线持久化

项目展示名为 SignalNest，Python 包和命令均为 signalnest。项目骨架、纯 Parser、原始文件存储与离线入库/重新解析已完成。当前协调范围仅为已有 bytes 的处理，尚无 HTTP 获取或分页扫描协调。

## 模块边界

- `config.py`：读取并校验普通 TOML，不创建存储。
- `cli.py`：配置、显式初始化、本地 JSON 元数据/HTML 导入、按响应 ID 重新解析和退出码；帮助/配置校验不连接数据库。
- `schema.py`：同步 SQLAlchemy Core 表定义，导入只构建内存元数据，不建表。
- `storage.py`：惰性 engine、SQLite 连接设置与 Alembic 初始化/升级。
- `rawstore.py`：原始 bytes 的摘要、原子发布与验证读取；不访问数据库。构造 RawStore 不操作文件。
- `ingestion.py`：响应证据登记、整页通知发现、版本与成功状态提交、失败登记及离线流程。业务入口不依赖 CLI，不获取 HTTP。
- `migrations/`：随 Python 包安装的固定历史迁移，升级与工作目录无关。
- `contracts.py`：不可变 Pydantic 输入/输出契约、规范化 JSON 与内容摘要，无 I/O。
- `fetching.py`：创建同步 HTTPX Client，不主动请求；不接触 Parser 或数据库。
- `parsing.py`：`html_tree`、`parse_list`、`parse_notice` 纯函数，按 UTF-8 解码并显式使用 `html.parser`；不联网、不访问存储，不配置 logger 或运行任务。
- `eventlog.py`：标准库 JSON 日志，CLI 显式启用；不在导入时配置日志。

HTTPX Client 保留 TLS 校验，显式设置 connect/read/write/pool 超时，限制为单连接，不自动跟随重定向或继承环境代理；后续获取器负责验证目标 URL/重定向与获取响应。依据 [HTTPX Client 文档](https://www.python-httpx.org/api/) 配置，尚无完整重试或采集流程。Beautiful Soup [显式指定后端](https://www.crummy.com/software/BeautifulSoup/bs4/doc/#specifying-the-parser-to-use)，避免本机装有 lxml 时改变结果；本源 fixture 为 UTF-8，解码失败必须报告错误。

已实现的站点函数签名为 `parse_list(page: PageInput) -> ListPage` 和 `parse_notice(page: PageInput) -> ParsedNotice`。页面输入包含内容字节与最终页面 URL，以最终 URL 解析相对地址；函数只返回结构化数据，不访问网络或数据库。

未来 HTTP 协调层负责 request_interval、列表遍历、详情补抓和重试，复用已有归档/业务入口。没有占位采集命令、空 coordinator 或插件工厂。不在数据库事务内执行文件 I/O、Parser 或等待 HTTP。

## 最小数据契约

- `PageInput`：原始页面 bytes 与 page_url；不包含 Client/Session 等运行依赖。
- `ListEntry` / `ListPage`：源内稳定身份、绝对详情 URL、标题、日期、条目集合与下一页 URL。source_id 由协调层从配置注入；本栏目空列表必须调查，不能默认为正常。
- `ParsedNotice` / `NoticeContent`：稳定身份、页面 URL、parser_version，以及标题、日期、正文 HTML/文本、链接/图片/附件引用。HTML 保留结构但未做展示安全处理，不能直接当作可信网页渲染。图片型正文允许 body_text 为空，Parser 必须验证正文有可见文字或有效图片引用。
- `AttachmentReference`：名称、URL、可选源文件 ID（本站建议 owner:wbfileid）和访问状态。默认 not_checked，仅有证据时标为 manual_required；不下载或绕过验证码。
- `RawResponseReference`：URL、获取时间（UTC 秒）、HTTP 状态、白名单头和正文路径/摘要。路径与摘要必须成对出现，路径限定为相对的 raw/<sha256>.bin，304 禁止正文引用；契约不操作磁盘，由 RawStore 验证实际文件。
- `ResponseInput`：已取得原文的明确来源，包含页面类型、source_id、URL、fetched_at、状态码和白名单头；详情必须给出源内身份、列表禁止给出目标。没有获取时间默认值，也不猜文件名。

契约拒绝未知字段、空必要字段、相对/非 HTTP(S)/含凭据 URL；引用集合使用 tuple，避免内容被随意修改。Parser 负责相对 URL 解析、路由身份提取和本站必要字段校验，不能只靠类型校验判断解析成功。

`NoticeContent.canonical_json()` 固定排序 JSON 键，使用 UTF-8 与紧凑序列化，保留正文内引用顺序；`content_sha256()` 对此编码求摘要。哈希包含标题、日期、正文和引用，排除来源身份、获取 URL/时间、Parser 版本及附件访问状态（后者是运行状态，不是公告更新）。数据库唯一键另含 parser_version。该序列化本身不等于 HTML 语义规范化；Parser 在序列化前按下面的固定规则处理 HTML。不同但任意语义等价的 HTML 不保证同一摘要。规则变化更新 Parser 版本。

## 日志

CLI 在参数解析后才配置 signalnest logger，帮助和包导入不触发配置。除配置/初始化事件，还记录 raw_archived、response_recorded、page_processed、processing_failed。每次命令有 run_id；字段白名单为 time、level、event 及 source_id/run_id/document_id/response_id/stage/error_code。身份和阶段/错误仅接受有限长度安全字符；原始消息、args、异常栈、任意 extra、URL、网页和配置不进入日志。未知普通日志转成 unstructured_log，不回显内容。服务函数通过 signalnest logger 发出事件，调用者自行显式配置日志。

stdout 为命令结果，stderr 为事件日志和必要的用户错误诊断。日志只输出标准流，无文件 handler、后台线程或全局 root logger 改动。字段白名单不是秘密识别器，调用者仍不得把密钥塞进身份字段。

## 已实现的数据模型

| 表 | 用途与关键约束 |
| --- | --- |
| `documents` | 稳定身份、详情 URL、发现时标题、发现/尝试/成功时间、最新处理状态、错误代码、下次到期时间，以及最近成功版本指针。`(source_id, source_document_id)` 唯一。 |
| `raw_responses` | 导入的响应证据：URL、状态、原获取时间、白名单头、原始路径/摘要、page_type、详情 document_id、最近处理时间/错误。列表无目标；详情绑定已发现通知。304 无正文。旧记录类型/目标可空。 |
| `notice_versions` | 通知的规范化内容 JSON、标题、发布日期、解析时间、解析器版本、规范化内容 SHA-256 和原始响应外键。`(document_id, content_sha256, parser_version)` 唯一。 |

`alembic_version` 是迁移工具的版本表，不是业务表。没有用户、搜索、推荐、发送或反馈表；信息源来自 TOML，暂不建立只有静态配置用途的 source 表。

本站 `source_document_id` 约定为 `栏目ID:文章ID`（如 `1517:128231`），Parser 已统一新旧链接。标题、URL 和内容摘要不替代稳定身份。原始字节摘要与规范化内容摘要分别计算，不能混用。

版本唯一键包含解析器版本：相同原文可用新 Parser 重解析，即使规范化内容恰好相同，也保留新解析器产物。相同 Parser 与相同内容不会产生重复版本。内容从 A 变为 B 后又回到 A 时，可复用 A 并更新 `current_version_id`；不能按最大版本 ID 判断当前正文。版本中的原始响应引用指向首次生成该产物的证据，并非每次复查事件；当前不提供完整处理尝试审计表。

`current_version_id` 通过复合外键保证属于该通知；为支持该双向引用，当前元数据的外键排序使用 `use_alter`，但 SQLite 初始迁移显式内联创建所有外键，不依赖不受支持的 ALTER ADD CONSTRAINT。`schema.py` 不用于 `create_all`。

## 状态与事务

- `discovered`：仅发现列表条目，尚未尝试详情处理；成功时间和版本指针均为空。
- `processed`：最近一次处理成功；必须有尝试时间、成功时间和属于该通知的版本指针，无错误代码。
- `failed`：最近一次处理失败；必须有尝试时间和错误代码。首次失败没有成功时间/版本；复查失败保留已有成功时间与版本。

所有操作时间为 UTC Unix 秒整数；发布日期为页面展示的日历日期，不从 HTTP Last-Modified 推导。`next_due_at` 可空，支持后续重启后补抓/复查；此阶段没有调度器。错误字段存简短分类代码，不存网页正文或可能含秘密的完整异常文本。

处理顺序已实现：先归档并独立登记响应证据，事务结束后读取/验证原文并解析，再用一个短事务提交幂等版本、通知成功状态和响应处理摘要；失败单独短事务记录错误，不覆盖成功产物。数据库约束验证引用、必要字段和幂等性；Parser 验证站点内容，RawStore 验证原始路径和摘要。列表全部条目及该响应处理摘要也在同一事务内提交。

raw_responses.last_attempt_at 是最近一次处理完成/失败的时间，last_error_code 为该尝试的分类：两者为空表示只有证据、尚无已提交处理结果；时间非空且错误为空表示处理成功；错误非空表示失败。不是 HTTP 获取时间、成功游标或完整尝试审计。新的导入独立建立证据行，即使元数据和 bytes 完全相同；重新解析只更新原响应的处理摘要，不改 fetched_at、URL、头或正文引用。原始文件去重与响应行策略独立。

处理时间使用当前真实处理时刻，或调用者显式给出的 UTC 秒。不得早于 fetched_at、该响应或目标通知的最近处理时间，拒绝时不回退状态。首次 discovered_at 为第一次本地登记时间，重复发现保留。每次成功处理显式设置 current_version_id，包括重新解析历史原文；版本 parsed_at 保留首次创建该产物的时间，最新尝试时间另存通知和响应。

按照 [SQLAlchemy SQLite 事务说明](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html) 配置连接；通过 [Alembic 共享连接接口](https://alembic.sqlalchemy.org/en/latest/cookbook.html#sharing-a-connection-across-one-or-more-programmatic-migration-commands) 执行升级。每个应用连接开启 SQLite 外键检查，等待锁上限 5 秒，显式 BEGIN 使 DDL 和版本号更新一起回滚；不添加 WAL、后台队列或连接并发机制。迁移由 CLI 显式运行，导入不迁移。生产升级应先备份，历史迁移冻结后通过新增 revision 演进；使用 SQLite batch migration 时需评估版本表与通知表的双向外键，不能盲目采纳自动生成代码。

## 原始文件与跨介质一致性

初始化建立 `data_dir/raw/`。RawStore 计算 SHA-256，正文固定为 `raw/<sha256>.bin` 相对于 data_dir；相同 bytes 复用文件，规范化 NoticeContent 另存数据库。图片、附件仅引用，不下载。304 不创建文件；真实 200 空 bytes 可归档为空原文证据，但不会通过 PageInput/Parser 成功验证。

发布协议（macOS/Linux POSIX）：在已打开的 raw 目录内以独占方式创建 `.tmp-<随机ID>`，完整写入/flush/fsync，重新读取验证摘要，再用同目录 `link` 原子地建立最终文件名。选择独占硬链接发布，避免 replace/rename 覆盖并发出现的损坏文件；遇到已存在的最终文件必须验证，并重新 fsync 文件/目录，不能覆盖或忽略上次目录同步失败。发布后 fsync 目录，移除本次临时名称并再次 fsync 目录；全部成功返回后才允许数据库引用。常规失败尽力清理本次临时文件；被强制终止时可能留下 `.tmp-*`。

读取也验证严格的路径/摘要格式、普通文件类型和实际 bytes 摘要。通过逐级目录 FD 与 O_NOFOLLOW 打开路径，拒绝路径组件及 blob 的符号链接，用 O_NONBLOCK 避免 FIFO 阻塞；不沿不可信相对路径或网页 URL 访问磁盘。配置加载先 resolve 既有路径别名；写入器在这个已解析的绝对路径上仍拒绝后续符号链接。data_dir 属于本地受信任的单用户进程，不能防御有目录写权限的其他进程恶意替换/移动存储；本阶段不支持 Windows 或多进程业务协调。

对正常进程中断，最终名称只暴露已写完文件；SQLite 未提交的业务事务不会推进成功版本。fsync 文件/目录是操作系统层的持久化请求，不能把原子命名等同于断电保障：文件系统、磁盘缓存和硬件仍影响结果，也未使用 macOS 专门的 F_FULLFSYNC。文件与 SQLite 没有共同事务。文件先成功、响应登记失败时允许出现孤立文件；后续读取再次校验，不信任单独存在的摘要记录。

恢复操作：文件发布前失败时不登记正文引用；响应证据登记成功而解析失败时，保留 response_id，修复规则后 `reparse`；版本/成功提交失败先回滚，再尝试独立失败事务。若失败事务也失败，抛 failure_state_unavailable，调用者不能宣称已登记恢复状态。突发中断可能只留下未处理证据或待处理通知，重新打开库后通过 documents.status 和 raw_responses.last_attempt_at 查询并继续处理。

暂不自动删除孤立文件或临时文件。停止所有写入并备份后，对照 `SELECT DISTINCT body_path FROM raw_responses WHERE body_path IS NOT NULL` 与 raw 目录中 `<64位小写摘要>.bin`；未引用的是孤立候选，`.tmp-*` 是未完成发布候选。先验证/调查再决定人工清理，不以文件修改时间或是否存在版本作为删除依据：解析失败和列表证据也需要保留。

0002 在 raw_responses 上增量 ADD COLUMN，避免启用外键时重建被 notice_versions 引用的表。page_type/document_id 的检查和外键保持目标明确；未迁移的旧记录保持 NULL，不凭 URL 自动赋予身份。最近处理时间/错误用于列表和详情的恢复诊断。本次没有新业务表，不修改 0001。

## 可复用的离线业务入口

| 入口 | 实际行为 |
| --- | --- |
| `open_initialized_engine(database)` | SQLite mode=rw 打开已有最新版库，缺失/旧迁移直接失败，不创建或升级。 |
| `RawStore.archive(bytes)` / `read(path, sha256)` | 完整归档或验证读取，不访问数据库。 |
| `record_response(engine, raw_store, evidence, bytes_or_none)` | 验证已发现的目标身份，发布文件后单独提交响应证据；返回 response_id，不表示解析成功。 |
| `discover_page(engine, source_id, ListPage, discovered_at)` | 每页全部条目事务性 upsert；最新列表 URL/标题覆盖，首次发现/处理状态/成功版本/错误不重置。返回条目处理数，不是新增通知数。 |
| `save_notice(engine, response_id, ParsedNotice, processed_at)` | 校验证据/目标/Parser 身份和最终 URL，按版本唯一键写入或复用，并与成功状态一起提交；低层入口要求调用者已验证原文并成功解析。 |
| `process_response(engine, raw_store, response_id, processed_at)` | 复用原获取证据，验证文件、完整解析、写入成功或失败状态；用于 reparse，也供 HTTP 层处理刚登记的响应。 |
| `import_page(engine, raw_store, ResponseInput, bytes_or_none, processed_at)` | 上述归档/登记/处理的组合；304 仅返回 evidence_only。 |

process_response 可注入纯 notice_parser/list_parser 函数以支持规则升级和离线测试，无工厂/插件配置。默认仍使用本站固定 Parser。每次调用执行完整解析，原文相同不会跳过失败或新版本规则。两个入口类型、获取时间与通知身份均显式提供；CLI 不等于又发生一次 HTTP 请求。

预期错误统一为 IngestError，提供 code/stage/response_id/document_id；Parser 分类加 `parse_` 前缀，文件错误为 raw_path_invalid/raw_missing/raw_digest_mismatch/raw_not_regular/raw_io_or_unsafe_path。身份不一致为 identity_mismatch；数据库读/写失败为 database_read_failed/database_write_failed；失败登记失败为 failure_state_unavailable。未发现目标、类型未知、无正文、非 200、来源不符或时间倒退都有明确代码。只存代码，不存整段异常。未经预期的程序缺陷仍抛出原异常，不转换为解析成功或空结果；不能据此假定失败状态已经保存。

业务函数中的程序缺陷直接传播；CLI 最后边界将非预期异常转为非零退出和无异常正文的 unexpected_error 诊断，明确不确认失败登记已完成。

没有完整的处理尝试审计：通知/响应只保留最近状态，同内容版本指向首次生成它的证据。历史原文 reparse 成功会切换当前版本，不自动判断哪个 fetched_at 更新；未来批量规则重算需先定义历史产物与当前指针的更新策略。

## 对调研建议的调整

列表返回 304 不代表没有待补抓详情或到期复查任务。列表检查与详情处理队列必须独立推进。

“遇到已知通知就停止分页”不能作为完整性的保证；置顶、顺序变化和分页移动会使重叠页策略漏报。普通运行的覆盖范围必须明确，周期完整核对及停机恢复策略将在可靠采集阶段实现。

普通运行关注增量及待处理任务；首次历史导入建立历史基线，不能默认把所有旧通知当作新提醒。未来通知策略需独立决定历史导入、首次发现和正文更新是否触发邮件，本阶段没有发送行为。

## 已完成的 WHU Parser

`PARSER_VERSION = "whu-student-notices-v1"`。选择器、提取语义与规范化规则固定在代码中；修改任何影响结果的规则时更新版本。不读取运行中可变的规则配置，也没有插件系统或原始摘要短路。每次调用重新构建 HTML 树、完整验证，不因前一次成功/失败改变后续结果。

列表只读取唯一的 `div.list_txt > ul.am-list`，逐条验证其 li / a / span / i。条目失败时整页抛错，item_index 标记位置；不返回部分成功。空列表异常；不硬编码页长 25。保留原始条目顺序和置前旧日期通知，不去重或按日期过滤。下一页来自列表所在区域的 `.page .p_pages .p_next a`；明确禁用标记或没有下一页时返回 None，活动“下页”链接损坏时抛错，不当作末页。

文章身份限本源 `uc.whu.edu.cn` 的 `/info/1517/<正整数>.htm` 和 `/2022/show.jsp?wbtreeid=1517&wbnewsid=<正整数>`。查询顺序与无关参数不影响身份；必要参数缺失、空值、重复参数（即使重复值相同）、新路由查询身份冲突、其他栏目/主机/路径都明确失败。旧路由 urltype 若提供，必须为 news.NewsContentUrl。普通正文外链不受文章身份规则限制。

详情确认外层 `.news_show`、直接标题区 `.title_nei` 及其中唯一 b / i，并在该区域读取唯一 `#vsb_content > .v_news_content`。fixture 内有嵌套的 .news_show，所以不要求正文直接属于最外层。标题需有文字/数字，日期严格来自“时间：YYYY-MM-DD”可见字段并校验日历有效性。缺少、重复或不识别的结构不会用全页标题/其他日期猜测。

附件只取通知区 `.fj > ul` 的每个 li 中唯一 a，记录可见名称及绝对 URL。WHU download.jsp 从唯一 owner 与 wbfileid 提取 `owner:wbfileid`；不强制 wbfileid 长度为 32（真实样本有不同长度），但要求字母/数字。普通附件链接可保留而无源文件标识。access 一律 not_checked，看到链接不证明可下载或需要验证码，不抓取文件。

## 固定规范化规则与范围

1. 只对选出的正文子树操作；标题/日期和附件另行提取。页外导航、footer、统计及 HTTP 元数据不进入正文。正文内移除 script/style/noscript/template、nav/footer/form、已知 fj/title_nei/p_pages 页面组件，以及 nattach 数字 ID、dynclicks ID、clickTimes 统计节点；不按“错误”“登录”“已下载”等词语删除普通通知文字。
2. 移除明确 hidden / aria-hidden=true / 内联 display:none 或 visibility:hidden 的节点。正文根容器若隐藏，视为无意义。注释、脚本只有字符串或空白/空 br/hr 都不能证明有效正文。明确 width=height=1 的图片不计作通知图片；其他有效 HTTP(S) 图片引用可单独构成图片型正文，不假装已检查图片内容。
3. 普通文本节点将空白连续段（含换行、tab、NBSP）折叠为一个空格，清除块元素边界的格式空白和块间空白节点；保留行内元素之间的必要空格，不给 Word 拆分的 span 人工加分隔符，以免将 2026 拆成 202 6。pre/code 子树的 HTML 文本不折叠。正文文本按块/换行分行，表格单元格用 tab 分隔；HTML 保留段落、表格、强调等结构。
4. 正文 a.href / img.src 用 PageInput 的最终 page_url 解析并改写为绝对 HTTP(S) URL；输出链接/图片引用按正文顺序排列，不限制外链域名，不解释全页 base 标签。空链接、片段锚点、mailto/javascript/tel/data 等不作为网页引用；移除它们的 href 而保留锚文本。显式但格式无效的 HTTP(S) 链接抛 invalid_field。图片缺失/无效 src 则失败，不悄悄丢失媒体。
5. 删除动态事件属性和 VSB 图片辅助属性（vurl/orisrc/vsbhref/vheight/vwidth），以及未支持的 srcset（仅保留 src 回退）；保留普通属性、内联样式与数字/日期。Beautiful Soup 确定性序列化属性，再由 NoticeContent 做稳定 JSON 与 SHA-256。不会任意删除 CSS、font/span 或标点，也不合并不同但语义等价的任意树。

清洗后必须有文字/数字或有效非装饰图片引用；附件的存在不能替空正文证明成功。HTML 保留与这里的规范化**不是展示安全清洗**，iframe 或其他未专门处理属性仍可能存在；未来界面必须采用独立的可信展示方案。没有 OCR、附件正文提取、CSS 布局计算，也未验证图片 URL 指向的实际字节。若站点模板/编码/附件参数形式改变，应失败并用新 fixture 更新规则及版本。

## 错误分类

`ParseError.code` 是 ParseErrorCode 字符串枚举，`field` 为有限字段名，`item_index` 为可选的零基条目位置。消息不包含输入值、URL、整页 HTML 或任意异常文本；仅捕获预期的 URL/日期/结构校验错误，程序缺陷仍抛出原异常。

| code | 含义 |
| --- | --- |
| invalid_utf8 | 不可按 UTF-8 严格解码 |
| empty_page | 非空字节却只有空白；空字节由 PageInput 拒绝 |
| missing_structure | 必要结构缺失或存在多个候选，不能可靠选择 |
| invalid_field | 标题、日期、URL、附件参数等必要字段无效 |
| unsupported_identity | 非本源/本栏目/支持路由，或身份形式不受支持 |
| ambiguous_identity | 身份参数重复或与路径身份冲突（包括附件标识） |
| empty_list | 本来源列表容器没有条目 |
| meaningless_body | 清洗后没有有意义文字或有效图片引用 |

Parser 不检查 HTTP 200/403/304，PageInput 未扩展状态码。200 错误页因缺少本站结构/字段失败，而非因全页含某个关键词失败。

## 调研启示与后续持久化契约

[可靠性报告](../research/changedetection-io-reliability.md) 和 [SignalNest 决策报告](../research/changedetection-io-signalnest-decisions.md) 提醒必须分开三个概念：原始字节摘要只标识取得的采集证据；规范化内容摘要标识解析产物；parser_version 标识规则。原文相同不证明上次解析成功。报告中关于上游崩溃窗口/raw-checksum shortcut 的后果是源码推断，尚未运行验证；本实现借鉴风险边界，不宣称复现上游缺陷，也未复制上游代码。

离线业务层已遵守，后续 HTTP 协调层也须遵守：

- 原始响应入档仅表示取得采集证据，不推进成功游标或成功状态。
- Parser 返回表示解析与字段验证成功；协调层仍须将版本及成功状态提交后才算持久化成功。
- 解析失败记录分类并保留最近成功版本；同一原文以后仍可重试，规则升级后仍可重新解析。
- 如将来跳过重复处理，必须有对应原文和 parser_version 的已成功解析结果，不能只看原始摘要。内容摘要不含 parser_version；现有数据库唯一键另含版本，所以新版规则产物即使摘要相同也可保留。

这里已实现原文写入与离线短事务协调，未实现成功游标、任务队列或通知 outbox，没有增加业务表。调研中的状态/调度/outbox 建议不会自动成为本阶段的 schema 要求。

## 下一阶段：单次可靠 HTTP 采集

1. 按新研究结果确定分页覆盖与完成条件，区分普通增量、首次历史导入及停机恢复，不改成遇已知条目即停止。
2. 实现同步串行获取：记录实际客户端获取时间，限制响应体大小和重定向、目标校验、超时/限速/有限重试，网络等待在事务外；通过 ResponseInput/record_response 登记证据。
3. 明确 ETag/Last-Modified 条件请求与可信基线：304 无新正文，只有已存在且验证可读的旧原文才可重解析；无可用基线需按获取策略处理，不能生成空基线。
4. 将已登记响应交给 process_response，独立从 SQLite 查询待处理/失败详情与未来到期复查；列表 304 不阻止这些工作。不用内存列表或最大版本 ID 作为成功事实。
5. 用离线 HTTP MockTransport 验证分页移动、304/基线丢失、限速和传输失败；另安排真实终止进程/重启实验，不能把当前故障注入等同于该实验。之后进入调度与 Email，最后真实运行观察。
