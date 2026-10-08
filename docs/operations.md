# 定时运行、诊断与恢复

提供处理政策、只读诊断及 Linux/systemd 定时模板；每次执行有界串行采集或邮件计划/发送，不在 Python 内启动常驻调度器。邮件默认关闭，需显式启用 `[mail_runtime]`、通知政策及 SMTP。默认测试离线；本次未在 Linux 安装或启动服务，未进行新的校园或真实 SMTP 请求。

## 一个部署目标：Linux + systemd 252 及以上

模板位于 [`deploy/systemd/`](../deploy/systemd/)，只针对系统级 systemd、POSIX 本地文件系统及单个数据库实例。macOS、Windows、容器内缺少 journald namespace 的环境不套用此模板。Python 仍为 3.12.x；安装依赖见 README。

模板采用这些可见固定路径；部署前一起调整，不在服务命令中使用 shell、环境变量拼接或动态寻找可执行文件：

| 项目 | 模板路径或值 |
| --- | --- |
| 只读项目及虚拟环境 | `/opt/signalnest`、`/opt/signalnest/.venv/bin/signalnest` |
| 配置 | `/etc/signalnest/signalnest.toml` |
| 运行用户与组 | `signalnest` |
| 唯一可写实例目录 | `/var/lib/signalnest` |
| 专用日志 namespace | `signalnest` |

配置中的存储部分必须使用同一实例：

```toml
[storage]
data_dir = "/var/lib/signalnest"
database = "/var/lib/signalnest/signalnest.sqlite3"
```

其余配置从 `config.example.toml` 保留并校验，包括 `[runtime]` 的处理政策和有限预算。邮件启用、计划/发送限额与凭据文件说明见 [后台邮件](background-mail.md)；不要把密码写入 TOML 或 unit。HTTP `request_interval_seconds` 建议起步 5 秒，它控制物理请求间隔，不控制定时周期。固定 User-Agent/profile，保持 TLS 验证。服务的 `ProtectHome=yes` 要求虚拟环境的真实 Python 和依赖不位于 `/home`、`/root` 或 `/run/user`；部署前检查 `readlink -f /opt/signalnest/.venv/bin/python`，用可访问的 3.12 解释器建立虚拟环境。服务账号必须可读项目/配置，可写实例目录；配置和备份权限限制在必要账号。

### 初始化、政策升级与首次启动

以下是供部署者逐步执行的命令，本次没有执行它们。先建立账号、安装目录及依赖，再以服务账号校验配置并显式初始化：

```sh
sudo -u signalnest /opt/signalnest/.venv/bin/signalnest config-check --config /etc/signalnest/signalnest.toml
sudo -u signalnest /opt/signalnest/.venv/bin/signalnest storage-init --config /etc/signalnest/signalnest.toml
sudo -u signalnest /opt/signalnest/.venv/bin/signalnest status --config /etc/signalnest/signalnest.toml
```

已有成功数据首次采用新的复查政策时，先备份，再显式执行一次：

```sh
sudo -u signalnest /opt/signalnest/.venv/bin/signalnest apply-recheck-policy --config /etc/signalnest/signalnest.toml
```

这会根据当前成功版本的站点日期与原成功处理时间保守重算成功通知的 due；已有 due 与新 due 取较早者，不能把逾期任务推后。保留失败退避与原获取/处理证据。该命令不获取网页，不把失败记录改为成功；`storage-init` 也不替代政策迁移。

**首次安装或停机至少 24 小时恢复时**，停止定时器，手工执行一次有界 full，再恢复普通轮询：

```sh
sudo systemctl stop signalnest-regular.timer signalnest-full.timer signalnest-mail.timer
sudo -u signalnest /opt/signalnest/.venv/bin/signalnest scheduled-run --config /etc/signalnest/signalnest.toml --mode full
```

