# M4 章节精读与写作成果实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 自动识别每本教材的完整章节树，按证据生成高质量 MBA 精读笔记、Mermaid 图、卡片、跨教材 TopicMap、贴文和公众号长文草稿。

**架构：** 章节识别采用目录/页眉/编号的第一遍候选和页级覆盖的第二遍校验。精读按“结构、概念、通俗解释、案例、实际问题、应用、Mermaid、卡片、证据审查、教学审查”有序执行；每阶段输出结构化 Artifact，独立审查不读取生成器的隐藏推理，只依据标准文档和证据。

**技术栈：** DeepSeek `deepseek-v4-pro`、Pydantic/JSON Schema、Jinja2 模板、CanonicalDocument、SQLite FTS5、Mermaid 11、React/Tauri WebView、pytest、Vitest、Playwright。

---

## 文件清单

**创建：**

- `src/parsing_core/workbench/domain/reading.py`：ChapterTree、ReadingNote、Concept、Case、Application、EvidenceClaim。
- `src/parsing_core/workbench/domain/topics.py`：TopicMap、TopicLink、CourseCard、WritingDraft。
- `src/parsing_core/workbench/ports/reading.py`：ChapterDetector、ReadingGenerator、EvidenceReviewer、TeachingReviewer、DiagramValidator。
- `src/parsing_core/workbench/application/services/chapter_service.py`：两遍章节检测和覆盖校验。
- `src/parsing_core/workbench/application/services/reading_service.py`：结构化精读阶段协调。
- `src/parsing_core/workbench/application/services/review_service.py`：独立证据与教学审查。
- `src/parsing_core/workbench/application/services/topic_service.py`：跨教材主题融合。
- `src/parsing_core/workbench/application/services/search_service.py`：FTS 与可选向量重排。
- `src/parsing_core/workbench/application/services/writing_service.py`：贴文和长文草稿。
- `src/parsing_core/workbench/application/services/export_service.py`：Markdown、HTML、PDF、DOCX 导出。
- `src/parsing_core/workbench/infrastructure/providers/deepseek_reading.py`：DeepSeek 结构化生成适配器。
- `src/parsing_core/workbench/infrastructure/prompts/zh-CN/*.j2`、`en-US/*.j2`：版本化提示词。
- `src/parsing_core/workbench/infrastructure/reading/markdown.py`：精读/专题/草稿 Markdown。
- `src/parsing_core/workbench/infrastructure/search/hybrid.py`：FTS5 候选和 Embedding 重排。
- `src/parsing_core/workbench/infrastructure/exports/html.py`、`pdf.py`、`docx.py`：带 AI 标识的导出适配器。
- `src/parsing_core/workbench/infrastructure/persistence/sql/0005_reading.sql`：章节、声明、审查、专题和草稿。
- `src/parsing_core/workbench/schemas/reading-note-v1.json`、`evidence-review-v1.json`、`teaching-review-v1.json`、`topic-map-v1.json`、`writing-draft-v1.json`：输出契约。
- `parsing-core-app/src/diagram/MermaidValidationHost.tsx`：隐藏 WebView 渲染主机。
- `parsing-core-app/src/diagram/validation.ts`：安全 Mermaid 渲染与像素摘要。
- `tests/test_workbench/application/test_chapter_service.py`、`test_reading_service.py`、`test_review_service.py`、`test_topic_service.py`、`test_writing_service.py`：应用测试。
- `tests/test_workbench/providers/test_deepseek_reading.py`：模型适配器测试。
- `tests/test_workbench/integration/test_course_reading_e2e.py`：双教材成果 E2E。
- `parsing-core-app/src/diagram/validation.test.tsx`：Mermaid 安全和非空像素测试。

**修改：**

- `pyproject.toml`、`uv.lock`：加入 ReportLab/svglib 导出依赖并接受许可证门禁。
- `src/parsing_core/workbench/chapter_detection.py`、`ocr/chapters.py`：迁入两遍识别门面。
- `src/parsing_core/workbench/pipeline.py`、`topic_pipeline.py`：兼容门面转调新服务。
- `src/parsing_core/workbench/application/pipelines/book.py`：接入 CHAPTERS/READING/TOPICS/WRITING/EXPORT。
- `src/parsing_core/workbench/ocr/deepseek_intensive_reading.py`：转为新 Provider 兼容门面。
- `src/parsing_core/workbench/markdown_sync.py`、`topic_markdown_sync.py`：使用确定性序列化。
- `src/parsing_core/serving/api/routes_chapters.py`、`routes_topics.py`：成果查询和局部重跑。
- `parsing-core-app/src/components/MermaidBlock.tsx`：共用安全渲染函数。

