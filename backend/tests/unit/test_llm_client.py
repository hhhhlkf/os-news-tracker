from app.llm.client import LlmClient
import pytest


class _StubTransport:
    def __init__(self):
        self.calls = 0

    def post(self, url, headers=None, json=None):
        self.calls += 1

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "hello"}}]}

        return _Resp()


def test_llm_client_completes_and_caches():
    transport = _StubTransport()
    client = LlmClient(http=transport)
    out1 = client.complete("prompt-1")
    out2 = client.complete("prompt-1")
    assert out1 == "hello"
    assert out2 == "hello"
    assert transport.calls == 1


class _FallbackTransport:
    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, json=None):
        self.calls.append(json)

        class _Resp:
            def __init__(self, payload):
                self.status_code = 400 if "response_format" in payload else 200
                self.text = (
                    '{"error":{"message":"This response_format type is unavailable now","code":"invalid_request_error"}}'
                    if self.status_code == 400
                    else '{"choices":[{"message":{"content":"ok-without-response-format"}}]}'
                )

            def raise_for_status(self):
                if self.status_code >= 400:
                    import httpx
                    req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
                    resp = httpx.Response(self.status_code, request=req, text=self.text)
                    raise httpx.HTTPStatusError(
                        "Client error '400 Bad Request' for url 'https://api.deepseek.com/chat/completions'",
                        request=req,
                        response=resp,
                    )

            def json(self):
                import json
                return json.loads(self.text)

        return _Resp(json)


def test_llm_client_retries_without_response_format_on_unsupported_400():
    transport = _FallbackTransport()
    client = LlmClient(http=transport)
    out = client.complete("prompt-1", response_format={"type": "json_object"})
    assert out == "ok-without-response-format"
    assert len(transport.calls) == 2
    assert transport.calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in transport.calls[1]


class _ErrorBodyTransport:
    def post(self, url, headers=None, json=None):
        class _Resp:
            status_code = 400
            text = '{"error":{"message":"schema too large","code":"invalid_request_error"}}'

            def raise_for_status(self):
                import httpx
                req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
                resp = httpx.Response(self.status_code, request=req, text=self.text)
                raise httpx.HTTPStatusError(
                    "Client error '400 Bad Request' for url 'https://api.deepseek.com/chat/completions'",
                    request=req,
                    response=resp,
                )

            def json(self):
                raise AssertionError("should not reach json() on error")

        return _Resp()


def test_llm_client_error_includes_response_body_snippet():
    client = LlmClient(http=_ErrorBodyTransport())
    with pytest.raises(RuntimeError) as excinfo:
        client.complete("prompt-1", response_format={"type": "json_object"})
    assert "schema too large" in str(excinfo.value)
