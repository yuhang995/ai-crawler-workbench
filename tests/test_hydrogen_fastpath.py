import json
import unittest
from copy import deepcopy
from types import SimpleNamespace

from core.common import CrawlError
from core.detector import detect, observe_market
from core.shopify_fastpath import (_hydrogen_collection_page, _hydrogen_route, collect_listing, explicit_colorway_entries,
                                   fetch_product, hydrogen_product_from_html)


BASE = 'https://example.com/'


def stream(value):
    flat = []

    def add(item):
        index = len(flat)
        flat.append(None)
        if isinstance(item, dict):
            encoded = {}
            for key, child in item.items():
                key_index = add(str(key))
                encoded[f'_{key_index}'] = add(child)
            flat[index] = encoded
        elif isinstance(item, list):
            flat[index] = [add(child) for child in item]
        else:
            flat[index] = item
        return index

    add(value)
    payload = json.dumps(flat, separators=(',', ':'))
    return ('<script>window.__reactRouterContext.streamController.enqueue('
            + json.dumps(payload) + ');</script>')


def product(handle='red-shirt', product_id='100', colour='Red', variant_id='1001'):
    image = f'https://cdn.example/{handle}.jpg'
    return {
        'id': f'gid://shopify/Product/{product_id}', 'title': 'Performance Shirt - ' + colour,
        'handle': handle, 'vendor': 'Brand', 'descriptionHtml': '<p>Real</p>',
        'productType': 'Shirt', 'tags': ['Shirt'],
        'featuredImage': {'url': image},
        'media': {'nodes': [{'previewImage': {
            'id': f'gid://shopify/ImageSource/{product_id}', 'url': image}}]},
        'options': [
            {'name': 'Color', 'optionValues': [{'name': colour}]},
            {'name': 'Size', 'optionValues': [{'name': 'M'}]},
        ],
        'variants': {'nodes': [{
            'id': f'gid://shopify/ProductVariant/{variant_id}', 'title': colour + ' / M',
            'availableForSale': True, 'quantityAvailable': 3, 'sku': 'SKU-' + variant_id,
            'image': {'id': f'gid://shopify/ProductImage/{product_id}', 'url': image},
            'price': {'amount': '49.00', 'currencyCode': 'USD'},
            'compareAtPrice': {'amount': '59.00', 'currencyCode': 'USD'},
            'selectedOptions': [{'name': 'Color', 'value': colour},
                                {'name': 'Size', 'value': 'M'}],
        }]},
    }


def product_html():
    red = product()
    blue = product('blue-shirt', '200', 'Blue', '2001')
    grouped_red = deepcopy(red)
    grouped_blue = deepcopy(blue)
    red['initialGrouping'] = {
        'title': "Men's Performance Shirt",
        'products': [{'handle': 'red-shirt', 'id': red['id']},
                     {'handle': 'blue-shirt', 'id': blue['id']}],
        'allProducts': [grouped_red, grouped_blue],
    }
    return stream({'loaderData': {
        'root': {'selectedLocale': {'country': 'US', 'currency': 'USD'}},
        'routes/($locale).products.$handle': {'product': red},
    }}) + '<!-- oxygen-v2 cdn.shopify.com -->'


def collection_html():
    results = [{'handle': 'red-shirt', 'uid': '100', 'url': '/products/red-shirt'},
               {'handle': 'blue-shirt', 'uid': '200', 'url': '/products/blue-shirt'},
               {'handle': 'green-shirt', 'uid': '300', 'url': '/products/green-shirt'}]
    return stream({'loaderData': {
        'root': {'selectedLocale': {'country': 'US', 'currency': 'USD'}},
        'routes/($locale).collections.$handle': {'collection': {
            'results': results, 'pagination': {'totalPages': 1, 'currentPage': 1,
                                               'totalResults': 3, 'perPage': 48}}},
    }}) + '<!-- oxygen-v2 cdn.shopify.com searchspring -->'


