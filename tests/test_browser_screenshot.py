import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from core.browser_screenshot import screenshot


class ScreenshotTests(unittest.TestCase):
    def test_font_wait_uses_pixels_and_subsequent_frames_skip_wait(self):
        page=MagicMock()
        page.screenshot.side_effect=RuntimeError('Timeout 3000ms exceeded: waiting for fonts to load...')
        session=page.context.new_cdp_session.return_value
        png=b'\x89PNG\r\n\x1a\nreal browser pixels'
        session.send.return_value={'data':base64.b64encode(png).decode()}
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'frame.png'
            self.assertEqual(screenshot(page,p)['backend'],'chromium_cdp')
            self.assertEqual(p.read_bytes(),png)
            screenshot(page,p)
            page.screenshot.assert_called_once()
            self.assertEqual(session.detach.call_count,2)

    def test_unrelated_failure_does_not_get_bypassed(self):
        page=MagicMock();page.screenshot.side_effect=RuntimeError('Target page closed')
        with self.assertRaisesRegex(RuntimeError,'closed'):screenshot(page,'unused.png')
        page.context.new_cdp_session.assert_not_called()

    def test_full_page_clip_and_invalid_png_are_checked(self):
        page=MagicMock();page._crawl_font_timeout=True
        session=page.context.new_cdp_session.return_value
        session.send.side_effect=[{'cssContentSize':{'width':1440,'height':5000}}, {'data':base64.b64encode(b'not PNG').decode()}]
        with self.assertRaisesRegex(ValueError,'invalid PNG'):screenshot(page,'unused.png',full_page=True)
        self.assertEqual(session.send.call_args.args[1]['clip'],{'x':0,'y':0,'width':1440,'height':5000,'scale':1})
        session.detach.assert_called_once()

    def test_real_chromium_pending_font_is_captured(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as d,sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:
                page=browser.new_page(viewport={'width':800,'height':600})
                pending=[]
                page.route('**/stalled.woff2',lambda route:pending.append(route))
                page.set_content('<style>@font-face{font-family:Slow;src:url(https://font.test/stalled.woff2);font-display:swap}body{font-family:Slow,Arial}</style><header>Shop</header>',wait_until='domcontentloaded')
                page.wait_for_function("document.fonts.status === 'loading'")
                p=Path(d)/'pending-font.png'
                result=screenshot(page,p)
                self.assertEqual(result['backend'],'chromium_cdp')
                self.assertGreater(p.stat().st_size,100)
                self.assertEqual(page.locator('header').inner_text(),'Shop')
                for route in pending:route.abort()
            finally:browser.close()
