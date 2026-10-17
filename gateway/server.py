"""Same-origin browser PCM -> Qwen Audio/Omni Realtime WebSocket gateway."""
import asyncio
import base64
import contextlib
import json
import hashlib
import hmac
import logging
import secrets
from pathlib import Path
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, WSServerHandshakeError, web
from .config import Config

ROOT = Path(__file__).resolve().parent.parent / "web"
CONFIG = web.AppKey("config", Config)
CLIENT = web.AppKey("client", ClientSession)
ACTIVE = web.AppKey("active", set)
CONNECTOR = web.AppKey("connector", object)
LOGINS = web.AppKey("logins", dict)
ATTEMPTS = web.AppKey("attempts", dict)
OWNERS = web.AppKey("owners", dict)
LOGGER = logging.getLogger(__name__)
STAGES = {"connect": "上游握手", "session_created": "等待会话创建",
          "session_updated": "等待音频配置确认", "streaming": "语音会话"}


def authenticated(request):
    tokens = request.app[LOGINS]
    now = time.monotonic()
    for token, expires in list(tokens.items()):
        if expires < now:
            tokens.pop(token, None)
    return request.cookies.get("s2s_session") in tokens


async def login(request):
    cfg = request.app[CONFIG]
    if request.headers.get("Origin") != cfg.origin:
        raise web.HTTPForbidden()
    now, peer = time.monotonic(), request.remote
    attempts = request.app[ATTEMPTS]
    for key, (_, started) in list(attempts.items()):
        if now - started >= 60:
            attempts.pop(key, None)
    count, started = attempts.get(peer, (0, now))
    if count >= 5 or len(attempts) >= 1024:
        return web.json_response({"error": "尝试过于频繁，请一分钟后重试。"}, status=429)
    attempts[peer] = (count + 1, started)
    if request.content_length is None or request.content_length > 2048:
        raise web.HTTPBadRequest()
    try:
        payload = await request.json()
    except ValueError:
        raise web.HTTPBadRequest() from None
    password = payload.get("password") if isinstance(payload, dict) else None
    if not isinstance(password, str):
        raise web.HTTPBadRequest()
    if cfg.demo_password:
        if not hmac.compare_digest(hashlib.sha256(password.encode()).digest(),
                                   hashlib.sha256(cfg.demo_password.encode()).digest()):
            return web.json_response({"error": "口令不正确。"}, status=401)
    elif cfg.live_enabled:
        return web.json_response({"error": "服务端尚未配置登录口令。"}, status=503)
    authenticated(request)  # prune expired sessions
    if len(request.app[LOGINS]) >= 256:
        return web.json_response({"error": "登录会话已满，请稍后重试。"}, status=429)
    token = secrets.token_urlsafe(32)
    request.app[LOGINS][token] = now + 3600
    response = web.json_response({"ok": True})
    response.set_cookie("s2s_session", token, httponly=True, samesite="Strict",
                        secure=cfg.origin.startswith("https://"), max_age=3600, path="/")
    return response


async def logout(request):
    if request.headers.get("Origin") != request.app[CONFIG].origin:
        raise web.HTTPForbidden()
    token = request.cookies.get("s2s_session")
    request.app[LOGINS].pop(token, None)
    # Revoke already-open model sessions in other tabs as well as future upgrades.
    await asyncio.gather(*(ws.close(code=1008, message=b"Logged out")
                           for ws, owner in list(request.app[OWNERS].items()) if owner == token))
    response = web.json_response({"ok": True})
    response.del_cookie("s2s_session")
    return response


class ProtocolError(Exception):
    def __init__(self, message, code="PROTOCOL_ERROR"):
        super().__init__(message)
        self.code = code


