# SignalNest

单用户、自托管、长期运行的个人校园信息助手，采用 Python 模块化单体。首个信息源为武汉大学本科生院“学生通知”。**项目骨架、WHU Parser、离线持久化、有界采集、处理政策、状态诊断与 N0 本地通知决策已完成**。显式执行 `crawl-once` 或 `scheduled-run` 获取页面；提供外部定时模板，没有 Python 后台调度器或邮件发送。

## 安装与检查

支持 Python **3.12.x**。安装稳定版 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在仓库根目录运行：

```sh
uv sync --locked
uv run --locked signalnest --help
uv run --locked signalnest config-check --config config.example.toml
uv run --locked pytest
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
# 另检查部署辅助程序。
uv run --locked ruff check deploy/verify_backup.py
uv run --locked ruff format --check deploy/verify_backup.py
```

`uv.lock` 锁定稳定依赖，禁止预发布版本。包名与 CLI 均为 `signalnest`，也可使用 `uv run --locked python -m signalnest`。技术栈为同步 HTTPX、Beautiful Soup（显式 `html.parser`）、Pydantic、同步 SQLAlchemy Core、Alembic、SQLite、argparse 和标准库 logging。

## 配置

```sh
cp config.example.toml signalnest.toml
uv run --locked signalnest config-check --config signalnest.toml
```

普通配置为 TOML，包含数据目录、数据库文件路径、信息源、HTTP 超时、请求间隔与 User-Agent。请求间隔是相邻物理请求的间隔，不是轮询周期；一次采集复用同一个 Fetcher，没有后台采集循环。没有尚未使用的邮件/模型密钥配置，未来密钥使用环境变量。

**data_dir 和 database 都相对于配置文件所在目录解析**，database 不相对于 data_dir。配置文件路径本身由调用者定位；绝对路径保持绝对路径，`~` 展开为主目录，符号链接配置以目标文件所在目录为准。

`config-check` 校验 TOML、未知字段、HTTP(S) URL、正数且有限的超时/请求间隔；不测试网络可达性或目录可写性，不创建目录/数据库。导入模块及查看帮助同样不联网、不打开数据库、不启动任务。

`[runtime]` 配置详情配额、复查日期档与有限错误延期；`[runtime.regular]` / `[runtime.full]` 为外部触发的单次任务预算。旧配置可以省略 runtime，使用新默认政策。定时频率由外部 timer 决定，不能用 HTTP 请求间隔设置。示例字段与行为见 [处理政策](docs/runtime-policy.md)。

## 显式初始化与升级

```sh
uv run --locked signalnest storage-init --config signalnest.toml
```

命令创建数据目录、`raw/`、数据库父目录，并通过 Alembic 升级至最新迁移（当前 `0003_ingestion_state`）。重复运行保留数据，后续安装新版本也用此命令升级；不使用 `create_all`，不提供清空或降级命令。成功退出码为 0，存储/处理错误为 1，配置、导入元数据或命令用法错误为 2。迁移失败回滚，已创建的目录或空数据库文件可能保留。导入和重新解析要求已初始化到最新迁移，不会隐式建库或升级。

可在临时目录验证（macOS / Linux）：

```sh
trial_dir="$(mktemp -d)"
cp config.example.toml "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
```

结果位于 `$trial_dir/data/`。初始化、升级、import-page、reparse、策略更新和两种采集入口共用数据库旁的 POSIX advisory 写入锁；并行写入立即拒绝，进程退出释放锁，锁文件保留。库调用者须使用同一 writer_lock 覆盖整个写入运行；`crawl_once` 自行持有整次运行的锁。升级个人数据前保留数据库与 raw 目录备份。

从早期节点升级：执行 `uv sync --locked` 并显式运行 `storage-init`；**继续使用原配置文件及原 database 路径**即可，不需要更名或搬动数据。0001/0002 保持冻结；0003 增加响应完整性/缓存绑定、首次发现来源和三个运行事实表，直接 ADD COLUMN，不重建旧表。既有响应不猜测类型或目标；重新解析这些旧记录时返回明确错误，可用已知元数据重新导入同一原文。

## 单次采集

数据库须先显式初始化；以下采集命令会真实发送 HTTP 请求。默认测试使用离线模拟传输，少量实采验证仅使用临时目录和更小额度：

```sh
uv run --locked signalnest crawl-once --help
# 明确受限扫描，只遍历一页并最多尝试两篇详情。
uv run --locked signalnest crawl-once --config signalnest.toml \
  --scan limited --max-pages 1 --max-details 2
# 尝试完整覆盖；实际页面证据和首页复核通过后才能报告 complete。
uv run --locked signalnest crawl-once --config signalnest.toml \
  --scan full --max-pages 64 --max-details 20 --max-requests 120 --run-seconds 600
```

