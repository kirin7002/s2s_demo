"""UI-only fixtures: no microphone permission, model connection or device requests."""
import os
from pathlib import Path
import unittest
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = [
    ('user', '帮我安排一下今天的工作，想留一点时间散步。'),
    ('assistant', '当然。上午先做最需要专注的事，下午处理沟通。傍晚留半小时出去走走，怎么样？'),
    ('user', '不错，下午还有一个项目会议。'),
    ('assistant', '那就把会议前的 20 分钟留给准备：整理进展、待确认的问题，以及下一步安排。'),
    ('user', '好，帮我把这几个重点再梳理一下。'),
    ('assistant', '可以。今天先完成最重要的一件事；会议上确认分工和时间；最后给自己留一点放松的空间。'),
]

class ChatLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            executable_path=os.environ.get('S2S_TEST_BROWSER', '/usr/bin/chromium'), args=['--no-sandbox'])
        self.page = await self.browser.new_page(viewport={'width': 390, 'height': 844}, device_scale_factor=2)
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        async def route(r):
            path = r.request.url.split('demo.test')[-1]
            if path == '/api/config':
                return await r.fulfill(json={'ready': False, 'model': 'qwen-audio-3.0-realtime-flash',
                                            'message': '本地预览 · 未启用真实模型'})
            if path == '/api/stackchan':
                return await r.fulfill(json={'configured': True, 'online': False, 'bound': False,
                                            'simulated': True, 'device_id': 'ui-fixture'})
            if path.startswith('/api/'):
                raise AssertionError('Unexpected API mutation: ' + path)
            name = 'index.html' if path == '/chat' else path.lstrip('/')
            if name == 'app.js':
                # Export UI rendering functions only in the intercepted test response.
                return await r.fulfill(content_type='application/javascript', body=(ROOT/'web/app.js').read_text() +
                    '\nwindow.uiFixture={transcript,buttons,status,reset:()=>{assistant=null;}};')
            await r.fulfill(path=str(ROOT/'web'/name))
        await self.page.route('http://demo.test/**', route)
        await self.page.goto('http://demo.test/chat')
        await self.page.wait_for_function('!!window.uiFixture')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.assertFalse(self.errors)

    async def add(self, role, text):
        await self.page.evaluate('([role,text])=>{uiFixture.reset();uiFixture.transcript(role,text)}', [role, text])

    async def test_sizes_panel_and_call_states(self):
        for width, height in [(320,568), (375,667), (390,844), (430,932), (844,390), (1440,900)]:
            await self.page.set_viewport_size({'width':width, 'height':height})
            for expanded in [False, True]:
                if expanded:
                    await self.page.click('#connection-toggle')
                self.assertEqual(await self.page.locator('#connection-toggle').get_attribute('aria-expanded'), str(expanded).lower())
                self.assertFalse(await self.page.evaluate('document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight'))
                history = await self.page.locator('#transcripts').bounding_box()
                if not expanded and width < 800:
                    self.assertGreater(history['height'], height * .55)
                for busy, ready, state in [(False,False,'idle'), (True,False,'connecting'), (True,True,'listening'), (True,True,'responding')]:
                    await self.page.evaluate('([b,r,s])=>{uiFixture.buttons(b,r);uiFixture.status(s,"模拟通话状态",s)}', [busy,ready,state])
                    control = await self.page.locator('#stop' if busy else '#start').bounding_box()
                    self.assertLessEqual(control['y'] + control['height'], height)
                await self.page.evaluate('uiFixture.buttons(false)')
                if expanded:
                    await self.page.click('#connection-toggle')
            # Rapid toggles leave panel and accessibility state in agreement.
            await self.page.evaluate('()=>{for(let i=0;i<6;i++)document.getElementById("connection-toggle").click()}')
            self.assertTrue(await self.page.locator('#connection-panel').is_hidden())

    async def test_history_scroll_streaming_limit_and_clear(self):
        for i in range(40):
            await self.add('assistant' if i%2 else 'user', f'第 {i} 条对话。' + '较长的聊天内容，保持完整可读。'*4)
        self.assertTrue(await self.page.evaluate('(()=>{let h=document.getElementById("transcripts");return h.scrollHeight-h.scrollTop-h.clientHeight<2})()'))
        await self.page.evaluate('document.getElementById("transcripts").scrollTop=300')
        await self.page.wait_for_timeout(100)
        old = await self.page.locator('#transcripts').evaluate('(h)=>h.scrollTop')
        await self.add('assistant', '一条新消息')
        await self.page.evaluate('uiFixture.transcript("assistant","，流式补充内容")')
        self.assertEqual(await self.page.locator('#transcripts').evaluate('(h)=>h.scrollTop'), old)
        self.assertTrue(await self.page.locator('#new-messages').is_visible())
        await self.page.click('#new-messages')
        self.assertTrue(await self.page.locator('#new-messages').is_hidden())
        await self.add('user', 'https://example.test/' + 'x'*2000)
        self.assertFalse(await self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))
        for i in range(70):
            await self.add('user', f'后续记录 {i}')
        self.assertEqual(await self.page.locator('.utterance').count(), 100)
        await self.page.evaluate('document.getElementById("transcripts").scrollTop=500')
        await self.page.wait_for_timeout(100)
        anchor = await self.page.locator('.utterance').nth(15).text_content()
        y = (await self.page.locator('.utterance').nth(15).bounding_box())['y']
        await self.add('user', '超过 100 条后移除最旧记录')
        row = self.page.locator('.utterance').filter(has_text=anchor)
        self.assertAlmostEqual((await row.bounding_box())['y'], y, delta=2)
        await self.page.click('#clear')
        self.assertEqual(await self.page.locator('.utterance').count(), 0)
        self.assertTrue(await self.page.locator('.empty').is_visible())
        self.assertTrue(await self.page.locator('#new-messages').is_hidden())

    async def test_deliverable_screenshots(self):
        output = os.environ.get('S2S_UI_SCREENSHOTS')
        if not output:
            self.skipTest('Set S2S_UI_SCREENSHOTS to save review screenshots')
        out = Path(output); out.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(out/'after-mobile-empty-390.png'))
        for role, text in MESSAGES:
            await self.add(role, text)
        # The screenshot text explicitly identifies simulated preview content.
        await self.page.evaluate('uiFixture.status("演示预览", "以下为示例对话 · 未连接模型、麦克风或设备", "idle")')
        for width,height in [(320,568),(390,844),(430,932),(1440,900)]:
            await self.page.set_viewport_size({'width':width,'height':height})
            await self.page.screenshot(path=str(out/f'after-chat-{width}.png'))
        await self.page.set_viewport_size({'width':390,'height':844})
        await self.page.click('#connection-toggle')
        await self.page.screenshot(path=str(out/'after-mobile-connection-390.png'))
