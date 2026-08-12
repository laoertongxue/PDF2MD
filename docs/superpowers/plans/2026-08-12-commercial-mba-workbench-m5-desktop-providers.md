# M5 双语桌面与 Provider 中心实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 交付类似知识库编辑器的三栏正式桌面体验，支持中英文即时切换、长文精读与证据浏览，并完整配置托管、Codex CLI 和 API/BYOK Provider。

**架构：** FastAPI OpenAPI 生成唯一 TypeScript 契约；TanStack Query 管理服务端状态，Zustand 只保存短暂 UI 状态。Tauri 负责系统对话框、Keychain、可信 CLI 探测、单实例和 Sidecar；React 不接触密钥和本地文件实现细节。

**技术栈：** React 19、TypeScript strict、React Router、TanStack Query/Virtual、FormatJS ICU、Lucide、Mermaid、Tauri 2、Rust keyring、Vitest、Testing Library、WebdriverIO Tauri service、axe-core。

---

## 文件清单

**创建：**

- `src/parsing_core/serving/openapi.py`：稳定 OpenAPI 导出。
- `scripts/generate-api-client.sh`：生成 TypeScript 契约并检查 diff。
- `parsing-core-app/src/api/generated/`：生成类型和客户端。
- `parsing-core-app/src/i18n/index.ts`、`locales/zh-CN.json`、`locales/en-US.json`：ICU 文案。
- `parsing-core-app/src/app/queryClient.ts`：查询默认值和缓存边界。
- `parsing-core-app/src/layout/AppShell.tsx`、`PrimarySidebar.tsx`、`LibraryTree.tsx`、`WorkspacePane.tsx`：三栏框架。
- `parsing-core-app/src/features/providers/ProviderCenter.tsx`、`ProviderCard.tsx`、`CapabilityMatrix.tsx`：Provider 配置。
- `parsing-core-app/src/features/jobs/JobCenter.tsx`、`JobTimeline.tsx`：持久任务 UI。
- `parsing-core-app/src/features/quality/QualityPanel.tsx`、`EvidencePanel.tsx`：质量与证据。
- `parsing-core-app/src/features/reading/ReadingEditor.tsx`、`OutlinePanel.tsx`：长文精读与大纲。
- `parsing-core-app/src/features/settings/StorageSettings.tsx`：缓存额度、占用、清理和保护范围。
- `parsing-core-app/src/features/help/HelpCenter.tsx`：内置双语帮助中心。
- `docs/help/zh-CN/getting-started.md`、`providers.md`、`privacy.md`、`troubleshooting.md`：中文帮助。
- `docs/help/en-US/getting-started.md`、`providers.md`、`privacy.md`、`troubleshooting.md`：英文帮助。
- `parsing-core-app/src-tauri/src/keychain.rs`：系统 Keychain 命令。
- `parsing-core-app/src-tauri/src/provider_probe.rs`：CLI 和本地端点探测。
- `parsing-core-app/src-tauri/src/single_instance.rs`：工作区单实例激活。
- `parsing-core-app/src-tauri/src/tray.rs`：菜单栏任务状态与安全命令。
- `parsing-core-app/tests/e2e/desktop.spec.ts`、`providers.spec.ts`、`i18n.spec.ts`、`mermaid.spec.ts`：真实 App E2E。
- `parsing-core-app/src/accessibility/app.a11y.test.tsx`：axe-core 可访问性门禁。
- `parsing-core-app/src/performance/ui.performance.test.tsx`：UI 性能预算。
- `parsing-core-app/wdio.conf.ts`：WebdriverIO Tauri 配置。

**修改：**

- `src/parsing_core/workbench/settings.py`、`keychain.py`：转为无密钥配置/兼容门面。
- `src/parsing_core/serving/api/routes_settings.py`：Provider 能力和测试 API。
- `src/parsing_core/serving/serve.py`：OpenAPI、路由和 UI 事件装配。
- `parsing-core-app/package.json`、`package-lock.json`：i18n、Query、E2E、可访问性依赖。
- `parsing-core-app/src/main.tsx`、`App.tsx`、`index.css`：新应用根和设计 token。
- `parsing-core-app/src/api/workbench.ts`、`workbenchTypes.ts`、`runtime.ts`：删除手写重复契约。
- `parsing-core-app/src/store/useWorkbenchStore.ts`：仅保留 UI 状态。
- `parsing-core-app/src/components/workbench/*.tsx`：迁移到 feature 组件。
- `parsing-core-app/src-tauri/Cargo.toml`、`Cargo.lock`、`src/main.rs`、`src/state.rs`、`tauri.conf.json`、`capabilities/main.json`：Keychain、单实例和命令。

