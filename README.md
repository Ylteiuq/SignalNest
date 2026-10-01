# SignalNest

单用户、自托管、长期运行的个人校园信息助手，采用 Python 模块化单体。首个信息源为武汉大学本科生院“学生通知”。**项目骨架、WHU Parser 和原文归档/幂等入库/重新解析的离线闭环已完成**；目前不会采集校园网站或发送邮件。

## 安装与检查

支持 Python **3.12.x**。安装稳定版 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在仓库根目录运行：

```sh
uv sync --locked
uv run --locked signalnest --help
uv run --locked signalnest config-check --config config.example.toml
uv run --locked pytest
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
```

`uv.lock` 锁定稳定依赖，禁止预发布版本。包名与 CLI 均为 `signalnest`，也可使用 `uv run --locked python -m signalnest`。技术栈为同步 HTTPX、Beautiful Soup（显式 `html.parser`）、Pydantic、同步 SQLAlchemy Core、Alembic、SQLite、argparse 和标准库 logging。

## 配置

```sh
cp config.example.toml signalnest.toml
uv run --locked signalnest config-check --config signalnest.toml
```

普通配置为 TOML，包含数据目录、数据库文件路径、信息源、HTTP 超时、请求间隔与 User-Agent。请求间隔是相邻请求间隔，不是轮询周期；未来协调层负责执行，当前没有采集循环。没有尚未使用的邮件/模型密钥配置，未来密钥使用环境变量。

**data_dir 和 database 都相对于配置文件所在目录解析**，database 不相对于 data_dir。配置文件路径本身由调用者定位；绝对路径保持绝对路径，`~` 展开为主目录，符号链接配置以目标文件所在目录为准。

`config-check` 校验 TOML、未知字段、HTTP(S) URL、正数且有限的超时/请求间隔；不测试网络可达性或目录可写性，不创建目录/数据库。导入模块及查看帮助同样不联网、不打开数据库、不启动任务。

## 显式初始化与升级

```sh
uv run --locked signalnest storage-init --config signalnest.toml
```

命令创建数据目录、`raw/`、数据库父目录，并通过 Alembic 升级至最新迁移（当前 `0002_response_target`）。重复运行保留数据，后续安装新版本也用此命令升级；不使用 `create_all`，不提供清空或降级命令。成功退出码为 0，存储/处理错误为 1，配置、导入元数据或命令用法错误为 2。迁移失败回滚，已创建的目录或空数据库文件可能保留。导入和重新解析要求已初始化到最新迁移，不会隐式建库或升级。

可在临时目录验证（macOS / Linux）：

```sh
trial_dir="$(mktemp -d)"
cp config.example.toml "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
```

结果位于 `$trial_dir/data/`。迁移仅支持单进程执行，尚无并发初始化协调。升级个人数据前保留数据库与 raw 目录备份。

从早期节点升级：执行 `uv sync --locked` 并显式运行 `storage-init`；**继续使用原配置文件及原 database 路径**即可，不需要更名或搬动数据。0001 历史迁移保持冻结，0002 增加响应的页面类型、目标通知与最近处理时间/错误。既有响应不猜测类型或目标；重新解析这些旧记录时返回明确错误，可用已知元数据重新导入同一原文。

## 本地导入与重新解析

在仓库根目录运行以下完整演示，所有操作离线，数据写到新临时目录：

```sh
trial_dir="$(mktemp -d)"
cp config.example.toml "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
uv run --locked signalnest import-page --config "$trial_dir/signalnest.toml" \
  --metadata examples/offline/list-page1.json --file research/fixtures/student-notices-page1.html
uv run --locked signalnest import-page --config "$trial_dir/signalnest.toml" \
  --metadata examples/offline/list-page2.json --file research/fixtures/student-notices-page2.html
uv run --locked signalnest import-page --config "$trial_dir/signalnest.toml" \
  --metadata examples/offline/current-notice.json --file research/fixtures/current-notice-detail.html
uv run --locked signalnest import-page --config "$trial_dir/signalnest.toml" \
  --metadata examples/offline/legacy-notice.json --file research/fixtures/legacy-notice-detail.html
# 上述全新库的第三条响应为新式详情；已有库请使用导入返回的 response_id。
uv run --locked signalnest reparse --config "$trial_dir/signalnest.toml" --response-id 3
```

结果为 50 个通知身份、2 个成功版本和 48 个待处理通知。命令输出 JSON，包含 `response_id`、`document_id`、`version_id`、`discovered_count` 和 `next_page_url`；当前只报告下一页，不自动遍历。重复导入不增加通知或相同规则/内容的版本；每次导入仍独立登记响应证据，相同 bytes 复用一个原始文件。重新解析复用原响应，不增加响应行或伪造获取时间。

