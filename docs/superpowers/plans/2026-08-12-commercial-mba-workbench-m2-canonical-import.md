# M2 多格式导入与 CanonicalDocument 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 PDF、DOCX、PPTX、XLSX 和图片通过真实适配器转换为可追溯 `CanonicalDocument`，并确定性生成 Markdown、页图和证据索引。

**架构：** 每种格式适配器实现同一 `DocumentAdapter` 端口，输出不可变块级 AST 和来源坐标。标准化产物按源文件哈希、适配器版本和配置哈希缓存，写入工作区前使用临时目录和原子重命名。

**技术栈：** pypdf、PyMuPDF、python-docx、python-pptx、openpyxl、Pillow、pillow-heif、Pydantic、SQLite、Markdown、pytest、Hypothesis。

---

## 文件清单

**创建：**

- `src/parsing_core/workbench/domain/document.py`：CanonicalDocument、Page、Block、Span、SourceRef。
- `src/parsing_core/workbench/ports/documents.py`：DocumentAdapter、DocumentRenderer、DocumentSerializer。
- `src/parsing_core/workbench/infrastructure/documents/registry.py`：格式嗅探与适配器注册。
- `src/parsing_core/workbench/infrastructure/documents/pdf.py`：PDF 文本、页图和几何。
- `src/parsing_core/workbench/infrastructure/documents/docx.py`：段落、标题、表格和图片。
- `src/parsing_core/workbench/infrastructure/documents/pptx.py`：幻灯片、形状、表格和备注。
- `src/parsing_core/workbench/infrastructure/documents/xlsx.py`：工作表、单元格、公式和表格。
- `src/parsing_core/workbench/infrastructure/documents/images.py`：图片页面和元数据。
- `src/parsing_core/workbench/infrastructure/documents/markdown.py`：确定性 Markdown 序列化。
- `src/parsing_core/workbench/infrastructure/filesystem/artifact_store.py`：原子 Artifact 存储和 LRU。
- `src/parsing_core/workbench/infrastructure/persistence/sql/0003_documents.sql`：修订、文档、块和来源表。
- `tests/fixtures/documents/build_fixtures.py`：生成五类可复现 fixture。
- `tests/test_workbench/documents/test_document_model.py`：AST 不变量。
- `tests/test_workbench/documents/test_registry.py`：MIME/魔数识别。
- `tests/test_workbench/documents/test_pdf_adapter.py`、`test_docx_adapter.py`、`test_pptx_adapter.py`、`test_xlsx_adapter.py`、`test_image_adapter.py`：适配器测试。
- `tests/test_workbench/documents/test_markdown_serializer.py`：序列化金标准。
- `tests/test_workbench/integration/test_multiformat_import.py`：格式 E2E。

**修改：**

- `pyproject.toml`、`uv.lock`：加入固定范围的格式库和 fixture 生成依赖。
- `src/parsing_core/workbench/application/pipelines/book.py`：接入 INGEST/NORMALIZE 步骤。
- `src/parsing_core/workbench/source_import.py`：退化为适配器门面。
- `src/parsing_core/workbench/repository.py`：提供旧查询兼容。
- `parsing-core-app/src-tauri/src/main.rs`：只允许真实支持的现代扩展名。
- `parsing-core-app/src/components/workbench/ImportTextbooks.tsx`：展示格式和逐文件错误。
- `scripts/check-release-sidecar.sh`：检查所有运行时依赖。

## 任务 1：定义 CanonicalDocument 不变量

**文件：**
- 创建：`src/parsing_core/workbench/domain/document.py`
- 创建：`tests/test_workbench/documents/test_document_model.py`

- [ ] **步骤 1：编写失败的页覆盖与来源属性测试**

```python
from hypothesis import given, strategies as st
import pytest

from parsing_core.workbench.domain.document import Block, BlockKind, SourceRef


@given(st.integers(min_value=1), st.integers(min_value=0))
def test_source_ref_requires_positive_page_and_non_negative_offsets(page, offset):
    ref = SourceRef(page=page, start=offset, end=offset, region=None)
    assert ref.page >= 1
    assert ref.start <= ref.end


def test_text_block_requires_source_evidence():
    with pytest.raises(ValueError, match="source_ref_required"):
        Block(id="b1", kind=BlockKind.PARAGRAPH, text="正文", source_refs=())
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/documents/test_document_model.py -q`