def native_collection_html():
    nodes = [product('red-shirt', '100', 'Red', '1001'),
             product('blue-shirt', '200', 'Blue', '2001'),
             product('green-shirt', '300', 'Green', '3001')]
    return stream({'loaderData': {
        'root': {'selectedLocale': {'country': 'US', 'currency': 'USD'}},
        'routes/($locale).collections.$handle': {'collection': {
            'id': 'gid://shopify/Collection/10', 'handle': 'shirts',
            'products': {'nodes': nodes, 'pageInfo': {
                'hasPreviousPage': False, 'hasNextPage': False,
                'startCursor': 'first', 'endCursor': 'last'}}}},
    }}) + '<!-- oxygen-v2 cdn.shopify.com -->'


class Client:
    def get(self, url):
        return SimpleNamespace(text=(product_html() if '/products/' in url else collection_html()),
                               url=url)

    def json(self, url):
        raise CrawlError('HTTP 404')


class HydrogenFastpathTests(unittest.TestCase):
    def test_grouped_collection_uses_distinct_raw_parent_products(self):
        def product(index):
            return {'id': f'gid://shopify/Product/{index}', 'handle': f'item-{index}',
                    'variants': {'nodes': [
                        {'price': {'amount': '10.00', 'currencyCode': 'USD'}}]}}
        raw = [product(index) for index in (1, 2, 3)]
        route = {'collection': {'handle': 'bottles',
                                'products': {f'Title {i}': item for i, item in enumerate(raw)},
                                'rawProducts': raw},
                 'pageInfo': {'hasNextPage': False, 'endCursor': 'done'}}
        html = stream({'loaderData': {'routes/($locale).collections.$handle': route}})
        parsed = _hydrogen_collection_page(
            html, 'https://example.com/en-us/collections/bottles')
        self.assertEqual(parsed['handles'], ['item-1', 'item-2', 'item-3'])
        self.assertEqual(parsed['pagination_kind'], 'cursor')

    def test_locale_product_handle_route_is_supported(self):
        route = {'product': {'id': 'gid://shopify/Product/10',
                             'handle': 'bottle', 'title': 'Bottle'}}
        html = stream({'loaderData': {
            'routes/($locale).products.$productHandle': route}})
        self.assertEqual(_hydrogen_route(html, 'products')['product']['handle'], 'bottle')

    def test_directory_style_react_router_collection_id_is_supported(self):
        route = {'handle': 'shirts', 'products': {'nodes': [{
            'type': 'product', 'data': {
                'id': 'gid://shopify/Product/100', 'handle': 'red-shirt'}
        }], 'pageInfo': {
            'hasNextPage': False, 'endCursor': None}}}
        html = stream({'loaderData': {'routes/collections/$handle/index': route}})
        self.assertEqual(_hydrogen_route(html, 'collections'), route)
        from core.shopify_fastpath import _hydrogen_collection_page
        parsed = _hydrogen_collection_page(html, BASE + 'collections/shirts')
        self.assertEqual(parsed['handles'], ['red-shirt'])
        self.assertEqual(parsed['products'][0]['id'], '100')

    def test_direct_product_route_accepts_edges_and_plain_option_values(self):
        product = {
            'id': 'gid://shopify/Product/100', 'handle': 'red-shirt',
            'title': 'Red Shirt', 'productType': 'Shirt',
            'descriptionHtml': '<p>Red</p>',
            'options': [{'name': 'Size', 'values': ['M']}],
            'variants': {'edges': [{'node': {
                'id': 'gid://shopify/ProductVariant/1001', 'sku': 'R-M',
                'availableForSale': True, 'quantityAvailable': 2,
                'price': {'amount': '10.00', 'currencyCode': 'USD'},
                'compareAtPrice': None,
                'selectedOptions': [{'name': 'Size', 'value': 'M'}]}}]},
            'images': {'edges': [{'node': {
                'id': 'gid://shopify/ProductImage/900',
                'url': 'https://cdn.example/red.jpg', 'altText': 'Red'}}]},
        }
        html = stream({'loaderData': {'routes/products/$handle/index': product}})
        parsed = hydrogen_product_from_html(html, 'red-shirt')
        self.assertEqual(parsed['variants'][0]['id'], '1001')
        self.assertEqual(parsed['options'][0]['values'], ['M'])
        self.assertEqual(parsed['images'][0]['src'], 'https://cdn.example/red.jpg')

    def test_variant_images_form_real_gallery_when_media_is_omitted(self):
        product = {
            'id': 'gid://shopify/Product/100', 'handle': 'bottle', 'title': 'Bottle',
            'options': [{'name': 'Color', 'optionValues': [{'name': 'Blue'}]}],
            'variants': {'nodes': [{
                'id': 'gid://shopify/ProductVariant/1001', 'availableForSale': True,
                'price': {'amount': '10.00', 'currencyCode': 'USD'},
                'selectedOptions': [{'name': 'Color', 'value': 'Blue'}],
                'image': {'id': 'gid://shopify/ProductImage/900',
                          'url': 'https://cdn.example/blue.jpg'}}]}}
        html = stream({'loaderData': {'routes/products/$handle/index': product}})
        parsed = hydrogen_product_from_html(html, 'bottle')
        self.assertEqual(parsed['images'][0]['src'], 'https://cdn.example/blue.jpg')
        self.assertEqual(parsed['images'][0]['variant_ids'], ['1001'])

    def test_market_and_detection_use_server_selected_oxygen_locale(self):
        html = collection_html()
        self.assertEqual(observe_market(html), {
            'countries': ['US'], 'currencies': ['USD'], 'confirmed': True})
        found = detect(Client(), BASE, html, {'Shirts': BASE + 'collections/shirts'})
        self.assertEqual(found['mode'], 'hydrogen_shopify')
        self.assertTrue(found['market']['confirmed'])

    def test_hydrogen_listing_and_product_preserve_source_order_and_variants(self):
        client = Client()
        listing = collect_listing(client, BASE + 'collections/shirts')
        self.assertEqual(listing['format'], 'hydrogen')
        self.assertEqual([url.rsplit('/', 1)[-1] for url in listing['urls']],
                         ['red-shirt', 'blue-shirt', 'green-shirt'])
        sample = fetch_product(client, listing['urls'][0], listing['products'][0])
        self.assertEqual(sample['source_format'], 'hydrogen_devalue')
        self.assertEqual(sample['product']['id'], '100')
        self.assertEqual(sample['product']['variants'][0]['id'], '1001')
        self.assertEqual(sample['product']['variants'][0]['option1'], 'Red')
        self.assertEqual(sample['product']['variants'][0]['option2'], 'M')
        self.assertEqual(sample['product']['variants'][0]['featured_image']['src'],
                         'https://cdn.example/red-shirt.jpg')
        self.assertEqual(hydrogen_product_from_html(
            sample['source_html'], 'red-shirt'), sample['product'])

    def test_native_hydrogen_product_connection_is_valid_listing_evidence(self):
        class NativeClient(Client):
            def get(self, url):
                return SimpleNamespace(text=(product_html() if '/products/' in url
                                             else native_collection_html()), url=url)
        listing = collect_listing(NativeClient(), BASE + 'collections/shirts')
        self.assertEqual(listing['format'], 'hydrogen')
        self.assertEqual([item['handle'] for item in listing['products']],
                         ['red-shirt', 'blue-shirt', 'green-shirt'])
        self.assertTrue(listing['complete'])

    def test_explicit_hydrogen_group_uses_real_colour_images_and_ids(self):
        entries = explicit_colorway_entries(product_html(), 'red-shirt')
        self.assertEqual([entry['handle'] for entry in entries],
                         ['red-shirt', 'blue-shirt'])
        self.assertEqual([entry['color'] for entry in entries], ['Red', 'Blue'])
        self.assertEqual(entries[0]['group_title'], "Men's Performance Shirt")
        self.assertEqual(entries[0]['group_handle'], 'mens-performance-shirt')
        self.assertEqual(entries[1]['product_image'],
                         'https://cdn.example/blue-shirt.jpg')


if __name__ == '__main__':
    unittest.main()