元数据是简单 JSON：必须显式给出 `page_type`（list/notice）、`source_id`、`requested_url`、`final_url`、`fetched_at`（非负 UTC Unix 秒整数）和 `status_code`。notice 还必须给出 `source_document_id`，且该身份已由列表发现；不按文件名猜身份。可选头仅 `content_type`、`etag`、`last_modified`。source_id 必须与配置一致。CLI 的输入文件路径相对于调用工作目录，存储位置始终相对于配置文件。

示例时间沿用既有 [fixture 报告](research/whu-undergrad-notice-source.md) 的记录（当时依据响应 Date，非精确客户端观测时间）。真实导入须填写已有的获取时间，不用本地读取时间替代，也不由本命令推导 HTTP 元数据。`--processed-at` 可显式提供本次处理时间，默认才取当前时间；处理时间不得早于获取时间或相关最近处理时间。重新解析旧原文成功后会将它设为当前版本，这是显式操作，不按原获取时间自动忽略旧内容。

非 200 响应可以归档和登记，但不会交给 Parser 或报告处理成功。304 元数据必须省略 `--file`，只登记无正文证据，输出 `outcome=evidence_only`；不能用来建立 HTML 基线，也不能单独重新解析。200 空文件会保留证据并记录 `parse_empty_page` 失败。没有提供仅返回成功的采集命令。

原文为 `data_dir/raw/<sha256>.bin`。写入器先完整写同目录临时文件、校验并 fsync，再原子且不覆盖地发布；已有文件、重新解析读取都验证摘要。拒绝路径逃逸、符号链接和非普通文件，损坏不覆盖。当前文件实现面向 macOS/Linux POSIX；配置加载先解析路径别名，写入器拒绝解析后路径内再出现符号链接。

文件 I/O 和解析均在数据库事务外；详情的版本、当前版本指针、成功状态及响应处理摘要一起提交。失败保留最近成功版本，同一原文可以再次尝试。连失败状态都无法登记时返回 `failure_state_unavailable`，不声称已经恢复。文件发布后、数据库登记前失败可留下孤立文件，保留供人工核对；进程中断也可能留下 `.tmp-*`。停止所有写入后，以 raw_responses 的 body_path 对照文件识别孤立文件；不自动删除。fsync/原子发布不能证明断电持久性，文件与 SQLite 也不构成跨介质原子事务，详见 [设计说明](docs/design.md)。

## 日志与当前边界

命令结果写 stdout；结构化事件日志写 stderr，包含 UTC 时间、级别、事件名和可选 source_id/run_id/document_id/response_id/stage/error_code。错误诊断也写 stderr，因此错误输出不是纯 JSON 流。日志不包含原始配置、URL、异常正文或网页正文；只在 CLI 显式配置 SignalNest 的 logger，不修改 root logger。

已实现：安装/CLI/配置、数据库初始化与迁移、三张业务表及约束、契约与稳定内容摘要、同步 HTTP 客户端配置、WHU Parser、原文存储、整页幂等发现、版本/成功状态原子提交、失败登记、离线导入/重新解析及结构化日志。

尚未实现：真实 HTTP 获取、重试/限速与分页协调、调度、Email、历史搜索、LLM/Embedding/RAG/Agent。HTTP 客户端不自动发送请求；Parser 返回结果也不代表已经持久化成功。不增加用户系统、微服务、Redis、Celery、向量数据库、Docker、CI 或跨语言接口。

fixture 驱动的 Parser 与离线闭环已完成；后续依次为：**单次可靠 HTTP 采集 → 调度与 Email → 真实运行观察**。普通运行处理增量/待补抓任务；首次历史导入建立基线；未来通知策略独立决定哪些事件发送邮件，不默认给所有历史通知发邮件。当前离线导入不产生邮件事件。

列表 304 不代表没有待补抓详情或到期复查任务；“遇到已知通知就停止分页”不能保证完整性。模块边界、规范化规则、错误分类和后续集成约定见 [设计说明](docs/design.md)。

## 离线 Parser

```python
from pathlib import Path
from signalnest.contracts import PageInput
from signalnest.parsing import parse_list, parse_notice

page = PageInput(
    content=Path("research/fixtures/student-notices-page1.html").read_bytes(),
    page_url="https://uc.whu.edu.cn/tzgg/xstz.htm",  # 获取后的最终 URL
)
listing = parse_list(page)
assert len(listing.entries) == 25  # 此 fixture 的数量，不是 Parser 的固定限制
notice = parse_notice(
    PageInput(
        content=Path("research/fixtures/legacy-notice-detail.html").read_bytes(),
        page_url="https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=127581",
    )
)
print(notice.source_document_id, notice.content.content_sha256())
```

