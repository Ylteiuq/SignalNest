# 当前生产通知规则的离线评估

本文起于 2026-10-08 新迭代 N0（通知规则修正与评估对齐），当前快照随新 N1 升为 v5；不重命名仓库早先邮件模块的 N0–N4。使用实际生产函数离线重放，未联网、读取个人配置、写业务数据库或发送邮件。“生产”指代码入口，不代表某个实例已启用新政策。

## 输入、人工确认与工程期望

[真实固定输入](notification-production-cases.json)保存 8 份已有真实 HTML、13 个固定情境的原文 SHA-256、来源 URL、虚构 Profile、显式历史时钟、事件上下文和工程回归期望。本轮保留这份输入文件原样。[两个最小合成输入](notification-production-synthetic-cases.json)单独保存明确的 `synthetic_notice_content` 和内容摘要，直接执行 `extract_facts → decide`；它们不是抓到的 HTML，不计入真实页面或 Parser 成功数，没有人工标签。

研究 S01–S09、S11 的 **10 个原固定输入 Action 已于 2026-10-08 由项目用户确认**，见[人工登记](../../research/experiments/notifications/human-review-20261008.json)。人工确认仅适用于原 fixture/Profile/事件/历史时钟的 Action，不包括资格事实、每个 `needs_review`、最终路线、生产正确性或准确率。S10 是旧 Parser 失败观察，没有 Action 标签；后来 Parser 解析成功不构成对旧记录的推翻。

[逐例映射](notification-production-mapping.json)记录原研究标签和适配内容。**本批没有整组输入完全相同的研究→生产案例**：P01–P11 的原文、来源 URL、历史时钟相同，但所有 Profile 都去掉了当时生产不支持的 `teaching_assistant`，事件也改用生产契约并显式给出原研究没有的 `next_digest_at`。不能用相同原文代替输入相同，也不能把原研究标签自动移植为生产 gold。

| 生产案例 | 原研究 | 输入关系与额外适配 | 原人工 Action | 工程 Action | 对齐状态 |
| --- | --- | --- | --- | --- | --- |
| P01 | S01 | 适配；2025 级 Profile，其余共同适配见上 | STORE_ONLY | STORE_ONLY | Action 一致，非同一完整输入 |
| P02 | S02 | 适配；入学年未知 | STORE_ONLY | STORE_ONLY | Action 一致，非同一完整输入 |
| P03 | S03 | 适配；旧式选课图片/附件 | STORE_ONLY | STORE_ONLY | Action 一致，非同一完整输入 |
| P04 | S04 | 适配；科研选课当天截止 | PUSH_NOW | PUSH_NOW | Action 一致，非同一完整输入 |
| P05 | S05 | 适配；`graduate` 明确具体化为 `master`，不代表所有研究生 | STORE_ONLY | IGNORE | **Action 语义差异待重新确认** |
| P06 | S06 | 适配；科研补报、团队条件未知 | PUSH_NOW | PUSH_NOW | Action 一致，非同一完整输入 |
| P07 | S07 | 适配；申请主体教师、Profile 为学生 | STORE_ONLY | IGNORE | **Action 语义差异待重新确认** |
| P08 | S08 | 适配；辅修第一轮当天 24:00 截止 | PUSH_NOW | PUSH_NOW | Action 一致，非同一完整输入 |
| P09 | S09 | 适配；科研结题结果 | STORE_ONLY | STORE_ONLY | Action 一致，非同一完整输入 |
| P10 | S10 | 适配；另换为明确竞赛兴趣/高价值 Profile，当前 Parser 已支持正文模板 | 无标签 | PUSH_NOW | 新生产决策，不冒充人工确认 |
| P11 | S11 | 适配；普通科研关注、非临近截止 | DIGEST | DIGEST | Action 一致，非同一完整输入 |
| P12 | S02 原文 | 新增；仅关注辅修的新生选课负例 | 不移植原标签 | IGNORE | 新工程案例 |
| P13 | S08 原文 | 新增；仅关注辅修的真实报名正例 | 不移植原标签 | PUSH_NOW | 新工程案例 |
| Q01 | 无 | 新增合成；标题科研主题，正文即日起报名、当天截止 | 无标签 | PUSH_NOW | 新工程案例 |
| Q02 | 无 | 新增合成；登录系统完成当前科研报名、当天截止 | 无标签 | PUSH_NOW | 新工程案例 |

`STORE_ONLY` 表示按照保存主题、参考信息、未来/历史候选等规则保留但不发邮件；`IGNORE` 表示当前 Profile 下明确不匹配、排除或无可用兴趣命中等，不等于删除通知。两者都没有邮件路线。P05/P07 在生产用 `IGNORE` 表达已知申请资格冲突，而人工原标签是 `STORE_ONLY`；本轮保留双方原值，等待对**适配输入与 Action 语义**重新确认，不改已确认标签迎合实现。

## 结果与历史证据

[修复前 v2](notification-production-before-v3.json)保留此前旁带“辅修缴费”误命中的实际结果；[独立 v3 历史快照](notification-production-v3.json)与本轮开始时原 `current` 文件逐字相同，SHA-256 为 `ee2ba7218fd7e082ea98daa5c22ededd783056ba1f6ca7c4cd9b14403d87feab`。[当前真实案例 v5](notification-production-current.json)重新调用生产 Parser、事实提取和决策；原 13 例未改输入或工程期望。

