import unittest

from core.shopify_fastpath import normalize_product_images, variant_images
from core.simple_nav import _thin_flat_navigation
from core.validator import image_quality


class ProductDataQualityTests(unittest.TestCase):
    def test_reports_unbound_visual_options(self):
        product = {'id': 'p', 'images': ['https://shop.example/a.jpg', 'https://shop.example/b.jpg'],
                   'options': [{'name': 'Color'}],
                   'variants': [{'id': '1', 'option1': 'Black'}, {'id': '2', 'option1': 'White'}]}
        report = image_quality([{'url': 'https://shop.example/products/p', 'product': product}])
        self.assertEqual(report['variants_with_bound_images'], 0)
        self.assertEqual(report['warnings'][0]['code'], 'visual_variants_without_images')

    def test_only_source_bound_variant_images_survive(self):
        product = {
            'images': [{'src': '/logo.svg'}, {'src': '/black.jpg', 'variant_ids': [1]}],
            'variants': [{'id': '1'}, {'id': '2', 'featured_image': '/white.jpg'},
                         {'id': '3'}],
        }
        normalize_product_images(product, 'https://shop.example')
        self.assertEqual(len(product['images']), 1)
        self.assertEqual(product['variants'][0]['featured_image'],
                         {'src': 'https://shop.example/black.jpg'})
        self.assertEqual(variant_images(product, product['variants'][1]),
                         ['https://shop.example/white.jpg'])
        self.assertEqual(variant_images(product, product['variants'][2]), [])

    def test_sparse_flat_navigation_with_trigger_gets_browser_pass(self):
        data = {'source_navigation_urls': list(range(10)), 'navigation_outline': '[a] Shop -> /shop'}
        html = '<header><button aria-expanded="false">Shop</button></header>'
        self.assertTrue(_thin_flat_navigation(data, html))
        data['navigation_outline'] += '\n  [a] Shoes -> /shoes'
        self.assertFalse(_thin_flat_navigation(data, html))
