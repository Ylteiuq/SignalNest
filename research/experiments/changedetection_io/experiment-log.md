# changedetection.io 小型实验记录

日期：2026-09-29（Asia/Shanghai）  
目标版本：`v0.60.7` / `593e9cc48c2b475dacbc3dbdebe2f5136bef8efa`。

## 结果

本次没有成功运行上游代码，也没有形成可报告的运行实验结果。故障窗口与调用顺序结论均标为源码推断；没有把它们写成实验验证。

## 环境与尝试

- 工作区：SignalNest 仓库；不修改其 `src/`、`tests/`、依赖清单或 Parser 的 fixture。
- 系统 Python：3.9.6；`requests` 不可导入。
- 尝试把固定 tag clone 到 `/private/tmp/changedetection-io-0.60.7`，GitHub 主机名解析失败：`Could not resolve host: github.com`。
- 当前环境网络受限；未安装完整上游运行依赖、浏览器或外部服务，也未访问用户账号、发送邮件或请求校园网站。

## 因此未验证的运行行为

- worker 运行中强制终止后，实际文件状态和重启调度结果。
- raw checksum 已更新、解析抛错后，同一正文下一轮是否如静态调用顺序所示被 shortcut 并清除错误。
- watch 元数据落盘与 history snapshot/index 之间断电时，恢复后是否出现孤儿快照、遗漏版本或提醒。
- Requests adapter 在实际网络异常下的重试次数与等待时长；源码配置和 urllib3 最终运行语义未在此环境测量。

## 可复现实验方案（未运行）

在具备该 commit 的 checkout 和其锁定依赖的隔离临时环境中，以临时 `datastore`、本地模拟 HTTP 服务和仅记录日志的通知 endpoint 运行：

1. 先让源返回正文 A，等基线、snapshot 与 history index 都写入。
2. 再返回正文 B，并在 processor 写完 `last-checksum.txt` 后、抽取成功前单独注入异常；保持上游源代码不改，可在实验副本用 monkeypatch 包装抽取函数。
3. 同正文 B 再跑一次，观察是否 raw-shortcut、`last_error` 是否清除、`previous_md5` 与 history 是否仍为 A。
4. 另在 worker 的 `update_watch()`、snapshot 写入、history append、notification enqueue 四个边界分别终止子进程；每次重启临时实例，比较 watch 元数据、文件、history 及通知日志。

以上步骤是后续复现建议，**不是本次实验结果**。要执行它需要先取得固定版本源码和兼容依赖；获取成功后应在隔离的临时数据目录注入故障，不修改 SignalNest 应用源码。
