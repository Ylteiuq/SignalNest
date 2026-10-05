# SignalNest 通知决策补充：Profile、规则与 Action

2026-10-05；**设计建议，未实现**。本次仅补充 Email 计划前的 N0，不增加 LLM、OCR、附件下载、规则 DSL 或任务平台。本文件定义决策；[Email 设计](email-delivery-design.md)继续定义冻结、投递和恢复。

## 1. 输入与当前缺口

**源码事实：**[NoticeContent](../src/signalnest/contracts.py)只有标题、站点日期、正文文本/HTML和图片/附件引用，没有资格、机会类别或截止日期。图片型正文可以没有文本；附件`access`是访问状态，不表示其内容已经读过。[Parser](../src/signalnest/parsing.py)不做OCR或附件解析。当前没有个人 Profile 或通知决策。

建议调用链：`live成功内容 → 内容/首启候选事件 → N0决策 → 按Action登记投递意图 → N2冻结 → N3/N4投递`。新近发布只影响时效，不直接决定发送；Parser采集成功也不表示用户符合资格。

## 2. 最小结构化 Profile 与透明规则

单个本地 Profile，Pydantic严格校验；未知值为null，不从“本科生院”推断用户身份。下面是**虚构示例**，不是当前用户资料：

```json
{
  "schema_version": 1,
  "profile_id": "self",
  "institution": "whu",
  "study_level": "undergraduate",
  "college": null,
  "major": null,
  "entry_year": null,
  "interest_topics": ["exchange", "scholarship"],
  "include_phrases": ["本科生科研"],
  "exclude_topics": [],
  "immediate_topics": ["exchange"]
}
```

- 首版只核对学校、培养层次、学院/专业、入学年份；不自动从入学年份推导当前年级。需要“二年级”而只有entry_year时为unknown。无效或过时事实不能默认为符合。
- 不收集学号、身份证、密码或未使用的GPA等字段；遇到成绩、语言、经济条件等未支持约束，列为缺失项，不能跳过。将来确有规则需要时再增加字段。
- `interest_topics`使用少量显式命名主题及可查看的词组表；include_phrases只在标题/可见正文作字面匹配。至少设置一个兴趣主题或词组才能activate；不隐式关注所有栏目。
- 规则为少量固定Python函数与受校验的数据：稳定`rule_id`、主题词组、受支持的资格/完整年份截止时间表达式、优先级。无隐藏分数、模糊相似度或任意配置代码执行。
- 关键词命中只证明“命中用户规则”。裸词“研究生”不能判定本科生不合格；本科生推免通知可能含这个词。排除主题也须有可靠分类证据，不能对任意正文出现词作全局否决。

N0先从已有文本生成独立`NoticeFacts`：兴趣命中、逐条资格约束、机会/普通信息类别、报名/参与依据、截止/开始时间、图片/附件依赖、冲突与未知项。每条事实保留字段、原文位置和摘录；不修改Parser输出和内容hash。首版只覆盖少量样本表达式，未识别部分明确保留unknown。

### 资格、时间与信息不足

资格按明确AND条件三值合并：可靠且无歧义的反例→`ineligible`；全部已识别且没有未解决的条件、必要Profile事实均匹配→`eligible`；缺值、复合OR未支持、冲突、未写对象、无法判断条件是否完整→`unknown`。理由写“按可见正文可识别条件”，不写“资格已官方核准”。“未写对象”不等于“面向全体”。普通信息规则可明确不要求资格，不把它误包装成报名机会。

时间只接受明确年份、可解释时区的表达式；日期只有月日、多个相互冲突截止日、报名是否开始不明均为unknown。站点日期不是截止日；最近7天不证明仍开放。日期截止默认上海当天结束，需在规则中明示并保留原句，不能把该假设用于有明确小时的文本。

图片型正文，或正文明确把资格/时间指向图片、附件，必须记录`information_incomplete`。相关候选进入REVIEW，不能从附件名/图片alt推断符合。仅有补充附件而正文明确给出所需条件，不必全部降为REVIEW，但理由说明附件未解析。若正文无兴趣命中、关键内容可能只在媒体中，兴趣也为unknown，不能当作可靠不相关而静默丢掉。