`--scan {full,limited}` 必填。列表页上限默认 full=64、limited=2；详情默认 20，`--max-details 0` 可只处理列表。物理请求默认 120 次、运行预算 600 秒、每个资源预算 60 秒、完整正文上限 2 MiB，分别由 `--max-requests`、`--run-seconds`、`--resource-seconds`、`--max-body-bytes` 设置。首页复核、重定向、重试和完整获取回退都计入请求/时间预算；期限不是阻塞 DNS/read 的硬终止保证。

每次扫描从首页开始，沿实际 next 链校验请求/最终 URI、连续页码、总数与尾页，并提交每页全部条目。full 还重新请求首页（`Cache-Control: no-cache`，允许条件验证），比较最终 URI 和完整结构化列表。受限、循环、漂移、预算耗尽或失败均不推进完整扫描成功时间。主动选择 limited 时，即使到达末页也保留 limited；full 被页预算截断则报告 interrupted。列表 304 或普通列表失败后，仍从数据库查询详情待办；服务端冷却、全局请求/时间预算约束所有网络请求。

旧的 processed 通知若 `next_due_at` 为空，在运行开始事务中入队首次联网复查，不根据发布日期猜测。详情成功后 24 小时到期，普通失败后 15 分钟可重试；首次失败无成功版本，复查失败保留旧版本。第一次 complete 前发现来源为 bootstrap，之后为 regular，不生成邮件事件。详情处理按数据库有效到期时间（空值用首次发现时间，数据库 ID 仅用于平局）取有界批次，单篇普通 HTTP/解析失败继续处理其他篇；归档、数据库或失败登记不能可靠完成时终止。

stdout 返回 JSON 摘要：`scanned_entries` 是已提交遍历页的全部行数（可含跨页重叠，不含首页复核），`new_documents` 是本来源的真实新增身份；另含详情尝试/成功/失败、实际请求、覆盖、运行结果及待办。`remaining_due` 为当前可处理数，`remaining_unprocessed` 为 discovered/failed 数，含保留旧成功版本的复查失败。succeeded 退出 0，其他运行结果/存储错误退出 1，参数错误退出 2，用户中断退出 130；limited 覆盖与成功运行可以同时成立。详见 [协调器设计与恢复边界](docs/crawling.md)。

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

结果为 50 个通知身份、2 个成功版本和 48 个待处理通知。离线命令输出 JSON，保留 `response_id`、`document_id`、`version_id`、`discovered_count` 和 `next_page_url`，增加 `pagination` 证据对象；本地导入不自动遍历。详情和仅登记证据的 304 输出 `pagination=null`，不能把它当末页。重复导入不增加通知或相同规则/内容的版本；每次导入仍独立登记响应证据，相同 bytes 复用一个原始文件。重新解析复用原响应，不增加响应行或伪造获取时间。

元数据是简单 JSON：必须显式给出 `page_type`（list/notice）、`source_id`、`requested_url`、`final_url`、`fetched_at`（非负 UTC Unix 秒整数）和 `status_code`。notice 还必须给出 `source_document_id`，且该身份已由列表发现；不按文件名猜身份。可选头为 `content_type`、`etag`、`last_modified`、`vary`、`cache_control`、`content_encoding`；可明确给出 `body_state`（complete/unavailable）与 `request_profile`（user_agent/accept/accept_encoding=identity）。既有示例没有 profile，不自动取得缓存资格。source_id 必须与配置一致。CLI 的输入文件路径相对于调用工作目录，存储位置始终相对于配置文件。

示例时间沿用既有 [fixture 报告](research/whu-undergrad-notice-source.md) 的记录（当时依据响应 Date，非精确客户端观测时间）。真实导入须填写已有的获取时间，不用本地读取时间替代，也不由本命令推导 HTTP 元数据。`--processed-at` 可显式提供本次处理时间，默认才取当前时间；处理时间不得早于获取时间或相关最近处理时间。重新解析旧原文成功后会将它设为当前版本，这是显式操作，不按原获取时间自动忽略旧内容。

非 200、未完整读取或只保存元数据的响应允许省略 `--file`，会登记证据并以非零退出报告无法处理；unavailable 禁止带文件，complete 必须带文件。304 必须省略文件；CLI 普通导入不猜测绑定，仍输出 `outcome=evidence_only`，无绑定不能单独 reparse。服务层显式绑定的 304 可重新处理真实 200 原文。带 profile 的空 200 不发布正文，应只登记 unavailable；既有无 profile 离线空文件仍保留 `parse_empty_page` 失败证据。没有提供仅返回成功的采集命令。

原文为 `data_dir/raw/<sha256>.bin`。写入器先完整写同目录临时文件、校验并 fsync，再原子且不覆盖地发布；已有文件、重新解析读取都验证摘要。拒绝路径逃逸、符号链接和非普通文件，损坏不覆盖。当前文件实现面向 macOS/Linux POSIX；配置加载先解析路径别名，写入器拒绝解析后路径内再出现符号链接。

