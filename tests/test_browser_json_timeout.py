import unittest
from unittest.mock import patch
from playwright.sync_api import TimeoutError as BrowserTimeout
from core.common import Client, CrawlError


class BrowserJSONTimeoutTests(unittest.TestCase):
    def run_browser(self, final_url, body, visible=True):
        with patch('playwright.sync_api.sync_playwright') as pw:
            browser=pw.return_value.__enter__.return_value.chromium.launch.return_value
            context=browser.new_context.return_value
            context.cookies.return_value=[]
            page=context.new_page.return_value
            def navigate(url,**kwargs):
                page.url=url if visible else 'about:blank'
                raise BrowserTimeout('late resource')
            page.goto.side_effect=navigate
            page.locator.return_value.count.return_value=int(visible)
            page.evaluate.return_value={'status':200,'url':final_url,'body':body}
            return Client().browser_json('https://example.com/collections/all/products.json')

    def test_timeout_accepts_only_parsed_same_source_json(self):
        self.assertEqual(self.run_browser('https://example.com/collections/all/products.json','{"products":[]}'),{'products':[]})

    def test_timeout_still_rejects_foreign_source_and_html(self):
        for url,body in [('https://other.example/products.json','{"products":[]}'),('https://example.com/collections/all/products.json','<html>challenge</html>')]:
            with self.subTest(url=url),self.assertRaises(CrawlError):self.run_browser(url,body)

    def test_incomplete_warmup_is_not_accepted(self):
        with self.assertRaises(CrawlError):self.run_browser('https://example.com/collections/all/products.json','{}',visible=False)
