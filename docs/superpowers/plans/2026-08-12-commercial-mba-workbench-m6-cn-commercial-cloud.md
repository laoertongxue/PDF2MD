# M6 中国区云端商业能力实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 建立中国大陆托管服务，提供账号、设备、订阅、额度、模型网关、临时对象、删除回执、支付对账、同意记录和运营能力，同时保持教材正文默认本地。

**架构：** 云端作为独立 FastAPI 服务部署在中国区，PostgreSQL 保存账号、设备、权益和不可变账本，Aliyun KMS/OSS 保存加密临时对象，Provider Gateway 代理模型/OCR 请求。桌面使用短期 Access Token 和 Keychain Refresh Token；每个调用以 `provider_call_id` 幂等记账。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy 2 async、Alembic、PostgreSQL 16、Redis 7、Aliyun OSS/KMS/SMS/DirectMail、DeepSeek、百度 OCR、Alipay、WeChat Pay、OpenTelemetry、pytest、testcontainers、Terraform。

---

## 文件清单

**创建：**

- `services/control-plane/pyproject.toml`、`uv.lock`：独立锁定云端服务。
- `services/control-plane/src/pdf2md_cloud/main.py`、`config.py`、`errors.py`：应用入口和配置。
- `services/control-plane/src/pdf2md_cloud/db.py`、`models/*.py`、`repositories/*.py`：数据库与仓储。
- `services/control-plane/alembic.ini`、`alembic/versions/*.py`：云端 Migration。
- `services/control-plane/src/pdf2md_cloud/auth/`：手机号/邮箱验证码、JWT、Refresh Token、设备会话。
- `services/control-plane/src/pdf2md_cloud/consent/`：授权、撤回、协议版本和 AI 标识配置。
- `services/control-plane/src/pdf2md_cloud/billing/`：套餐、权益、额度账本、支付宝、微信支付、退款和发票状态。
- `services/control-plane/src/pdf2md_cloud/gateway/`：托管视觉、DeepSeek、百度 OCR 路由、限流、熔断和幂等。
- `services/control-plane/src/pdf2md_cloud/storage/`：OSS/KMS 临时对象、保留和删除回执。
- `services/control-plane/src/pdf2md_cloud/resume/`：独立授权的云端续跑任务包。
- `services/control-plane/src/pdf2md_cloud/operations/`：脱敏审计、诊断包、状态与管理 API。
- `services/control-plane/tests/`：单元、集成、安全、账本、删除和负载测试。
- `services/control-plane/docker-compose.test.yml`：PostgreSQL、Redis、MinIO 测试环境。
- `infra/terraform/cn/`：VPC、容器、PostgreSQL、Redis、OSS、KMS、WAF、日志和监控。
- `docs/legal/zh-CN/PRIVACY.md`、`TERMS.md`、`AI_NOTICE.md`、`COPYRIGHT_POLICY.md`：中文法律草案。
- `docs/legal/en-US/PRIVACY.md`、`TERMS.md`、`AI_NOTICE.md`、`COPYRIGHT_POLICY.md`：英文对应稿。
- `docs/runbooks/data-deletion.md`、`provider-outage.md`、`security-incident.md`、`billing-reconciliation.md`：运行手册。

**修改：**

- `pyproject.toml`：声明仓库工具排除/包含云端路径。
- `.github/workflows/ci.yml`：云端单元、集成、安全和 Migration 门禁。
- `parsing-core-app/src/features/account/`：登录、设备、套餐、额度、授权和删除 UI。
- `parsing-core-app/src-tauri/src/keychain.rs`：Refresh Token 生命周期。
- `src/parsing_core/workbench/infrastructure/providers/managed_vision.py`、`deepseek_reading.py`、`baidu_ocr.py`：调用中国区网关。

## 任务 1：创建独立云端服务和可回滚 Migration