预期：FAIL，领域模型不存在。

- [ ] **步骤 3：实现不可变 AST**

定义 `DocumentFormat`、`BlockKind`、`BoundingBox`、`SourceRef`、`Span`、`TableCell`、`Block`、`Page`、`CanonicalDocument`。每个文本/表格/公式块至少有一个来源；页号从 1 连续；块 ID 为内容和来源的稳定哈希；领域模型不导入解析库或文件系统。

- [ ] **步骤 4：验证模型属性和类型**

运行：

```bash
uv run pytest tests/test_workbench/documents/test_document_model.py -q
uv run mypy --strict src/parsing_core/workbench/domain/document.py
```

预期：全部通过。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/domain/document.py tests/test_workbench/documents/test_document_model.py
git commit -m "feat: define canonical document ast"
```

## 任务 2：建立格式嗅探和适配器端口

**文件：**
- 创建：`src/parsing_core/workbench/ports/documents.py`
- 创建：`src/parsing_core/workbench/infrastructure/documents/__init__.py`
- 创建：`src/parsing_core/workbench/infrastructure/documents/registry.py`
- 创建：`tests/test_workbench/documents/test_registry.py`
- 修改：`parsing-core-app/src-tauri/src/main.rs`

- [ ] **步骤 1：编写失败的伪扩展名测试**

```python
def test_registry_uses_signature_not_only_extension(registry, tmp_path):
    fake = tmp_path / "book.pdf"
    fake.write_bytes(b"PK\x03\x04not-a-pdf")
    with pytest.raises(UnsupportedDocumentError) as exc:
        registry.resolve(fake)
    assert exc.value.code == "document.signature_mismatch"
```

Rust 测试断言选择器仅包含 `pdf/docx/pptx/xlsx/png/jpg/jpeg/tiff/tif/heic`，不包含旧式 `doc/ppt/xls`。

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/test_workbench/documents/test_registry.py -q
cd parsing-core-app/src-tauri && cargo test
```

预期：注册表不存在或旧格式白名单测试失败。

- [ ] **步骤 3：实现端口和魔数检测**

`DocumentAdapter` 暴露 `format`、`probe(path)`、`normalize(path, context)`；注册表先验证文件大小和魔数，再解析 ZIP 容器中的 Office content type。错误只返回稳定代码和脱敏参数。

- [ ] **步骤 4：验证格式选择与恶意输入**

运行：`uv run pytest tests/test_workbench/documents/test_registry.py tests/test_security.py -q`

预期：扩展名欺骗、ZIP 炸弹、超限文件和未知格式均在解析前拒绝。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/ports/documents.py src/parsing_core/workbench/infrastructure/documents parsing-core-app/src-tauri/src/main.rs tests/test_workbench/documents/test_registry.py
git commit -m "feat: register verified document adapters"
```

## 任务 3：实现 PDF 原生提取与稳定页图

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/documents/pdf.py`
- 创建：`tests/test_workbench/documents/test_pdf_adapter.py`
- 创建：`tests/fixtures/documents/build_fixtures.py`
- 创建：`tests/test_workbench/integration/test_multiformat_import.py`
- 修改：`pyproject.toml`
- 修改：`uv.lock`

- [ ] **步骤 1：编写失败的 PDF 几何测试**

```python
def test_pdf_adapter_preserves_page_text_order_and_coordinates(pdf_fixture, pdf_adapter):
    document = pdf_adapter.normalize(pdf_fixture, context())
    assert document.page_count == 3
    assert [b.text for b in document.pages[0].blocks][:2] == ["第一章", "运营决策"]
    assert all(ref.region is not None for b in document.pages[0].blocks for ref in b.source_refs)
    assert document.pages[1].needs_ocr is True
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/documents/test_pdf_adapter.py -q`