查看命令摘要中的 `coverage` 与退出码。full 受冷却或预算限制仍可能失败，不能据命令名称声称完成；不要立即循环重跑。确认问题后可以人工另行安排 full，普通轮询可继续补详情。此节点不从旧 run 的 `coverage` 猜测扫描模式，不添加 full 重试事实，不在每次 regular 前强制 full，避免失败回扫长期占住详情预算。

### 模板触发与预算

| 实例 | Asia/Shanghai 触发 | 起步配置中的有限预算 |
| --- | --- | --- |
| `signalnest@regular.service` | 每小时整点及半点 | limited，2 页、20 详情、64 请求、600 秒 |
| `signalnest@full.service` | 每日 01:15 | full，64 页、0 详情、96 请求、600 秒 |
| `signalnest-mail.service` | 启动后 2 分钟；每小时 02、07…57 分钟 | 每轮计划最多 5 封，发送最多 5 封/300 秒 |

`scheduled-run --mode regular/full` 从配置读取预算，不靠模板另写一份数字。full 把预算用于发现与覆盖验证，详情由 regular 分批补全；两者共用相同写入锁、缓存、冷却及 due。启用后台邮件后，采集普通失败仍接着计划/发送；数据库、归档或状态登记等系统错误终止本轮。独立邮件 timer 可推进已有冻结积压，单项渲染阻断不阻止其他冻结邮件；暂停则禁止 SMTP。两个入口均持相同实例锁。详情分组容量、复查年龄档与失败延期见 README；分配槽不保证预算耗尽或冷却时仍完成相应尝试。

