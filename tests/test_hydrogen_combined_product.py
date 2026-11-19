import unittest
from unittest.mock import Mock

from core.common import CrawlError
from core.shopify_fastpath import fetch_product, hydrogen_product_from_html, product_rows
from test_hydrogen_fastpath import product, stream


def grouped_route():
    parent = product('sheet_', '100', 'White', '1')
    group_url = 'https://example.com/products/sheet/'
    gid = group_url + '#group'
    children = [product('sheet-white_', '101', 'White', '2'),
                product('sheet-blue_', '102', 'Blue', '3')]
    structured = [{'@type': 'ProductGroup', 'productGroupID': '100', 'url': group_url, '@id': gid}]
    for child in children:
        child['parent'] = {'reference': {'id': parent['id']}}
        child['media']['nodes'][0]['mediaContentType'] = 'IMAGE'
        variant = child['variants']['nodes'][0]
        colour = variant['selectedOptions'][0]['value']
        structured.append({'@type': 'Product', 'isVariantOf': {'@id': gid},
                           'sku': variant['sku'], 'color': colour, 'size': 'M',
                           'offers': {'priceCurrency': 'USD', 'price': '49.0',
                                      'url': group_url + '?color=' + colour}})
    parent['childProducts'] = {'references': {'nodes': children}}
    return {'rootProductId': parent['id'], 'rootProduct': parent, 'jsonLd': structured}


def parse(route):
    return hydrogen_product_from_html(stream({'loaderData': {'routes/($locale).products.$handle': route}}),
                                      'sheet')


class CombinedProductTests(unittest.TestCase):
    def test_hydrogen_card_reads_query_pdp_and_checks_listing_parent(self):
        url = 'https://example.com/products/sheet/?color=White&size=M'
        card = {'id': '100', 'handle': 'sheet', 'variants': [], '_hydrogen_partial': True}
        client = Mock()
        client.get.return_value.text = stream({'loaderData': {'routes/products.$handle': grouped_route()}})
        sample = fetch_product(client, url, card)
        client.json.assert_not_called()
        client.get.assert_called_once_with(url, attempts=3, timeout=60)
        self.assertEqual([v['sku'] for v in sample['product']['variants']], ['SKU-2', 'SKU-3'])
        with self.assertRaisesRegex(CrawlError, '身份与分类卡片不一致'):
            fetch_product(client, url, dict(card, id='999'))

    def test_missing_pdp_or_configurable_bundle_reports_url_and_reason(self):
        url = 'https://example.com/products/sheet/'
        card = {'id': '100', 'handle': 'sheet', 'variants': [{'id': '1'}], '_hydrogen_partial': True}
        client = Mock()
        for route, reason in [({}, '商品页缺少受支持'), ({'bundle': {'id': '100'}, 'productItems': [
                {'selectors': []}]}, '配置型套装缺少独立可验证变体')]:
            with self.subTest(reason=reason):
                client.get.return_value.text = stream({'loaderData': {'routes/products.$handle': route}})
                with self.assertRaises(CrawlError) as error:
                    fetch_product(client, url, card)
                self.assertIn(url, str(error.exception))
                self.assertIn(reason, str(error.exception))
        client.json.assert_not_called()

    def test_complete_collection_fallback_preserved_and_ajax_suffix_precedes_query(self):
        url = 'https://example.com/products/sheet/?color=White'
        source = {'handle': 'sheet', 'variants': [{'id': '1'}]}
        client = Mock()
        client.json.return_value = {'product': source}
        result = fetch_product(client, url, source)
        client.json.assert_called_once_with('https://example.com/products/sheet.json?color=White')
        self.assertEqual(result['source'], 'json')
        client.json.side_effect = CrawlError('404')
        client.get.return_value.text = '<html></html>'
        result = fetch_product(client, url, source)
        self.assertEqual(result['endpoint'], 'listing.json')
        self.assertEqual(result['product'], source)

    def test_export_uses_the_observed_offer_url_for_each_colour(self):
        product_data = parse(grouped_route())
        sample = {'url': 'https://example.com/products/sheet/?color=White',
                  'product': product_data, 'source': 'collection',
                  'source_format': 'hydrogen_devalue'}
        rows = product_rows(sample, ['Sheets'])
        self.assertEqual([row['External URL'] for row in rows], [
            'https://example.com/products/sheet/?color=White',
            'https://example.com/products/sheet/?color=Blue',
        ])
        self.assertEqual({row['Parent'] for row in rows}, {'100'})
        self.assertNotEqual(rows[0]['Variant Image'], rows[1]['Variant Image'])

    def test_real_child_currency_images_and_sku_identity_survive_parent_merge(self):
        result = parse(grouped_route())
        self.assertEqual(result['id'], '100')
        self.assertEqual(result['handle'], 'sheet')
        self.assertEqual([(v['id'], v['source_child_product_id'], v['option1'], v['sku'])
                          for v in result['variants']],
                         [('2', '101', 'White', 'SKU-2'), ('3', '102', 'Blue', 'SKU-3')])
        self.assertTrue(result['variants'][1]['featured_image']['src'].endswith('sheet-blue_.jpg'))
        self.assertTrue(result['variants'][1]['source_variant_url'].endswith('?color=Blue'))

    def test_lost_child_forged_price_or_foreign_offer_fails(self):
        for mutation in ('child', 'price', 'url', 'owner'):
            with self.subTest(mutation=mutation):
                route = grouped_route()
                if mutation == 'child':
                    route['rootProduct']['childProducts']['references']['nodes'].pop()
                elif mutation == 'price':
                    route['jsonLd'][1]['offers']['price'] = '0.00'
                elif mutation == 'url':
                    route['jsonLd'][1]['offers']['url'] = 'https://other.com/products/sheet/'
                else:
                    route['rootProduct']['childProducts']['references']['nodes'][0]['parent'] = {}
                with self.assertRaises(CrawlError):
                    parse(route)