预期：FAIL，PDF 适配器不存在。

- [ ] **步骤 3：实现页面流式解析**

使用 pypdf 读取元数据和页数，PyMuPDF 按块提取文字与 bbox、按需以 300 DPI 渲染 OCR 原图、以 144 DPI 生成 UI 图。每页独立产生结果并立即释放 pixmap；文本密度低于阈值或字符映射异常时标记 `needs_ocr`，不将坏文本写成已验证正文。

- [ ] **步骤 4：验证 PDF 适配器和性能样本**

运行：

```bash
uv run pytest tests/test_workbench/documents/test_pdf_adapter.py -q
uv run pytest tests/test_workbench/integration/test_multiformat_import.py -k pdf -q
```

预期：全部通过，fixture 页序和坐标稳定，测试进程峰值内存有上限断言。

- [ ] **步骤 5：Commit**

```bash
git add pyproject.toml uv.lock src/parsing_core/workbench/infrastructure/documents/pdf.py tests/fixtures/documents/build_fixtures.py tests/test_workbench/documents/test_pdf_adapter.py tests/test_workbench/integration/test_multiformat_import.py
git commit -m "feat: normalize native pdf pages"
```

## 任务 4：实现 DOCX 与 PPTX 适配器

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/documents/docx.py`
- 创建：`src/parsing_core/workbench/infrastructure/documents/pptx.py`
- 创建：`tests/test_workbench/documents/test_docx_adapter.py`
- 创建：`tests/test_workbench/documents/test_pptx_adapter.py`
- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 修改：`tests/fixtures/documents/build_fixtures.py`

- [ ] **步骤 1：编写失败的结构保真测试**

DOCX 断言标题层级、段落、合并单元格、脚注引用和图片顺序；PPTX 断言幻灯片顺序、文本框 z-order、表格、演讲者备注和嵌入图片来源。

```python
def test_pptx_adapter_keeps_slide_and_notes(pptx_fixture, pptx_adapter):
    document = pptx_adapter.normalize(pptx_fixture, context())
    assert document.pages[0].label == "Slide 1"
    assert any(b.kind is BlockKind.NOTE for b in document.pages[0].blocks)
    assert all(ref.page == 1 for b in document.pages[0].blocks for ref in b.source_refs)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/documents/test_docx_adapter.py tests/test_workbench/documents/test_pptx_adapter.py -q`

预期：FAIL，适配器不存在。

- [ ] **步骤 3：实现 Office 结构映射**

使用 `python-docx` 和 `python-pptx`，读取 XML 顺序而非只遍历高层集合；生成逻辑页与结构来源。无法稳定定位到物理页的 DOCX 块使用 `source_part`、段落/表格索引和字符偏移，不伪造 PDF 坐标。

- [ ] **步骤 4：验证两类适配器**

运行：`uv run pytest tests/test_workbench/documents/test_docx_adapter.py tests/test_workbench/documents/test_pptx_adapter.py -q`

预期：全部通过，重复运行 AST JSON 字节完全一致。

- [ ] **步骤 5：Commit**

```bash
git add pyproject.toml uv.lock src/parsing_core/workbench/infrastructure/documents/docx.py src/parsing_core/workbench/infrastructure/documents/pptx.py tests/fixtures/documents/build_fixtures.py tests/test_workbench/documents/test_docx_adapter.py tests/test_workbench/documents/test_pptx_adapter.py
git commit -m "feat: normalize word and powerpoint sources"
```

## 任务 5：实现 XLSX 与图片适配器

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/documents/xlsx.py`
- 创建：`src/parsing_core/workbench/infrastructure/documents/images.py`
- 创建：`tests/test_workbench/documents/test_xlsx_adapter.py`
- 创建：`tests/test_workbench/documents/test_image_adapter.py`
- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 修改：`tests/fixtures/documents/build_fixtures.py`

- [ ] **步骤 1：编写失败的公式与图像测试**

