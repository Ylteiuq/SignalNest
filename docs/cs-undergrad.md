# 计算机学院本科教学：离线适配节点

已完成离线列表/详情解析、决策回放、归档入库和重新解析验收。仓库只保留少量脱敏结构样本；选样方式见[说明](../research/selected-samples-20261010.md)。
**本节点尚未接入自动采集，也没有更新已部署实例的政策或发送邮件。**
生产联网来源仍是本科生院；不能用改 list_url、覆盖 source_id 或放宽主机替代第二来源接入。

## 样本与解析接口

测试使用仓库中选取的脱敏 HTML/JSON；随附 SHA-256 对应脱敏副本，不对应原始响应。

| 已选脱敏样本 | 提取结果 | 明确边界 |
| --- | --- | --- |
| cs-undergrad-list-home | 15 条，逻辑 1/4；下一页 bkjx/3.htm | 标题取 p，日期取 span，不用 a 的学院 title 属性 |
| cs-undergrad-list-page2 | 15 条，逻辑 2/4；下一页 bkjx/2.htm | 文件名 3 不是逻辑页码 3 |
| cs-undergrad-list-last | 13 条，逻辑 4/4；next=null | 下一页和尾页明确禁用；页长、旧日期不是终止证据 |
| cs-ta-recruit-64481 | 1074:64481；标题学年 2026–2027；站点日期 2026-07-13 | 保留正文落款 2025-07-10，不更正或当作站点日期 |

只保留第 1、2、4 页的脱敏样本，缺少第 3 页，因此 **43 个已观察身份不代表完整历史覆盖**。
离线入库不创建生产 run，也不推进完整扫描成功时间。

纯函数位于 `signalnest.cs_parsing`：

- `parse_cs_list(PageInput) -> ListPage`，沿用 entries、references、ordered_rows、next_page_url、PaginationEvidence。
- `parse_cs_notice(PageInput) -> ParsedNotice`，沿用 NoticeContent 与内容摘要。
- Parser 版本 `cs-undergrad-notices-v1`，独立来源标识 `whu-cs-undergrad-teaching`。

列表最终 URL 和所有分页链接只接受 HTTPS/443、cs.whu.edu.cn、`/xwdt/tzgg/bkjx.htm` 或其正整数 `.htm` 子页，不接受 query/fragment。
以实际最终 URL 解析相对链接；从数字声明、first/prev/next/last 控制相互印证页码和总数。
缺少或重复区域/当前页/按钮、隐藏标记、冲突活动状态、坏 URL、自链、数字目标冲突明确失败。
不推算静态路径，不硬编码总页数 4 或页长 15，也不从已知身份停止。
源码仅接受观察到的分页形状；数字缺口要求显式省略标记，无分页不是单页证据。

原生文章身份限栏目 1074 的 `/info/1074/<id>.htm` 与 `/content.jsp?...wbtreeid=1074&wbnewsid=<id>`，统一为 `1074:<id>`。
查询参数顺序/无关参数不改身份，但实际 URI 没有删参数或重排。
缺少必要身份、重复/冲突参数或损坏的已知路由仍使整页失败。
有效但未适配的外站、其他栏目、其他路径复用 PendingReference，不冒充文章或抓取任务；后续可整页去重登记。
**JSP 的身份回放只使用显式提供的静态正文，并未验证该路由匿名 HTTP 的权限或正文模板。**

详情限定唯一 article / `_newscontent_fromname` 表单、直接 h2.title / h4.information、div.content 内的唯一 #vsb_content / .v_news_content。
日期取可见“发布时间：YYYY-MM-DD”，忽略浏览量；错误页、隐藏必要字段、缺失/重复节点、无意义正文均失败，不能全页猜标题或日期。
沿用严格 UTF-8、显式 html.parser 和本科生院的保守正文清洗/空白/段落规则；不更改原 Parser 提取或摘要语义。
脚本、隐藏节点、动态计数和正文外导航不进入内容，段落、表格、日期、数值保留。
HTTP(S) a/img 引用变绝对，非 HTTP 的 href 去掉但保留文字；有意义的图片正文有效。

64481 **没有 HTML 链接、图片或附件节点**，腾讯文档标识、QQ群号和个人联系信息在脱敏副本中已移除；群文件只保留为文本事实，不伪造 a 或附件，也不请求第三方服务。
工程构造样本覆盖正文内 a/img 和 download.jsp 的 owner/wbfileid，保持 not_checked；没有真实样本证明其他 CS 附件区模板，不能宣称全部已支持。
HTML 保留不是安全清洗，不可直接信任并用于界面渲染。

复用 ParseError 的有限分类：invalid_utf8、empty_page、missing_structure、invalid_field、unsupported_identity、ambiguous_identity、empty_list、meaningless_body、invalid_pagination。
条目错误含零起始 item_index，不含 URL/HTML；整页失败，不静默丢行。

## 决策验收与政策 v7

实际回放暴露政策 v6 的两个缺口：`招募本科课程助教` 不在有限招聘词形中；“面向全院研究生”被作硬条件，而申请对象后文另有本科/博士/硕士/教师分支。
v7 仅补这个招聘词形和有限现在招募句、明确助教申请对象跨分号分支及其冲突、可追溯的附加条件与时间风险；保留跨主题优先级、菜单/历史/缴费排除。
没有按文章 ID 硬编码决策，也没有把本科教学课程当申请资格。

对不含真实院校、院系、专业和入学年份的合成本科档案回放 64481，得到：