文件 I/O 和解析均在数据库事务外；详情的版本、当前版本指针、成功状态及响应处理摘要一起提交。失败保留最近成功版本，同一原文可以再次尝试。连失败状态都无法登记时返回 `failure_state_unavailable`，不声称已经恢复。文件发布后、数据库登记前失败可留下孤立文件，保留供人工核对；进程中断也可能留下 `.tmp-*`。停止所有写入后，以 raw_responses 的 body_path 对照文件识别孤立文件；不自动删除。fsync/原子发布不能证明断电持久性，文件与 SQLite 也不构成跨介质原子事务，详见 [设计说明](docs/design.md)。

## 单次 HTTP 的离线衔接接口

这些证据/业务接口不发送 HTTP，详见 [持久状态设计](docs/ingestion-state.md)。`crawl_once` 将 Fetcher 与这些接口连接，详情到期时间仍只有 documents.next_due_at：

- `select_cache_candidate` 按 source + 实际请求完整 URI + 固定 RequestProfile 选择具体 200，并校验文件。无候选或原文丢失/损坏时返回 `requires_full_fetch`，不能取最近成功版本代替。
- `record_response(..., candidate=...)` 将无正文 304 绑定到请求前选定的 200。新的完整 200 即使解析失败也成为最新传输原文；旧成功内容保留。URI 查询不排序、不删除；旧路由与新路由、不同 profile 不共用验证器。
- `process_cached_response` 自动处理最新原文/绑定 304，返回 `body_response_id` 与观察的 `response_id`；无法安全复用时返回 `outcome=full_fetch_required` 与有限错误代码，获取器据此完整获取。解析/数据库失败仍抛错，不能当作无变化。
- `discover_page_in_transaction`、`save_notice_in_transaction`、`record_failure_in_transaction` 使用已有 Connection，与资源标记/详情 due/来源状态组合提交；便利入口仍自行包裹短事务。`record_failure` 可直接登记元数据失败，保留旧成功结果。
- 来源/运行状态接口记录列表尝试、有效正文证据、整页登记、完整/受限/中断覆盖及独立运行结果；`pending_documents` / `pending_resources` 从 SQLite 重建工作，详情 due 只有 documents.next_due_at。服务端 not_before 冷却持久化，HTTP 层负责遵守。

支持的 Vary 仅 User-Agent/Accept/Accept-Encoding；未知 Vary、`*`、no-store、压缩编码或无效验证器要求完整获取。不兼容 304 会持久阻断后续条件请求，直到新的完整 200。private/no-cache 可以条件验证；不会用 304 改写原 200 头和获取时间。显式历史 reparse 可以切换旧版本，自动缓存恢复会拒绝旧原文回退。

完整扫描状态接口只校验传入的分页和复核声明；`crawl_once` 在调用前实际提交全部页面、验证 URI 链/连续页码/总数/明确末页并完成首页复核。列表 complete 与运行 partial_failure 可以同时成立。旧记录的发现 origin 保持 unknown，不根据日期猜测。

写入锁支持 macOS/Linux POSIX，本次实测 macOS；Windows 未支持。数据库路径别名解析到同一锁，硬链接数据库和符号链接锁文件拒绝。帮助/config-check 不获取锁；外部不遵守 advisory lock 的程序不受保护。文件/Parser 均在事务外，正常回滚与锁释放不证明断电耐久性。

## 有界 HTTP 获取接口

`HttpFetcher(engine, raw_store, settings.http, source_id=...)` 是库入口；构造不发送请求，调用者持有 writer_lock、在一次运行内复用实例，并用上下文管理器关闭 Client。`fetch(FetchTarget(uri=..., page_type="list"))` 获取一个资源；详情显式指定 page_type="notice" 与 source_document_id。`crawl_once` 复用一个实例完成列表与详情。

`FetchResult.outcome` 为 complete、bodyless、transport_failure 或 deferred；`attempts` 保留每个实际 GET 的时间、ResponseInput、完整 bytes/None、304 的具体 candidate 与有限错误。调用者须按顺序登记所有已收到的响应，然后调用既有业务处理接口。Fetcher 自己只校验缓存文件和保存冷却，不归档响应或提交通知成功。

默认 profile 的 User-Agent 来自配置，Accept=text/html、Accept-Encoding=identity；也可显式提供 RequestProfile，实际三个请求头始终完全一致，拒绝不能直接发送的非 ASCII profile 值。Cookie/Auth 不发送；TLS 验证开启，环境代理关闭，底层重试为 0。手动跳转限本站 HTTPS 443、支持的列表路径或同身份详情路由，每跳重新选择自己的验证器。

