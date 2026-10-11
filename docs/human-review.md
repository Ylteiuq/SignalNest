# 独立人工评价

工程回归与人工评价分开。31 个固定通知情境、15 个搜索查询仍保留原工程标签和测试；
新工具不会把这些期望或系统输出填成真人意见，不修改生产决策、政策、数据库或冻结邮件。
当前[待确认报告](validation/human-review-pending-20261011.json)中，46 项均 pending，人工统计分母为零、率为 null。

## 冻结输入

先使用现有离线评估器生成**完整**结果；精简 summary 没有逐项输入，工具明确拒绝。
结果写到自己受限的工作目录，保留原 report 和引用的原 fixture。这里的 review-dir 须已存在。

```sh
python deploy/evaluate_notifications.py --check --output review-dir/notification-production.json
python deploy/evaluate_notifications.py --check \
  --cases docs/validation/notification-production-synthetic-cases.json --output review-dir/synthetic.json
python deploy/evaluate_notifications.py --check \
  --cases docs/validation/teaching-assistant-cases.json --output review-dir/teaching-assistant.json
python deploy/evaluate_notifications.py --check \
  --cases docs/validation/notification-current-instruction-cases.json --output review-dir/instruction.json
python deploy/evaluate_notifications.py --check \
  --cases docs/validation/notification-original-misses-cases.json --output review-dir/original-misses.json
python deploy/evaluate_notifications.py --check \
  --cases docs/validation/cs-offline-cases.json --output review-dir/cs-offline.json
python deploy/evaluate_search.py --check --output review-dir/search.json
python deploy/human_review.py prepare --notifications review-dir/notification-production.json \
  review-dir/synthetic.json review-dir/teaching-assistant.json review-dir/instruction.json \
  review-dir/original-misses.json review-dir/cs-offline.json \
  --search review-dir/search.json --output review-dir/bundle.json
python deploy/human_review.py report --bundle review-dir/bundle.json
```

Bundle 的每个 item 有稳定限定 ID、输入、input_sha256、系统判断及证据；工程期望被剔除。
通知输入绑定原文/内容摘要、页面 URL/Parser、完整 Profile、事件上下文和固定时钟；
搜索输入绑定查询、日期/来源过滤及**整个**语料身份/原文/内容摘要。Bundle 摘要同时冻结预测、事实、版本及原回放出处。
可从 bundle 查看合成原文或沿 fixture/URL 核对真实原文、资格和时间；不能仅看标题或模型给的 Action 批量确认。
工具只接收已有离线快照，不自动读取私人实例正文；日后真实样本导出须明确同样的输入边界。

## 逐项标注

使用 bundle 实际 item_id；通知 ID 为 notification:<结果文件 stem>:<case_id>，搜索为 search:<query_id>。
下列 Action/相关集只是命令语法示例，不是对任何当前案例的人工作答。由真人核对后填写：

```sh
python deploy/human_review.py label --bundle review-dir/bundle.json \
  --item-id 'notification:notification-production:P01' --reviewer '<操作者>' \
  --at '<实际带时区的核对时间>' --action DIGEST --output review-dir/p01-label.json
python deploy/human_review.py label --bundle review-dir/bundle.json \
  --item-id search:S03 --reviewer '<操作者>' --at '<实际带时区的核对时间>' \
  --relevant-id ems-250571 --output review-dir/s03-label.json
# 完整相关集为空时，--relevant-id 后不填成员；信息不足可显式 --pending --note。
python deploy/human_review.py report --bundle review-dir/bundle.json \
  --labels review-dir/p01-label.json review-dir/s03-label.json
```

每份标签独立绑定 bundle_sha256/item_id/input_sha256、reviewer、真实 reviewed_at 与 optional note。
pending 不允许同时填 Action 或相关集；confirmed 必须明确给出恰当类型的判断。
搜索相关集从整个冻结语料选择，包括系统没有返回的相关通知；空集是明确人工判断，不等于未标注。
未知成员、重复成员、无时区时间、跨类型标签明确失败。工具没有身份认证，reviewer 是操作者声明。

所有写入拒绝覆盖已有文件。更正时保存新记录，并在 report 中**显式**选择有效记录；旧文件保留。
两份不同判断冲突会失败，不能自动取最新或多数票。完全重复的记录去重。
旧 bundle 或旧输入标签列为 stale，相关项目继续 pending；输入/规则重放后不可静默套用旧标签。
不修改工程期望文件或已有历史实验；新评估快照会有新的 bundle 摘要，需显式决定是否重新核对。

## 统计口径

只统计精确绑定且 confirmed 的样本，另报 pending/stale。通知同时报告四种 Action 的混淆，
以及提醒二分类：PUSH_NOW/DIGEST 为提醒，STORE_ONLY/IGNORE 为不提醒。
FN = 人工要提醒、系统不提醒；FP = 人工不提醒、系统提醒。
漏报率 FN/(TP+FN)，误报率 FP/(TN+FP)，无分母为 null。
人工 PUSH_NOW 而系统未 PUSH_NOW 另外列 urgent_miss_items；Digest 的延误不会被二分类一致掩盖。
STORE_ONLY 与 IGNORE 的差异仍在 Action 混淆中体现，不合并它们的产品含义。

搜索非空人工相关集计算宏平均 Recall@1/3/5，空集另报是否正确返回空。
这只是**已标注的固定小样本子集**，没有随机采样或真实用户日志，不能推断真实全站漏报率或召回率。
本次未替用户创建真实 confirmed 记录，也未宣称已有人工 gold。

`prepare_review`、`make_label`、`review_report` 是不依赖 CLI 的纯函数。
CLI 成功退出 0；有限输入、摘要绑定、冲突与文件错误退出 2。帮助/导入无业务存储副作用。