## 任务 1：定义章节、精读和证据领域模型

**文件：**
- 创建：`src/parsing_core/workbench/domain/reading.py`
- 创建：`src/parsing_core/workbench/domain/topics.py`
- 创建：`tests/test_workbench/domain/test_reading.py`

- [ ] **步骤 1：编写失败的不变量测试**

```python
def test_source_claim_requires_block_evidence() -> None:
    with pytest.raises(ValueError, match="reading.evidence_required"):
        EvidenceClaim(
            id="claim-1", text="规模经济降低单位成本", source_block_ids=(),
            inference=False, confidence=1.0,
        )


def test_chapter_children_must_fit_parent_range() -> None:
    with pytest.raises(ValueError, match="chapter.range_outside_parent"):
        ChapterNode(id="c1.1", title="越界", start_page=3, end_page=9, children=()).within(
            ChapterNode(id="c1", title="父章", start_page=1, end_page=8, children=())
        )
```

另测 TopicLink 必须指向两本教材的有效概念或明确同书关系，WritingDraft 必须保存来源声明 ID。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/domain/test_reading.py -q`

预期：FAIL，模型不存在。

- [ ] **步骤 3：实现不可变领域模型**

定义 `ChapterTree`、`ChapterNode`、`ReadingNote`、`ConceptExplanation`、`CaseAnalysis`、`PracticalApplication`、`EvidenceClaim`、`CourseCard`、`TopicMap`、`TopicLink`、`WritingDraft`。每个来源性事实携带 block IDs；推断必须显式 `inference=True` 并列出依据声明。所有成果显式保存 `source_language`、`output_locale`、`artifact.locale`、模板版本和模型追踪，不能从 UI 语言隐式推断。

- [ ] **步骤 4：验证模型和 strict mypy**

运行：

```bash
uv run pytest tests/test_workbench/domain/test_reading.py -q
uv run mypy --strict src/parsing_core/workbench/domain
```

预期：全部通过。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/domain/reading.py src/parsing_core/workbench/domain/topics.py tests/test_workbench/domain/test_reading.py
git commit -m "feat: define evidence-bound reading artifacts"
```

## 任务 2：实现两遍章节识别和页级覆盖校验

**文件：**
- 创建：`src/parsing_core/workbench/ports/reading.py`
- 创建：`src/parsing_core/workbench/application/services/chapter_service.py`
- 创建：`tests/test_workbench/application/test_chapter_service.py`
- 修改：`src/parsing_core/workbench/chapter_detection.py`
- 修改：`src/parsing_core/workbench/ocr/chapters.py`

- [ ] **步骤 1：编写失败的多教材章节测试**

```python
def test_each_textbook_keeps_an_independent_complete_tree(course_documents, service):
    trees = service.detect_all(course_documents)
    assert set(trees) == {"textbook-a", "textbook-b"}
    for source_id, tree in trees.items():
        pages = covered_pages(tree)
        expected = set(range(1, course_documents[source_id].page_count + 1))
        assert pages == expected
        assert no_overlaps_or_gaps(tree)
```

另测目录页码偏移、章首跨页、附录、索引、无目录教材和隔离页阻断。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/application/test_chapter_service.py -q`

预期：现有单遍规则在页码偏移或第二本教材上失败。

- [ ] **步骤 3：实现候选和验证两遍流程**

第一遍合并目录条目、标题层级、页眉页脚、编号模式和版式变化；第二遍按页验证开端/结尾、消除重叠和空洞，保留前言、附录、索引等非章区。隔离页所属章状态为 `BLOCKED`，其他章可继续。

- [ ] **步骤 4：验证章节准确与性能**

运行：

```bash
uv run pytest tests/test_workbench/application/test_chapter_service.py tests/test_workbench/test_chapter_detection.py tests/test_workbench/test_ocr_chapters.py -q
```

预期：全部通过；300 页原生 PDF 在基准机器 90 秒内产生候选。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/ports/reading.py src/parsing_core/workbench/application/services/chapter_service.py src/parsing_core/workbench/chapter_detection.py src/parsing_core/workbench/ocr/chapters.py tests/test_workbench/application/test_chapter_service.py
git commit -m "feat: detect complete textbook chapters"
```

