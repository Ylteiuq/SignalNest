# 查询 Agent 的回放与人工评价

最初节点完全离线，后续本机兼容代理的受限真实回放见[10-13记录](validation/query-proxy-20261013.md)。九篇既有UC/CS原文、九个工程查询连接真实归档、Parser、SQLite索引和只读Agent；研究与fixture不变。问题和时钟是工程情境，不是用户搜索日志，最低候选检查不是人工gold。

完整回放和审核 bundle 会包含真实通知正文、回答证据及查询画像。请将它们保存在仓库外的本机受限目录；仓库只保存不含这些正文/画像的汇总记录。`.gitignore` 会拦截标准命名的本地结果文件，但不能替代提交前检查。

## 本地回放

在仓库内使用已安装的环境运行，输出目录须已存在，文件名须未使用：

```sh
private_dir="$(mktemp -d "${TMPDIR:-/tmp}/signalnest-query.XXXXXX")"
uv run --locked python deploy/evaluate_queries.py --check \
  --output "$private_dir/query-local.json"
uv run --locked python deploy/human_review.py prepare \
  --queries "$private_dir/query-local.json" --output "$private_dir/query-bundle.json"
uv run --locked python deploy/human_review.py report \
  --bundle "$private_dir/query-bundle.json"
```

默认没有模型请求，不读取模型或 SMTP 凭据。程序只使用自己新建的临时 UC/CS 实例，明确覆盖示例中的存储路径，不操作生产数据库；临时实例归档使用 fixture 原获取时间，处理和本地目标登记使用本轮实际时间，目标的发现来源为 unknown，不冒充真实列表发现。运行前后校验数据库字节保持不变。原始字节与内容摘要、Parser 版本、模型/查询代码摘要均保留。

回放画像始终是合成测试输入。它的 `profile_id = "self"` 沿用单用户契约，不能用任意测试名称代替这个字段；测试身份在清单说明中标注，不表示操作者已确认该画像。2026-10-13 修正了清单的无效 ID；清单摘要及新回放的输入指纹随之变化，旧快照和人工记录保留，不继承旧输入标签。

`--check` 只核对有限候选与程序日期范围；不把“关键词搜到”当成资格、截止或真实报名机会的正确判断。规则和搜索的旧 46 项人工评价继续使用旧入口和旧记录，不被新九例替换。

## 真实模型的后续入口

用户选定工具调用兼容模型、核对实际单价并配置环境变量后，才显式运行：

```sh
uv run --locked python deploy/evaluate_queries.py --model-config ask.toml \
  --max-total-cost-usd 0.05 --run-seconds 180 \
  --output "$private_dir/query-model.json"
uv run --locked python deploy/human_review.py prepare \
  --queries "$private_dir/query-model.json" --output "$private_dir/query-model-bundle.json"
```

这里`--model-config`**会启用实际提供方请求**，只取模型设置；私人实例和画像不参与固定样本回放。问题、有限原文和合成画像会发送给选定提供方，不能与`--check`同用。

明确配置`unpriced_proxy`时，不填美元单价/预算，改用整批物理请求上限：

```sh
uv run --locked python deploy/evaluate_queries.py --model-config ask.proxy.example.toml \
  --max-total-requests 4 --run-seconds 90 \
  --output "$private_dir/query-proxy.json"
```

此时费用与美元预算为null；传入`--max-total-cost-usd`会退出2，不用虚构价格声称控制真实额度。每项的HTTP上限取整批剩余与默认8次中的较小值，重试也计入。耗尽保留已处理案例、失败和未运行ID，不算全九项完成。

每个问题复用`ask`的调用、工具、文本及输出上限；整批最多12个问题，另有请求上限（默认64次，配置1–96）、合作式时间上限（默认300秒，最大600秒）。`token_rates`保留累计USD费用估算（默认0.50），每项不超过整批剩余额度，未知usage保留预留。`unpriced_proxy`不计算美元，但仍限制请求、时间和token。限制不证明供应商账单或代理账户额度已控制，也没有跨运行账本。

