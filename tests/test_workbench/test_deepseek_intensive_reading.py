import json
import threading

import pytest

from parsing_core.workbench.deepseek import DeepSeekClient, DeepSeekError
from parsing_core.workbench.ocr.deepseek_intensive_reading import (
    MODEL_NAME,
    DeepSeekGenerationError,
    DeepSeekIntensiveReadingGenerator,
    build_generation_prompt,
    prompt_fingerprint,
)
from parsing_core.workbench.ocr.markdown_notes import (
    AcceptedIntensiveReadingNote,
    _digest,
    _register_accepted_note,
    _render_markdown,
)


def _base_note():
    return {
        "schema_version": 1,
        "metadata": {
            "input_fingerprint": "input-1",
            "chapter_fingerprint": "chapter-1",
            "evidence_fingerprint": "evidence-1",
            "prompt_rules_version": "mba-intensive-reading-v1",
            "source_id": "book-1",
            "chapter_id": "ch-1",
            "page_start": 1,
            "page_end": 1,
            "citation_ids": ["[src:book-1:p1:b1]"],
        },
        "sections": [
            {
                "key": "source_evidence",
                "title": "原文证据",
                "content": "- [src:book-1:p1:b1]：需求预测",
                "source_refs": ["[src:book-1:p1:b1]"],
            },
            {"key": "concepts", "title": "核心概念", "content": "占位", "source_refs": []},
            {
                "key": "plain_explain",
                "title": "通俗、有趣、生活化的解释",
                "content": "占位",
                "source_refs": [],
            },
            {"key": "cases", "title": "教材案例解读", "content": "占位", "source_refs": []},
            {
                "key": "problem_solving",
                "title": "实际例子与问题解决",
                "content": "占位",
                "source_refs": [],
            },
            {"key": "applications", "title": "实际应用", "content": "占位", "source_refs": []},
        ],
        "mermaid": [
            {
                "key": "concept_map",
                "title": "知识结构图",
                "type": "flowchart",
                "source": 'flowchart TD\n  A["需求"] --> B["预测"]',
            },
            {
                "key": "application_flow",
                "title": "应用流程图",
                "type": "flowchart",
                "source": 'flowchart LR\n  A["识别"] --> B["行动"]',
            },
        ],
        "markdown": "placeholder",
    }


def _generated(base):
    result = json.loads(json.dumps(base))
    result.pop("markdown", None)
    result["metadata"].pop("note_fingerprint", None)
    result["metadata"]["model"] = MODEL_NAME
    result["metadata"]["prompt_fingerprint"] = "pending"
    result["sections"][1]["content"] = "需求预测回答的是未来需要多少，而不是凭感觉拍脑袋。"
    result["sections"][1]["source_refs"] = result["metadata"]["citation_ids"]
    result["sections"][2]["content"] = "像餐馆备菜：太少会断货，太多会浪费。"
    result["sections"][2]["source_refs"] = result["metadata"]["citation_ids"]
    result["sections"][3]["content"] = "教材案例说明预测误差会影响库存与排产。"
    result["sections"][3]["source_refs"] = result["metadata"]["citation_ids"]
    result["sections"][4]["content"] = "先估计需求，再比较误差，最后调整补货。"
    result["sections"][4]["source_refs"] = result["metadata"]["citation_ids"]
    result["sections"][5]["content"] = "当数据不足时降低结论强度，并持续复盘。"
    result["sections"][5]["source_refs"] = result["metadata"]["citation_ids"]
    result["mermaid"] = [
        {
            "key": "concept_map",
            "title": "知识结构图",
            "type": "flowchart",
            "source": 'flowchart TD\n  A["需求"] --> B["预测"]\n  B --> C["决策"]',
        },
        {
            "key": "application_flow",
            "title": "应用流程图",
            "type": "flowchart",
            "source": 'flowchart LR\n  A["数据"] --> B["判断"]\n  B --> C["行动"]',
        },
    ]
    return result


def _accepted_base_note():
    note = _base_note()
    chapter = {"number": "", "title": "第 1 章 需求预测"}
    note["markdown"] = _render_markdown(
        chapter, note["metadata"], note["sections"], note["mermaid"]
    )
    note["metadata"]["note_fingerprint"] = _digest(
        {
            "metadata": note["metadata"],
            "sections": note["sections"],
            "mermaid": note["mermaid"],
        }
    )
    accepted = AcceptedIntensiveReadingNote(note)
    _register_accepted_note(accepted)
    return accepted


