# 新 N2：上线前检查与连续观察

这里的 N2 是 2026-10-08 新迭代的真实部署阶段，与此前邮件规划 N2 编号独立。新增本地检查和观察入口，沿用既有采集、邮件、锁、systemd 模板及备份校验，不增加后台常驻进程。

**当前目标机已确认是 Windows 11（NT 10.0.22621.0，PowerShell 5.1），只读核验未见已安装的 WSL 发行版。** 现有写入锁、原文发布和 systemd 部署目标要求 POSIX/Linux，不能将原生 Windows 当作已支持的部署环境；安装 Linux 环境或改用 Linux 主机需单独明确，不修改当前 Mac 或远端系统。真实个人画像、邮箱配置和收件记录尚待确认，也未开始一周运行观察。文档不保存私人主机地址或邮箱。

本地测试通过、`prepared=true` 或七行观察 JSON 都不能替代 N2 的实际验收。已有本科生院来源可以在受支持环境部署；EMS 当期助教来源仍未验收。

## 本地入口

```sh
# 只读，无网络、不初始化、不取写锁、不修改暂停或通知启用状态。
# profile.personal.toml 必须是自己核对的真实画像；不要直接确认示例。
observed_at="$(date -u +%s)"
signalnest rollout-check --config /etc/signalnest/signalnest.toml \
  --profile /etc/signalnest/profile.personal.toml --profile-confirmed \
  --release-root /opt/signalnest --at "$observed_at"

# 输出一行 JSON；追加由调用者完成，应用不创建或轮转观察日志。
signalnest observe --config /etc/signalnest/signalnest.toml \
  --profile /etc/signalnest/profile.personal.toml --profile-confirmed \
  --release-root /opt/signalnest --at "$observed_at" >> /var/log/signalnest-observation.jsonl
```

`--at` 是显式 UTC Unix 秒，观测年龄和等待时间都使用它。应使用真实观察时刻，不补造过去记录。观察日志的目录权限、备份和保留由部署者管理，不把包含个人实例状态的日志提交仓库。命令不读取 notice 正文、冻结邮件正文、收发地址或凭据文件；SMTP 仅检查配置的用户名/密码环境变量是否非空，既不输出值也不计算秘密摘要。当前交互 shell 中存在凭据不证明 systemd 已正确注入；必须另行在目标服务环境核验。

报告包含：

| 字段 | 含义及边界 |
| --- | --- |
| `prepared` | 本地数据库、raw 目录、画像声明、运行源码与发布证据等必要检查没有 blocked；不表示已上线。 |
| `checks.writing_platform_supported` | POSIX 写入平台为 pass，否则 blocked；仍允许输出只读观察，但不能报告 prepared 或 mail_locally_ready。 |
| `mail_locally_ready` | 本进程 SMTP 配置/凭据存在、邮件开启、通知已启用、画像政策一致且未暂停；不测试网络、认证或收件。 |
| `externally_verified` | 始终 false；实际部署、收件和一周验收须另记运行证据，不由该命令推断。 |
| `checks` | pass / attention / blocked；未启用邮件、缺 SMTP、暂停等在首次安全准备期属于 attention。 |
| `profile.operator_declared_personal` | 仅保存操作者确认声明；不判断示例是否适合本人，不证明资格。 |
| `collection` | 复用 `status`，保留列表尝试/响应/登记、完整扫描时间、运行结果、首次积压、成功复查、冷却与有限诊断。 |
| `mail` | 复用 `mail-status` 的六类投递计数、到期数、未知尝试、未分配意图、计划阻断和暂停；不会恢复遗留 sending。 |
| `release` | 当前源码允许集合的逐文件摘要、内容指纹、Git HEAD/该集合 dirty、uv.lock、Python/SQLite/平台、当前 migration head。 |

`rollout-check` 可以在缺库或未启用通知时输出清晰的准备不足结果，不偷偷建库或启用。库版本错误同样只读失败。报告的采集和邮件部分来自**不同只读快照**，合法写入可能在两次读取之间提交；它是诊断观察，不是全实例跨表原子备份。

`rollout-check` 在 `prepared=false` 时退出 1，配置/命令参数错误退出 2；`observe` 成功产生观察 JSON 即退出 0，即使里面有 attention/blocked，便于持续记录异常事实。非法观察时间或不能可靠产生报告的错误返回有限分类与非零退出；不要只按 observe 的退出码判断实例健康。

