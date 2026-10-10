# CS 自动采集与 WSL 实机验收

完成本轮四项中的第 3 项：计算机学院本科教学通知的显式来源绑定、离线全链路回归、关闭邮件的小额度实采和专用定时服务。生产通知启用、CS 真实完整扫描及至少七天观察仍是下一交付，不将本节点称为整个上线验收完成。

## 冻结版本与环境

当前安装的代码发布为 **`9366fcb66ddae188d63291d460dfd8a2c6931b1c`**，冻结时允许集合及工作区均干净。发布包含 63 份允许文件，不含研究、测试、原文、个人配置或凭据。

| 发布证据 | 值 |
| --- | --- |
| 源码归档 SHA-256 | `dd31c0cf3308ae8120c65483683d5ce92f274275a65288b5d3cbe1341941fd00` |
| 内容指纹 | `4c4b4a32ed854c8488211b1b5026eda306c651abe243484992842d0cbc5e607b` |
| uv.lock SHA-256 | `f207f07d2019db12987c7d1d9c486b0ce5e148f2bcd801fcf257496050215c55` |
| UC / CS Parser | `whu-student-notices-v4` / `cs-undergrad-notices-v2` |
| 事实、规则、决策 / routing | v8 / v1 |
| 数据库 head | `0008_list_references`，本次没有 schema 或迁移变化 |

目标仍为用户指定的 Windows 11 / Ubuntu 24.04 / WSL2 x86_64，systemd 255、CPython 3.12.3、SQLite 3.45.1、Linux ext4。共享安装 `/opt/signalnest`；两个实例使用服务账号 signalnest。复用已校验的 uv 0.12.20，冻结锁的 **离线**依赖同步退出 0，没有下载依赖；运行账号读到的全部允许源码与发布清单相同。

本文件在实机验收后提交，不在运行包允许集合中；文档提交不改变上述已安装代码版本或冒充新的采集实验。当前仓库 fixture 为已整理的选样/脱敏副本，其摘要不是本次 HTTP 原文摘要。真实获取 bytes 留在目标受限 raw 归档；本次适配没有修改 research 文件或重新下载 fixture。

## 来源和实例边界

新增固定来源绑定贯穿配置、Fetcher 初始请求/每跳目标校验、列表/详情 Parser、缓存重处理、run 的解析版本及 live 比较旧正文。没有按主机猜 Parser、解析失败换 Parser 或重写 JSP 查询参数的分支。

| 实例 | 配置 | 数据库 / 数据目录 | Parser / source.id |
| --- | --- | --- | --- |
| UC | `/etc/signalnest/signalnest.toml` | `/var/lib/signalnest` | WHU v4 / whu-undergrad-student |
| CS | `/etc/signalnest/cs.toml` | `/var/lib/signalnest-cs` | CS v2 / whu-cs-undergrad-teaching |

CS 仅允许 HTTPS/443 的 cs.whu.edu.cn、本科教学栏目 1074、明确首页/静态子页和支持的 info/content.jsp 详情；重定向仍要求同一文章身份。缓存键仍为 source + 实际 URI + RequestProfile，查询顺序不因身份统一被改写。当前 profile 的 SHA 为 `38f96818d922d55f90505f621e23d0a1bd05e8265e5e8edffb21c57652afc7cd`，User-Agent 为 SignalNest/0.1、Accept 为 text/html、Accept-Encoding 为 identity。

每库仍只有一个通知启用通道；错误来源在创建 run/联网前拒绝。CS 不复用 UC 的数据库、实例锁、冷却、政策或通知基线。两库均保持 mail_runtime.enabled=false、零启用通道/事件/outbox/冻结邮件/发送任务；本次没有 SMTP 请求。UC 既有不加载 SMTP 的 drop-in 保留，CS service 没有 EnvironmentFile。

## 备份和安装

升级共享程序前停止 UC/CS 的 timer 和写入服务，分别持已有服务账号拥有的数据库锁，使用 SQLite backup API，再复制 raw。升级前一致快照：

受限备份目录中的 UC / CS 升级前一致快照。

| 升级前实例 | 通知 | 版本 | 响应 | 原文文件 | 完整扫描证据链 |
| --- | ---: | ---: | ---: | ---: | ---: |
| UC | 587 | 137 | 245 | 163 | 1 |
| CS | 30 | 1 | 7 | 6 | 0 |

