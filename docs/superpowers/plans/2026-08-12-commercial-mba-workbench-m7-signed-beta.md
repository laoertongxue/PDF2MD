# M7 签名 Beta 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 交付 Apple Silicon 与 Intel 正式签名、公证、可自动更新的 Beta 客户端，并用私有真实教材、性能、安全和 30 天受控运行证明商业准入。

**架构：** 每个架构在原生 macOS Runner 独立构建和签名，发布协调 Job 验证两套资产、SBOM、来源证明和更新签名后生成 Beta 清单。Updater 在迁移前备份数据库，支持 beta/stable 渠道、分阶段放量、暂停和回退。

**技术栈：** Tauri v2 Updater、Developer ID Application、Hardened Runtime、Apple notarytool/stapler、GitHub Actions、Syft/SPDX、cosign/attestation、WebdriverIO、私有 macOS Runner、pytest-benchmark。

---

## 文件清单

**创建：**

- `parsing-core-app/src-tauri/src/updater.rs`：更新检查、下载、迁移备份和安装状态。
- `parsing-core-app/src-tauri/entitlements.plist`：最小 hardened runtime 权限。
- `scripts/sign-macos-app.sh`：嵌套二进制签名顺序。
- `scripts/notarize-macos-app.sh`：提交、公证、staple 和验证。
- `scripts/verify-macos-release.sh`：架构、签名、公证、Sidecar、版本、恶意篡改验证。
- `scripts/generate-sbom.sh`：SPDX SBOM 和许可证清单。
- `scripts/build-update-manifest.py`：stable/beta 更新清单和 rollout 百分比。
- `scripts/verify-release.sh`：发布候选统一门禁。
- `.github/workflows/private-textbook-regression.yml`：每日/每周私有教材回归。
- `.github/workflows/beta-release.yml`：双架构 Beta 构建与发布。
- `tests/test_updater_manifest.py`：更新清单和签名契约。
- `tests/test_release_security.py`：Action SHA、Secret、产物和签名规则。
- `parsing-core-app/tests/e2e/update.spec.ts`：N-1 更新和回退 E2E。
- `docs/release/BETA_RUNBOOK.md`、`ROLLBACK_RUNBOOK.md`：Beta 和回退手册。
- `docs/releases/templates/beta-zh-CN.md`、`beta-en-US.md`：双语说明模板。
- `docs/beta/metrics-schema.json`、`acceptance-report.md`：脱敏指标契约和准入报告。
- `scripts/validate-beta-report.py`：验证 30 个连续日收据和商业阈值。
- `tests/test_beta_report.py`：Beta 报告缺失、篡改和阈值测试。

**修改：**

- `parsing-core-app/package.json`、`package-lock.json`：Updater 插件和 E2E 脚本。
- `parsing-core-app/src-tauri/Cargo.toml`、`Cargo.lock`、`src/main.rs`、`tauri.conf.json`、`capabilities/main.json`：Updater、签名和最小权限。
- `.github/workflows/release.yml`：完整 SHA、双架构、签名、公证、SBOM 和来源证明。
- `scripts/check-release-sidecar.sh`、`scripts/test-release-sidecar.sh`：嵌套运行时深度验证。
- `CHANGELOG.md`、`README.md`、`README_EN.md`：Beta 下载和安全说明。

## 任务 1：实现更新清单、数据库备份和失败恢复

**文件：**
- 创建：`parsing-core-app/src-tauri/src/updater.rs`
- 创建：`scripts/build-update-manifest.py`
- 创建：`tests/test_updater_manifest.py`
- 创建：`parsing-core-app/tests/e2e/update.spec.ts`
- 修改：`parsing-core-app/src-tauri/Cargo.toml`
- 修改：`parsing-core-app/src-tauri/Cargo.lock`
- 修改：`parsing-core-app/src-tauri/src/main.rs`
- 修改：`parsing-core-app/src-tauri/tauri.conf.json`

- [ ] **步骤 1：编写失败的清单与迁移备份测试**

