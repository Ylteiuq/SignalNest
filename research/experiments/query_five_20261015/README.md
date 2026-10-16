# 五题证据整理实验

2026-10-15。复用既有真实模型回放，不再调用模型、校园网站或生产实例。

## 实际执行

```sh
.venv/bin/python research/experiments/query_five_20261015/prepare_review.py \
  --source /path/to/private-replay \
  --output /path/to/new-private-review-directory \
  --manifest /path/to/private-review-manifest.json
```

退出0；新私有目录0700、文件0600，数据库副本0400。输出拒绝覆盖；未来重新整理需新目录和新manifest，不删除本次证据。

脚本只用Python标准库，sqlite3以mode=ro&immutable=1打开明确提供的静态查询副本，且先校验DB SHA与非空WAL。没有读取credential、SMTP、邮件表或个人Profile字段；画像仅按原字节和摘要复制进私有包。仓库只保留本说明、整理脚本和脱敏失败候选；manifest、完整性记录、完整回答、通知正文和画像副本留在本机受限目录，不提交。

## 通过的材料检查

- 原report与10个绑定输入均按SHA匹配、复制前后不变；模型设置、时钟、预算和5个实际结果原样保留。
- 导出645身份、644份成功正文，25份重点原HTML与原响应SHA匹配；包括窗口内全部11份成功正文、7份标题明确的CS招募及反例。
- 实际答案的instance/document/version、内容摘要、Parser与冻结数据库匹配，全部引用存在于对应原文连续文本。
- 人工模板5题pending，相关集/答案判断均null。**引句连续不证明语义正确。**

## 环境与限制

实际Python/SQLite/平台信息保留在本机manifest；没有安装依赖、导入应用、运行其测试或增加网络请求。模型原回放的16请求是旧运行事实，本准备实验新增0。

原运行agent.py/tools.py与整理时源码不同；仅保存旧记录的源码SHA，没有声称恢复旧可执行源码或终端逐字文本。answers.md是原JSON字段视图，不是用当前renderer生成的旧答案。原数据库完整bytes可能含私有状态，因此所有副本和原文/回答均留仓库外。

人工先确认相关集再看回答，范围最多先限25篇；需全库结论时另核对完整成功语料。脚本不计算或生成真人质量成绩；已知材料来自开发/调试，不是未见留出集。

[研究报告与核对入口](../../query-five-review-20261015.md)