**文件：**
- 创建：`services/control-plane/pyproject.toml`
- 创建：`services/control-plane/src/pdf2md_cloud/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/main.py`
- 创建：`services/control-plane/src/pdf2md_cloud/config.py`
- 创建：`services/control-plane/src/pdf2md_cloud/db.py`
- 创建：`services/control-plane/alembic.ini`
- 创建：`services/control-plane/alembic/versions/0001_initial.py`
- 创建：`services/control-plane/tests/test_health.py`
- 创建：`services/control-plane/tests/test_migrations.py`
- 创建：`services/control-plane/docker-compose.test.yml`

- [ ] **步骤 1：编写失败的健康和 Migration 测试**

```python
async def test_health_does_not_leak_configuration(client):
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "database" not in response.text


def test_upgrade_and_downgrade_round_trip(database_url):
    alembic_upgrade(database_url, "head")
    assert current_revision(database_url) == "0001"
    alembic_downgrade(database_url, "base")
    assert user_tables(database_url) == set()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd services/control-plane && uv run pytest tests/test_health.py tests/test_migrations.py -q`

预期：FAIL，服务不存在。

- [ ] **步骤 3：实现最小服务骨架**

配置只从环境读取，启动时验证中国区 endpoint、KMS key、JWT issuer 和数据库；日志使用 JSON 且自动过滤 token、手机号、邮箱、正文和本地路径。SQLAlchemy 会话请求级关闭，Alembic 初始表只包含 service metadata。

- [ ] **步骤 4：验证服务和容器环境**

运行：

```bash
cd services/control-plane
docker compose -f docker-compose.test.yml up -d
uv run pytest tests/test_health.py tests/test_migrations.py -q
docker compose -f docker-compose.test.yml down -v
```

预期：全部通过，升级/降级可重复。

- [ ] **步骤 5：Commit**

```bash
git add services/control-plane
git commit -m "feat: scaffold china control plane"
```

## 任务 2：实现手机号/邮箱登录和设备会话

**文件：**
- 创建：`services/control-plane/src/pdf2md_cloud/auth/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/auth/models.py`
- 创建：`services/control-plane/src/pdf2md_cloud/auth/service.py`
- 创建：`services/control-plane/src/pdf2md_cloud/auth/routes.py`
- 创建：`services/control-plane/src/pdf2md_cloud/auth/providers.py`
- 创建：`services/control-plane/alembic/versions/0002_auth_devices.py`
- 创建：`services/control-plane/tests/auth/test_auth.py`
- 创建：`services/control-plane/tests/auth/test_devices.py`
- 创建：`parsing-core-app/src/features/account/SignIn.tsx`
- 创建：`parsing-core-app/src/features/account/Devices.tsx`
- 创建：`parsing-core-app/src/features/account/AccountAccess.test.tsx`

- [ ] **步骤 1：编写失败的验证码和撤销测试**

测试验证码哈希存储、5 分钟过期、60 秒发送冷却、每日上限、5 次错误锁定、手机号/邮箱归一化；Refresh Token 轮换后旧 token 立即失效；远程注销设备后其 token 全部拒绝。桌面测试必须证明未登录用户仍可打开、阅读和编辑本地课程，只有托管调用、云端续跑和商业账户功能要求登录。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd services/control-plane && uv run pytest tests/auth -q`

预期：FAIL，认证模块不存在。

- [ ] **步骤 3：实现无密码认证和设备会话**

Aliyun SMS/DirectMail 实现 `VerificationSender`，测试使用内存 fake。Access Token 15 分钟，Refresh Token 30 天且每次使用轮换；数据库只存 Refresh Token 哈希、设备公钥摘要、最后活动和撤销时间。桌面把 Refresh Token 放 Keychain，内存保存 Access Token。

- [ ] **步骤 4：验证攻击矩阵和桌面登录**

运行：

```bash
(cd services/control-plane && uv run pytest tests/auth -q)
(cd parsing-core-app && npm test -- src/features/account/AccountAccess.test.tsx)
```

预期：重放、暴力尝试、枚举账号、撤销设备和 token 轮换测试通过。

- [ ] **步骤 5：Commit**

```bash
git add services/control-plane/src/pdf2md_cloud/auth services/control-plane/alembic/versions/0002_auth_devices.py services/control-plane/tests/auth parsing-core-app/src/features/account parsing-core-app/src-tauri/src/keychain.rs
git commit -m "feat: authenticate users and devices"
```

## 任务 3：实现授权、隐私权利和协议版本

**文件：**
- 创建：`services/control-plane/src/pdf2md_cloud/consent/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/consent/models.py`
- 创建：`services/control-plane/src/pdf2md_cloud/consent/service.py`
- 创建：`services/control-plane/src/pdf2md_cloud/consent/routes.py`
- 创建：`services/control-plane/alembic/versions/0003_consent.py`
- 创建：`services/control-plane/tests/consent/test_consent.py`
- 创建：`parsing-core-app/src/features/account/PrivacyCenter.tsx`
- 创建：`parsing-core-app/src/features/account/PrivacyCenter.test.tsx`
- 创建：`docs/legal/zh-CN/PRIVACY.md`
- 创建：`docs/legal/en-US/PRIVACY.md`
- 创建：`docs/legal/zh-CN/AI_NOTICE.md`
- 创建：`docs/legal/en-US/AI_NOTICE.md`

- [ ] **步骤 1：编写失败的细粒度授权测试**

```python
async def test_cloud_ocr_requires_specific_active_consent(client, user):
    token = login(user)
    response = await client.post("/v1/gateway/ocr", headers=token, json=page_request())
    assert response.status_code == 403
    assert response.json()["code"] == "consent.cloud_ocr_required"