库入口为 `build_release_manifest(repo_root)`、`inspect_rollout(settings, *, at, release_root, profile_path=None, profile_confirmed=False, environment=None)` 和同参数的 `observe_instance`。返回普通 JSON 数据；只有显式调用才读取本地证据，导入模块无 I/O。

## 固定可重现版本

发布指纹与源码快照使用 POSIX 文件保护。缺少 O_NOFOLLOW/O_NONBLOCK 的平台返回有限
`release_platform_unsupported`，准备报告会保留 blocked；不会改用不安全的符号链接读取方式。
平台边界测试在 macOS 中移除相关常量模拟，不是原生 Windows CLI 运行验收；
只读目标机系统查询也不证明项目已能在该系统运行。

先在将要部署的源码目录使用 `uv sync --locked`，完成全部测试和检查。发布指纹只读取 `src/signalnest/**/*.py`、migration 模板、`pyproject.toml`、`uv.lock`、打包所需 `README.md`、systemd `.service`/`.timer`/`.conf` 及部署校验/评估/发布脚本；拒绝符号链接、非普通文件和过大的文件。它不会遍历私人 TOML、SMTP 环境文件、研究原文或数据目录。个人配置和数据库须单独受限备份。

保存完整报告，并保留 `release.file_sha256` 列出的**确切文件**与依赖锁。存在未提交改动时，仅保存 Git HEAD 或执行 `git archive HEAD` 会漏掉实际运行代码；新增显式本地源码快照工具保存允许集合的实际 bytes，不提交或推送：

```sh
# --output 必须是源目录外、尚不存在的新 .tar.gz，父目录必须已存在。
# macOS 的 /tmp 是符号链接，使用规范路径 /private/tmp 或真实备份目录。
python deploy/release_snapshot.py --release-root /opt/signalnest \
  --output /var/backups/signalnest/source-20261009.tar.gz
```

同时保存 `.tar.gz.manifest.json`；它记录整个归档 SHA、字节数、HEAD/dirty、Python/zlib 等运行环境与逐文件 SHA。包内 `signalnest/RELEASE.json` 只保存确定的内容指纹，tar 文件名固定排序、mtime=0、uid/gid=0、mode=0644，gzip 的 mtime=0 且不含临时文件名。因此在相同 Python/zlib 生成环境下，同一组名称与 bytes 重建得到相同归档 bytes，观察时间、HEAD 或 dirty 变化不会改变内容归档。解包到新目录后逐文件核对包内清单，另以旁边 manifest 核对压缩包 SHA，再使用其 uv.lock 安装。该快照不是私人实例备份，不包含配置、raw、研究/测试 fixture 或用户秘密，不宣称包含全部测试环境或离线 wheel 缓存。

完整文件先写同目录临时文件、fsync，再以不覆盖的原子发布建立目标；拒绝现有归档、现有旁边 manifest、符号链接、源目录内输出、未知项目及处理期间变化的源文件。归档与旁边 manifest 是两个目录项，发布不具有跨文件原子性：进程突然中断可能留下临时文件或只有一件产物，此时不能称发布完成；两件齐全且摘要相符后才保存为发布证据。普通发布故障会尽力清理本次的部分产物，绝不覆盖既有证据。fsync 不等于已验证断电或异地耐久性。

源码快照不代替提交、安装或依赖一致性检查，不会自动创建 Git tag。`runtime_code_matches_release` 比较实际运行包与所给源码目录的 Python 文件摘要，避免检查另一个 checkout 却运行旧安装；安装后再次检查。取证期间停止源码编辑，冻结后不在原目录临时改代码。

## 部署和小额度验收顺序

