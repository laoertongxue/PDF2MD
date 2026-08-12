# M8 1.0.0 商业发布实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将已通过 Beta 准入的 PDF2MD 1.0.0 发布到中国大陆下载渠道和 GitHub 镜像，完成双语说明、支持、状态、退款/发票、分阶段放量和发布后回退保障。

**架构：** 同一组经过签名验证的不可变资产先进入 Aliyun OSS 私有 staging，经发布协调器校验后复制到 CDN origin 并同步 GitHub Release。stable 更新清单按 5%、25%、100% 推进；运营控制面提供状态、支持和暂停开关，发布团队按 runbook 执行。

**技术栈：** GitHub Actions、Aliyun OSS/CDN、Tauri Updater、GitHub Release、SPDX、SHA-256、OpenTelemetry、状态页、双语 Markdown、pytest。

---

## 文件清单

**创建：**

- `scripts/validate-launch-gate.py`：1.0.0 全部准入收据验证。
- `scripts/publish-cn-release.sh`：OSS staging、校验和、CDN 发布和缓存预热。
- `scripts/publish-github-mirror.sh`：同哈希 GitHub Release 镜像。
- `scripts/promote-stable-rollout.py`：stable 5/25/100 放量。
- `scripts/run-incident-drill.py`：Provider 故障与删除 SLA 演练器。
- `scripts/validate-post-launch-report.py`：7 天观察与 RPO/RTO 报告验证。
- `.github/workflows/commercial-release.yml`：环境保护的正式发布流程。
- `config/release/channels.json`：stable/beta 渠道、所需环境变量和保留策略。
- `tests/test_launch_gate.py`、`test_release_publication.py`、`test_stable_rollout.py`：发布测试。
- `tests/test_incident_drill.py`、`test_post_launch_report.py`：事故与发布后报告测试。
- `docs/releases/1.0.0-zh-CN.md`、`1.0.0-en-US.md`：正式更新说明。
- `docs/operations/launch-checklist.md`、`support-sla.md`、`refund-policy.md`、`invoice-process.md`：运营文档。
- `docs/operations/post-launch-report.md`：发布后观察报告。

**修改：**

- `.github/workflows/release.yml`：只保留兼容入口或转调 commercial workflow。
- `README.md`、`README_EN.md`、`CHANGELOG.md`：正式下载与校验说明。
- `services/control-plane/src/pdf2md_cloud/operations/routes.py`：公开状态摘要和内部发布暂停开关。
- `services/control-plane/src/pdf2md_cloud/billing/routes.py`：退款与发票工单状态。
- `infra/terraform/cn/`：下载域名、CDN、状态页、告警和 WAF。

## 任务 1：建立不可绕过的 1.0.0 准入验证器

**文件：**
- 创建：`scripts/validate-launch-gate.py`
- 创建：`tests/test_launch_gate.py`
- 创建：`config/release/channels.json`

- [ ] **步骤 1：编写失败的完整收据测试**

```python
def test_launch_gate_rejects_missing_legal_receipt(tmp_path):
    bundle = complete_gate_bundle(tmp_path)
    bundle.pop("legal_approval")
    result = validate_gate(bundle, version="1.0.0")
    assert result.ok is False
    assert result.errors == ["gate.legal_approval_missing"]
```

