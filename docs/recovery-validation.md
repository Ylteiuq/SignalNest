# 恢复与少量实采验证

日期：2026-10-05。本交付验证已实现的 `crawl_once`，没有增加调度、邮件、并发或新的持久状态。生产代码、Parser v2、数据库 schema、历史迁移与 research/fixture 均未改；保留之前交付的未提交修改。

## 验证层次

| 层次 | 实际执行 | 能说明什么 |
| --- | --- | --- |
| 离线集成 | MockTransport + 真实 Fetcher/RawStore/缓存/Parser/临时 SQLite；显式关闭重开连接 | 跨运行的状态、绑定、去重、冷却与受限覆盖行为；没有真实 socket |
| 进程终止 | 父进程等待边界标记，对正在运行生产协调器的子进程发送 SIGKILL；全新子进程恢复及再次复查 | 不执行 finally 的中断、真实 SQLite 回滚与持久事实恢复；HTTP 仍是模拟的 |
| 少量实采 | 正常生产 CLI、TLS 校验开启、临时数据目录，三轮共 9 次 WHU GET | 当前少量真实页面可以完成归档、解析、业务提交和预算中断后继续处理 |

这三个层次不能合并称作断电恢复验证。没有停止机器、破坏磁盘或在真实 HTTP 请求中杀进程，也没有验证长期运行、Linux/Windows、真实 slow-drip 超时或源站分页插删。

## 离线恢复回归

原有测试保留。新增 `tests/test_crawl_recovery.py` 的 9 项测试连接生产组件，覆盖：

- 详情 429 后重开数据库，新的运行仍遵守整个来源冷却；到期后恢复待办，保留旧成功版本。
- 最新 200 是错误模板时，后续 304 仍绑定该原文并再次明确失败，不改用旧成功正文；随后有效 200 可以恢复。
- 跨运行 A → B → A，当前指针回到 A，之后 304 不增加同内容/同规则版本。
- 已有完整扫描时间在后续循环、总页数漂移、页数或请求预算中止后保持原值。
- 304 登记后原文损坏时完整获取不同原文，保留损坏文件和错误证据，不默默覆盖。
- 将本次实采的 `Vary` 缩减现象重放：保守完整回退计入共享请求额度；未发送详情仍待处理，下次有界运行继续。

缺失原文、同摘要损坏拒绝覆盖、已知首页后续新身份、普通详情失败后继续、失败登记不可用、首页复核与各类预算检查仍由现有 `test_crawling.py` 等覆盖。没有把研究脚本的简化模型当作这些验证结果。

## 五项真实 SIGKILL 实验

入口为 `tests/test_process_recovery.py`，辅助进程为 `tests/helpers/crawl_recovery_worker.py`。辅助程序运行生产 `crawl_once`，屏蔽 socket，只用钩子测量边界并暂停；父进程实际发送 SIGKILL 并确认退出码为 `-SIGKILL`。标记文件完整发布后才终止，等待及清理都有超时。

| 终止边界 | 终止后的数据库事实 | 新进程恢复结果 |
| --- | --- | --- |
| 详情 200 原文登记后 | 完整原文和响应已提交，尚无详情成功产物 | 304 绑定该 200，原获取时间不变，成功入库 |
| 详情业务提交前 | 标记已测到同一事务内版本、current、due、资源处理标记；SIGKILL 后整组回滚 | 从持久待办重新处理，无半成功状态 |
| 所有列表页及首页复核完成、覆盖提交前 | 条目保留，coverage 仍 pending，完整扫描时间为空 | 旧运行记 interrupted，新运行重扫后才登记 complete |
| coverage 已提交、运行收尾前 | complete 事实和已成功详情保留，运行仍 running | 恢复旧运行状态，保留已成立覆盖，重复运行不增版本 |
| 已有 A 成功版本，处理 B 的提交前 | B 原文已提交；B 的版本/current/due/资源标记回滚，A 仍当前且成功时间不变 | 304 绑定最新 B 原文，成功切换至 B，旧 A 保留 |

每个案例恢复后再启动第三个独立进程，实际复查到期详情并验证通知/版本唯一性、正文文件摘要和原获取时间。锁由内核释放，恢复由数据库事实驱动，不依赖旧进程内存。尚未覆盖文件发布途中或磁盘/文件系统断电。

