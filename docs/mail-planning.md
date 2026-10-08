# N2 邮件计划、预览与冻结

2026-10-07。本批完成本地计划、纯文本渲染和冻结；不连接 SMTP，不发送邮件，也不实现发送重试、暂停或人工重评。计划模块自身不触发采集或发送；后续新增的显式启用 [后台入口](background-mail.md)可在采集后或独立 timer 中调用它。

## 使用

已有实例先备份，再显式执行 `storage-init` 升级到 `0005_mail_planning`。须已经通过 N1 的 `notifications-activate` 启用，并有生产 live 采集保存的投递资格；本地导入、维护 reparse 不制造这些资格。发件人与收件人使用启用时明确提供的地址，不新增 SMTP 配置或凭据。

```sh
# 只读预览当前可计划内容；不保留成员或写入诊断。
uv run --locked signalnest mail-plan --config signalnest.toml --preview
# 显式计划并冻结，使用默认额度。
uv run --locked signalnest mail-plan --config signalnest.toml
# 更小的独立批次；--at 是可选的 UTC Unix 秒，默认当前时间。
uv run --locked signalnest mail-plan --config signalnest.toml \
  --max-messages 3 --max-events 20 --max-bytes 65536
# 使用上一步 JSON 返回的 mail_ids，查看保存的邮件与精确成员。
uv run --locked signalnest mail-preview --config signalnest.toml --mail-id 1
```

未初始化/未升级、未启用、未知邮件或数据库/冻结内容错误退出 1；参数/配置错误退出 2。完成计划或只读预览退出 0，包括没有到期项，或存在明确阻断的单项。计划 JSON 区分 `planned`、`mail_ids`、本轮 `blocked` 有限诊断、持久 `blocked_count`、`remaining_immediate`、`remaining_digest`、`due_digest`、`deferred_digest` 与 `candidate_limit_reached`，不能把退出 0 理解为全部积压已处理。

两个预览命令的 stdout **有意展示地址、正文与决策证据**；请保管这些输出。stderr 事件日志只包含有限的来源、运行、阶段和错误分类，不输出地址、正文或异常参数。帮助不打开存储；预览需要现有数据库，但不获取写入锁或修改记录。`mail-plan` 写入持有既有实例锁。

## 数据与接口

保留 N1 表及历史迁移，0005 仅新增三张表，不重建存在循环外键的 SQLite 通知表：

| 表 | 用途 |
| --- | --- |
| `mail_messages` | 第一份成功冻结的完整 RFC 5322 bytes、摘要、地址、Subject、Message-ID、Date、渲染版本、额度、批次/分片及 pending 状态。立即邮件唯一引用 N1 意图；Digest 的安装/档次/分片唯一。 |
| `mail_message_members` | 每个 event 的唯一邮件分配，明确顺序，关联该事件的已选决策，并保存版本、实际正文响应、来源身份和完整决策理由快照。`event_id` 全局唯一，阻止立即与 Digest 双重分配。 |
| `mail_plan_errors` | 未分配项最近一次有限阻断：事件/决策、渲染版本、字节额度、错误代码与时间。不是发送记录。 |

旧 `email_outbox` 仍表示立即路线的 planned **意图**；N2 不更新它或事件的 `outbox_id`。`notifications-status.planned_immediate` 保持原来的意图总数语义，不表示未冻结积压；计划 JSON 的 remaining_* 通过未分配成员计算。升级不会启用、产生事件、补计划或发送历史邮件。升级前已存在且符合资格的 N1 事件可被之后的显式计划消费。

库入口：

- `render_mail(...) -> RenderedMail`：纯函数，明确传入内容/决策、地址、Message-ID 和时间，无默认时钟或随机数。
- `prepare_plan(engine, source_id, options, at=...) -> PreparedPlan`：只读快照，关闭读事务后完成渲染与装箱。
- `commit_plan_in_transaction(connection, prepared)`：在调用者事务中重验快照并写入消息、精确成员和诊断，不渲染或读文件。
- `plan_mail(...)`：包裹准备及一笔短提交；调用者须持同一实例 writer_lock。过期准备最多重新准备一次，持续冲突明确失败。
- `preview_plan(...)`：只读计划预览，不预留成员，之后计划的档次/分片可能改变。
- `load_frozen_mail(...) -> FrozenMessage` / `preview_mail(...)`：读已保存的 bytes 和成员；校验摘要、顺序、成员数量及邮件头，不读取当前 Profile 或重新渲染。这是后续 SMTP 适配器的输入边界。

## 到期、幂等与积压

沿用 N1 保存的 `effective_route` 和选中决策。immediate 消费 `email_outbox` 意图；digest 消费尚未分配的资格事件；none 不分配。到期读取决策时保存的 `context.next_digest_at`，不按新 Profile、当前日期或正文重新决策。默认上海 **09:00** 日历保持 N1 已有值，不照搬研究中的 08:00。

