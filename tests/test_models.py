from parsing_core.models.dataclasses import AIArtifact, Section, Task


def test_task_creation():
    t = Task(
        id="t1",
        file_path="/a/b.xlsx",
        snapshot_path="/tmp/a.xlsx",
        file_sha256="abc",
        status="PENDING",
        model_tier="stub",
    )
    assert t.id == "t1"
    assert t.status == "PENDING"
    assert t.model_tier == "stub"


def test_section_creation():
    s = Section(
        id="s1",
        task_id="t1",
        seq=0,
        raw_md_path="/x/0.raw.md",
        sha256="abc",
        char_count=100,
        ai_status="PENDING",
    )
    assert s.seq == 0
    assert s.ai_status == "PENDING"


def test_ai_artifact_creation():
    a = AIArtifact(
        id="a1",
        section_id="s1",
        ai_md_path="/x/0.ai.md",
        tokens_in=10,
        tokens_out=5,
        cost_usd=0.0,
        retry_count=0,
        model_name="stub",
    )
    assert a.section_id == "s1"
    assert a.retry_count == 0
    assert a.ai_md == ""