## 3. 四种 Action 与固定判断顺序

| Action | 默认条件 | 邮件衔接 |
| --- | --- | --- |
| `IMMEDIATE` | 兴趣确认命中；报名/参与机会有文本依据；资格与开放时间均可核对；近期new且属immediate_topics，或可信截止距决策时0–72h（含已关注机会的可核紧急更新） | 立即planned outbox；不得因“近期”单独进入；首启候选另有降级规则。 |
| `DIGEST` | 相关的普通信息，或条件可核对且仍开放但不满足立即优先级的机会；通常的相关内容更新 | 下个Digest的“相关信息/机会/更新”区。 |
| `REVIEW` | 相关性可能成立，但资格、开放时间、关键图片/附件、语义冲突等有未知 | Digest单列“待核对”，包含未知项和官网链接；不写“你符合资格”。不额外建人工任务平台。 |
| `IGNORE` | 可见信息充分却未命中兴趣、明确排除、明确不符合、可信截止已过；或历史首发抑制 | 保存决策及理由，不登记邮件路径。数据仍保留供检索。 |

顺序固定：可靠排除→判断兴趣→判断是否过期/不符合→未知信息→立即优先级→Digest。没有可信兴趣匹配且文字证据不足时REVIEW；文字充分但未命中规则时IGNORE，理由为`no_interest_match`，不宣称客观“与你无关”。必须依赖未知资格才能确定的机会不能IMMEDIATE。

首版固定规则/理由至少如下；实际词组与受支持表达式随rule revision保存，不隐藏在发送代码里：

| rule_id | 结果 / reason_codes |
| --- | --- |
| `explicit_exclusion` / `no_interest_match` | IGNORE / `excluded_topic`、`no_interest_match` |
| `eligibility_mismatch` / `deadline_passed` | IGNORE / `eligibility_mismatch`、`deadline_passed` |
| `information_incomplete` | REVIEW / `interest_unknown`、`eligibility_unknown`、`time_unknown`、`media_required`、`conflicting_evidence`（可多项） |
| `preferred_or_urgent` | IMMEDIATE / `preferred_topic`或`deadline_soon`，并保留兴趣/资格/时效证据 |
| `relevant_default` | DIGEST / `relevant_information`或`relevant_opportunity` |
| `followed_opportunity_changed` / `activation_cap` | 条件变化DIGEST；首启IMMEDIATE降DIGEST / `followed_conditions_changed`、`activation_recent_digest` |

证据位置格式为`field=title/body_text`加Unicode字符`start/end`及最多120字摘录；媒体只能记录引用索引/URL与“未解析”，不能提供虚构内容证据。qualification约束另保存constraint类型、所需Profile字段、实际比较值与三值结果。`REVIEW`理由须指明缺什么，不能只有“低置信度”。

**更新例外：**曾登记可投递决策的机会（包括DIGEST/REVIEW尚未分配outbox），正文明确撤销/收紧资格/改变截止日时，即使新状态不合格或已关闭，仍可DIGEST提醒“条件变化”；否则变化会被新的IGNORE吞掉。它仍是新的真实内容事件。用户明确排除该主题优先；普通排版变化不因“以前发过”升级为立即。只有受支持事实差异可证明紧急变化时，才按相同资格/相关性规则考虑IMMEDIATE；未知变化用REVIEW。

`digest_only`可把IMMEDIATE降为DIGEST；REVIEW始终保持待核对区，IGNORE始终不发送。删除旧方案的全局`immediate_only`及`updates=immediate`旁路；需要立即偏好用Profile主题/透明规则表达，不能绕过未知资格。

## 4. 首次启用：保留历史基线，也检查近期机会

### 4.1 修正 ID 假设