验证器必须检查 M0-M7 提交/测试收据、两架构签名资产、30 天 Beta、私有教材指标、性能、安全、数据删除、账本对账、法务、商标/版权、ICP备案或适用资质、支持和回退演练。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_launch_gate.py -q`

预期：FAIL，验证器不存在。

- [ ] **步骤 3：实现签名收据 Schema 和验证**

每份收据包含 kind、version、commit、issued_at、expires_at、artifact hashes、issuer 和 Ed25519 signature。验证器拒绝过期、版本不一致、非 `main` 提交、资产哈希不一致和缺失项；输出机器 JSON 和人类 Markdown 两份报告。

- [ ] **步骤 4：验证失败矩阵与完整 bundle**

运行：`uv run pytest tests/test_launch_gate.py -q`

预期：篡改、过期、缺失、错误版本均拒绝；完整 bundle 通过。

- [ ] **步骤 5：Commit**

```bash
git add scripts/validate-launch-gate.py tests/test_launch_gate.py config/release/channels.json
git commit -m "feat: enforce commercial launch receipts"
```

## 任务 2：发布同哈希资产到中国区 CDN 与 GitHub 镜像

**文件：**
- 创建：`scripts/publish-cn-release.sh`
- 创建：`scripts/publish-github-mirror.sh`
- 创建：`tests/test_release_publication.py`
- 创建：`.github/workflows/commercial-release.yml`
- 修改：`infra/terraform/cn/`
- 修改：`.github/workflows/release.yml`

- [ ] **步骤 1：编写失败的不可变发布测试**

测试 staging 与 public 资产集必须精确包含两架构 DMG/app.zip、更新签名、SHA-256、SPDX SBOM、第三方通知、双语说明和 provenance；OSS 与 GitHub 下载后的 SHA 必须与准入 bundle 相同；目标已存在但哈希不同则拒绝覆盖。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_release_publication.py -q`

预期：FAIL，发布脚本不存在。

- [ ] **步骤 3：实现环境保护发布流程**

workflow 只接受 `main` 上的 `v1.0.0` 标签，先下载同一构建 run 的已验证资产，运行 launch gate，再上传 OSS staging。人工审批生产环境后，以 server-side copy 发布不可变版本路径，生成 stable manifest，预热 CDN，最后创建 GitHub Release 镜像。所需变量为 `CN_DOWNLOAD_BASE_URL`、`ALIYUN_OSS_BUCKET`、`ALIYUN_CDN_DISTRIBUTION`，凭据使用 GitHub OIDC 短期角色。

- [ ] **步骤 4：验证下载、缓存和 GitHub 镜像**

运行：

```bash
uv run pytest tests/test_release_publication.py -q
./scripts/publish-cn-release.sh --dry-run build/launch-gate.json build/release-assets
./scripts/publish-github-mirror.sh --dry-run v1.0.0 build/release-assets
```

预期：dry-run 输出完整不可变对象计划，无上传；正式执行后两渠道下载哈希一致。

- [ ] **步骤 5：Commit**

```bash
git add scripts/publish-cn-release.sh scripts/publish-github-mirror.sh tests/test_release_publication.py .github/workflows/commercial-release.yml .github/workflows/release.yml infra/terraform/cn
git commit -m "ci: publish verified commercial assets"
```

## 任务 3：完成双语 Release Notes、下载说明和校验体验

**文件：**
- 创建：`docs/releases/1.0.0-zh-CN.md`
- 创建：`docs/releases/1.0.0-en-US.md`
- 修改：`README.md`
- 修改：`README_EN.md`
- 修改：`CHANGELOG.md`
- 创建：`tests/test_release_docs.py`

- [ ] **步骤 1：编写失败的发布文档测试**

