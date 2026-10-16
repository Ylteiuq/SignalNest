# 真实知识库上的五题回放

`deploy/replay_query_snapshot.py` 读取既有 `ask` TOML，在 UC／CS 的一致只读快照上运行五个工程问题：科研机会、CS 助教招聘、学生报名与教师征题、模糊截止、无匹配。它不导入样本，不初始化数据库，不采集网页或发送邮件；默认只做本地检索，只有显式 `--model` 才启用配置中的模型。

配置中的实例必须指向已生成、可查询的静态快照，不能一边采集一边回放。程序拒绝非空 WAL，并在每题之后复核数据库、查询配置、实例配置、每来源的快照清单、画像及可选顶层清单的字节摘要；发现变化明确失败。报告还绑定查询版本及现有评估入口列出的源码摘要，每题和结束时复核源码不变；子进程的查询版本也必须与父进程一致。这个检查不能证明快照生成过程可靠，`--snapshot-manifest` 也只绑定该清单摘要，不替代备份验证。

以下时间和路径仅为命令示例，请换成实际查询时钟及自己的受限快照位置：

```sh
uv run --locked python deploy/replay_query_snapshot.py \
  --config /private/signalnest-snapshot/ask.local.toml \
  --snapshot-manifest /private/signalnest-snapshot/prepared.json \
  --at '2026-10-15T12:00:00+08:00' \
  --output /private/signalnest-replays/local.json
```

完整报告可能含个人画像派生信息、真实标题、正文引句和模型回答。输出目录须已存在，输出必须在仓库外；新文件权限为 `0600`，拒绝覆盖。终端只打印有限汇总。模型模式会向选定提供方发送问题、有限正文与画像；密钥仍从标准模型配置指定的环境变量读取，报告不保存环境变量名或密钥。

准备工具生成 `ask.local.toml`、顶层 `prepared.json` 和每来源的 `snapshot.json`；前者默认没有模型。模型回放应复制为同目录的 `ask.model.toml`，按[模型说明](query-agent.md)加入模型块，保留每个实例的 `snapshot_manifest` 绑定。`--snapshot-manifest prepared.json` 只额外绑定顶层准备记录；实际数据库事实与暂停状态由实例配置指向的 `snapshot.json` 校验，两者用途不同。回放入口沿用环境变量凭据，不接收 `--api-key-file`；该选项仅属于单题日常启动工具 `deploy/ask_saved.py`。

当前 `QueryReport.query_version` 为 `campus-query-v4`：最多两轮自主探索，然后保留正文读取与回答步骤，预算紧时提前收束；代理协议或工具失败仍可能降级。`model_usage.exploration_rounds` 与 `forced_tools` 可帮助定位检索、读取和回答阶段。v4 在原预算内最多一次引用纠正，`answer_corrections` 记录次数，HTTP重试层不变；纠正后的 answered 仍保留工具失败和降级标记，不当作无错误通过。v1/v2/v3 的旧报告与人工记录保持原样，v4 新建独立输出，不沿用旧输入标签。外层回放格式仍为 `signalnest-query-snapshot-replay-v1`，它与查询行为版本是两个字段。

## 每题独立预算

每题获得配置中完整且相同的 `limits` 和模型单题费用额度，剩余额度不转给下一题。五题发起前，批次请求上限必须至少覆盖五份 `max_requests`；有可信单价时，美元预算必须覆盖五份 `model.max_cost_usd`。任何一项不足都会在模型请求前退出，不再用先到先用的剩余份额运行后面的题。

每题通过独立进程执行 `ask`，合作式时间为 `limits.run_seconds`，另有 5 秒进程启动和收尾余量。父进程的硬 watchdog 最迟在该时间包络后终止未退出的子进程；批次需再预留 5 秒协调余量。时间上限针对查询进程阶段，读入和校验快照及写报告另需本地文件处理时间。若异常开销已挤占后续完整包络，对应题明确记为未运行，而非缩小它的额度。没有外层重试。

例如将 `ask.model.toml` 的单题配置设为 `max_requests=8`、`run_seconds=60.0`，五题至少需要 40 次请求和 330 秒；这不是默认配置的时间预算：

```sh
uv run --locked python deploy/replay_query_snapshot.py \
  --config /private/signalnest-snapshot/ask.model.toml \
  --at '2026-10-15T12:00:00+08:00' --model \
  --max-total-requests 40 --run-seconds 330 \
  --output /private/signalnest-replays/model.json
```

`unpriced_proxy` 的美元费用保持未知，不接受 `--max-total-cost-usd`；请求、输出及时间上限仍有效。`token_rates` 必须显式提供足够的 `--max-total-cost-usd`，这是应用估算的总额度，不是供应商账单保证。异常终止的 worker 可能已提交请求，其用量标为未知，并继续保留整份请求／费用预留，不能当作零调用。普通模型失败沿用 `ask` 的检索降级；进程硬终止可能没有完整报告，只留下有限错误及未知使用量。

可以先单独检验一题，单题仍使用完整配置额度：

```sh
uv run --locked python deploy/replay_query_snapshot.py \
  --config /private/signalnest-snapshot/ask.model.toml \
  --at '2026-10-15T12:00:00+08:00' --case cs-ta --model \
  --max-total-requests 8 --run-seconds 70 \
  --output /private/signalnest-replays/cs-ta.json
```

上面的单题 70 秒同样以 `run_seconds=60.0` 为前提；默认 90 秒配置需要至少 100 秒。`ask.proxy.example.toml` 的 60 秒/4 请求配置，五题请求预留为 20、时间预留仍为 330 秒。不得只缩小外层预算而保留更大的单题配置。

其他 ID 为 `research`、`applicant`、`ambiguous-deadline`、`no-match`；可以重复 `--case` 选取不同题，不能重复同一 ID。问题是工程情境，不是人工 gold；任一快照可能没有某类真实通知，空结果不能被自动判定为正确。

## 结果核对

每题保存显式时钟、实际 `QueryReport`、工具轨迹、来源状态、候选、引用、有限错误及 `human_review.status=pending`。汇总分别列出已完成报告、形成回答、降级、已观察请求和未知请求 worker；退出 0 只表示程序流程完成，不能说明答案质量通过。退出 1 表示至少一题降级或未完成，退出 2 表示配置、预算、文件或输入一致性错误。

真人需要核对相关通知是否漏检、申请主体是否正确、条件和截止引句是否回答问题，以及未知和积压限制是否明确。本入口不为真实知识库自动填标签，也不把旧九篇 fixture 的人工评价迁移到新快照。现有固定语料回放与独立人工评价入口继续保留，见[查询评估说明](query-evaluation.md)。
