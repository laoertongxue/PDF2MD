import pytest

from parsing_core.utils.retry import with_retry


def test_retry_succeeds_first_try():
    calls = {"n": 0}

    @with_retry(max_attempts=3, base_delay=0)
    def ok():
        calls["n"] += 1
        return "ok"

    assert ok() == "ok"
    assert calls["n"] == 1


def test_retry_succeeds_on_third():
    state = {"n": 0}

    @with_retry(max_attempts=3, base_delay=0)
    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("boom")
        return "recovered"

    assert flaky() == "recovered"
    assert state["n"] == 3


def test_retry_exhausts_raises():
    @with_retry(max_attempts=2, base_delay=0)
    def always_fail():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        always_fail()
