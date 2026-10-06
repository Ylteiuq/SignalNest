# 首批通知规则与真实样本评估

2026-10-05取样，2026-10-06补充；**研究探针已运行，期望Action尚待人工确认**。不是生产N0，不是已通过的准确率评估。本文件补[通知决策](notification-decisions.md)，保持一个来源、有限GET、不取附件/图片。

## 1. 规则清单与支持边界

可复现的词组/表达式数据：[rules.json](experiments/notifications/rules.json)，研究修订v3。N0发布规则必须独立版本化，并用确认后的样本验证；不要直接当作完整实现复制探针。

| 主题 | 首批词组/判断 | 真实证据与状态 |
| --- | --- | --- |
| research | 科研训练、本科生创新研究基金、大学生创新创业训练计划、大学生创新训练计划 | 128291/17361/14147/127511有正向主题；标题优先，正文需同句申请/报名等行动证据，排除登录菜单。 |
| minor | 辅修专业 | 117011正向申请；128231“辅修专业单独缴费…不允许退课”是反例，不能仅正文裸词命中。 |
| course_enrollment | 选课通知、新生选课 | 两份旧fixture及128291；可作为store_only_topics，不能覆盖同时明确研究机会的主动兴趣。 |
| teaching_assistant | 助教/教学助理与招聘/招募/选聘在同语句近邻；禁止裸“助教” | 128291提到现有助教、辅导课，验证了负例；**招聘正样本未取得**，候选规则仅预览，启用前补正例并人工确认。 |
| exchange / scholarship | 交换生、海外交流项目、国际交流项目；奖学金申请/奖学金评选 | 当前小批未覆盖，不宣称已验证；首批自动策略先限已核定主题。 |

**阶段：**标题“结题验收结果/结题结果/验收结果/录取名单/立项名单”通常参考存档；17361虽有“中期检查暨立项”，正文仍允许补报，不能只靠阶段名静默。申请主体与项目最终受益学生要分开，14147当前是教师提供选题。

| 资格表达式 | 支持/限制 |
| --- | --- |
| `YYYY级新生`明确对象 | 比较entry_year；128231可测已知不匹配/缺值。学年“2026–2027”不是入学年份。 |
| “选课对象与要求：全日制在校本科生” | 128291比较study_level；已知研究生明确不匹配。本科只匹配一部分，Profile未表示全日制/当前在校状态，保留full_time_enrollment/current_enrollment未知；另有“每人每学期限选一门”，数量未核对。“建议英语…”不作为强制排除，学分只能兑换一次不等于课程不能重复报名。 |
| “面向全校教师征集”且为本次申报主体 | 14147比较role；role未知须unknown，不能自动不符合。 |
| GPA、主辅修专业大类、年级例外、持续自主科研团队/项目状态 | 117011/17361是真实未知条件；首版不收集或强行判断，missing_fields明确列出，不阻断紧急提醒。 |
| 限某学院/专业的通用表达式 | 本批无可信正例；不能仅凭原设计示例宣称支持，补样后再启用。 |

| 时间表达式 | 具体行为/证据 |
| --- | --- |
| 完整`YYYY年M月D日HH:mm` | 128291选课截止为2026-09-28 23:59；另有改选截止，按行动上下文选选课截止，不能直接取全篇最后日期。 |
| 同句区间`2024年12月2日9:00至12月9日24:00` | 117011终点继承同一区间起点年份；24:00为次日00:00。跨年不自增，若冲突就unknown。 |
| 完整年份日期但只有“日前” | 17361团队/导师/学院是不同截止，不混取最后一个；团队2月25日前保留日期边界区间与needs_review。不得把推断日初当确定已过期时刻。 |
| 单独月日、预计、另行通知、学年 | 128231/legacy/14147仍time_unknown；不从发布日期/项目标题自动猜年份。 |

取消规则候选必须为整项机会明确取消/终止，并关联曾登记邮件资格的机会；18135“违规取消参赛资格”只是条件，不是整项取消。**本批没有真实取消正例，也没有同URL取消前后快照**；不能据此启用自动取消提醒或宣称真实更新链已验证。合成接口场景可另测，但不能混入真实gold集。

