from app.llm.client import LlmClient


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
