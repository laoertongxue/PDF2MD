import base64
import copy
import gc
import importlib
import io
import json
import os
import pickle
import pkgutil
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from parsing_core.workbench.ocr.alignment import authorize_baidu_escalation
from parsing_core.workbench.ocr.baidu import (
    BaiduEscalationAuthorization,
    BaiduEscalationReason,
    BaiduOcrClient,
    BaiduOcrError,
    BaiduRequest,
    _send_request_subprocess,
    redact_baidu_error,
)


def test_production_transport_module_is_importable_and_package_discoverable():
    package = importlib.import_module("parsing_core.workbench.ocr")
    discovered = {module.name for module in pkgutil.iter_modules(package.__path__)}

    assert "baidu_transport" in discovered
    transport = importlib.import_module("parsing_core.workbench.ocr.baidu_transport")
    assert callable(transport.main)


class _FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


class _FakeHttpsResponse:
    def __init__(self, chunks, *, status=200):
        self.status = status
        self._chunks = iter(chunks)

    def read(self, _size):
        return next(self._chunks, b"")


def _install_inline_send(monkeypatch, baidu_module):
    def send_inline(
        request,
        *,
        timeout,
        deadline,
        cancel_event,
        max_response_bytes,
        command=None,
    ):
        assert command is None
        return baidu_module._send_https_request_inline(
            request,
            timeout=timeout,
            deadline=deadline,
            cancel_event=cancel_event,
            max_response_bytes=max_response_bytes,
        )

    monkeypatch.setattr(baidu_module, "_send_request_subprocess", send_inline)