## 任务 1：用 OpenAPI 生成唯一 TypeScript 客户端

**文件：**
- 创建：`src/parsing_core/serving/openapi.py`
- 创建：`scripts/generate-api-client.sh`
- 创建：`tests/test_serving/test_openapi_contract.py`
- 创建：`parsing-core-app/src/api/generated/`
- 修改：`parsing-core-app/package.json`
- 修改：`parsing-core-app/package-lock.json`
- 修改：`parsing-core-app/src/api/workbench.ts`
- 修改：`parsing-core-app/src/api/workbenchTypes.ts`

- [ ] **步骤 1：编写失败的契约漂移测试**

```python
def test_openapi_contains_job_and_provider_contract(app):
    schema = app.openapi()
    assert "/api/jobs/{job_id}" in schema["paths"]
    assert "/api/providers" in schema["paths"]
    assert schema["components"]["schemas"]["JobResponse"]["properties"]["status"]["enum"]
```

生成脚本测试运行后 `git diff --exit-code parsing-core-app/src/api/generated`。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_serving/test_openapi_contract.py -q`

预期：FAIL，Provider 路由或稳定导出不存在。

- [ ] **步骤 3：实现稳定导出和生成脚本**

OpenAPI 按 path/schema 键排序并移除运行时 server URL。脚本先输出 `build/openapi.json`，再用固定版本 `openapi-typescript` 生成 `schema.d.ts` 和薄客户端；手写文件只包含认证 fetch、错误映射和分页助手，不再重复 DTO。

- [ ] **步骤 4：验证无漂移和前端类型**

运行：

```bash
./scripts/generate-api-client.sh
git diff --exit-code parsing-core-app/src/api/generated
uv run pytest tests/test_serving/test_openapi_contract.py -q
cd parsing-core-app && npm run typecheck
```

预期：全部通过。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/serving/openapi.py scripts/generate-api-client.sh tests/test_serving/test_openapi_contract.py parsing-core-app/src/api parsing-core-app/package.json parsing-core-app/package-lock.json
git commit -m "refactor: generate desktop api contracts"
```

## 任务 2：建立 zh-CN/en-US ICU 国际化

**文件：**
- 创建：`parsing-core-app/src/i18n/index.ts`
- 创建：`parsing-core-app/src/i18n/locales/zh-CN.json`
- 创建：`parsing-core-app/src/i18n/locales/en-US.json`
- 创建：`parsing-core-app/src/i18n/i18n.test.ts`
- 创建：`parsing-core-app/scripts/check-i18n.mjs`
- 创建：`parsing-core-app/src/features/help/HelpCenter.tsx`
- 创建：`docs/help/zh-CN/getting-started.md`
- 创建：`docs/help/zh-CN/providers.md`
- 创建：`docs/help/zh-CN/privacy.md`
- 创建：`docs/help/zh-CN/troubleshooting.md`
- 创建：`docs/help/en-US/getting-started.md`
- 创建：`docs/help/en-US/providers.md`
- 创建：`docs/help/en-US/privacy.md`
- 创建：`docs/help/en-US/troubleshooting.md`
- 修改：`parsing-core-app/package.json`
- 修改：`parsing-core-app/package-lock.json`
- 修改：`parsing-core-app/src/main.tsx`

- [ ] **步骤 1：编写失败的键覆盖与语言分离测试**

```typescript
it("keeps ui locale separate from output locale", () => {
  const settings = createLocaleSettings({ uiLocale: "en-US", outputLocale: "zh-CN" });
  expect(settings.uiLocale).toBe("en-US");
  expect(settings.outputLocale).toBe("zh-CN");
});
```

