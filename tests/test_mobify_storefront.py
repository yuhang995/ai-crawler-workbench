import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.custom_site import identify_platform
from core.mobify_storefront import (collect_listing, fetch_product, navigation,
                                    validate_listing, verify_source)


BASE = 'https://example.com/us/en-us/'


def page(data):
    return '<html><script id="mobify-data" type="application/json">' + json.dumps(data) + '</script></html>'


def state(queries):
    return {'__CONFIG__': {'app': {'algolia': {'appId': 'APP', 'apiKey': 'KEY'}}},
            '__PRELOADED_STATE__': {'__reactQuery': {'queries': [
                {'state': {'data': value}, 'queryKey': ['fixture', index]}
                for index, value in enumerate(queries)]}}}


def hit(number):
    return {'objectID': 'o' + str(number), 'productId': 'SKU' + str(number),
            'urlSlug': 'product-' + str(number)}


class Reply:
    def __init__(self, data=None, text='', url=BASE):
        self._data, self.text, self.url = data, text, url
        self.status_code = 200

    def json(self):
        return self._data


class MobifyTests(unittest.TestCase):
    def test_new_pwa_header_category_links_replace_legacy_main_nav(self):
        raw = state([])
        raw['__PRELOADED_STATE__']['__reactQuery']['queries'] = [
            {'queryKey': ['nav-categories', 'en-US'],
             'state': {'data': {'categories': []}}}]
        html = ('<html><header><nav>'
                '<a href="/en-us/category/products/">Products</a>'
                '<a href="/en-us/category/products/vacuums/">Vacuums</a>'
                '<a href="/en-us/category/products/vacuums/upright/">Upright Vacuums</a>'
                '</nav></header><script id="mobify-data" type="application/json">'
                + json.dumps(raw) + '</script></html>')
        self.assertEqual(navigation(html, 'https://example.com/'), {
            'Products': 'https://example.com/en-us/category/products/',
            'Products,Products Vacuums': 'https://example.com/en-us/category/products/vacuums/',
            ('Products,Products Vacuums,Products Vacuums Upright Vacuums'):
                'https://example.com/en-us/category/products/vacuums/upright/',
        })

    def test_navigation_uses_structured_menu_and_drops_explore_and_product_cards(self):
        root = {'title': 'Fire Pits', 'url_custom': {'internal': '/plp/fire-pits'},
                'secondary_links': [{'display_title': 'Explore', 'link': [
                    {'title': 'Holiday Deals', 'url_custom': {'internal': '/plp/deals'}}]}],
                'link': [
                    {'title': 'Backyard', 'url_custom': {'internal': '/plp/fire-pits/backyard'}},
                    {'title': 'Patio Heater', 'url_custom': {'internal': '/p/heater?sku=H1'}},
                    {'title': 'Shop All', 'url_custom': {'internal': '/plp/fire-pits'}}]}
        html = page(state([{'main_nav': [{'link': [root]}]}]))
        self.assertEqual(identify_platform(html), 'mobify')
        self.assertEqual(navigation(html, BASE), {
            'Fire Pits': BASE + 'plp/fire-pits',
            'Fire Pits,Fire Pits Backyard': BASE + 'plp/fire-pits/backyard'})

    def test_listing_replays_all_declared_algolia_pages_and_order(self):
        first = {'hits': [hit(1)], 'nbHits': 2, 'page': 0, 'nbPages': 2,
                 'hitsPerPage': 1, 'index': 'idx', 'params': 'hitsPerPage=1&page=0'}
        second = dict(first, hits=[hit(2)], page=1)
        listing_html = page(state([first]))

        class Client:
            timeout = 20
            def __init__(self):
                self.local = SimpleNamespace(session=SimpleNamespace(
                    post=lambda *args, **kwargs: Reply(second)))
            def pace(self): pass
            def get(self, url, timeout=None): return Reply(text=listing_html, url=url)

        result = collect_listing(Client(), BASE + 'plp/fire-pits')
        self.assertEqual(result['urls'], [
            BASE + 'p/product-1?sku=SKU1', BASE + 'p/product-2?sku=SKU2'])
        validate_listing(result, result['urls'])

    def test_product_source_replays_identity_price_stock_and_gallery(self):
        url = BASE + 'p/product-1?sku=SKU1'
        raw = {'product': {'id': 'SKU1', 'name': 'Product One', 'currency': 'USD',
                           'price': 49.5, 'brand': 'Brand', 'shortDescription': '<p>Body</p>',
                           'c_urlSlug': 'product-1', 'c_productType': 'Fire Pit',
                           'inventory': {'orderable': True, 'stockLevel': 7},
                           'imageGroups': [{'images': [{'link': 'http://cdn.example/a.jpg'}]}]},
               'category': {}, 'CTS': {'images': [{'product_images': [
                   {'image': [{'secure_url': 'https://cdn.example/b.jpg'}]}]}]}}
        html = page(state([raw]))
        client = SimpleNamespace(get=lambda requested, timeout=None: Reply(text=html, url=requested))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'evidence').mkdir()
            sample = fetch_product(client, url, root / 'evidence')
            self.assertEqual(sample['product']['variants'][0]['inventory_quantity'], 7)
            self.assertEqual([image['src'] for image in sample['product']['images']], [
                'https://cdn.example/a.jpg', 'https://cdn.example/b.jpg'])
            verify_source(root, sample)


if __name__ == '__main__':
    unittest.main()