```

另测模型、诊断、分析分别授权，撤回后新调用立即拒绝，协议更新需要重新确认，数据导出和账号删除请求可追踪。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd services/control-plane && uv run pytest tests/consent -q`

预期：FAIL，授权模块不存在。

- [ ] **步骤 3：实现版本化同意记录**

每条记录包含 purpose、policy_version、granted_at、revoked_at、client_version 和证明哈希；不保存 IP 全值。PrivacyCenter 清楚展示云端 OCR、模型、诊断、分析四个开关及影响，正文遥测默认关闭。

- [ ] **步骤 4：验证撤回和本地优先路径**

运行：

```bash
(cd services/control-plane && uv run pytest tests/consent -q)
(cd parsing-core-app && npm test -- src/features/account/PrivacyCenter.test.tsx)
```

预期：撤回后托管调用停止，本地功能继续可用。

- [ ] **步骤 5：Commit**

```bash
git add services/control-plane/src/pdf2md_cloud/consent services/control-plane/alembic/versions/0003_consent.py services/control-plane/tests/consent parsing-core-app/src/features/account/PrivacyCenter.tsx parsing-core-app/src/features/account/PrivacyCenter.test.tsx docs/legal
git commit -m "feat: record privacy consent and withdrawal"
```

## 任务 4：实现套餐、权益和不可变额度账本

**文件：**
- 创建：`services/control-plane/src/pdf2md_cloud/billing/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/billing/models.py`
- 创建：`services/control-plane/src/pdf2md_cloud/billing/ledger.py`
- 创建：`services/control-plane/src/pdf2md_cloud/billing/entitlements.py`
- 创建：`services/control-plane/src/pdf2md_cloud/billing/routes.py`
- 创建：`services/control-plane/alembic/versions/0004_billing.py`
- 创建：`services/control-plane/tests/billing/test_ledger.py`
- 创建：`services/control-plane/tests/billing/test_entitlements.py`
- 创建：`parsing-core-app/src/features/account/PlanAndUsage.tsx`

- [ ] **步骤 1：编写失败的双重记账和幂等测试**

```python
def test_provider_call_is_charged_exactly_once(ledger, account):
    first = ledger.capture(account, provider_call_id="call-1", units=10)
    second = ledger.capture(account, provider_call_id="call-1", units=10)
    assert first.entry_id == second.entry_id
    assert ledger.balance(account) == account.opening_balance - 10


def test_failed_provider_call_releases_reservation(ledger, account):
    reservation = ledger.reserve(account, "call-2", units=20)
    ledger.release(reservation, reason="provider_timeout")
    assert ledger.balance(account) == account.opening_balance
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd services/control-plane && uv run pytest tests/billing/test_ledger.py tests/billing/test_entitlements.py -q`

预期：FAIL，账本不存在。

- [ ] **步骤 3：实现追加式账本和签名权益**

