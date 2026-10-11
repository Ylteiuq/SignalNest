# 只读运行汇总

`deploy/summarize_observations.py` 只读取已有 JSONL 文件；不打开实例数据库、不取写锁、不联网、不修复。
输入可以是 `signalnest observe` 的逐行 JSON，或下面的完整 journal 导出：

```sh
journalctl --namespace=signalnest -u signalnest-observe@signalnest.service \
  --since '@1791694219' --output=json --all --no-pager > uc-observations.jsonl
journalctl --namespace=signalnest -u signalnest-observe@cs.service \
  --since '@1791694219' --output=json --all --no-pager > cs-observations.jsonl
python deploy/summarize_observations.py uc-observations.jsonl cs-observations.jsonl \
  --start 1791694219 --source whu-undergrad-student --source whu-cs-undergrad-teaching
```

输出到 stdout；由调用者保存到受限目录，不提交完整私人观察。`--end` 默认实际系统时间，
重现既有汇总时可明确给出当时真实 Unix 秒。不得通过未来 end 冒充已经观察七天。
本轮固定窗口起点 2026-10-11 12:50:19，最早七天边界 2026-10-18 12:50:19 上海时间。

- 每个来源按实际 at 排序，保留积压 total/due/deferred、已成功数、失败数、复查 due、冷却、暂停和六类投递计数。
- `net_changes` 是首末有效快照的净变化，不是新增通知数、处理吞吐或失败率；期间新增身份也会改变积压。
- `gaps` 记录窗口首尾及相邻样本间超过 `interval_seconds+tolerance_seconds` 的区间。
  默认每小时一次、允许 15 分钟抖动；完全缺失的显式来源也会报告，不推断持续在线。
- 从实际 `last_complete_scan_at` 重新计算年龄；超过 26 小时为 stale，无 complete 证据为 missing。
  `coverage_stale_at` 仅声明哪些观察时刻过期，不猜缺失期间是否成功扫描。
- 缺库/读取阻断的样本保留未知，不能填零。未激活或无法读邮件的样本不当作“未暂停且健康”。
- 同来源同秒的相同指标去重；冲突指标、损坏观察、来源/时间绑定不符使整个汇总失败。
  窗口外有效样本单独计数，不纳入趋势。普通 journal 消息跳过并计数；null MESSAGE 明报不可读，不能补造。
- 只投影需要的计数、时间和发布内容摘要，不输出个人画像、地址、正文或凭据。
  `selected_evidence_sha256` 绑定投影指标，不是原始日志 bytes 的摘要。

成功产生报告退出 0，包括报告有缺口或过期；输入/参数错误退出 2。
`seven_days_elapsed` 仅是窗口长度判断，`rollout_acceptance` 始终 requires_human_evidence_review。
工具不能核实首页复核、SMTP 实收、休眠、宿主重启、真实恢复或异常原因；须结合实际运行/备份/journal 和收件确认。
原始观察是分别读取的只读快照，不是跨所有表的一致备份。

库入口 `read_observations(paths)` 与 `summarize_observations(records, start=..., end=..., expected_sources=...)` 可独立复用。
模块导入无读取副作用；不新增定时器或告警策略。观察窗口不中途重置，维护停机/修复另记时间与版本。