## 任务 3：实现版本化 DeepSeek 结构化精读

**文件：**
- 创建：`src/parsing_core/workbench/infrastructure/providers/deepseek_reading.py`
- 创建：`src/parsing_core/workbench/infrastructure/prompts/zh-CN/reading-note-v1.j2`
- 创建：`src/parsing_core/workbench/infrastructure/prompts/en-US/reading-note-v1.j2`
- 创建：`src/parsing_core/workbench/schemas/reading-note-v1.json`
- 创建：`tests/test_workbench/providers/test_deepseek_reading.py`
- 修改：`src/parsing_core/workbench/ocr/deepseek_intensive_reading.py`

- [ ] **步骤 1：编写失败的模型和 Schema 测试**

```python
async def test_reading_provider_rejects_wrong_model(provider_factory):
    with pytest.raises(ValueError, match="provider.model_not_allowed"):
        provider_factory(model="deepseek-chat")


async def test_reading_request_contains_only_chapter_evidence(provider, fake_http, chapter_packet):
    await provider.generate(chapter_packet, locale="zh-CN")
    body = fake_http.requests[0].json
    assert body["model"] == "deepseek-v4-pro"
    assert "other_chapter_text" not in str(body)
```

另测 JSON Schema 错误、截断、内容注入和超出引用范围时拒绝结果。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/providers/test_deepseek_reading.py -q`

预期：FAIL，新 Provider 和 Schema 不存在。

- [ ] **步骤 3：实现结构化生成适配器**

固定模型 `deepseek-v4-pro`；请求只包含章节块、证据索引、课程目标、输出语言和模板版本。Schema 要求摘要、概念、通俗生活化解释、案例解读、实际问题求解、应用建议、Mermaid 源码、卡片和逐项 evidence block IDs。模型文本永不作为工具指令执行。

- [ ] **步骤 4：验证 Provider 契约**

运行：`uv run pytest tests/test_workbench/providers/test_deepseek_reading.py tests/test_workbench/test_deepseek_intensive_reading.py -q`

预期：全部通过，旧生成器通过兼容门面使用同一 Schema。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/infrastructure/providers/deepseek_reading.py src/parsing_core/workbench/infrastructure/prompts src/parsing_core/workbench/schemas/reading-note-v1.json src/parsing_core/workbench/ocr/deepseek_intensive_reading.py tests/test_workbench/providers/test_deepseek_reading.py
git commit -m "feat: generate structured mba reading notes"
```

## 任务 4：实施独立证据审查与教学质量审查

**文件：**
- 创建：`src/parsing_core/workbench/application/services/review_service.py`
- 创建：`src/parsing_core/workbench/schemas/evidence-review-v1.json`
- 创建：`src/parsing_core/workbench/schemas/teaching-review-v1.json`
- 创建：`src/parsing_core/workbench/infrastructure/prompts/zh-CN/evidence-review-v1.j2`
- 创建：`src/parsing_core/workbench/infrastructure/prompts/en-US/evidence-review-v1.j2`
- 创建：`src/parsing_core/workbench/infrastructure/prompts/zh-CN/teaching-review-v1.j2`
- 创建：`src/parsing_core/workbench/infrastructure/prompts/en-US/teaching-review-v1.j2`
- 创建：`tests/test_workbench/application/test_review_service.py`

- [ ] **步骤 1：编写失败的独立性和阻断测试**

```python
async def test_unverified_claim_blocks_publication(review_service, generated_note, evidence):
    generated_note.claims[0] = replace(generated_note.claims[0], source_block_ids=("missing",))
    result = await review_service.review(generated_note, evidence)
    assert result.publishable is False
    assert result.findings[0].code == "reading.claim_without_evidence"
```