- **DIGEST / needs_review / eligibility=unknown / time_status=unknown**，表示相关招聘需要核对。
- 高年级、已修申请课程且 85 分以上、全日制分支、教师/课程组考查同意、不能同时任两门课均有原文证据，不从入学年推认资格。
- 保留研究生声明与本科申请分支冲突，不能硬排本科，也不自动判硕士符合。
- “9月20日前”两个申请动作保存 year_missing；不从学年或发布日期补成确定截止，也不取期末考核时间。
- 标题学年与独立落款年份不一致，保存 recruitment_year_conflict 与两个原文位置；阻止开放确认和绝对时间/紧急截止推断。
- 腾讯文档及群文件保存 application_channel_unverified，岗位、申请表与当前补招未知。

这不是“当前仍可申请”的证明，也不是人工资格确认。薪酬条款的冲突保留在规范正文，当前不提取或承诺工资。
非紧迫的待核对信息仍可进普通摘要；只关注辅修时不因“不含辅修课程”产生辅修机会。
仅关注助教且明确把选课设为 store-only 的画像，对 128291 保持 STORE_ONLY/none；原 N1E05 的不同画像仍为 IGNORE/none。
真正科研课程的当天截止继续 PUSH_NOW，介绍现有助教不抢占主题。

CS 离线节点的政策当时为 facts/rules/decision v7；随后[原句漏报修复](validation/notification-original-misses-20261010.md)升为 v8，六情境 Action 不变。routing v1、CS Parser v1、WHU v4 与 EMS v1 不变。
今后选择器、身份、分页或正文清洗改变时更新受影响的 Parser 版本；共享正文 helper 的行为变化也须评估并更新 CS 版本。
原始 bytes SHA、NoticeContent 内容 SHA 和 Parser/政策版本职责不变，版本不加入内容摘要。
当前保证确定性及上述保守清洗，不保证任意语义相同 HTML 都产生相同摘要。
保留一条精简的 v6 决策序列化样本与当前汇总；不重复提交多版本逐例输出。
已有政策、决策、邮件资格和 frozen bytes 不静默重写。已有实例先预览真实样本，再按[维护入口](notification-maintenance.md)显式发布当前 v8 和新 operation ID；本次未在目标实例执行。

## 使用与复现

从仓库根目录运行，使用已锁定依赖；示例画像不是个人资格认证：

```sh
uv run --locked signalnest profile-check --profile profile.cs-undergrad.example.toml
uv run --locked signalnest decision-preview \
  --profile profile.cs-undergrad.example.toml --parser cs-undergrad-notices \
  --file research/fixtures/ta-entrypoints-20261009/cs-ta-recruit-64481.html \
  --url https://cs.whu.edu.cn/info/1074/64481.htm \
  --at 2026-10-10T09:00:00+08:00 --next-digest-at 2026-10-11T09:00:00+08:00
uv run --locked python deploy/evaluate_notifications.py --check \
  --cases docs/validation/cs-offline-cases.json
```

CS Parser 必须显式选择；默认 UC 行为保持，不按域名或文件名猜测，不在失败后换模板。
帮助/画像检查/预览不读取业务配置、获取写入锁、访问数据库或网络。
六个[固定离线输入情境](validation/cs-offline-cases.json)与[当前输出](validation/cs-offline-current.json)另列；工程期望不是人工 gold。
CS 脱敏样本的四个合成画像/时钟情境，加现有 UC 回归案例均可离线重放。
菜单、历史、缴费、纯研究生硬条件、改动落款、明确期限的合成边界单列测试，不充当新的真实正文样本。

列表的纯离线调用：

```python
import json
from pathlib import Path
from signalnest.contracts import PageInput
from signalnest.cs_parsing import parse_cs_list

folder = Path("research/fixtures/ta-entrypoints-20261009")
metadata = json.loads((folder / "cs-undergrad-list-home.json").read_bytes())
listing = parse_cs_list(PageInput(
    content=(folder / "cs-undergrad-list-home.html").read_bytes(),
    page_url=metadata["final_url"],
))
print(listing.row_count, listing.pagination, listing.next_page_url)
```

库调用者可复用 `import_page(..., list_parser=parse_cs_list, list_parser_version=PARSER_VERSION)`，详情传 `notice_parser=parse_cs_notice`，同源目标须已由列表登记。
使用独立 source_id、已初始化的隔离库，持 writer_lock，明确 processing_origin=offline；重解析用 maintenance。
ResponseInput 的 fetched_at 保留样本 JSON 已知获取完成时间的整数秒，另传本次 processed_at，不能伪造新抓取。
本轮离线验收不推定这些研究记录有生产缓存资格，未传 RequestProfile；每次导入登记独立响应，相同字节/版本幂等，重解析复用原证据。
CLI import-page/reparse 仍只用默认 UC Parser，没有增加不完整的第二来源写入模式。
纯 render_mail 可产生待核对 Digest 预览；未知项显示有数量上限，完整证据以 decision-preview/决策 JSON 和原文为准，不生成真实发送任务。

## 下一节点：自动采集的接入顺序

1. 明确单来源配置、run、通知通道与启用边界如何管理 CS；保留 UC 数据归属，不覆盖既有来源或承诺已支持多来源。
2. 显式绑定 CS 列表/详情 Parser 和版本；为本来源实现有限目标/重定向校验，不只放宽 UC 主机白名单。
3. 接真实归档/缓存/profile/业务事务，用模拟 HTTP 验证 CS 200/304、JSP 不可获取、坏模板、未适配引用、详情失败和重启待办。
4. 协调器仍须证明完整实际 URI 链、连续页码、稳定总数、全部行提交和首页复核；目前缺第 3 页，不能宣称 complete。
5. 隔离实例、邮件关闭下有限低频实采，确认当期正文和个人资格后再预览启用；不取腾讯文档、QQ 群文件或绕过登录。