脚本递归比较中英文键集合、编译 ICU Message，并扫描 TSX 中新增的可见硬编码中文/英文字符串。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npm test -- src/i18n/i18n.test.ts`

预期：FAIL，i18n 模块不存在。

- [ ] **步骤 3：实现 LocaleProvider 和完整资源**

使用 `react-intl`；`uiLocale` 即时切换并持久化到非敏感设置，`outputLocale` 只影响新生成成果。迁移导航、按钮、状态、错误、设置和空状态文案；后端错误使用 `code + params` 映射资源键。帮助中心读取打包的中英文 Markdown，覆盖入门、Provider、隐私和故障排查，切换语言时定位到对应同名章节。

- [ ] **步骤 4：验证 100% 键覆盖**

运行：

```bash
cd parsing-core-app
npm test -- src/i18n/i18n.test.ts
node scripts/check-i18n.mjs
npm run typecheck
```

预期：全部通过，中英文键集合完全一致，硬编码扫描无新增违规。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src/i18n parsing-core-app/src/features/help parsing-core-app/src/main.tsx parsing-core-app/scripts/check-i18n.mjs parsing-core-app/package.json parsing-core-app/package-lock.json docs/help
git commit -m "feat: localize desktop in chinese and english"
```

## 任务 3：整理服务端状态和 UI 状态边界

**文件：**
- 创建：`parsing-core-app/src/app/queryClient.ts`
- 创建：`parsing-core-app/src/app/queryClient.test.ts`
- 修改：`parsing-core-app/src/store/useWorkbenchStore.ts`
- 修改：`parsing-core-app/src/store/useWorkbenchStore.test.ts`
- 修改：`parsing-core-app/package.json`
- 修改：`parsing-core-app/package-lock.json`

- [ ] **步骤 1：编写失败的状态所有权测试**

```typescript
it("does not persist server entities in zustand", () => {
  const state = useWorkbenchStore.getState();
  expect(state).not.toHaveProperty("courses");
  expect(state).not.toHaveProperty("chapters");
  expect(state).not.toHaveProperty("jobs");
  expect(state).toMatchObject({ selectedCourseId: null, leftPaneOpen: true });
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npm test -- src/store/useWorkbenchStore.test.ts`

预期：FAIL，现有 Store 保存课程、章节或任务副本。

- [ ] **步骤 3：引入 QueryClient 并收缩 Zustand**

Query 默认 `staleTime=5_000`、网络错误最多重试 2 次、认证错误不重试；Job 通过事件序号失效精确 query key。Zustand 只保存选择项、面板尺寸、展开节点和编辑器临时状态，刷新后服务端数据重新查询。

- [ ] **步骤 4：验证缓存与断线恢复**

运行：`cd parsing-core-app && npm test -- src/app/queryClient.test.ts src/store/useWorkbenchStore.test.ts`

预期：全部通过，WebSocket 断线补拉不会重复插入事件。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src/app parsing-core-app/src/store parsing-core-app/package.json parsing-core-app/package-lock.json
git commit -m "refactor: separate server and ui state"
```

## 任务 4：实现三栏知识工作台和长文浏览

**文件：**
- 创建：`parsing-core-app/src/layout/AppShell.tsx`
- 创建：`parsing-core-app/src/layout/PrimarySidebar.tsx`
- 创建：`parsing-core-app/src/layout/LibraryTree.tsx`
- 创建：`parsing-core-app/src/layout/WorkspacePane.tsx`
- 创建：`parsing-core-app/src/features/reading/ReadingEditor.tsx`
- 创建：`parsing-core-app/src/features/reading/OutlinePanel.tsx`
- 创建：`parsing-core-app/src/layout/AppShell.test.tsx`
- 修改：`parsing-core-app/src/App.tsx`
- 修改：`parsing-core-app/src/index.css`

- [ ] **步骤 1：编写失败的工作流和布局测试**

测试桌面 1440x900 显示主导航、课程/教材/章节树和文档区；1024x768 时大纲可折叠；最窄 900px 无文本重叠。键盘可遍历树、打开章节、切换编辑/预览；10 万块文档只渲染可见窗口。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npm test -- src/layout/AppShell.test.tsx`