def _install_fake_https(monkeypatch, response):
    import parsing_core.workbench.ocr.baidu as baidu_module

    connections = []

    class FakeHttpsConnection:
        def __init__(self, host, port=None, *, timeout=None, context=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.context = context
            self.sock = _FakeSocket()
            self.requests = []
            self.closed = False
            connections.append(self)

        def connect(self):
            return None

        def request(self, method, path, *, body, headers):
            self.requests.append((method, path, body, headers))

        def getresponse(self):
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr(baidu_module.http.client, "HTTPSConnection", FakeHttpsConnection)

    _install_inline_send(monkeypatch, baidu_module)
    return connections


def _authorization(status="conflict", *, page_hash="page-sha", input_fingerprint="input-sha"):
    return authorize_baidu_escalation(
        page_hash,
        1,
        status,
        input_fingerprint=input_fingerprint,
        sample_rate=1,
    )


def _official_response(
    *,
    content: str = "MBA 教材内容 https://remote.example/secret.jpg",
    response_id: str = "as-test-123",
) -> bytes:
    return json.dumps(
        {
            "id": response_id,
            "result": {
                "layoutParsingResults": [
                    {
                        "prunedResult": {
                            "model_settings": {
                                "use_doc_preprocessor": True,
                                "use_layout_detection": True,
                                "use_chart_recognition": True,
                                "format_block_content": True,
                            },
                            "parsing_res_list": [
                                {
                                    "block_label": "text",
                                    "block_content": content,
                                    "block_bbox": [120, 160, 1080, 320],
                                    "block_id": 7,
                                    "block_order": 1,
                                }
                            ],
                            "layout_det_res": {
                                "boxes": [
                                    {
                                        "cls_id": 0,
                                        "label": "text",
                                        "score": 0.98,
                                        "coordinate": [120.0, 160.0, 1080.0, 320.0],
                                    }
                                ]
                            },
                        },
                        "markdown": {
                            "text": content,
                            "images": {"remote": "https://remote.example/secret.jpg"},
                        },
                        "outputImages": {"layout": "https://remote.example/layout.jpg"},
                        "inputImage": "https://remote.example/input.jpg",
                    }
                ],
                "dataInfo": {"type": "image", "width": 1200, "height": 1600},
            },
            "usage": {"numPages": 1},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalized_official(content: str = "MBA 教材内容") -> dict:
    return {
        "engine": "baidu_pp_structure",
        "request_id": "as-test-123",
        "data_info": {"type": "image", "width": 1200, "height": 1600},
        "blocks": [
            {
                "id": "baidu-7",
                "type": "paragraph",
                "text": content,
                "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.1},
                "reading_order": 1,
                "confidence": 0.98,
                "source_label": "text",
            }
        ],
    }


def _recognize(client: BaiduOcrClient) -> dict:
    return client.recognize(
        b"123",
        authorization=_authorization(),
        page_hash="page-sha",
        input_fingerprint="input-sha",
        page=1,
        alignment_status="conflict",
    )


def test_baidu_client_is_network_disabled_by_default():
    client = BaiduOcrClient(api_key="secret-key")

    with pytest.raises(BaiduOcrError, match="network disabled"):
        client.recognize(b"image-bytes")


def test_baidu_client_requires_bounded_image_and_response():
    client = BaiduOcrClient(api_key="secret-key", max_image_bytes=4)

    with pytest.raises(BaiduOcrError, match="image is too large"):
        client.recognize(b"12345")


def test_baidu_uses_official_pp_structure_v3_contract_and_normalizes_evidence():
    seen = []

    def transport(request, **_control):
        seen.append(request)
        return 200, _official_response()

    result = _recognize(BaiduOcrClient(api_key="secret-key", transport=transport))

    assert len(seen) == 1
    request = seen[0]
    assert request.url == "https://qianfan.baidubce.com/v2/ocr/paddleocr"
    assert request.headers == {
        "Authorization": "Bearer secret-key",
        "Content-Type": "application/json",
    }
    body = json.loads(request.body)
    assert body == {
        "model": "pp-structurev3",
        "file": base64.b64encode(b"123").decode("ascii"),
        "fileType": 1,
        "layoutNms": True,
        "useDocOrientationClassify": True,
        "useDocUnwarping": True,
        "useTextlineOrientation": True,
        "useRegionDetection": True,
        "useChartRecognition": True,
        "useTableRecognition": True,
        "useFormulaRecognition": True,
        "useOcrResultsWithTableCells": True,
        "useTableOrientationClassify": True,
        "visualize": False,
    }
    assert set(result) == {"engine", "request_id", "data_info", "blocks"}
    assert result["request_id"] == "as-test-123"
    assert result["data_info"] == {"type": "image", "width": 1200, "height": 1600}
    assert result["blocks"][0]["bounding_box"] == {
        "x": 0.1,
        "y": 0.1,
        "width": 0.8,
        "height": 0.1,
    }
    assert result["blocks"][0]["confidence"] == 0.98
    assert "https://" not in json.dumps(result)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/v2/ocr/paddleocr",
        "https://qianfan.baidubce.com:444/v2/ocr/paddleocr",
        "https://qianfan.baidubce.com/v2/ocr/paddleocr?redirect=evil",
        "https://qianfan.baidubce.com/v2/ocr/structure",
        "https://qianfan.baidubce.com:443/v2/ocr/paddleocr/extra",
    ],
)
def test_baidu_production_endpoint_is_exactly_official_host_path_and_port(endpoint):
    with pytest.raises(ValueError, match="endpoint"):
        BaiduOcrClient(api_key="secret-key", endpoint=endpoint)


def test_one_argument_transport_is_adapted_without_duplicate_side_effects():
    calls = []

    def legacy_transport(request):
        calls.append(request)
        return 200, _official_response()

    result = _recognize(
        BaiduOcrClient(api_key="secret-key", transport=legacy_transport, max_retries=2)
    )

    assert result["request_id"] == "as-test-123"
    assert len(calls) == 1


def test_invalid_transport_signature_is_rejected_before_authorization_is_consumed():
    def invalid_transport(request, *, timeout):
        return 200, _official_response()

    with pytest.raises(ValueError, match="transport"):
        BaiduOcrClient(api_key="secret-key", transport=invalid_transport)


@pytest.mark.parametrize(
    "response",
    [
        b"{}",
        b'{"error":{"type":"invalid_request_error"}}',
        b'{"id":"as-empty","result":{"layoutParsingResults":[],"dataInfo":{"type":"image","width":1200,"height":1600}}}',
        _official_response(content="   "),
    ],
)
def test_baidu_rejects_error_empty_and_meaningless_official_responses(response):
    client = BaiduOcrClient(
        api_key="secret-key", transport=lambda _request, **_control: (200, response)
    )

    with pytest.raises(BaiduOcrError, match="response"):
        _recognize(client)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://qianfan.baidubce.com/v2/ocr/structure",
        "https://user:secret@qianfan.baidubce.com/v2/ocr/structure",
        "https://qianfan.baidubce.com/v2/ocr/structure#fragment",
        "https:///v2/ocr/structure",
    ],
)
def test_baidu_endpoint_parser_rejects_non_https_credentials_fragments_and_missing_host(endpoint):
    with pytest.raises(ValueError, match="endpoint"):
        BaiduOcrClient(api_key="secret-key", endpoint=endpoint)


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

    def transport(_request, **_control):
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

    def transport(request, **_control):
        seen.append(request)
        return 200, _official_response(content="ok")

    client = BaiduOcrClient(api_key="secret-key", transport=transport)
    result = client.recognize(
        b"123",
        authorization=_authorization("consistent"),
        page_hash="page-sha",
        input_fingerprint="input-sha",
        page=1,
        alignment_status="consistent",
    )

    assert result == _normalized_official("ok")
    assert seen[0].headers["Authorization"] == "Bearer secret-key"
    assert json.loads(seen[0].body)["file"] == base64.b64encode(b"123").decode("ascii")


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
    client = BaiduOcrClient(
        api_key="secret-key", transport=lambda _request, **_control: (200, b"{}")
    )

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
    client = BaiduOcrClient(
        api_key="secret-key", transport=lambda _request, **_control: (200, b"{}")
    )

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

    def transport(_request, **_control):
        calls.append(1)
        return 200, _official_response()

    client = BaiduOcrClient(api_key="secret-key", transport=transport)
    result = client.recognize(
        b"123",
        authorization=_authorization(),
        page_hash="page-sha",
        input_fingerprint="input-sha",
        page=1,
        alignment_status="conflict",
    )

    assert result == _normalized_official()
    assert calls == [1]


