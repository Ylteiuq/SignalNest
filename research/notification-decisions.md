# SignalNest 通知决策补充：Profile、规则与 Action

2026-10-05初稿，2026-10-06修订3；**设计与验收建议，不代表持久化/投递已经实现**。Action统一为PUSH_NOW/DIGEST/STORE_ONLY/IGNORE，needs_review独立；本次统一登记前最终路线与重评重放契约。规则清单/真实样本及当前N0差异见[评估记录](notification-rule-evaluation.md)。不增加LLM、OCR、附件下载或规则DSL；[Email设计](email-delivery-design.md)继续定义投递和恢复。

## 1. 输入与当前缺口

**源码事实：**[NoticeContent](../src/signalnest/contracts.py)只有标题、站点日期、正文文本/HTML和图片/附件引用，没有资格、机会类别或截止日期。图片型正文可以没有文本；附件`access`是访问状态，不表示其内容已经读过。[Parser](../src/signalnest/parsing.py)不做OCR或附件解析。本稿是研究契约，不将并行N0代码或其测试视为通知持久化/投递已经实现；具体支持范围须由实现Agent按当前代码核对。

建议调用链：`live成功内容 → 内容/首启候选事件 → Action+needs_review → 合成effective_route → 登记决策/对应意图 → N2冻结 → N3/N4投递`。N0需要产生决策及纯路线合成结果，N1原子登记；N2不重新选择路线。Parser采集成功不表示用户符合资格。

## 2. 最小结构化 Profile 与透明规则

单个本地 Profile，Pydantic严格校验；未知值为null，不从“本科生院”推断用户身份。下面是**虚构示例**，不是当前用户资料：

```json
{
  "schema_version": 1,
  "profile_id": "self",
  "institution": "whu",
  "role": "student",
  "study_level": "undergraduate",
  "college": null,
  "major": null,
  "entry_year": null,
  "interest_topics": ["exchange", "scholarship"],
  "include_phrases": ["本科生科研"],
  "exclude_topics": [],
  "high_value_topics": ["exchange"],
  "store_only_topics": ["course_enrollment"]
}
```

- 首版只核对学校、行动主体student/teacher、培养层次、学院/专业、入学年份；不自动从入学年份推导当前年级。需要“二年级”而只有entry_year时为unknown，未知role不能自动判不符合。实际已验证表达式子集见规则清单；无效事实不能默认为符合。
- 不收集学号、身份证、密码或未使用的GPA等字段；遇到成绩、语言、经济条件等未支持约束，列为缺失项，不能跳过。将来确有规则需要时再增加字段。
- `interest_topics`使用少量显式命名主题及可查看的词组表；include_phrases只在标题/可见正文作字面匹配。至少设置一个兴趣主题或词组才能activate；不隐式关注所有栏目。
- 规则为少量固定Python函数与受校验的数据：稳定`rule_id`、主题词组、受支持的资格/完整年份截止时间表达式、优先级。无隐藏分数、模糊相似度或任意配置代码执行。
- 关键词命中只证明“命中用户规则”。裸词“研究生”不能判定本科生不合格；本科生推免通知可能含这个词。排除主题也须有可靠分类证据，不能对任意正文出现词作全局否决。

N0先从已有文本生成独立`NoticeFacts`：兴趣命中、逐条资格约束、机会/普通信息类别、报名/参与依据、截止/开始时间、图片/附件依赖、冲突与未知项。每条事实保留字段、原文位置和摘录；不修改Parser输出和内容hash。首版只覆盖少量样本表达式，未识别部分明确保留unknown。

### 资格、时间与信息不足

资格按明确AND条件三值合并：可靠且无歧义的反例→`ineligible`；全部已识别且没有未解决的条件、必要Profile事实均匹配→`eligible`；缺值、复合OR未支持、冲突、未写对象、无法判断条件是否完整→`unknown`。不能把“全日制在校本科生”只匹配本科便判符合；全日制/在校状态、每学期限选一门等未支持条件必须留下未知。理由写“按可见正文可识别条件”，不写“资格已官方核准”。“未写对象”不等于“面向全体”。普通信息规则可明确不要求资格，不把它误包装成报名机会。

