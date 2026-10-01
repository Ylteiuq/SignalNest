# 单次 HTTP 采集离线实验记录

日期：2026-10-01。运行结果：[results.json](results.json)。脚本：[offline_checks.py](offline_checks.py)。这些是独立研究探针，非应用实现/生产schema；不导入fetcher、rawstore或业务协调服务，仅调用现有纯Parser以复现问题。

## 环境与运行

- macOS26.6.2 arm64，CPython3.12.14，HTTPX0.28.1，httpcore1.0.9，现有 `.venv`，不安装依赖。
- 从仓库根运行：`.venv/bin/python research/experiments/ingestion/offline_checks.py`。保存结果可重定向到新的JSON或覆盖本目录结果。临时文件和独立SQLite数据库由TemporaryDirectory清理。
- 真实页面捕获是单独脚本 `capture_pages.py`，**不要当离线实验运行**。本次只执行两次成功GET：首页和尾页，间隔4秒，无自动跳转/重试/详情/附件。第一次沙箱DNS失败未保存fixture，之后获准网络执行成功。运行会写指定fixture名称；复采时应修改为新名称，勿覆盖本次证据。
- 每份新fixture有相邻JSON：客户端开始/完成UTC、requested/final URL、status、实际bytes、SHA-256、请求头、白名单响应头。Cookie及非必要头完全省略。HTML未解析重写。新目录 `research/fixtures/ingestion/`。

## 已执行与结果

24项探针断言通过，结果JSON记录每项证据，不把“pass”解释为生产功能已经实现。v1/v2处理标记是实验数据库中的模拟规则版本，并没有修改或运行一份真实v2 Parser；实际Parser调用使用当前whu-student-notices-v1。

| 实验 | 实际验证范围 |
| --- | --- |
| 缺少paginator | 调用现有parse_list复现25条+None缺陷；pass指成功复现错误行为。 |
| 新首页与真实末页 | 当前Parser分别25条/正确next、13条/None；HTML明确终止标记经独立观察。尚未实现新pagination契约。 |
| 原文提交、业务故障注入回滚、重开DB、304重处理 | 简化两表SQLite模型，普通RuntimeError在业务commit前注入，raw evidence保持、processing回滚；后续304无正文，从校验正确原文处理v1/v2。不是SignalNest schema/service集成。 |
| 无基线/原文丢失/损坏→304 | MockTransport发送304，再发送无条件GET并得到200；检查第二次没有If-None-Match。丢失/损坏模拟的是请求前检查之后仍收到304的恢复分支。 |
| 自动与手动redirect | HTTPX自动跟随同域302会携带原If-None-Match；手动follow_redirects=False重新构造headers则没有。不是跨域真实服务器实验。 |
| stream超限/ReadError | 自定义SyncByteStream在累计字节超小限额或产出一块后异常；context manager关闭stream，无完整正文。 |
| Retry-After | 120秒、未来GMT日期、过往日期、负数/小数/无效输入；延期保留120而不截为15；时间基准固定2026-10-01 08:00UTC。 |
| 请求gate | 假时钟演示所有发送（含跳转/重试）间隔至少3秒；没有实际sleep/socket测量。 |
| 请求策略探针 | 只在实验中的小型状态机：ReadTimeout→200、503×3耗尽、403/501不重试、429直接延期、304走验证；断言物理次数和假时钟gate。jitter固定0.25以便复现；不是HTTPX默认重试行为。 |
| 重定向策略探针 | 跨主机、HTTPS降级、环、第四跳在test-only策略中被拒绝；MockTransport验证禁止目标不再发送。路径/稳定IDallowlist完整规则留给实施验收。 |
| TLS异常分类 | 人工构造ConnectError cause为SSLCertVerificationError，确认可识别cause；没有实际TLS握手。 |

## 未验证与限制

MockTransport直接调用handler，没有DNS、连接池socket、真实TLS/read/write超时，因此不能证明60秒硬deadline、真实slow-drip中断、连接跨请求复用或真实速率限制。SQLite仅正常进程中的故障注入rollback+关闭重开，不证明kill/断电/fsync耐久性。没有运行OS单实例锁实验、压缩炸弹测试、完整协调器扫描或真实站点插删实验。生产验收场景见[设计文档](../../ingestion-design.md#可直接编写的验收场景)。

实验没有修改上游代码、应用源码/测试/锁文件或旧fixture；没有启动changedetection.io。实验中的简单缓存恢复/政策算法为本任务独立编写，不复制上游实现。