测试审查调用使用不同 `provider_call_id`、独立提示词和独立上下文；教学审查必须逐项评价通俗性、趣味性、生活化、案例、求解、应用与认知负荷。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/application/test_review_service.py -q`

预期：FAIL，审查服务不存在。

- [ ] **步骤 3：实现双审与有限修订循环**

先本地验证所有 evidence IDs 和数字一致性，再调用证据审查；通过后调用教学审查。每类问题最多触发 2 次定向修订，修订只接收发现和相关证据；仍不通过则章节 `BLOCKED`，不发布低质量笔记。

- [ ] **步骤 4：验证审查和重试上限**

运行：`uv run pytest tests/test_workbench/application/test_review_service.py -q --count=10`

预期：无随机死循环；来源性事实无证据率为 0；最多 2 次修订。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/application/services/review_service.py src/parsing_core/workbench/schemas/evidence-review-v1.json src/parsing_core/workbench/schemas/teaching-review-v1.json src/parsing_core/workbench/infrastructure/prompts tests/test_workbench/application/test_review_service.py
git commit -m "feat: independently review reading quality"
```

## 任务 5：建立 Mermaid 真实渲染门禁

**文件：**
- 创建：`parsing-core-app/src/diagram/MermaidValidationHost.tsx`
- 创建：`parsing-core-app/src/diagram/validation.ts`
- 创建：`parsing-core-app/src/diagram/validation.test.tsx`
- 创建：`tests/test_workbench/application/test_reading_service.py`
- 修改：`parsing-core-app/src/components/MermaidBlock.tsx`
- 创建：`src/parsing_core/workbench/application/services/reading_service.py`
- 修改：`src/parsing_core/serving/api/routes_jobs.py`

- [ ] **步骤 1：编写失败的语法、安全和像素测试**

```typescript
it("rejects script links and accepts a nonblank flowchart", async () => {
  await expect(validateMermaid('flowchart LR; A["教材"] --> B["应用"]')).resolves.toMatchObject({
    valid: true,
    nonBlankPixels: expect.any(Number),
  });
  await expect(validateMermaid('flowchart LR; A["<script>alert(1)</script>"]')).resolves.toMatchObject({
    valid: false,
    code: "diagram.unsafe_content",
  });
});
```

测试还需断言 `nonBlankPixels >= 100`、SVG 尺寸非零、无外部 URL、无 `foreignObject` 和点击回调。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd parsing-core-app && npm test -- src/diagram/validation.test.tsx`

预期：FAIL，验证模块不存在。

- [ ] **步骤 3：实现共用安全渲染器和异步回执**

`validateMermaid()` 固定 `securityLevel: "strict"`、禁用 HTML labels、最大源码 20 KiB、最大节点 200、2 秒超时，渲染到隔离 DOM 后清洗 SVG并计算非空像素摘要。后台 ValidationHost 从本地 Job API 领取 `DIAGRAM_VALIDATION` 步骤并提交 `{valid, code, svg_hash, width, height, non_blank_pixels}`；精读 Job 只有收到成功回执才发布 Markdown。

- [ ] **步骤 4：验证前端和应用流程**

运行：

```bash
(cd parsing-core-app && npm test -- src/diagram/validation.test.tsx src/components/MermaidBlock.test.tsx)
uv run pytest tests/test_workbench/application/test_reading_service.py -k mermaid -q
```

预期：所有有效图实际渲染，恶意/空白/超限图被阻断并触发定向修订。

- [ ] **步骤 5：Commit**

```bash
git add parsing-core-app/src/diagram parsing-core-app/src/components/MermaidBlock.tsx src/parsing_core/workbench/application/services/reading_service.py src/parsing_core/serving/api/routes_jobs.py tests/test_workbench/application/test_reading_service.py
git commit -m "feat: gate notes on rendered mermaid"
```

## 任务 6：编排完整精读阶段并确定性输出 Markdown

**文件：**
- 修改：`src/parsing_core/workbench/application/services/reading_service.py`
- 创建：`src/parsing_core/workbench/infrastructure/reading/markdown.py`
- 创建：`src/parsing_core/workbench/infrastructure/persistence/sql/0005_reading.sql`
- 修改：`tests/test_workbench/application/test_reading_service.py`
- 修改：`src/parsing_core/workbench/application/pipelines/book.py`
- 修改：`src/parsing_core/workbench/pipeline.py`
- 修改：`src/parsing_core/workbench/markdown_sync.py`

- [ ] **步骤 1：编写失败的阶段顺序与局部重跑测试**

测试阶段固定为 `STRUCTURE, CONCEPTS, PLAIN_EXPLANATION, CASES, PRACTICAL_SOLUTION, APPLICATION, MERMAID, CARDS, EVIDENCE_REVIEW, TEACHING_REVIEW, PUBLISH`。修改一个概念后，只重跑受影响阶段和审查，不重复 OCR/章节；最终 Markdown 必含 Front Matter、证据索引和 Mermaid fenced block。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/application/test_reading_service.py -q`