class Session:
    def __init__(self, browser, config, connect):
        self.browser, self.config, self.connect = browser, config, connect
        self.upstream = None
        self.connect_task = None
        self.ready = False
        self.responding = False
        self.cancel_pending = False
        self.suppressed = False
        self.audio_window = time.monotonic()
        self.audio_bytes = 0
        self.stage = "connect"

    async def emit(self, kind, **fields):
        await asyncio.wait_for(self.browser.send_json({"type": kind, **fields}), 5)

    async def upstream_send(self, event):
        await asyncio.wait_for(self.upstream.send_json(event), 5)

    async def interrupt(self, automatic=False):
        self.suppressed = True
        if self.responding and not self.cancel_pending and not (automatic and self.config.is_audio):
            self.cancel_pending = True
            await self.upstream_send({"type": "response.cancel"})
        await self.emit("interrupted")

    async def from_browser(self):
        while True:
            msg = await self.browser.receive(timeout=self.config.idle_seconds)
            if msg.type == WSMsgType.BINARY:
                data = msg.data
                if not self.ready or not data or len(data) % 2 or len(data) > 6400:
                    raise ProtocolError("音频格式或会话状态无效。")
                now = time.monotonic()
                if now - self.audio_window >= 1:
                    self.audio_window, self.audio_bytes = now, 0
                self.audio_bytes += len(data)
                if self.audio_bytes > 64000:
                    raise ProtocolError("音频发送过快，请重新连接。")
                await self.upstream_send({"type": "input_audio_buffer.append",
                                          "audio": base64.b64encode(data).decode("ascii")})
            elif msg.type == WSMsgType.TEXT:
                try:
                    event = json.loads(msg.data)
                except ValueError:
                    raise ProtocolError("无效控制消息。") from None
                if not isinstance(event, dict) or set(event) != {"type"}:
                    raise ProtocolError("无效控制消息。")
                if event["type"] == "stop":
                    return
                if event["type"] == "interrupt" and self.ready:
                    await self.interrupt()
                else:
                    raise ProtocolError("不支持的控制消息。")
            else:
                return

    async def receive_event(self, timeout=None):
        msg = await self.upstream.receive(timeout=timeout)
        if msg.type != WSMsgType.TEXT:
            raise ProtocolError("模型连接已断开，请重新连接。")
        try:
            event = json.loads(msg.data)
        except ValueError:
            raise ProtocolError("模型返回无效协议消息。") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ProtocolError("模型返回无效协议消息。")
        if event["type"] == "error":
            # Upstream messages can contain request/config details. Do not relay or log them.
            raise ProtocolError("模型返回错误，请核对服务端地域、API Key、模型权限及配额。", "UPSTREAM_EVENT_ERROR")
        return event

    async def from_upstream(self):
        async def acquire():
            async with asyncio.timeout(self.config.handshake_seconds):
                return await self.connect()
        self.connect_task = asyncio.create_task(acquire())
        # Let an in-flight upgrade finish under its deadline even if the browser
        # leaves, so its returned socket is always owned and closed in finally.
        self.upstream = await asyncio.shield(self.connect_task)
        self.stage = "session_created"
        async with asyncio.timeout(self.config.handshake_seconds):
            while (await self.receive_event())["type"] != "session.created":
                pass
            await self.upstream_send(self.config.session_update())
            self.stage = "session_updated"
            while (await self.receive_event())["type"] != "session.updated":
                pass
        self.ready = True
        self.stage = "streaming"
        await self.emit("ready", model=self.config.model, input_rate=16000, output_rate=24000)
        while True:
            event = await self.receive_event(self.config.idle_seconds)
            kind = event["type"]
            if kind == "response.created":
                self.responding, self.cancel_pending, self.suppressed = True, False, False
                await self.emit("responding")
            elif kind == "input_audio_buffer.speech_started":
                await self.interrupt(automatic=True)
                await self.emit("listening")
            elif kind == "response.audio.delta" and not self.suppressed:
                try:
                    pcm = base64.b64decode(event["delta"], validate=True)
                except (ValueError, KeyError, TypeError):
                    raise ProtocolError("模型返回无效音频。") from None
                if len(pcm) % 2:
                    raise ProtocolError("模型返回无效音频。")
                await asyncio.wait_for(self.browser.send_bytes(pcm), 5)
            elif kind in ("response.audio_transcript.delta", "response.text.delta") and not self.suppressed:
                await self.emit("transcript", role="assistant", text=str(event.get("delta", ""))[:16000])
            elif kind == "conversation.item.input_audio_transcription.completed":
                await self.emit("transcript", role="user", text=str(event.get("transcript", ""))[:16000])
            elif kind == "conversation.item.input_audio_transcription.failed":
                await self.emit("notice", message="本轮输入转写失败，语音会话仍可继续。")
            elif kind == "response.done":
                self.responding, self.cancel_pending = False, False
                response = event.get("response", {})
                if response.get("status") == "failed":
                    raise ProtocolError("本轮模型响应失败，请重新连接。")
                await self.emit("turn_done")

    async def report_failure(self, code, message, http_status=0):
        # Only fixed local codes/stages and numeric HTTP status are recorded.
        # Never log exception repr, upstream error payloads, headers or audio.
        LOGGER.warning("s2s_failure stage=%s code=%s http_status=%d", self.stage, code, http_status)
        with contextlib.suppress(Exception):
            await self.emit("error", message=f"{message}（阶段：{STAGES[self.stage]}；诊断码：{code}）",
                            code=code, stage=self.stage)

    async def run(self):
        tasks = [asyncio.create_task(self.from_browser()), asyncio.create_task(self.from_upstream())]
        try:
            async with asyncio.timeout(self.config.max_seconds):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except ProtocolError as error:
            await self.report_failure(error.code, str(error))
        except WSServerHandshakeError as error:
            status = error.status if isinstance(error.status, int) and 100 <= error.status <= 599 else 0
            await self.report_failure("UPSTREAM_HTTP_REJECTED", f"模型握手被拒绝，HTTP {status}。", status)
        except ClientError:
            await self.report_failure("UPSTREAM_NETWORK_ERROR", "模型网络连接失败，请检查服务器网络。")
        except TimeoutError:
            await self.report_failure("SESSION_TIMEOUT", "连接等待、传输或会话时长已超限，请重新连接。")
        except Exception:
            await self.report_failure("SESSION_INTERNAL_ERROR", "模型连接失败，请核对服务端配置或稍后重连。")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.connect_task is not None:
                with contextlib.suppress(Exception):
                    self.upstream = await self.connect_task
            if self.upstream is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.upstream.close(), 2)
            await self.browser.close()


