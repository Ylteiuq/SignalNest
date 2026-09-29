# SignalNest

单用户、自托管、长期运行的个人校园信息助手，采用 Python 模块化单体。首个信息源为武汉大学本科生院“学生通知”。**第一阶段项目骨架已完成**；目前不会采集校园网站或发送邮件。

## 安装与检查

支持 Python **3.12.x**。安装稳定版 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在仓库根目录运行：

```sh
uv sync --locked
uv run --locked signalnest --help
uv run --locked signalnest config-check --config config.example.toml
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

`uv.lock` 锁定稳定依赖，禁止预发布版本。包名与 CLI 均为 `signalnest`，也可使用 `uv run --locked python -m signalnest`。技术栈为同步 HTTPX、Beautiful Soup（显式 `html.parser`）、Pydantic、同步 SQLAlchemy Core、Alembic、SQLite、argparse 和标准库 logging。

## 配置

```sh
cp config.example.toml signalnest.toml
uv run --locked signalnest config-check --config signalnest.toml
```

普通配置为 TOML，包含数据目录、数据库文件路径、信息源、HTTP 超时、请求间隔与 User-Agent。请求间隔是相邻请求间隔，不是轮询周期；未来协调层负责执行，当前没有采集循环。没有尚未使用的邮件/模型密钥配置，未来密钥使用环境变量。

**data_dir 和 database 都相对于配置文件所在目录解析**，database 不相对于 data_dir。配置文件路径本身由调用者定位；绝对路径保持绝对路径，`~` 展开为主目录，符号链接配置以目标文件所在目录为准。

`config-check` 校验 TOML、未知字段、HTTP(S) URL、正数且有限的超时/请求间隔；不测试网络可达性或目录可写性，不创建目录/数据库。导入模块及查看帮助同样不联网、不打开数据库、不启动任务。

## 显式初始化与升级

```sh
uv run --locked signalnest storage-init --config signalnest.toml
```

命令创建数据目录、`raw/`、数据库父目录，并通过 Alembic 升级至最新迁移（当前 `0001_initial`）。重复运行保留数据，后续安装新版本也用此命令升级；不使用 `create_all`，不提供清空或降级命令。成功退出码为 0，存储错误为 1，配置或命令用法错误为 2。迁移失败回滚，已创建的目录或空数据库文件可能保留。

可在临时目录验证（macOS / Linux）：

```sh
trial_dir="$(mktemp -d)"
cp config.example.toml "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
```

结果位于 `$trial_dir/data/`。迁移仅支持单进程执行，尚无并发初始化协调。升级个人数据前保留数据库与 raw 目录备份。

从前两个节点升级：执行 `uv sync --locked` 后改用 `signalnest` 命令；**继续使用原配置文件及原 database 路径**即可，更名不搬动数据库，不修改已有表或迁移版本。新示例使用 `signalnest.toml` / `signalnest.sqlite3`，不需要手动给已有文件更名。

## 日志与当前边界

命令结果写 stdout；结构化事件日志写 stderr，包含 UTC 时间、级别、事件名和可选 source_id/run_id/document_id。错误诊断也写 stderr，因此错误输出不是纯 JSON 流。日志不包含原始配置、URL、异常正文或网页正文；只在 CLI 显式配置 SignalNest 的 logger，不修改 root logger。

已实现：安装/CLI/配置、数据库初始化与迁移、三张业务表及约束、列表/正文/附件/原始响应引用契约、稳定内容摘要、同步 HTTP 客户端配置、纯 HTML 树构建、结构化日志与离线测试。

尚未实现：完整站点 Parser、原始文件写入器、通知发现与更新服务、HTTP 重试/限速循环、调度、Email、历史搜索、LLM/Embedding/RAG/Agent。HTTP 客户端不自动发送请求；HTML 树构建不等于通知字段解析。不增加用户系统、微服务、Redis、Celery、向量数据库、Docker、CI 或跨语言接口。

后续依次为：**fixture 驱动的 Parser → 单次可靠采集 → 调度与 Email → 真实运行观察**。普通运行处理增量/待补抓任务；首次历史导入建立基线；未来通知策略独立决定哪些事件发送邮件，不默认给所有历史通知发邮件。

列表 304 不代表没有待补抓详情或到期复查任务；“遇到已知通知就停止分页”不能保证完整性。模块边界、数据模型与 Parser 任务见 [设计说明](docs/design.md)。

## 实际验证

2026-09-29，macOS / Darwin arm64，CPython 3.12.14，SQLite 3.53.1，uv 0.12.20。锁定的直接依赖为 Pydantic 2.13.5、SQLAlchemy 2.0.54、Alembic 1.20.0、HTTPX 0.28.1、Beautiful Soup 4.15.0；开发检查使用 pytest 8.4.2、Ruff 0.16.9。工具安装于临时隔离环境，未修改系统 Python。

**54 项离线测试全部通过，Ruff 检查与格式检查通过。** 测试覆盖配置和路径、错误退出、无副作用导入、迁移与重复初始化、唯一/外键/状态约束、升级失败回滚、数据契约与摘要、HTTP MockTransport、fixture HTML 后端、日志字段与敏感内容排除。测试默认离线；调研与 fixture 保持原样。sdist/wheel 构建通过；按锁文件在独立环境安装 wheel 后，已从临时工作目录验证帮助、配置校验不建库、两次初始化、迁移版本、错误退出与 JSON 日志。更名前的配置文件/数据库路径可继续使用，初始迁移内容未变。未验证其他操作系统/Python 次版本、并发迁移或校园网站实采。