两套备份均通过 integrity、外键、成功指针、搜索结构、全部原文路径/SHA 及邮件状态检查，无孤立/临时文件。安装后各 **27 张旧表**按列、行数和包含 BLOB 的行摘要逐项一致；原配置摘要、数据库和锁的 uid/gid 保持。UC 完整成功时间仍为 `1791631482`（2026-10-10 19:24:42 +08:00），不是被 CS 受限采集推进的新时间。

旧程序保留在受限的前一版本目录。源码包成员逐文件校验再安装，离线同步及运行源码指纹检查通过；不对已初始化的 CS 库再次创建实例。config-check、实际 systemd 模板 verify 和日历解析均退出 0。UC 普通 timer 已恢复，full/mail timer 仍 disabled/inactive。

## 实采暴露的正文包装和归档恢复

使用 CS v1 的两轮有限试采分别取得 15、30 个身份。第一轮详情 1 成功/1 失败，第二轮 0 成功/2 失败；普通失败仍保存原文、有限错误和 due，没有把失败报告为成功，后续条目继续处理。

三篇失败原文有支持的文章身份、唯一标题/日期/正文结构，但正文包装为 `div#vsb_content_501`；v1 仅支持 `#vsb_content`，因此返回 parse_missing_structure。直接检查已归档证据后，v2 仅增加这个已观察包装，仍要求唯一直接 `.v_news_content`，不接受任意后缀或猜正文。身份、分页和规范化规则未改，UC Parser 未改。

| 原响应 ID | 文章身份 | 原 fetched_at | 原始 bytes SHA-256 |
| --- | --- | ---: | --- |
| 2 | 1074:66441 | 1791634628 | `e3d3423004841c411ca9765fbcd418098b6614880832af3878f1f3bf9108fe55` |
| 6 | 1074:65481 | 1791634812 | `8130f65dc2a88139ad97a6918d3cd72185d6c8141d19f82d8d58ec5d1d553433` |
| 7 | 1074:65321 | 1791634816 | `01eb9313756bd5ebd19f8dc4a0b24f8b77dafdc2e8fd5e03824f572a29bafb61` |

20:51 +08:00，用服务账号对响应 2/3/6/7 执行实际 `reparse --config /etc/signalnest/cs.toml`，三条失败全部恢复；另再次重解析响应 2 验证幂等。通知仍 30、响应仍 7、文件仍 6，所有响应的不可变获取字段和全部文件 SHA 保持。版本由 1 增至 5，旧 v1 行未改；已成功的 1074:65851 在 v2 下内容摘要与 v1 相同，仍保留新版解析产物。重复解析复用已有 v2 版本，没有增加邮件任务或 live 更新。

维护重解析只更新解析产物/成功事实，不假造 HTTP 获取或完整扫描。未来自动复查仍使用 latest 对应原文，不能把维护入口当成自动回退旧正文。

## 真实服务运行与定时入口

2026-10-10 **20:51:53–20:52:08 +08:00**，手动启动实际 `signalnest-cs@regular.service`，调用配置中的 scheduled-run：

| 运行事实 | 实际值 |
| --- | --- |
| run_id | `f9cb1a3f92234bb882a8e71df4811c13` |
| CLI / systemd / result | 0 / success，ExecMainStatus=0 / succeeded |
| coverage / 原因 | limited / page_limit |
| 页与条目 | 2 页、30 个本站条目、0 新身份、0 引用 |
| 详情 | 2 尝试、2 成功、0 失败 |
| 物理请求 | 4，预算为最多 12；请求间隔为 5 秒 |
| 时间、正文上限 | 配置 180 秒、资源 45 秒、2 MiB；服务硬上限 8 分钟 |
| 剩余首次处理 / due | 24 / 24 |
| 首页复核 / 完整成功时间 | false / NULL，bootstrap 完成时间也为 NULL |

首页和第 2 页返回 304：新响应 8/9 无正文，分别绑定响应 1/5 的完整 200；对应原 fetched_at 保持。随后真实获取并处理 1074:65041、1074:64481 两篇详情。三轮 CS 实采合计 11 次请求；Wrapper 诊断和维护重解析没有额外下载。

