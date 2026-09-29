# Campus Information Agent

单用户、自托管的个人校园信息助手，采用 Python 模块化单体。首个信息源为武汉大学本科生院“学生通知”。

按小块交付，当前是 **第一阶段 / 第 2 块：本地存储与迁移**，并非完整第一阶段验收版本。

## 安装与验证

支持 Python **3.12.x**；暂不承诺其他次版本。安装稳定版 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在仓库根目录运行：

```sh
uv sync --locked
uv run --locked campus-information-agent --help
uv run --locked campus-information-agent config-check --config config.example.toml
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

uv.lock 固定依赖版本；禁止预发布依赖。也可以通过 `uv run --locked python -m campus_information_agent` 使用相同 CLI。

## 配置

复制 `config.example.toml` 为 `campus.toml`，使用 `--config campus.toml` 指定。配置路径本身由调用者定位；**配置中的 data_dir 和 database 都相对于配置文件所在目录**，数据库路径不是相对于 data_dir。绝对路径保持绝对路径；`~` 展开为用户主目录。符号链接配置以实际目标文件所在目录为准。

配置仅包含数据目录、数据库文件路径、信息源和未来同步 HTTP 请求所需的超时、请求间隔、User-Agent。请求间隔是相邻请求间隔，不是轮询周期。没有邮件或模型密钥配置。

`config-check` 校验 TOML、字段、HTTP(S) URL、有限正数超时和请求间隔；错误显示字段并返回退出码 2，不回显原始配置值。它不验证网络可达性或目录可写性，不创建目录或数据库。URL 校验不代替后续获取器的重定向与主机检查。

## 显式初始化与升级

先校验配置，再运行：

```sh
uv run --locked campus-information-agent storage-init --config campus.toml
```

该命令创建数据目录、`raw/` 子目录和数据库父目录，再通过 Alembic 升级到最新迁移（当前 `0001_initial`）。后续安装新版后仍使用同一命令升级。重复运行不会清空已有记录或原始文件。不使用 `create_all`，不把已有未知版本数据库强行标记为最新版本。配置错误退出码为 2，预期存储错误为 1，成功为 0。迁移事务失败会回滚，但已创建的目录或空数据库文件可能保留。

可在临时目录试用，避免写入仓库数据目录（macOS / Linux）：

```sh
trial_dir="$(mktemp -d)"
cp config.example.toml "$trial_dir/campus.toml"
uv run --locked campus-information-agent storage-init --config "$trial_dir/campus.toml"
uv run --locked campus-information-agent storage-init --config "$trial_dir/campus.toml"
```

结果位于 `$trial_dir/data/`。升级现有个人数据前请保留数据库与 raw 目录的备份；本阶段只支持单进程执行迁移，尚未实现并发初始化协调。CLI 不提供降级或清空数据命令。

## 当前边界与后续

已实现可安装 src 包、CLI 帮助、无写入的配置校验、SQLite 初始化/升级、三张业务表与唯一/外键/状态约束、离线测试和 Ruff。同步 SQLAlchemy Core 负责存储；未额外引入 ORM 实体层。

目前初始化只创建结构，不写入示例通知或发起采集。原始响应文件写入、通知发现/更新服务、Parser、调度、邮件、搜索及模型能力尚未实现。当前不会请求校园网站或发送邮件。

**第一阶段还剩第 3 块**：数据契约、获取/解析/协调边界、标准库结构化日志及整体验收。HTTPX 和 Beautiful Soup 将随使用它们的模块加入，不预建空实现。

之后依次实现：fixture 驱动的 Parser → 单次可靠采集 → 调度与 Email → 真实运行观察。详见 [设计说明](docs/design.md)。不增加用户系统、服务拆分或跨语言接口。

## 实际验证环境

2026-09-29：macOS / Darwin arm64，CPython 3.12.14，SQLite 3.53.1，uv 0.12.20；Pydantic 2.13.5、SQLAlchemy 2.0.54、Alembic 1.20.0、pytest 8.4.2、Ruff 0.16.9。uv 使用临时工具目录，未更改系统 Python。全部依赖锁定稳定版，禁止预发布版本。

36 项离线测试通过，Ruff 检查与格式检查通过。已验证锁文件安装、sdist/wheel 构建、独立 wheel 安装后从临时工作目录运行迁移、CLI 帮助与配置校验、临时 SQLite 初始化和重复初始化、业务唯一约束、外键、失败后保留成功版本、迁移升级/失败回滚、Ruff 检查与格式检查。测试离线，不请求校园网站；既有 fixture 保持原样。未验证其他操作系统及 Python 次版本、并发迁移或真实采集。