本轮有限搜索用`site:uc.whu.edu.cn 助教 招聘/招募/选聘`及`site:uc.whu.edu.cn 取消 报名/课程/考试`、`终止/中止 项目`等查询；助教命中是现有助教介绍/教学新闻，取消命中是参赛资格条款，均未找到符合类别定义的原站正例，未为此追加取页。**这是样本缺口，不是网站不存在此类通知的证据**；搜索索引覆盖未知。

## 2. 取样证据

已有2份详情原文直接复用；新增6份公开详情原文。普通沙箱5次DNS连接失败，没有取得响应；允许的直接HTTP环境随后每URL一次GET成功，串行间隔5秒（最后1页单独补取），未重试站点、遍历分页、下载附件/图片或访问账号。HTTPX trust_env=False、TLS开启、不跟随重定向；头白名单保存，Set-Cookie打码。

| 原站通知 | 新fixture及采集元数据（UTC） |
| --- | --- |
| [科研训练选课128291](https://uc.whu.edu.cn/info/1517/128291.htm) | [HTML](fixtures/notifications/notice-128291-20261005T133533Z.html) / [metadata](fixtures/notifications/notice-128291-20261005T133533Z.json)，13:35:33–13:35:33，200 |
| [大创补报17361](https://uc.whu.edu.cn/info/1517/17361.htm) | [HTML](fixtures/notifications/notice-17361-20261005T133538Z.html) / [metadata](fixtures/notifications/notice-17361-20261005T133538Z.json)，13:35:38，200 |
| [教师征集选题14147](https://uc.whu.edu.cn/info/1517/14147.htm) | [HTML](fixtures/notifications/notice-14147-20261005T133543Z.html) / [metadata](fixtures/notifications/notice-14147-20261005T133543Z.json)，13:35:43–13:35:44，200 |
| [辅修报名117011](https://uc.whu.edu.cn/info/1517/117011.htm) | [HTML](fixtures/notifications/notice-117011-20261005T133549Z.html) / [metadata](fixtures/notifications/notice-117011-20261005T133549Z.json)，13:35:49，200 |
| [数智竞赛18135](https://uc.whu.edu.cn/info/1517/18135.htm) | [HTML](fixtures/notifications/notice-18135-20261005T133554Z.html) / [metadata](fixtures/notifications/notice-18135-20261005T133554Z.json)，13:35:54，200；当前Parser拒绝 |
| [科研结题结果127511](https://uc.whu.edu.cn/2022/show.jsp?urltype=news.NewsContentUrl&wbtreeid=1517&wbnewsid=127511) | [HTML](fixtures/notifications/notice-127511-20261005T133829869212Z.html) / [metadata](fixtures/notifications/notice-127511-20261005T133829869212Z.json)，13:38:29–13:38:31，200 |

各metadata保存实际客户端started/finished时间、来源/最终URL、状态、字节数、SHA-256与白名单头；新原文未改写。已有[current](fixtures/current-notice-detail.html)/[legacy](fixtures/legacy-notice-detail.html)来源及旧采集口径见[原报告](whu-undergrad-notice-source.md)。

## 3. 拟议标签：需要人工确认

[cases.json](experiments/notifications/cases.json)固定原文hash、虚构Profile、明确历史evaluated_at、context/mode和拟议Action。Profile不是当前用户信息：WHU本科、role=student、入学年通常未知，关注research/minor/teaching_assistant；选课仅保存；科研/辅修是高价值主题。S01指定2025级、S05指定研究生、S11无高价值主题。

**回放时钟不是实际发现当天。**S04/S08用真实原文的截止当日09:00模拟“今天截止”；没有声称这篇通知发布当日就截止，也没有改原文日期。S06只有日期边界，核对标记保留。

| ID | 原文/情境 | 拟议Action | 探针needs_review / 原因 |
| --- | --- | --- | --- |
| S01 | 128231新生选课，2025级Profile | STORE_ONLY | true，截止年未明；已确认入学年不符合，不主动提醒 |
| S02 | 同篇，入学年未知，仅保存选课 | STORE_ONLY | true，entry_year与时间未知 |
| S03 | legacy选课，时间图片/细则附件 | STORE_ONLY | true，保留媒体/对象未知，不冒充已核对 |
| S04 | 128291，2026-09-28 09:00，科研高价值 | PUSH_NOW | true，全日制/在校未表示、当学期选课数量未核对；明示截止，不是助教招聘 |
| S05 | 同篇但虚构研究生Profile | STORE_ONLY | false，明确本科对象不匹配 |
| S06 | 17361，2月24日09:00，科研补报 | PUSH_NOW | true，团队/项目条件未知、日期边界；不推迟到Digest |
| S07 | 14147教师提供选题，学生Profile | STORE_ONLY | true，当前主体明确不匹配；截止年份未知、后续参考价值 |
| S08 | 117011，12月9日09:00，辅修当天截止 | PUSH_NOW | true，GPA/专业/年级例外/附件条件未知，邮件需显著标注 |
| S09 | 127511科研结题结果 | STORE_ONLY | false，只保存结果，不当成开放申请 |
| S10 | 18135当前Parser失败 | 不产生决策 | 不纳入成功分类；原文取消资格不是机会取消，负例另记录 |
| S11 | 128291，9月10日普通科研关注 | DIGEST | true，全日制/在校未表示、当学期选课数量未核对；非高价值且非临近截止 |

全部annotation_status=pending_human，human_expected_action=null；**没有gold准确率**。人工确认应针对固定Profile/时刻及理由，不只看标题；确认记录应有审核者/时间与对应文件hash，之后冻结gold版本。修改标签保留版本，不能重运行build_cases覆盖确认记录。

## 4. 已运行结果及修正

离线环境：macOS26.6.2 arm64、CPython3.12.14、SQLAlchemy2.0.54、SQLite3.53.1、HTTPX0.28.1、BeautifulSoup4.15.0、Pydantic2.13.5，未新增依赖。使用真实parse_notice与hash校验，再跑研究词组/资格/时间子集和路线纯函数。[结果](experiments/notifications/results.json)，[首次基线](experiments/notifications/results-baseline.json)，[v2结果](experiments/notifications/results-v2.json)。

- 8个不同真实页面、11个Profile/时钟案例；10例解析成功，1例Parser失败（18135 body结构未通过），不是无关/空内容。
- v3输出PUSH_NOW=3、DIGEST=1、STORE_ONLY=6；10例中needs_review=8、eligibility unknown=7。后一项含只保存参考资料、无需报名资格的案例；已匹配主动兴趣且非结果的6例中，unknown资格=4（3个PUSH_NOW、1个DIGEST）。
- 这是偏向难例、重复页面/虚构Profile的小样本，**不能推断全站未知比例或召回率**。缺失原因分为真实Profile缺值、GPA/团队/附件条件未支持、时间信息不完整，不将unknown一律静默/延期。
- 初次v1把新生选课中“辅修专业单独缴费…不允许退课”误识别为辅修兴趣，S02输出DIGEST。v2要求标题或正文同句行动证据、排除登录菜单，S02改STORE_ONLY。原结果保留，尚待人工确认这个修正符合期望。
- v2将“全日制在校本科生”只比较study_level，错误把S04/S11判eligible。v3保留全日制/在校状态及当学期选课数量未知；Action不变、needs_review改true。首版可暂不增加Profile字段，但不能声称已符合；若用户确需减少这类未知，另行明确定义结构化事实。人工确认前不能将v2的false核对标记作为gold。
- 路线探针4项通过：未知资格紧急PUSH_NOW可immediate；digest_only在登记前输出digest；首启可信紧急截止逃逸保留immediate；非紧急首启默认digest。只验证纯决策合成，**未验证生产outbox/事务/重启**。
- 真实助教招聘、整体机会取消、限定学院资格正例仍缺；已有助教提及/取消资格负例、科研/资格未知/明确不符合/图片附件/当天截止回放。不能用合成正例冒充真实覆盖。

N0启用前：人工确认拟议标签、补助教招聘/取消正例或显式将对应规则限制为预览、用生产实现重跑确认后的gold。N1持久接口可先按已统一Action/needs_review/effective_route与重评冻结契约设计，不因这次探针就宣称通知策略已验收。

## 5. 当前N0快照与研究建议的对齐

**额外离线验证，不是gold验收。**并行N0代码已存在，故另用它实际支持的虚构Profile（WHU本科，research/minor主动且高价值，选课可选仅保存）跑7个固定样本/时钟情境。没有静默把研究Profile的role/teaching_assistant转换成现有接口；完整输入、HEAD与5份源码SHA在[n0-snapshot-result.json](experiments/notifications/n0-snapshot-result.json)，运行脚本为[audit_n0_snapshot.py](experiments/notifications/audit_n0_snapshot.py)。HEAD是`1c827773544027850912148007d55a6300d41d0b`，N0文件为并行未提交内容，**不能仅靠HEAD重现**；结果只描述所记hash的快照。首轮后facts.py被并行修改，保留[更新前结果](experiments/notifications/n0-snapshot-before-update.json)并重跑；7例Action/route结论相同。运行前/导入后/运行末核对源码hash，拒绝混合正在改变的版本；未来新快照应另存，不覆盖本次证据。

| 实际快照行为 | 实现前的具体调整 |
| --- | --- |
| A01同篇128291有科研主动兴趣、选课仅保存时STORE_ONLY；A02去掉保存主题后PUSH_NOW | [decision.py](../src/signalnest/notifications/decision.py)的`elif stored`先于紧急分支。保存主题只约束该主题；不能压住另一主动主题。增D19跨主题场景，同主题的仅保存语义仍保留。 |
| A03科研课程真实截止当日正确提取23:59，PUSH_NOW+资格unknown；全日制/在校残余未被判符合 | 当前N0的未知保护正确；v2研究探针自身遗漏已修正，不能将这个错误归给N0。核对字段可细分，建议英语分数不能当硬门槛；当学期课程限额仍须核对。 |
| A04辅修当天截止输出DIGEST、时间unknown；A05大创团队临近截止也DIGEST、时间unknown | [facts.py](../src/signalnest/notifications/facts.py)目前不支持无年终点/24:00，团队“日前”也未归入可用截止。[既有测试](../tests/test_notification_facts.py)明确按unsupported验收，并非偶发异常。首批按真实117011同句起年继承/24:00及17361团队截止补齐；其他歧义继续unknown，不能选导师/学院的较晚期限。 |
| A06新生选课保存主题为STORE_ONLY；A07教师征集选题资格unknown，但近期高价值→PUSH_NOW | 研究预期S07为保存参考、当前主体明确不匹配。现Profile没有role；`study_level=faculty`不足以表达学生/教师行动角色。N0应加入可空主体角色并仅对明确主体表达式作三值判断，或先将未支持主体分类限制为预览。不是把所有“教师”词变排除。 |

研究Profile还使用`graduate`及teaching_assistant；当前Profile培养层次枚举为master/doctoral等，主题尚无teaching_assistant。S01–S11尚不能原样作为N0接口测试输入；实现Agent须显式对齐契约/标签版本，不能用自动删字段后的结果宣称同一gold通过。

**N1迁移前统一：**[当前N1接口稿](../docs/notification-state.md)已有Action/needs_review/effective_route口径，但表清单尚未明确`delivery_intent_registered_at`和冻结重评operation记录。按决策§5补齐：待计划Digest已经锁定；operation绑定原集合/政策/时钟/输入，ID-only resume复用结果，参数不符零副作用。时钟跨08:00或截止本身不使冻结context失效。此项只需窄记录，不扩展任务平台。

仍可延后：助教/取消正例未取得时保持规则预览或关闭；OCR/附件全文、全站未知比例估计与自动资格撤销不进入N1。上线前仍需人工标签和生产N0重放，这次7例快照没有准确率、数据库事务、SMTP或崩溃恢复结论。
