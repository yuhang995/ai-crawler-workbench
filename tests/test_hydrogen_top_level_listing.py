import unittest
from unittest.mock import patch

from core.common import CrawlError
from core.shopify_fastpath import (_expand_adjacent_variant_product,
                                   _hydrogen_collection_page,
                                   _normalize_hydrogen_product)


URL = 'https://example.com/collections/new'


def route():
    return {
        'collection': {'handle': 'new', 'title': 'New'},
        'products': [
            {'type': 'product', 'id': '101', 'variationId': '201',
             'handle': 'first', 'price': {'amount': '10', 'currencyCode': 'USD'}},
            {'type': 'product', 'id': '102', 'variationId': '202',
             'handle': 'second', 'price': {'amount': '12', 'currencyCode': 'USD'}},
            {'type': 'product', 'id': '103', 'variationId': '203',
             'handle': 'third', 'price': {'amount': '14', 'currencyCode': 'USD'}},
        ],
        'pagination': {'currentPage': 1, 'resultsPerPage': 3,
                       'totalPages': 2, 'totalResults': 6},
    }


class HydrogenTopLevelListingTests(unittest.TestCase):
    def test_server_loader_products_and_pagination_are_accepted(self):
        with patch('core.shopify_fastpath._hydrogen_route', return_value=route()):
            found = _hydrogen_collection_page('<main/>', URL)
        self.assertEqual(found['handles'], ['first', 'second', 'third'])
        self.assertEqual(found['current_page'], 1)
        self.assertEqual(found['total_results'], 6)
        self.assertTrue(found['has_next_page'])
        self.assertEqual(found['pagination_kind'], 'page')

    def test_wrong_collection_or_non_usd_product_is_rejected(self):
        wrong = route()
        wrong['collection']['handle'] = 'other'
        with patch('core.shopify_fastpath._hydrogen_route', return_value=wrong):
            with self.assertRaisesRegex(CrawlError, 'handle'):
                _hydrogen_collection_page('<main/>', URL)

    def test_complete_one_option_adjacent_variants_are_normalized(self):
        def variant(number, value):
            return {'id': f'gid://shopify/ProductVariant/{number}',
                    'title': value, 'sku': value, 'availableForSale': True,
                    'selectedOptions': [{'name': 'Size', 'value': value}],
                    'price': {'amount': '10', 'currencyCode': 'USD'},
                    'compareAtPrice': None,
                    'image': {'id': f'gid://shopify/ProductImage/{number}',
                              'url': f'https://cdn.example.com/{number}.jpg'}}
        small, large = variant(201, 'Small'), variant(202, 'Large')
        source = {'id': 'gid://shopify/Product/101', 'handle': 'item',
                  'title': 'Item', 'encodedVariantExistence': 'v1_0-1',
                  'options': [{'name': 'Size', 'optionValues': [
                      {'name': 'Small', 'firstSelectableVariant': small},
                      {'name': 'Large', 'firstSelectableVariant': large}]}],
                  'selectedOrFirstAvailableVariant': small,
                  'adjacentVariants': [large]}
        expanded = _expand_adjacent_variant_product(source)
        product = _normalize_hydrogen_product(expanded, 'item')
        self.assertEqual([v['id'] for v in product['variants']], ['201', '202'])
        self.assertEqual(product['options'][0]['values'], ['Small', 'Large'])

    def test_incomplete_or_multi_option_adjacent_shape_stays_fail_closed(self):
        source = {'encodedVariantExistence': 'v1_0-2', 'options': [
            {'name': 'Size', 'optionValues': []}], 'adjacentVariants': []}
        self.assertIs(_expand_adjacent_variant_product(source), source)
        multi = {'options': [{'name': 'Size'}, {'name': 'Color'}]}
        self.assertIs(_expand_adjacent_variant_product(multi), multi)
        wrong = route()
        wrong['products'][0]['price']['currencyCode'] = 'CAD'
        with patch('core.shopify_fastpath._hydrogen_route', return_value=wrong):
            with self.assertRaisesRegex(CrawlError, '币种'):
                _hydrogen_collection_page('<main/>', URL)


if __name__ == '__main__':
    unittest.main()
