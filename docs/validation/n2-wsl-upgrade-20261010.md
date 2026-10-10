# WSL 升级与真实完整扫描验收

完成本轮四项中的第 2 项：冻结发布、备份原实例、显式迁移 0008、重处理失败列表，并取得真实完整覆盖。CS 自动采集、生产通知启用和七天观察仍属后续交付；不把本节点称为整个 N2 验收完成。

## 发布与实际环境

采集时安装的应用提交为 `ddbd9359ddb3d2fea4131a1aa8aeafea60722cf4`，冻结时工作区干净。源码包包括 60 份允许文件，不含个人配置、凭据、研究、测试或业务原文。

- 源码归档 SHA-256：`3723b6cf097cbe47ce32805666f7bf4b8fcefda889f8685ea6937d73133508b5`。
- 内容指纹：`f51fed3e07c9987ffbf18d2db34f726f48bb1d4187e70ad652efc104646d6223`。
- uv.lock SHA-256：`f207f07d2019db12987c7d1d9c486b0ce5e148f2bcd801fcf257496050215c55`，与旧实例相同。

Windows 11 / Ubuntu 24.04 / WSL2 x86_64，systemd 为 PID 1，实际解释器为 `/usr/bin/python3.12`；Python 3.12.3、SQLite 3.45.1、Linux ext4 已重新核对。复用已校验的 uv 0.12.20，按冻结锁执行 `uv sync --frozen --no-dev`。离线同步因缺少构建缓存失败，随后允许联网的锁定同步退出 0；不把安装称为离线安装。运行账号读到的 Python 源文件与冻结允许集合逐项相同。

程序路径仍为 `/opt/signalnest`，配置 `/etc/signalnest/signalnest.toml`，实例 `/var/lib/signalnest`；保留原配置、HTTP profile 和 5 秒物理请求间隔。未改源码、依赖锁、Parser 规则、数据库 schema 或历史迁移；本轮仓库修改为验收文档。

当前事实/规则/决策代码为 v8、routing 为 v1，UC Parser 为 `whu-student-notices-v4`。代码安装不激活通知、不更新已有政策、不重写旧决定或冻结邮件。实例尚无启用通道；用户授权的画像仍是未确认的假设画像。

验收后的 README 修正版本及运行状态，另冻结文档发布快照；只有 README 属于该快照允许集合的内容差异，应用 Python、模板、迁移和 uv.lock 与上述实采版本相同。具体 Git/SHA 清单保存在目标受限发布记录中，按全部文件复核，不将文档更新伪报为又一次采集实验。

## 停写、备份与迁移

升级前停止并禁用 regular/full/mail 三个 timer，确认三个采集/邮件 service inactive。备份期间由管理员持**已有服务账号拥有的同一 writer_lock**，使用 SQLite backup API，再复制完整 raw 和受限配置；未删除或重建锁文件，也没有让 root 初始化业务库。

旧快照位于目标机 `/var/backups/signalnest/20261010T110937Z-pre0008`，先使用旧代码校验其 `0007_history_search`。旧代码目录保留在 `/opt/signalnest-releases/b078056-20261010T111726Z`；回退必须使用匹配代码和对应数据库/raw，不把旧代码直接指向已升级库。

| 校验时点 | 通知 | 版本 | 响应 | 原文文件 | 完整证据链 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 升级前一致快照（0007） | 125 | 121 | 183 | 130 | 不具备新证据列 |
| 迁移与重复初始化后（0008） | 125 | 121 | 183 | 130 | 0 |
| 重处理第六页后 | 149 | 121 | 183 | 130 | 0 |
| 真实 full 后 | 587 | 121 | 215 | 148 | 1 |

服务账号执行 `config-check`、第一次与重复 `storage-init` 均退出 0。迁移为 `0008_list_references`，新增引用表为空，旧运行的 coverage_evidence 仍为 NULL。26 张旧表按旧列投影，逐行比较数量和包含 BLOB 的内容摘要，全部一致；原数据库/锁的服务账号所有权、备份文件摘要保持。

`integrity_check`、`foreign_key_check`、成功版本指针、搜索结构、全部原文路径及 SHA、邮件状态专项检查通过；无孤立或临时原文。目标 systemd 模板实际 verify 退出 0。

## 原失败列表的恢复

19:19:30 +08:00，以服务账号执行：

```sh
/opt/signalnest/.venv/bin/signalnest reparse \
  --config /etc/signalnest/signalnest.toml --response-id 6
```

这是曾因 SIM 外链失败的 `/tzgg/xstz/19.htm` 完整 200 原文，获取时间仍为 `1791512635`（2026-10-09 10:23:55 +08:00），原文 SHA 仍为 `642e2025fd23613bb098bbad6ca05c788b332f9a27956c10773ca0eb2244abce`。重新解析得到当前页 6/24、24 条本站身份、1 条待适配引用及实际下一页 `/tzgg/xstz/18.htm`，全部整页登记，错误清除。

新增 24 个通知身份及 1 个引用，响应数量仍为 183；未联网、未伪造获取时间、未制造正文版本/通知事件/邮件。维护重解析仍不宣称完整扫描成功。未把没有绑定可用正文的旧 304 当作 HTML 重放。

## 真实完整覆盖

2026-10-10 **19:22:11–19:24:42 +08:00**，服务账号执行一次真实有界列表采集；不注入 SMTP 环境，不抓正文或外链。

```sh
/opt/signalnest/.venv/bin/signalnest crawl-once \
  --config /etc/signalnest/signalnest.toml --scan full \
  --max-pages 64 --max-details 0 --max-requests 96 \
  --run-seconds 600 --resource-seconds 60 --max-body-bytes 2097152
```