账本只追加 `RESERVE/CAPTURE/RELEASE/GRANT/REFUND/EXPIRE`，每条含前一条哈希；余额由事务内汇总和约束保证不透支。权益响应以服务端 Ed25519 签名，桌面离线只允许查看编辑，不允许离线消费托管额度。

- [ ] **步骤 4：验证并发扣费和额度暂停**

运行：`cd services/control-plane && uv run pytest tests/billing -q --count=20`

预期：并发请求不超扣；额度不足返回 `billing.quota_exhausted`，桌面 Job 进入 `PAUSED_BY_QUOTA`。

- [ ] **步骤 5：Commit**

```bash
git add services/control-plane/src/pdf2md_cloud/billing services/control-plane/alembic/versions/0004_billing.py services/control-plane/tests/billing parsing-core-app/src/features/account/PlanAndUsage.tsx
git commit -m "feat: meter provider usage exactly once"
```

## 任务 5：实现中国区 Provider Gateway、限流和熔断

**文件：**
- 创建：`services/control-plane/src/pdf2md_cloud/gateway/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/gateway/providers/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/gateway/contracts.py`
- 创建：`services/control-plane/src/pdf2md_cloud/gateway/service.py`
- 创建：`services/control-plane/src/pdf2md_cloud/gateway/routes.py`
- 创建：`services/control-plane/src/pdf2md_cloud/gateway/providers/deepseek.py`
- 创建：`services/control-plane/src/pdf2md_cloud/gateway/providers/baidu.py`
- 创建：`services/control-plane/src/pdf2md_cloud/gateway/providers/vision.py`
- 创建：`services/control-plane/tests/gateway/test_gateway.py`
- 创建：`services/control-plane/tests/gateway/test_circuit_breaker.py`
- 创建：`services/control-plane/tests/load/gateway_smoke.py`
- 修改：`src/parsing_core/workbench/infrastructure/providers/managed_vision.py`
- 修改：`src/parsing_core/workbench/infrastructure/providers/deepseek_reading.py`
- 修改：`src/parsing_core/workbench/infrastructure/providers/baidu_ocr.py`

- [ ] **步骤 1：编写失败的网关幂等和脱敏测试**

测试同一 `provider_call_id + request_hash` 返回同一结果；相同 ID 不同哈希返回 409；日志不含图像、正文、API Key、手机号和本地路径；熔断打开时不扣费并返回可重试时间。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd services/control-plane && uv run pytest tests/gateway -q`

预期：FAIL，网关不存在。

- [ ] **步骤 3：实现能力路由和密钥隔离**

Provider Secret 只从 Aliyun KMS/Secret Manager 获取，进程内短期缓存并从不返回桌面。Gateway 只服务 `PDF2MD_MANAGED` profile；Codex CLI 与所有 BYOK profile 保持本地直连。Gateway 验证 entitlement、consent、Schema、大小、hash、call ID、内容安全策略和 AI 标识版本，预留额度后调用 Provider，成功捕获、失败释放；供应商质量或错误率越界自动熔断。内容安全命中返回可申诉的稳定类别，不把供应商原始审查细节或敏感内容写入日志。

- [ ] **步骤 4：验证故障和网关延迟**

运行：

```bash
cd services/control-plane
uv run pytest tests/gateway tests/billing -q
uv run python tests/load/gateway_smoke.py --requests 500 --p95-ms 300
```

预期：故障矩阵、内容安全、未成年人保护配置、投诉关联和 AI 标识版本通过，网关额外延迟 p95 不超过 300ms；BYOK 测试证明网关调用数为 0。

- [ ] **步骤 5：Commit**

```bash
git add services/control-plane/src/pdf2md_cloud/gateway services/control-plane/tests/gateway src/parsing_core/workbench/infrastructure/providers
git commit -m "feat: proxy providers through china gateway"
```

## 任务 6：实现加密临时对象、保留和删除回执

**文件：**
- 创建：`services/control-plane/src/pdf2md_cloud/storage/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/resume/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/storage/models.py`
- 创建：`services/control-plane/src/pdf2md_cloud/storage/service.py`
- 创建：`services/control-plane/src/pdf2md_cloud/storage/routes.py`
- 创建：`services/control-plane/src/pdf2md_cloud/storage/cleanup.py`
- 创建：`services/control-plane/alembic/versions/0005_storage.py`
- 创建：`services/control-plane/tests/storage/test_retention.py`
- 创建：`services/control-plane/tests/storage/test_deletion.py`
- 创建：`services/control-plane/src/pdf2md_cloud/resume/models.py`
- 创建：`services/control-plane/src/pdf2md_cloud/resume/service.py`
- 创建：`services/control-plane/src/pdf2md_cloud/resume/routes.py`
- 创建：`services/control-plane/tests/storage/test_cloud_resume.py`
- 创建：`parsing-core-app/src/features/account/CloudContinuationConsent.tsx`
- 创建：`parsing-core-app/src/features/account/CloudContinuationConsent.test.tsx`
- 创建：`docs/runbooks/data-deletion.md`

- [ ] **步骤 1：编写失败的最小上传和删除测试**

测试只允许声明过的单页/裁剪上传；对象使用每任务数据密钥和 KMS 信封；正常任务完成立即删除，异常最长 24 小时。云端续跑必须有独立授权、显式任务范围和最长 7 天期限；撤回后停止新步骤并删除任务包。删除覆盖对象、缓存、任务载荷并返回签名回执。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd services/control-plane && uv run pytest tests/storage -q`

