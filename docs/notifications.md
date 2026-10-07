# N0 本地事实与规则决策

2026-10-06 的 N0 已实现纯本地计算与 CLI 预览，这些函数仍没有持久化副作用。2026-10-07 另行完成 [N1 启用与事件持久化](notification-state.md)；邮件计划、SMTP 和发送协调尚未实现。

## 可调用接口

```python
from datetime import datetime
from pathlib import Path
from signalnest.notifications.contracts import EventContext
from signalnest.notifications.profile import load_profile
from signalnest.notifications.facts import extract_facts, rule_manifest
from signalnest.notifications.decision import decide

profile = load_profile(Path("profile.toml"))
facts = extract_facts(notice_content)  # 已有 NoticeContent；不再取网页
context = EventContext(next_digest_at=datetime.fromisoformat("2026-10-06T09:00:00+08:00"))
decision = decide(profile, facts, context, now=datetime.fromisoformat("2026-10-05T20:00:00+08:00"))
```

`extract_facts` 和 `decide` 每次完整计算，输入不可变，没有缓存、随机数或系统时钟。`load_profile` 才执行显式本地文件读取。CLI 的 HTML 输入先调用既有 Parser，JSON 输入直接校验 NoticeContent；不读取数据库。未来协调器负责提供可信事件 provenance，本地 `EventContext` 只声明预览意图。

## Profile 与事实覆盖

Profile 是独立 UTF-8 TOML（最多 64 KiB）；`profile.example.toml` 是虚构示例。字段为固定 schema_version=1、profile_id=self、可空 institution/study_level/college/major/entry_year，以及 interest_topics/include_phrases/exclude_topics/high_value_topics/store_only_topics。缺省个人事实为 unknown，入学年不转换成当前年级。拒绝额外字段、非法值、重复项、空/控制字符词组和高价值与仅保存主题重叠；至少有一个关注主题或字面词组。主题/词组集合规范排序，影响判断的值变化会改变 Profile 摘要。主题包括 exchange、scholarship、research、competition、course_enrollment、minor、recommendation。

`rule_manifest()` 返回固定规则清单，主题词组在 `facts.TOPIC_PHRASES` 中可检查；词组是字面匹配，没有打分/模型/DSL。标题与正文都可提供相关性证据；**只有标题明确主题命中**才能触发排除，避免正文提及另一主题就误杀。用户字面 include_phrases 不作为正则执行。store_only_topics 可以独立命中保存价值。

资格只从明确“面向/仅限/报名对象”等对象声明提取学校、学习层次、学院、专业、20XX 入学年等有限条件。字段按显式 AND 作 match/mismatch/unknown；已知必要条件可靠不匹配为 ineligible，缺值/未支持/冲突为 unknown。GPA、语言分数、年级、全日制/在校状态、年龄、处分记录、项目成员身份及 OR/例外当前不能核准。识别“本科生”不能掩盖另外列出的条件；明确资格章节内尚未理解的编号条目保留未知，跨条目/跨句 OR 不按 AND 作可靠排除。没有对象声明不等于面向全体。eligible 仅表示支持的可见条件匹配，不是官方资格认定；不承诺理解任意通知的全部隐藏条件。

时间只接受有明确上下文的完整年份日期与支持的时刻，按 Asia/Shanghai 解释；显式小时按原文，只有日期的截止取当日 23:59:59，开始取 00:00:00，并保留原句。即日起/自通知发布之日起可使用输入的站点发布日期作开始依据。不能从发布时间补全年份、继承区间第二日期年份、把站点日期当截止，或猜多个截止的正确角色。24:00、秒/不支持的时刻、未支持时区、多个冲突/损坏时间保持 unknown，不把“中午12点”等截断成日期截止。额外“截止另行通知”等未解决的时间声明会取消可信截止，不能一边标未知一边按旧时间制造紧急提醒。可信截止已经过去为 closed；明确未来开始为 not_started；只有支持的开始依据且尚未截止才是 open。

结果/结题/验收等标题按 reference 保存价值处理；机会类别依据有限报名/申请/选课/征集词组。图片、附件只记录引用索引与 URL：没有 OCR、下载或附件正文解析。媒体型正文或明确“条件/时间见附件”等依赖保留 information_incomplete / unknown；正文信息充分、仅补充附件不会一律变资格未知。无法从媒体确认相关性时默认 STORE_ONLY + needs_review，可能漏掉媒体独有的机会。