def test_baidu_authorization_is_consumed_after_matching_validation():
    calls = []

    def transport(_request, **_control):
        calls.append(1)
        return 200, _official_response()

    authorization = _authorization()
    client = BaiduOcrClient(api_key="secret-key", transport=transport)
    context = {
        "page_hash": "page-sha",
        "input_fingerprint": "input-sha",
        "page": 1,
        "alignment_status": "conflict",
    }

    assert (
        client.recognize(b"123", authorization=authorization, **context) == _normalized_official()
    )
    with pytest.raises(BaiduOcrError, match="authorization"):
        client.recognize(b"123", authorization=authorization, **context)
    assert calls == [1]


def test_baidu_wrong_context_does_not_consume_authorization():
    calls = []

    def transport(_request, **_control):
        calls.append(1)
        return 200, _official_response()

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
    assert (
        client.recognize(
            b"123",
            authorization=authorization,
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
        )
        == _normalized_official()
    )
    assert calls == [1]


def test_baidu_authorization_is_consumed_before_network_failure():
    import parsing_core.workbench.ocr.baidu as baidu

    authorization = _authorization()
    client = BaiduOcrClient(
        api_key="secret-key",
        transport=lambda _request, **_control: (_ for _ in ()).throw(RuntimeError("offline")),
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

    def transport(_request, **_control):
        calls.append(1)
        return 200, _official_response()

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
    assert results.count(_normalized_official()) == 1
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


def test_baidu_cancel_before_request_does_not_send_network_call():
    calls = []
    cancel = threading.Event()
    cancel.set()
    client = BaiduOcrClient(
        api_key="secret-key",
        transport=lambda request, **control: calls.append((request, control)),
    )

    with pytest.raises(BaiduOcrError, match="cancelled"):
        client.recognize(
            b"123",
            authorization=_authorization(),
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
            deadline=time.monotonic() + 1,
            cancel_event=cancel,
        )

    assert calls == []


def test_baidu_custom_transport_cooperates_with_cancel_and_finishes_before_return():
    cancel = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    outcomes = []

    def transport(_request, *, timeout, cancel_event):
        started.set()
        while not cancel_event.wait(min(0.05, timeout)):
            pass
        finished.set()
        raise InterruptedError("transport cancelled")

    client = BaiduOcrClient(api_key="secret-key", transport=transport, timeout=2)

    def recognize():
        try:
            client.recognize(
                b"123",
                authorization=_authorization(),
                page_hash="page-sha",
                input_fingerprint="input-sha",
                page=1,
                alignment_status="conflict",
                deadline=time.monotonic() + 2,
                cancel_event=cancel,
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=recognize, name="baidu-cancel-test")
    worker.start()
    assert started.wait(timeout=1)
    cancel.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert finished.is_set()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], BaiduOcrError)
    assert "cancelled" in str(outcomes[0])