测试中英文说明均包含产品定位、支持格式、双教材章节、OCR 质量策略、精读结构、Mermaid、Provider 配置、隐私、AI 标识、系统要求、两架构下载、SHA 验证、更新/回退、已知限制、支持和安全邮箱；版本和文件名与资产清单一致。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_release_docs.py -q`

预期：FAIL，1.0.0 文档不存在。

- [ ] **步骤 3：撰写双语说明和 README 下载区**

中文和英文结构对等但不是机械直译；不宣称未通过 E2E 的格式或 Provider。README 下载链接指向版本化 CDN 和 GitHub 镜像，展示 SHA 验证命令：

```bash
shasum -a 256 -c PDF2MD_1.0.0_aarch64.dmg.sha256
```

- [ ] **步骤 4：验证链接和版本一致性**

运行：`uv run pytest tests/test_release_docs.py tests/test_version_consistency.py -q`

预期：全部通过，所有下载链接返回目标资产且哈希一致。

- [ ] **步骤 5：Commit**

```bash
git add docs/releases/1.0.0-zh-CN.md docs/releases/1.0.0-en-US.md README.md README_EN.md CHANGELOG.md tests/test_release_docs.py
git commit -m "docs: publish bilingual 1.0.0 notes"
```

## 任务 4：上线支持、状态、退款、发票和事故响应

**文件：**
- 创建：`docs/operations/launch-checklist.md`
- 创建：`docs/operations/support-sla.md`
- 创建：`docs/operations/refund-policy.md`
- 创建：`docs/operations/invoice-process.md`
- 创建：`services/control-plane/tests/operations/test_public_status.py`
- 创建：`services/control-plane/tests/operations/test_support_workflow.py`
- 创建：`scripts/run-incident-drill.py`
- 创建：`tests/test_incident_drill.py`
- 修改：`services/control-plane/src/pdf2md_cloud/operations/routes.py`
- 修改：`services/control-plane/src/pdf2md_cloud/billing/routes.py`
- 修改：`infra/terraform/cn/`

- [ ] **步骤 1：编写失败的运营流程测试**

测试公开状态只展示聚合健康和事件，不泄漏 Provider、用户或区域内部信息；支持诊断包必须用户确认；退款请求绑定账本条目；发票工单最小化信息；P0/P1 事件自动通知值班并可暂停 stable manifest。演练器必须验证告警、状态、暂停、通知、恢复和复盘六个阶段，缺少任一阶段即失败。

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
(cd services/control-plane && uv run pytest tests/operations -q)
uv run pytest tests/test_incident_drill.py -q
```

预期：FAIL，流程或 API 不完整。

- [ ] **步骤 3：实现状态和工单状态机**

状态事件为 `INVESTIGATING/IDENTIFIED/MONITORING/RESOLVED`；支持工单为 `OPEN/NEEDS_USER/IN_PROGRESS/RESOLVED/CLOSED`；退款和发票沿用账本/支付状态，不创建第二套金额事实源。所有策略文档中英文可访问，安全邮箱和投诉渠道可实际收件。

- [ ] **步骤 4：执行演练**

运行：

```bash
(cd services/control-plane && uv run pytest tests/operations tests/billing -q)
uv run pytest tests/test_incident_drill.py -q
uv run python scripts/run-incident-drill.py --scenario provider-outage
uv run python scripts/run-incident-drill.py --scenario deletion-sla
```

预期：告警、状态更新、暂停更新、用户通知、恢复和复盘完整闭环。

- [ ] **步骤 5：Commit**

```bash
git add docs/operations services/control-plane/src/pdf2md_cloud/operations/routes.py services/control-plane/src/pdf2md_cloud/billing/routes.py services/control-plane/tests/operations scripts/run-incident-drill.py tests/test_incident_drill.py infra/terraform/cn
git commit -m "feat: operate commercial support workflows"
```

## 任务 5：执行 stable 5%、25%、100% 分阶段放量

**文件：**
- 创建：`scripts/promote-stable-rollout.py`
- 创建：`tests/test_stable_rollout.py`
- 修改：`services/control-plane/src/pdf2md_cloud/operations/routes.py`

- [ ] **步骤 1：编写失败的 stable 放量测试**