```python
def test_update_manifest_requires_both_architectures_and_rollout(tmp_path):
    manifest = build_manifest(release_fixture(tmp_path), channel="beta", rollout=5)
    assert set(manifest["platforms"]) == {"darwin-aarch64", "darwin-x86_64"}
    assert manifest["rollout"] == 5
    assert all(item["signature"] for item in manifest["platforms"].values())


def test_rollout_must_be_allowed_step():
    with pytest.raises(ValueError, match="release.invalid_rollout"):
        build_manifest(release_fixture(), channel="beta", rollout=17)
```

Updater E2E 断言安装 N-1 工作区后更新成功，Migration 前生成带哈希备份；Migration 或启动失败时恢复备份并保留旧 App。

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/test_updater_manifest.py -q
cd parsing-core-app && npm run test:e2e -- update.spec.ts
```

预期：FAIL，清单和 Updater 不存在。

- [ ] **步骤 3：实现 stable/beta 更新协议**

Tauri 配置 `createUpdaterArtifacts: true`，公钥内置，endpoint 从签名发行配置读取且只允许 HTTPS。客户端以安装 ID 哈希稳定分桶，只接受 rollout 0/5/25/100；更新前关闭 Worker、checkpoint、备份 SQLite 和 Migration 元数据，成功启动后才清理备份。

- [ ] **步骤 4：验证篡改、N-1 和回退**

运行：

```bash
uv run pytest tests/test_updater_manifest.py -q
cd parsing-core-app && npm run test:e2e -- update.spec.ts
```

预期：签名错误、架构错误、降级攻击和损坏下载均拒绝；N-1 更新与失败恢复通过。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src-tauri/src/updater.rs parsing-core-app/src-tauri/src/main.rs parsing-core-app/src-tauri/Cargo.toml parsing-core-app/src-tauri/Cargo.lock parsing-core-app/src-tauri/tauri.conf.json scripts/build-update-manifest.py tests/test_updater_manifest.py parsing-core-app/tests/e2e/update.spec.ts
git commit -m "feat: add signed staged updates"
```

## 任务 2：建立 Developer ID 嵌套签名和公证脚本

**文件：**
- 创建：`parsing-core-app/src-tauri/entitlements.plist`
- 创建：`scripts/sign-macos-app.sh`
- 创建：`scripts/notarize-macos-app.sh`
- 创建：`scripts/verify-macos-release.sh`
- 创建：`tests/test_release_security.py`
- 修改：`parsing-core-app/src-tauri/tauri.conf.json`

- [ ] **步骤 1：编写失败的脚本契约测试**

测试检查签名脚本必须先签动态库/helper/Python/主二进制，再签 `.app`；必须使用 `--options runtime --timestamp`；公证脚本必须调用 `notarytool submit --wait`、`stapler staple`、`stapler validate` 和 `spctl --assess`；禁止 `codesign -s -`。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_release_security.py -q`

预期：FAIL，脚本不存在且配置仍为 ad-hoc identity。

- [ ] **步骤 3：实现最小 entitlement 与签名顺序**

Entitlement 仅包含 Sidecar/Python 必需的 JIT 或库验证例外；如果实测不需要则不声明。签名脚本要求 `APPLE_SIGNING_IDENTITY`，逐个验证 Mach-O 架构和签名；公证使用 `APPLE_ID`、`APPLE_APP_SPECIFIC_PASSWORD`、`APPLE_TEAM_ID`，所有 Secret 仅由 CI 注入。

- [ ] **步骤 4：在签名测试证书和正式 CI 上验证**

运行：

```bash
uv run pytest tests/test_release_security.py -q
./scripts/sign-macos-app.sh "$PDF2MD_APP_PATH"
./scripts/notarize-macos-app.sh "$PDF2MD_DMG_PATH"
./scripts/verify-macos-release.sh "$PDF2MD_APP_PATH" "$PDF2MD_DMG_PATH"
```

预期：`codesign --verify --deep --strict`、`spctl --assess --type execute`、`stapler validate` 全部通过。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src-tauri/entitlements.plist parsing-core-app/src-tauri/tauri.conf.json scripts/sign-macos-app.sh scripts/notarize-macos-app.sh scripts/verify-macos-release.sh tests/test_release_security.py
git commit -m "build: sign and notarize macos bundles"
```

## 任务 3：构建 Apple Silicon 与 Intel 原生产物

**文件：**
- 创建：`.github/workflows/beta-release.yml`
- 修改：`.github/workflows/release.yml`
- 修改：`tests/test_release_workflow.py`
- 修改：`scripts/check-release-sidecar.sh`
- 修改：`scripts/test-release-sidecar.sh`

