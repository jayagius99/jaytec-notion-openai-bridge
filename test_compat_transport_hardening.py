import asyncio
import json
import unittest

import compat_server


class _CaptureApp:
    def __init__(self):
        self.called = False
        self.body = b""
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.scope = scope
        chunks = []
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        self.body = b"".join(chunks)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class _TwoReceiveApp:
    def __init__(self):
        self.messages = []

    async def __call__(self, scope, receive, send):
        self.messages.append(await receive())
        self.messages.append(await receive())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _invoke(messages, *, path="/mcp", content_type=b"application/json", content_length=None):
    capture = _CaptureApp()
    middleware = compat_server.LegacyCatalogCompatMiddleware(capture, max_body_bytes=1024)
    queued = list(messages)

    async def receive():
        return queued.pop(0) if queued else {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", content_type)]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers,
    }
    await middleware(scope, receive, send)
    return capture, sent


async def _invoke_receive_probe(body, *, app):
    middleware = compat_server.LegacyCatalogCompatMiddleware(app, max_body_bytes=1024)
    queued = [{"type": "http.request", "body": body, "more_body": False}]
    upstream_calls = 0

    async def receive():
        nonlocal upstream_calls
        upstream_calls += 1
        return queued.pop(0) if queued else {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    }
    await middleware(scope, receive, send)
    return upstream_calls, sent


class _FakeMCP:
    def __init__(self):
        self.kwargs = None

    def http_app(self, **kwargs):
        self.kwargs = kwargs
        return kwargs


class TestCompatTransportHardening(unittest.TestCase):
    def test_http_wrapper_explicitly_preserves_reliable_transport_settings(self):
        fake = _FakeMCP()
        result = compat_server.create_http_app(fake)
        self.assertIs(result, fake.kwargs)
        self.assertTrue(fake.kwargs["stateless_http"])
        self.assertFalse(fake.kwargs["host_origin_protection"])
        middleware = fake.kwargs["middleware"]
        self.assertEqual(len(middleware), 2)
        self.assertIs(middleware[0].cls, compat_server.MeetingBusMiddleware)
        self.assertIs(middleware[1].cls, compat_server.LegacyCatalogCompatMiddleware)

    def test_declared_oversized_mcp_json_body_is_rejected_before_downstream(self):
        messages = [{"type": "http.request", "body": b"{}", "more_body": False}]
        capture, sent = asyncio.run(_invoke(messages, content_length=2048))
        self.assertFalse(capture.called)
        self.assertEqual(sent[0]["status"], 413)

    def test_chunked_oversized_mcp_json_body_is_rejected_incrementally(self):
        messages = [
            {"type": "http.request", "body": b"a" * 700, "more_body": True},
            {"type": "http.request", "body": b"b" * 700, "more_body": False},
        ]
        capture, sent = asyncio.run(_invoke(messages))
        self.assertFalse(capture.called)
        self.assertEqual(sent[0]["status"], 413)

    def test_non_mcp_post_is_outside_compatibility_boundary(self):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "collaborate",
                "arguments": {"task": compat_server.RELIABILITY_STATUS_COMMAND},
            },
        }
        body = json.dumps(payload).encode("utf-8")
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        capture, sent = asyncio.run(_invoke(messages, path="/oauth/callback", content_length=len(body)))
        self.assertTrue(capture.called)
        self.assertEqual(capture.body, body)
        self.assertEqual(sent[0]["status"], 204)

    def test_non_json_mcp_post_is_outside_compatibility_boundary(self):
        body = b"not-json"
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        capture, sent = asyncio.run(
            _invoke(messages, content_type=b"application/octet-stream", content_length=len(body))
        )
        self.assertTrue(capture.called)
        self.assertEqual(capture.body, body)
        self.assertEqual(sent[0]["status"], 204)

    def test_rewritten_receive_delegates_post_body_receive_to_upstream(self):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "collaborate",
                "arguments": {"task": compat_server.RELIABILITY_STATUS_COMMAND},
            },
        }
        body = json.dumps(payload).encode("utf-8")
        app = _TwoReceiveApp()
        upstream_calls, _ = asyncio.run(_invoke_receive_probe(body, app=app))

        self.assertEqual(upstream_calls, 2)
        self.assertEqual(app.messages[0]["type"], "http.request")
        self.assertEqual(app.messages[1], {"type": "http.disconnect"})
        rewritten = json.loads(app.messages[0]["body"])
        self.assertEqual(rewritten["params"]["name"], "reliability_status")

    def test_replay_receive_delegates_post_body_receive_to_upstream(self):
        body = b'{"jsonrpc":"2.0","method":"ping"}'
        app = _TwoReceiveApp()
        upstream_calls, _ = asyncio.run(_invoke_receive_probe(body, app=app))

        self.assertEqual(upstream_calls, 2)
        self.assertEqual(app.messages[0], {"type": "http.request", "body": body, "more_body": False})
        self.assertEqual(app.messages[1], {"type": "http.disconnect"})


if __name__ == "__main__":
    unittest.main()
