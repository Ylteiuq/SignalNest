# Campus Information Agent

单用户、自托管的个人校园信息助手，采用 Python 模块化单体。首个信息源为武汉大学本科生院“学生通知”。

按小块交付，当前是 **第一阶段 / 第 1 块：安装、配置与 CLI**，并非完整第一阶段验收版本。

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

## 当前边界与后续

已实现可安装 src 包、CLI 帮助和无写入的配置校验、离线测试、Ruff。尚无数据库初始化/迁移、采集命令、完整 Parser、调度、邮件、搜索或模型能力。当前不会请求校园网站或发送邮件。

第一阶段剩余两块：

1. SQLAlchemy / Alembic / SQLite 的真实初始化与升级、最小数据模型、幂等性验证。
2. 数据契约、模块边界、标准库结构化日志和阶段验收。

之后依次实现：fixture 驱动的 Parser → 单次可靠采集 → 调度与 Email → 真实运行观察。详见 [设计说明](docs/design.md)。不增加用户系统、服务拆分或跨语言接口。

## 本块实际验证记录

2026-09-29：macOS / Darwin arm64，CPython 3.12.14，uv 0.12.20；锁定并验证 Pydantic 2.13.5、pytest 8.4.2、Ruff 0.16.9。uv 在临时目录安装，Python 使用本机可用的独立运行时，未更改系统 Python。

已验证锁文件安装、CLI 帮助、示例配置、15 项离线测试、Ruff 检查/格式检查、sdist 和 wheel 构建。测试覆盖跨工作目录路径解析、配置错误与非零退出、导入/帮助无网络或数据库副作用。所有既有 fixture 的 SHA-256 保持一致。未验证其他操作系统及 Python 次版本；数据库迁移、重复初始化、唯一约束属于下一块，当前未实现也未验证。