- [ ] **步骤 1：编写失败的双架构和 Action SHA 测试**

测试要求构建矩阵包含 `aarch64-apple-darwin` 与 `x86_64-apple-darwin`，分别使用原生 ARM64 与 Intel Runner；所有 `uses:` 引用均为 40 位小写提交 SHA；release Job 只消费同一 workflow run 的具名 artifact。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_release_workflow.py tests/test_release_security.py -q`

预期：FAIL，当前只有 Apple Silicon、Action 使用版本标签。

- [ ] **步骤 3：实现双架构工作流和完整 SHA 固定**

ARM 使用仓库变量 `MACOS_ARM_RUNNER`，Intel 使用 `MACOS_X64_RUNNER`；变量在仓库环境分别配置为经验证的原生 Runner label。执行：

```bash
for spec in actions/checkout:v4 actions/setup-node:v4 actions/setup-python:v5 actions/upload-artifact:v4 actions/download-artifact:v4; do
  repo="${spec%%:*}"; tag="${spec##*:}"
  sha="$(git ls-remote "https://github.com/$repo.git" "refs/tags/$tag^{}" | awk '{print $1}')"
  test -n "$sha" || sha="$(git ls-remote "https://github.com/$repo.git" "refs/tags/$tag" | awk '{print $1}')"
  test "${#sha}" -eq 40
  printf '%s %s\n' "$repo@$tag" "$sha"
done
```

将每个返回的 40 位 SHA 写入 workflow 并由测试锁定。两架构分别构建完整嵌入 Python 和 Vision helper，禁止交叉编译后伪装原生。

- [ ] **步骤 4：验证两架构产物集合**

运行：

```bash
uv run pytest tests/test_release_workflow.py tests/test_release_security.py -q
gh workflow run beta-release.yml -f version=0.9.0-beta.1 -f rollout=0
gh run watch --exit-status
```

预期：两个构建 Job 与汇总 Job 通过，资产包含两架构 DMG、app.zip、更新签名和 SHA-256。

- [ ] **步骤 5：Commit**

```bash
git add .github/workflows/beta-release.yml .github/workflows/release.yml tests/test_release_workflow.py tests/test_release_security.py scripts/check-release-sidecar.sh scripts/test-release-sidecar.sh
git commit -m "ci: build native macos architectures"
```

## 任务 4：生成 SBOM、许可证和来源证明

**文件：**
- 创建：`scripts/generate-sbom.sh`
- 创建：`scripts/check-licenses.py`
- 创建：`config/license-policy.json`
- 创建：`tests/test_sbom.py`
- 修改：`.github/workflows/beta-release.yml`
- 修改：`.github/workflows/release.yml`

- [ ] **步骤 1：编写失败的物料清单测试**

测试每个发布版本包含 Python、npm、Cargo 和嵌入二进制组件；每项有名称、版本、许可证、来源和哈希；GPL/AGPL/未知许可证按策略阻断；SBOM 格式为 SPDX JSON。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_sbom.py -q`

预期：FAIL，SBOM 不存在。

- [ ] **步骤 3：实现合并和许可证门禁**

脚本从 `uv.lock`、`package-lock.json`、`Cargo.lock` 和 bundle 扫描结果合并，输出 `PDF2MD_<version>_<arch>.spdx.json` 与 `THIRD_PARTY_NOTICES.txt`。CI 对 DMG/app.zip/SBOM 生成 GitHub artifact attestation，证明 subject hash 与发布资产一致。

- [ ] **步骤 4：验证 SBOM 和篡改检测**

运行：

```bash
./scripts/generate-sbom.sh "$PDF2MD_APP_PATH" build/sbom
uv run python scripts/check-licenses.py build/sbom/*.spdx.json
uv run pytest tests/test_sbom.py -q
```

预期：全部通过，篡改一个 bundle 文件会导致哈希验证失败。

- [ ] **步骤 5：Commit**

```bash
git add scripts/generate-sbom.sh scripts/check-licenses.py config/license-policy.json tests/test_sbom.py .github/workflows/beta-release.yml .github/workflows/release.yml
git commit -m "build: publish auditable software bills"
```

