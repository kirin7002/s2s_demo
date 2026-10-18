"""Chromium mobile viewport + synthetic microphone + localhost upstream only.

Permission denial/pending prompts and system audio interruption are injected.
This is not physical Safari/iOS or real-model validation.
"""
import asyncio
import os
from pathlib import Path
import sys
import unittest
from playwright.async_api import async_playwright
from tests.test_gateway import Harness
from gateway.server import ACTIVE


MONITOR = """(() => {
 window.__streams = []; window.__micCalls = 0; window.__sources = [];
 const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
 navigator.mediaDevices.getUserMedia = async (...args) => {
   window.__micCalls++; const stream = await original(...args);
   window.__streams.push(stream); return stream;
 };
 const Original = window.AudioContext; window.__contexts = [];
 window.AudioContext = class extends Original {
   constructor(...args) { super(...args); window.__contexts.push(this); }
   createBufferSource() {
     const source = super.createBufferSource(); window.__sources.push(source);
     const stop = source.stop.bind(source), disconnect = source.disconnect.bind(source);
     source.stop = (...args) => { source.__stopped = true; return stop(...args); };
     source.disconnect = (...args) => { source.__disconnected = true; return disconnect(...args); };
     return source;
   }
 };
 const OriginalSocket = window.WebSocket; window.__sockets = [];
 window.WebSocket = class extends OriginalSocket {
   constructor(...args) { super(...args); window.__sockets.push(this); }
 };
})();"""


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = await Harness().open()
        self.h.mode = "turns"
        self.pw = await async_playwright().start()
        browser_path = os.environ.get('S2S_TEST_BROWSER')
        if not browser_path:
            candidate = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' if sys.platform == 'darwin' else '/usr/bin/chromium'
            browser_path = candidate if Path(candidate).is_file() else None
        self.browser = await self.pw.chromium.launch(executable_path=browser_path, args=["--no-sandbox", "--use-fake-device-for-media-stream"])
        self.context = await self.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, permissions=["microphone"])
        await self.context.add_init_script(MONITOR)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        # App config changes after startup are avoided by mutating the test fixture's
        # frozen object internally; never used by production code.
        object.__setattr__(self.h.cfg, "origin", str(self.h.client.make_url("/")).rstrip("/"))
        await self.context.request.post(str(self.h.client.make_url("/api/login")),
                                        data={"password": self.h.cfg.demo_password},
                                        headers={"Origin": self.h.cfg.origin})
        await self.page.goto(str(self.h.client.make_url("/chat")))

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        await self.h.close()
        self.assertFalse(self.errors)

    async def listening(self):
        await self.page.wait_for_function("() => !document.getElementById('interrupt').disabled")

    async def released(self):
        await self.page.wait_for_function("() => window.__streams.every(s => s.getTracks().every(t => t.readyState === 'ended')) && window.__contexts.every(c => c.state === 'closed')")
        await asyncio.sleep(.1)
        self.assertFalse(self.h.app[ACTIVE])

    async def test_mobile_stream_stop_reconnect_and_double_tap(self):
        await self.page.evaluate("document.getElementById('start').click(); document.getElementById('start').click()")
        await self.listening()
        await self.page.wait_for_function("() => document.getElementById('transcripts').textContent.includes('本地测试轮次')")
        self.assertEqual(await self.page.evaluate("window.__micCalls"), 1)
        self.assertFalse(await self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        await self.page.click("#interrupt")
        await self.page.click("#stop")
        await self.released()
        await self.page.click("#start")
        await self.listening()
        await self.page.click("#stop")
        await self.released()

    async def test_permission_denied(self):
        await self.page.evaluate("() => { navigator.mediaDevices.getUserMedia = async () => {throw new DOMException('denied','NotAllowedError')}; }")
        await self.page.click("#start")
        await self.page.wait_for_function("() => document.getElementById('status').textContent.includes('权限被拒绝')")
        self.assertEqual(len(self.h.upstreams), 0)
        await self.released()

    async def test_login_plain_text_rejection_has_readable_error(self):
        await self.page.goto(str(self.h.client.make_url('/login')))
        await self.page.wait_for_function("() => !document.getElementById('login-submit').disabled")
        await self.page.route('**/api/login', lambda route: route.fulfill(
            status=403, content_type='text/plain', body='synthetic-internal-detail'))
        await self.page.fill('#password', 'synthetic-input')
        await self.page.click('#login-submit')
        await self.page.wait_for_function("() => document.getElementById('login-message').textContent.includes('登录请求被拒绝')")
        self.assertNotIn('synthetic-internal-detail', await self.page.locator('#login-message').inner_text())
        self.assertFalse(await self.page.locator('#login-submit').is_disabled())

    async def test_cancel_pending_permission_releases_late_stream(self):
        await self.page.evaluate("""async () => {
          window.__lateStream = await navigator.mediaDevices.getUserMedia({audio:true});
          navigator.mediaDevices.getUserMedia = () => new Promise(resolve => window.__allowLater = () => resolve(window.__lateStream));
        }""")
        await self.page.click("#start")
        await self.page.wait_for_function("() => !!window.__allowLater")
        await self.page.click("#stop")
        await self.page.evaluate("window.__allowLater()")
        await self.page.wait_for_function("() => window.__streams.length === 1")
        await self.released()
        self.assertEqual(len(self.h.upstreams), 0)

    async def test_offline_releases_and_allows_reconnect(self):
        await self.page.click("#start")
        await self.listening()
        await self.page.evaluate("window.dispatchEvent(new Event('offline'))")
        await self.page.wait_for_function("() => document.getElementById('status').textContent.includes('离线')")
        await self.released()

    async def test_upstream_disconnect(self):
        await self.page.click("#start")
        await self.listening()
        await self.h.upstreams[0].close()
        await self.page.wait_for_function("() => !document.getElementById('start').disabled")
        await self.released()

    async def test_audio_interruption(self):
        await self.page.click("#start")
        await self.listening()
        await self.page.evaluate("window.__contexts[0].suspend()")
        await self.page.wait_for_function("() => document.getElementById('status').textContent.includes('音频已暂停')")
        await self.released()

    async def test_background_releases(self):
        await self.page.click("#start")
        await self.listening()
        await self.page.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'))")
        await self.page.wait_for_function("() => document.getElementById('status').textContent.includes('后台')")
        await self.released()

    async def test_login_two_pages_and_logout(self):
        await self.context.request.post(str(self.h.client.make_url("/api/logout")), headers={"Origin": self.h.cfg.origin})
        await self.page.goto(str(self.h.client.make_url("/chat")))
        self.assertTrue(self.page.url.endswith("/login"))
        await self.page.fill("#password", "wrong")
        await self.page.click("#login-submit")
        await self.page.wait_for_function("() => document.getElementById('login-message').textContent.includes('不正确')")
        await self.page.fill("#password", self.h.cfg.demo_password)
        await self.page.click("#login-submit")
        await self.page.wait_for_url("**/chat")
        await self.page.click("#logout")
        await self.page.wait_for_url("**/login")

    async def test_device_binding_is_explicit_and_disabled_during_call(self):
        from gateway.server import DEVICES
        for name, value in [('stackchan_enabled', True), ('stackchan_device_id', 'local_simulator'),
                             ('stackchan_device_token', 'synthetic-test-token-without-real-access')]:
            object.__setattr__(self.h.cfg, name, value)
        await self.page.reload()
        await self.page.wait_for_function("() => !document.getElementById('device-panel').hidden")
        self.assertIn('离线', await self.page.locator('#device-status').inner_text())
        await self.page.click('#device-bind')
        await self.page.wait_for_function("() => document.getElementById('device-bind').textContent === '取消关联'")
        self.assertEqual(len(self.h.app[DEVICES].bindings), 1)
        for width, height in [(320, 568), (390, 844)]:
            await self.page.set_viewport_size({'width': width, 'height': height})
            self.assertFalse(await self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
            box = await self.page.locator('#start').bounding_box()
            self.assertLessEqual(box['y'] + box['height'], height)
        # No device is online, but the voice session may still query its status.
        self.h.mode = 'normal'
        await self.page.click('#start')
        await self.listening()
        self.assertTrue(await self.page.locator('#device-bind').is_disabled())
        await self.page.click('#stop')
        await self.released()
        await self.page.click('#device-bind')
        await self.page.wait_for_function("() => document.getElementById('device-bind').textContent === '关联设备'")
        self.assertFalse(self.h.app[DEVICES].bindings)

    async def test_old_socket_callbacks_do_not_change_new_session(self):
        await self.page.click("#start")
        await self.listening()
        await self.page.evaluate("() => { const ws=window.__sockets[0]; window.__oldCallbacks=[ws.onclose,ws.onerror,ws.onmessage]; }")
        await self.page.click("#stop")
        await self.released()
        await self.page.click("#start")
        await self.listening()
        await self.page.evaluate("() => { window.__oldCallbacks[0](); window.__oldCallbacks[1](); window.__oldCallbacks[2]({data:JSON.stringify({type:'error',message:'old error'})}); }")
        self.assertTrue(await self.page.locator("#start").is_disabled())
        self.assertEqual(await self.page.evaluate("window.__micCalls"), 2)
        self.assertFalse(await self.page.evaluate("document.getElementById('detail').textContent.includes('old error')"))
        await self.page.click("#stop")
        await self.released()

    async def test_pagehide_resets_for_back_navigation(self):
        await self.page.click("#start")
        await self.listening()
        await self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        await self.released()
        await self.page.click("#start")
        await self.listening()
        await self.page.click("#stop")
        await self.released()

    async def test_output_backpressure_includes_incoming_packet(self):
        import base64
        self.h.mode = "normal"
        await self.page.click("#start")
        await self.listening()
        await self.h.upstreams[0].send_json({"type": "response.created"})
        await self.h.upstreams[0].send_json({"type": "response.audio.delta", "delta": base64.b64encode(b"\x00\x00" * 24000 * 9).decode()})
        await self.page.wait_for_function("() => document.getElementById('transcripts').textContent.includes('本次回复已打断')")
        self.assertTrue(await self.page.locator('#start').is_disabled())
        self.assertEqual(await self.page.evaluate("window.__streams[0].getTracks()[0].readyState"), 'live')
        self.assertEqual(await self.page.evaluate("window.__sources.length"), 0)
        # Suppress residual audio, then play the next response on the same WS.
        await self.h.upstreams[0].send_json({'type': 'response.created'})
        await self.h.upstreams[0].send_json({'type': 'response.audio.delta', 'delta': base64.b64encode(bytes(4800)).decode()})
        await self.page.wait_for_function('() => window.__sources.length === 1')
        self.assertEqual(await self.page.evaluate('window.__sockets.length'), 1)
        await self.page.click('#stop')
        await self.released()

    async def test_tool_diagnostic_is_text_only_and_keeps_call_alive(self):
        self.h.mode = 'normal'
        await self.page.click('#start')
        await self.listening()
        await self.page.evaluate("""() => window.__sockets[0].onmessage({data: JSON.stringify({
            type:'tool_diagnostic', tool:'stackchan_perform', error_code:'OUT_OF_RANGE',
            details:{reason:'speed', frame_index:1, minimum_move_ms:1334}
        })})""")
        self.assertIn('1334', await self.page.locator('#transcripts').inner_text())
        await self.page.evaluate("""() => window.__sockets[0].onmessage({data: JSON.stringify({
            type:'tool_timing', requested_duration_ms:2500, effective_duration_ms:3501
        })})""")
        self.assertIn('2500 → 3501', await self.page.locator('#transcripts').inner_text())
        self.assertEqual(await self.page.evaluate('window.__sources.length'), 0)
        self.assertTrue(await self.page.locator('#start').is_disabled())
        await self.page.click('#stop')
        await self.released()

    async def test_interrupt_stops_and_disconnects_queued_audio(self):
        import base64
        self.h.mode = "normal"
        await self.page.click("#start")
        await self.listening()
        await self.h.upstreams[0].send_json({"type": "response.created"})
        await self.h.upstreams[0].send_json({"type": "response.audio.delta", "delta": base64.b64encode(b"\x00\x00" * 24000 * 2).decode()})
        await self.page.wait_for_function("() => window.__sources.length === 1")
        await self.page.click("#interrupt")
        await self.page.wait_for_function("() => window.__sources[0].__stopped && window.__sources[0].__disconnected")
        await asyncio.sleep(.1)
        self.assertEqual(await self.page.evaluate("window.__sources.length"), 1)
        await self.page.click("#stop")
        await self.released()

    async def test_call_controls_timer_mute_and_history(self):
        self.h.mode = 'normal'
        self.assertTrue(await self.page.locator('#start').is_visible())
        self.assertFalse(await self.page.locator('#stop').is_visible())
        await self.page.click('#start')
        await self.listening()
        self.assertFalse(await self.page.locator('#start').is_visible())
        self.assertTrue(await self.page.locator('#stop').is_visible())
        await self.page.wait_for_function("() => document.getElementById('duration').textContent !== '00:00'")
        await self.page.click('#mute')
        self.assertEqual(await self.page.locator('#mute').get_attribute('aria-pressed'), 'true')
        self.assertTrue(await self.page.evaluate("window.__streams[0].getAudioTracks().every(t => !t.enabled)"))
        await asyncio.sleep(.25)
        # Drain packets captured before muting; none should arrive while muted.
        while not self.h.events.empty():
            self.h.events.get_nowait()
        await asyncio.sleep(.25)
        self.assertTrue(self.h.events.empty())
        await self.page.click('#mute')
        self.assertTrue(await self.page.evaluate("window.__streams[0].getAudioTracks().every(t => t.enabled)"))
        await self.h.upstreams[0].send_json({'type': 'response.audio_transcript.delta', 'delta': '本地对话记录'})
        await self.page.wait_for_function("() => document.getElementById('transcripts').textContent.includes('本地对话记录')")
        await self.page.click('#stop')
        await self.released()
        self.assertTrue(await self.page.locator('#start').is_visible())
        self.assertTrue(await self.page.locator('#transcripts').get_by_text('本地对话记录').is_visible())
        await self.page.click('#clear')
        self.assertTrue(await self.page.locator('.empty').is_visible())
        self.assertEqual(await self.page.locator('.utterance').count(), 0)

    async def test_small_phone_and_desktop_keep_controls_visible(self):
        for width, height in [(320, 568), (390, 844), (1440, 1000)]:
            await self.page.set_viewport_size({'width': width, 'height': height})
            self.assertFalse(await self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
            box = await self.page.locator('#start').bounding_box()
            self.assertLessEqual(box['y'] + box['height'], height)
            self.assertGreaterEqual(box['width'], 44)


if __name__ == "__main__":
    unittest.main()
