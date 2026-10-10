# 当前通知规则的离线评估

评估器调用项目实际 Parser、事实提取和决策函数；离线工程期望用于回归，不是人工准确率、全站召回率或资格判定。固定输入与合成输入分开保存，所有 Profile 均为示例配置，不代表当前用户。

## 当前记录

- [固定输入清单](notification-production-cases.json)与[真实通知汇总](notification-production-current.json)
- [合成输入清单](notification-production-synthetic-cases.json)与[合成当前结果](notification-production-synthetic-current.json)
- [助教回归清单](teaching-assistant-cases.json)与[当前结果](teaching-assistant-current.json)
- [有限指令回归清单](notification-current-instruction-cases.json)与[当前结果](notification-current-instruction-current.json)
- [原始漏报表达清单](notification-original-misses-cases.json)与[当前结果](notification-original-misses-current.json)
- [CS 离线输入](cs-offline-cases.json)与[紧凑汇总](cs-offline-current.json)

历史材料保留聚焦的 v5/v7 失败回归、清单变更记录和一个精简 v6 决策兼容样本。重复的多版本全量输出已从待推送内容中裁掉；测试继续比较固定输入、关键 Action 和版本边界。

## 版本与运行

当前 facts/rules/decision 为 v8，routing 为 v1；WHU、EMS 和 CS Parser 分别保持自身版本。变更规则不会静默重写旧决策或冻结邮件。新代码需要由实例明确迁移和启用。

从仓库根目录运行评估器时，为每份输出指定一个尚不存在的路径：

    uv run --locked python deploy/evaluate_notifications.py --check --output /tmp/signalnest-notification-current.json
    uv run --locked python deploy/evaluate_notifications.py --check --cases docs/validation/notification-original-misses-cases.json --output /tmp/signalnest-notification-misses.json

不传 --output 时结果写到标准输出；已有输出文件不会被覆盖。详情见[通知规则说明](../notifications.md)和[维护入口](../notification-maintenance.md)。
