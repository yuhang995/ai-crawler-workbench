import unittest
from types import SimpleNamespace
from core.detector import canonical_storefront, resolve_storefront
from core.common import CrawlError

class CanonicalTests(unittest.TestCase):
    def test_hydrogen_geo_redirect_recovers_verified_same_store_us_locale(self):
        marker = '<script src="https://cdn.shopify.com/oxygen-v2/app.js"></script>'
        cdn = '<img src="https://cdn.shopify.com/s/files/1/043925373087/item.jpg">'
        source = SimpleNamespace(
            url='https://store.test/ja-jp',
            text=marker + cdn + 'Shopify.country = "JP"; '
                 'Shopify.currency = {"active":"USD"};')
        us = SimpleNamespace(
            url='https://store.test/en-us',
            text=marker + cdn + 'Shopify.country = "US"; '
                 'Shopify.currency = {"active":"USD"};')
        client = SimpleNamespace(get=lambda url: us if url == us.url else self.fail(url))
        self.assertIs(resolve_storefront(client, 'https://store.test/', source), us)

    def test_hydrogen_us_locale_requires_same_shopify_store_fingerprint(self):
        marker = '<script src="https://cdn.shopify.com/oxygen-v2/app.js"></script>'
        source = SimpleNamespace(
            url='https://store.test/ja-jp',
            text=marker + '<img src="https://cdn.shopify.com/s/files/1/111/a.jpg">')
        us = SimpleNamespace(
            url='https://store.test/en-us',
            text=marker + '<img src="https://cdn.shopify.com/s/files/1/222/a.jpg">' +
                 'Shopify.country = "US"; Shopify.currency = {"active":"USD"};')
        self.assertIs(resolve_storefront(
            SimpleNamespace(get=lambda url: us), 'https://store.test/', source), source)

    def test_braun_global_geo_redirect_uses_verified_us_homepage(self):
        target = 'https://us.braun.com/en-us'
        data = ('{"props":{"pageProps":{"settings":{"siteName":"Braun"},'
                '"content":{"headerMenu":{"fields":{"contentType":"miaHeader","menuItems":[{}]}}}}}}')
        source = SimpleNamespace(url='https://www.braun.ro/ro-ro', text='')
        html = (f'<html lang="en-US"><link rel="canonical" href="{target}">'
                f'<script id="__NEXT_DATA__" type="application/json">{data}</script></html>')
        us = SimpleNamespace(url=target, text=html)
        client = SimpleNamespace(get=lambda url: us if url == target else self.fail(url))
        self.assertIs(resolve_storefront(client, 'https://braun.com/', source), us)
        us.text = html.replace('en-US', 'ro-RO')
        with self.assertRaises(CrawlError):
            resolve_storefront(client, 'https://braun.com/', source)

    def response(self, url, shop):
        return SimpleNamespace(url=url, text='<link rel="canonical" href="https://canonical.test/">' + f'Shopify.shop = "{shop}"; Shopify.country = "US"; Shopify.currency = {{"active":"USD"}};')
    def test_same_store_declared_canonical(self):
        source=self.response('https://alias.test/','same.myshopify.com')
        target=self.response('https://canonical.test/','same.myshopify.com')
        self.assertIs(canonical_storefront(SimpleNamespace(get=lambda url:target),source),target)
    def test_different_store_rejected(self):
        source=self.response('https://alias.test/','one.myshopify.com')
        target=self.response('https://canonical.test/','two.myshopify.com')
        with self.assertRaises(CrawlError):canonical_storefront(SimpleNamespace(get=lambda url:target),source)