预期：FAIL，存储服务不存在。

- [ ] **步骤 3：实现状态机和幂等清理**

对象状态为 `PENDING_UPLOAD/AVAILABLE/DELETE_REQUESTED/DELETED/DELETE_FAILED`；预签名 URL 最长 10 分钟、绑定内容长度与 SHA。`CloudResumePackage` 只包含已授权阶段需要的块、页裁剪、Artifact 哈希和检查点，不默认上传原书；桌面单独显示范围、期限和删除状态。清理器可重复执行，删除回执包含 object IDs、请求时间、完成时间、结果、策略版本和服务签名，不含教材内容。

- [ ] **步骤 4：验证故障恢复和回执**

运行：

```bash
(cd services/control-plane && uv run pytest tests/storage -q --count=10)
(cd parsing-core-app && npm test -- src/features/account/CloudContinuationConsent.test.tsx)
```

预期：OSS 超时、重复删除、部分失败和恢复全部通过，无超期对象。

- [ ] **步骤 5：Commit**

```bash
git add services/control-plane/src/pdf2md_cloud/storage services/control-plane/src/pdf2md_cloud/resume services/control-plane/alembic/versions/0005_storage.py services/control-plane/tests/storage parsing-core-app/src/features/account/CloudContinuationConsent.tsx parsing-core-app/src/features/account/CloudContinuationConsent.test.tsx docs/runbooks/data-deletion.md
git commit -m "feat: delete encrypted temporary content"
```

## 任务 7：接入支付宝、微信支付、退款和发票对账

**文件：**
- 创建：`services/control-plane/src/pdf2md_cloud/billing/payments/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/billing/payments/base.py`
- 创建：`services/control-plane/src/pdf2md_cloud/billing/payments/alipay.py`
- 创建：`services/control-plane/src/pdf2md_cloud/billing/payments/wechat.py`
- 创建：`services/control-plane/src/pdf2md_cloud/billing/payments/webhooks.py`
- 创建：`services/control-plane/src/pdf2md_cloud/billing/reconciliation.py`
- 创建：`services/control-plane/tests/billing/test_payments.py`
- 创建：`services/control-plane/tests/billing/test_reconciliation.py`
- 创建：`docs/runbooks/billing-reconciliation.md`

- [ ] **步骤 1：编写失败的签名和乱序事件测试**

