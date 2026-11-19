import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.common import CrawlError, save_json
from core.jsonld_storefront import (collect_listing, fetch_product,
                                    normalize_product, product_market,
                                    supports_home, validate_listing,
                                    verify_source)


BASE = 'https://store.example/'


def product_source(sku='SKU-1'):
    return {'@context': 'https://schema.org/', '@type': 'Product',
            'name': 'Observed Frame', 'sku': sku, 'url': BASE + 'goods-detail/' + sku,
            'brand': {'@type': 'Brand', 'name': 'Example'}, 'color': 'Black',
            'description': 'Observed description',
            'image': [BASE + 'media/one.jpg', BASE + 'media/two.jpg'],
            'offers': {'@type': 'Offer', 'priceCurrency': 'USD', 'price': '12.00',
                       'availability': 'https://schema.org/InStock'}}


class Client:
    def __init__(self):
        self.pages = {
            BASE + 'goods-list/70': '<main><a href="/goods-detail/A">A</a><a href="/goods-detail/B">B</a></main>',
            BASE + 'goods-list/70?page=2': '<main><a href="/goods-detail/C">C</a></main>',
            BASE + 'goods-list/70?page=3': '<main></main>',
        }

    def get(self, url):
        if url in self.pages:
            return SimpleNamespace(url=url, text=self.pages[url])
        sku = url.rstrip('/').rsplit('/', 1)[-1]
        payload = json.dumps(product_source(sku))
        return SimpleNamespace(url=url, text='<script type="application/ld+json">' + payload + '</script>')


class JSONLDStorefrontTests(unittest.TestCase):
    def test_detects_repeated_goods_list_menu_and_paginates_to_empty_page(self):
        home = '<div id="header">' + ''.join(
            f'<a href="/goods-list/{number}">Category</a>' for number in range(3)) + '</div>'
        self.assertTrue(supports_home(home))
        listing = collect_listing(Client(), BASE + 'goods-list/70')
        self.assertEqual(listing['urls'], [BASE + 'goods-detail/A', BASE + 'goods-detail/B',
                                           BASE + 'goods-detail/C'])
        self.assertTrue(listing['complete'])
        self.assertEqual(len(listing['pages']), 2)
        validate_listing(listing, listing['urls'])
        with self.assertRaisesRegex(CrawlError, '商品网格'):
            validate_listing(listing, list(reversed(listing['urls'])))

    def test_product_jsonld_replays_identity_price_images_and_market(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'evidence').mkdir()
            (root / 'evidence/home.html').write_text(
                '<link rel="alternate" hreflang="en-US" href="https://store.example/">',
                encoding='utf-8')
            save_json(root / 'config.json', {'base_url': BASE})
            sample = fetch_product(Client(), BASE + 'goods-detail/SKU-1', root / 'evidence')
            self.assertEqual(sample['product']['variants'][0]['price'], '12.00')
            self.assertEqual(len(sample['product']['images']), 2)
            verify_source(root, sample)
            self.assertEqual(product_market(root, sample), ('US', 'USD'))

    def test_wrong_identity_and_non_usd_fail_closed(self):
        raw = product_source()
        raw['offers']['priceCurrency'] = 'CAD'
        with self.assertRaisesRegex(CrawlError, 'USD'):
            normalize_product(raw, BASE + 'goods-detail/SKU-1')


if __name__ == '__main__':
    unittest.main()
