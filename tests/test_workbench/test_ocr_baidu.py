import pytest

from parsing_core.workbench.ocr.baidu import (
    BaiduOcrClient,
    BaiduOcrError,
    redact_baidu_error,
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
        client.recognize(b"123", allow_network=True)
    assert len(calls) == 3


def test_baidu_client_validates_json_and_never_logs_secret(monkeypatch):
    seen = []

    def transport(request):
        seen.append(request)
        return 200, b'{"result": [{"text":"ok"}]}'

    client = BaiduOcrClient(api_key="secret-key", transport=transport)
    result = client.recognize(b"123", allow_network=True)

    assert result == {"result": [{"text": "ok"}]}
    assert seen[0].headers["Authorization"] == "Bearer secret-key"
    assert b"123" in seen[0].body