完整 200 正文流式计数，默认最多 2 MiB，拒绝压缩编码、短读、空正文与非 HTML 类型；站点结构仍由 Parser 验证。默认两次额外尝试、三跳重定向、单资源 60 秒、全实例 600 秒/120 次物理请求；HTTP 超时按剩余预算收紧。异常 304 最多一次完整 GET 修复，共用额外尝试/间隔/运行预算。429 与适用的 Retry-After 保存完整 not-before，最多本次等 15 秒，其他同来源 URL 与重启也须遵守。期限是协作预算，不能承诺同步 DNS/read 内的精确硬终止。

`fetch(..., revalidate=True)` 用于首页复核；登记 304 后发现绑定文件再失效时，协调器调用 `repair(result)`，在同一次资源预算和唯一重试层内完整获取，不创建虚假原文或获取时间。交接表、错误分类、保守策略及限制见 [Fetcher 设计](docs/fetching.md) 和 [协调器设计](docs/crawling.md)。已完成真实子进程终止/重启及临时目录少量低频实采，具体证据、结果和验证边界见 [恢复验证记录](docs/recovery-validation.md)。默认测试仍完全离线。

## 定时运行与状态诊断

```sh
# 只读 JSON 诊断：不联网、不建库、不取得写入锁。
uv run --locked signalnest status --config signalnest.toml
# 以下两项会联网，使用 TOML 中对应的有界预算。
uv run --locked signalnest scheduled-run --config signalnest.toml --mode regular
uv run --locked signalnest scheduled-run --config signalnest.toml --mode full
# 首次采用新政策：先备份，再显式保守重算旧成功 due；不联网。
uv run --locked signalnest apply-recheck-policy --config signalnest.toml
```

到期详情分为前两页首次待办 F、其他首次积压 H、已有成功基线复查 R；默认保留 12/4/4，借用空槽并轮转，失败也计逻辑尝试。成功按上海日历年龄 0–7/8–30/>30 天分别 24 小时/7 天/30 天复查；未来日期按近期档并计数。失败按有限类别延期，始终服从来源冷却。due 与成功状态同事务提交；旧成功 due 不会自动批量重写，显式更新不会推迟逾期或清除失败退避。

`status` 区分列表尝试、有效响应、登记与完整扫描，展示首次积压、成功基线复查、冷却、最近运行及过期/预算提示。正常读到警告仍退出 0，存储错误 1、配置错误 2；它不把 running 行当活进程，也不从持久记录猜测本次 F/H 分组。新摘要和组日志提供分配、尝试、成功、失败、未服务、剩余 due 与最老逾期。

交付一个 **Linux/systemd 252+** 模板：普通每半小时，full 每日上海时间 01:15，均单次串行、同实例锁、无自动重启。首次或至少 24 小时停机恢复先执行一次有界 full；错过的普通周期不排队重放。模板尚未安装/启用，目标机需验证。专用日志保留、一致备份与只读恢复校验见 [部署与运维](docs/operations.md)；处理和诊断的准确语义见 [政策说明](docs/runtime-policy.md)。

## 本地画像与通知决策预览（N0）

`profile.example.toml` 明确标注为虚构示例，必须按自己的情况编辑；缺少的学校、层次、学院、专业和入学年保持未知，不推断为“符合资格”。Profile 独立于采集配置，不包含邮箱或凭据。至少配置一项关注主题或字面词组，拒绝未知字段、非法主题、重复项和数值隐式转换。

```sh
cp profile.example.toml profile.toml
uv run --locked signalnest profile-check --profile profile.toml
# 使用已有真实 fixture，纯离线；显式提供最终 URL 与决策时钟。
uv run --locked signalnest decision-preview --profile profile.toml \
  --file research/fixtures/notifications/notice-128291-20261005T133533Z.html \
  --url https://uc.whu.edu.cn/info/1517/128291.htm \
  --at 2026-10-05T20:00:00+08:00 --next-digest-at 2026-10-06T09:00:00+08:00
# 也可输入 NoticeContent JSON；不从数据库隐式取当前版本。
uv run --locked signalnest decision-preview --profile profile.toml \
  --notice-json /absolute/path/notice.json \
  --at 2026-10-05T20:00:00+08:00 --next-digest-at 2026-10-06T09:00:00+08:00
```

以上命令不需要 `--config`、数据库初始化或写入锁，不联网、不写数据库或原文、不发送邮件。画像路径、HTML/JSON 路径相对于调用工作目录；时间必须含时区，下一次 Digest 时间必须晚于决策时间。`--file` 必须给出实际最终详情 URL，不能根据文件名猜文章身份。JSON 输入完整字段见 [NoticeContent](src/signalnest/contracts.py)，不是数据库行或 ParsedNotice 包装。