## 任务 5：建立私有真实教材每日与每周回归

**文件：**
- 创建：`.github/workflows/private-textbook-regression.yml`
- 创建：`scripts/private-regression-report.py`
- 创建：`tests/test_private_regression_report.py`
- 修改：`scripts/run-private-ocr-benchmark.sh`

- [ ] **步骤 1：编写失败的隐私和调度测试**

测试 workflow 仅允许私有、自托管 macOS label；每日各抽 10 页，每周完整两本；artifact allowlist 只允许 `metrics.json`、`failures.redacted.json`、`run-metadata.json`；禁止上传 PDF、图片、Markdown 和绝对路径。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_private_regression_report.py tests/test_release_workflow.py -q`

预期：FAIL，workflow 和报告器不存在。

- [ ] **步骤 3：实现私有 Runner 流程**

Runner 通过 `PDF2MD_PRIVATE_BENCHMARK_MANIFEST` 挂载两本教材和金标准，Provider 测试账号设置费用上限。报告器校验输出 Schema、替换路径为 source hash、只保留页码和错误类别；失败现场留在 Runner 的加密工作目录并按 7 天清理。

- [ ] **步骤 4：验证每日和完整模式**

运行：

```bash
gh workflow run private-textbook-regression.yml -f mode=daily
gh run watch --exit-status
gh workflow run private-textbook-regression.yml -f mode=weekly
gh run watch --exit-status
```

预期：两个模式通过，GitHub artifact 不含教材内容。

- [ ] **步骤 5：Commit**

```bash
git add .github/workflows/private-textbook-regression.yml scripts/private-regression-report.py scripts/run-private-ocr-benchmark.sh tests/test_private_regression_report.py
git commit -m "ci: regress against private textbooks"
```

## 任务 6：建立发布前性能、稳定性和安全长测

**文件：**
- 创建：`scripts/verify-release.sh`
- 创建：`tests/performance/test_desktop_budgets.py`
- 创建：`tests/performance/test_pipeline_budgets.py`
- 创建：`tests/stability/test_24h_run.py`
- 创建：`tests/security/test_malicious_documents.py`
- 修改：`.github/workflows/beta-release.yml`

- [ ] **步骤 1：编写失败的预算测试**

测试规格全部预算：冷启动 3 秒、1200 页课程打开 2 秒、点击 100ms、页面切换 300ms、300 页原生 PDF 候选 90 秒/标准化 5 分钟、Apple Vision 20 页/分钟、空闲 500MB、500 页峰值 2GB、恢复 10 秒、搜索 200ms、缓存 80%。

- [ ] **步骤 2：运行小型预算测试验证失败**

运行：`uv run pytest tests/performance -m pr_budget -q`

预期：缺少基准 fixture 或至少一个预算尚未实现，测试失败并报告具体指标。

- [ ] **步骤 3：实现测量和统一发布脚本**

每个指标重复预热后至少测 20 次并报告 p50/p95；内存使用进程树 RSS；24 小时测试限制日志、事件、队列和缓存并检测增长斜率。`verify-release.sh` 依次运行 fast、architecture、security、契约、E2E、性能、真实格式、签名和 SBOM。

- [ ] **步骤 4：执行发布候选门禁**

运行：

```bash
./scripts/verify-release.sh
uv run pytest tests/stability/test_24h_run.py -m overnight -q
```

预期：所有预算达标；24 小时无无界增长、死锁、重复扣费或敏感日志。

- [ ] **步骤 5：Commit**

```bash
git add scripts/verify-release.sh tests/performance tests/stability tests/security/test_malicious_documents.py .github/workflows/beta-release.yml
git commit -m "test: enforce release performance budgets"
```

## 任务 7：实现 Beta 指标、5/25/100 放量和暂停回退

**文件：**
- 创建：`docs/beta/metrics-schema.json`
- 创建：`docs/release/BETA_RUNBOOK.md`
- 创建：`docs/release/ROLLBACK_RUNBOOK.md`
- 创建：`docs/releases/templates/beta-zh-CN.md`
- 创建：`docs/releases/templates/beta-en-US.md`
- 修改：`scripts/build-update-manifest.py`
- 修改：`services/control-plane/src/pdf2md_cloud/operations/routes.py`

- [ ] **步骤 1：编写失败的放量状态机测试**

测试 rollout 只允许 0→5→25→100 或任意值→0；推进必须读取最近 24 小时 crash-free、任务成功率、误接受率、Provider 错误、支持工单和删除失败指标；crash-free 低于 99.5%、核心任务成功率低于 98% 或任何阻断阈值越界均不能推进。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_updater_manifest.py -q`

