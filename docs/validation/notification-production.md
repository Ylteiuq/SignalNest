# 当前生产通知规则的离线评估

本次用实际 `parse_notice → extract_facts → decide` 重放 8 份已有真实原文、13 个固定情境。没有联网、读取个人配置、写业务数据库或发送邮件。这里的“生产”指代码入口，而非已经为某个运行实例启用了新政策。

[固定输入](notification-production-cases.json)包含原文 SHA-256、来源 URL、虚构 Profile、显式历史时钟、事件上下文和工程回归期望；[修复前 v2](notification-production-before-v3.json)与[当前 v3](notification-production-current.json)保存版本、源码摘要、输入摘要、环境和有限原文证据。`research/` 中原探针、标签及旧快照保持原样。

原研究 S01–S11 的 `teaching_assistant` 尚不是受支持主题，本次明确移除；研究中的 `graduate` 明确具体化为 `master`，不声称涵盖全部研究生情境。竞赛例显式加入 `competition` 兴趣，当前 Parser 已能解析。P05/P07 沿用生产对明确主体不匹配的 `IGNORE` 语义，不把原研究拟议 `STORE_ONLY` 标签冒充为同一契约验收。P12/P13 用仅关注辅修的 Profile 隔离负例与正例。

## 结果

| 案例 | 真实样本与情境 | v2 | v3 |
| --- | --- | --- | --- |
| P01 | 新生选课，画像为 2025 级，选课仅保存 | DIGEST | STORE_ONLY |
| P02 | 新生选课，入学年未知，选课仅保存 | DIGEST | STORE_ONLY |
| P03 | 旧式选课，包含图片/附件，选课仅保存 | STORE_ONLY | STORE_ONLY |
| P04 | 科研训练选课，截止当天，另有选课仅保存主题 | PUSH_NOW | PUSH_NOW |
| P05 | 同一科研机会，画像为硕士 | IGNORE | IGNORE |
| P06 | 大创补报，学生团队截止临近且有边界不确定性 | PUSH_NOW | PUSH_NOW |
| P07 | 教师征集选题，画像明确为学生 | IGNORE | IGNORE |
| P08 | 真实辅修报名，第一轮当天 24:00 截止 | PUSH_NOW | PUSH_NOW |
| P09 | 科研结题结果 | STORE_ONLY | STORE_ONLY |
| P10 | 真实竞赛报名，显式竞赛高价值兴趣 | PUSH_NOW | PUSH_NOW |
| P11 | 科研机会，普通关注、非近期截止 | DIGEST | DIGEST |
| P12 | 新生选课，仅关注辅修 | DIGEST | IGNORE |
| P13 | 真实辅修报名，仅关注辅修 | PUSH_NOW | PUSH_NOW |

修复前 3 个新生选课情境因“辅修专业单独缴费”误入 Digest；修复后不再取得 `interest.topic.minor`。真正的辅修报名仍能立即提醒，科研主动机会也仍优先于选课仅保存。`IGNORE`/`STORE_ONLY` 均没有邮件路线，不代表删掉原通知或已经确认所有资格条件。

当前事实/规则/决策版本分别为 `whu-notice-facts-v3`、`notification-rules-v3`、`notification-decision-v3`；Parser 仍为 `whu-student-notices-v3`，路线合成仍为 `notification-routing-v1`。13 例工程回归全部符合：v2 的 PUSH_NOW/DIGEST/STORE_ONLY/IGNORE 数量为 5/4/2/2，v3 为 5/1/4/3；立即邮件路线均为 5 例，待核对均为 12 例。减少的是旁带辅修误命中，未减少这批真实科研或辅修机会的紧迫提醒。

原文、Profile、事件和评估时刻固定；规则升级不重写历史快照。本次不是人工 gold，也不是准确率或全站召回率评估。原研究人工标签仍待确认，未补助教招聘、整体取消等真实正样本。其他未知资格、时间或媒体依赖继续出现在结果的 `unknowns` 与 `needs_review`；P01 的入学年份资格仍可能 unknown，本轮没有扩展其提取规则。

## 复现

先按 README 安装项目。以下命令从仓库根目录执行，离线调用生产函数；新快照文件必须尚不存在：

```sh
.venv/bin/python deploy/evaluate_notifications.py --check \
  --output /tmp/signalnest-notification-replay.json
```

不传 `--output` 时仅向标准输出写 JSON；传入已有文件会返回 2，防止覆盖旧证据。`--check` 在工程回归期望不符时返回 1，但仍保存实际结果，不将其改成期望值。输入/原文摘要/路径或源码一致性检查失败返回 2。帮助不读取 fixture 或创建数据文件。

报告记录实际 HEAD 和工作区生产源码 SHA-256；并行未提交代码不能仅凭 HEAD 重现。重放前、导入后和结束时检查源码摘要，期间变更会拒绝生成混合报告。正文全量文本不复制进报告，保留 Unicode 位置、有限引用、事实及决策摘要，便于回查原 fixture。后续更换 Profile、时钟或工程期望应保存新的输入/结果快照，不覆盖本次记录。

新实例按当前代码启用；已有实例仍需按照[通知规则说明](../notifications.md)显式预览并更新政策，代码升级本身不会改变已登记决策或冻结邮件。
