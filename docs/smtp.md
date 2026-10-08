# N3 SMTP 适配器

2026-10-07。本批实现标准库同步 SMTP 适配器，消费 N2 已冻结的单收件人邮件，返回有限发送观察。适配器自身没有重试或数据库操作；发送 CLI、持久尝试/due、暂停与恢复已由 [N4](mail-sending.md)另行实现；crawl-once、mail-plan、帮助和 config-check 均不调用它；后续 [后台入口](background-mail.md) 在显式启用后调用 N4 发送。没有进行外部邮箱投递。

## 配置与调用边界

普通配置可省略 smtp，旧配置不变。配置检查只验证结构，不读凭据或测试 SMTP 连接。示例默认注释禁用；明确选择服务后才能填写：

```toml
[smtp]
host = "smtp.example.org"       # 示例，不是实际服务
port = 587                     # 必填，不根据模式暗猜端口
security = "starttls"          # 或 "tls"（隐式 TLS），禁止明文
timeout_seconds = 30.0
username_env = "SIGNALNEST_SMTP_USERNAME"
password_env = "SIGNALNEST_SMTP_PASSWORD"
```

host 为 ASCII DNS 名或 IP 地址，不接受 URL、凭据、路径、带作用域的 IPv6 或控制字符。timeout_seconds 默认 30 秒，范围为大于 0、不超过 120 的有限数。username_env/password_env 必须成对给出合法环境变量名；都省略时可以使用明确配置的 TLS relay，不发送 AUTH。地址不从此处读取，始终使用冻结邮件的 sender/recipient。

仅显式 `send_frozen(message: FrozenMessage, settings: SmtpSettings, environ=None) -> SendResult` 会读环境值及建立连接。通常由 `load_frozen_mail` 提供已验证邮件；发送前再次校验 payload SHA、大小（最多 1 MiB）、CRLF/ASCII 编码及单一 From/To/Subject/Message-ID、UTF-8 纯文本 MIME 和冻结地址一致性。它不读数据库、原始 HTML 或当前 Profile，不重新渲染、生成 Date 或 Message-ID。SMTP 的点转义只是传输封装，服务端还原后的 bytes 与冻结 bytes 一致。

没有可直接执行的示例发送脚本。以下持久步骤现由 N4 实现，N3 适配器保持独立：

1. 持有同一实例写入锁，加载并校验已冻结邮件；结束读事务。
2. 短事务持久登记 sending/attempt，提交失败则绝不调用 SMTP。
3. 事务外调用 send_frozen，保存它的有限结果。
4. 短事务登记接受、拒绝或未知；结果写入失败立即停止后续发送。

当前手动调用适配器不会改变 mail_messages.pending；再次调用仍会发送一次，没有内部去重。不能把 N2 的 pending 直接当成可安全 drain 的状态。

## 有限结果

不可变 `SendResult` 只包含 outcome、stage、error_code、可选实际 smtp_code、scope（message/channel）与 cleanup_failed，不保存任意服务器文本、异常字符串、邮件或凭据。scope 帮助 N4 区分单封邮件问题和连接/TLS/认证配置问题，适配器自身不暂停通道或安排重试。

| outcome | 证据与含义 |
| --- | --- |
| accepted | DATA 之后实际读到最终 250；stage=accepted，无失败代码。只表示 SMTP 服务接受，不表示收件箱送达。 |
| retryable | DATA 之前的连接/超时/断线、明确 4xx 或可重新尝试的协议错误；不代表本批已经执行重试。 |
| uncertain | 进入 SMTP.data 调用后，未取得可信最终回复的断线、超时、TLS 读写失败或异常回复；服务可能已接受，重复尝试可能重复投递。 |
| permanent | 明确 5xx、损坏冻结输入、缺失/不支持的凭据或 TLS/认证配置问题；不改变通知正文的成功状态。 |

stage 为 config/connect/hello/tls/auth/mail/rcpt/body_or_final/accepted。错误代码固定在 SendErrorCode：invalid_frozen_mail、credentials_missing/invalid、tls_verification_failed/not_supported/failed、auth_not_supported/mechanism_not_supported、authentication_rejected、server_rejected、connection_failed、timeout、disconnected、protocol_error。

SMTP.data 同时封装 354 握手和正文发送/最终回复；进入调用后断线即保守归 uncertain，即使模拟服务证明正文尚未发送。只有明确的初始或最终 4xx/5xx 拒绝可归确定失败。最终异常 2xx/1xx 或损坏回复不会伪装 accepted。标准库可能为过长回复本地生成 SMTPResponseException(500)，不能把它当作真实服务端拒绝；DATA 后为 uncertain。STARTTLS 此类 500 无法区分真实拒绝与本地合成，保守返回 protocol_error/retryable 且不保存伪造 smtp_code。