def test_baidu_custom_transport_success_after_cancel_cannot_publish_result():
    cancel = threading.Event()

    def transport(_request, **_control):
        cancel.set()
        return 200, b'{"result": []}'

    client = BaiduOcrClient(api_key="secret-key", transport=transport, max_retries=0)

    with pytest.raises(BaiduOcrError, match="cancelled"):
        client.recognize(
            b"123",
            authorization=_authorization(),
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
            deadline=time.monotonic() + 1,
            cancel_event=cancel,
        )


def test_baidu_deadline_bounds_transport_and_prevents_retry_after_expiry():
    timeouts = []

    def transport(_request, *, timeout, cancel_event):
        timeouts.append(timeout)
        time.sleep(timeout)
        raise TimeoutError("remote timeout")

    client = BaiduOcrClient(api_key="secret-key", transport=transport, timeout=2, max_retries=2)

    with pytest.raises(BaiduOcrError, match="timed out"):
        client.recognize(
            b"123",
            authorization=_authorization(),
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
            deadline=time.monotonic() + 0.1,
            cancel_event=threading.Event(),
        )

    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 0.1


def test_default_https_transport_uses_verified_tls_strict_host_and_chunked_reads(monkeypatch):
    response_body = _official_response()
    midpoint = len(response_body) // 2
    response = _FakeHttpsResponse([response_body[:midpoint], response_body[midpoint:], b""])
    connections = _install_fake_https(monkeypatch, response)
    client = BaiduOcrClient(api_key="secret-key", max_retries=0)

    result = client.recognize(
        b"image-bytes",
        authorization=_authorization(),
        page_hash="page-sha",
        input_fingerprint="input-sha",
        page=1,
        alignment_status="conflict",
        deadline=time.monotonic() + 1,
        cancel_event=threading.Event(),
    )

    assert result == _normalized_official()
    assert len(connections) == 1
    connection = connections[0]
    assert connection.host == "qianfan.baidubce.com"
    assert connection.port is None
    assert connection.context.verify_mode == ssl.CERT_REQUIRED
    assert connection.context.check_hostname is True
    assert len(connection.requests) == 1
    method, path, request_body, headers = connection.requests[0]
    assert method == "POST"
    assert path == "/v2/ocr/paddleocr"
    assert json.loads(request_body)["model"] == "pp-structurev3"
    assert headers == {
        "Authorization": "Bearer secret-key",
        "Content-Type": "application/json",
    }
    assert connection.sock.timeouts
    assert all(0 < timeout <= 1 for timeout in connection.sock.timeouts)
    assert connection.closed is True


def test_default_https_transport_checks_deadline_after_each_response_chunk(monkeypatch):
    class SlowResponse(_FakeHttpsResponse):
        def read(self, size):
            time.sleep(0.06)
            return super().read(size)

    response = SlowResponse([b'{"result": []}', b""])
    connections = _install_fake_https(monkeypatch, response)
    client = BaiduOcrClient(api_key="secret-key", max_retries=0, timeout=2)
    started = time.monotonic()

    with pytest.raises(BaiduOcrError, match="timed out"):
        client.recognize(
            b"image-bytes",
            authorization=_authorization(),
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
            deadline=time.monotonic() + 0.04,
            cancel_event=threading.Event(),
        )

    assert time.monotonic() - started < 0.25
    assert connections[0].closed is True


def test_default_https_transport_cancel_closes_blocked_response_and_returns(monkeypatch):
    import parsing_core.workbench.ocr.baidu as baidu_module

    cancel = threading.Event()
    read_started = threading.Event()
    connection_closed = threading.Event()
    outcomes = []

    class BlockingResponse:
        status = 200

        def read(self, _size):
            read_started.set()
            connection_closed.wait(timeout=2)
            raise OSError("connection closed")

    class BlockingConnection:
        def __init__(self, *_args, **_kwargs):
            self.sock = _FakeSocket()

        def connect(self):
            return None

        def request(self, *_args, **_kwargs):
            return None

        def getresponse(self):
            return BlockingResponse()

        def close(self):
            connection_closed.set()

    monkeypatch.setattr(baidu_module.http.client, "HTTPSConnection", BlockingConnection)
    _install_inline_send(monkeypatch, baidu_module)
    client = BaiduOcrClient(api_key="secret-key", max_retries=0, timeout=5)

    def recognize():
        try:
            client.recognize(
                b"image-bytes",
                authorization=_authorization(),
                page_hash="page-sha",
                input_fingerprint="input-sha",
                page=1,
                alignment_status="conflict",
                deadline=time.monotonic() + 4,
                cancel_event=cancel,
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=recognize, name="baidu-default-transport-cancel-test")
    worker.start()
    assert read_started.wait(timeout=1)
    cancel.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert connection_closed.is_set()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], BaiduOcrError)
    assert "cancelled" in str(outcomes[0])


