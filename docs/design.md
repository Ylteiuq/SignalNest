# SignalNest 第一阶段设计

三个节点均已完成：安装/配置/CLI、本地存储/迁移、数据契约/模块边界/日志。第一阶段为项目骨架；原始文件写入与可靠采集流程留到后续阶段。项目展示名为 SignalNest，Python 包和命令均为 signalnest。

## 模块边界

- `config.py`：读取并校验普通 TOML，不创建存储。
- `cli.py`：参数、显式初始化入口和退出码；查看帮助/校验配置不连接数据库。
- `schema.py`：同步 SQLAlchemy Core 表定义，导入只构建内存元数据，不建表。
- `storage.py`：惰性 engine、SQLite 连接设置与 Alembic 初始化/升级。
- `migrations/`：随 Python 包安装的固定历史迁移，升级与工作目录无关。
- `contracts.py`：不可变 Pydantic 输入/输出契约、规范化 JSON 与内容摘要，无 I/O。
- `fetching.py`：创建同步 HTTPX Client，不主动请求；不接触 Parser 或数据库。
- `parsing.py`：`html_tree(PageInput)` 纯函数，按 UTF-8 解码并显式使用 `html.parser`；不联网、不访问存储。站点字段解析尚未实现。
- `eventlog.py`：标准库 JSON 日志，CLI 显式启用；不在导入时配置日志。

