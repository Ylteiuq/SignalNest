# 通知规则小型研究实验

全部文件在research；不是SignalNest生产N0，无新增依赖。原实验环境CPython3.12.14、项目已有.venv；原文8页（复用2、新采6），11个历史时钟/虚构Profile案例。2026-10-08用户确认10个非空Action标签，见[确认记录](human-review-20261008.json)；S10仅保留原Parser失败记录。没有重跑实验或新增准确率结论。

从仓库根目录离线重放：

```sh
.venv/bin/python research/experiments/notifications/evaluate_probe.py
```

读取cases.json/rules.json、校验fixture SHA-256、调用现有纯Parser，写results.json；不联网、发邮件或写应用数据库。读取已有cases，禁止为了通过探针修改原始页面。上述是原运行方式；当前代码可能已改变，未来重跑先另存旧results，不能覆盖本次确认所引用的历史证据。脚本的human_confirmed_labels=0/accuracy=null是原研究口径，不能据此否认新确认记录或直接作为生产gold评估器。

`build_cases.py`是首次构造工具；已存在人工标注时拒绝覆盖，不要重跑。cases.json中10个human_expected_action已按用户确认填写，annotation_status=human_confirmed，label_revision绑定确认记录；不将needs_review或资格事实一起升级为人工gold。`results-baseline.json`保留v1的正文辅修裸词误匹配；`results-v2.json`保留v2遗漏全日制/在校条件的结果；当前rules.json是v3，旧结果的pending_human保留原样。

`collect_samples.py`包含有限公开URL白名单、单次GET、5秒间隔、1MiB正文上限、TLS开启/trust_env=False、不跳转/重试/取媒体。**默认离线实验不运行它**；本次6次真实GET已完成，fixture/metadata足够。必要时显式`--ids 127511`可只重取一个列明身份，保存新时间文件。socket timeout不是整个请求墙钟上限，不能把研究捕获脚本当生产Fetcher。

验证范围：规则子集、真实Parser接受/拒绝、历史时钟条件、Action与核对标记、路线纯函数。未验证生产事务/队列/SMTP/socket超时/断电持久性；重评操作恢复由N1/N4验收，不在本探针中假装验证。

详细规则、实际结果、真实样本缺口、拟议人工标签：[评估报告](../../notification-rule-evaluation.md)。

当前并行N0的窄范围实际行为另行记录，使用显式符合其schema的虚构Profile，**不是S01–S11人工gold**：

```sh
.venv/bin/python research/experiments/notifications/audit_n0_snapshot.py
```

写`n0-snapshot-result.json`，含7例完整输入/Decision、HEAD和所读源码SHA。只有hash匹配时才能复现该快照；未提交代码由其他Agent负责，本研究不修改。`n0-snapshot-before-update.json`保留一次并行修改前的结果，7例Action/route相同；未来重跑先另存本次文件。此脚本不联网/写应用数据库/SMTP；源码在导入/运行期间变动会拒绝生成结果。现有应用测试未在此运行。