stdout 为 JSON：事实、短原文证据、资格/时间三值判断、Action、独立 `needs_review`、理由、命中规则、未知项、版本和摘要。正文全文不重复输出。Action 为 `PUSH_NOW / DIGEST / STORE_ONLY / IGNORE`；有效路线为 `immediate / digest / none`。路线只是预览结果，**当前不存在投递意图、邮件计划或发送记录**。资格未知仍可提示核对可信紧迫的相关机会，不会声称用户已符合资格；近期日期本身不触发推送。

`--event-kind {new,update,activation_recent,historical}` 与 `--mode {hybrid,digest_only}` 仅声明预览上下文。update 可给 `--previous-notice-json` 和 `--previous-route`，缺少旧内容则标明比较未知。首启预览默认汇总，可信截止不晚于下一次 Digest 时保留紧急路线；显式 digest_only 仍优先。命令不证明实际发生新内容或此前已登记邮件资格。校验/参数错误退出 2，Parser 无法支持输入页面退出 1，成功预览退出 0。

当前只支持有限字面主题、对象表达式和完整年份的时间；图片/附件仅保留引用，不做 OCR 或解析下载文件。不能可靠识别的条件与时间保留未知。覆盖、限制及纯函数入口见 [N0 规则说明](docs/notifications.md)。[N1 接口稿](docs/notification-state.md)定义启用边界、独立 live 基线和未来成功事务，但尚未实现；本轮不新增 schema、迁移或接入采集后通知。

## 日志与当前边界

命令结果写 stdout；结构化事件日志写 stderr，包含 UTC 时间、级别、事件名和可选 source_id/run_id/document_id/response_id/stage/error_code。错误诊断也写 stderr，因此错误输出不是纯 JSON 流。日志不包含原始配置、URL、异常正文或网页正文；只在 CLI 显式配置 SignalNest 的 logger，不修改 root logger。

已实现：安装/CLI/配置、数据库初始化与迁移、三张核心业务表和三张运行事实表及约束、契约与稳定内容摘要、有界同步 HTTP Fetcher、WHU Parser、原文存储、整页幂等发现、版本/成功状态原子提交、失败登记、离线导入/重新解析、单次完整/受限扫描、独立详情补抓/复查、分组处理与到期政策、只读诊断、运行摘要、日志及外部定时模板、备份恢复校验程序，以及 N0 本地画像、事实提取和决策/路线预览。

尚未开展：目标 Linux 定时器实际部署、长期运行观察、Email、历史搜索、LLM/Embedding/RAG/Agent。HTTP 必须显式调用 Fetcher 或采集入口；获取或 Parser 成功也不代表已经持久化成功。不增加用户系统、微服务、Redis、Celery、向量数据库、Docker、CI 或跨语言接口。

