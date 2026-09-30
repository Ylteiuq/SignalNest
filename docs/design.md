# SignalNest 设计与 WHU Parser

第一阶段三个节点均已完成：安装/配置/CLI、本地存储/迁移、数据契约/模块边界/日志。第一阶段为项目骨架；原始文件写入与可靠采集流程留到后续阶段。项目展示名为 SignalNest，Python 包和命令均为 signalnest。当前新增的纯 Parser 阶段也已完成，尚无采集/入库协调层。

## 模块边界

- `config.py`：读取并校验普通 TOML，不创建存储。
- `cli.py`：参数、显式初始化入口和退出码；查看帮助/校验配置不连接数据库。
- `schema.py`：同步 SQLAlchemy Core 表定义，导入只构建内存元数据，不建表。
- `storage.py`：惰性 engine、SQLite 连接设置与 Alembic 初始化/升级。
- `migrations/`：随 Python 包安装的固定历史迁移，升级与工作目录无关。
- `contracts.py`：不可变 Pydantic 输入/输出契约、规范化 JSON 与内容摘要，无 I/O。
- `fetching.py`：创建同步 HTTPX Client，不主动请求；不接触 Parser 或数据库。
- `parsing.py`：`html_tree`、`parse_list`、`parse_notice` 纯函数，按 UTF-8 解码并显式使用 `html.parser`；不联网、不访问存储，不配置 logger 或运行任务。
- `eventlog.py`：标准库 JSON 日志，CLI 显式启用；不在导入时配置日志。

HTTPX Client 保留 TLS 校验，显式设置 connect/read/write/pool 超时，限制为单连接，不自动跟随重定向或继承环境代理；后续获取器负责验证目标 URL/重定向与获取响应。依据 [HTTPX Client 文档](https://www.python-httpx.org/api/) 配置，尚无完整重试或采集流程。Beautiful Soup [显式指定后端](https://www.crummy.com/software/BeautifulSoup/bs4/doc/#specifying-the-parser-to-use)，避免本机装有 lxml 时改变结果；本源 fixture 为 UTF-8，解码失败必须报告错误。

已实现的站点函数签名为 `parse_list(page: PageInput) -> ListPage` 和 `parse_notice(page: PageInput) -> ParsedNotice`。页面输入包含内容字节与最终页面 URL，以最终 URL 解析相对地址；函数只返回结构化数据，不访问网络或数据库。

协调层未来负责 request_interval、列表遍历、详情补抓、重试、原始文件归档和短事务的顺序。现在用此文档明确职责，不预建空 coordinator 或插件工厂，也没有占位采集命令。不在数据库事务内等待 HTTP 请求。

## 最小数据契约

- `PageInput`：原始页面 bytes 与 page_url；不包含 Client/Session 等运行依赖。
- `ListEntry` / `ListPage`：源内稳定身份、绝对详情 URL、标题、日期、条目集合与下一页 URL。source_id 由协调层从配置注入；本栏目空列表必须调查，不能默认为正常。
- `ParsedNotice` / `NoticeContent`：稳定身份、页面 URL、parser_version，以及标题、日期、正文 HTML/文本、链接/图片/附件引用。HTML 保留结构但未做展示安全处理，不能直接当作可信网页渲染。图片型正文允许 body_text 为空，Parser 必须验证正文有可见文字或有效图片引用。
- `AttachmentReference`：名称、URL、可选源文件 ID（本站建议 owner:wbfileid）和访问状态。默认 not_checked，仅有证据时标为 manual_required；不下载或绕过验证码。
- `RawResponseReference`：URL、获取时间（UTC 秒）、HTTP 状态、白名单头和正文路径/摘要。路径与摘要必须成对出现，路径限定为相对的 raw/<sha256>.bin，304 禁止正文引用；不验证磁盘文件，因为写入器尚未实现。

契约拒绝未知字段、空必要字段、相对/非 HTTP(S)/含凭据 URL；引用集合使用 tuple，避免内容被随意修改。Parser 负责相对 URL 解析、路由身份提取和本站必要字段校验，不能只靠类型校验判断解析成功。

`NoticeContent.canonical_json()` 固定排序 JSON 键，使用 UTF-8 与紧凑序列化，保留正文内引用顺序；`content_sha256()` 对此编码求摘要。哈希包含标题、日期、正文和引用，排除来源身份、获取 URL/时间、Parser 版本及附件访问状态（后者是运行状态，不是公告更新）。数据库唯一键另含 parser_version。该序列化本身不等于 HTML 语义规范化；Parser 在序列化前按下面的固定规则处理 HTML。不同但任意语义等价的 HTML 不保证同一摘要。规则变化更新 Parser 版本。

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

本站 `source_document_id` 约定为 `栏目ID:文章ID`（如 `1517:128231`），Parser 已统一新旧链接。标题、URL 和内容摘要不替代稳定身份。原始字节摘要与规范化内容摘要分别计算，不能混用。

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

后续协调层应遵守：

- 原始响应入档仅表示取得采集证据，不推进成功游标或成功状态。
- Parser 返回表示解析与字段验证成功；协调层仍须将版本及成功状态提交后才算持久化成功。
- 解析失败记录分类并保留最近成功版本；同一原文以后仍可重试，规则升级后仍可重新解析。
- 如将来跳过重复处理，必须有对应原文和 parser_version 的已成功解析结果，不能只看原始摘要。内容摘要不含 parser_version；现有数据库唯一键另含版本，所以新版规则产物即使摘要相同也可保留。

这里没有实现原始文件写入、成功游标、任务队列、事务协调或通知 outbox，也没有增加数据库表。

## 下一阶段：原始文件存储与单次可靠采集

1. 实现原始 bytes 的不可变、原子落盘，校验摘要与路径并登记响应白名单元数据，明确孤立文件的恢复/清理方式。
2. 实现同步串行获取：状态码/304、有限重定向与目标校验、超时/限速/有限重试；网络等待放在数据库事务外。
3. 协调列表发现与详情补抓，每页列表验证成功后处理全部条目；304 仍检查待补抓与到期复查，分页覆盖策略单独定义。
4. 将原始引用、Parser 输出、幂等版本和状态更新接起来；解析/存储失败保留最近成功版本，同 raw 可重试，新 Parser 可重解析。明确历史导入与普通运行的行为。
5. 增加离线 HTTP 响应与文件写入失败的集成测试，验证中断后的恢复；之后再进入调度与 Email，最后真实运行观察。