class FakeClient:
    model = MODEL_NAME

    def __init__(self, output):
        self.output = output
        self.calls = []

    def complete(self, prompt, *, timeout=120, max_tokens=4096, cancel_event=None, retries=2):
        self.calls.append((prompt, timeout, max_tokens, cancel_event, retries))
        return self.output


def test_generation_prompt_is_deterministic_and_requires_quality_rules():
    prompt = build_generation_prompt(_base_note())
    assert prompt == build_generation_prompt(_base_note())
    assert "概念通俗、有趣、生活化" in prompt
    assert "案例解读" in prompt
    assert "实际例子" in prompt
    assert "Mermaid" in prompt
    assert prompt_fingerprint(prompt) == prompt_fingerprint(prompt)


def test_generator_rejects_non_canonical_model():
    client = FakeClient("{}")
    client.model = "deepseek-chat"
    with pytest.raises(DeepSeekGenerationError, match="deepseek-v4-pro"):
        DeepSeekIntensiveReadingGenerator(client).generate(_base_note())


def test_generator_validates_output_and_binds_prompt_and_evidence():
    base = _accepted_base_note()
    output = _generated(base)
    prompt = build_generation_prompt(base)
    output["metadata"]["prompt_fingerprint"] = prompt_fingerprint(prompt)
    client = FakeClient(json.dumps(output, ensure_ascii=False))
    result = DeepSeekIntensiveReadingGenerator(client).generate(base)
    assert result["metadata"]["model"] == MODEL_NAME
    assert result["metadata"]["prompt_fingerprint"] == prompt_fingerprint(prompt)
    assert client.calls[0][0] == prompt


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value["metadata"].update({"input_fingerprint": "foreign"}),
        lambda value: value["sections"][0].update({"content": "模型改写后的原文"}),
        lambda value: value["sections"].__setitem__(1, {**value["sections"][1], "content": ""}),
        lambda value: value["mermaid"][0].update({"source": "```\n<script>bad</script>"}),
        lambda value: value["mermaid"][0].update({"key": "application_flow"}),
        lambda value: value["mermaid"][0].update({"title": "被模型修改的标题"}),
        lambda value: value["mermaid"][0].update({"type": "graph"}),
    ],
)
def test_generator_blocks_untrusted_or_incomplete_output(mutator):
    base = _accepted_base_note()
    output = _generated(base)
    prompt = build_generation_prompt(base)
    output["metadata"]["prompt_fingerprint"] = prompt_fingerprint(prompt)
    mutator(output)
    with pytest.raises(DeepSeekGenerationError):
        DeepSeekIntensiveReadingGenerator(
            FakeClient(json.dumps(output, ensure_ascii=False))
        ).generate(base)


def test_generator_reports_cancellation_without_publishing(tmp_path):
    event = threading.Event()
    event.set()
    client = FakeClient("{}")
    output = tmp_path / "note.md"
    with pytest.raises(DeepSeekGenerationError, match="cancelled") as error:
        DeepSeekIntensiveReadingGenerator(client).generate(
            _accepted_base_note(), cancel_event=event, output_path=output
        )
    assert error.value.status == "cancelled"
    assert not output.exists()


def test_generator_rejects_self_consistent_forged_base_before_model_call():
    client = FakeClient("{}")
    with pytest.raises(DeepSeekGenerationError, match="accepted OCR note"):
        DeepSeekIntensiveReadingGenerator(client).generate(_base_note())
    assert client.calls == []


def test_generator_rejects_mutated_registered_base_before_model_call():
    base = _accepted_base_note()
    base["mermaid"][0]["title"] = "被篡改"
    client = FakeClient("{}")
    with pytest.raises(DeepSeekGenerationError, match="accepted OCR note"):
        DeepSeekIntensiveReadingGenerator(client).generate(base)
    assert client.calls == []


def test_client_rejects_non_canonical_model_and_non_https_url():
    with pytest.raises(DeepSeekError, match="deepseek-v4-pro"):
        DeepSeekClient("sk-test", "deepseek-chat")
    with pytest.raises(DeepSeekError, match="HTTPS"):
        DeepSeekClient("sk-test", MODEL_NAME, base_url="http://localhost")