测试伪造签名拒绝、重复 webhook 幂等、支付成功晚于取消、退款晚于续费等乱序事件最终一致；订单金额、币种和账号不匹配时拒绝入账；发票状态和支付账本可对账。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd services/control-plane && uv run pytest tests/billing/test_payments.py tests/billing/test_reconciliation.py -q`

预期：FAIL，支付模块不存在。

- [ ] **步骤 3：实现签名验证和订单状态机**

支付宝和微信密钥只从 KMS 读取；Webhook 先验证平台签名、时间窗、商户号和金额，再以平台事件 ID 幂等处理。权益只由已确认账本事件产生；退款追加反向账本，不修改历史记录。发票仅保存必要抬头信息并单独加密。

- [ ] **步骤 4：验证沙箱和日终对账**

运行：`cd services/control-plane && uv run pytest tests/billing -q`

预期：支付、退款、重复、乱序、签名失败和日终差异报告全部通过。

- [ ] **步骤 5：Commit**

```bash
git add services/control-plane/src/pdf2md_cloud/billing services/control-plane/tests/billing docs/runbooks/billing-reconciliation.md
git commit -m "feat: reconcile china payments"
```

## 任务 8：补齐运营、安全、基础设施和合规验收

**文件：**
- 创建：`services/control-plane/src/pdf2md_cloud/operations/__init__.py`
- 创建：`services/control-plane/src/pdf2md_cloud/operations/audit.py`
- 创建：`services/control-plane/src/pdf2md_cloud/operations/diagnostics.py`
- 创建：`services/control-plane/src/pdf2md_cloud/operations/routes.py`
- 创建：`services/control-plane/tests/security/test_authorization.py`
- 创建：`services/control-plane/tests/security/test_redaction.py`
- 修改：`services/control-plane/tests/load/gateway_smoke.py`
- 创建：`infra/terraform/cn/`
- 创建：`docs/runbooks/provider-outage.md`
- 创建：`docs/runbooks/security-incident.md`
- 创建：`docs/legal/zh-CN/TERMS.md`
- 创建：`docs/legal/en-US/TERMS.md`
- 创建：`docs/legal/zh-CN/COPYRIGHT_POLICY.md`
- 创建：`docs/legal/en-US/COPYRIGHT_POLICY.md`
- 修改：`.github/workflows/ci.yml`

- [ ] **步骤 1：编写失败的授权和脱敏测试**

测试所有管理 API 默认拒绝，角色和租户隔离；诊断包只含版本、错误码、指标、哈希和用户明确选择的日志；审计日志追加式、防篡改、无正文和 Secret；AI 标识配置必须随成果版本返回。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd services/control-plane && uv run pytest tests/security -q`

预期：FAIL，运营模块不存在。

- [ ] **步骤 3：实现运营边界和中国区 Terraform**

Terraform 建立私有子网、最小 IAM、WAF、容器服务、PostgreSQL 多可用区、Redis、OSS 私有桶、KMS、日志保留、告警和备份；服务指标不带用户内容标签。CI 运行 Migration、SAST、依赖、Secret、许可证、容器扫描和 Terraform validate。

- [ ] **步骤 4：执行 M6 验收**

运行：

```bash
cd services/control-plane
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy --strict src
uv run pytest -q --cov=pdf2md_cloud --cov-fail-under=90
terraform -chdir=../../infra/terraform/cn fmt -check -recursive
terraform -chdir=../../infra/terraform/cn validate
```

预期：全部通过；删除、账本、授权、安全和负载收据齐全。法律文件必须由中国大陆执业律师和隐私/生成式 AI 专业人员签字验收后，M6 才可关闭。

- [ ] **步骤 5：Commit**

```bash
git add services/control-plane/src/pdf2md_cloud/operations services/control-plane/tests/security services/control-plane/tests/load infra/terraform/cn docs/runbooks docs/legal .github/workflows/ci.yml
git commit -m "feat: harden china commercial operations"
```

## M6 完成收据

- [ ] 账号、设备、Refresh Token 轮换和远程注销通过攻击测试。
- [ ] 授权可分项授予和撤回；数据导出、账号删除和云任务删除有回执。
- [ ] 每个 ProviderCall 幂等预留、捕获或释放，失败不扣费。
- [ ] Provider 密钥仅在 KMS/Secret Manager，教材临时对象按策略删除。
- [ ] 支付、退款、发票状态和账本日终对账一致。
- [ ] 中国区基础设施、安全测试、法务和合规验收均有签字收据。