预期：FAIL，新布局不存在。

- [ ] **步骤 3：实现稳定三栏尺寸和虚拟内容**

主栏 64px，资料栏可调 `240-420px`，内容区 `minmax(0,1fr)`，大纲侧栏 240px 可折叠；使用 1px 分隔线和最大 8px 圆角，不嵌套卡片。章节、卡片和文档块使用虚拟列表，选中项和加载状态不能改变行高。

- [ ] **步骤 4：验证布局、可访问性和长文性能**

运行：

```bash
cd parsing-core-app
npm test -- src/layout/AppShell.test.tsx
npm run typecheck
npm run build
```

预期：全部通过；10 万块合成数据滚动无 DOM 爆炸，交互反馈测试 p95 不超过 100ms。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src/layout parsing-core-app/src/features/reading parsing-core-app/src/App.tsx parsing-core-app/src/index.css
git commit -m "feat: build three-pane reading workspace"
```

## 任务 5：建立后端 Provider 配置与能力路由

**文件：**
- 创建：`src/parsing_core/workbench/domain/providers.py`
- 创建：`src/parsing_core/workbench/application/services/provider_service.py`
- 创建：`src/parsing_core/serving/api/routes_settings.py`
- 创建：`tests/test_workbench/application/test_provider_service.py`
- 修改：`src/parsing_core/workbench/settings.py`
- 修改：`src/parsing_core/serving/serve.py`

- [ ] **步骤 1：编写失败的能力矩阵测试**

```python
def test_provider_router_never_selects_missing_capability(service):
    service.register(provider("codex", capabilities={Capability.VISION}))
    with pytest.raises(ProviderUnavailableError) as exc:
        service.route(Capability.STRUCTURED_READING)
    assert exc.value.code == "provider.capability_unavailable"
```

另测每个能力可配置一个主 Provider 和一个备用 Provider；能力枚举固定为 `TEXT_READING`、`VISION_TRANSCRIPTION`、`ADJUDICATION`、`LAYOUT_OCR`、`EVIDENCE_REVIEW`、`TEACHING_REVIEW`、`EMBEDDING`、`RETRIEVAL`。路由优先级为用户显式选择、托管默认、已验证 BYOK、本地 CLI；备用 Provider 必须满足同一能力、Schema、质量门槛、隐私授权和额度，否则暂停而不是降级。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/application/test_provider_service.py -q`

预期：FAIL，能力服务不存在。

- [ ] **步骤 3：实现无密钥 ProviderProfile**

`ProviderProfile` 保存 kind、display_name、base_url、model、capabilities、auth_secret_ref、enabled、priority、data_region、cross_border、last_probe；`CapabilityRoute` 保存 primary/fallback profile ID 和最低质量。API 永不返回 secret。连接测试返回稳定 `ProbeResult`，包含认证、网络、模型、能力、延迟和错误码。

- [ ] **步骤 4：验证路由和 API Schema**

运行：`uv run pytest tests/test_workbench/application/test_provider_service.py tests/test_serving/test_openapi_contract.py -q`

预期：全部通过，密钥不出现在 API、日志或 SQLite。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/domain/providers.py src/parsing_core/workbench/application/services/provider_service.py src/parsing_core/workbench/settings.py src/parsing_core/serving/api/routes_settings.py src/parsing_core/serving/serve.py tests/test_workbench/application/test_provider_service.py
git commit -m "feat: route provider capabilities"
```

## 任务 6：用 Tauri Keychain 和可信探测实现 Provider 中心

**文件：**
- 创建：`parsing-core-app/src-tauri/src/keychain.rs`
- 创建：`parsing-core-app/src-tauri/src/provider_probe.rs`
- 创建：`parsing-core-app/src/features/providers/ProviderCenter.tsx`
- 创建：`parsing-core-app/src/features/providers/ProviderCard.tsx`
- 创建：`parsing-core-app/src/features/providers/CapabilityMatrix.tsx`
- 创建：`parsing-core-app/src/features/providers/ProviderCenter.test.tsx`
- 修改：`parsing-core-app/src-tauri/Cargo.toml`
- 修改：`parsing-core-app/src-tauri/Cargo.lock`
- 修改：`parsing-core-app/src-tauri/src/main.rs`
- 修改：`parsing-core-app/src-tauri/capabilities/main.json`
- 修改：`parsing-core-app/src/components/workbench/Settings.tsx`

- [ ] **步骤 1：编写失败的密钥生命周期和 UI 测试**

Rust 测试使用 Keychain fake 断言保存、读取 masked 状态、替换和删除；React 测试断言托管、Codex CLI、DeepSeek API、百度 API、OpenAI 兼容 API、本地兼容端点均可配置，密码字段回显始终为空或掩码。远程 BYOK 必须显示数据区域和跨境提示，用户未单独确认时不能启用；本地 Provider 不显示云端授权。

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
(cd parsing-core-app/src-tauri && cargo test keychain provider_probe)
(cd parsing-core-app && npm test -- src/features/providers/ProviderCenter.test.tsx)
```