**实际DDL核对：**当前环境Python3.12.14 / SQLAlchemy2.0.54 / SQLite3.53.1，用`CreateTable(documents).compile(dialect=sqlite.dialect())`没有`AUTOINCREMENT`；0001迁移也未设置`sqlite_autoincrement=True`。Column级`autoincrement=True`不能证明SQLite关键字。默认INTEGER主键可能在删除后复用。[SQLAlchemy官方说明](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#using-the-autoincrement-keyword)、[SQLite ROWID规则](https://www.sqlite.org/autoinc.html)。本次只生成DDL，未修改/迁移应用数据库。

若继续用max(id)水位，必须限制不删除、不复用、不修改ID、仅数据库自动分配新ID；这些是应用前提，当前DDL没有替你保证。**本方案改用启用身份集合**，N1不依赖这个前提，也不为此重建documents。

activate持实例锁，短事务保存activation_at、installation_id，以及当时已知的`(source_id, source_document_id)`集合到`notification_activation_members`（唯一installation/source/source_document）。只保存稳定来源身份，不用document_id大小判断前后。空集合也有完整激活状态；重复activate不重设边界。删除/复用数值ID的模拟不会改变来源身份的成员判断；这不授权删除业务版本、observation或去重记录。

### 4.2 一次性近期候选

- 仍先要求一次full列表发现；老历史身份首个live仅建基线，不产生历史邮件洪峰。
- 默认`initial_recent_review=true`：首启时上海日期及之前6个自然日的身份，允许一次`activation_recent`候选；**这是发现窗口，不是资格/开放判断**。首次live同时建立baseline且只生成该候选，不再生成new/update。
- 候选来自已经解析的列表日期或已有版本日期，记录日期和证据引用；已有版本须经live成功验证再决策。当前documents没有列表发布日期，N1不能假装已有此列：在启用预览/随后成功列表处理时，用真实ListEntry日期登记成员标记。状态最少为unknown/selected/not_recent/disabled/generated：日期缺失为unknown，不能提前标历史完成。如果列表日期仍未知而详情先live成功，在同成功事务用可靠详情日期补分类并生成一次activation_recent或静默历史baseline。普通运行中对集合成员使用固定activation_at窗口，延期到第8天不会消失；已有selected不因当前日历改窗口，详情日期不一致时保留冲突转REVIEW。
- 已知旧截止日明确在未来，可列入首启预览供显式选择；首版不自动遍历全部旧详情寻找尚开放机会。未来发布日期转REVIEW，不视为近期已发布。明确过期的近期候选为IGNORE。
- 首启近期候选默认IMMEDIATE降为DIGEST；REVIEW仍待核对，IGNORE无邮件。启用预览展示规则结果/未知数，允许显式关闭近期回顾；不自动切成全历史补发。
- 未成功候选的身份/选择窗口持久保留，详情按现有容量与失败due分批处理，不一次下载所有历史详情。首启可见近期候选应在现有总详情预算内优先，不能绕过HTTP冷却；尚未验证全站近期覆盖，前两页不保证覆盖所有近期/置顶文章，积压可能错过截止。
- 不在集合中的regular身份首次live生成new候选；启用后发现但origin=historical/unknown且未有可靠新发现依据者仍默认历史抑制。首次资格看独立observation，不看last_success_at；离线成功不抢占首次机会。

不论Action如何，真实live比较基线都推进；IGNORE/REVIEW不能令同内容每轮重新产生事件。以后真实更新仍产生新的事件，不用“这篇以前提醒过”永久压制。

## 5. 决策证据、版本与事务

除启用成员记录外，最小增加`notification_policy_revisions`与`notification_decisions`，不建订阅/评分/通用规则平台：

| 持久记录 | 必须内容 |
| --- | --- |
| policy revision | 不可变Profile规范JSON+hash、透明规则数据/规则版本、facts_extractor_version、decision_engine_version；相同规范配置复用revision，secret与邮箱密码不在Profile。 |
| decision | event_id、policy_revision、evaluation_key、facts/input摘要与必要结构化事实、evaluated_at、action、稳定reason_codes、匹配rule_id、证据位置/短摘录、missing_fields、首启等context；唯一`(event_id, policy_revision, evaluation_key)`。初次为initial；显式重评有持久操作ID，同操作重放复用。 |
| activation member | installation/source/source_document唯一；不可变成员身份；recent候选日期/证据、选中/关闭状态及是否已生成候选。不要把未成功处理误标为完成。 |

事实版本/引擎版本属于policy revision，因此同政策只有一种事实提取/决策口径；evaluated_at是实际决策时钟输入。重放选中decision或同evaluation_key复用原决策，不按新时钟偷偷改变结果；显式重评记录新操作ID与时刻，可处理同政策下截止已过的未投递候选。邮件展示截止日与“截至决策时间”的事实，长期等待重试可能过时，第一版不自动撤销已冻结邮件。

内容事件保留独立seq（A→B→A成立），所有真实变化先登记事件，之后可以得到IGNORE；route不是事件天然属性。历史静默首次只建baseline/成员处理标记；近期首启候选用独立kind=`activation_recent`，唯一成员标记保证不重复。相关事件`decision_id`被投递意图引用，邮件冻结理由/未知项，不在render时读当前Profile。

计算Facts/旧文重解析/决策在事务外，持锁并保留baseline与active policy token；成功事务核对token，然后提交业务版本/成功状态、baseline、event、decision，以及IMMEDIATE的planned任务。DIGEST/REVIEW提交选中decision与对应route，outbox可为空，N2后续计划；IGNORE提交decision但无邮件路径。任何一项登记失败整组回滚。profile未激活时无通知记录，发送暂停则仍做决策。

Profile/规则改版先preview，**不制造内容update，不自动把历史全量重评并群发**。显式re-evaluate只允许当前live版本、仍无投递意图的近期/开放候选；追加decision，更新选中decision但沿用原event/delivery身份，同操作ID重放复用。DIGEST/REVIEW的选中待计划记录已经是投递资格，不因outbox_id尚为空就视为可以重路由；已有planned、Digest已分配、accepted或uncertain者也只可预览新结果。真实新内容事件仍可另作更新提醒。首次实现不提供取消后改投、Profile变更批量补发或持续按时钟重新决策。

## 6. N0/N1 验收补充（尚未执行）

| ID | 场景 | 预期 |
| --- | --- | --- |
| D1 | 近期但充分文本无兴趣命中；近期且兴趣匹配、资格/时间可核且高优先 | 前者IGNORE，后者IMMEDIATE；发布日期不能独自决定Action。 |
| D2 | 学院/年级未知、语言要求未支持、条件OR/冲突 | REVIEW+missing_fields，不默认符合、不按推断排除。 |
| D3 | 本科推免正文含“研究生”；明确仅研究生且用户已知本科 | 前者不能裸词排除；后者可靠ineligible、IGNORE。 |
| D4 | 图片型通知、资格见附件、完整正文加补充附件 | 前两项REVIEW；后一项按正文决定，均保留未解析附件事实。 |
| D5 | 近期已截止、截止缺年份、未来发布日期 | 分别IGNORE/REVIEW/REVIEW。 |
| D6 | 首启旧历史；首启近期相关；近期资格未知；近期明确无关 | 历史baseline；其余分别DIGEST/REVIEW/IGNORE，无new与activation双事件。 |
| D7 | 首启候选详情失败到第8天；列表日期未知但详情先成功；重复activate/列表/304 | 固定窗口保留；详情可补分类而非先静默完成；以当前可核期限判断；仅一次候选/投递身份。 |
| D8 | 在独立成员样例中模拟旧数值ID删除复用、新来源身份复用该ID | 成员判断按来源身份；旧身份仍属首启集合，新身份不被错误压制；验证生成DDL而非只看Column参数。不删除真实业务数据。 |
| D9 | event/decision/立即意图任一写失败 | 业务成功/baseline/通知登记整体回滚；Digest/REVIEW可以暂没有outbox。 |
| D10 | 同evaluation_key重放；Profile新revision；IGNORE显式重评变相关；同政策新操作时已截止 | 重放复用；新增决策保留理由/时刻；未投递候选可登记一次意图，过期变IGNORE，不伪造update。 |
| D11 | REVIEW已入Digest/accepted/uncertain后修改Profile | 只预览，不再发一封“资格已确认”；冻结成员/邮件/Message-ID不变。 |
| D12 | 已关注机会真实改截止/取消；稍后再A→B→A | 新事件提醒条件变化；回退有新seq；不因current IGNORE或曾发送而漏掉真实更新。 |

N0交付纯函数、契约、固定小规则及上述决策单测；N1负责持久化与事务，N2只消费选中Action。生产实现仍按[通知模块实现任务](email-implementation-plan.md)安排。
