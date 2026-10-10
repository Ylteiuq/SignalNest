# 计算机学院：显式来源采集

UC 与 CS 复用有界 Fetcher、RawStore、缓存、短事务、扫描覆盖和详情待办机制，但使用明确的来源绑定。`sources.py` 只包含两个固定绑定，不是插件系统。Parser 的选择器、身份、分页和规范化未改，CS 仍为 `cs-undergrad-notices-v1`、UC 为 v4，政策仍为 v8；schema/迁移/依赖不变。

| 配置/许可 | UC | CS |
| --- | --- | --- |
| source.parser | whu-student-notices（兼容默认） | cs-undergrad-notices（必须显式） |
| 示例 source.id | whu-undergrad-student | whu-cs-undergrad-teaching |
| HTTPS/443 主机 | uc.whu.edu.cn | cs.whu.edu.cn |
| 首页及正整数静态子页 | /tzgg/xstz.htm | /xwdt/tzgg/bkjx.htm |
| 栏目 | 1517 | 1074 |
| 旧式详情路由 | /2022/show.jsp | /content.jsp |

配置必须给所选来源的首页，不能把中间页当扫描入口；未知 Parser、相反的已知 source.id、错误主机/路径/查询明确失败。自定义 source.id 仍是持久命名空间，不得随意更改或复用为另一个来源。没有主机推断、解析失败换 Parser 或 JSP 改写为新路由的隐式分支。

## 可使用的入口

```sh
cp config.cs.example.toml cs.toml
uv run --locked signalnest config-check --config cs.toml
uv run --locked signalnest storage-init --config cs.toml
# 以下两条会联网；先用第一条的小额度验收。
uv run --locked signalnest crawl-once --config cs.toml \
  --scan limited --max-pages 1 --max-details 1 --max-requests 6 --run-seconds 120
uv run --locked signalnest scheduled-run --config cs.toml --mode regular
uv run --locked signalnest status --config cs.toml
```

数据路径相对配置所在目录。示例独立存入 data-cs，普通任务最多两页、两篇详情、12 个物理请求、180 秒；每次物理请求间隔 5 秒。重试、跳转、修复均消耗同一预算。调度频率由外部 timer 控制，不由 request_interval 决定。

`import-page` / `reparse` 也使用 `[source].parser`，元数据 source.id 必须匹配。保存研究 HTML 时仍保留其已知获取时间，不假造网络；维护重解析仍不制造 live 更新。纯 `decision-preview --parser cs-undergrad-notices` 独立于采集配置。

库入口 `crawl_once` 根据配置绑定全链路，包含 start_run 的 parser_version、列表/详情缓存重处理，以及通知比较旧原文的 Parser。直接使用 `HttpFetcher` 时明确传 `source_parser="cs-undergrad-notices"`；省略仍只允许 UC。纯 Parser 和业务函数的显式注入接口保留。

## 缓存、失败与覆盖

缓存键仍是 source + 实际请求 URI + RequestProfile；旧式查询参数顺序和无关参数保留，不能用 1074:文章号代替 URI。实际 User-Agent / Accept / Accept-Encoding 逐项匹配 profile。每跳重新校验所选主机、栏目和同一文章身份，登录、外站、其他栏目/文章、降级和片段均拒绝。JSP 允许发送不是已证明匿名正文有效；错误模板的 200 仍由 CS Parser 明确失败。

304 仍无正文，绑定请求前选定的具体完整 200。最新 200 解析失败后继续处理它，不退回旧成功版本；丢失/损坏归档触发完整获取需求。重新获取相同 bytes 对应损坏的既有摘要文件仍明确失败，需人工隔离损坏文件，不默默覆盖。新 Parser 可重新处理同一原文，原 fetched_at 不变。

列表与未适配引用、日期证据和资源标记整页原子提交；详情版本、成功指针/due、搜索索引、live 事件/决策和资源标记复用已有成功事务。普通单篇失败保留旧成功结果、继续下一篇；数据库无法可靠登记等系统错误停止本轮。没有新增邮件补发/政策更新行为。

有限扫描始终 limited。full 仍要实际连续页码、稳定总页数/尾页、无 URI 循环、全部混合行登记及独立首页复核后，才能保存 complete 与成功时间。已知条目、旧日期、短页不终止扫描。未适配引用登记后不加入详情请求，不能阻断后续分页。

## 单来源通知与部署

当前每库仅一个通知启用通道。部署为两个独立实例：UC 保留原配置/database/data/启用边界；CS 单独 config/database/data/实例锁、运行事实、冷却和通知基线。共享程序安装不共享业务状态。采集前发现当前数据库已启用另一来源的通知即拒绝，避免网络和部分写入后才发现冲突；没有跨来源邮件聚合。

Linux/systemd 模板：`signalnest-cs@.service` 使用 `/etc/signalnest/cs.toml`、`/var/lib/signalnest-cs`，不读取 SMTP EnvironmentFile；`signalnest-cs-regular.timer` 每小时 Asia/Shanghai :15 触发一次。CS 配置将 storage 两路径设为该绝对数据目录；初始化/迁移及写入仍统一服务账号。安装后先校验模板、手动执行一次服务及其有限摘要，再启用专用 timer。升级共享程序时同时停 UC/CS timer 和服务，分别备份实例；已有 UC 无 SMTP drop-in 保留。

CS mail_runtime.enabled=false，且无启用通道；不会因接入来源自动复用 UC 政策、个人画像或邮件资格。后续需本来源真实完整扫描、画像核对、首启候选预览和显式 activation，再决定小额度生产邮件。本阶段不启用它。

## 验证范围

默认测试用模拟 HTTP 串起实际 RawStore/缓存/Parser/SQLite，覆盖 CS 200/304、失败原文再次处理、不同解析版本、原文丢失/损坏、真实详情、JSP 受限跳转、独立来源/冷却、未适配引用继续分页、提交故障回滚、通知启用/live 重复处理和 CLI。研究仅有真实第 1/2/4 页，因此完整链测试的第 3 页明确为工程构造，不伪报为真实完整 CS 扫描。真实请求、定时服务和实际结果另记验证记录；模拟传输不称为真实网络或断电实验。

CS 独立采集功能在 2026-10-10 的原始研究样本集合上完成过一次验证：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1，pytest 2283 项通过（125.39 秒），Ruff 与格式检查覆盖 132 个 Python 文件。该次结果属于当时包含更多本地样本的工作树。本次合并前已缩减并脱敏 fixture、更新依赖这些 fixture 的回归用例；本次样本整理后没有重跑完整测试。新版 Parser 的同原文 304 和原文外区域变化等历史验证结果仍可作为开发记录，不能视作当前精简工作树的新一轮通过记录。