regular 的 `Persistent=false` 不在重新启用时补每个错过的半小时；full 的 `Persistent=true` 在重新激活时至多补一次错过的日常触发，不重放整段历史。休眠恢复可能触发一次已经到期的 calendar timer，不能把 `Persistent=false` 当作禁止任何恢复触发。每日 full 与 regular 错开 15 分钟不是互斥保证：同一 unit 活跃时不重新启动它；不同实例、手工命令或很慢的运行仍由统一写入锁拒绝并行，退出非零保留锁拒绝事实。本服务不设自动重启，下一固定周期再试。[systemd timer 官方说明](https://github.com/systemd/systemd/blob/v252/man/systemd.timer.xml)

组合采集服务配置 20 分钟启动期限，独立邮件服务 10 分钟，均有 30 秒停止宽限；oneshot 在命令退出前仍是 starting，显式 `TimeoutStartSec` 因此约束整次命令。[systemd service 官方说明](https://github.com/systemd/systemd/blob/v252/man/systemd.service.xml) 超时先发送 SIGTERM，停止宽限后可以 SIGKILL 终止 control group 全组进程。[systemd 终止规则](https://github.com/systemd/systemd/blob/v252/man/systemd.kill.xml) 这是部署级保护，不把 Fetcher 的 600 秒与发送的 300 秒协作预算改称精确硬期限；修改应用预算时同步核对服务上限。邮件接受后结果未提交的终止窗口保留不确定性，下一运行恢复 sending 为 uncertain，详见 [发送恢复](mail-sending.md)。被终止的 run 可能暂留 running，下一写入运行按现有恢复机制登记中断；事务/原文恢复验证范围见 [恢复实验](recovery-validation.md)。

在目标 Linux 主机先验证命令、日历与模板，再安装和启用：

```sh
systemd-analyze calendar '*-*-* *:00,30:00 Asia/Shanghai'
systemd-analyze calendar '*-*-* 01:15:00 Asia/Shanghai'
systemd-analyze calendar '*-*-* *:02/5:00 Asia/Shanghai'
systemd-analyze verify deploy/systemd/signalnest@.service deploy/systemd/signalnest-regular.timer deploy/systemd/signalnest-full.timer deploy/systemd/signalnest-mail.service deploy/systemd/signalnest-mail.timer
sudo install -m 0644 deploy/systemd/signalnest@.service /etc/systemd/system/signalnest@.service
sudo install -m 0644 deploy/systemd/signalnest-regular.timer /etc/systemd/system/signalnest-regular.timer
sudo install -m 0644 deploy/systemd/signalnest-full.timer /etc/systemd/system/signalnest-full.timer
sudo install -m 0644 deploy/systemd/signalnest-mail.service /etc/systemd/system/signalnest-mail.service
sudo install -m 0644 deploy/systemd/signalnest-mail.timer /etc/systemd/system/signalnest-mail.timer
sudo install -d -m 0755 /etc/systemd/journald@signalnest.conf.d
sudo install -m 0644 deploy/systemd/journald-signalnest-retention.conf /etc/systemd/journald@signalnest.conf.d/retention.conf
sudo systemctl daemon-reload
# 邮件入口仅在明确启用、核对冻结邮件和凭据后一起开启。
sudo systemctl enable --now signalnest-regular.timer signalnest-full.timer signalnest-mail.timer
systemctl list-timers signalnest-regular.timer signalnest-full.timer signalnest-mail.timer
```

日历显式标注时区，不依赖主机本地时区；启动前确认系统时钟与 Asia/Shanghai 时区数据正常。[systemd 时间表达式](https://github.com/systemd/systemd/blob/v252/man/systemd.time.xml) 升级或备份时停止三个 timer 和正在运行的采集/邮件 service；仅停止 timer 不会停止已经启动的进程。

采集及邮件服务模板均读取 `/etc/signalnest/smtp.env`（可选），由管理员建立 root 所有、0600 的 `NAME=value` 文件，名称须与 `[smtp]` 对应；不写 export、shell 命令或真实凭据示例。启用邮件但缺凭据会持久暂停，修复后先 `mail-status`，再显式 `mail-resume`。首次上线先保持 `mail-pause` 验证 unit 环境和路径，再核对预览并人工恢复；实际发送和超时演练仍需在目标机受控验证。

## 状态与日志

```sh
/opt/signalnest/.venv/bin/signalnest status --config /etc/signalnest/signalnest.toml
systemctl status signalnest@regular.service signalnest@full.service signalnest-mail.service
journalctl --namespace=signalnest -u signalnest@regular.service --since '24 hours ago'
journalctl --namespace=signalnest -u signalnest@full.service --since '7 days ago'
journalctl --namespace=signalnest -u signalnest-mail.service --since '24 hours ago'
/opt/signalnest/.venv/bin/signalnest mail-status --config /etc/signalnest/signalnest.toml
```

`status` 只读已有数据库，不联网、不建库、不取得写入锁、不修正 run 或 due；未初始化、不可读或版本不符必须明确失败。读取可能与合法写入同时发生，输出是诊断快照。重点看未成功历史数量/最老等待、到期复查及逾期、最近列表尝试/登记、最近完整扫描、独立运行结果与来源冷却。`remaining_unprocessed` 含保留成功版本的复查失败，不能替代 `last_success_at=NULL` 的历史积压。预算截断是可恢复事实，也可能表明容量不足；不因 `succeeded` 就推断 complete，不因首页 304 就跳过详情。

命令摘要写 stdout，有限结构化事件与错误写 stderr，都交给 namespace journal 保存；stdout 与 stderr 汇合后的日志不是单一纯 JSON 流。用 source/run/document/阶段/有限错误代码定位，不记录整页正文、URL 参数、配置或秘密。查看 F/H/R 的尝试、成功、失败与剩余 due，把连续预算截断、久未完整扫描和最老积压作为排查依据；这些是诊断提示，不发送系统告警邮件。邮件阶段按 mail_plan/mail_send/mail_storage 定位，查看暂停原因、阻断项及 uncertain；摘要保留采集与邮件各自结果，不能只看进程是否启动。

模板通过 `LogNamespace=signalnest` 自动连接独立 journald 实例；不会改全机器的日志配置。[systemd 执行环境说明](https://github.com/systemd/systemd/blob/v252/man/systemd.exec.xml) 专用 retention 配置按 128 MiB、14 天、单文件 16 MiB/最长一天起步；journal 会轮转、删除归档文件，活跃文件及元数据可有额外占用，不能宣传精确的总磁盘硬上限，也不保证保留满 14 天。[journald 配置说明](https://github.com/systemd/systemd/blob/v252/man/journald.conf.xml)

如果该 namespace 已运行，修改 retention 后只重启其 `systemd-journald@signalnest.service`，然后检查 namespace 的磁盘用量。不要为本项目修改 `/etc/systemd/journald.conf.d/` 或执行全机 vacuum；旧 systemd/无 namespace 部署须另行选择日志方案，不默默降低此模板的要求。日志不是业务数据库或原文备份；关键 run、失败、due 与冷却事实存在 SQLite，journal 过期不应使待办丢失。

## 一致备份

备份同时保存数据库、完整 `raw/`、配置、`uv.lock` 和使用的代码版本说明。仅复制 SQLite 主文件而遗漏正在使用的 WAL/日志不构成可靠运行中备份；仅复制数据库也不能恢复正文。此节点选择**停写 + 同一 writer_lock + SQLite backup API + raw 目录复制**，不做自动在线备份调度。

1. 停止并禁用三个 timer；停止所有采集/邮件 service，确认它们已 inactive，禁止同时执行手工导入、reparse、政策更新、初始化、采集、mail-plan 或 mail-drain。
2. 使用与生产相同配置和同一数据库锁，直到数据库 snapshot 和 raw 复制及校验结束。不能靠 `.lock` 文件存在与否判断停写，也不能删除一个可能仍被进程占用的 lock inode。外部不遵守 advisory lock 的程序须另行停止。
3. 在独立空目录建立备份；任何步骤失败视为未完成备份，不覆盖或清空原实例。没有数据库指针之前发布的孤立文件与 `.tmp-*` 暂时一并保留。
4. 完成校验后再恢复 timer；备份放到另一故障域，并按个人需要保留多份日期副本，例如每日 7 份和每周 4 份。当前不自动清理备份或业务原文。

下面在 `/opt/signalnest` 用项目 Python 执行，先让服务账号拥有专用备份父目录；将日期路径改为**尚不存在**的新目录。本示例使用真正的 SQLite backup API，锁覆盖两个存储介质的快照：

```sh
sudo systemctl disable --now signalnest-regular.timer signalnest-full.timer signalnest-mail.timer
sudo systemctl stop signalnest@regular.service signalnest@full.service signalnest-mail.service
cd /opt/signalnest
sudo -u signalnest /opt/signalnest/.venv/bin/python - <<'PY'
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from deploy.verify_backup import verify_backup
from signalnest.config import load_config
from signalnest.instance_lock import writer_lock

config = Path("/etc/signalnest/signalnest.toml")
settings = load_config(config)
snapshot = Path("/var/backups/signalnest/2026-10-08")
snapshot.mkdir(mode=0o700)  # Fail if the destination already exists.
with writer_lock(settings.storage.database):
    # Reject an unsafe source raw/ root before copytree can follow it.
    verify_backup(settings.storage.database, settings.storage.data_dir)
    with closing(sqlite3.connect(settings.storage.database.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(snapshot / "signalnest.sqlite3")) as destination:
            source.backup(destination)
    shutil.copytree(settings.storage.data_dir / "raw", snapshot / "raw", symlinks=True)
    shutil.copyfile(config, snapshot / "signalnest.original.toml")
    shutil.copyfile(Path("uv.lock"), snapshot / "uv.lock")
    print(verify_backup(snapshot / "signalnest.sqlite3", snapshot))
    (snapshot / "backup.complete").write_text("verified\n", encoding="ascii")
PY
```

另写明对应 Git revision 或源码快照（未提交改动不能只写 HEAD），可选导出必要日期的 journal。`backup.complete` 只标记本脚本走完校验，不证明断电耐久性，也不能替代异地副本与恢复演练。先在锁内校验原实例，拒绝 raw 根目录或路径组件为符号链接；`copytree(symlinks=True)` 本身不拒绝源根目录符号链接。复制子项时保留符号链接，不追到目录外，复制后再次校验。发现 unsafe 路径/入口即失败，先调查原实例，不能默默解引用修补。

## 恢复演练与正式切换

先用对应代码版本和 Python 3.12 在新的临时目录恢复，始终保持离线：复制 snapshot 数据库为 `signalnest.sqlite3`，复制 raw 目录；不要把正文重新导入，不改 fetched_at、SHA、解析版本、due、成功指针或冷却。不要覆盖正在运行的数据库。锁文件不作为备份内容，它只是协调活进程的 inode。

```sh
# restore_dir is an existing empty absolute directory containing a copied DB and raw/.
/opt/signalnest/.venv/bin/python /opt/signalnest/deploy/verify_backup.py \
  --database "$restore_dir/signalnest.sqlite3" --data-dir "$restore_dir"
```

校验器以 SQLite `mode=ro` 打开，检查当前代码所期望的 migration revision、`integrity_check`、`foreign_key_check`、成功指针，并逐一通过 RawStore 验证每个数据库正文引用的路径和 SHA。缺失/损坏/逃逸/符号链接明确非零退出，不下载修补，不修改表，也不自动删孤立文件；同时专项校验冻结 MIME、精确成员与历史决策绑定、投递状态及逐次尝试/暂停关系，输出邮件各状态及尝试数量；详情和有限错误分类见 [邮件备份恢复](mail-backup.md)。输出也包含原文引用数、版本/通知/响应数、孤立文件和未完成临时文件数。它只验证**被引用的**正文摘要，不认证未引用孤立文件的内容。

revision 不匹配时先用备份对应版本验证；计划升级则保留该备份、复制另一份，显式运行新版本 `storage-init`，再次校验。不要通过在唯一备份上迁移来隐藏版本不一致。为恢复副本建立新的测试配置，data_dir/database 指向恢复目录，然后运行 `config-check`、`status`，核对身份数、当前版本、待办、run 和冷却。不得调用 `scheduled-run` 来证明离线恢复。

校验与人工核对通过后，仍停止生产 timer/服务，在锁保护且没有其他写入者时切换到恢复实例，同步服务可写路径/配置并检查账号权限。保留原数据库与 raw 副本作为回退，不直接擦除。重新启用前按首次/长停机规则执行一次有界 full；若恢复的服务器冷却尚有效，继续等待，不能清空 not-before 来追赶积压。

此校验和离线备份测试覆盖真实 SQLite snapshot、文件复制、缺失/损坏/路径异常及外部损坏的版本指针；不等于目标 Linux 的 systemd 启动验证、真实断电验证、异地介质耐久性测试或无人值守长期运行证明。上线后先观察一周再调整频率/预算。


## 含邮件状态的备份恢复

专项校验已实现，pending、accepted、sending、uncertain、暂停均为有效快照事实；不会重新渲染或重评旧政策邮件。恢复副本保持所有 timer 停止、`mail_runtime.enabled=false`、不注入 SMTP 凭据，先 `mail-pause` 再 `mail-status`；不是只依赖不同数据库路径的锁来避免同时发送。

遗留 sending 由持锁发送入口一次恢复为 uncertain，暂停仍禁止任何 SMTP；只读校验器和 status 不执行恢复。accepted 永不自动重发，快照后的外部接受可能不在备份中，核对原实例与服务端日志后才切换。不得删除或重建 accepted、成员或投递唯一键以清理积压。完整步骤和实际 SQLite backup API 验证范围见 [邮件备份恢复](mail-backup.md)。

本轮包含真实子进程的争锁、凭据传递和 supervisor 超时终止/暂停恢复实验；SMTP 为本地模拟服务，macOS 不运行 systemd。目标 Linux 的 unit 启动、EnvironmentFile 加载和实际服务超时仍须验证，不能将静态检查或模拟 supervisor 称为已完成部署。