两个函数都完整解析并验证输入，使用 UTF-8 / `html.parser`，不联网、不执行脚本、不下载引用、不写文件或数据库，没有原始摘要缓存。两种文章路由统一为 `1517:文章ID`；列表沿实际“下页”链接，不因日期或已知条目提前停止。任一必要条目无效则整页失败。

详情保留段落/表格、正文文本及 HTTP(S) 链接/图片/附件引用。HTML 中的 `a[href]` 和 `img[src]` 改为相对于最终页面 URL 的绝对地址；锚点、mailto、javascript 等链接不进入网页引用，并移除 href、保留可见文本。图片与附件仅有元数据；附件 access 保持 `not_checked`。脚本、已知统计节点、正文外区域不参与内容摘要。保留的 HTML **不是安全清洗产物**，后续展示不能直接信任它。

规则版本为 `whu-student-notices-v1`。原始字节摘要标识采集证据，不证明解析成功；`NoticeContent.content_sha256()` 标识规范化内容；`parser_version` 标识提取/清洗规则，独立于内容摘要。规则变化须更新版本，允许对同一原文重解析。

失败抛出 `ParseError`，提供 `code`、可选 `field` 和从 0 开始的 `item_index`：编码/空白输入、必要结构缺失、无效字段、身份不支持/含糊、异常空列表、无意义正文分别分类。空字节仍由 PageInput 拒绝。具体代码与规范化保证范围见 [设计说明](docs/design.md)。PageInput 不携带状态码；离线入库层仅处理 200，后续获取层负责完整的 HTTP 状态和 304 基线策略。

## 实际验证

2026-09-29，macOS / Darwin arm64，CPython 3.12.14，SQLite 3.53.1，uv 0.12.20。锁定的直接依赖为 Pydantic 2.13.5、SQLAlchemy 2.0.54、Alembic 1.20.0、HTTPX 0.28.1、Beautiful Soup 4.15.0；开发检查使用 pytest 8.4.2、Ruff 0.16.9。工具安装于临时隔离环境，未修改系统 Python。

**54 项离线测试全部通过，Ruff 检查与格式检查通过。** 测试覆盖配置和路径、错误退出、无副作用导入、迁移与重复初始化、唯一/外键/状态约束、升级失败回滚、数据契约与摘要、HTTP MockTransport、fixture HTML 后端、日志字段与敏感内容排除。测试默认离线；调研与 fixture 保持原样。sdist/wheel 构建通过；按锁文件在独立环境安装 wheel 后，已从临时工作目录验证帮助、配置校验不建库、两次初始化、迁移版本、错误退出与 JSON 日志。更名前的配置文件/数据库路径可继续使用，初始迁移内容未变。未验证其他操作系统/Python 次版本、并发迁移或校园网站实采。

2026-09-30 Parser 阶段：在上述 CPython 3.12.14 环境中完整执行 pytest，**136 项离线测试通过**（新增 82 项、原有 54 项），Ruff 检查和格式检查通过。使用四份既有 HTML 与内存构造样本，覆盖列表、身份、正文/媒体、字段错误、无意义正文、重复失败与确定性、统计/正文外修改不改变内容摘要、真实正文变化改变摘要。原报告、fixture 和用户未提交调研文件保持原样；契约、依赖、schema 和历史迁移未修改。没有实时抓取或运行上游故障实验。

2026-10-01 离线持久化阶段：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1，依赖未变。`uv sync --locked --offline` 成功，完整 pytest **199 项通过**；`ruff check src tests`、`ruff format --check src tests` 和 `git diff --check` 通过。另按本页命令在新临时库中初始化两次、导入四份 fixture、重新解析及重复导入，查询确认 50 通知/2 版本/48 待处理；重复导入共 8 条独立响应证据，仍仅 4 个原文文件，重新解析不增加响应。

新增验证使用真实文件/SQLite，覆盖身份/版本幂等、A→B→A、失败原文与状态保留、损坏/缺失/路径/符号链接、整页回滚、版本与成功状态一起回滚、文件发布与目录同步失败、提交失败、失败登记不可用、重新打开库继续处理、新版规则重解析、CLI 非零退出和无副作用帮助。中断场景是进程内 KeyboardInterrupt/数据库触发器/事件/I/O **故障注入**；没有真正终止进程、断电或真实 HTTP 实验，也未在 Linux/Windows 上运行验证。Parser、0001 和原有四份 HTML 的字节摘要核对不变；本任务未修改 research，保留并行调研文件。

另执行了全仓库 `ruff check .` 和 `ruff format --check .`：未通过，原因仅为另一项研究中的 `research/experiments/ingestion/capture_pages.py` 与 `offline_checks.py`（21 项 lint，2 个文件需格式化）。这些独立研究脚本不属于本次业务代码，按协作边界保留不修改；上面的开发命令检查全部项目源代码与测试。
