import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.braun_product import braun_product, braun_product_market, parse_braun_product, verify_braun_product
from core.common import CrawlError


URL = 'https://us.braun.com/en-us/grooming/model-1'


def document(change=None):
    product = {'productName': 'Model 1', 'sku': '00102', 'internalLink': '/grooming/model-1',
               'productDescription': {'nodeType': 'paragraph', 'content': [
                   {'nodeType': 'hyperlink', 'content': [{'nodeType': 'text', 'value': 'Steel head'}]}]},
               'productImageVideo': [{'fields': {'contentType': 'video', 'desktop': '/promo.mp4'}},
                                     {'fields': {'contentType': 'image', 'desktop': '//images.example/item2.jpg'}}],
               'bigCProductDetails': {'id': 11, 'price': 49, 'calculatedPrice': 39.5,
                                      'inventoryLevel': 0, 'availability': 'disabled',
                                      'modifiers': {'data': []}}}
    offer = {'@type': 'Offer', 'priceCurrency': 'USD', 'url': URL, 'price': 39.5,
             'availability': 'https://schema.org/OutOfStock'}
    model = {'@type': 'Product', 'name': 'Model 1', 'sku': '00102', 'offers': offer,
             'image': 'https://images.example/cover.jpg', 'brand': {'name': 'Braun'}}
    if change:
        change(product, model)
    state = {'props': {'pageProps': {'content': {'type': 'product', 'page': product,
                                                'megaPdp': {'fields': {'variants': [{'sku': 'other-model'}]}}}}}}
    return (f'<html lang="en-US"><link rel="canonical" href="{URL}">'
            f'<script id="__NEXT_DATA__">{json.dumps(state)}</script>'
            f'<script type="application/ld+json">{json.dumps(model)}</script></html>')


class BraunProductTests(unittest.TestCase):
    def test_model_sku_price_and_own_gallery_replay(self):
        client = SimpleNamespace(get=lambda _: SimpleNamespace(url=URL, text=document()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = braun_product(client, URL, root/'evidence')
            self.assertEqual(braun_product_market(root, sample), ('US', 'USD'))
            verify_braun_product(root, sample)
            product = sample['product']
            self.assertEqual(product['body_html'], '<p>Steel head</p>')
            self.assertEqual([image['src'] for image in product['images']],
                             ['https://images.example/cover.jpg', 'https://images.example/item2.jpg'])
            variant, = product['variants']
            self.assertEqual((variant['id'], variant['price'], variant['compare_at_price'],
                              variant['available'], variant['inventory_quantity']),
                             ('00102', '39.50', '49.00', False, 0))
            variant['price'] = '99.99'
            with self.assertRaises(CrawlError):
                verify_braun_product(root, sample)

    def test_disagreeing_offer_identity_price_currency_and_unproven_option_are_rejected(self):
        for change in (lambda p, m: m.update(sku='00103'),
                       lambda p, m: m['offers'].update(price=1),
                       lambda p, m: m['offers'].update(priceCurrency='CAD'),
                       lambda p, m: m['offers'].update(url=URL.replace('model-1', 'model-2')),
                       lambda p, m: p['bigCProductDetails']['modifiers']['data'].append(
                           {'product_id': 11, 'display_name': 'Model', 'required': True})):
            with self.subTest(change=change), self.assertRaises(CrawlError):
                parse_braun_product(document(change), URL)

    def test_shipping_interval_without_a_sku_does_not_multiply_the_product(self):
        def add_subscription(p, m):
            p['bigCProductDetails']['modifiers']['data'].append({
                'product_id': 11, 'display_name': 'Ships Every', 'required': False,
                'option_values': [{'label': '6 Months', 'adjusters': {'price': None}},
                                  {'label': '9 Months', 'adjusters': {'price': None}}]})
        self.assertEqual(len(parse_braun_product(document(add_subscription), URL)['variants']), 1)