报告记录已运行及未运行案例、模型请求数、估算消耗、降级和预算原因。预算中断不伪装为全批完成；只有已完成的案例可进入人工 bundle。没有完成任何案例时，bundle 创建明确失败，原回放报告仍可诊断。退出码0为完成（不代表质量通过），1为预算受限、查询降级或 `--check` 工程不符，2为输入/文件错误。输出拒绝覆盖。

## 独立人工标注

Bundle 冻结问题、显式时钟、合成 Profile、整个语料、有效模型设置、查询限制、版本/摘要和实际回答证据，剔除工程候选期望。核对者既能检查被选择的原文，也能在全部九篇中找系统漏掉的相关项；不能仅以系统候选为相关集。

查询 item_id 是 `query:Q01` 等。由真人查看原文后填写完整相关集，再独立判断回答。以下只是语法示例，**未对当前输入预填人工标签**：

```sh
uv run --locked python deploy/human_review.py label \
  --bundle "$private_dir/query-model-bundle.json" --item-id query:Q01 \
  --reviewer '<核对者>' --at '<实际带时区的核对时间>' \
  --relevant-id uc-128291 --answer-assessment incomplete \
  --note '<实际依据>' --output "$private_dir/q01-label.json"
uv run --locked python deploy/human_review.py report \
  --bundle "$private_dir/query-model-bundle.json" --labels "$private_dir/q01-label.json"
```

`--relevant-id` 后不填成员表示明确确认相关集为空。仅暂存记录时使用 `--pending`，不得同时填相关集或回答判断；未标注、stale 和 pending 均不进入人工统计分母。

| 回答判断 | 核对口径 |
| --- | --- |
| supported | 回答与问题相关，条件/截止有正确原文依据，必要的未知和覆盖限制明确保留。 |
| unsupported | 至少一处关键判断缺乏依据或误用证据；原文存在该句不代表它回答了这个问题。 |
| incomplete | 已有回答含有用证据，但遗漏了必要条件、期限、相关通知或边界。 |
| not_answered | 没有形成可评价的回答，例如 local / fallback；这些状态不能标为 supported。 |

查询标签使用新增 `signalnest-human-label-v2`，包含 `answer_assessment`；规则/搜索的 v1 标签、旧 bundle 和旧统计继续兼容。标签仍绑定 bundle/input 摘要、核对者和实际时间；不认证核对者身份。更正保存新文件并显式选择，不自动取最新、不覆盖。别的输入/快照的标签列为 stale，冲突明确失败。

查询统计单列检索候选集合 Recall、最终回答通知集合 Recall、选择的不相关通知数量及四类回答判断。来源数据库分数不可直接比较，本轮不将 UC/CS 的拼接顺序当作统一排名来算 Recall@K。相关集为空单列，没有真人标签时率为 null。local 的候选召回可被核对，但没有模型回答；局部相关集上的这些统计不代表全校漏报率或模型总体质量。

输入校验核对回答与冻结语料的实例/通知身份、版本、内容摘要、Parser、原网址、发布日期和连续原文；拒绝未绑定结果或捏造引用。它验证文本与输入的一致性，不能证明某句的语义选择正确，也不能证明模型见过所有正文。实际交付仍只有已保存知识库、有限词法检索、引用和未知边界，不包含附件正文或实时校园信息。

本轮[实际回放和验证记录](validation/query-evaluation-20261012.md)明确列出零模型请求、九项 pending 与全量测试结果，没有将工程检查包装成人工质量验收。

后续查询 v2 修正截止引句的局部支持校验；v1/v2 完整快照和审核 bundle 作为本地私有证据保存在仓库外，仓库中的验证记录只保留汇总统计。v2 使用新文件名回放并生成独立 bundle，源码绑定新增 querying/evidence.py。旧 bundle 仍可核对，旧标签不会自动用于新输入；没有通过修改旧快照宣称 v1 已修复。

[v2实际验证](validation/query-deadline-v2-20261012.md)记录新的完整回放和九项独立pending统计；原v1记录保持。两版均尚无人工质量验收。
