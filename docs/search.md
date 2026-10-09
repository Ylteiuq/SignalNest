# 新 N3：基础历史搜索

搜索是当前成功通知的派生能力，不改变通知政策 v5、Parser、内容摘要或冻结邮件。
没有 Embedding、向量数据库、LLM 或自然语言日期解析。CLI 使用已有实例配置定位数据库。

## 使用

```sh
signalnest storage-init --config /etc/signalnest/signalnest.toml
# 从 0006 升级的库必须显式重建一次；新库的正文成功处理自动增量同步。
signalnest search-rebuild --config /etc/signalnest/signalnest.toml
signalnest search --config /etc/signalnest/signalnest.toml --query '助教招聘' --limit 10
signalnest search --config /etc/signalnest/signalnest.toml --query '科研训练 报名'
signalnest search --config /etc/signalnest/signalnest.toml --query '竞赛' \
  --from 2024-06-01 --to 2024-07-01 --source-id whu-undergrad-student
signalnest notice-show --config /etc/signalnest/signalnest.toml --document-id 12
```

`document_id` 从查询结果获取，示例 12 不是固定业务身份。JSON 返回标题、站点发布日期、
原始成功版本的页面 URL、最多约 240 字的正文片段、document/source/source_document/version ID。
详情返回同一当前成功版本的正文、Parser 版本、摘要与最近处理诊断。
HTML 只是保留的解析证据，未作安全展示清洗，后续界面不能直接信任并渲染它。

日期过滤以站点日历日期为准，包含上下界；不使用 fetched_at/parsed_at。
来源为精确 source_id，省略则查询全部已保存来源。`--limit` 为 1–100，默认 20；
`--offset` 为 0–10000，默认 0。没有匹配是成功空结果；参数错误退出 2，
未初始化/旧 schema、索引陈旧、未处理通知或查询存储错误退出 1，且不回显 SQL/正文。

## 词法规则与选择依据

`index_version=literal-trigram-v1`：索引标题与正文文本，NFC、casefold 和连续空白折叠。
保存的 NoticeContent 不变，索引不读取 HTML、图片或附件文件内容。
查询总长最多 200 字、最多 8 个去重词组；空格分组为 AND。
`query_version=literal-alias-v1` 只有两个明确的精确词组展开：

| 查询词组 | 组内 OR 字面候选 |
| --- | --- |
| 助教招聘 | 助教招聘、助教选聘、招聘助教、选聘助教、助教招募 |
| 竞赛 | 竞赛、大赛 |

裸词“助教”和其他词保持字面语义；不自动认定招聘、资格符合或截止未到。
结果输出原 query、实际 expanded_terms 和版本。引号、SQL/FTS 操作符作为字面数据，
不向用户暴露 FTS 查询语言。参数绑定，不用用户字符串拼 SQL 语句。

所有候选长度至少三字的组用 FTS5 trigram 取得候选，再字面复核；
含短词的组用参数化 instr 匹配。纯短词检索可能扫描全体成功正文，当前小语料可接受，
尚未测量大规模延迟或证明性能优势。长词排序用 BM25（标题权重 5、正文 1），
纯短词按命中标题组数优先；随后按站点日期倒序、document_id 排定平局。
字面词法无法识别任意同义词、隔断表达或图片里的字；正文顺带提及会误检。
9 篇真实原文的方法比较、15 个查询的相关性理由和 Recall@K 见[检索评估](search-evaluation.md)，
工程标签待用户确认，不能外推所有校园通知的质量。

## 派生索引与事务

0007 仅新增 `search_documents`（每个 document 一行，明确 version_id）及 SQLite 管理的
`search_fts` / shadow tables、三个同步触发器。普通表有外键绑定该通知的版本，
FTS5/trigram 能力由真实建表校验；不支持时整个升级事务失败，不悄悄降低能力。
0001–0006 不变，旧通知、原文、版本和邮件保留；升级不自动回放 Parser 或补发邮件。

`sync_document_in_transaction(connection, document_id)` 接在现有详情成功事务中：
正文版本、current_version_id、成功/due、资源标记、live 基线/事件/决策/投递意图和派生索引一起提交。
只处理已规范化 JSON，没有网络、文件读取或 Parser。
索引失败视为系统性提交失败，整组回滚，再独立登记有限错误；不把旧正文标成成功更新。
复查失败仍保留旧当前成功版本和可查索引；A→B→A 根据当前指针同步，不能按最大版本 ID。
离线导入与显式历史 reparse 也走此路径，后者可以有意改变当前正文，但不制造 live 更新邮件。

`rebuild_index(engine)` 必须由调用方持统一 writer_lock；CLI 已持锁。
它在一个本地事务内从当前成功版本 JSON 重建，不重新获取原文、运行 Parser 或重评邮件。
失败全体回滚，原文/业务成功仍是权威事实。当前面向小型个人库，全量重建不分批。

`search_notices(engine, SearchQuery)` / `get_notice(engine, document_id)` 是独立库入口。
CLI 以 SQLite mode=ro + query_only 打开最新已初始化库，无写锁/隐式迁移。
搜索校验全部当前指针与索引版本，缺失或陈旧报 search_index_stale，要求显式重建；
单条详情直接读权威成功版本，不依赖派生索引。
不同分页调用是不同只读快照，期间新提交可能改变 offset 排序；单次结果与 total 同一快照。
每次查询不会全面检查 FTS postings 的任意损坏，显式重建可修复已验证的 postings 丢失。

## 备份与恢复

SQLite backup 包含普通索引和 FTS 内部表，仍须按[一致备份步骤](mail-backup.md)保留 raw 与邮件状态。
备份校验器验证权威业务/邮件/原文及 SQLite/FK 完整性，同时检查搜索必要 schema 对象。
缺结构返回 backup_search_schema_missing，不能用重建内容来修复已丢的表或触发器；应使用完整备份。
校验不要求派生内容最新，也不保证诊断任意 postings 语义损坏。
旧备份使用对应代码校验，另建副本升级后显式 search-rebuild。
索引陈旧不允许查询暗中写入；隔离恢复副本先暂停发送、不注入凭据，再重建、查询并核对结果。
