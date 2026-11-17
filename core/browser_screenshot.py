"""Capture actual Chromium pixels when a remote font stalls Playwright."""
import base64
from pathlib import Path


def screenshot(page, path, full_page=False):
    if getattr(page, '_crawl_font_timeout', False) is not True:
        try:
            page.screenshot(path=str(path), full_page=full_page, animations='disabled', timeout=3000)
            return {'backend':'playwright','font_wait_timed_out':False}
        except Exception as error:
            # Only bypass the font-readiness wait, never unrelated navigation,
            # closed-page, rendering or filesystem failures.
            if 'Timeout' not in str(error) or 'waiting for fonts to load' not in str(error):
                raise
            page._crawl_font_timeout = True
    session=page.context.new_cdp_session(page)
    try:
        options={'format':'png','fromSurface':True,'captureBeyondViewport':bool(full_page)}
        if full_page:
            size=session.send('Page.getLayoutMetrics')['cssContentSize']
            options['clip']={'x':0,'y':0,'width':size['width'],'height':size['height'],'scale':1}
        result=session.send('Page.captureScreenshot',options)
        pixels=base64.b64decode(result['data'],validate=True)
        if not pixels.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Chromium returned an invalid PNG')
        Path(path).write_bytes(pixels)
        return {'backend':'chromium_cdp','font_wait_timed_out':True}
    finally:
        session.detach()