首批时间规则见评估清单：完整年份日期/小时及同一句明确起年区间；单独月日、冲突截止或对象无法区分为unknown。仅日期/“日前”保留上海日期边界区间与needs_review，不伪造精确小时；可作保守紧急提醒依据，但区间下界过去不等于已截止。站点日期不是截止日，最近7天不证明仍开放。

图片型正文，或正文明确把资格/时间指向图片、附件，记录`needs_review=true`及missing_fields，不能从附件名/图片alt推断符合。仅有补充附件而正文明确给出所需条件，不必一律标资格未知，但理由说明附件未解析。正文无兴趣命中、关键内容可能只在媒体中时，兴趣也为unknown，默认STORE_ONLY+needs_review，不声称可靠无关；标题等已确认相关且高价值/紧急时，仍可提醒待核对。

## 3. Action、核对标记与最终路线

Action表达主动提醒优先级，needs_review表达认知缺口；两者独立。资格`ineligible`与`unknown`必须分别保留。

| Action | 默认规则 | needs_review可能值 |
| --- | --- | --- |
| `PUSH_NOW` | 已确认相关的高价值机会，可信截止在72h内；或近期new且属于high_value_topics。明确不符合/已关闭排除，资格未知不自动降级 | 可为true；立即邮件显著写“资格待核实/信息待核对”、截止证据及missing_fields。 |
| `DIGEST` | 相关但非紧急机会、相关普通信息/更新；已确认相关但不够立即优先的未知资格机会 | 可为true；Digest按核对标记单列，而非按一种REVIEW Action分流。 |
| `STORE_ONLY` | 相关结题/验收/结果/参考资料；过期历史但值得检索；仅命中保存主题且无其他主动兴趣；当前申请主体明确不匹配但有后续参考价值；缺少相关性证据的媒体型信息 | 可为true；持久保存决策/资料，无主动邮件。不是候选自动丢弃。 |
| `IGNORE` | 用户明确排除，充分可见文本未命中关注或保存主题；明确不符合且无保留价值；纯噪音 | 可为false；无主动邮件，既有原文/业务记录仍保留。 |

顺序：用户可靠排除→兴趣/参考价值→申请阶段/主体→可信过期或明确不符合→紧急/高价值→普通汇总。unknown本身不触发排除；needs_review仅标注缺失条件/时间/媒体/冲突，不能作为“延迟到明天”的统一开关。兴趣unknown且无正向证据默认STORE_ONLY+needs_review，待今后媒体能力补齐；这可能漏掉仅媒体承载的机会，必须在评估/状态中可见。

`store_only_topics`对命中主题生效，不对整篇通知作全局否决：主动兴趣主题为`interest_topics - store_only_topics`，字面include命中也可提供独立主动兴趣。科研选课同时命中research（主动）和course_enrollment（仅保存）时仍按科研机会提醒；同一主题同时关注与仅保存则服从仅保存。可靠exclude仍优先。验收增加128291跨主题反例，不能只测单主题配置。

首版固定reason_codes：excluded_topic/no_interest_match/reference_only/audience_mismatch/deadline_passed/deadline_soon/high_value_topic/relevant_digest；核对理由另列eligibility_unknown/time_unknown/media_required/conflicting_evidence。稳定rule_id与匹配词组见评估清单。证据为title/body_text的Unicode start/end及≤120字摘录；媒体只记录引用、未解析，不能虚构正文。

**更新例外：**曾登记邮件资格的机会（effective_route=digest也算，即使outbox为空）真实取消、收紧资格/改截止，应提醒“条件变化”，不能因新状态不符合而直接吞掉。可信紧急撤销可以PUSH_NOW，普通变更DIGEST；不把“违规取消参赛资格”判成机会整体取消。当前用户明确排除仍优先；未知变化保留needs_review。首批真实取消正样本尚缺，规则启用门槛见评估记录。

### 3.1 在登记之前合成 effective_route

纯函数输入：Action、notification_mode、activation_policy、事件context、固定evaluated_at、next_digest_at及可信截止；输出effective_route与routing_reason_codes。它们与decision一起持久化：

