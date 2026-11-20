import csv
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.ai_repair import _remove_tree_iterative
from core.common import Client, AdapterRequired
from core.pipeline import compact_shopify_listing_evidence, collect_full_shopify_listings
from core.shopify_fastpath import (image_url, normalize_product_images, listing_dom,
                                   run_products, source_product_without_images,
                                   ListingCollectionTimeout)
from core.common import read_json, save_json
from core.validator import _page_html


class _Response:
    def __init__(self, url, size):
        self.url = url
        self.content = b'x' * size
        self.text = self.content.decode()
        self.status_code = 200
        self.headers = {'Content-Type': 'text/plain'}

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self, sizes):
        self.sizes = iter(sizes)

    def get(self, url, **kwargs):
        return _Response(url, next(self.sizes))


class BatchStabilityTests(unittest.TestCase):
    def test_validator_accepts_large_evidence_backed_csv_field(self):
        payload = 'x' * 200_000
        rows = list(csv.DictReader(io.StringIO('Description\n"' + payload + '"\n')))
        self.assertEqual(rows[0]['Description'], payload)

    def test_full_collection_sweep_is_bounded_parallel_and_keeps_source_order(self):
        categories = {f'C{i}': f'https://shop.example/collections/c{i}' for i in range(8)}
        lock = threading.Lock()
        active = 0
        peak = 0
        progress = []

        def collect(client, url, prefer_feed_order=False, max_seconds=None):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.02)
                return {'url': url, 'urls': [url + '/products/a'], 'products': [],
                        'feeds': [], 'pages': [], 'complete': True}
            finally:
                with lock:
                    active -= 1

        with patch('core.pipeline.collect_listing', side_effect=collect):
            urls, values, warnings = collect_full_shopify_listings(
                object(), categories,
                progress=lambda done, total, url, status: progress.append(
                    (done, total, status)))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual(urls, list(categories.values()))
        self.assertEqual([item['url'] for item in values], list(categories.values()))
        self.assertEqual(progress[-1], (8, 8, 'success'))
        self.assertEqual(warnings, [])

    def test_client_cache_is_lru_bounded_and_skips_large_response(self):
        client = Client()
        client.CACHE_MAX_ENTRIES = 2
        client.CACHE_MAX_BYTES = 12
        client.CACHE_MAX_ITEM_BYTES = 8
        session = _Session([6, 6, 6, 20])
        with patch('core.common.public_url', side_effect=lambda value: value), \
                patch.object(client, '_session', return_value=session), \
                patch.object(client, 'pace'), \
                patch('core.common.is_access_challenge_response', return_value=False):
            for suffix in ('a', 'b', 'c', 'large'):
                client.get('https://example.test/' + suffix)
        self.assertLessEqual(len(client.cache), 2)
        self.assertLessEqual(client.cache_bytes, 12)
        self.assertNotIn('https://example.test/a', client.cache)
        self.assertNotIn('https://example.test/large', client.cache)
        client.clear_cache()
        self.assertEqual((len(client.cache), client.cache_bytes), (0, 0))

    def test_listing_html_is_persisted_instead_of_retained(self):
        listing = {'order_evidence': 'html_dom', 'membership_sources': [],
                   'pages': [{'url': 'https://example.test/collections/a',
                              'html': '<main>' + ('x' * 1000) + '</main>'}],
                   'products': [], 'feeds': []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / 'evidence' / 'listing-pages'
            compact_shopify_listing_evidence(listing, evidence)
            self.assertEqual(listing['pages'][0]['html'], '')
            self.assertEqual(_page_html(listing['pages'][0], root),
                             '<main>' + ('x' * 1000) + '</main>')

    def test_deep_ai_workspace_cleanup_is_iterative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'work'
            current = root
            for _ in range(70):
                current /= 'd'
            current.mkdir(parents=True)
            (current / 'candidate.txt').write_text('x', encoding='utf-8')
            old_limit = sys.getrecursionlimit()
            try:
                sys.setrecursionlimit(50)
                _remove_tree_iterative(root)
            finally:
                sys.setrecursionlimit(old_limit)
            self.assertFalse(root.exists())

    def test_product_images_are_normalized_and_bad_member_is_skipped(self):
        base = 'https://shop.example/products/item'
        product = {
            'images': [
                {'id': 1, 'src': '//cdn.example/a b.jpg'},
                {'id': 2, 'src': '/media/c d.jpg'},
                {'id': 3, 'src': 'javascript:bad'},
            ],
            'variants': [{'id': 10, 'featured_image': {'src': '//cdn.example/a b.jpg'}}],
        }
        discarded = normalize_product_images(product, base)
        self.assertEqual(discarded, ['javascript:bad'])
        self.assertEqual(product['images'][0]['src'], 'https://cdn.example/a%20b.jpg')
        self.assertEqual(product['images'][1]['src'], 'https://shop.example/media/c%20d.jpg')
        self.assertEqual(product['variants'][0]['featured_image']['src'],
                         'https://cdn.example/a%20b.jpg')
        self.assertEqual(image_url('data:image/png;base64,x', base), '')

    def test_listing_dom_uses_nonsemantic_body_after_removing_navigation(self):
        html = '''<html><body><header><a href="/products/nav">Nav</a></header>
        <div class="catalog"><a href="/products/real-one">One</a>
        <a href="/product/real-two">Two</a></div>
        <aside><a href="/products/recommended">Recommended</a></aside></body></html>'''
        handles, next_url, dynamic = listing_dom(
            html, 'https://shop.example/collections/all')
        self.assertEqual(handles, ['real-one', 'real-two'])
        self.assertIsNone(next_url)
        self.assertFalse(dynamic)

    def test_standard_shopify_full_run_skips_noncollection_content_landing(self):
        categories = {
            'Campaign': 'https://shop.example/pages/campaign',
            'Shop': 'https://shop.example/collections/all',
        }
        collection = {'url': categories['Shop'], 'urls': ['https://shop.example/products/a'],
                      'products': [], 'feeds': [], 'pages': [],
                      'membership_sources': [], 'order_evidence': 'html_dom'}
        def collect(client, url, prefer_feed_order=False):
            if '/pages/' in url:
                raise AdapterRequired('Shopify Hydrogen 顶级分类路由缺少可验证商品数据')
            return collection
        with patch('core.pipeline.collect_listing', side_effect=collect):
            urls, values, warnings = collect_full_shopify_listings(
                object(), categories, skip_non_collection_landings=True)
        self.assertEqual(urls, [categories['Shop']])
        self.assertEqual(values, [collection])
        self.assertEqual(warnings[0]['code'], 'content_landing_skipped')

    def test_standard_shopify_full_run_defers_and_skips_empty_collection(self):
        categories = {
            'Empty': 'https://shop.example/collections/empty',
            'Shop': 'https://shop.example/collections/all',
        }
        collection = {'url': categories['Shop'], 'urls': ['https://shop.example/products/a'],
                      'products': [], 'feeds': [], 'pages': [],
                      'membership_sources': [], 'order_evidence': 'html_dom'}
        def collect(client, url, prefer_feed_order=False):
            if url.endswith('/empty'):
                raise AdapterRequired('落地页既无完整子集合来源，也无完整、有限的显式商品卡片')
            return collection
        with patch('core.pipeline.collect_listing', side_effect=collect):
            urls, values, warnings = collect_full_shopify_listings(
                object(), categories, skip_non_collection_landings=True)
        self.assertEqual(urls, [categories['Shop']])
        self.assertEqual(values, [collection])
        self.assertEqual(warnings[0]['code'], 'empty_or_landing_collection_skipped')

    def test_full_run_skips_one_timed_out_collection_and_keeps_others(self):
        categories = {
            'Huge': 'https://shop.example/collections/huge',
            'Shop': 'https://shop.example/collections/all',
        }
        collection = {'url': categories['Shop'], 'urls': ['https://shop.example/products/a'],
                      'products': [], 'feeds': [], 'pages': [],
                      'membership_sources': [], 'order_evidence': 'html_dom'}

        def collect(client, url, prefer_feed_order=False, max_seconds=None):
            self.assertEqual(max_seconds, 180)
            if url.endswith('/huge'):
                raise ListingCollectionTimeout('single collection timeout')
            return collection

        with patch('core.pipeline.collect_listing', side_effect=collect):
            urls, values, warnings = collect_full_shopify_listings(object(), categories)
        self.assertEqual(urls, [categories['Shop']])
        self.assertEqual(values, [collection])
        self.assertEqual(warnings[0]['code'], 'listing_collection_timeout_skipped')

    def test_full_products_skips_official_empty_gallery_with_audit_warning(self):
        class FakeClient:
            def clear_cache(self): pass
        def sample(url, image=True):
            slug = url.rsplit('/', 1)[-1]
            number = int(slug[1:])
            return {'url': url, 'source': 'json', 'endpoint': url + '.json',
                    'warnings': [], 'product': {
                        'id': number, 'handle': slug, 'title': slug,
                        'images': ([{'src': f'https://cdn.example/{slug}.jpg'}] if image else []),
                        'variants': [{'id': number * 10, 'price': '1.00'}]}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            urls = [f'https://shop.example/products/p{i}' for i in range(1, 7)]
            save_json(root / 'detail_url.json', {'Shop': urls})
            save_json(root / 'detail_release.json', {'navigation_version': 'v1', 'sha256': 'digest'})
            save_json(root / 'sample_scope.json', {'detail_scope': 'pending'})
            save_json(root / 'warnings.json', [])
            with patch('core.common.Client', FakeClient), \
                    patch('core.navigation_release.require_release', return_value={'navigation_version': 'v1'}), \
                    patch('core.navigation_release.digest', return_value='digest'), \
                    patch('core.shopify_fastpath.fetch_product',
                          side_effect=lambda client, url: sample(url, not url.endswith('/p1'))), \
                    patch('core.shopify_fastpath.export'), \
                    patch('core.validator.validate', return_value={'passed': True, 'products': 5}):
                result = run_products(root, all_products=True)
            self.assertTrue(result['passed'])
            self.assertEqual(len(read_json(root / 'samples.json')), 5)
            failures = read_json(root / 'failed_products.json')
            self.assertEqual(failures[0]['code'], 'source_product_without_images')
            warning = next(item for item in read_json(root / 'warnings.json')
                           if item['code'] == 'products_without_images_skipped')
            self.assertEqual(warning['urls'], [urls[0]])
            self.assertTrue(source_product_without_images(sample(urls[0], False)))
            poisoned = sample(urls[0], False)
            poisoned['warnings'] = [{'code': 'invalid_product_images_skipped'}]
            self.assertFalse(source_product_without_images(poisoned))


if __name__ == '__main__':
    unittest.main()