预期：FAIL，清单尚不验证指标收据。

- [ ] **步骤 3：实现签名指标收据与暂停**

控制面生成只含聚合指标的签名 `BetaGateReceipt`；构建脚本验证收据签名、时间和版本后才提高 rollout。暂停将清单 rollout 设为 0；回退发布 N-1 的新签名清单，不复用旧清单签名。

- [ ] **步骤 4：验证放量和回退演练**

运行：

```bash
uv run pytest tests/test_updater_manifest.py -q
./scripts/build-update-manifest.py --channel beta --rollout 5 --gate-receipt build/gate.json --output build/beta.json
./scripts/build-update-manifest.py --channel beta --rollout 0 --output build/beta-paused.json
```

预期：有效收据允许推进；过期/伪造/不达标收据拒绝；暂停和回退清单通过签名验证。

- [ ] **步骤 5：Commit**

```bash
git add docs/beta/metrics-schema.json docs/release docs/releases/templates scripts/build-update-manifest.py services/control-plane/src/pdf2md_cloud/operations/routes.py tests/test_updater_manifest.py
git commit -m "feat: gate beta rollout on quality metrics"
```

## 任务 8：完成 30 天受控 Beta 准入报告

**文件：**
- 创建：`scripts/validate-beta-report.py`
- 创建：`tests/test_beta_report.py`
- 创建：`docs/beta/acceptance-report.md`
- 修改：`CHANGELOG.md`
- 修改：`README.md`
- 修改：`README_EN.md`

- [ ] **步骤 1：编写失败的连续日与阈值测试**

测试 29 天、日期中断、签名错误、crash-free 低于 99.5%、核心任务成功率低于 98%、OCR 或删除指标越界均拒绝；30 个连续日全部达标才返回通过。

- [ ] **步骤 2：运行报告测试确认失败**

运行：`uv run pytest tests/test_beta_report.py -q`

预期：FAIL，报告验证器不存在。

- [ ] **步骤 3：实现验证器并连续收集 30 天签名收据**

每天保存构建版本、活跃设备数、crash-free、任务成功率、OCR 指标、精读审查通过率、Mermaid 成功率、Provider 成本、删除 SLA、支持工单和安全事件聚合值。正文遥测保持关闭。

- [ ] **步骤 4：运行准入报告验证器并完成演练**

运行：`uv run python scripts/validate-beta-report.py docs/beta/acceptance-report.md`

预期：在少于 30 个连续日收据或任一指标缺失时 FAIL；完整达标后 PASS。

在 Apple Silicon 与 Intel 干净 Mac 分别安装 Beta；两台机器都不预装 Codex CLI 或 Python，使用 PDF2MD 托管路径导入五格式、运行两教材样本、更新 N-1、模拟失败并回退。另在高级用户机器验证 Codex CLI/BYOK 路径。记录 `codesign`、`spctl`、`stapler`、Updater 和数据哈希收据。

- [ ] **步骤 5：执行 M7 完整验收并 Commit**

运行：

```bash
./scripts/verify-release.sh
uv run python scripts/validate-beta-report.py docs/beta/acceptance-report.md
```

预期：全部通过，准入报告由产品、工程、安全、运维和法务负责人签字。

```bash
git add scripts/validate-beta-report.py tests/test_beta_report.py docs/beta/acceptance-report.md CHANGELOG.md README.md README_EN.md
git commit -m "docs: approve commercial beta exit"
```

## M7 完成收据

- [ ] Apple Silicon 与 Intel 资产均正式签名、公证、staple 并通过 Gatekeeper。
- [ ] Updater 签名、N-1 数据迁移、暂停和回退演练通过。
- [ ] SBOM、许可证、校验和和来源证明与资产哈希一致。
- [ ] 私有教材每日/每周回归不上传正文或图片。
- [ ] 30 天 Beta 的质量、性能、安全、成本和支持指标全部达标。