| 输入 | 最终路线 |
| --- | --- |
| STORE_ONLY / IGNORE | none |
| DIGEST | digest |
| PUSH_NOW + hybrid普通事件 | immediate |
| PUSH_NOW + digest_only | digest（明确用户选择；仍展示紧急/待核对） |
| PUSH_NOW + hybrid首启候选 | 默认digest；可信截止≤下一次Digest时保留immediate，记录activation_urgency_exception，避免首启降级造成当天漏报 |

只有effective_route=immediate才在N1建立即planned outbox；digest登记邮件资格/时间，outbox可待N2计划；none不登记邮件资格。Action与effective_route可以不同，必须都保存，mode/首启政策版本也进决策输入。之后N2仅读effective_route，不能再读取当前mode/Profile来重路由。邮件资格首次登记后路线和选中decision锁定；未来改模式影响新事件，不影响原队列。

## 4. 首次启用：保留历史基线，也检查近期机会

### 4.1 修正 ID 假设

**实际DDL核对：**当前环境Python3.12.14 / SQLAlchemy2.0.54 / SQLite3.53.1，用`CreateTable(documents).compile(dialect=sqlite.dialect())`没有`AUTOINCREMENT`；0001迁移也未设置`sqlite_autoincrement=True`。Column级`autoincrement=True`不能证明SQLite关键字。默认INTEGER主键可能在删除后复用。[SQLAlchemy官方说明](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#using-the-autoincrement-keyword)、[SQLite ROWID规则](https://www.sqlite.org/autoinc.html)。本次只生成DDL，未修改/迁移应用数据库。

若继续用max(id)水位，必须限制不删除、不复用、不修改ID、仅数据库自动分配新ID；这些是应用前提，当前DDL没有替你保证。**本方案改用启用身份集合**，N1不依赖这个前提，也不为此重建documents。

activate持实例锁，短事务保存activation_at、installation_id，以及当时已知的`(source_id, source_document_id)`集合到`notification_activation_members`（唯一installation/source/source_document）。只保存稳定来源身份，不用document_id大小判断前后。空集合也有完整激活状态；重复activate不重设边界。删除/复用数值ID的模拟不会改变来源身份的成员判断；这不授权删除业务版本、observation或去重记录。

### 4.2 一次性近期候选

- 仍先要求一次full列表发现；老历史身份首个live仅建基线，不产生历史邮件洪峰。
- 默认`initial_recent_review=true`：首启时上海日期及之前6个自然日的身份，允许一次`activation_recent`候选；**这是发现窗口，不是资格/开放判断**。首次live同时建立baseline且只生成该候选，不再生成new/update。
- 候选来自已解析的列表日期或已有版本日期，记录日期/证据；已有版本须live验证再决策。documents没有列表发布日期，N1显式定义证据持久化。成员状态最少unknown/selected/not_recent/disabled/generated；日期未知不能提前历史完成，详情先live成功则用可靠详情日期补分类。同一成功事务生成一次activation_recent或静默历史baseline。延期不改变固定窗口；列表/详情日期冲突加needs_review，不伪造一致。
- 已知旧截止日明确在未来，可首启预览显式选择；首版不遍历所有旧详情寻找开放机会。未来发布日期加needs_review，不视为近期已发布；已过期相关候选STORE_ONLY，不主动发送。
- 首启降级在§3.1路线合成时完成，可信截止早于下一档Digest的PUSH_NOW候选例外保留immediate；其他默认汇总。启用预览展示最终路线与核对项，可关闭近期回顾；不自动全历史补发。
- 未成功候选的身份/选择窗口持久保留，详情按现有容量与失败due分批处理，不一次下载所有历史详情。首启可见近期候选应在现有总详情预算内优先，不能绕过HTTP冷却；尚未验证全站近期覆盖，前两页不保证覆盖所有近期/置顶文章，积压可能错过截止。
- 不在集合中的regular身份首次live生成new候选；启用后发现但origin=historical/unknown且未有可靠新发现依据者仍默认历史抑制。首次资格看独立observation，不看last_success_at；离线成功不抢占首次机会。

不论Action/needs_review如何，真实live比较基线都推进；无主动邮件不能令同内容每轮再生事件。真实更新仍产生新事件，不用“这篇以前提醒过”永久压制。

## 5. 决策证据、版本与事务

除启用成员记录外，增加policy revision、decision与一个有界重评operation记录，不建订阅/评分/任务平台：

| 持久记录 | 必须内容 |
| --- | --- |
| policy revision | 不可变Profile规范JSON+hash、透明规则数据/规则版本、facts_extractor_version、decision_engine_version；相同规范配置复用revision，secret与邮箱密码不在Profile。 |
| decision | event_id、policy_revision、evaluation_key、事实/input摘要、evaluated_at、action、needs_review、missing_fields、reason_codes/rule_id/证据；effective_route、mode/首启政策、routing reasons/clock输入；初次initial，重评evaluation_key=operation_id，唯一event/policy/evaluation_key。 |
| activation member | installation/source/source_document唯一；不可变成员身份；recent候选日期/证据、选中/关闭状态及是否已生成候选。不要把未成功处理误标为完成。 |
| notification operation | operation_id唯一、规范参数hash、明确event集合及每项version/body/observation token、固定policy/evaluated_at/路由context、成员处理结果/完成状态；JSON足以表达小批成员，不建可扩展任务队列。 |

事实版本/引擎版本属于policy revision，因此同政策只有一种事实提取/决策口径；evaluated_at是实际决策时钟输入。重放选中decision或同evaluation_key复用原决策，不按新时钟偷偷改变结果；显式重评记录新操作ID与时刻，可处理同政策下截止已过的未投递候选。邮件展示截止日与“截至决策时间”的事实，长期等待重试可能过时，第一版不自动撤销已冻结邮件。

内容事件保留独立seq（A→B→A成立），所有真实变化先登记事件，之后可STORE_ONLY/IGNORE。历史静默首次只建baseline/成员标记；activation_recent唯一成员标记防重复。事件保存selected_decision_id/effective_route及delivery_intent_registered_at；后者只有immediate/digest资格登记时填写，不能根据outbox是否为空推断锁定。邮件冻结理由、needs_review与未知项，不在render时读当前Profile。

Facts/旧文重解析/决策及路线合成在事务外，持锁保留baseline/policy/routing token；成功事务核对token，提交业务成功、baseline、event、decision/effective_route和仅immediate的planned任务。digest提交邮件资格但outbox可为空，N2后续计划；none只留决策。任何登记失败整组回滚。未激活无通知记录，发送暂停仍决策。

### 5.1 可重评条件与操作重放

Profile改版先preview，不制造内容update或全历史群发。新持久重评操作仅接受：event对应当前live版本；effective_route=none；delivery_intent_registered_at为空；从未有outbox/批次资格；按该操作固定evaluated_at仍属明确近期或开放候选。none包括STORE_ONLY/IGNORE，不包括待计划Digest。已登记immediate/digest者只可preview；真实内容更新是另一个event，照常决策。

1. 首次create(operation_id)持锁，将排序去重后的**明确event集合**、policy revision、evaluated_at、每项live version/body/observation/Facts版本、mode/首启政策/next_digest_at等输入固定后存operation。若CLI用筛选表达式，先解析成集合并存下；恢复不重新跑查询。
2. 相同ID重放先核对原规范参数。只传ID的resume读取原快照，不重新用当前时间/默认政策；明确提供参数而任一不同（即使只是新增event）返回operation_parameters_mismatch，零决策/意图副作用。
3. 同操作已完成成员直接返回原结果，**先于新操作资格门禁**；即使其后来已有Digest资格也不能误报重复失败。未完成项各短事务核对原输入token与无资格条件；内容变更为stale_input，模式/首启/Digest配置revision等可变配置token变更为stale_context，不在原operation换新输入。恢复不重新计算now、next_digest_at或近期/开放时间门槛；跨过08:00、日期或截止时间本身不是stale_context。要按新时刻判断是否过期，需新operation_id。
4. 每项原子提交decision、selected/effective_route、对应意图及operation结果。进程中断后只继续原集合未完成项；失败明确记结果/未完成，不扩展集合。新时间/新政策/新成员要使用新operation_id。
5. preview无投递副作用；apply才登记邮件资格。不同operation不能抢已锁定事件；第一版不支持资格撤销、取消后改投或持续按时钟重评。operationID只防重复操作，不作为邮件唯一键；邮件沿用原event身份。

## 6. N0/N1 验收补充（尚未执行）

| ID | 场景 | 预期 |
| --- | --- | --- |
| D1 | 近期但充分文本无关注/保存命中；近期相关高价值机会 | 前者IGNORE，后者PUSH_NOW；日期不能独自决定Action。 |
| D2 | 高价值当天截止但学院/年级未知、条件OR/附件不足 | PUSH_NOW+needs_review/missing_fields；显著标资格待核实，不延期到明天。 |
| D3 | 本科推免正文含“研究生”；明确仅研究生且用户已知本科 | 前者不能裸词排除；后者ineligible、无主动邮件，有参考价值STORE_ONLY，否则IGNORE。 |
| D4 | 已确认相关的媒体型/附件条件通知，分别紧急/普通；完整正文加补充附件 | PUSH_NOW或DIGEST与needs_review独立；仅补充附件不自动造成资格未知；兴趣不明STORE_ONLY可核对。 |
| D5 | 相关已过期历史；低优先截止缺年份/未来日期 | 前者STORE_ONLY，后者DIGEST+needs_review；不能猜日期；日期区间下界过不自动标已关闭。 |
| D6 | 首启老历史；非紧急近期相关含未知资格；明确无关 | baseline；近期候选按规则通常digest，unknown仍标核对；无关IGNORE，无new/activation双事件。 |
| D7 | 首启候选详情失败到第8天；列表日期未知但详情先成功；重复activate/列表/304 | 固定窗口保留；详情可补分类而非先静默完成；以当前可核期限判断；仅一次候选/投递身份。 |
| D8 | 在独立成员样例中模拟旧数值ID删除复用、新来源身份复用该ID | 成员判断按来源身份；旧身份仍属首启集合，新身份不被错误压制；验证生成DDL而非只看Column参数。不删除真实业务数据。 |
| D9 | event/decision/有效路线/立即意图任一写失败 | 整组回滚；effective_route=digest资格可以暂没有outbox，仍锁定。 |
| D10 | STORE_ONLY/IGNORE当前live候选显式重评；同政策新操作时已截止 | 保留原event，新decision/时刻；有参考价值过期为STORE_ONLY，不伪造update。 |
| D11 | 带needs_review邮件已计划/accepted/uncertain后修改Profile | 只预览，冻结decision/路线/成员/Message-ID不变，不再发一封“资格已确认”。 |
| D12 | 已关注机会真实改截止/取消；稍后再A→B→A | 新事件提醒条件变化；回退有新seq；不因current IGNORE或曾发送而漏掉真实更新。 |
| D13 | PUSH_NOW+digest_only；hybrid首启紧急截止≤下一Digest；首启非紧急 | 分别digest/immediate/digest；仅最终immediate建立即任务，N2不二次选择。 |
| D14 | route=digest、资格登记时间非空、outbox为空 | 新持久重评被拒绝；同operation已有结果重放仍原样返回。 |
| D15 | 同operation_id只传ID恢复；重复显式相同参数；恢复跨过08:00及截止时间 | 复用原集合/policy/evaluated_at/next_digest_at/input/routing和结果，不查当前候选或用当前时钟；重新判断过期须新操作。 |
| D16 | 同ID改集合、policy、时刻、任一输入或路由参数 | operation_parameters_mismatch，无新增决策/意图；新需求须新ID。 |
| D17 | 未完成成员的live版本/配置revision变了 | stale_input/stale_context，不在原operation换输入；成员结果与意图同事务。 |
| D18 | 正文只含菜单“教学科研”、辅修退课限制、已有助教或违规取消资格 | 不判研究申请/辅修报名/助教招聘/机会整体取消；真实样本中保留反例。 |
| D19 | 同篇科研选课：research主动+course_enrollment仅保存；随后去掉主动兴趣 | 前者按科研紧急/高价值规则提醒，不被保存主题压制；后者STORE_ONLY；同主题关注与仅保存仍服从仅保存。 |

N0交付纯函数、契约、固定小规则；首批10个Action标签已于2026-10-08获用户确认，范围与记录见[评估记录](notification-rule-evaluation.md)，不等于生产规则已验收。N1负责持久化/事务与操作重放，N2只消费已登记effective_route及锁定decision。实现仍按[通知模块任务](email-implementation-plan.md)安排。