HTTPX Client 保留 TLS 校验，显式设置 connect/read/write/pool 超时，限制为单连接，不自动跟随重定向或继承环境代理；后续获取器负责验证目标 URL/重定向与获取响应。依据 [HTTPX Client 文档](https://www.python-httpx.org/api/) 配置，尚无完整重试或采集流程。Beautiful Soup [显式指定后端](https://www.crummy.com/software/BeautifulSoup/bs4/doc/#specifying-the-parser-to-use)，避免本机装有 lxml 时改变结果；本源 fixture 为 UTF-8，解码失败必须报告错误。

后续站点函数签名约定为 `parse_list(page: PageInput) -> ListPage` 和 `parse_notice(page: PageInput) -> ParsedNotice`。页面输入包含内容字节与最终页面 URL，以最终 URL 解析相对地址；函数只返回结构化数据，不访问网络或数据库。

协调层未来负责 request_interval、列表遍历、详情补抓、重试、原始文件归档和短事务的顺序。现在用此文档明确职责，不预建空 coordinator 或插件工厂，也没有占位采集命令。不在数据库事务内等待 HTTP 请求。

## 最小数据契约

- `PageInput`：原始页面 bytes 与 page_url；不包含 Client/Session 等运行依赖。
- `ListEntry` / `ListPage`：源内稳定身份、绝对详情 URL、标题、日期、条目集合与下一页 URL。source_id 由协调层从配置注入；本栏目空列表必须调查，不能默认为正常。
- `ParsedNotice` / `NoticeContent`：稳定身份、页面 URL、parser_version，以及标题、日期、正文 HTML/文本、链接/图片/附件引用。HTML 保留结构但未做展示安全处理，不能直接当作可信网页渲染。图片型正文允许 body_text 为空，未来 Parser 仍须验证正文有意义。
- `AttachmentReference`：名称、URL、可选源文件 ID（本站建议 owner:wbfileid）和访问状态。默认 not_checked，仅有证据时标为 manual_required；不下载或绕过验证码。
- `RawResponseReference`：URL、获取时间（UTC 秒）、HTTP 状态、白名单头和正文路径/摘要。路径与摘要必须成对出现，路径限定为相对的 raw/<sha256>.bin，304 禁止正文引用；不验证磁盘文件，因为写入器尚未实现。

契约拒绝未知字段、空必要字段、相对/非 HTTP(S)/含凭据 URL；引用集合使用 tuple，避免内容被随意修改。下一阶段 Parser 负责相对 URL 解析、路由身份提取和站点语义校验，不能只靠类型校验判断解析成功。

`NoticeContent.canonical_json()` 固定排序 JSON 键，使用 UTF-8 与紧凑序列化，保留正文内引用顺序；`content_sha256()` 对此编码求摘要。哈希包含标题、日期、正文和引用，排除来源身份、获取 URL/时间、Parser 版本及附件访问状态（后者是运行状态，不是公告更新）。数据库唯一键另含 parser_version。这里只保证同一结构序列化稳定，不声称已实现 HTML 语义规范化；下一阶段必须确定正文清洗、空白、动态统计剔除规则，规则变化更新 Parser 版本。

## 日志

CLI 在参数解析后才配置 signalnest logger，帮助和导入不触发配置。事件枚举目前仅有 config_validated/config_invalid/storage_initialized/storage_init_failed；每次命令有 run_id。JSON 字段限定 time、level、event 及三个可选身份字段；原始消息、args、异常栈、任意 extra、网页和配置不进入日志。未知普通日志转成 unstructured_log，不回显内容；以后增加行为时再增添对应事件。

stdout 为命令结果，stderr 为事件日志和必要的用户错误诊断。日志只输出标准流，无文件 handler、后台线程或全局 root logger 改动。字段白名单不是秘密识别器，调用者仍不得把密钥塞进身份字段。

## 已实现的数据模型

| 表 | 用途与关键约束 |
| --- | --- |
| `documents` | 稳定身份、详情 URL、发现时标题、发现/尝试/成功时间、最新处理状态、错误代码、下次到期时间，以及最近成功版本指针。`(source_id, source_document_id)` 唯一。 |
| `raw_responses` | 每次 HTTP 响应的 URL、状态、获取时间、白名单响应元数据、原始内容路径及 SHA-256。可保存列表或详情响应，不强制绑定通知。304 必须无正文引用。 |
| `notice_versions` | 通知的规范化内容 JSON、标题、发布日期、解析时间、解析器版本、规范化内容 SHA-256 和原始响应外键。`(document_id, content_sha256, parser_version)` 唯一。 |

`alembic_version` 是迁移工具的版本表，不是业务表。没有用户、搜索、推荐、发送或反馈表；信息源来自 TOML，暂不建立只有静态配置用途的 source 表。

本站 `source_document_id` 约定为 `栏目ID:文章ID`（如 `1517:128231`），后续 Parser 统一新旧链接。标题、URL 和内容摘要不替代稳定身份。原始字节摘要与规范化内容摘要分别计算，不能混用。

版本唯一键包含解析器版本：相同原文可用新 Parser 重解析，即使规范化内容恰好相同，也保留新解析器产物。相同 Parser 与相同内容不会产生重复版本。内容从 A 变为 B 后又回到 A 时，可复用 A 并更新 `current_version_id`；不能按最大版本 ID 判断当前正文。版本中的原始响应引用指向首次生成该产物的证据，并非每次复查事件；当前不提供完整处理尝试审计表。

`current_version_id` 通过复合外键保证属于该通知；为支持该双向引用，当前元数据的外键排序使用 `use_alter`，但 SQLite 初始迁移显式内联创建所有外键，不依赖不受支持的 ALTER ADD CONSTRAINT。`schema.py` 不用于 `create_all`。

## 状态与事务

- `discovered`：仅发现列表条目，尚未尝试详情处理；成功时间和版本指针均为空。
- `processed`：最近一次处理成功；必须有尝试时间、成功时间和属于该通知的版本指针，无错误代码。
- `failed`：最近一次处理失败；必须有尝试时间和错误代码。首次失败没有成功时间/版本；复查失败保留已有成功时间与版本。

所有操作时间为 UTC Unix 秒整数；发布日期为页面展示的日历日期，不从 HTTP Last-Modified 推导。`next_due_at` 可空，支持后续重启后补抓/复查；此阶段没有调度器。错误字段存简短分类代码，不存网页正文或可能含秘密的完整异常文本。

未来处理应先获取/解析，再用一个短事务写版本并更新通知状态；失败则单独短事务记录失败，不覆盖最近成功产物。数据库约束能验证引用、必要字段和幂等性，不能验证网页是否解析正确、文件是否存在或内容摘要是否正确；这些由后续 Parser/存储服务负责。测试使用合成数据验证约束，不表示采集写入服务已实现。

按照 [SQLAlchemy SQLite 事务说明](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html) 配置连接；通过 [Alembic 共享连接接口](https://alembic.sqlalchemy.org/en/latest/cookbook.html#sharing-a-connection-across-one-or-more-programmatic-migration-commands) 执行升级。每个应用连接开启 SQLite 外键检查，等待锁上限 5 秒，显式 BEGIN 使 DDL 和版本号更新一起回滚；不添加 WAL、后台队列或连接并发机制。迁移由 CLI 显式运行，导入不迁移。生产升级应先备份，历史迁移冻结后通过新增 revision 演进；使用 SQLite batch migration 时需评估版本表与通知表的双向外键，不能盲目采纳自动生成代码。

## 原始数据约定（写入器尚未实现）

初始化会建立 `data_dir/raw/`。后续原始响应正文以不可变字节文件保存，建议路径 `raw/<sha256>.bin` 相对于 data_dir；相同字节可复用文件，但每次响应元数据各自保存。规范化内容另存数据库，附件与图片先保存引用。

写入器必须先完整、原子地写文件，再在短事务建立引用；故障可能留下孤立文件，不能留下指向未完成文件的成功记录。304 没有正文，不能作为新原始 HTML。只保留 Content-Type、ETag、Last-Modified 等必要元数据，不保存 Cookie 或任意响应头。将来写入器必须校验路径位于 raw 目录、摘要和文件一致，拒绝把错误页/空正文标记为成功版本。

## 对调研建议的调整

列表返回 304 不代表没有待补抓详情或到期复查任务。列表检查与详情处理队列必须独立推进。

“遇到已知通知就停止分页”不能作为完整性的保证；置顶、顺序变化和分页移动会使重叠页策略漏报。普通运行的覆盖范围必须明确，周期完整核对及停机恢复策略将在可靠采集阶段实现。

普通运行关注增量及待处理任务；首次历史导入建立历史基线，不能默认把所有旧通知当作新提醒。未来通知策略需独立决定历史导入、首次发现和正文更新是否触发邮件，本阶段没有发送行为。

## 下一阶段 Parser 任务

- 用现有两个列表 fixture 验证每页 25 条、标题/日期、相对链接及“下页”链；不依赖静态页码规律。
- 统一新式路径和旧式查询参数中的身份，验证置前文章不会被跳过。
- 用两个详情 fixture 提取标题、日期、正文段落/表格、链接和图片引用。
- 提取附件名称和 URL，保留文件标识，不自动下载验证码保护附件。
- 建立空页面、错误页、缺少必要节点和模板变化的失败测试，不把解析失败当作空列表或成功正文。
- 在已有 NoticeContent 序列化之上确定正文规范化规则，记录解析器版本，验证重复解析输出及内容摘要一致；所有测试离线且不改写 fixture。
