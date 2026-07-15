import copy
import gc
import pickle
from concurrent.futures import ThreadPoolExecutor

import pytest

from parsing_core.workbench.ocr.alignment import authorize_baidu_escalation
from parsing_core.workbench.ocr.baidu import (
    BaiduEscalationAuthorization,
    BaiduEscalationReason,
    BaiduOcrClient,
    BaiduOcrError,
    redact_baidu_error,
)


def _authorization(status="conflict", *, page_hash="page-sha", input_fingerprint="input-sha"):
    return authorize_baidu_escalation(
        page_hash,
        1,
        status,
        input_fingerprint=input_fingerprint,
        sample_rate=1,
    )


def test_baidu_client_is_network_disabled_by_default():
    client = BaiduOcrClient(api_key="secret-key")

    with pytest.raises(BaiduOcrError, match="network disabled"):
        client.recognize(b"image-bytes")


def test_baidu_client_requires_bounded_image_and_response():
    client = BaiduOcrClient(api_key="secret-key", max_image_bytes=4)

    with pytest.raises(BaiduOcrError, match="image is too large"):
        client.recognize(b"12345")


def test_baidu_error_redacts_key_paths_and_response_body():
    error = redact_baidu_error(
        '401 key=secret-key /Users/laoer/book.pdf token=abc123 response={"access_token":"xyz"}'
    )

    assert "secret-key" not in error
    assert "/Users/laoer/book.pdf" not in error
    assert "abc123" not in error
    assert "xyz" not in error
    assert "Baidu OCR request failed" in error


def test_baidu_client_retries_only_bounded_transient_failures():
    calls = []

    def transport(_request):
        calls.append(1)
        return 429, b'{"error_code":18,"error_msg":"rate limited"}'

    client = BaiduOcrClient(api_key="secret-key", transport=transport, max_retries=2)

    with pytest.raises(BaiduOcrError, match="rate limited"):
        client.recognize(
            b"123",
            authorization=_authorization(),
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
        )
    assert len(calls) == 3


def test_baidu_client_validates_json_and_never_logs_secret(monkeypatch):
    seen = []

    def transport(request):
        seen.append(request)
        return 200, b'{"result": [{"text":"ok"}]}'

    client = BaiduOcrClient(api_key="secret-key", transport=transport)
    result = client.recognize(
        b"123",
        authorization=_authorization("consistent"),
        page_hash="page-sha",
        input_fingerprint="input-sha",
        page=1,
        alignment_status="consistent",
    )

    assert result == {"result": [{"text": "ok"}]}
    assert seen[0].headers["Authorization"] == "Bearer secret-key"
    assert b"123" in seen[0].body


def test_baidu_client_rejects_untyped_or_invalid_upgrade_authorization():
    client = BaiduOcrClient(api_key="secret-key")
    with pytest.raises(BaiduOcrError, match="typed escalation authorization"):
        client.recognize(b"123", allow_network=True)
    with pytest.raises(BaiduOcrError, match="unsupported escalation reason"):
        client.recognize(b"123", authorization="consistent")


def test_baidu_client_rejects_directly_forged_authorization():
    with pytest.raises(TypeError, match="trusted alignment decision"):
        BaiduEscalationAuthorization(
            BaiduEscalationReason.CONFLICT,
            "page-sha",
            "input-sha",
            "conflict",
            1,
        )


@pytest.mark.parametrize("clone", [copy.copy, copy.deepcopy, pickle.loads])
def test_baidu_client_rejects_copied_or_unpickled_authorization(clone):
    authorization = _authorization()
    cloned = clone(pickle.dumps(authorization)) if clone is pickle.loads else clone(authorization)
    client = BaiduOcrClient(api_key="secret-key", transport=lambda _request: (200, b'{}'))

    with pytest.raises(BaiduOcrError, match="authorization"):
        client.recognize(
            b"123",
            authorization=cloned,
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
        )


def test_baidu_client_rejects_object_new_authorization():
    forged = object.__new__(BaiduEscalationAuthorization)
    client = BaiduOcrClient(api_key="secret-key", transport=lambda _request: (200, b'{}'))

    with pytest.raises(BaiduOcrError, match="authorization"):
        client.recognize(
            b"123",
            authorization=forged,
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
        )


def test_old_authorization_capability_is_not_exposed():
    import parsing_core.workbench.ocr.baidu as baidu

    assert not hasattr(baidu, "_AUTHORIZATION_CAPABILITY")
    assert not hasattr(baidu, "_issue_baidu_escalation_authorization")


