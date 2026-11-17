import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.common import save_json
from core.custom_site import demandware_navigation, identify_platform
from core.validator import verify_source_mapping


class CustomSiteTests(unittest.TestCase):
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