def test_default_https_transport_rejects_redirect_without_following(monkeypatch):
    response = _FakeHttpsResponse([b'{"message":"moved"}', b""], status=302)
    connections = _install_fake_https(monkeypatch, response)
    client = BaiduOcrClient(api_key="secret-key", max_retries=0)

    with pytest.raises(BaiduOcrError, match="302"):
        client.recognize(
            b"image-bytes",
            authorization=_authorization(),
            page_hash="page-sha",
            input_fingerprint="input-sha",
            page=1,
            alignment_status="conflict",
            deadline=time.monotonic() + 1,
            cancel_event=threading.Event(),
        )

    assert len(connections) == 1
    assert len(connections[0].requests) == 1


def test_subprocess_transport_cancels_blocked_connect_without_lingering_process(tmp_path):
    worker = tmp_path / "blocked_transport.py"
    pid_path = tmp_path / "worker.pid"
    worker.write_text(
        "import os, pathlib, sys, time\n"
        "sys.stdin.buffer.read()\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    cancel = threading.Event()
    outcomes = []

    def run():
        try:
            _send_request_subprocess(
                BaiduRequest(
                    url="https://qianfan.baidubce.com/v2/ocr/structure",
                    headers={"Authorization": "Bearer secret-key"},
                    body=b"image",
                ),
                timeout=5,
                deadline=time.monotonic() + 5,
                cancel_event=cancel,
                max_response_bytes=1024,
                command=(sys.executable, str(worker), str(pid_path)),
            )
        except BaseException as exc:
            outcomes.append(exc)

    caller = threading.Thread(target=run, name="baidu-subprocess-cancel-test")
    caller.start()
    deadline = time.monotonic() + 2
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_path.exists()
    worker_pid = int(pid_path.read_text(encoding="utf-8"))

    cancel.set()
    caller.join(timeout=1)

    assert not caller.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], BaiduOcrError)
    assert "cancelled" in str(outcomes[0])
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)


def test_subprocess_transport_uses_bounded_success_protocol(tmp_path):
    worker = tmp_path / "successful_transport.py"
    worker.write_text(
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(b'OK 200\\n{\"result\":[]}')\n",
        encoding="utf-8",
    )

    status, body = _send_request_subprocess(
        BaiduRequest(
            url="https://qianfan.baidubce.com/v2/ocr/structure",
            headers={
                "Authorization": "Bearer secret-key",
                "Content-Type": "application/octet-stream",
            },
            body=b"image",
        ),
        timeout=2,
        deadline=time.monotonic() + 2,
        cancel_event=threading.Event(),
        max_response_bytes=1024,
        command=(sys.executable, str(worker)),
    )

    assert status == 200
    assert body == b'{"result":[]}'


def test_transport_worker_frames_request_without_echoing_credentials(monkeypatch):
    from parsing_core.workbench.ocr import baidu_transport

    body = b"image"
    header = json.dumps(
        {
            "url": "https://qianfan.baidubce.com/v2/ocr/paddleocr",
            "headers": {
                "Authorization": "Bearer secret-key",
                "Content-Type": "application/json",
            },
            "body_length": len(body),
            "timeout": 1,
            "max_response_bytes": 1024,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    stdin = io.BytesIO(len(header).to_bytes(4, "big") + header + body)
    stdout = io.BytesIO()
    stderr = io.StringIO()
    seen = []

    def send_inline(request, **_control):
        seen.append(request)
        return 200, b'{"result":[]}'

    monkeypatch.setattr(baidu_transport, "_send_https_request_inline", send_inline)

    assert baidu_transport.main(stdin, stdout, stderr) == 0
    assert stdout.getvalue() == b'OK 200\n{"result":[]}'
    assert stderr.getvalue() == ""
    assert seen[0].headers["Authorization"] == "Bearer secret-key"
    assert b"secret-key" not in stdout.getvalue()
