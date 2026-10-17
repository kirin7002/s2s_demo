"""Local WebSocket protocol fixtures. No provider traffic or recorded user audio."""
import asyncio
import base64
from dataclasses import replace
import json
import unittest

from aiohttp import ClientSession, WSMsgType, web, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from gateway.config import Config
from gateway.server import ACTIVE, LOGINS, create_app, upstream_frame_error
from aiohttp.http_websocket import WebSocketError


class Harness:
    def __init__(self, **kwargs):
        self.cfg = replace(Config(api_key="local-test-placeholder", demo_password="local-demo-placeholder", workspace="local-test",
                                  live_enabled=True), **kwargs)
        self.upstreams = []
        self.events = asyncio.Queue()
        self.mode = "normal"
        self.turn = 0
        self.connect_error = None

    async def provider(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.upstreams.append(ws)
        if self.mode != "hang":
            await ws.send_json({"type": "session.created"})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            event = json.loads(msg.data)
            await self.events.put(event)
            if event["type"] == "session.update":
                if self.mode == "reserved_bits":
                    # Malformed unnegotiated RSV1 frame, not a provider JSON error.
                    request.transport.write(b"\xc1\x00")
                elif self.mode == "oversized_control":
                    # RFC 6455 control frames cannot use a payload length of 126.
                    # Synthetic reason must never be forwarded or logged.
                    payload = b"\x03\xf0" + b"sensitive-frame-payload".ljust(124, b"x")
                    request.transport.write(b"\x88\x7e\x00\x7e" + payload)
                elif self.mode == "error":
                    await ws.send_json({"type": "error", "error": {"message": "sensitive-upstream-detail"}})
                else:
                    await ws.send_json({"type": "session.updated"})
            elif event["type"] == "input_audio_buffer.append" and self.mode == "turns":
                self.turn += 1
                await ws.send_json({"type": "response.created", "response": {"id": str(self.turn)}})
                await ws.send_json({"type": "response.audio.delta", "delta": event["audio"]})
                await ws.send_json({"type": "response.audio_transcript.delta", "delta": f"本地测试轮次{self.turn}"})
                await ws.send_json({"type": "response.done", "response": {"status": "completed"}})
            elif event["type"] == "response.cancel":
                await ws.send_json({"type": "response.audio.delta", "delta": "AAA="})
                await ws.send_json({"type": "response.done", "response": {"status": "cancelled"}})
        return ws

    async def open(self):
        upstream = web.Application()
        upstream.router.add_get("/realtime", self.provider)
        self.provider_server = TestServer(upstream)
        await self.provider_server.start_server()
        self.http = ClientSession()
        async def connect():
            if self.connect_error is not None:
                raise self.connect_error
            return await self.http.ws_connect(self.provider_server.make_url("/realtime"))
        self.app = create_app(self.cfg, connector=connect)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        await self.client.post("/api/login", json={"password": self.cfg.demo_password},
                               headers={"Origin": self.cfg.origin})
        return self

    async def close(self):
        await self.client.close()
        await self.http.close()
        await self.provider_server.close()

    async def socket(self):
        return await self.client.ws_connect("/ws", headers={"Origin": self.cfg.origin})


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = await Harness().open()

    async def asyncTearDown(self):
        await self.h.close()

    async def receive(self, ws, kind):
        event = await ws.receive_json(timeout=2)
        self.assertEqual(event["type"], kind)
        return event

    async def test_stream_multiple_turns_and_cleanup(self):
        self.h.mode = "turns"
        ws = await self.h.socket()
        await self.receive(ws, "ready")
        config = await self.h.events.get()
        self.assertEqual(config["session"]["turn_detection"], {"type": "smart_turn"})
        self.assertNotIn("audio", config["session"])
        for _ in range(2):
            pcm = b"\x00\x01" * 1600
            await ws.send_bytes(pcm)
            await self.receive(ws, "responding")
            self.assertEqual((await ws.receive(timeout=2)).data, pcm)
            await self.receive(ws, "transcript")
            await self.receive(ws, "turn_done")
        await ws.send_json({"type": "stop"})
        await ws.receive(timeout=2)
        await asyncio.sleep(.05)
        self.assertFalse(self.h.app[ACTIVE])
        self.assertTrue(self.h.upstreams[0].closed)

    async def test_manual_interrupt_suppresses_late_audio(self):
        ws = await self.h.socket()
        await self.receive(ws, "ready")
        await self.h.events.get()
        await self.h.upstreams[0].send_json({"type": "response.created"})
        await self.receive(ws, "responding")
        await ws.send_json({"type": "interrupt"})
        await self.receive(ws, "interrupted")
        await self.receive(ws, "turn_done")  # late binary must have been suppressed
        self.assertEqual((await self.h.events.get())["type"], "response.cancel")
        await ws.close()

    async def test_audio_vad_does_not_double_cancel(self):
        ws = await self.h.socket()
        await self.receive(ws, "ready")
        await self.h.events.get()
        up = self.h.upstreams[0]
        await up.send_json({"type": "response.created"})
        await self.receive(ws, "responding")
        await up.send_json({"type": "input_audio_buffer.speech_started"})
        await self.receive(ws, "interrupted")
        await self.receive(ws, "listening")
        self.assertTrue(self.h.events.empty())
        await ws.close()

    async def test_reject_origin_and_cap(self):
        with self.assertRaises(WSServerHandshakeError) as ctx:
            await self.h.client.ws_connect("/ws", headers={"Origin": "https://untrusted.invalid"})
        self.assertEqual(ctx.exception.status, 403)
        sockets = [await self.h.socket(), await self.h.socket()]
        for ws in sockets:
            await self.receive(ws, "ready")
        with self.assertRaises(WSServerHandshakeError) as ctx:
            await self.h.socket()
        self.assertEqual(ctx.exception.status, 429)
        for ws in sockets:
            await ws.close()

    async def test_invalid_payload_closes_and_releases(self):
        for payload in [b"x", {"type": "session.update", "session": {}}, ["bad"], "bad-json"]:
            ws = await self.h.socket()
            await self.receive(ws, "ready")
            if isinstance(payload, bytes):
                await ws.send_bytes(payload)
            elif isinstance(payload, str):
                await ws.send_str(payload)
            else:
                await ws.send_json(payload)
            await self.receive(ws, "error")
            await ws.receive(timeout=2)
            await asyncio.sleep(.01)
            self.assertFalse(self.h.app[ACTIVE])

    async def test_upstream_error_is_sanitized(self):
        self.h.mode = "error"
        with self.assertLogs("gateway.server", level="WARNING") as logs:
            ws = await self.h.socket()
            event = await self.receive(ws, "error")
        self.assertEqual(event["code"], "UPSTREAM_EVENT_ERROR")
        self.assertEqual(event["stage"], "session_updated")
        self.assertIn("等待音频配置确认", event["message"])
        self.assertNotIn("sensitive-upstream-detail", json.dumps(event))
        self.assertNotIn("sensitive-upstream-detail", str(logs.output))
        self.assertNotIn(self.h.cfg.api_key, str(logs.output))
        self.assertNotIn(self.h.cfg.demo_password, str(logs.output))
        await ws.close()

    async def test_parser_error_has_specific_code_and_never_logs_frame_payload(self):
        self.h.mode = "reserved_bits"
        with self.assertLogs("gateway.server", level="WARNING") as logs:
            ws = await self.h.socket()
            event = await self.receive(ws, "error")
        self.assertEqual(event["code"], "UPSTREAM_WS_RESERVED_BITS")
        self.assertEqual(event["stage"], "session_updated")
        self.assertEqual(event["ws_close_code"], 1002)
        self.assertIn("ws_close_code=1002", str(logs.output))
        self.assertNotIn(self.h.cfg.api_key, str(logs.output))
        await ws.close()

    async def test_oversized_control_frame_reproduces_1002_and_keeps_reason_private(self):
        self.h.mode = "oversized_control"
        with self.assertLogs("gateway.server", level="WARNING") as logs:
            ws = await self.h.socket()
            event = await self.receive(ws, "error")
        self.assertEqual(event["code"], "UPSTREAM_WS_CONTROL_LENGTH")
        self.assertEqual(event["stage"], "session_updated")
        self.assertEqual(event["ws_close_code"], 1002)
        self.assertNotIn("sensitive-frame-payload", str(logs.output))
        self.assertNotIn("sensitive-frame-payload", json.dumps(event))
        await ws.close()

    async def test_http_rejection_is_distinct_without_leaking_exception_details(self):
        self.h.connect_error = WSServerHandshakeError(None, (), status=401,
                                                      message="sensitive-upstream-detail")
        with self.assertLogs("gateway.server", level="WARNING") as logs:
            ws = await self.h.socket()
            event = await self.receive(ws, "error")
        self.assertEqual(event["code"], "UPSTREAM_HTTP_REJECTED")
        self.assertEqual(event["stage"], "connect")
        self.assertIn("401", event["message"])
        self.assertNotIn("sensitive-upstream-detail", json.dumps(event))
        self.assertNotIn("sensitive-upstream-detail", str(logs.output))
        await ws.close()

    async def test_disconnect_before_ready(self):
        self.h.mode = "hang"
        ws = await self.h.socket()
        await ws.close()
        await asyncio.sleep(.1)
        self.assertFalse(self.h.app[ACTIVE])
        self.assertTrue(all(up.closed for up in self.h.upstreams))

    async def test_disabled_does_not_connect(self):
        h = await Harness(live_enabled=False).open()
        try:
            with self.assertRaises(WSServerHandshakeError) as ctx:
                await h.socket()
            self.assertEqual(ctx.exception.status, 503)
            self.assertFalse(h.upstreams)
        finally:
            await h.close()

    async def test_handshake_timeout(self):
        h = await Harness(handshake_seconds=.05).open()
        h.mode = "hang"
        try:
            ws = await h.socket()
            event = await self.receive(ws, "error")
            self.assertEqual(event["code"], "SESSION_TIMEOUT")
            self.assertEqual(event["stage"], "session_created")
            await ws.close()
        finally:
            await h.close()

    async def test_metadata_has_no_secret(self):
        res = await self.h.client.get("/api/config")
        body = await res.text()
        self.assertNotIn(self.h.cfg.api_key, body)
        self.assertNotIn(self.h.cfg.workspace, body)

    async def test_login_logout_and_websocket_auth(self):
        # A separate client has no cookie; even a correct Origin cannot open a model session.
        async with ClientSession() as visitor:
            url = self.h.client.make_url("/ws")
            with self.assertRaises(WSServerHandshakeError) as ctx:
                await visitor.ws_connect(url, headers={"Origin": self.h.cfg.origin})
            self.assertEqual(ctx.exception.status, 401)
        response = await self.h.client.post("/api/logout", headers={"Origin": self.h.cfg.origin})
        self.assertEqual(response.status, 200)
        with self.assertRaises(WSServerHandshakeError) as ctx:
            await self.h.socket()
        self.assertEqual(ctx.exception.status, 401)
        response = await self.h.client.post("/api/login", json={"password": "wrong"}, headers={"Origin": self.h.cfg.origin})
        self.assertEqual(response.status, 401)

    async def test_login_csrf_cookie_and_rate_limit(self):
        response = await self.h.client.post("/api/login", json={"password": self.h.cfg.demo_password}, headers={"Origin": "https://untrusted.invalid"})
        self.assertEqual(response.status, 403)
        response = await self.h.client.post("/api/login", json={"password": self.h.cfg.demo_password}, headers={"Origin": self.h.cfg.origin})
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        for _ in range(4):
            response = await self.h.client.post("/api/login", json={"password": "wrong"}, headers={"Origin": self.h.cfg.origin})
        self.assertEqual(response.status, 429)

    async def test_logout_revokes_open_sockets_in_other_tabs(self):
        sockets = [await self.h.socket(), await self.h.socket()]
        for ws in sockets:
            await self.receive(ws, "ready")
        response = await self.h.client.post("/api/logout", headers={"Origin": "https://untrusted.invalid"})
        self.assertEqual(response.status, 403)
        self.assertEqual(len(self.h.app[ACTIVE]), 2)
        response = await self.h.client.post("/api/logout", headers={"Origin": self.h.cfg.origin})
        self.assertEqual(response.status, 200)
        for ws in sockets:
            self.assertEqual((await ws.receive(timeout=2)).type, WSMsgType.CLOSE)
        await asyncio.sleep(.05)
        self.assertFalse(self.h.app[ACTIVE])
        self.assertTrue(all(up.closed for up in self.h.upstreams))

    async def test_expired_session_and_error_cache_headers(self):
        for token in self.h.app[LOGINS]:
            self.h.app[LOGINS][token] = 0
        response = await self.h.client.get("/chat", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with self.assertRaises(WSServerHandshakeError) as ctx:
            await self.h.socket()
        self.assertEqual(ctx.exception.status, 401)

    async def test_missing_key_is_blocked_before_upstream(self):
        h = await Harness(api_key="").open()
        try:
            response = await h.client.get("/api/config")
            data = await response.json()
            self.assertFalse(data["ready"])
            self.assertIn("API Key", data["message"])
            with self.assertRaises(WSServerHandshakeError) as ctx:
                await h.socket()
            self.assertEqual(ctx.exception.status, 503)
            self.assertFalse(h.upstreams)
        finally:
            await h.close()

    async def test_audio_public_configuration_can_establish_local_protocol_session(self):
        h = await Harness(workspace="").open()
        try:
            ws = await h.socket()
            await self.receive(ws, "ready")
            self.assertEqual((await h.events.get())["session"]["turn_detection"], {"type": "smart_turn"})
            await ws.close()
        finally:
            await h.close()

    async def test_https_origin_sets_secure_cookie(self):
        h = await Harness(origin="https://demo.example").open()
        try:
            response = await h.client.post("/api/login", json={"password": h.cfg.demo_password}, headers={"Origin": h.cfg.origin})
            self.assertEqual(response.status, 200)
            self.assertIn("Secure", response.headers["Set-Cookie"])
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        finally:
            await h.close()


class ConfigurationTests(unittest.TestCase):
    def test_frame_error_classification_discards_untrusted_details(self):
        for detail, expected in [
            ("Invalid close code: 1005", "UPSTREAM_WS_INVALID_CLOSE_CODE"),
            ("Invalid close frame: sensitive-payload", "UPSTREAM_WS_FRAME_ERROR"),
            ("Invalid UTF-8 text message", "UPSTREAM_WS_INVALID_UTF8"),
            ("Control frame payload cannot be larger than 125 bytes", "UPSTREAM_WS_CONTROL_LENGTH"),
        ]:
            error = upstream_frame_error(WebSocketError(1002, detail), 1002)
            self.assertEqual(error.code, expected)
            self.assertNotIn("sensitive-payload", str(error))
            self.assertEqual(error.ws_close_code, 1002)

    def test_runtime_configuration_does_not_require_workspace(self):
        from unittest.mock import patch
        values = {"DASHSCOPE_API_KEY": "local-placeholder", "S2S_DEMO_PASSWORD": "local-placeholder", "S2S_ENABLE_LIVE": "1"}
        with patch("gateway.config.os.getenv", side_effect=lambda name, default=None: values.get(name, default)):
            cfg = Config.from_env(env_file=None)
        self.assertIsNone(cfg.problem())
        self.assertEqual(cfg.workspace, "")
        self.assertTrue(cfg.url.startswith("wss://dashscope.aliyuncs.com/"))

    def test_audio_defaults_to_beijing_public_endpoint_without_workspace(self):
        cfg = Config(api_key="local-placeholder", demo_password="local-placeholder", live_enabled=True)
        self.assertIsNone(cfg.problem())
        self.assertEqual(cfg.model, "qwen-audio-3.0-realtime-flash")
        self.assertEqual(cfg.session_update()["session"]["voice"], "longanqian")
        self.assertEqual(cfg.url, "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-flash")

    def test_public_region_and_explicit_model_are_preserved(self):
        cfg = Config(region="ap-southeast-1", model="qwen-audio-3.0-realtime-flash")
        self.assertEqual(cfg.url, "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-flash")

    def test_workspace_endpoint_is_optional_for_audio_and_required_for_omni(self):
        cfg = Config(api_key="local-placeholder", demo_password="local-placeholder", live_enabled=True, workspace="example-space")
        self.assertIsNone(cfg.problem())
        self.assertEqual(cfg.url, "wss://example-space.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-flash")
        omni = replace(cfg, model="qwen3.8-omni-flash-realtime", workspace="")
        self.assertIsNotNone(omni.problem())
        with self.assertRaises(ValueError):
            _ = omni.url

    def test_public_endpoint_missing_key_and_invalid_destinations_are_blocked(self):
        cfg = Config(demo_password="local-placeholder", live_enabled=True)
        self.assertIn("API Key", cfg.problem())
        for changes in ({"region": "unknown"}, {"workspace": "x@evil.invalid/"}):
            bad = replace(cfg, **changes)
            self.assertIsNotNone(bad.problem())
            with self.assertRaises(ValueError):
                _ = bad.url

    def test_profiles_are_distinct(self):
        for model in ("qwen-audio-3.1-realtime-plus", "qwen-audio-3.0-realtime-plus", "qwen-audio-3.0-realtime-flash"):
            session = Config(model=model).session_update()["session"]
            self.assertEqual(session["input_audio_format"], "pcm")
            self.assertNotIn("audio", session)
            self.assertEqual(session["turn_detection"]["type"], "smart_turn")
        session = Config(model="qwen3.8-omni-flash-realtime").session_update()["session"]
        self.assertEqual(session["audio"]["output"]["format"]["sample_rate"], 24000)
        self.assertNotIn("input_audio_format", session)
        self.assertEqual(session["turn_detection"]["type"], "semantic_vad")

    def test_config_rejects_legacy_and_injection(self):
        cfg = Config(api_key="local-placeholder", demo_password="local-placeholder", workspace="test", live_enabled=True)
        self.assertIsNone(cfg.problem())
        for changes in ({"model": "qwen3-omni-flash-realtime-2025-09-15"}, {"region": "elsewhere"}, {"workspace": "x@evil.invalid/"}):
            self.assertIsNotNone(replace(cfg, **changes).problem())


if __name__ == "__main__":
    unittest.main()