```python
def test_xlsx_adapter_preserves_formula_and_display_value(xlsx_fixture, xlsx_adapter):
    document = xlsx_adapter.normalize(xlsx_fixture, context())
    cell = document.pages[0].blocks[0].table.rows[1].cells[2]
    assert cell.formula == "=A2*B2"
    assert cell.display_value == "120"


def test_image_adapter_normalizes_orientation(image_fixture, image_adapter):
    document = image_adapter.normalize(image_fixture, context())
    assert document.pages[0].width > document.pages[0].height
    assert document.pages[0].needs_ocr is True
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/documents/test_xlsx_adapter.py tests/test_workbench/documents/test_image_adapter.py -q`

预期：FAIL，适配器不存在。

- [ ] **步骤 3：实现工作表和图片映射**

XLSX 使用 `openpyxl` 同时加载公式视图和缓存值视图，fixture 生成器直接写入带公式与缓存值的 Open XML，保留 sheet、range、合并单元格、隐藏行列标记和数值类型。图片使用 Pillow 和 `pillow-heif` 应用 EXIF 方向、验证像素上限、转为无损 PNG，原图哈希与转换哈希都写入来源。

- [ ] **步骤 4：验证 XLSX 与图片安全边界**

运行：`uv run pytest tests/test_workbench/documents/test_xlsx_adapter.py tests/test_workbench/documents/test_image_adapter.py tests/test_security.py -q`

预期：全部通过，超大像素、损坏图片和外部链接工作簿被稳定拒绝。

- [ ] **步骤 5：Commit**

```bash
git add pyproject.toml uv.lock src/parsing_core/workbench/infrastructure/documents/xlsx.py src/parsing_core/workbench/infrastructure/documents/images.py tests/fixtures/documents/build_fixtures.py tests/test_workbench/documents/test_xlsx_adapter.py tests/test_workbench/documents/test_image_adapter.py
git commit -m "feat: normalize spreadsheet and image sources"
```