预期：FAIL，新模块不存在。

- [ ] **步骤 3：实现系统边界**

Rust 用 `keyring` crate 将 Secret 保存为 `com.pdf2md.provider/<profile_id>`；命令只接受/返回 secret ref 和 masked metadata。CLI 由文件选择器选择后调用 M3 的解析后验证，显示版本、目标路径和 SHA 前 12 位；OpenAI 兼容远程端点只允许 `https`，拒绝云元数据、链路本地、私网解析结果和不安全重定向，本地例外仅限显式本地 Provider 的 loopback。BYOK 请求由本地 Sidecar 直连，不经过 PDF2MD 云端。

- [ ] **步骤 4：验证真实 Keychain、CLI 和 API 连接**

运行：

```bash
(cd parsing-core-app/src-tauri && cargo test)
(cd parsing-core-app && npm test -- src/features/providers/ProviderCenter.test.tsx)
PDF2MD_E2E_KEYCHAIN_SERVICE=com.pdf2md.test npm run test:e2e -- providers.spec.ts
```

预期：保存/测试/删除完整通过；测试结束清理测试 Keychain 项；真实 Codex 登录可探测，未安装状态提供结构化提示。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src-tauri parsing-core-app/src/features/providers parsing-core-app/src/components/workbench/Settings.tsx
git commit -m "feat: configure cli and api providers"
```

## 任务 7：实现任务、质量、证据与编辑冲突界面

**文件：**
- 创建：`parsing-core-app/src/features/jobs/JobCenter.tsx`
- 创建：`parsing-core-app/src/features/jobs/JobTimeline.tsx`
- 创建：`parsing-core-app/src/features/quality/QualityPanel.tsx`
- 创建：`parsing-core-app/src/features/quality/EvidencePanel.tsx`
- 创建：`parsing-core-app/src/features/settings/StorageSettings.tsx`
- 创建：`parsing-core-app/src/features/settings/StorageSettings.test.tsx`
- 创建：`parsing-core-app/src/features/jobs/JobCenter.test.tsx`
- 创建：`parsing-core-app/src/features/quality/EvidencePanel.test.tsx`
- 修改：`parsing-core-app/src/features/reading/ReadingEditor.tsx`

- [ ] **步骤 1：编写失败的可恢复工作流测试**

测试任务页展示教材、阶段、页数、调用范围、费用区间、暂停/继续/取消/局部重跑；隔离页显示冲突候选、裁决理由和证据裁剪；编辑保存带 `expected_revision`，冲突时展示差异而不覆盖。存储设置展示缓存占用和上限，清理预览明确不会删除证据与已发布成果，确认后只删除可重建 LRU Artifact。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npm test -- src/features/jobs/JobCenter.test.tsx src/features/quality/EvidencePanel.test.tsx`

预期：FAIL，新组件不存在。

- [ ] **步骤 3：实现紧凑操作界面**

状态使用图标、文本和颜色三重表达；任务行尺寸稳定，进度动画只在 `RUNNING` 时显示；`BLOCKED`/`PAUSED_BY_QUOTA` 显示原因与可执行命令。证据面板按声明跳转页和 bbox，不默认加载整本页图。

- [ ] **步骤 4：验证交互和无障碍**