默认每轮最多 **5 封**、每封 **50 个事件**、完整编码邮件 **128 KiB**；允许范围分别为 1–20、1–100、1 KiB–1 MiB。大小包含邮件头、quoted-printable 编码和 CRLF，不仅是正文字符数。预览逻辑文本统一使用 LF，冻结的实际邮件 bytes 保留 CRLF。立即邮件每封一项；Digest 按数量及真实 bytes 分片。同档已冻结的分片保持原样，之后分片继续递增。

immediate 与已到期 Digest 同时积压时，为 Digest 留至少一封容量；max_messages=1 时先补 Digest。过去档次的未分配事件归入当前最近已到的上海档次，明确标注事件原发生时间和积压补计划；新事件仍须等待自身保存的到期时间。不用最后计划/发送时间跳过持久未分配项。

每轮最多准备 1000 项完整内容快照，交替选择两条路线；达到上限可再次执行。计数与资格查询仍读取所有未分配项的轻量元数据/context，这个上限不承诺总数据库读取、内存或运行时间都固定。

单项过大或无法渲染时保留事件，保存 `mail_item_too_large`、`render_input_invalid` 或 `render_content_mismatch`，继续后续正常项。相同决策/渲染版本/字节额度不会每轮反复渲染已阻断项；更换额度或渲染规则后允许再试。准备数据不可信或数据库故障是整轮错误，不能伪装为普通阻断。暂不提供人工重评/重试入口。

## 冻结与事务边界

正文仅使用事件关联的不可变版本、实际正文响应的最终 URL 与当时已选决策，不读 documents.current_version_id。需要核对的项在立即邮件中显著标记，在 Digest 中单独分组。标题、理由、未知项和正文摘录做保守长度限制并提示省略；精确成员保留完整决策快照。只生成纯文本计划，不转发 HTML，不下载图片/附件。

渲染版本为 `signalnest-text-mail-v1`。它标识模板和展示规则，不加入 NoticeContent 摘要。N1 没有预先冻结 renderer，本批在第一次成功冻结时确定它。冻结后正文、Profile、地址或后续渲染代码变化不改变已保存邮件。待分配项没有地址修改/政策更新接口；手工修改数据库不属于支持的维护方式。

立即 Date 使用意图登记时间，Digest Date 使用档次时间；真正冻结时刻另存 frozen_at。Message-ID 从稳定 delivery_key 与明确发件地址的域生成，不依赖主机名或随机数。它不是邮件服务端的去重保证，不承诺 exactly-once 投递。

准备阶段没有写入；提交重验地址/日历、事件版本/已选决策/路线、未分配成员及 Digest 分片，然后在同一短事务内保存整轮邮件、成员和阻断诊断。任何登记失败整体回滚，保留原通知成功状态、事件与意图供下次计划。没有 Parser、网络、原始文件读取或 N0 计算在写事务内。

N2 冻结模块保持无网络副作用；mail_messages.state 的 pending 仅是冻结标签，N4 mail_delivery 单独记录 sending、attempt、due 和 SMTP 结果。正常异常回滚和重新打开库验证不等于断电实验，本批没有新增真实子进程终止实验。N3 已另行实现 [SMTP 适配器](smtp.md)，只消费已冻结字节和地址，返回有限发送结果；网络前后尝试登记及恢复已由 [N4](mail-sending.md)实现。

## 本批验证

macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1；使用已有虚拟环境，无新增依赖或锁文件变动。全套 **1112 项离线测试通过**（保留 999 项，新增渲染 37、计划/约束 44、CLI 28、迁移 4）。Ruff 检查、格式检查（src/tests 与部署校验程序，共 75 个 Python 文件）及 git diff --check 通过。

新增测试连接真实 N1 原文、Parser、封存决策与 SQLite，验证数量/编码字节分片、101 项边界、250 项以上积压、立即/Digest 排他、停机补计划、待核对组、冻结后正文/Profile/地址变化、准备与分配一致性、实际唯一/复合外键、第三条插入故障及诊断登记故障的整体回滚、重新打开库和损坏检测。0004 已有事件/意图/原文的升级保存及三个建表位置异常回滚通过；不称为断电验证。

另从不同工作目录使用安装的 CLI，在新临时实例检查帮助和配置无写入、初始化两次、只读预览、首次计划一封/再次零封及冻结预览文本/bytes 摘要一致。临时启用的生产覆盖事实来自显式 fixture 设置，不声称实际遍历校园网站。70 份已跟踪的 research/fixture、Parser、原契约/N0 规则、0001–0004 和依赖文件逐字节保持不变。没有新校园/SMTP 请求，没有个人实例自动启用、提交、推送或 PR。