async def socket(request):
    cfg = request.app[CONFIG]
    if request.headers.get("Origin") != cfg.origin:
        LOGGER.warning("s2s_rejected code=LOCAL_ORIGIN_REJECTED")
        raise web.HTTPForbidden(text="Origin rejected")
    if not authenticated(request):
        LOGGER.warning("s2s_rejected code=LOCAL_AUTH_REQUIRED")
        raise web.HTTPUnauthorized(text="Login required")
    if cfg.problem():
        return web.json_response({"error": cfg.problem()}, status=503)
    active = request.app[ACTIVE]
    if len(active) >= cfg.max_sessions:
        LOGGER.warning("s2s_rejected code=LOCAL_SESSION_LIMIT")
        return web.json_response({"error": "会话数量已达上限。"}, status=429)
    # Reserve before any await so parallel upgrades cannot exceed the cap.
    reservation = object()
    active.add(reservation)
    browser = web.WebSocketResponse(heartbeat=20, max_msg_size=8192)
    try:
        await browser.prepare(request)
        if not authenticated(request):  # logout may have raced the upgrade await
            await browser.close(code=1008, message=b"Login required")
            return browser
        active.remove(reservation)
        active.add(browser)
        request.app[OWNERS][browser] = request.cookies.get("s2s_session")
        async def connect():
            if CONNECTOR in request.app:  # injected by local tests only, never an env-configured URL
                return await request.app[CONNECTOR]()
            return await request.app[CLIENT].ws_connect(
                cfg.url, headers={"Authorization": f"Bearer {cfg.api_key}"},
                heartbeat=20, max_msg_size=2 * 1024 * 1024)
        await Session(browser, cfg, connect).run()
    finally:
        active.discard(reservation)
        active.discard(browser)
        request.app[OWNERS].pop(browser, None)
    return browser


async def metadata(request):
    cfg = request.app[CONFIG]
    return web.json_response({"provider": "Alibaba Cloud Qwen Realtime", "model": cfg.model,
                              "region": cfg.region, "ready": cfg.problem() is None,
                              "message": cfg.problem(), "memory": "external interface only",
                              "password_required": bool(cfg.demo_password) or cfg.live_enabled,
                              "tools": "disabled"})


async def asset(request):
    name = request.match_info.get("name", "index.html")
    if name not in ("login.js", "app.js", "capture.js", "style.css"):
        raise web.HTTPNotFound()
    return web.FileResponse(ROOT / name)


async def entry(request):
    raise web.HTTPFound("/chat" if authenticated(request) else "/login")


async def chat(request):
    if not authenticated(request):
        raise web.HTTPFound("/login")
    return web.FileResponse(ROOT / "index.html")


async def login_page(request):
    return web.FileResponse(ROOT / "login.html")


@web.middleware
async def security_headers(request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as error:
        # Authentication failures and redirects need the same cache/frame policy.
        response = error
    if not response.prepared:
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'",
                                 "Permissions-Policy": "microphone=(self), camera=()"})
    if isinstance(response, web.HTTPException):
        raise response
    return response


def create_app(config=None, connector=None):
    app = web.Application(middlewares=[security_headers])
    app[CONFIG], app[ACTIVE] = config or Config.from_env(), set()
    app[LOGINS], app[ATTEMPTS], app[OWNERS] = {}, {}, {}
    if connector is not None:
        app[CONNECTOR] = connector
    async def resources(app):
        async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=10)) as client:
            app[CLIENT] = client
            yield
    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001, message=b"Server shutdown")
                               for ws in list(app[ACTIVE]) if hasattr(ws, "close")))
    app.cleanup_ctx.append(resources)
    app.on_shutdown.append(shutdown)
    app.router.add_get("/api/config", metadata)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/ws", socket)
    app.router.add_get("/", entry)
    app.router.add_get("/login", login_page)
    app.router.add_get("/chat", chat)
    app.router.add_get("/{name}", asset)
    return app


if __name__ == "__main__":
    # Local by default; public hosting and production identity remain outside this demo.
    web.run_app(create_app(), host="127.0.0.1", port=8080, access_log=None)