fixture 驱动的 Parser、离线闭环及三个采集交付均已完成：**有界 Fetcher → 单次采集协调器与 CLI → 整条恢复验证、真实终止实验及少量低频实采**。定时运行与诊断节点现提供政策和部署模板，后续在目标机观察，再设计 Email 记录与补偿。既有实采验证限于记录中的边界和样本，不等于断电或全站扫描验证。普通运行处理增量/待补抓任务；首次历史导入建立基线；未来通知策略独立决定哪些事件发送邮件，不默认给所有历史通知发邮件。当前所有入口均不产生邮件事件。

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
assert listing.pagination.current_page == 1
assert listing.pagination.total_pages == 24  # 从这一份页面的证据取得，未硬编码
assert not listing.pagination.is_last_page
notice = parse_notice(
    PageInput(
        content=Path("research/fixtures/legacy-notice-detail.html").read_bytes(),
        page_url="https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=127581",
    )
)
print(notice.source_document_id, notice.content.content_sha256())
```

两个函数都完整解析并验证输入，使用 UTF-8 / `html.parser`，不联网、不执行脚本、不下载引用、不写文件或数据库，没有原始摘要缓存。两种文章路由统一为 `1517:文章ID`；列表沿实际“下页”链接，不因日期或已知条目提前停止。任一必要条目无效则整页失败。

`ListPage.entries` 和顶层 `next_page_url` 的访问方式不变；新增必需的 `pagination: PaginationEvidence`，手工构造 ListPage 时也须提供证据。字段为 `current_page`、`total_pages`、`is_last_page`、`terminal_evidence`、`last_page_url`。非末页的总页数由最大可见数字锚与活动“尾页”链接一致性支持；末页必须当前页等于最大可见页码，且“下页”与“尾页”均为唯一、无锚的禁用标记，证据为 `disabled_next_and_last`。末页的 next/last URL 都为 null，非末页保留 HTML 中的真实链接。

既有首页、第二页及新增真实末页分别解析为 1/24、2/24、24/24，条目数为 25/25/13。文件名 `1.htm` 不代表第 1 页；不从文件名计算任何页码或下一页。缺失/重复 paginator、必要数字/控制缺失、状态冲突或活动链接损坏均抛 ParseError。当前没有可信的无分页单页模板，不能省略 paginator；少于 25 条、日期较旧或条目全已知也不证明末页。

分页 URL 限 HTTPS、`uc.whu.edu.cn` 默认 443、无凭据/查询/片段，路径为 `/tzgg/xstz.htm` 或 `/tzgg/xstz/<正整数>.htm`；下一页自链拒绝。可见 next 数字锚存在时须与活动“下页”一致。可见数字唯一、递增，跳过的数字须有实际省略号；明确 hidden、aria-hidden 或内联隐藏样式的必要证据拒绝，不计算外部 CSS 或执行脚本。原文模板不能可靠识别时失败。完整性由 `crawl_once` 校验跨页连续性、总页数漂移、请求/最终 URI 循环及尾页目标，所有页面成功登记并完成首页复核后才能判断扫描完成。单页的末页证据不等于一次完整扫描成功。

详情保留段落/表格、正文文本及 HTTP(S) 链接/图片/附件引用。HTML 中的 `a[href]` 和 `img[src]` 改为相对于最终页面 URL 的绝对地址；锚点、mailto、javascript 等链接不进入网页引用，并移除 href、保留可见文本。图片与附件仅有元数据；附件 access 保持 `not_checked`。脚本、已知统计节点、正文外区域不参与内容摘要。保留的 HTML **不是安全清洗产物**，后续展示不能直接信任它。

分页阶段将规则版本升级为 `whu-student-notices-v2`，本次协调器交付未改变它。沿用一个全站 Parser 版本，因此详情解析产物也标为 v2；详情的提取、规范化和 NoticeContent 摘要规则未变，两份详情 fixture 的内容摘要与 v1 一致。已有 v1 详情重解析可新增同摘要的 v2 产物，旧版本保留，遵守现有版本唯一键。原始字节摘要仅标识采集证据，不证明解析成功；parser_version 不进入内容摘要。

失败抛出 `ParseError`，提供 `code`、可选 `field` 和从 0 开始的 `item_index`：编码/空白输入、必要结构缺失、无效字段、身份不支持/含糊、异常空列表、无意义正文分别分类。空字节仍由 PageInput 拒绝。具体代码与规范化保证范围见 [设计说明](docs/design.md)。PageInput 不携带状态码；业务层仅把完整 200 原文交给 Parser，Fetcher 与缓存接口处理 HTTP 状态和 304 基线策略。

分页结构缺失/重复使用 `missing_structure`，非法整数或 URL 使用 `invalid_field`，相互矛盾的页码、数字链接或活动/禁用标记使用新增 `invalid_pagination`。离线入库仍按原规则保存证据和 `parse_` 错误代码，不登记部分条目，不改变原有事务与恢复语义；分页证据不新增数据库字段，重新解析时从归档原文重新取得。

## 实际验证

2026-09-29，macOS / Darwin arm64，CPython 3.12.14，SQLite 3.53.1，uv 0.12.20。锁定的直接依赖为 Pydantic 2.13.5、SQLAlchemy 2.0.54、Alembic 1.20.0、HTTPX 0.28.1、Beautiful Soup 4.15.0；开发检查使用 pytest 8.4.2、Ruff 0.16.9。工具安装于临时隔离环境，未修改系统 Python。

**54 项离线测试全部通过，Ruff 检查与格式检查通过。** 测试覆盖配置和路径、错误退出、无副作用导入、迁移与重复初始化、唯一/外键/状态约束、升级失败回滚、数据契约与摘要、HTTP MockTransport、fixture HTML 后端、日志字段与敏感内容排除。测试默认离线；调研与 fixture 保持原样。sdist/wheel 构建通过；按锁文件在独立环境安装 wheel 后，已从临时工作目录验证帮助、配置校验不建库、两次初始化、迁移版本、错误退出与 JSON 日志。更名前的配置文件/数据库路径可继续使用，初始迁移内容未变。未验证其他操作系统/Python 次版本、并发迁移或校园网站实采。

2026-09-30 Parser 阶段：在上述 CPython 3.12.14 环境中完整执行 pytest，**136 项离线测试通过**（新增 82 项、原有 54 项），Ruff 检查和格式检查通过。使用四份既有 HTML 与内存构造样本，覆盖列表、身份、正文/媒体、字段错误、无意义正文、重复失败与确定性、统计/正文外修改不改变内容摘要、真实正文变化改变摘要。原报告、fixture 和用户未提交调研文件保持原样；契约、依赖、schema 和历史迁移未修改。没有实时抓取或运行上游故障实验。

2026-10-01 离线持久化阶段：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1，依赖未变。`uv sync --locked --offline` 成功，完整 pytest **199 项通过**；`ruff check src tests`、`ruff format --check src tests` 和 `git diff --check` 通过。另按本页命令在新临时库中初始化两次、导入四份 fixture、重新解析及重复导入，查询确认 50 通知/2 版本/48 待处理；重复导入共 8 条独立响应证据，仍仅 4 个原文文件，重新解析不增加响应。

新增验证使用真实文件/SQLite，覆盖身份/版本幂等、A→B→A、失败原文与状态保留、损坏/缺失/路径/符号链接、整页回滚、版本与成功状态一起回滚、文件发布与目录同步失败、提交失败、失败登记不可用、重新打开库继续处理、新版规则重解析、CLI 非零退出和无副作用帮助。中断场景是进程内 KeyboardInterrupt/数据库触发器/事件/I/O **故障注入**；没有真正终止进程、断电或真实 HTTP 实验，也未在 Linux/Windows 上运行验证。Parser、0001 和原有四份 HTML 的字节摘要核对不变；本任务未修改 research，保留并行调研文件。

另执行了全仓库 `ruff check .` 和 `ruff format --check .`：未通过，原因仅为另一项研究中的 `research/experiments/ingestion/capture_pages.py` 与 `offline_checks.py`（21 项 lint，2 个文件需格式化）。这些独立研究脚本不属于本次业务代码，按协作边界保留不修改；上面的开发命令检查全部项目源代码与测试。

2026-10-02 分页证据阶段：上述 Python 3.12.14 环境，完整 pytest **315 项离线测试通过**；项目代码 Ruff、格式检查及 `git diff --check` 通过。覆盖四份真实列表、分页结构/数字/控制破坏及明确隐藏的证据、来源 URL 与自链、非 25 条/旧日期/全已知页面、离线入库及 CLI/reparse 输出、v1→v2 同摘要详情产物。核对全部 research、schema 和历史迁移的文件摘要不变，两份详情的完整规范化内容及内容摘要也与 v1 一致。没有联网、遍历网站、改变数据库结构或提交 Git。全仓库 Ruff 仍只有上段的 21 项研究脚本问题和 2 个格式问题；保留未改。

2026-10-02 HTTP 衔接状态阶段：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1，依赖未变。完整 pytest **375 项离线测试通过**（保留 315 项、新增 60 项）；`ruff check src tests`、`ruff format --check src tests`（35 个 Python 文件）与 `git diff --check` 通过。0002→0003 保留旧通知/版本/原文/响应，旧缓存资格未知；重复初始化与迁移异常回滚通过。测试覆盖 304 精确绑定及头冲突、丢失/损坏原文的完整获取结果、新 200 解析失败不回退、规则升级/获取时间保留、业务/资源/due/冷却组合回滚、重开库重建待办、完整覆盖与运行部分失败独立，以及所有 CLI 写入争锁。

两个真实 POSIX 子进程验证争锁拒绝、正常退出及 SIGTERM 后锁可再次获取；这仅验证 OS 锁释放，不是数据库进程崩溃恢复实验。数据库中断仍是正常进程内 SQL 触发器/异常注入与关闭重开；未验证真实网络、HTTP 条件头发送/回退、数据库 kill/断电或 Linux/Windows。核对 28 个 research/Parser/rawstore/0001/0002 文件摘要不变，保留之前分页节点修改。全仓库 Ruff 再次执行，仍只有前述研究脚本 21 项 lint 与 2 个格式问题，按边界未修改；没有提交、推送或创建 PR。

2026-10-03 有界 Fetcher 交付：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1、HTTPX 0.28.1，依赖未变。`uv sync --locked --offline`、完整 pytest **530 项通过**（原有 375 项、新增 155 项）、`ruff check src tests`、`ruff format --check src tests`（36 个 Python 文件）及 `git diff --check` 通过；CLI 帮助/示例配置检查仍正确。新增测试使用 MockTransport、自定义 SyncByteStream 和假时钟，连接真实归档/缓存/SQLite 服务：精确 profile/条件头、Cookie/Auth 排除、响应关闭、原始流上限/部分失败、唯一重试层、手动目标校验/循环/累计跳数、共同请求/运行预算、304 完整回退、最新失败原文重处理、原获取时间/新规则保留、完整 Retry-After、冷却写入故障及关闭重开库后的冷却。

没有真实 HTTP 请求、TLS/DNS/slow-drip 测量、整条采集协调器、数据库杀进程或断电实验；协作 deadline 不是硬期限，损坏归档不会自动覆盖。research/fixture、Parser、rawstore、schema、历史迁移和锁文件保持不变，无新增依赖；下一交付再连接扫描、详情与运行摘要。全仓库研究脚本问题保留，开发检查范围仍为 src/tests。

2026-10-04 单次采集交付：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1、HTTPX 0.28.1、pytest 8.4.2，依赖未变。`uv sync --locked --offline`、安装入口的采集帮助/示例配置校验、完整 pytest **601 项通过**（保留 530 项、新增 71 项）、`ruff check src tests`、`ruff format --check src tests`（40 个 Python 文件）及 `git diff --check` 通过。

新增离线验证将 MockTransport 连接实际 Fetcher、归档、缓存、Parser 和 SQLite：两个真实列表共 50 个身份、重复新增 0、完整链及首页复核、已知首页后的新通知、循环/跳页/漂移/预算中止、304 重处理失败原文及文件丢失后的原预算修复、旧成功首次复查、普通详情失败继续与版本保留、按到期时间推进待办、冷却跨运行保留、数据库/失败登记/运行收尾故障明确终止。列表尝试意图、实际发送起点和响应接收时间分别验证。

本交付已完成协调器与 CLI，未进行真实网站请求、数据库杀进程或断电实验；故障注入与关闭重开库不能替代这些实验。research/fixture、Parser、rawstore、schema、历史迁移、依赖及锁实现未改，无 Git 提交或推送。下一交付再做真正的终止/重启恢复验证及少量低频实采，当前在此节点停止。

2026-10-05 恢复验证交付：同一 macOS 26.6.2 / CPython 3.12.14 环境，`uv sync --locked --offline`、CLI 帮助及示例配置校验通过。完整 pytest **615 项通过**（保留 601 项，新增 9 项跨运行集成测试及 5 项真实 SIGKILL/新进程恢复测试）；`ruff check src tests`、`ruff format --check src tests`（43 个 Python 文件）和 `git diff --check` 通过。测试仍完全离线；终止实验运行真实协调器、归档与 SQLite，但 HTTP 为模拟，不等于断电验证。

随后临时目录三轮低频实采共 **9 次真实 GET**，最短请求间隔 5.006 秒，得到 25 个通知身份、4 个成功版本、5 个不同原文文件和 21 条待办。首页 304 的 Vary 变化要求完整回退，第二轮达到请求预算后明确中断，第三轮在同一库继续；没有重复身份或版本，也没有误报完整扫描成功。结果、有限样本与实验边界见 [恢复验证记录](docs/recovery-validation.md) 和 [实采摘要](docs/validation/2026-10-05-live.json)。本交付未改生产代码，核对 32 个 research/Parser/schema/迁移/依赖文件摘要不变；无提交、推送或 PR。本轮三个交付全部完成，在此停止，尚未开展定时运行、邮件或长期观察。

2026-10-05 定时运行与诊断节点：同一 macOS / Python 3.12.14 环境，完整 pytest **715 项离线测试通过**（既有 615 项、新增 100 项：处理政策 51、状态/只读 CLI 28、配置/定时入口/日志 12、备份恢复 9）。三项旧恢复场景明确将无关首次待办延期，以继续测量成功基线的失败/回滚边界，不依赖已替换的全局 due 排序。连续十轮饱和批次每轮实际尝试 12/4/4；成功/due/资源标记回滚、政策升级不推迟逾期、304 当前处理时间、缺失/损坏原文恢复校验均通过。

`uv sync --locked --offline`、新命令帮助及示例配置校验、Ruff 与格式检查（src/tests 和 deploy/verify_backup.py，共 51 个 Python 文件）、`git diff --check` 通过。核对 33 个 research/fixture/Parser/schema/迁移/依赖文件摘要不变；未新增依赖或 schema。Linux/systemd 252+ 模板仅核对配置及官方语义，未在目标机启动、未启用定时任务，未进行新校园请求或断电实验；这些仍需部署后的验证与观察。交付处理政策、只读诊断、有限定时模板及独立日志/备份恢复说明后停止，无提交、推送或 PR。

2026-10-06 N0 与 N1 接口稿节点：macOS 26.6.2 arm64、CPython 3.12.14、Pydantic 2.13.5、SQLite 3.53.1、pytest 8.4.2，使用现有虚拟环境，未新增依赖或改锁文件。完整 `.venv/bin/pytest -q` **906 项离线测试通过**（既有 715 项，新增事实 90、决策 59、Profile/CLI 42）；`.venv/bin/ruff check src tests deploy/verify_backup.py`、格式检查（59 个 Python 文件）及 `git diff --check` 通过。

验证五份真实通知的事实/未知项、不同画像、短截止与日期边界、媒体/OR/否定/冲突、额外编号/年龄/处分条件、不支持的时刻/时区与未解决截止声明、首启路线例外、显式更新比较、版本口径校验、原文位置和跨子进程 hash seed 的完整决策确定性。安装入口的 profile-check 与真实 HTML decision-preview 在临时工作目录通过，不创建文件/数据库或获取锁；错误输入与当前 Parser 不支持的竞赛页面明确非零退出。未进行新网站/SMTP 请求、通知数据库迁移或 N1 事务实验；N1 只有明确标注的接口稿。Parser、schema、历史迁移、入库和依赖文件摘要不变；本实现未修改 research，保留并行 Agent 对三份通知/邮件报告的更新与新调研文件。无 Git 提交、推送或 PR，N1 持久化与 N2–N4 留后续交付。
