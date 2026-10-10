# WSL 部署与实例生命周期

本指南用于新 N2 的 Windows 宿主 + Linux 实例部署。当前已核验 Ubuntu 24.04 / WSL2、systemd 255（PID 1）、Python 3.12.3、SQLite 3.45.1 及 FTS5 trigram、ext4，并完成受限实采、工程邮件实际收件和 Linux 隔离恢复。2026-10-10 已升级至 0008，真实 24 页完整扫描、首页复核、持久成功时间及新快照隔离恢复通过，见[升级验收](validation/n2-wsl-upgrade-20261010.md)。生产通知、宿主重启及一周验收仍未完成；此前中断与邮件工程验收见[历史实机记录](validation/n2-wsl-20261009.md)。

## Linux 路径与应用前置条件

沿用 [运维模板](operations.md) 的单实例路径：项目和虚拟环境 `/opt/signalnest`，配置 `/etc/signalnest/signalnest.toml`，数据和数据库 `/var/lib/signalnest`，运行账号 `signalnest`。数据库和 raw 放在 Linux ext4，避免移到 `/mnt/c` 等 Windows 挂载目录。初始化、迁移和后续写入统一使用服务账号，避免 root 建立仅 root 可写的数据库或 `.lock`。

`ProtectHome=yes` 会限制服务访问 `/home` 和 `/root`；核对 `.venv/bin/python` 的真实路径，不能仅看虚拟环境位于 `/opt` 就认为解释器可访问。使用已核验的 Python 3.12，安装锁定依赖、核对源码快照及 `rollout-check`；当前 migration 包含搜索虚拟表，初始化实际成功才证明该解释器的 SQLite 可用。具体发布和检查入口见 [上线准备](rollout.md)。

目标 Windows 的 `C:\Windows\System32\wsl.exe` 曾返回“系统无法访问文件”，而以下显式入口已能正常调用：

```powershell
$wslExe = 'C:\Program Files\WSL\wsl.exe'
& $wslExe --list --verbose
```