测试 5% 至少观察 24 小时、25% 至少观察 48 小时后才可 100%；crash-free 低于 99.5%、核心任务成功率低于 98%、OCR 误接受、删除失败、支付差异、P0/P1 事件任一越界即拒绝推进并建议暂停；分桶对同一设备稳定。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_stable_rollout.py -q`

预期：FAIL，推进工具不存在。

- [ ] **步骤 3：实现签名推进命令**

命令读取控制面签名指标收据、当前 stable 清单和目标百分比，验证观察时长、版本、资产和阈值后生成新清单；生产写入需要 GitHub Environment 审批。任何操作生成审计 ID 和前后清单哈希。

- [ ] **步骤 4：执行三个阶段**

运行：

```bash
uv run python scripts/promote-stable-rollout.py --version 1.0.0 --to 5 --receipt build/stable-gate.json
uv run python scripts/promote-stable-rollout.py --version 1.0.0 --to 25 --receipt build/stable-gate.json
uv run python scripts/promote-stable-rollout.py --version 1.0.0 --to 100 --receipt build/stable-gate.json
```

预期：只有满足观察窗口和指标的阶段成功；每次 CDN stable manifest 哈希和审计记录一致。

- [ ] **步骤 5：Commit**

```bash
git add scripts/promote-stable-rollout.py tests/test_stable_rollout.py services/control-plane/src/pdf2md_cloud/operations/routes.py
git commit -m "feat: stage stable commercial rollout"
```

## 任务 6：发布后观察、回退演练和 1.0.0 关闭报告

**文件：**
- 创建：`scripts/validate-post-launch-report.py`
- 创建：`tests/test_post_launch_report.py`
- 创建：`docs/operations/post-launch-report.md`
- 修改：`docs/operations/launch-checklist.md`
- 修改：`docs/release/ROLLBACK_RUNBOOK.md`

- [ ] **步骤 1：编写失败的发布后报告测试**

测试 6 天、日期中断、RPO 超过 5 分钟、RTO 超过 1 小时、crash-free 低于 99.5%、核心任务成功率低于 98% 和缺少回退收据均拒绝关闭发布。

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_post_launch_report.py -q`

预期：FAIL，报告验证器不存在。

- [ ] **步骤 3：实现验证器并在 100% 后连续观察 7 天**

每日记录 crash-free、任务成功率、两本教材回归、OCR/精读/Mermaid 指标、Provider 可用性和成本、网关延迟、删除 SLA、支付对账、支持量、安全和版权投诉。数据只使用聚合和脱敏值。

- [ ] **步骤 4：执行生产等价回退演练并运行关闭验证**

将测试渠道 stable 清单切回 N-1，验证客户端更新策略、数据库兼容、云端 API 兼容和用户数据不丢失，再恢复 1.0.0；记录每步时间，RTO 必须不超过 1 小时。

运行：

```bash
./scripts/verify-release.sh
uv run python scripts/validate-launch-gate.py build/launch-receipts --version 1.0.0
uv run python scripts/validate-post-launch-report.py docs/operations/post-launch-report.md
```

预期：全部通过，7 天收据完整，RPO 不超过 5 分钟，RTO 不超过 1 小时。

- [ ] **步骤 5：确认远程发布状态并 Commit**

运行：

```bash
git fetch --prune
test "$(git branch --format='%(refname:short)')" = "main"
test "$(git branch -r --format='%(refname:short)' | grep -v 'origin/HEAD')" = "origin/main"
gh release view v1.0.0 --json isDraft,isPrerelease,assets
```

预期：本地和远程只保留 `main`；Release 非草稿、非预发布，资产集完整。

```bash
git add scripts/validate-post-launch-report.py tests/test_post_launch_report.py docs/operations/post-launch-report.md docs/operations/launch-checklist.md docs/release/ROLLBACK_RUNBOOK.md
git commit -m "docs: close 1.0.0 commercial launch"
git push origin main
```

## M8 完成收据

- [ ] 1.0.0 准入 bundle 完整、签名有效且对应 `main`。
- [ ] 中国区 CDN 与 GitHub Release 的两架构资产、SBOM 和校验和完全一致。
- [ ] 双语 Release Notes、隐私、AI 标识、退款、发票、支持和安全渠道可用。
- [ ] stable 5%、25%、100% 每阶段满足观察窗口和质量阈值。
- [ ] 发布后 7 天无阻断事故，回退演练达到 RPO/RTO。
- [ ] 本地与远程普通分支最终仍只有 `main`。
