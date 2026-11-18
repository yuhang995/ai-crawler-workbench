import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.common import AccessChallenge, CrawlError, save_json, read_json
from core.pipeline import default_sample_candidates, retryable_sample_verification_error, run_job


BASE = 'https://leaf.example'
HOME = '<script>Shopify.country="US";Shopify.currency={"active":"USD"};</script><header></header>'


class Response:
    def __init__(self, url, text=HOME):
        self.url, self.text = url, text


class Client:
    def get(self, url):
        return Response(url)


def product(number):
    return {'id': number, 'handle': f'p{number}', 'title': f'P{number}',
            'variants': [{'id': number * 10, 'title': 'Default', 'price': '10.00'}],
            'images': [], 'options': []}


class LeafSelectionTests(unittest.TestCase):
    def test_configuration_only_sample_can_be_replaced(self):
        self.assertTrue(retryable_sample_verification_error(
            CrawlError('Hydrogen 配置型套装缺少独立可验证变体: /products/build-your-set')))
        self.assertFalse(retryable_sample_verification_error(
            CrawlError('Hydrogen 子款 SKU、价格或库存证据不完整')))

    def test_deepest_true_leaves_are_stable_and_urls_are_not_retried(self):
        categories = {
            'A': BASE + '/collections/a',
            'A,A One': BASE + '/collections/one',
            'A,A One,A One First': BASE + '/collections/first',
            'B': BASE + '/collections/b',
            'B,B Second': BASE + '/collections/second',
            'C,C Alias,C Alias First': BASE + '/collections/first',
            'Flat': BASE + '/collections/flat',
        }
        self.assertEqual(default_sample_candidates(categories), [
            ('A,A One,A One First', BASE + '/collections/first'),
            ('B,B Second', BASE + '/collections/second'),
            ('Flat', BASE + '/collections/flat')])

    def test_display_comma_does_not_inflate_leaf_depth(self):
        categories = {
            'Shop': BASE + '/collections/all',
            'Shop,Shop Plain': BASE + '/collections/plain',
            'Shop,Shop Plain,Shop Plain First': BASE + '/collections/plain-first',
            'Shop,Shop Collagen, Whey & Supplements': BASE + '/collections/powders',
            ('Shop,Shop Collagen, Whey & Supplements,'
             'Shop Collagen, Whey & Supplements Protein'): BASE + '/collections/protein',
        }
        self.assertEqual(default_sample_candidates(categories), [
            ('Shop,Shop Plain,Shop Plain First', BASE + '/collections/plain-first'),
            (('Shop,Shop Collagen, Whey & Supplements,'
              'Shop Collagen, Whey & Supplements Protein'), BASE + '/collections/protein'),
        ])

    def test_default_sampling_retries_at_most_three_and_records_chosen_leaf(self):
        categories = {
            'Shop': BASE + '/collections/all',
            'Shop,Shop Devices': BASE + '/collections/devices',
            'Shop,Shop Devices,Shop Devices Empty': BASE + '/collections/empty',
            'Shop,Shop Devices,Shop Devices Small': BASE + '/collections/small',
            'Shop,Shop Devices,Shop Devices Ready': BASE + '/collections/ready',
            'Flat': BASE + '/collections/flat',
        }
        def navigation(root, base, html):
            from core.simple_nav import publish_simple
            publish_simple(root, categories)
            return categories, {'method': 'simple_nav', 'roots': []}, [html]
        calls = []
        def listing(client, url):
            calls.append(url)
            count = {'empty': 0, 'small': 2, 'ready': 4}[url.rsplit('/', 1)[-1]]
            products = [product(i) for i in range(1, count + 1)]
            return {'url': url, 'urls': [BASE + '/products/' + p['handle'] for p in products],
                    'products': products, 'pages': [], 'feeds': [{'products': products}],
                    'card_sources': [], 'warnings': ([{'code':'listing_order_feed_fallback',
                        'message':'feed order','url':url}] if url.endswith('/ready') else []), 'complete': True,
                    'first3_seconds': 0, 'first3_cpu_seconds': 0}
        def fetched(client, url, fallback=None):
            p = fallback or product(int(url.rsplit('p', 1)[-1]))
            return {'url': url, 'product': p}
        detection = {'mode': 'standard_shopify', 'shopify': True, 'provider': '',
                     'graphql_candidates': [], 'market': {'countries': ['US'], 'currencies': ['USD'], 'confirmed': True},
                     'probes': []}
        with tempfile.TemporaryDirectory() as d, \
             patch('core.pipeline.Client', Client), \
             patch('core.detector.canonical_storefront', side_effect=lambda client, response: response), \
             patch('core.simple_nav.run_simple', side_effect=navigation), \
             patch('core.pipeline.detect', return_value=detection), \
             patch('core.pipeline.collect_listing', side_effect=listing), \
             patch('core.pipeline.fetch_product', side_effect=fetched), \
             patch('core.pipeline.export'), patch('core.pipeline.prepare_runtime'), \
             patch('core.pipeline.validate', return_value={'passed': True}), \
             patch('core.pipeline.package'):
            report = run_job(BASE, d)
            scope = read_json(Path(d) / 'sample_scope.json')
        self.assertEqual(calls, [BASE + '/collections/empty', BASE + '/collections/small', BASE + '/collections/ready'])
        chosen = 'Shop,Shop Devices,Shop Devices Ready'
        self.assertEqual(scope['selected_categories'], {chosen: BASE + '/collections/ready'})
        self.assertEqual([a['distinct_products'] for a in scope['selection_attempts']], [0, 2, 4])
        self.assertEqual(report['selected_sample_categories'], [chosen])
        self.assertEqual(len(report['sample_category_attempts']), 3)
        self.assertEqual(report['warnings'][-1]['code'], 'listing_order_feed_fallback')

    def test_implicit_sampling_continues_past_three_and_skips_listing_errors(self):
        categories = {
            'Shop,Shop One': BASE + '/collections/one',
            'Shop,Shop Two': BASE + '/collections/two',
            'Shop,Shop Three': BASE + '/collections/three',
            'Shop,Shop Four': BASE + '/collections/four',
        }
        def navigation(root, base, html):
            from core.simple_nav import publish_simple
            publish_simple(root, categories)
            return categories, {'method': 'simple_nav', 'roots': []}, [html]
        calls = []
        def listing(client, url):
            slug = url.rsplit('/', 1)[-1]; calls.append(slug)
            if slug == 'one':
                raise CrawlError('HTTP 404 collection feed')
            count = {'two': 1, 'three': 2, 'four': 3}[slug]
            products = [product(i) for i in range(1, count + 1)]
            return {'url': url, 'urls': [BASE + '/products/' + p['handle'] for p in products],
                    'products': products, 'pages': [], 'feeds': [{'products': products}],
                    'card_sources': [], 'warnings': [], 'complete': True,
                    'first3_seconds': 0, 'first3_cpu_seconds': 0}
        detection = {'mode': 'standard_shopify', 'shopify': True, 'provider': '',
                     'graphql_candidates': [],
                     'market': {'countries': ['US'], 'currencies': ['USD'], 'confirmed': True},
                     'probes': []}
        with tempfile.TemporaryDirectory() as d, \
             patch('core.pipeline.Client', Client), \
             patch('core.detector.canonical_storefront', side_effect=lambda client, response: response), \
             patch('core.simple_nav.run_simple', side_effect=navigation), \
             patch('core.pipeline.detect', return_value=detection), \
             patch('core.pipeline.collect_listing', side_effect=listing), \
             patch('core.pipeline.fetch_product', side_effect=lambda c,u,p=None: {'url':u,'product':p}), \
             patch('core.pipeline.export'), patch('core.pipeline.prepare_runtime'), \
             patch('core.pipeline.validate', return_value={'passed': True}), \
             patch('core.pipeline.package'):
            report = run_job(BASE, d)
        self.assertEqual(calls, ['one','two','three','four'])
        self.assertEqual(report['selected_sample_categories'], ['Shop,Shop Four'])
        self.assertEqual([a['status'] for a in report['sample_category_attempts']],
                         ['unverifiable_listing','insufficient','insufficient','selected'])

    def test_access_challenge_is_not_swallowed_by_leaf_retry(self):
        categories = {
            'Shop,Shop First': BASE + '/collections/first',
            'Shop,Shop Second': BASE + '/collections/second',
        }
        def navigation(root, base, html):
            from core.simple_nav import publish_simple
            publish_simple(root, categories)
            return categories, {'method': 'simple_nav', 'roots': []}, [html]
        calls = []
        def listing(client, url):
            calls.append(url)
            raise AccessChallenge('站点持续返回人机验证挑战', url)
        detection = {'mode': 'standard_shopify', 'shopify': True, 'provider': '',
                     'graphql_candidates': [],
                     'market': {'countries': ['US'], 'currencies': ['USD'], 'confirmed': True},
                     'probes': []}
        with tempfile.TemporaryDirectory() as d, \
             patch('core.pipeline.Client', Client), \
             patch('core.detector.canonical_storefront', side_effect=lambda client, response: response), \
             patch('core.simple_nav.run_simple', side_effect=navigation), \
             patch('core.pipeline.detect', return_value=detection), \
             patch('core.pipeline.collect_listing', side_effect=listing), \
             patch('core.pipeline.prepare_runtime'):
            with self.assertRaises(AccessChallenge):
                run_job(BASE, d)
            failure = read_json(Path(d) / 'failure.json')
            scope = read_json(Path(d) / 'sample_scope.json')
        self.assertEqual(calls, [BASE + '/collections/first'])
        self.assertEqual(failure['status'], 'needs_access_confirm')
        self.assertEqual(scope['selection_status'], 'needs_access_confirm')
        self.assertEqual(scope['selection_attempts'], [])

    def test_explicit_category_is_not_silently_replaced(self):
        categories = {'A,A Deep': BASE + '/collections/a', 'B,B Deep': BASE + '/collections/b'}
        self.assertEqual(list({k: u for k, u in categories.items() if k == 'A,A Deep'}.items()),
                         [('A,A Deep', BASE + '/collections/a')])

    def test_default_sampling_retries_next_leaf_when_samples_cannot_be_verified(self):
        categories = {
            'Shop,Shop First': BASE + '/collections/blocked',
            'Shop,Shop Second': BASE + '/collections/ready',
            'Shop,Shop Third': BASE + '/collections/spare',
        }
        def navigation(root, base, html):
            from core.simple_nav import publish_simple
            publish_simple(root, categories)
            return categories, {'method': 'simple_nav', 'roots': []}, [html]
        listing_calls = []
        def listing(client, url):
            listing_calls.append(url)
            slug = url.rsplit('/', 1)[-1]
            products = [
                {'id': i, 'handle': f'{slug}-{i}', 'title': f'{slug} {i}',
                 'variants': [{'id': i * 10, 'title': 'Default', 'price': '10.00'}],
                 'images': [], 'options': []}
                for i in range(1, 4)
            ]
            return {'url': url,
                    'urls': [BASE + '/products/' + item['handle'] for item in products],
                    'products': products, 'pages': [], 'feeds': [{'products': products}],
                    'card_sources': [], 'warnings': [], 'complete': True,
                    'first3_seconds': 0, 'first3_cpu_seconds': 0}
        def fetched(client, url, fallback=None):
            if '/products/blocked-' in url:
                raise CrawlError('达到接口 250 变体上限，需非标完整性核验')
            return {'url': url, 'product': fallback}
        detection = {'mode': 'standard_shopify', 'shopify': True, 'provider': '',
                     'graphql_candidates': [],
                     'market': {'countries': ['US'], 'currencies': ['USD'], 'confirmed': True},
                     'probes': []}
        with tempfile.TemporaryDirectory() as d, \
             patch('core.pipeline.Client', Client), \
             patch('core.detector.canonical_storefront', side_effect=lambda client, response: response), \
             patch('core.simple_nav.run_simple', side_effect=navigation), \
             patch('core.pipeline.detect', return_value=detection), \
             patch('core.pipeline.collect_listing', side_effect=listing), \
             patch('core.pipeline.fetch_product', side_effect=fetched), \
             patch('core.pipeline.export'), patch('core.pipeline.prepare_runtime'), \
             patch('core.pipeline.validate', return_value={'passed': True}), \
             patch('core.pipeline.package'):
            report = run_job(BASE, d)
            scope = read_json(Path(d) / 'sample_scope.json')
        self.assertEqual(listing_calls, [BASE + '/collections/blocked', BASE + '/collections/ready'])
        self.assertEqual(scope['selected_categories'],
                         {'Shop,Shop Second': BASE + '/collections/ready'})
        self.assertEqual([attempt['status'] for attempt in scope['selection_attempts']],
                         ['unverifiable_samples', 'selected'])
        self.assertIn('250', scope['selection_attempts'][0]['error'])
        self.assertEqual(report['selected_sample_categories'], ['Shop,Shop Second'])
        self.assertEqual([attempt['status'] for attempt in report['sample_category_attempts']],
                         ['unverifiable_samples', 'selected'])

    def test_explicit_category_sample_verification_failure_does_not_retry_other_leaf(self):
        categories = {
            'A,A Deep': BASE + '/collections/a',
            'B,B Deep': BASE + '/collections/b',
        }
        def navigation(root, base, html):
            from core.simple_nav import publish_simple
            publish_simple(root, categories)
            return categories, {'method': 'simple_nav', 'roots': []}, [html]
        listing_calls = []
        def listing(client, url):
            listing_calls.append(url)
            products = [product(i) for i in range(1, 4)]
            return {'url': url, 'urls': [BASE + '/products/' + item['handle'] for item in products],
                    'products': products, 'pages': [], 'feeds': [{'products': products}],
                    'card_sources': [], 'warnings': [], 'complete': True,
                    'first3_seconds': 0, 'first3_cpu_seconds': 0}
        detection = {'mode': 'standard_shopify', 'shopify': True, 'provider': '',
                     'graphql_candidates': [],
                     'market': {'countries': ['US'], 'currencies': ['USD'], 'confirmed': True},
                     'probes': []}
        with tempfile.TemporaryDirectory() as d, \
             patch('core.pipeline.Client', Client), \
             patch('core.detector.canonical_storefront', side_effect=lambda client, response: response), \
             patch('core.simple_nav.run_simple', side_effect=navigation), \
             patch('core.pipeline.detect', return_value=detection), \
             patch('core.pipeline.collect_listing', side_effect=listing), \
             patch('core.pipeline.fetch_product', side_effect=CrawlError(
                 '达到接口 250 变体上限，需非标完整性核验')), \
             patch('core.pipeline.export'), patch('core.pipeline.prepare_runtime'), \
             patch('core.pipeline.validate', return_value={'passed': True}), \
             patch('core.pipeline.package'):
            with self.assertRaisesRegex(CrawlError, '250'):
                run_job(BASE, d, category='A,A Deep')
        self.assertEqual(listing_calls, [BASE + '/collections/a'])

    def test_default_sampling_does_not_hide_non_retryable_product_failure(self):
        categories = {
            'A,A Deep': BASE + '/collections/a',
            'B,B Deep': BASE + '/collections/b',
        }
        def navigation(root, base, html):
            from core.simple_nav import publish_simple
            publish_simple(root, categories)
            return categories, {'method': 'simple_nav', 'roots': []}, [html]
        listing_calls = []
        def listing(client, url):
            listing_calls.append(url)
            products = [product(i) for i in range(1, 4)]
            return {'url': url,
                    'urls': [BASE + '/products/' + item['handle'] for item in products],
                    'products': products, 'pages': [], 'feeds': [{'products': products}],
                    'card_sources': [], 'warnings': [], 'complete': True,
                    'first3_seconds': 0, 'first3_cpu_seconds': 0}
        detection = {'mode': 'standard_shopify', 'shopify': True, 'provider': '',
                     'graphql_candidates': [],
                     'market': {'countries': ['US'], 'currencies': ['USD'], 'confirmed': True},
                     'probes': []}
        with tempfile.TemporaryDirectory() as d, \
             patch('core.pipeline.Client', Client), \
             patch('core.detector.canonical_storefront', side_effect=lambda client, response: response), \
             patch('core.simple_nav.run_simple', side_effect=navigation), \
             patch('core.pipeline.detect', return_value=detection), \
             patch('core.pipeline.collect_listing', side_effect=listing), \
             patch('core.pipeline.fetch_product', side_effect=CrawlError('商品身份与源响应不符')), \
             patch('core.pipeline.export'), patch('core.pipeline.prepare_runtime'), \
             patch('core.pipeline.validate', return_value={'passed': True}), \
             patch('core.pipeline.package'):
            with self.assertRaisesRegex(CrawlError, '商品身份'):
                run_job(BASE, d)
        self.assertEqual(listing_calls, [BASE + '/collections/a'])


if __name__ == '__main__':
    unittest.main()
