# 计算机学院：显式来源采集

UC 与 CS 复用有界 Fetcher、RawStore、缓存、短事务、扫描覆盖和详情待办机制，但使用明确的来源绑定。`sources.py` 只包含两个固定绑定，不是插件系统。实采暴露 CS 正文的 `vsb_content_501` 包装，已有限补入并升为 `cs-undergrad-notices-v2`；不接受任意后缀或猜正文。后续首周归档失败依据补到 UC v5 / CS v3，见[分类与有限恢复](parser-failure-recovery.md)；政策仍为 v8，身份、分页、正文规范化、schema/迁移/依赖不变。

WSL 已安装冻结发布 `9366fcb`，完成原文恢复、两页小额度实采和专用每小时 timer 启用；邮件保持关闭。实际结果与验收范围见[CS 实机记录](validation/cs-collection-wsl-20261010.md)，不把受限扫描和已排期 timer 称为完整覆盖或连续运行。

后续 2026-10-11 已部署 `bc2567d`，完成真实四页 full、独立启用与后台邮件，见[生产起点](validation/n2-production-wsl-20261011.md)。没有近期首启候选，不补发历史；七天验收仍待完成。上面的关闭状态是前一节点记录。

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

Linux/systemd 模板：`signalnest-cs@.service` 使用 `/etc/signalnest/cs.toml`、`/var/lib/signalnest-cs`；`signalnest-cs-regular.timer` 每小时 Asia/Shanghai :15 触发一次，`signalnest-cs-full.timer` 每日 01:45 完整扫描。采集和新增独立 `signalnest-cs-mail.service` 都读取可选 SMTP EnvironmentFile；后台邮件仍必须由本配置显式开启，不会因环境文件存在而发送。CS 配置将 storage 两路径设为该绝对数据目录；初始化/迁移及写入仍统一服务账号。安装后先校验模板、手动执行一次服务及其有限摘要，再启用专用 timer。升级共享程序时同时停 UC/CS 所有采集、邮件 timer 和服务，分别备份实例；邮件关闭验收期间已有 UC 无 SMTP drop-in 保留。

CS 示例仍为 mail_runtime.enabled=false，不会因接入来源自动复用 UC 政策、个人画像或邮件资格。2026-10-11 已完成四页真实完整扫描与首页复核，58 个通知身份均已登记；其中此前未遍历的 28 个身份仍按详情待办继续处理。目标实例在用户确认画像、候选预览和显式 activation 后独立开启小额度邮件；新增模板本身不自动启用它。

独立 CS 邮件 timer 在上海时间每小时 04、09…59 分钟推进冻结积压，UC 保持 02、07…57；各库按自己的锁和暂停状态运行。CS 采集服务 8 分钟上限适合当前 full 300 秒加小额度发送 120 秒，调整预算须同步核对上限。只读观察模板的实例名为 `cs`，默认仍记录测试画像未确认，详见[连续观察](rollout.md)。

## 验证范围

初次 CS v2 接受直接位于 div.content 内的唯一 `div#vsb_content` 或 `div#vsb_content_501`，仍要求唯一直接 `.v_news_content`。501 包装来自实采已归档的 66441/65481/65321，原始响应保留在目标实例，来源 URL 和摘要见验收记录；不追加研究文件。离线回归以既有真实正文的内存副本换包装，明确是工程适配输入：内容及摘要完全一致，混合重复包装、未观察的后缀、脚本空正文仍失败。列表规则未改；沿用单个 CS Parser 版本，所以列表运行/处理标记和详情产物都使用 v2，新版本不进入内容摘要。旧 v1 版本行保留，维护重解析不制造 live 更新。

默认测试用模拟 HTTP 串起实际 RawStore/缓存/Parser/SQLite，覆盖 CS 200/304、失败原文再次处理、不同解析版本、原文丢失/损坏、真实详情、JSP 受限跳转、独立来源/冷却、未适配引用继续分页、提交故障回滚、通知启用/live 重复处理和 CLI。研究仅有真实第 1/2/4 页，因此完整链测试的第 3 页明确为工程构造，不伪报为真实完整 CS 扫描。真实请求、定时服务和实际结果另记验证记录；模拟传输不称为真实网络或断电实验。

最初来源绑定在 2026-10-10 当时含更多本地样本的工作树上通过 2283 项 pytest（125.39 秒）及 Ruff/格式检查 132 文件。这是历史开发记录；不将其用作当前选样/脱敏工作树的验证结果。

上述为最初来源绑定的冻结检查。随后仓库整理为选样/脱敏 fixture 和摘要评估格式；保留整理后的提交和文件，不恢复被移除的详细结果。CS v2 包装修正再加 8 项回归，对当前仓库完整运行 **2288 项测试通过**（120.01 秒），Ruff/格式覆盖 133 文件。六组共 31 情境重新重放，当前摘要的标签、manifest 摘要和统计不变，只更新 Parser 版本/源码证据；政策仍为 v8。历史实采原文留在目标受限归档，不加入 research；没有改变 UC Parser、分页、schema 或迁移。

首周真实 39801/4204 的完整 200 使用 wrapper_2 / wrapper_6，v3 在同一直接 content 层级有限加入，两份脱敏原文投影与实际归档重解析测试见[失败恢复](parser-failure-recovery.md)。本文 v2 测试和部署时间仍属历史证据，列表算法未改，Parser 标识随来源一并升级。