这些取舍基于本地 **CPython 3.12.14** 源码，以及 [Python SMTP 接口](https://docs.python.org/3.12/library/smtplib.html)、[最终接受与失败回复](https://www.rfc-editor.org/rfc/rfc5321.html#section-4.2.5)。Message-ID 不提供一般 SMTP 外部去重保证。

## 连接、认证与清理

使用 ssl.create_default_context，保留 CERT_REQUIRED、hostname 校验和 SNI；支持 SMTP_SSL 或强制 STARTTLS，升级后重新 EHLO，不降级明文。本实现要求 ESMTP，不回退 HELO。先构造无连接对象再连接，保留清理所有权；CPython 3.12 的 connect 不保存 TLS 目标，显式设置 _host，两个模式均用真实本地握手测试校验了正确主机名。

凭据在每次显式调用时读取一次，只保留于内存；值必须是非空、最多 4096 字符的可打印 ASCII，不在 TOML 存密码。缺值/不支持的字符返回有限配置错误，不发请求。当前不支持 Unicode SASL 凭据、OAuth2 或客户端证书配置。只在验证过的 TLS 中认证，根据服务广告按 CRAM-MD5/PLAIN/LOGIN 顺序选择 **一个**机制；拒绝后不切换机制，不使用 login 内部的多机制重试。认证结束清空 client.user/password，不承诺 Python 能彻底擦除内存副本。

显式执行 MAIL → 单个 RCPT → DATA，不用 sendmail/send_message，避免标准库 sendmail 在明确拒绝后的 RSET 清理掩盖原结果。没有连接、邮件或认证重试层。debuglevel 固定为 0；适配器不配置 logger 或输出正文、凭据、原始回复。

失败和 uncertain 直接关闭，不再发 QUIT。最终 250 后可以 QUIT；拒绝、断线或 close 错误只设置 cleanup_failed，不能反转 accepted。预期 SMTP/socket/SSL 错误转换为有限结果；程序缺陷传播，连接仍在 finally 清理，不用宽泛异常捕获将缺陷伪装成功。

timeout_seconds 是阻塞 socket 操作的超时，不是整次发送或 DNS 的硬墙钟期限；SMTP 读超时可能被标准库包装为断线，保留有限 TIMEOUT 分类。较短最终确认等待可能增加 uncertain；本默认值是应用取舍，实际服务行为未验证，不能宣称符合所有服务的确认时间。RFC 给出了更长的 [DATA 结束等待建议](https://www.rfc-editor.org/rfc/rfc5321.html#section-4.5.3.2.6)。

## 验证范围与后续

默认测试不连接公网：模拟服务通过 socketpair 驱动真实 smtplib 协议方法，另以真实 SSLContext 和本地测试证书完成 STARTTLS/隐式 TLS 握手、信任和 hostname 校验。测试证书/私钥是公开的 test-only fixture，不能部署使用；没有真实账号或密码进入仓库。部分协议故障测试使用透明 TLS wrapper，只验证协议顺序，这些不能称为握手测试。

N4 已新增发送尝试和状态迁移，完成网络前后登记、持久 due/有限重试、认证阻断、人工重试、遗留 sending 恢复与真实子进程终止测试。SMTP 接受和本地结果提交不能原子完成；接受后本地提交失败仍有不确定窗口。未验证公网服务兼容、退信、垃圾箱或收件箱送达，未进行新进程终止或断电实验。


2026-10-07 N3 验证：macOS 26.6.2 arm64、CPython 3.12.14、OpenSSL 3.5.8、SQLite 3.53.1，依赖和锁文件不变。全套 **1237 项测试通过**（原有 N2 的 1112 项，新增配置 54、适配器/契约 36、实际 SMTP 协议 29、真实本地 TLS 6）。Ruff、格式检查（80 个 Python 文件）和 git diff --check 通过。

验证连接失败、单机制认证拒绝、MAIL/RCPT/DATA 明确 4xx/5xx、正文保存后断线/超时、异常/过长最终回复、最终 250 后 QUIT/close 失败、凭据缺失/非法、损坏冻结输入、无重渲染及有限结果。真实 N2 归档/Parser/SQLite 冻结邮件也通过本地服务的精确字节验证。两种 TLS 的真实握手与证书/hostname 拒绝均通过，失败不发送 AUTH/MAIL；另有透明 wrapper 和故障注入测试，明确区分验证层级。

从不同工作目录执行安装入口的帮助和带 SMTP 的配置检查通过，无凭据仍可校验，不创建存储；模块导入和配置加载不读凭据、不构造 SMTP 或 SSL context。70 份已跟踪的 research/fixture、Parser、原契约/N0、0001–0004 与依赖文件逐字节未变；保留 N2 未提交工作，N3 未新增数据库迁移。没有外部 SMTP/校园请求、真实账号投递或新终止/断电实验，没有提交、推送或 PR。N3 本批完成，N4 发送登记、诊断与恢复仍待交付。