| 真实案例 | v2 | v3 | 当前 v5 |
| --- | --- | --- | --- |
| P01 新生选课、2025 级、选课仅保存 | DIGEST | STORE_ONLY | STORE_ONLY |
| P02 新生选课、入学年未知、选课仅保存 | DIGEST | STORE_ONLY | STORE_ONLY |
| P03 旧式选课 | STORE_ONLY | STORE_ONLY | STORE_ONLY |
| P04 科研训练选课、当天截止 | PUSH_NOW | PUSH_NOW | PUSH_NOW |
| P05 本科科研机会、硕士 Profile | IGNORE | IGNORE | IGNORE |
| P06 大创补报、截止边界不确定 | PUSH_NOW | PUSH_NOW | PUSH_NOW |
| P07 教师征集选题、学生 Profile | IGNORE | IGNORE | IGNORE |
| P08 真实辅修报名、当天截止 | PUSH_NOW | PUSH_NOW | PUSH_NOW |
| P09 科研结题结果 | STORE_ONLY | STORE_ONLY | STORE_ONLY |
| P10 真实竞赛报名、明确竞赛兴趣 | PUSH_NOW | PUSH_NOW | PUSH_NOW |
| P11 普通科研兴趣、非近期截止 | DIGEST | DIGEST | DIGEST |
| P12 新生选课、仅辅修兴趣 | DIGEST | IGNORE | IGNORE |
| P13 真实辅修报名、仅辅修兴趣 | PUSH_NOW | PUSH_NOW | PUSH_NOW |

另有[合成 v3 失败基线](notification-production-synthetic-v3.json)及[当前合成 v5](notification-production-synthetic-current.json)，固定同一组最小输入：

| 合成案例 | v3 实际 | 当前 v5 | 所验证的边界 |
| --- | --- | --- | --- |
| Q01 | DIGEST | PUSH_NOW | 标题给出科研主题，正文明确开放报名和当天截止；普通兴趣也进入 `deadline_soon` |
| Q02 | IGNORE | PUSH_NOW | 真实登录完成报名不是导航菜单；普通科研兴趣在当天截止前进入 `deadline_soon` |

当前事实/规则/决策版本分别为 `whu-notice-facts-v5`、`notification-rules-v5`、`notification-decision-v5`；Parser 仍为 `whu-student-notices-v3`，路线合成仍为 `notification-routing-v1`。13 个真实工程回归与 2 个合成工程回归分别通过；真实 13 例的 PUSH_NOW/DIGEST/STORE_ONLY/IGNORE 为 5/1/4/3，没有因新增关联规则压掉科研、辅修或竞赛机会。

这不是人工准确率或全站召回率评估；未将 10 个原 Action 标签变为生产 gold，也未把 2 个合成例加入真实样本数。原 13 例仍不混入助教样本。新 N1 单列[六个助教情境](teaching-assistant-cases.json)及[当前结果](teaching-assistant-current.json)：EMS 历史普通/当天/软资格与当前关闭，以及现有助教负例和科研跨主题正例，6/6 符合工程期望（PUSH_NOW 3、DIGEST 1、IGNORE 2），均保留核对提示。显式 parser=ems-notices，不改变默认本科生院来源。自动发现当期助教和整体机会取消仍未验收；[来源调研](../../research/teaching-assistant-source-20261008.md)与[实现边界](../teaching-assistant.md)说明当期登录限制。未知资格、时间、媒体条件和无法可靠关联的主题继续保留在事实及 `unknowns` 中。原研究探针和旧实验结果保持原样，旧错误描述是当时源码行为，不代表当前生产结论。

升级前实际 v4 的[13 例快照](notification-production-v4.json)、[两个合成快照](notification-production-synthetic-v4.json)保持原字节；对应 Action 与当前 v5 一致，事实/政策摘要随版本与助教证据增加而变化。实际 v4 全量 seal 保存在 tests/fixtures/notification-policy-v4.json，用于历史序列化兼容检查。

## 复现

先按 README 安装项目。从仓库根目录离线运行；输出文件必须尚不存在：

```sh
.venv/bin/python deploy/evaluate_notifications.py --check \
  --output /tmp/signalnest-notification-real.json
.venv/bin/python deploy/evaluate_notifications.py --check \
  --cases docs/validation/notification-production-synthetic-cases.json \
  --output /tmp/signalnest-notification-synthetic.json
.venv/bin/python deploy/evaluate_notifications.py --check \
  --cases docs/validation/teaching-assistant-cases.json \
  --output /tmp/signalnest-notification-teaching-assistant.json
```

不传 `--output` 时只向标准输出写 JSON；已有文件返回 2，防止覆盖历史证据。`--check` 在工程期望不符时返回 1，仍保留实际结果，不改成期望值；输入、摘要、路径或源码一致性失败返回 2。帮助不读取 fixture 或创建文件。

报告分别统计 `fixture_cases`、`synthetic_cases`、`parse_success` 与 `decision_success`。真实报告不复制整段正文，仅保留有限引用和 Unicode 位置；合成报告保存小型显式输入，标记 `parser_applied=false`，不伪造 URL、通知身份或原始字节。脚本、Parser、契约、事实、决策与 Profile 源码 SHA-256 在重放前、导入后及结束时核对，发现并行变更即拒绝混合报告；HEAD 本身不足以重现未提交代码。

原文、Profile、事件、时钟和旧结果不会随规则升级改写。已有实例应先预览，按[通知规则说明](../notifications.md)显式更新政策；代码升级不会静默修改旧决策或已冻结邮件。