## 任务 6：持久化文档、来源与原子 Artifact

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/persistence/sql/0003_documents.sql`
- 创建：`src/parsing_core/workbench/infrastructure/filesystem/artifact_store.py`
- 创建：`tests/test_workbench/infrastructure/test_artifact_store.py`
- 修改：`src/parsing_core/workbench/infrastructure/persistence/migrations.py`

- [ ] **步骤 1：编写失败的断电原子性测试**

```python
def test_failed_commit_never_exposes_partial_artifact(store, document, monkeypatch):
    monkeypatch.setattr(store, "_atomic_replace", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(ArtifactWriteError):
        store.put_document(document)
    assert store.list_visible(document.source_id) == []
```

另测同内容哈希复用、已发布成果不被 LRU 删除、数据库引用与文件哈希一致。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/infrastructure/test_artifact_store.py -q`

预期：FAIL，ArtifactStore 不存在。

- [ ] **步骤 3：实现版本表和原子目录提交**

新增 `wb_source_revisions`、`wb_documents`、`wb_document_blocks`、`wb_source_refs`。Artifact 先写 `<workspace>/.staging/<uuid>`，执行 `fsync` 后原子重命名到哈希路径，再在短事务中登记；启动时清理无引用 staging，绝不删除 `published=1` 或 evidence 类型。

- [ ] **步骤 4：验证原子性和 LRU 上限**

运行：`uv run pytest tests/test_workbench/infrastructure/test_artifact_store.py tests/test_workbench/infrastructure/test_migrations.py -q`

预期：全部通过，故障注入后无半成品可见。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/infrastructure/persistence src/parsing_core/workbench/infrastructure/filesystem/artifact_store.py tests/test_workbench/infrastructure/test_artifact_store.py
git commit -m "feat: persist canonical source artifacts"
```

## 任务 7：生成确定性 Markdown 和证据索引

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/documents/markdown.py`
- 创建：`tests/test_workbench/documents/test_markdown_serializer.py`
- 修改：`src/parsing_core/workbench/markdown_sync.py`

- [ ] **步骤 1：编写失败的金标准测试**

```python
def test_markdown_is_deterministic_and_traceable(document_fixture, serializer):
    first = serializer.serialize(document_fixture)
    second = serializer.serialize(document_fixture)
    assert first.markdown == second.markdown
    assert first.evidence_index == second.evidence_index
    assert "source_id:" in first.markdown
    assert "block_id:" in first.markdown
    assert set(first.evidence_index) == {b.id for p in document_fixture.pages for b in p.blocks}
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/documents/test_markdown_serializer.py -q`

预期：FAIL，序列化器不存在。

- [ ] **步骤 3：实现块级确定性序列化**

Front Matter 使用固定键顺序，正文按 page/order/block 排序；表格、公式、图片、注释分别序列化；每个块添加不可见证据标记并在相邻 JSON 写入 source refs。禁止把原始解析对象直接转字符串。

- [ ] **步骤 4：验证金标准和旧 Markdown 兼容**

运行：`uv run pytest tests/test_workbench/documents/test_markdown_serializer.py tests/test_workbench/test_markdown_sync.py -q`

预期：全部通过，重复运行无 diff。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/infrastructure/documents/markdown.py src/parsing_core/workbench/markdown_sync.py tests/test_workbench/documents/test_markdown_serializer.py
git commit -m "feat: serialize traceable markdown"
```

## 任务 8：接入 BookPipeline 并执行五格式 E2E

**文件：**
- 修改：`tests/test_workbench/integration/test_multiformat_import.py`
- 修改：`src/parsing_core/workbench/application/pipelines/book.py`
- 修改：`src/parsing_core/workbench/source_import.py`
- 修改：`src/parsing_core/workbench/repository.py`
- 修改：`parsing-core-app/src/components/workbench/ImportTextbooks.tsx`
- 修改：`parsing-core-app/src/components/workbench/ImportTextbooks.test.tsx`
- 修改：`scripts/check-release-sidecar.sh`

- [ ] **步骤 1：编写失败的五格式流水线测试**

参数化五类 fixture，提交导入 Job，运行至 `CHAPTERS` 前，断言每个来源产生 AST、Markdown、证据索引和页图；第二次运行 Artifact 命中率为 100%，不重复解析。API 测试断言缺少 `rights_attested=true` 时返回 `source.rights_attestation_required`，前端测试断言用户确认合法使用权后才能提交。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/integration/test_multiformat_import.py -q`

预期：FAIL，BookPipeline 尚未连接适配器。

- [ ] **步骤 3：连接 INGEST/NORMALIZE 与 UI 状态**

流水线根据注册表选择适配器，逐文件创建 SourceRevision；SourceRevision 保存确认时间、客户端版本和声明版本，不保存多余身份信息。单个文件失败不回滚同批其他文件，UI 展示每本教材的格式、页数、状态和稳定错误码。旧 `source_import.py` 只保留输入兼容和新服务调用。

- [ ] **步骤 4：执行 M2 验收**

运行：

```bash
uv run pytest tests/test_workbench/documents tests/test_workbench/integration/test_multiformat_import.py -q
./scripts/verify-fast.sh
cd parsing-core-app && npm run tauri build && cd ..
./scripts/check-release-sidecar.sh parsing-core-app/src-tauri/target/release/bundle/macos/PDF2MD.app
```

预期：五格式通过；真实安装包内可导入每类 fixture；第二次运行缓存复用率 100%。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/application/pipelines/book.py src/parsing_core/workbench/source_import.py src/parsing_core/workbench/repository.py parsing-core-app/src/components/workbench/ImportTextbooks.tsx parsing-core-app/src/components/workbench/ImportTextbooks.test.tsx scripts/check-release-sidecar.sh tests/test_workbench/integration/test_multiformat_import.py
git commit -m "feat: run all formats through canonical import"
```

## M2 完成收据

- [ ] PDF、DOCX、PPTX、XLSX 和图片均由真实适配器处理，不使用扩展名伪支持。
- [ ] 每个 Canonical 块都有来源，页级覆盖率为 100%。
- [ ] Markdown 和证据索引重复生成字节一致。
- [ ] 故障注入不暴露半成品，缓存不会删除证据和已发布成果。
- [ ] 安装包内五格式冒烟通过。
