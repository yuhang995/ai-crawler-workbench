import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.common import CrawlError, read_json, save_json
from core.custom_site import (_distinct_samples, demandware_navigation,
                              identify_platform, run_custom_sample)
from core.validator import verify_source_mapping


class CustomSiteTests(unittest.TestCase):
    def test_full_detail_retries_transient_tls_without_hiding_data_errors(self):
        url = 'https://example.com/product.html'
        sample = {'url': url, 'product': {'id': 'p1'}}
        transient = CrawlError('请求失败 https://example.com/product.html: curl: (35) SSL_connect connection closed')
        product = Mock(side_effect=[transient, sample])
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.custom_site.time.sleep'), patch(
                'core.custom_site._preserve_product_source'):
            values = _distinct_samples(object(), Path(directory), product, [url], {}, None)
        self.assertEqual(values, [sample])
        self.assertEqual(product.call_count, 2)

        invalid = Mock(side_effect=CrawlError('商品价格与源响应不一致'))
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
                CrawlError, '价格'):
            _distinct_samples(object(), Path(directory), invalid, [url], {}, None)
        self.assertEqual(invalid.call_count, 1)

    def test_full_detail_retries_transient_dns_and_reuses_checkpoint(self):
        url = 'https://example.com/product.html'
        sample = {'url': url, 'product': {'id': 'p1'}}
        dns = CrawlError('request 请求失败: curl: (6) Could not resolve host')
        product = Mock(side_effect=[dns, sample])
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.custom_site.time.sleep'), patch(
                'core.custom_site._preserve_product_source'):
            root = Path(directory)
            values = _distinct_samples(object(), root, product, [url], {}, None)
            self.assertEqual(values, [sample])
            self.assertEqual(product.call_count, 2)
            cached_product = Mock(side_effect=AssertionError('checkpoint must avoid network'))
            values = _distinct_samples(object(), root, cached_product, [url], {}, None)
            self.assertEqual(values, [sample])
            cached_product.assert_not_called()

    def test_full_store_records_one_exhausted_network_product_and_continues(self):
        urls = [f'https://example.com/p{i}.html' for i in range(6)]
        failed_url = urls[-1]
        def collect(client, url):
            return {'url': url, 'urls': urls, 'catalog_urls': urls,
                    'complete': True, 'pages': []}
        def product(client, url, evidence):
            if url == failed_url:
                raise CrawlError('请求失败: curl: (6) Could not resolve host')
            number = urls.index(url) + 1
            return {'url': url, 'product': {'id': f'p{number}', 'warnings': []},
                    'source_mapping': {'kind': 'fixture'}}
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.custom_site._collectors', return_value=(collect, product)), patch(
                'core.custom_site._native_identity', return_value=True), patch(
                'core.custom_site._preserve_product_source'), patch(
                'core.custom_site._save_market_evidence'), patch(
                'core.custom_site.time.sleep'):
            root = Path(directory)
            _, _, _, samples, _ = run_custom_sample(
                object(), root, 'magento', [('Shop', 'https://example.com/shop')],
                all_categories=True, full_products=True)
            self.assertEqual(len(samples), 5)
            failures = read_json(root / 'failed_products.json')
            self.assertEqual(failures, [{'url': failed_url,
                'error': '请求失败: curl: (6) Could not resolve host',
                'exception_type': 'CrawlError'}])
            self.assertEqual(read_json(root / 'warnings.json')[0]['code'],
                             'transient_products_skipped')

    def test_full_custom_store_skips_empty_parent_but_keeps_leaf_categories(self):
        parent = 'https://example.com/accessories'
        child = parent + '/hats'
        product_url = 'https://example.com/hat.html'
        categories = [('Accessories', parent), ('Accessories,Accessories Hats', child)]
        def collect(client, url):
            if url == parent:
                raise CrawlError('Magento 分类页没有可验证的商品网格: ' + url)
            return {'url': url, 'urls': [product_url], 'catalog_urls': [product_url],
                    'complete': True, 'pages': [{'url': url, 'html': '<main></main>'}]}
        sample = {'url': product_url, 'product': {
            'id': 'hat-1', 'handle': 'hat', 'title': 'Hat', 'body_html': '',
            'vendor': '', 'product_type': '', 'tags': [], 'options': [], 'images': [],
            'variants': [{'id': 'hat-1', 'sku': 'hat-1', 'price': '10.00',
                          'compare_at_price': None, 'available': True, 'option1': None,
                          'option2': None, 'option3': None, 'featured_image': None}],
            'warnings': []}, 'source_mapping': {'kind': 'fixture'}}
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.custom_site._collectors', return_value=(collect, lambda *args: sample)), patch(
                'core.custom_site._native_identity', return_value=True), patch(
                'core.custom_site._preserve_product_source'), patch(
                'core.custom_site._save_market_evidence'):
            root = Path(directory)
            selected, detail, listings, samples, attempts = run_custom_sample(
                object(), root, 'magento', categories, all_categories=True, full_products=True)
            self.assertEqual(selected, {'Accessories,Accessories Hats': child})
            self.assertEqual(detail, {'Accessories,Accessories Hats': [product_url]})
            self.assertEqual(len(samples), 1)
            self.assertEqual(attempts[0]['status'], 'content_landing_skipped')
            self.assertEqual(attempts[1]['status'], 'selected')
    def test_native_platform_does_not_probe_incidental_shopify_links(self):
        from core.detector import detect

        markers = {
            'demandware': '/on/demandware.store/',
            'magento': 'text/x-magento-init',
            'bigcommerce': 'stencil cdn11.bigcommerce.com',
        }
        for platform, marker in markers.items():
            with self.subTest(platform=platform):
                client = Mock()
                found = detect(client, 'https://example.com/', marker + ' cdn.shopify.com',
                               {'Seasonal': 'https://example.com/collections/seasonal'})
                self.assertFalse(found['shopify'])
                self.assertEqual(found['custom_platform'], platform)
                self.assertEqual(found['mode'], 'custom_site')
                client.json.assert_not_called()

    def test_demandware_navigation_uses_declared_levels(self):
        html = '''
        <header><nav class="b-menu_bar-container"><ul>
          <li class="b-menu_bar-item">
            <a class="b-menu_bar-link" data-level="1" href="/us/men/">Men</a>
            <div><ul><li class="b-menu_item"><h3>
              <a class="b-menu_item-link" data-level="2" href="/us/men/shoes/">Shoes</a>
            </h3><ul>
              <li class="b-menu_item"><a class="b-menu_item-link" data-level="3" href="/us/men/shoes/clogs/">Clogs</a></li>
              <li class="b-menu_item"><a class="b-menu_item-link m-all_link" data-level="3" href="/us/men/shoes/">View all</a></li>
            </ul></li></ul></div>
          </li>
        </ul></nav></header>'''
        categories = demandware_navigation(html, 'https://example.com/us/')
        self.assertEqual(categories, {
            'Men': 'https://example.com/us/men/',
            'Men,Men Shoes': 'https://example.com/us/men/shoes/',
            'Men,Men Shoes,Men Shoes Clogs': 'https://example.com/us/men/shoes/clogs/',
        })
        self.assertEqual(identify_platform('<a href="/on/demandware.store/x">x</a>'),
                         'demandware')

    def test_custom_mapping_accepts_independent_variant_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {'product': {'id': 'group-1', 'productName': 'Clog',
                                'images': [{'url': 'https://cdn.example/a.jpg'}]}}
            variants = [
                {'product': {'id': 'sku-1', 'price': {'sales': {'value': 10}},
                             'selectedAttributes': {'size': {'displayValue': '{"wsize":"41;10"}'}}}},
                {'product': {'id': 'sku-2', 'price': {'sales': {'value': 12}},
                             'selectedAttributes': {'size': {'displayValue': '{"wsize":"42;11"}'}}}},
            ]
            save_json(root / 'evidence/base.json', base)
            for index, raw in enumerate(variants):
                save_json(root / f'evidence/v{index}.json', raw)
            product = {'id': 'group-1', 'title': 'Clog', 'handle': 'clog',
                       'options': [{'name': 'Size'}],
                       'images': [{'src': 'https://cdn.example/a.jpg'}],
                       'variants': [
                           {'id': 'sku-1', 'price': 10, 'option1': '41'},
                           {'id': 'sku-2', 'price': 12, 'option1': '42'},
                       ]}
            sample = {'url': 'https://example.com/clog.html', 'product': product,
                      'source_mapping': {
                          'file': 'evidence/base.json',
                          'product_fields': {'id': '/product/id', 'title': '/product/productName'},
                          'variant_sources': [
                              {'file': 'evidence/v0.json', 'path': '/product'},
                              {'file': 'evidence/v1.json', 'path': '/product'},
                          ],
                          'variant_fields': {'id': '/id', 'price': '/price/sales/value',
                                             'option1': '/selectedAttributes/size/displayValue'},
                          'variant_transforms': {'option1': 'sfcc_size_eu'},
                          'image_paths': ['/product/images/0/url'],
                      }}
            verify_source_mapping(root, sample)


if __name__ == '__main__':
    unittest.main()