证据位置为 **NoticeContent.title/body_text 中的 Unicode 字符索引，end 不包含**，摘录最多 120 字且与位置相符，不是原 HTML 字节偏移。媒体证据无虚构摘录，只记录引用。保留引用不意味着网页 HTML 可安全展示，后续界面仍须安全处理。没有可靠的全机会取消正样本，目前不启用取消提取；违规取消个人资格不等于活动取消。

## 决策与路线

| 输出 | 当前语义 |
| --- | --- |
| PUSH_NOW | 确认相关且已开放、可信截止在 72 小时内；或 recent new 命中高价值主题。未知资格仍明确待核对。 |
| DIGEST | 相关非紧急机会、普通信息或内容更新；可同时 needs_review。 |
| STORE_ONLY | 结果/参考内容、仅保存主题、相关已截止的首启/历史候选，或相关性不足的媒体信息。 |
| IGNORE | 可靠明确排除、充分可见文本无兴趣命中、无保留价值的已知不符合/已关闭机会；原业务数据仍保留。 |

近期按上海当日及之前 6 个自然日，仅配合高价值主题与机会/new 上下文；日期近期本身不触发提醒。未来发布日期标记待核对，不作为已发布紧急依据。旧日期但可信短截止的正常新机会仍可及时提示。needs_review 独立表示信息缺口；它既不等于资格符合，也不强制所有机会等到明天。

普通 update 默认汇总，不将任意正文变化猜成紧急条件变化。显式旧事实与登记路线表明此前已取得邮件资格，且支持的资格/开始/截止确实改变时，保留“条件变化”提醒，即使新条件不符合或截止已过；用户当前可靠排除仍优先。缺少比较证据显式标记未知；有限事实相同不能证明其他正文变化仅是排版。N0 不创建或去重实际事件。

`effective_route` 在决策中一并计算：STORE_ONLY/IGNORE→none，DIGEST→digest；PUSH_NOW + hybrid 通常 immediate，digest_only→digest。activation_recent 默认降为 digest，但可信开放截止不晚于 next_digest_at 保留 immediate；digest_only 是显式用户选择，仍优先。historical 不产生主动路线。所有时钟显式传入且带 UTC offset，next_digest_at 必须晚于 now；输出 evaluated_at 统一 UTC。路线不代表邮件已经计划或发送。

## 版本、摘要与边界

固定版本分别为 `whu-notice-facts-v1`、`notification-rules-v1`、`notification-decision-v1`、`notification-routing-v1`。变更词组/选择语义/条件或时间提取、优先规则、路线合成时，更新对应版本；不读取运行中可变全局配置，也不修改已有 WHU Parser 版本。`decision.policy_manifest(profile)` 返回全新可序列化快照（Profile、事实规则、决策阈值/顺序、路线规则与版本），其规范摘要为 policy_sha256；input_sha256 另纳入事实、完整 EventContext、显式决策时间。Decision 保存各版本、Profile/content/facts/policy/input 摘要、命中规则、有限理由及未知项，便于本地比较重放。新旧事实不可混合口径；decide 拒绝非当前提取器版本，需先重新提取。

原 bytes 摘要、`NoticeContent.content_sha256()`、Parser 版本保持原职责；事实摘要不替代正文摘要，规则版本不加入正文摘要。相同输入、上下文与时钟产生同一决策；新时钟/画像/上下文会产生新输入摘要，当前不持久化这些结果。版本标识加固定规则清单是追溯约定，不保证跨规则改动忘记升版仍能安全恢复。

真实 fixture 离线覆盖科研训练选课、辅修 OR/绩点条件、大创中期多时间/项目身份、教师选题征集和结题结果。当前 `notice-18135-20261005T133554Z.html` 缺少既有 Parser 支持的正文结构，预览明确退出 1 / missing_structure；没有为了规则样例修改 Parser 或猜造正文。当前真实样本没有覆盖全部主题、所有资格表达或可靠取消正例，相关边界另有小型合成 NoticeContent 测试。

本实现参考[通知决策调研](../research/notification-decisions.md)最新 Action+needs_review 口径，拆开认知缺口和优先级；规则仍以代码和已验证样本为准，调研中的旧验收表/IMMEDIATE/REVIEW 术语不作为实现接口。独立 live 基线、稳定启用身份、A→B→A 事件与原子 planned 意图已由 [N1](notification-state.md)实现并另行测试；N0 纯函数测试本身不证明数据库事务、SMTP、重启投递或断电恢复。