预期：FAIL，新服务不存在。

- [ ] **步骤 3：实现阶段 Artifact 和依赖哈希**

每阶段输入哈希由章节版本、模板版本、模型版本、前一阶段 Artifact 和输出语言组成。阶段输出先保存草稿，双审和 Mermaid 通过后才原子更新 `published_revision_id`；旧 pipeline 只转调新服务。

- [ ] **步骤 4：验证精读稳定性**

运行：`uv run pytest tests/test_workbench/application/test_reading_service.py tests/test_workbench/test_pipeline.py tests/test_workbench/test_markdown_sync.py -q`

预期：全部通过，重复运行命中缓存且 Markdown 无差异。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/application/services/reading_service.py src/parsing_core/workbench/infrastructure/reading src/parsing_core/workbench/infrastructure/persistence/sql/0005_reading.sql src/parsing_core/workbench/application/pipelines/book.py src/parsing_core/workbench/pipeline.py src/parsing_core/workbench/markdown_sync.py tests/test_workbench/application/test_reading_service.py
git commit -m "feat: run evidence-gated chapter reading"
```

## 任务 7：生成卡片、TopicMap 与写作草稿

**文件：**
- 创建：`src/parsing_core/workbench/application/services/topic_service.py`
- 创建：`src/parsing_core/workbench/application/services/search_service.py`
- 创建：`src/parsing_core/workbench/application/services/writing_service.py`
- 创建：`src/parsing_core/workbench/infrastructure/search/__init__.py`
- 创建：`src/parsing_core/workbench/infrastructure/search/hybrid.py`
- 创建：`src/parsing_core/workbench/schemas/topic-map-v1.json`
- 创建：`src/parsing_core/workbench/schemas/writing-draft-v1.json`
- 创建：`tests/test_workbench/application/test_topic_service.py`
- 创建：`tests/test_workbench/application/test_search_service.py`
- 创建：`tests/test_workbench/application/test_writing_service.py`
- 修改：`src/parsing_core/workbench/topic_pipeline.py`
- 修改：`src/parsing_core/workbench/topic_markdown_sync.py`

- [ ] **步骤 1：编写失败的跨教材与版权测试**

```python
def test_topic_map_links_concepts_without_merging_chapter_trees(topic_service, two_books):
    result = topic_service.build(two_books)
    assert result.links
    assert {link.left.source_id for link in result.links} == {"book-a"}
    assert {link.right.source_id for link in result.links} == {"book-b"}
    assert two_books[0].chapter_tree != two_books[1].chapter_tree


def test_long_quote_is_rejected_by_copyright_gate(writing_service, source):
    with pytest.raises(CopyrightGateError):
        writing_service.publish(draft_with_contiguous_quote(source, chars=400))


