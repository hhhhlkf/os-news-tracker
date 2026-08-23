# systemd 社区讨论与 TPM2/FIDO2 信号调研

> 调研日期：2026-08-17<br>
> 范围：只采用 systemd 项目和 GitHub 的一手资料；回答 systemd 是否有可用于趋势研究的社区讨论，并给出一条近期、可直接查看的技术讨论。

## 结论

有。`systemd/systemd` 的主要讨论面不是单独的 GitHub Discussions 论坛，而是 **GitHub Issue → Pull Request/评审 → 合并提交** 这条可关联的工程讨论链；`systemd-devel` 邮件列表是一般技术问题的补充入口。

截至本次核查，仓库导航只有 Issues 和 Pull requests，未列出 Discussions；访问 [`/discussions`](https://github.com/systemd/systemd/discussions) 返回 404。因此不应把 GitHub Discussions 配置为该仓库的采集源，或将其空缺误判为社区低活跃。官方贡献指南明确规定：bug/RFE 只在 GitHub Issues 跟踪、代码改动走 Pull Request，普通 systemd 问题可投递 `systemd-devel`。[systemd Contributing](https://systemd.io/CONTRIBUTING/)

**推荐直接看的样本：** [PR #39570：`cryptenroll` 支持 `tpm2+fido2` 注册](https://github.com/systemd/systemd/pull/39570)。它在 2025-11-05 发起，当前仍打开，2026-08-11 仍有提交和评审；这是围绕 TPM2 与 FIDO2 联合解锁安全模型的长期、可读的工程设计讨论，而不只是一个缺陷报告。

## 可采集的讨论渠道

| 渠道 | 官方定位/现状 | 趋势采集方式 |
| --- | --- | --- |
| [GitHub Issues](https://github.com/systemd/systemd/issues) | 官方唯一的 bug 与 RFE 跟踪入口。 | 按 `updated` 增量拉取 Issue、评论、标签、关闭/重开事件；以 Issue 号作为问题事件 ID。 |
| [GitHub Pull Requests](https://github.com/systemd/systemd/pulls) | 官方代码提交和评审入口；开放且长期活跃的 PR 也是设计讨论。 | 拉取 PR、review、review comment、检查状态、提交和 merge 时间；保留 Issue 引用，将其合成为一个事件链。 |
| [`systemd-devel`](https://lists.freedesktop.org/mailman/listinfo/systemd-devel) | 官方贡献指南指定的一般 systemd 问题入口，而非 bug/RFE 主跟踪器。 | 订阅后经 IMAP/Webhook 摄取；按 `Message-ID`、`In-Reply-To` 聚合线程，作为早期讨论或背景证据。 |
| GitHub Discussions | 该仓库当前未启用（导航无此项，`/discussions` 为 404）。 | 不配置此源；若以后启用，再纳入辅助讨论流。 |

项目安全页还说明 Issue tracker 与 `systemd-devel` 都是完全公开的；敏感漏洞不能在这两处讨论。[systemd Security](https://systemd.io/SECURITY/)

GitHub 的 Issue REST 接口支持按 `updated`、`since`、标签和分页读取仓库条目；但 Issue 列表会混入 PR，必须根据 `pull_request` 字段排除，再单独用 Pull Request API 获取实现和评审事件。[GitHub Issues REST 文档](https://docs.github.com/en/rest/issues/issues) [GitHub Pull Requests REST 文档](https://docs.github.com/en/rest/pulls/pulls)

## 样本：TPM2 + FIDO2 的联合 LUKS 解锁

### 可直接查看的讨论

[PR #39570](https://github.com/systemd/systemd/pull/39570) 提议让 `systemd-cryptenroll` 支持 `tpm2+fido2` 注册。其跨越 `cryptsetup`、`tpm2`、`creds`、`repart` 等组件标签，首次提交是“验证如何支持 TPM2 + FIDO2 注册”的早期草案；截至 2026-08-11，作者仍在推送实现与接受评审，说明这一主题仍在持续演进，而不是一次性提案。

核心设计分歧很有研究价值：

1. 初版将 FIDO2 秘密按普通密码处理，但维护者 Lennart Poettering 指出这会使主机一旦获知该秘密就永久破坏安全性；他主张由 TPM2 校验 FIDO2 设备对挑战的签名，避免把秘密暴露给主机。[PR #39570，2025-11-07 讨论](https://github.com/systemd/systemd/pull/39570)
2. 作者随后把方案细化为由 FIDO2 创建凭据、在 LUKS2 header 保存必要元数据、由 FIDO2 对 nonce 作断言、再以 `TPM2_PolicySigned` 验证签名的路径。[PR #39570，2025-11-13 讨论](https://github.com/systemd/systemd/pull/39570)
3. 2026-04 的后续验证发现，标准 TPM2 的 `TPM2_PolicySigned` 待签数据与 FIDO2 的签名输入无法直接相同；讨论因而回到 `hmac-secret` 扩展这个可行路径。2026-08 的提交说明实现选择为“用 `hmac-secret` 扩展 TPM2 PIN 授权策略”。[PR #39570，2026-04/08 更新](https://github.com/systemd/systemd/pull/39570)

### 信号判断与边界

该讨论说明 systemd 正在推进**把硬件根信任（TPM2）与用户持有因素（FIDO2）组合到 LUKS 解锁**，并且安全模型优先避免把长期秘密泄露给主机。它可作为“Linux 启动/磁盘解锁向多因素、硬件绑定凭据演进”的强候选信号：存在长期实现、维护者安全设计反馈、反复重构和近期活动。

但一条 PR 本身不等于已落地趋势：它仍是开放状态，不能写成 systemd 已发布该能力。应在后续观察到合并、release note、发行版集成或更多相关 Issue/PR 后，再将其升级为已验证趋势。

## 对 OS News Tracker 的启示

- systemd 应优先采集 GitHub Issues、PR、review/comment、commit 与 release；邮件列表仅补充背景和未进入 issue 的技术讨论。
- 此样本应以 PR #39570 为一个事件，提交/评审/状态变化是同一事件的时间线，不能逐条计为新闻。
- 建议抽取：`组件=systemd-cryptenroll/cryptsetup`、`技术=TPM2+FIDO2`、`机制=hmac-secret/TPM2 policy`、`目标=LUKS 多因素解锁`、`状态=open/under-review`。以后结合独立项目或发行版证据验证动量、扩散和持续性。