def test_private_alignment_factory_rejects_direct_calls():
    with pytest.raises(TypeError, match="trusted alignment decision"):
        BaiduEscalationAuthorization._from_alignment(
            BaiduEscalationReason.CONFLICT,
            page_hash="page-sha",
            input_fingerprint="input-sha",
            alignment_status="conflict",
            page=1,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_hash", "other-page"),
        ("input_fingerprint", "other-input"),
        ("page", 2),
        ("alignment_status", "consistent"),
    ],
)
def test_baidu_client_rejects_authorization_for_wrong_page_context(field, value):
    client = BaiduOcrClient(api_key="secret-key")
    context = {
        "page_hash": "page-sha",
        "input_fingerprint": "input-sha",
        "page": 1,
        "alignment_status": "conflict",
    }
    context[field] = value

    with pytest.raises(BaiduOcrError, match="authorization context"):
        client.recognize(b"123", authorization=_authorization(), **context)


def test_baidu_client_allows_network_only_for_matching_upgrade_page():
    calls = []

    def transport(_request):
        calls.append(1)
        return 200, b'{"result": []}'

    client = BaiduOcrClient(api_key="secret-key", transport=transport)
    result = client.recognize(
        b"123",
        authorization=_authorization(),
        page_hash="page-sha",
        input_fingerprint="input-sha",
        page=1,
        alignment_status="conflict",
    )

    assert result == {"result": []}
    assert calls == [1]


def test_baidu_authorization_is_consumed_after_matching_validation():
    calls = []

    def transport(_request):
        calls.append(1)
        return 200, b'{"result": []}'

    authorization = _authorization()
    client = BaiduOcrClient(api_key="secret-key", transport=transport)
    context = {
        "page_hash": "page-sha",
        "input_fingerprint": "input-sha",
        "page": 1,
        "alignment_status": "conflict",
    }

    assert client.recognize(b"123", authorization=authorization, **context) == {
        "result": []
    }
    with pytest.raises(BaiduOcrError, match="authorization"):
        client.recognize(b"123", authorization=authorization, **context)
    assert calls == [1]


def test_baidu_wrong_context_does_not_consume_authorization():
    calls = []

    def transport(_request):
        calls.append(1)
        return 200, b'{"result": []}'

    authorization = _authorization()
    client = BaiduOcrClient(api_key="secret-key", transport=transport)
    with pytest.raises(BaiduOcrError, match="authorization context"):
        client.recognize(
            b"123",
            authorization=authorization,
            page_hash="wrong-page",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
        )
    assert client.recognize(
        b"123",
        authorization=authorization,
        page_hash="page-sha",
        input_fingerprint="input-sha",
        page=1,
        alignment_status="conflict",
    ) == {"result": []}
    assert calls == [1]


def test_baidu_authorization_is_consumed_before_network_failure():
    import parsing_core.workbench.ocr.baidu as baidu

    authorization = _authorization()
    client = BaiduOcrClient(
        api_key="secret-key",
        transport=lambda _request: (_ for _ in ()).throw(RuntimeError("offline")),
        max_retries=0,
    )
    with pytest.raises(BaiduOcrError, match="offline"):
        client.recognize(
            b"123",
            authorization=authorization,
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
        )
    assert id(authorization) not in baidu._AUTHORIZATION_REGISTRY


def test_baidu_authorization_registry_does_not_retain_expired_handles():
    import parsing_core.workbench.ocr.baidu as baidu

    authorizations = [_authorization() for _ in range(1000)]
    assert len(baidu._AUTHORIZATION_REGISTRY) == 1000
    del authorizations
    gc.collect()
    assert len(baidu._AUTHORIZATION_REGISTRY) == 0


def test_baidu_authorization_is_single_use_under_concurrency():
    calls = []

    def transport(_request):
        calls.append(1)
        return 200, b'{"result": []}'

    authorization = _authorization()
    client = BaiduOcrClient(api_key="secret-key", transport=transport)
    context = {
        "page_hash": "page-sha",
        "input_fingerprint": "input-sha",
        "page": 1,
        "alignment_status": "conflict",
    }

    def recognize():
        try:
            return client.recognize(b"123", authorization=authorization, **context)
        except BaiduOcrError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: recognize(), range(8)))
    assert results.count({"result": []}) == 1
    assert calls == [1]


def test_baidu_client_stays_offline_without_authorization_even_with_context():
    client = BaiduOcrClient(api_key="secret-key")

    with pytest.raises(BaiduOcrError, match="network disabled"):
        client.recognize(
            b"123",
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="consistent",
        )
