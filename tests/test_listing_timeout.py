import unittest
from unittest.mock import MagicMock,patch
from playwright.sync_api import TimeoutError
from core.shopify_fastpath import goto_listing


class ListingTimeoutTests(unittest.TestCase):
    def test_json_recovery_keeps_original_403_reason(self):
        from core.common import Client,CrawlError
        client=Client()
        with patch.object(client,'get',side_effect=CrawlError('HTTP Error 403')),patch.object(client,'browser_json',side_effect=TimeoutError('home timeout')):
            with self.assertRaisesRegex(CrawlError,'403.*home timeout'):client.json('https://example.com/collections/all/products.json')

    def test_loaded_same_collection_grid_continues_with_warning(self):
        page=MagicMock();page.url='https://example.com/collections/shop';page.goto.side_effect=TimeoutError('load timeout')
        page.locator.return_value.count.return_value=3;warnings=[]
        goto_listing(page,page.url,warnings)
        self.assertEqual(warnings[0]['code'],'listing_load_timeout')

    def test_empty_or_redirected_page_does_not_pass(self):
        for actual,count in [('https://example.com/collections/shop',0),('https://example.com/challenge',3),('https://other.example/collections/shop',3)]:
            page=MagicMock();page.url=actual;page.goto.side_effect=TimeoutError('load timeout');page.locator.return_value.count.return_value=count
            with self.subTest(actual=actual,count=count),self.assertRaises(TimeoutError):goto_listing(page,'https://example.com/collections/shop',[])

    def test_transient_connection_close_retries_before_reading_grid(self):
        from playwright.sync_api import Error
        page=MagicMock();page.url='https://example.com/collections/shop'
        page.goto.side_effect=[Error('net::ERR_CONNECTION_CLOSED'),None]
        page.locator.return_value.first.wait_for.return_value=None
        goto_listing(page,page.url,[])
        self.assertEqual(page.goto.call_count,2)