最终 CS：**30 个身份、6 个成功正文、24 个待处理、7 个版本（含保留的 v1）、11 个响应、8 个原文文件**，零失败通知、零孤立/临时文件、零通知投递工作。这是两页受限覆盖，不是全栏目扫描；没有根据所有条目已知或两页 304 宣称 complete。

专用 `signalnest-cs-regular.timer` 于 20:55 +08:00 启用并处于 active/waiting，Asia/Shanghai 每小时 :15，下一次为 21:15。普通额度为两页、两篇详情、12 次请求；不启用 CS full/mail timer。UC timer active/waiting，仍按原半小时间隔运行。这里验证了手动启动真实定时服务和 timer 排期，尚未观察下一次自然触发，不声称已完成连续运行。

助教招聘正文 1074:64481 通过实际抓取、归档、Parser 和业务入库。在经本地核验的工程画像预览中为相关 opportunity / DIGEST / needs_review；资格和时间均 unknown，保留报名渠道、年份冲突、截止/开起时间、成绩及教师同意等待核对项。它证明核心场景链路可用，不能证明用户已符合或该招聘仍可报名；没有以此启用或发送邮件。

## 升级后备份、恢复与诊断

受限证据目录为上述升级备份的 `new-release/`，其中 `cs-validated-backup/` 再次使用同一锁和 SQLite backup API 保存当前库及 8 份 raw；verify_backup 的完整结果与实例相同。复制为 `cs-isolated-restore/` 后复核相同计数、原文摘要及业务引用；新配置没有 SMTP，mail_runtime.enabled=false，没有 timer 或联网动作。配置检查和只读 status 均退出 0。恢复副本没有启用通道，因此也没有未暂停的投递工作。

生产 CS 的服务账号 status、notifications-status 退出 0；清洁现场记录存入 `/var/lib/signalnest-cs/acceptance/cs-collection-20261010.json`。受限目录中的备份、原始响应和日志没有复制到仓库。

两次辅助检查曾因错误表名/版本常量名停止，未改业务数据；修正后各 27 张表比对通过。恢复副本初始 TOML 缺 HTTP 节，被 config-check/status 拒绝，补齐非秘密必需字段后通过。所有检查失败均阻止后续启用，没有吞掉错误假报验收成功。

20:57 +08:00 通过纯 Windows 查询核对既有保活任务：State=Running，LastTaskResult=267009（任务正在运行）；`wsl --list --running` 返回 Ubuntu-24.04，没有为这项检查启动 Linux 来伪造存活。没有更改全局电源、WSL 配置或任务定义。本次安装/恢复验证不是断电、宿主重启、异地恢复或七天存活实验。

## 离线验证及下一节点

冻结前在 macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1 上运行当前全套 **2288 项 pytest 通过**（120.01 秒）、Ruff/格式检查通过（src/tests/deploy，共 133 个 Python 文件）；实机验收后仅修改文档，再执行该范围的两项检查及 git diff --check 均通过。另行扫描 `ruff check .` 返回原有研究实验脚本的 85 项 lint 问题，按协作边界保留 research，未自动修复；不将项目范围通过说成全仓库无问题。

模拟 HTTP 连接真实 RawStore、缓存、Parser、SQLite、事务及通知模块：200/304、错误原文重处理、损坏/缺失回退、新解析版本、有限身份/跳转、独立冷却、数据库提交回滚、整页外链登记、重复事件和 CLI 均有回归。另有 8 项有限包装测试，现有正文规范化摘要保持。

六组共 31 个规则情境重新重放；政策仍 v8，case manifest、标签和统计保持，CS 解析版本及源代码证据更新。标签为工程预期，不是已确认个人标签或人工 gold。完整模拟链的第 3 页明确为工程构造；真实 fixture 只有第 1/2/4 页，不能把这项测试冒充本次真实 CS 完整扫描。

下一交付先取得 CS 真实 complete 和独立首页复核，再核对画像、通知启用边界及首启候选。候选预览通过后才小额度开启生产邮件，核对实际收件并开始至少七天覆盖、积压、投递和恢复观察。当前到第 3 项结束，邮件保持关闭。