## 少量真实页面与运行比较

完整离线与终止测试通过后才实采。沙箱最初 DNS 解析失败，有限尝试明确返回 connect_error，未取得响应/正文/成功版本；该失败库保留在 `/private/tmp/signalnest-live-20261005-wat13waz/`。获得网络执行权限后另建干净临时库，不把失败尝试伪装成成功获取。

实际使用 `/private/tmp/signalnest-live-20261005-gqqwm_q1/signalnest.toml`，库与原文都在该临时目录。普通 CLI：

```sh
signalnest crawl-once --config "$trial_dir/signalnest.toml" \
  --scan limited --max-pages 1 --max-details 2 --max-requests 3 \
  --run-seconds 40 --resource-seconds 15 --max-body-bytes 1048576
```

TOML 使用 connect=5 秒、read=10 秒、请求间隔 5 秒，User-Agent=`SignalNest/0.1 (bounded verification)`，Accept=`text/html`，Accept-Encoding=`identity`。每轮请求上限包含全部回退；第二轮前等候至少 5 秒。没有下载附件/图片、修改验证码状态或关闭 TLS。

| 运行 | 新身份 | 详情成功 | 请求数 | 运行结果 | 剩余到期待办 |
| --- | --- | --- | --- | --- | --- |
| 1 | 25 | 2 | 3 | succeeded | 23 |
| 2 | 0 | 1 | 3 | interrupted / request_limit | 22 |
| 3（相同上限，仅 max-details=1） | 0 | 1 | 3 | succeeded | 21 |

第二轮确实收到首页 304，但其 `Vary: User-Agent` 与原 200 的 `Vary: User-Agent,Accept-Encoding` 不同。当前保守规则要求一次完整 200 回退，它占用请求额度，随后仅一篇详情成功，另一篇在发送前被预算阻止。第三轮沿用原库和相同物理请求上限，仅补一篇，验证真实中断后可继续；没有为凑成功扩大额度或放宽缓存校验。这是本次站点观察，不据此认定上游存在缺陷。

三轮首页均为 25 行；新增身份为 25 → 0 → 0。第二/三轮处理其他待办，版本总数 2 → 3 → 4 属于正常推进，不是重复版本；最初两篇成功的当前指针保持。实采没有复查这两篇成功详情，同文复查去重由离线及真实子进程测试证明。全部扫描故意 limited，last_complete_scan_at/bootstrap_completed_at 始终为空，不能声称遍历了所有页面。

最终 25 通知、4 版本、9 响应证据、5 个不同原文文件、21 待办。两条 304 均无正文并绑定具体 200；三次首页 200 相同 bytes 复用一个文件。所有正文重新通过 RawStore 摘要校验；SQLite integrity_check=ok、foreign_key_check 无错误。事件记录的最短请求开始间隔为 5.006 秒。

持久摘要及响应白名单元数据见 [实采结果 JSON](validation/2026-10-05-live.json)。完整原文、数据库、三次 stdout/stderr 和完整比较记录保留在上述临时目录，便于检查；项目文档不包含整段网页或 Cookie。临时目录不是长期保存承诺，清理前可自行保留需要的原文。

## 复现与后续边界

```sh
uv run --locked pytest -q
uv run --locked pytest -q tests/test_process_recovery.py tests/test_crawl_recovery.py
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
```

默认测试完全离线，实采不是 pytest 自动步骤。复现实采须新建临时目录、复制示例配置并将上述 HTTP 设置写入该目录配置，再显式 storage-init；以有限额度运行，遇持久冷却须停止。不要使用个人正式库来复现终止实验。

实际验证环境：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1、HTTPX 0.28.1、pytest 8.4.2，依赖未变。完整 pytest **615 项通过**（保留 601 项、新增 14 项）；`uv sync --locked --offline`、CLI 帮助/示例配置校验、Ruff 检查和格式检查（43 个 Python 文件）、`git diff --check` 均通过。32 个受保护的 research/Parser/schema/迁移/依赖文件摘要不变。

这里只证明所列边界及有限真实样本；协作式预算、保守缓存回退成本、同摘要损坏文件须明确处理、分页非原子快照等限制仍存在。下一步是最简单的定时运行与观察，再设计 Email 发送记录与补偿；本交付不实现它们。
