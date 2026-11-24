import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from core.custom_site import identify_platform
from core.custom_site import run_navigation
from core.woocommerce_storefront import (
    collect_listing, fetch_product, navigation, product_market, verify_source,
)

BASE = 'https://shop.example/'


def item(number, slug):
    return {
        'id': number, 'name': 'Item ' + str(number), 'slug': slug,
        'permalink': BASE + 'product/' + slug + '/', 'sku': 'SKU-' + str(number),
        'description': '<p>Body</p>', 'is_in_stock': True,
        'prices': {'price': '1500', 'regular_price': '5000',
                   'currency_code': 'USD', 'currency_minor_unit': 2},
        'images': [{'src': BASE + 'media/' + slug + '.jpg'}],
    }


class Client:
    def __init__(self, rows): self.rows = rows
    def get(self, url, **kwargs):
        return SimpleNamespace(url=url, text='<body class="woocommerce term-gifts term-69"></body>')
    def json(self, url, **kwargs):
        if url.endswith('/cart'):
            return {'totals': {'currency_code': 'USD'},
                    'shipping_address': {'country': 'US'}}
        if 'slug=' in url:
            return [self.rows[0]]
        return [] if 'page=2' in url else self.rows


class WooCommerceStorefrontTests(unittest.TestCase):
    def test_detection_navigation_listing_product_and_market_replay(self):
        home = '''<html><body class="woocommerce"><header><ul><li>
        <a href="/product-category/gifts/">Gifts</a><ul><li>
        <a href="/product-category/gifts/baby/">Baby</a></li></ul></li></ul></header>
        <script src="/wp-content/plugins/woocommerce/a.js"></script></body></html>'''
        self.assertEqual(identify_platform(home), 'woocommerce')
        self.assertEqual(navigation(home, BASE), {
            'Gifts': BASE + 'product-category/gifts/',
            'Gifts,Gifts Baby': BASE + 'product-category/gifts/baby/',
        })
        rows = [item(1, 'one'), item(2, 'two'), item(3, 'three')]
        client = Client(rows)
        listing = collect_listing(client, BASE + 'product-category/gifts/')
        self.assertEqual(listing['urls'], [row['permalink'] for row in rows])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'evidence').mkdir()
            sample = fetch_product(client, rows[0]['permalink'], root / 'evidence')
            self.assertEqual(sample['product']['variants'][0]['price'], '15')
            self.assertEqual(sample['product']['variants'][0]['compare_at_price'], '50')
            verify_source(root, sample)
            self.assertEqual(product_market(root, sample), ('US', 'USD'))

    def test_empty_native_navigation_falls_back_to_simple_nav(self):
        html = '''<html><body class="woocommerce"><header><nav>
          <a href="/collections/shop">Shop</a></nav></header>
          <script src="/wp-content/plugins/woocommerce/a.js"></script></body></html>'''
        expected = {'Shop': BASE + 'collections/shop'}

        def fallback(root, base, source):
            from core.common import save_json
            save_json(Path(root) / 'navigation_release.json', {'passed': True})
            return expected, {'method': 'simple_nav'}, [source]

        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.run_simple', side_effect=fallback) as simple:
            categories, evidence, documents, release = run_navigation(
                directory, BASE, html, 'woocommerce')
        self.assertEqual(categories, expected)
        self.assertEqual(evidence['method'], 'simple_nav')
        self.assertTrue(release['passed'])
        simple.assert_called_once()

    def test_product_store_api_and_us_page_prove_market_when_cart_is_empty(self):
        row = item(1, 'one')
        class FallbackClient(Client):
            def get(self, url, **kwargs):
                return SimpleNamespace(url=url, text=(
                    '<html lang="en-US"><meta property="og:locale" content="en_US">'
                    '<script type="application/ld+json">'
                    '{"@type":"Product","offers":{"priceCurrency":"USD"}}'
                    '</script></html>'))
            def json(self, url, **kwargs):
                if url.endswith('/cart'):
                    return {}
                return [row]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'evidence').mkdir()
            sample = fetch_product(FallbackClient([row]), row['permalink'], root / 'evidence')
            self.assertEqual(sample['source_mapping']['market_mode'],
                             'product_store_api_and_page')
            self.assertEqual(product_market(root, sample), ('US', 'USD'))


if __name__ == '__main__':
    unittest.main()