本实例使用已核验的显式路径，不依赖 PATH 或自动替换其他发行版。Ubuntu 版本不等于 WSL 注册名称；命令中的发行版名称须从当前拥有者账号的列表核对。WSL 的 `--distribution` 和 `--user` 支持显式指定目标；安装属于 Windows 用户，不能换成 SYSTEM 就假定仍能访问同一实例。[WSL 命令](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)、[用户与发行版](https://learn.microsoft.com/en-us/windows/wsl/setup/environment)

## systemd 不负责 WSL 保活

微软明确说明 systemd 服务不会使 WSL 实例持续存活；仅在 Linux 中启用 SignalNest timer，不能证明离开终端后仍会运行。[官方 systemd 说明](https://learn.microsoft.com/en-us/windows/wsl/systemd)

本项目采用一个应用专用 Windows 登录任务，直接保持前台 WSL 命令：

```text
C:\Program Files\WSL\wsl.exe --distribution "<核对后的实际名称>" --user signalnest --exec /usr/bin/sleep infinity
```

`sleep` 不执行采集、不访问数据库，只保留命令会话；采集和邮件仍由 Linux systemd timer 触发。不把 sleep 改成 Linux systemd 服务作为保活证明，也不修改全局 `.wslconfig`、宿主电源、自动登录或安全策略。这是根据前台会话生命周期设计的工程方案，微软文档没有对该具体命令提供持续运行保证，必须在本机验证。

## 最小 Windows 登录任务

以下为供操作者检查后执行的模板。任务属于拥有该 WSL 发行版的 Windows 用户；在其交互桌面会话中执行。当前目标机直接 executable 和 cmd 包装曾提示 WSL_E_DISTRO_NOT_FOUND，而相同 SID/注册的 PowerShell 参数调用已运行，故模板使用该入口；未断言原错误根因是权限或用户差异。替换实际发行版名称，核对 `signalnest` Linux 账号已经存在。任务不包含 SMTP 凭据、Windows 密码或应用正文。

```powershell
$taskName = 'SignalNest-WSL-KeepAlive'
$wslExe = 'C:\Program Files\WSL\wsl.exe'
$distribution = '<核对后的实际名称>'
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($distribution -notmatch '^[A-Za-z0-9_.-]+$') {
    throw 'This template supports a checked distro name without spaces or shell syntax.'
}

# 已有同名任务须先核对，不覆盖未知任务。
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'Task already exists; inspect its owner and action before updating.'
}

$principal = New-ScheduledTaskPrincipal `
    -UserId $taskUser -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$logDir = Join-Path $env:LOCALAPPDATA 'SignalNest'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$launch = @'
$ErrorActionPreference='Stop'
$log=Join-Path $env:LOCALAPPDATA 'SignalNest\wsl-keepalive.log'
& 'C:\Program Files\WSL\wsl.exe' --distribution {0} --user signalnest --exec /usr/bin/sleep infinity *> $log
exit $LASTEXITCODE
'@ -f $distribution
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($launch))
$action = New-ScheduledTaskAction `
    -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument ('-NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $encoded) `
    -WorkingDirectory $logDir
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'SignalNest WSL session while the instance owner is logged on.'
```

注册后导出任务 XML，核对 `InteractiveToken`、`LeastPrivilege`（默认值可能省略）、`ExecutionTimeLimit=PT0S`、`IgnoreNew`、三次重启和一分钟间隔，以及解码后的 executable/发行版。EncodedCommand 只是完整传递公开脚本，不是加密，也不改变执行策略；日志每次启动覆盖，不记录凭据。不要使用最高权限、SYSTEM、S4U、`InteractiveOrPassword` 或 `-Password`，也不要为注册失败而更改全机策略。同账号的交互任务可不提供密码；仍受任务文件和目录权限约束。[任务权限](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks)、[登录类型](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype)

默认任务运行时限为 72 小时；`PT0S` 才允许无限运行，不能省略时限后宣称一周保活。`IgnoreNew` 避免重复启动同一任务实例；失败重启最多三次，并非无限重试。[时限](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-executiontimelimit)、[并行规则](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-multipleinstances)、[重启间隔](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-restartinterval)

模板不设置 `WakeToRun`，也不改变宿主睡眠设置。电池相关条件保留系统创建的任务设置，部署者须核对：如果需要在电池供电且机器仍醒着时保持会话，可只对该任务显式采用 `AllowStartIfOnBatteries` / `DontStopIfGoingOnBatteries`；这不意味着防止宿主休眠。不要在文档未说明时暗中更改这两个条件。[任务设置参数](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset)

## 交互登录、重启与休眠边界

Interactive 任务仅能在该用户已有交互登录会话时运行。SSH 连接成功不证明 Windows 桌面用户已登录；无交互会话时即使任务注册成功，也不能声称已开始保活。Windows 重启后，需要该用户实际登录才能触发本方案，**不承诺无人登录开机启动**。[交互令牌](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype)

正常退出、人工停止任务或有限重启耗尽后，可能需要人工启动或下一次登录。`StartWhenAvailable` 仅适用于相应时间触发，不能给登录任务补充无人登录开机能力。[适用范围](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-startwhenavailable)

宿主休眠、关机、注销或显式终止发行版期间，不能假定采集和 SMTP 仍在推进。当前没有宿主唤醒、自动登录或 Windows 服务方案。Linux timer 的 Persistent 行为只能在 Linux 恢复运行后处理相应错过触发；不是休眠期间运行，也不是重放每次遗漏。长停机恢复继续遵循 [运维说明](operations.md) 的有界 full 和冷却规则，不清空 due 或 not-before 追赶。

## 保活的实际验证方法

1. 启动专用任务，记录 Windows 任务状态和该 `wsl.exe` 的 PID/参数、Linux PID 1 启动时刻、三个 timer 的下次触发及发布指纹。
2. 退出 SSH 和其他 WSL 终端。等待数分钟后，先只使用 Windows 任务状态和 `wsl.exe --list --running` 检查发行版仍在运行；不要先执行 Linux 查询命令，因为它可能重新启动已停止的发行版，造成假阳性。
3. 确认仍在运行后，再进入 Linux 对比 PID 1 起始时间，并核对断线期间实际产生的 timer/journal 和应用运行记录。不能只比较 boot_id：同一 WSL2 虚拟机的其他发行版可能仍在运行，单个发行版重启未必改变共享内核标识。
4. 重复启动同一任务，核对 IgnoreNew 未产生第二个任务实例。任务“正在运行”不是应用健康证明；同时检查列表完整扫描时间、处理积压、邮件暂停/未知尝试及实际收件。
5. 在邮件暂停、目标实例可中断且无业务写入时，单独验证失败重启。只终止已确认的项目 sleep 进程，记录 wsl.exe 的实际退出结果和任务重启时间；不要用全局 `wsl --shutdown` 影响其他发行版。正常返回 0 与显式人工停止不保证走失败重启。
6. 下一次真实用户登录和宿主重启时另留证；没有实际执行就标未验证，不为凑验收而强制重启用户机器。连续七天保存观察，明确所有睡眠、关机、人工操作和未观察区间。

上述是完整验收步骤；本次有限实机运行不能替代下一次登录、宿主重启和一周运行结论。实际时间、任务定义、断线期间运行和未验证项继续记录于[实机记录](validation/n2-wsl-20261009.md)。

## 应用验收仍按原顺序推进

先在邮件关闭状态下初始化并完成真实 full；只有 `coverage=complete`、首页复核及持久时间符合预期才记录覆盖成功。再检查授权画像、通知启用预览与基线，激活后先暂停，预览小额度冻结邮件。`mail_runtime.enabled=false` 只关闭后台邮件，显式 `mail-drain` 仍可能发送，暂停状态才是通道控制。

凭据由 Linux systemd 的受限 EnvironmentFile 注入，不加入 Windows 保活任务。核对单封即时邮件、Digest、本地 accepted 与真实收件后，才启用邮件定时入口。组合采集服务上限 20 分钟、独立邮件服务 10 分钟，停止宽限 30 秒；这些 Linux 服务限制与 Windows 保活任务的无限运行时限是不同职责，不能互相替代。

服务超时、锁冲突、凭据注入和备份恢复使用隔离实例，避免对正在投递的正式邮件制造未知接受窗口；恢复副本先关闭 timer、不注入凭据并暂停。完整流程见 [上线准备](rollout.md)、[后台邮件](background-mail.md) 与 [邮件恢复](mail-backup.md)。用户授权的假设画像只用于初步运行验证，不证明本人资格；后续正式配置需再核对并显式更新政策。
