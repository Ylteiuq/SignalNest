# changedetection.io 获取与恢复机制调研

调研日期：2026-09-28（Asia/Shanghai）  
范围：仅看 HTTP/浏览器获取、比较状态、持久化和失败恢复，不评述全项目架构。  
版本：`v0.60.7`，commit [`593e9cc48c2b475dacbc3dbdebe2f5136bef8efa`](https://github.com/dgtlmoon/changedetection.io/commit/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa)。以下源码链接固定在该 commit，便于复查。

## 1. 获取方式、超时和参数

**上游实际实现。** 默认 HTTP fetcher 是 `html_requests`；全局请求超时的默认值为 45 秒，可通过 `DEFAULT_SETTINGS_REQUESTS_TIMEOUT` 配置，fetcher 接收并传入该 timeout。每个 watch 可以选择 fetcher。HTTP 请求支持方法、headers、body、proxy 等请求参数；当前 requests fetcher 显式关闭 requests 的自动重定向，自己跟随并限制跳转次数。源码中请求使用 `verify=False`，这是上游当前实现，不应不加判断照搬。[fetcher 选择与配置](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/processors/base.py) · [HTTP 请求实现](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/content_fetchers/requests.py) · [默认设置](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/model/App.py)

HTTP 传输层的 `Retry` 配置由 `REQUESTS_RETRY_MAX_COUNT` 控制，源码默认值为 6，`backoff_factor=0.5`；连接错误和读取错误可重试，`status=0` 表示 HTTP 状态码本身不触发 urllib3 的状态重试。相邻注释仍写着“default: 3 attempts”，与实际默认值不一致，应以可执行配置为准。这是“最多 6 次重试配置”，不是 6 次总请求。[HTTP 重试配置](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/content_fetchers/requests.py)

浏览器 fetcher 作为可选后端，由 watch/global 设置解析。浏览器实现接收 timeout；连接 Chrome DevTools Protocol 的调用另有 60 秒超时。它适合页面必须执行 JavaScript、或需要浏览器步骤的情况，资源和运行复杂度更高。[后端解析](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbde2f5136bef8efa/changedetectionio/processors/base.py) · [Playwright fetcher](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbde2f5136bef8efa/changedetectionio/content_fetchers/playwright.py)

**本项目建议。** 第一阶段先用普通 HTTP；只有校园页面在 HTTP 响应里确实缺少所需字段，或明确要求浏览器交互时，再加浏览器方案。为连接/读取分别设有界超时，超时数值按实际页面测量后定；保留 TLS 证书校验。最多少量重试瞬时传输失败；不要把所有状态码都重试。重定向时校验目标主机，避免被诱导访问内网地址。

## 2. 失败记录、重试与下一轮调度

**上游实际实现。** HTTP 的上述自动重试属于单次 fetch 内的传输层重试，覆盖连接/读取类异常，不覆盖 HTTP 状态码。worker 对失败异常分类后写入 watch 的 `last_error`，部分错误还保存状态码、页面文本或截图；成功检查会清除错误。`last_checked` 在检查开始时记录，因此它表示尝试时间，不等同于成功时间。[worker 状态处理](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/worker.py) · [HTTP fetcher](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbde2f5136bef8efa/changedetectionio/content_fetchers/requests.py)

worker 在异常后 `sleep(1)` 是消费完当前任务后的短暂停顿，不是把失败任务重新入队。watch 有 `time_between_check` 检查间隔；默认设置为 3 小时，并可按 watch 配置。创建 watch 的 API 注释也明确把普通检查交给 scheduler，而不是每次创建时立即入队。[worker](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/worker.py) · [调度间隔模型](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/model/Watch.py) · [创建 watch 与 scheduler 说明](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbde2f5136bef8efa/changedetectionio/api/Watch.py) · [默认间隔](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbde2f5136bef8efa/changedetectionio/model/App.py)

**尚未确认。** 本次未完整确认 scheduler 对失败 watch 是否应用独立退避、失败后准确的下一次到期计算，以及当前 scheduler tick 的时序；因此不能据此断言“失败后固定 3 小时再试”。能确认的是存在请求内重试和常规周期调度两层，worker 的 1 秒停顿不是第三层重试。[scheduler/API 入口](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/api/Watch.py)

**本项目建议。** 把“本次抓取失败”与“内容为空/内容变更”区分开，保留最近一次成功内容；错误状态记录 `last_attempt`、`last_success`、错误类别/HTTP 状态和下次计划时间。对临时网络错误做少量有上限的快速重试，耗尽后交由低频调度，避免失败循环。若需要恢复到期任务，在启动时按持久化的 `next_due` 补入即可。

## 3. 比较、持久化与重启

**上游实际实现。** 默认文本处理器先按过滤/清洗后的结果计算内容摘要并比较；页面原始 HTML 还有单独 checksum，用来在原始响应未变且过滤配置未变时跳过后续处理。只有首次基线或检测到变化时才保存历史快照。历史由 watch 目录内的 `history.txt` 索引时间戳和快照文件组成。[默认差异处理器](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/processors/text_json_diff/processor.py) · [watch 历史/快照](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/model/Watch.py) · [worker 保存变更](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/worker.py)

watch 元数据落盘为每个 watch 目录中的 `watch.json`；文件存储有原子写入路径，启动时从存储目录加载 watch。历史快照与元数据分开保存。任务队列是进程内结构，所以队列中的待执行项不应视为持久化；重启时 CLI 提供 `-r all` 强制全部重新排队。普通启动后 scheduler 如何挑选到期项取决于调度逻辑。[文件数据存储](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/store/file_saving_datastore.py) · [持久化入口](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/model/persistence.py) · [内存队列](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/custom_queue.py) · [启动重抓选项](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/changedetectionio/__init__.py)

**本项目建议。** 以稳定公告 URL 或站点公告 ID 去重，另存规范化内容摘要；仅在首次抓取或摘要改变时产生新版本。持久化每个入口的游标/最近记录、成功与失败时间、摘要和下次到期时间。采用单机本地存储即可；写入元数据应具备事务或原子替换，重启时扫描 `next_due <= now` 的入口。HTTP 错误页、验证码页、空响应不能覆盖最后一次成功基线。

## 4. 第一阶段取舍

**适合借鉴：** HTTP 优先；显式超时；有限的连接/读取重试；失败与成功状态分开记录；稳定 ID + 内容摘要去重；原子/事务式本地状态；启动时恢复已到期采集。

**等需求出现再考虑：** Playwright/浏览器池、多 worker 与优先级队列、插件式 fetcher/processor、截图和错误页面归档、复杂过滤规则。changedetection.io 的配置面和兼容分支服务于通用网页监控，对 1～3 个校园入口会增加部署与运维成本。

## 5. 许可证与代码复用

该 commit 根目录许可证为 Apache License 2.0。许可证允许复制、修改和分发，但分发时需附许可证文本、标注修改过的文件、保留适用的版权/专利/商标/归属声明；若源分发包含 `NOTICE`，其适用声明也需保留。本报告建议只借鉴行为，不复制代码。若以后复制具体实现，还需核实被复制依赖及其许可证；上游主仓库许可证不自动覆盖依赖。[LICENSE](https://github.com/dgtlmoon/changedetection.io/blob/593e9cc48c2b475dacbc3dbdebe2f5136bef8efa/LICENSE)