def test_hybrid_search_uses_fts_then_optional_embedding_rerank(search_service):
    results = search_service.search("如何做价格决策", locale="zh-CN", limit=20)
    assert results[0].concept_id == "pricing-decision"
    assert all(result.source_block_ids for result in results)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/application/test_topic_service.py tests/test_workbench/application/test_writing_service.py -q`

预期：FAIL，新服务不存在。

- [ ] **步骤 3：实现专题、卡片和两种草稿模板**

TopicMap 只引用已发布概念和证据；关系类型限制为 `SAME_CONCEPT`、`COMPLEMENTS`、`CONTRADICTS`、`APPLIES_TO`、`PREREQUISITE`。`SearchService` 先用 SQLite FTS5 将 10 万块缩到最多 500 个候选，再按已配置 `EMBEDDING` 能力可选重排；向量响应按文本哈希和模型版本缓存，缺少向量 Provider 时保持可解释 FTS 排序。写作服务输出贴文与公众号长文，保存声明 ID、模型追踪和 AI 辅助标识；连续原文、累计原文比例和跨章节还原触发版权门禁。

- [ ] **步骤 4：验证专题和写作产物**

运行：`uv run pytest tests/test_workbench/application/test_topic_service.py tests/test_workbench/application/test_search_service.py tests/test_workbench/application/test_writing_service.py tests/test_workbench/test_topic_pipeline.py -q`

预期：全部通过，草稿可追溯且不批量还原教材原文。

- [ ] **步骤 5：Commit**

```bash
git add src/parsing_core/workbench/application/services/topic_service.py src/parsing_core/workbench/application/services/search_service.py src/parsing_core/workbench/application/services/writing_service.py src/parsing_core/workbench/infrastructure/search src/parsing_core/workbench/schemas/topic-map-v1.json src/parsing_core/workbench/schemas/writing-draft-v1.json src/parsing_core/workbench/topic_pipeline.py src/parsing_core/workbench/topic_markdown_sync.py tests/test_workbench/application/test_topic_service.py tests/test_workbench/application/test_search_service.py tests/test_workbench/application/test_writing_service.py
git commit -m "feat: create cross-book writing artifacts"
```

## 任务 8：双教材完整精读 E2E

**文件：**
- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 创建：`src/parsing_core/workbench/application/services/export_service.py`
- 创建：`src/parsing_core/workbench/infrastructure/exports/__init__.py`
- 创建：`src/parsing_core/workbench/infrastructure/exports/html.py`
- 创建：`src/parsing_core/workbench/infrastructure/exports/pdf.py`
- 创建：`src/parsing_core/workbench/infrastructure/exports/docx.py`
- 创建：`tests/test_workbench/application/test_export_service.py`
- 创建：`tests/test_workbench/integration/test_course_reading_e2e.py`
- 修改：`src/parsing_core/serving/api/routes_chapters.py`
- 修改：`src/parsing_core/serving/api/routes_topics.py`

- [ ] **步骤 1：编写失败的课程级 E2E**

导入两本合成教材，运行完整 BookPipeline，断言两棵章节树、所有章节笔记、每章至少一个 Mermaid、卡片池、跨教材 TopicMap、贴文和长文草稿；将成果导出为 Markdown、HTML、PDF、DOCX，断言可见 AI 说明、模型/模板元数据、来源索引和实际 Mermaid 图均存在。随机断开网络和重启进程后继续，ProviderCall 不重复。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_workbench/integration/test_course_reading_e2e.py -q`

预期：至少一个成果或恢复断言失败。

- [ ] **步骤 3：补齐查询 DTO 与局部重跑命令**

章节、声明、审查、图、卡片、专题和草稿均提供分页查询；用户编辑块保留原始版本并触发最小依赖重算。将 `reportlab>=4,<5` 与 `svglib>=1.5,<2` 加入锁文件。`ExportService` 从同一 Artifact 构造四种格式：HTML 对所有文本转义，PDF 使用 ReportLab/svglib 和系统中文字体，DOCX 使用 python-docx，三者嵌入已经验证的 Mermaid SVG；任何格式都不能删除 AI 标识或证据元数据。API 返回稳定错误码，不返回本地路径或供应商原始响应。

- [ ] **步骤 4：执行 M4 验收**

运行：

```bash
uv run pytest tests/test_workbench/domain/test_reading.py tests/test_workbench/application/test_chapter_service.py tests/test_workbench/application/test_reading_service.py tests/test_workbench/application/test_review_service.py tests/test_workbench/application/test_topic_service.py tests/test_workbench/application/test_search_service.py tests/test_workbench/application/test_writing_service.py tests/test_workbench/application/test_export_service.py tests/test_workbench/integration/test_course_reading_e2e.py -q
(cd parsing-core-app && npm test -- src/diagram/validation.test.tsx src/components/MermaidBlock.test.tsx)
./scripts/verify-fast.sh
```

预期：全部通过，来源性事实无证据率 0，Mermaid 解析与实际渲染成功率 100%。

- [ ] **步骤 5：Commit**

```bash
git add pyproject.toml uv.lock src/parsing_core/workbench/application/services/export_service.py src/parsing_core/workbench/infrastructure/exports src/parsing_core/serving/api/routes_chapters.py src/parsing_core/serving/api/routes_topics.py tests/test_workbench/application/test_export_service.py tests/test_workbench/integration/test_course_reading_e2e.py
git commit -m "test: verify complete multi-book reading flow"
```

## M4 完成收据

- [ ] 每本教材有独立、完整、无重叠空洞的章节树。
- [ ] 每章精读包含通俗解释、案例、实际求解、应用、卡片和可直接预览 Mermaid。
- [ ] 证据审查和教学审查独立执行，失败不会发布。
- [ ] TopicMap 连接两本教材但不合并章节树。
- [ ] 贴文和公众号草稿保存证据、版本、模型与 AI 标识。
