# 通知模块代码任务安排

2026-10-05；输入依据：[决策补充N0](notification-decisions.md)、[Email设计](email-delivery-design.md)。设计已形成，下面任务可交实现Agent；**本次仅安排任务与验收，不执行生产实现或真实邮件发送**。当前多人未提交改动保留。实施开始先重读AGENTS、Git、最新schema/事务/配置/运行接口，不以研究快照覆盖其他Agent代码。

## 顺序与可并行边界

```text
N0 Profile/规则/Action → N1 事务与持久状态 → N2 计划/冻结 → N4 恢复集成
                               └──────→ N3 SMTP ────────┘
```

N1/N2同一Agent顺序负责schema/迁移/ingestion接口，避免同时修改事务链。N3在有限结果契约确定后可由另一Agent并行实现标准库SMTP接口和模拟测试；N4最后接入。每项完成后留可审查结果，不自动提交/推送。所有真实账号/邮件操作不在本代码任务授权范围。

### N0：Profile 与透明规则决策（先完成，小范围）

- 新增严格Profile、NoticeFacts、Decision契约与纯函数；四种Action为IMMEDIATE/DIGEST/REVIEW/IGNORE。未填事实为unknown，日期近期只选候选，不能代替相关性与资格。
- 规则只支持少量可查看的主题词组、资格/绝对日期表达式，逐条给reason_codes、rule_id、证据、missing_fields；未支持条件/关键媒体转REVIEW。无LLM/OCR/附件下载/通用规则DSL。
- 定义Profile/规则/Facts提取/引擎revision和规范摘要；定义首启近期候选最高DIGEST、历史静默、已关注机会条件变化与明确排除的优先级。同事件决策与内容版本分开。
- 交付D1–D5纯决策单测及D6–D12的接口场景；N0不迁移/写数据库、不建outbox、不发邮件。不是全量“校园规则库”，第一版未支持表达式明确unknown。

### N1：持久事件与原子投递意图

- 定义单收件人notification context、observation、事件序号、选中decision、route与planned任务及有限发送结果契约；持久化policy revision/决策证据，新增迁移，不重写0001–0003。
- **迁移前纠正ID假设：**核对生成DDL；activate保存稳定source/source_document身份成员，替代max(id)水位，不重建documents来补AUTOINCREMENT。冻结active_from/to；近期候选使用固定activation_at窗口和真实列表/详情日期证据，默认首启回顾，旧历史静默。
- 默认未启用，无observation/邮件事件/网络。live与offline/maintenance分清，new看observation与来源身份，近期activation候选不能再重复生new；列表日期目前未落库，显式登记首启成员的候选日期/证据，不能虚构已有字段。
- 在已有save_notice_in_transaction内一起写业务成功、observation、event、N0 decision、IMMEDIATE的planned意图；DIGEST/REVIEW登记选中资格待N2计划，IGNORE仅留决策；不要在协调器成功返回后补event。
- 解决A→B→A、Parser升级、离线reparse污染、actual body evidence与版本首次raw不同的问题。附加原文读取/比较在事务外，提交核对baseline token；comparison_unknown诊断持久化。
- 验收E1–E6/E15与D6–D12；迁移保留旧数据，不因迁移补发；只有显式activate近期回顾能创建相应候选；写入失败全部回滚。验证身份集合不依赖数值ID、规则升级不伪造内容update。N1不调用SMTP、不写发送线程或broker。

### N2：立即与Digest计划、不可变渲染

- 只消费N0选中Action，hybrid/digest_only不能绕过资格；IMMEDIATE立即、DIGEST汇总、REVIEW单列待核对、IGNORE无邮件。每个选中event只有一个outbox；事件/决策在成功事务登记，MIME渲染在后续本地阶段完成。
- Digest每日上海08:00、最多50事件/封、完整MIME128KiB；事务外渲染按字节装箱，短事务冻结成员/payload，不在固定成员后重新拆分。每次最多计划5封，按MAX(part)+1继续；长期停机只取持久未分配事件，不靠last_digest_sent_at跳过。
- 冻结完整bytes/hash、Message-ID、Date、from/to、成员、decision/reasons/未知项与渲染版本；重试不用current/Profile现值；planned渲染失败恢复同任务，不创建第二个意图。
- 验收E7–E9，空Digest、重复计划、分片、回退事件展示、后续配置变化。N2不访问校园网站、不下载附件、不发邮件。

### N3：SMTP有限结果适配器，可并行

- 标准库email/smtplib，单收件人；显式证书/hostname验证、SMTP_SSL或强制STARTTLS、凭据env；默认不能因导入/config-check连网。
- 输入已冻结邮件，输出accepted/retryable/uncertain/permanent及有限阶段/回复码，不带任意服务器文本。
- 验证最终250前超时、socket异常包装、sendmail拒绝字典、QUIT错误不覆盖accepted；不在适配器内部重试。
- 使用fake SMTP、socket模拟或loopback本地服务，无真实邮箱；验收E10/E12/E13的接口部分。无需新增第三方依赖；任何新依赖必须有实际理由。

### N4：发送协调、诊断与真实终止恢复

- 新的显式mail-plan/mail-drain/mail-status/activate与人工retry命令（最终名称以实现评审决定）；复用实例锁，网络放在DB事务外。
- 增加本地Profile校验/决策preview与显式re-evaluate入口；只允许未有投递意图的当前live候选，复用原event/delivery身份。变更Profile不自动重评全历史或重发冻结邮件；activate先展示近期机会/待核对/忽略数量，不假定用户Profile。
- sender网络前持久sending/attempt，恢复遗留为uncertain；默认有限6次、持久due、uncertain至少30min再试、认证通道暂停、预算及上限。人工retry授予一次额外许可，累计attempt_no不重置，启动不重复延后已恢复due。
- 不把邮件失败写入documents失败；不以crawl-run整体succeeded作为消费前提；不得在DB无法记成功后同进程马上再发。
- status区分planned/pending/retry/uncertain/accepted/blocked、最老待办、接受结果未知次数。stderr不回显秘密；部署模板仅提供建议，不安装/启动自动发送。
- 运行E10–E16集成验收；用真实子进程SIGKILL+新进程恢复，分别在mock已接受未返回、本地接受commit前、commit后终止。mock服务/接受记录留在父进程或独立服务，不随sender消失；记录观察到的重复，不能让测试伪造SMTP去重。
- 全部已有离线测试与新邮件测试通过；代码/格式/迁移检查和文档同步完成。默认测试无公网网络；无真实凭据/邮件、无邮件服务选择与账号操作。

## 任务完成门槛

N0–N4通过后才进入“用户提供明确SMTP配置、审阅一封预览、安排少量真实投递”的独立验证任务。实现交付必须说明：规则覆盖与unknown限制、启用边界按来源身份、本地唯一键已验证、SMTP重复风险仍在、accepted不是delivered、没有退信/收件箱送达保障。不得通过隐藏uncertain状态来美化成功率，也不得把Message-ID当外部幂等协议。