| 运行事实 | 实际值 |
| --- | --- |
| run_id | `63628d6f48764a02bfd0375702d1b60d` |
| result / coverage / 退出码 | succeeded / complete / 0 |
| 分页 | 1/24 连续至 24/24，沿实际 next；首页及 2–23 页各 25 行，末页 14 行 |
| 遍历行 / 本站行 / 引用行 | 589 / 587 / 2；不含首页复核 |
| full 本轮新增 | 438 个本站身份、1 个引用；另 24 个身份/1 个引用来自先前维护重解析 |
| 物理 HTTP 请求 | 32，包含条件验证和保守完整获取回退；5 秒间隔 |
| 首页复核 | true，另一次实际请求及完整 ListPage 比较成功 |
| 完整成功、bootstrap 完成时间 | 均为 `1791631482`（2026-10-10 19:24:42 +08:00） |
| 详情尝试 / 成功 / 失败 | 0 / 0 / 0，原有 121 个版本保持 |
| 剩余首次处理 / 所有 due | 466 / 467；其中 4 个既有首次失败，另 1 个成功正文到期复查 |
| 未适配引用 | 2，仍为 pending_adapter，不抓外部正文 |

真实末页当前为 14 行，早期 research fixture 为 13 行；原 fixture 保留不变，没有改规则或硬编码计数来适配本次实采。

验收不只读取 `home_rechecked=true`：重开数据库，读取 coverage_evidence 的 24 页及独立复核首页，对全部归档正文重新纯解析，核对每页分页、实际请求/最终 URI、next 链、有序 notice/reference 行和列表摘要，并校验响应绑定、整页成员入库和持久来源成功指针。25 份已登记页面证据均匹配，来源成功时间与该 run 的 coverage_at 一致。原 125 个身份、首次发现时间、成功指针及原 121 个版本逐字段保持。

完整 200 列表的处理失败已为 0。旧及本轮 `cache_repair_required` 等 304 传输证据继续保存；其无正文/完整回退事实不能等同于当前列表未登记。覆盖成功不等于所有详情已处理，也不是全站原子快照。

## 升级后的快照与隔离恢复

停写持同一锁，再建立 `/var/backups/signalnest/20261010T112802Z-post0008-full`；SQLite backup API、148 份 raw、匹配源码包/清单及私有配置一并保留。0008 专项校验取得 587 通知、121 版本、215 响应、2 引用、1 完整证据链，各邮件状态均为 0，无孤立/临时文件。

复制到独立 `/var/lib/signalnest-restored-0008-20261010T112802Z`，不复制配置或凭据、不安装 timer，不触发采集/发送。由服务账号实际执行只读 `verify_backup` 退出 0；数据库文件与全部 raw 逐字节保持，返回计数与新快照相同。升级前唯一快照没有迁移或覆盖。

这验证本机实际 SQLite snapshot、文件复制、0008 引用/覆盖链恢复及服务账号读权限，不等于正式回滚、恶意篡改认证、真实断电、异地副本或邮件未知接受窗口演练；本次生产库没有邮件，既往含邮件恢复证据仍见[旧实机记录](n2-wsl-20261009.md)。

## 运行边界与现场问题

验收期间 `[mail_runtime].enabled=false`，生产启用通道/事件/决策/outbox/冻结邮件/发送尝试均为 0；采集 service 的既有 `acceptance-no-smtp.conf` 保留，EnvironmentFiles 为空。full/mail timer 保持 disabled/inactive，恢复原有每半小时 regular timer；下一次触发为当日 20:00 +08:00，按原 2 页/10 详情预算补正文积压。不为该节点启用生产提醒。

首次备份辅助脚本以服务账号访问管理员 0700 备份父目录时失败，未建立数据库快照；保留失败目录，改由管理员持现有锁备份，业务迁移仍由服务账号执行。大包经 Windows/WSL 标准输入传输曾阻塞且未开始安装；只终止那一个传输进程，改为文件传输后再次验证同一归档 SHA，没有终止业务进程。

另实际发现 Windows 专用保活任务 Ready、退出结果 `3221225786`，WSL 在命令退出后已停止；没有推断具体历史终止原因。逐字核对其公开动作只执行指定发行版/服务账号的 `sleep infinity`，保留 Interactive/Limited、PT0S、IgnoreNew，重新启动已有任务，没有更改全局电源或任务定义。随后观察 task=Running、Ubuntu=running；这只证明当前会话恢复，不证明下次登录、宿主重启或七天存活。

只读 `status`、`notifications-status`、`observe` 和再次重开库的 status 均退出 0，持久完整时间保持；prepared/mail_locally_ready/externally_verified 仍为 false，符合没有确认个人画像、未启用生产通知和未注入 SMTP 的实际状态。现场记录位于实例 `acceptance/upgrade0008-20261010.json` 与受限备份的 `new-release/`，不把私有主机、邮箱或授权码写入仓库。

## 验证范围与下一交付

冻结前同一应用代码已通过全部 **2242 项离线测试**（116.20 秒）、Ruff 与格式检查 129 文件；本节点在目标机执行实际安装、迁移、重解析、校园 HTTP、归档、SQLite、持久覆盖、备份和恢复检查，没有把 MockTransport 或探针称为真实网络。没有新功能代码变化，因此未把上一节点测试伪报为本轮另一次完整 pytest。

下一项为 CS 自动采集：明确 UC/CS 的来源配置、目标限制、Parser/缓存/运行和通知启用边界，用模拟 HTTP 连接真实归档/入库，再做关闭邮件的小额度实采。其后才核对画像和真实候选、小额度启用生产邮件并开始至少七天观察；本次到第 2 项结束。
