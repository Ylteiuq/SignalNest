# 未适配列表引用登记

武汉大学本科生院列表中的有效 HTTP(S) 条目不一定属于已实现的 1517 正文模板。
2026-10-09 保存的第 6、7 页各有 24 条本站通知及 1 条栏目外引用：SIM 页面与带完整查询参数的 EK 页面。
本轮仅使用已有原文离线验证，不请求这两个正文，也不把它们的 URL 猜成本站文章身份。

## Parser 与契约

`whu-student-notices-v4` 将有效但未适配的目标返回为 `PendingReference`：

- `raw_href` 是 HTML 解析后取得的 href 属性值；HTML 实体已解码，原始字节仍在归档中。
- `resolved_url` 相对于输入页面最终 URL 解析，保留完整路径、查询参数及顺序、片段。
- 列表标题、可见发布日期、从 0 开始的 `row_index` 保留原行信息。
- `reference_kind` 为 external、unsupported_column 或 unsupported_route；状态为 pending_adapter。

`ListPage.entries` 继续只含可用稳定身份的本站通知，`next_page_url` 和分页证据读取方式不变。
新增 `references`、`row_count` 和 `ordered_rows()`；混合页、重复 URL 行以及只有未适配引用的页面都保留实际行序。
entries 和 references 同时为空仍失败，不能用丢弃条目的方式声称整页成功。

有效的外部主机、未适配栏目或未知路由可登记。已知本站文章路由缺失、重复或冲突的身份参数，
非法 URL、空标题、无效日期、锚点/非 HTTP 引用以及损坏分页仍使整页抛出原有限 `ParseError`。
不捕获任意解析异常再将其降级为外链。详情 Parser 的身份要求保持严格。

沿用一个站点 Parser 版本，因此本站详情产物也标为 v4；详情选择器、规范化和
`NoticeContent.content_sha256()` 规则没有改变。相同内容可保留新版 Parser 产物。
同原文的 live 规则升级沿用已有静默基线更新行为，maintenance 重解析不产生更新邮件。
通知事实、决策政策仍为 v6，EMS Parser 仍为 v1，旧决策及冻结邮件不改写。

## 持久化与去重

新增迁移 `0008_list_references`；先备份，再用原配置运行 `storage-init`，不自动升级。
0001–0007 不变；本次只创建 `discovered_references` 并为 ingestion_runs 增加可空 coverage_evidence，
没有重建现有表。旧通知、版本、缓存、邮件与运行数据保留，旧扫描没有虚构的响应链证据。

引用唯一键是 `(source_id, candidate_key)`：candidate_key 为
`link:v1:` 加 `http-url-v1` 规范化目标 URL 的 UTF-8 SHA-256。
HttpUrl 规范化主机和默认端口，保留查询顺序、无关参数、HTTP/HTTPS 差异及片段；
不根据文章号合并不同主机，不擅自删除 EK 的 query，也不把这个候选键当成通知身份。
同 URL 不同标题复用引用；同页重复行增加观察行数，但只保留一个候选。

首次登记保留 first_seen_at、发现 origin/run、实际 200 正文和本次观察响应、行位置及 Parser 版本。
最近登记更新列表标签、日期、href、响应/run/位置/Parser 和 last_seen_at；
较早处理时间的历史回放不能覆盖较新的标签。日期来自列表，不从发现时间推导；
first_seen_at 是本地登记时间，fetched_at 仍只有 raw_responses 中的实际获取证据。
这些字段用于追溯与未来适配，不另建详情 due、失败重试或引用抓取队列。

`discover_page_in_transaction` 在一个已有 Connection 中提交本站通知、待适配引用、
本站通知的日期证据、原响应/资源处理标记及来源整页登记时间。引用要求归档列表的明确 response_id；
便利入口 discover_page/import_page/process_response 仍使用一个短事务。
任何一个登记失败，整页业务事务回滚；原始文件与独立响应证据保留，失败分类照常登记。
文件读取和 Parser 都在事务外。304 复用绑定的 200，分别保存 body/observed 响应，不生成正文或新获取时间。

引用不进入 documents、pending_documents、正文搜索、通知事件或邮件计划，不获取外部正文。
首次发现来源与原始列表证据保留，避免将来接入适配器时因后来才解析正文而把旧引用默认当成新通知。
引用升级为正文通知、身份映射、通知启用集合的衔接需另行显式设计；本轮没有实现或声称完成这种升级。

## 完整覆盖的证据

抓取器许可范围、请求预算和分页规则不扩展。协调器继续沿真实 next 请求后页，
验证 URI 循环、连续页码、总数/尾页一致性、整页提交及首页复核。
复核比较完整 ListPage，包含引用的目标、标题、日期、href、位置以及本站通知；
只改变外链也使 coverage 中断，不因已知通知或较少行数提前停止。

生产 `ScanCompletion` 增加 RegisteredListPage 链与独立 home_recheck。
每页保存原正文/观察响应 ID、链请求 URL、实际响应请求 URL（区分重定向）、最终 URL、
实际 next、分页证据、有序 `(notice|reference, key)` 行及完整结构化列表摘要。
摘要是列表复核证据，不替代原始字节摘要或 NoticeContent 摘要。
提交 complete 的短事务校验响应来源/类型/处理成功、304 绑定，以及每个行键确已登记。
coverage_evidence 与来源完整扫描成功时间一起提交；收尾失败不能留下成功时间。

旧的仅声明 ScanCompletion 调用保留兼容，但不具备响应/成员证据；新生产协调器始终传入完整记录。
接口不执行网络遍历或在事务内重解析原文；实际链验证仍由协调器负责。
完整列表覆盖不表示所有正文都已处理：未适配引用数量另行展示。
首页复核也不是网站的原子快照，无法证明其他页面在扫描期间始终未变。

## 查看与恢复

```sh
uv run --locked signalnest storage-init --config signalnest.toml
uv run --locked signalnest references-list --config signalnest.toml --limit 20 --offset 0
uv run --locked signalnest status --config signalnest.toml
# 使用之前失败的实际列表 response_id；保留原获取时间，重新完整解析并整页登记。
uv run --locked signalnest reparse --config signalnest.toml --response-id 42
```

`references-list` 只读、按 ID 分页，返回完整 URL、列表标签和首末证据；不获取写锁、联网、修复或迁移。
limit 为 1–100，offset 为 0–10000；库函数 `list_references` 可复用。
status 增加 unadapted_references，采集摘要增加 scanned_references、new_references、remaining_unadapted_references。
scanned_entries/registered_row_count 包含两种行，重复行照计且不含首页复核；
discovered_count 仍仅表示本站条目处理数，new_documents/new_references 分别是本轮新增的唯一身份/引用。
新增引用计数包含首页复核登记，不等于扫描观察行数。详情待办与待适配引用分开，不将后者伪装成成功正文。
日志只增加有限计数；完整查询地址仅在显式查看输出中出现，不进入日志。

之前因外链处理失败的最新缓存原文可以在本规则下重处理，包括收到绑定 304 后重处理；
相同 bytes 不证明上次整页成功。升级不会自己恢复旧失败响应或声称完整扫描，需显式 reparse 或后续实际采集。
备份校验器核验引用键、首末列表绑定、原文，以及完整扫描链的成员和首页声明。
历史真实采集、实际进程终止及邮件验收记录保持原样；本轮验证是离线模拟 HTTP、实际文件/SQLite 和故障注入，
没有新增实采、目标机器升级或断电实验。