运行：`cd parsing-core-app && npm test -- src/features/jobs src/features/quality src/features/reading src/features/settings`

预期：全部通过，键盘操作和屏幕阅读标签完整，无重叠文本。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src/features/jobs parsing-core-app/src/features/quality parsing-core-app/src/features/reading/ReadingEditor.tsx parsing-core-app/src/features/settings
git commit -m "feat: expose durable jobs and evidence"
```

## 任务 8：单实例、真实桌面 E2E 和性能门禁

**文件：**
- 创建：`parsing-core-app/src-tauri/src/single_instance.rs`
- 创建：`parsing-core-app/src-tauri/src/tray.rs`
- 创建：`parsing-core-app/wdio.conf.ts`
- 创建：`parsing-core-app/tests/e2e/desktop.spec.ts`
- 创建：`parsing-core-app/tests/e2e/providers.spec.ts`
- 创建：`parsing-core-app/tests/e2e/i18n.spec.ts`
- 创建：`parsing-core-app/tests/e2e/mermaid.spec.ts`
- 创建：`parsing-core-app/src/accessibility/app.a11y.test.tsx`
- 创建：`parsing-core-app/src/performance/ui.performance.test.tsx`
- 修改：`parsing-core-app/package.json`
- 修改：`parsing-core-app/package-lock.json`
- 修改：`parsing-core-app/src-tauri/src/main.rs`

- [ ] **步骤 1：编写失败的真实 App E2E**

E2E 启动 `.app`，创建课程、选择本地文件夹、导入两本 fixture、观察 Job、切换中英文、配置测试 Provider、打开精读、点击证据、编辑并保存、确认 Mermaid 非空。菜单栏展示正在运行/暂停/阻断数量，只提供打开应用、暂停全部和退出命令。第二次启动相同工作区必须激活首个窗口而非启动第二个 Sidecar。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npm run test:e2e`

预期：FAIL，WebdriverIO 配置或单实例尚不存在。

- [ ] **步骤 3：实施单实例与 E2E 工具链**

Tauri 单实例事件只传递经过规范化的工作区路径和文件打开意图；首实例获得焦点并处理。菜单栏只读取聚合 Job 状态，退出前请求 Worker checkpoint，不显示教材标题或正文。WebdriverIO 使用 Tauri service，截图固定 1440x900、1024x768 和 900x700；Mermaid 用 canvas/SVG 像素摘要断言非空。

`package.json` 增加：

```json
{
  "scripts": {
    "test:e2e": "wdio run wdio.conf.ts",
    "test:a11y": "vitest run src/accessibility/app.a11y.test.tsx",
    "benchmark:ui": "vitest run src/performance/ui.performance.test.tsx"
  }
}
```

- [ ] **步骤 4：执行 M5 验收**

运行：

```bash
cd parsing-core-app
npm run test:e2e
npm run test:a11y
npm run benchmark:ui
npm run build
cd src-tauri && cargo clippy --all-targets -- -D warnings && cargo test
```

预期：中英文 E2E、可访问性和布局截图通过；冷启动 p95 不超过 3 秒，索引课程打开 p95 不超过 2 秒，页面切换 p95 不超过 300ms。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src-tauri/src/single_instance.rs parsing-core-app/src-tauri/src/tray.rs parsing-core-app/src-tauri/src/main.rs parsing-core-app/wdio.conf.ts parsing-core-app/tests/e2e parsing-core-app/src/accessibility parsing-core-app/src/performance parsing-core-app/package.json parsing-core-app/package-lock.json
git commit -m "test: verify bilingual desktop workflows"
```

## M5 完成收据

- [ ] 三栏工作台可完成课程、教材、章节、精读、专题和写作主流程。
- [ ] `zh-CN` 与 `en-US` 键覆盖 100%，界面语言不改变成果语言或 OCR 数据。
- [ ] 托管、Codex CLI、DeepSeek、百度和兼容 API 均可配置和探测。
- [ ] 所有 Secret 只存 Keychain，不进入前端状态、SQLite 或日志。
- [ ] 真实 macOS App 的中英文、Mermaid、双实例、性能和可访问性门禁通过。