1. **补齐实际输入。** 确认 Linux/systemd 252+ 主机、架构、Python 3.12.x、磁盘/权限、真实 Profile、SMTP 服务/TLS、发件和收件地址。服务账号必须能够读项目/配置、写唯一实例目录。部署模板固定路径、解释器实际位置、时区和预算逐项核对，详见 [运维说明](operations.md)。不得把 macOS 的静态模板检查记成 Linux 启动验证。
2. **先关闭邮件。** TOML 保持 `[mail_runtime].enabled=false`，不注入真实 SMTP 凭据。按 README 显式初始化或升级，执行 `status` 和只读准备检查。配置与 schema 一致后执行真实有界 full；只有实际输出 `coverage=complete`、首页复核通过且持久成功时间推进，才记录完成。冷却或受限扫描不能冒充完整扫描。
3. **显式通知基线。** 核对真实画像和首启近期候选行为，再调用既有 `notifications-activate`；升级旧实例用显式政策预览/更新入口。不要伪造 complete 状态来绕过前置条件，不因新版本自动补发历史邮件。启用后先 `mail-pause`。
4. **预览再少量投递。** 保持暂停，用小额度 `mail-plan`，逐封核对 `mail-preview`、成员、理由、地址和截止证据。在目标机以既有安全方式注入凭据；先核对暂停状态和准备报告，再显式 `mail-resume` 与一次小额度发送。分别核验即时邮件和 Digest 的 Message-ID、本地 accepted 与实际收件箱；accepted 只证明服务器接受，不等于收件。没有自然候选时等待或明确记录工程测试邮件，不能把历史样本伪装成新机会。
5. **启用定时与隔离演练。** 目标机执行 systemd verify/calendar、受控服务启动与超时，核对独立邮件 timer、锁冲突、普通采集失败后邮件仍推进、暂停及凭据注入。按 [含邮件状态备份](mail-backup.md) 用 SQLite backup API 和 raw 一致快照；恢复副本独立路径、关闭 timer、不注入 SMTP，先暂停再验证 sending→uncertain。原实例和副本绝不同时工作。
6. **开始真实连续观察。** 定时器启用并核对一次实际运行后，记录开始时间，连续至少七天每天保存观察 JSON、必要 journal 和实际收件核对。出现中断或人工修复如实注明，不从两个时间戳之差推断持续在线。

目标机命令、暂停/恢复、服务超时以及凭据文件权限继续遵循 [后台邮件](background-mail.md)、[发送恢复](mail-sending.md) 和 [运维说明](operations.md)，不在这里建立第二套发送策略。

## 每日记录与判断

建议每天上海时间固定时点保存一次观察，并在异常后补一条，记录关联发布内容指纹和人工说明：

| 观察项 | 每天核对的证据 |
| --- | --- |
| 抓取覆盖 | 最近完整扫描时间/年龄、最近运行独立结果与覆盖、列表登记时间；26 小时仍无 complete 时调查预算、漂移、站点或冷却。 |
| 处理积压 | 首次未成功 total/due/deferred/最老等待、成功复查 due/最老逾期、有限错误；对比趋势，不能把失败退避隐藏为零待办。 |
| 邮件计划与投递 | unplanned immediate、unallocated digest、计划阻断、pending/retry/uncertain/sending/accepted/blocked、暂停原因和实际收件。 |
| 延迟 | `oldest_nonaccepted_wait_seconds` 从冻结时刻起算，包含 blocked/sending；`max_frozen_to_local_acceptance_seconds` 为历史已接受邮件的最大冻结→本地接受间隔。两者均不是事件→计划或收件箱到达延迟。 |
| 失败恢复 | 冷却 not-before、未知尝试、遗留运行、服务超时/重启后的继续进度；实际修复动作、恢复时间及是否可能重复投递。 |

Digest 尚未到计划时间时，未分配不一定是积压故障；发送暂停时冻结邮件等待属于预期事实。历史最大接受延迟不会随新一轮变小，不把它误当最近一天的分位数。有限最近错误样本最多复用 `mail-status` 的 100 封诊断，报告显式标记是否截断。观察记录不是冻结 MIME/原文备份，也不证明邮件 exactly-once。

一周报告应逐日列出真实记录路径、完整扫描成功、积压变化、邮件冻结/接受/实际收件时间、失败与恢复；明确所有人工干预、停机和未观察区间。只有这些目标机与邮箱证据完整后才能称 N2 首次验收通过，随后继续积累更长运行证据。

## 本轮离线验证范围

新增工具测试使用临时 SQLite、现有真实归档/通知/冻结/投递服务，验证未初始化无副作用、画像声明、允许集合的源码指纹、凭据存在性不泄露值、冷却/积压、pending/accepted/sending/uncertain/暂停、冻结等待与本地接受延迟，以及持写锁时仍可只读观察。遗留 sending 保持原状；数据库读取失败明确 blocked。未进行目标 Linux 安装、真实 SMTP、真实校园扫描、断电、异地恢复或一周观察。
