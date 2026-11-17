import copy
import json
from html import escape
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from core.common import AdapterRequired, CrawlError, read_json, save_json
from core.sfcc_storefront import (
    _normalize, _root_endpoint, _variation_tree, collect_listing,
    collect_react_listing, fetch_react_product, is_client_rendered_shell,
    navigation, product_market, seed_react_cards, validate_listing,
    verify_source, supports, fetch_product,
)
from core.validator import verify_source_mapping


BASE = 'https://store.example/'
ENDPOINT = BASE + 'on/demandware.store/Sites-store-Site/en_US/Product-Variation'
ROOT = ENDPOINT + '?dwvar_master_color=&pid=master&quantity=1'
BLUE = ENDPOINT + '?dwvar_master_color=blue&pid=master&quantity=1'
RED_SMALL = ENDPOINT + '?dwvar_master_color=red&dwvar_master_size=small&pid=master'
RED_LARGE = ENDPOINT + '?dwvar_master_color=red&dwvar_master_size=large&pid=master'
BLUE_LARGE = ENDPOINT + '?dwvar_master_color=blue&dwvar_master_size=large&pid=master'


def response(color, size=''):
    colors = [{'displayValue': 'Red', 'selected': color == 'red', 'url': ROOT},
              {'displayValue': 'Blue', 'selected': color == 'blue', 'url': BLUE}]
    sizes = [{'displayValue': 'Small', 'selected': size == 'small',
              'hasVariants': color == 'red', 'url': RED_SMALL},
             {'displayValue': 'Large', 'selected': size == 'large',
              'url': RED_LARGE if color == 'red' else BLUE_LARGE}]
    return {'locale': 'en_US', 'product': {
        'id': color + '-' + size if size else 'master',
        'productName': 'Bag', 'productType': 'variant' if size else 'master',
        'variationAttributes': [{'id': 'color', 'displayName': 'Color', 'values': colors},
                                {'id': 'size', 'displayName': 'Size', 'values': sizes}],
        'price': {'sales': {'value': 20, 'currency': 'USD'}}, 'available': True,
        'selectedProductUrl': '/bag/master.html?color=' + color + '&size=' + size,
        'images': {'large': [{'url': BASE + color + '.jpg'}]},
    }}


def manifest():
    return {'url': BASE + 'bag/master.html', 'endpoint': ROOT,
            'html': '<a href="' + ROOT.replace('&', '&amp;') + '">Red</a>',
            'responses': {ROOT: response('red'), BLUE: response('blue'),
                          RED_SMALL: response('red', 'small'),
                          RED_LARGE: response('red', 'large'),
                          BLUE_LARGE: response('blue', 'large')}}


HTML_RED = BASE + 'US/show-variation/?pid=master&dwvar_master_color=red'
HTML_BLUE = BASE + 'US/show-variation/?pid=master&dwvar_master_color=blue'


def html_variant(color, size='', canonical=BASE + 'shirt.html'):
    metadata = {'item_id': 'master', 'item_name': 'Shirt', 'item_variant': color,
                'color': color.title(), 'item_brand': 'Maker', 'price': 20}
    colors = ''.join('<li class="' + ('selected' if color == name else '') + '"><a href="'
                     + escape(url, quote=True) + '"></a></li>'
                     for name, url in [('red', HTML_RED), ('blue', HTML_BLUE)])
    sizes = []
    for value in (['S', 'L'] if color == 'red' else ['L']):
        target = (HTML_RED if color == 'red' else HTML_BLUE) + '&dwvar_master_size=' + value
        selected = ' selected' if value == size else ''
        stock = 'unselectable' if value == 'L' else 'selectable'
        href = '' if value == 'L' and color == 'red' and not size else ' href="' + escape(target, quote=True) + '"'
        sizes.append('<li class="' + stock + selected + '"><a data-size="' + value + '"'
                     + href + '>' + value + '</a></li>')
    return '<html><link rel="canonical" href="' + canonical + '''"><script>CQuotient.locale = 'en_US';var data={"ecommerce":{"currency":"USD"}};</script>
      <div id="pdpMain"><h1>Shirt</h1><div class="product-variations">
      <ul class="swatches color">''' + colors + '''</ul><ul class="swatches online size">''' + ''.join(sizes) + '''</ul></div>
      <input class="ga4-gtm-product-data" data-gtag="''' + escape(json.dumps(metadata), quote=True) + '''">
      <input name="pid" value="''' + (color + '-' + size if size else 'master') + '''">
      <input id="rt_ats" value="''' + ('0' if size == 'L' else '4') + '''">
      <div class="product-price"><span class="price-sales">$20.00</span></div>
      <div class="product-thumbnails"><a class="thumbnail-link" href="/''' + color + '''.jpg"></a></div>
      <div id="description-tab"><p>Shirt <a href="/care">care</a></p></div></div></html>'''


def html_manifest():
    urls = {HTML_BLUE: html_variant('blue')}
    for color, sizes in [('red', ['S', 'L']), ('blue', ['L'])]:
        prefix = HTML_RED if color == 'red' else HTML_BLUE
        for size in sizes:
            urls[prefix + '&dwvar_master_size=' + size] = html_variant(color, size)
    return {'url': BASE + 'shirt.html', 'format': 'sitegenesis_html', 'html': html_variant('red'),
            'responses': {url: {'url': url, 'html': html} for url, html in urls.items()}}


class SfccStorefrontTests(unittest.TestCase):
    def test_react_customer_listing_paginates_in_api_order(self):
        def card(master, sku):
            return {'id': f'{master}-{sku}', 'masterId': master, 'name': 'Item ' + master,
                    'orderable': True, 'size': 'Full',
                    'image': BASE + f'images/{master}.png',
                    'description': 'Description ' + master,
                    'url': f'/item-{master}/{master}.html',
                    'price': {'current': {'value': 10 + int(master)}}}

        def payload(start, products, total=3):
            return {'dataLayer': {'country_code': 'US', 'language_code': 'en_US',
                                  'currency': 'USD', 'ecom_page_type': 'searchresults'},
                    'paging': {'start': start, 'size': 2, 'total': total, 'current': 1},
                    'products': products}

        first = payload(0, [card('1', '10'), card('2', '20')])
        second = payload(2, [card('3', '30')])
        client = Mock()
        client.json.side_effect = [first, second]
        response = SimpleNamespace(url=BASE + 'category', text='<div id="root"><div class="loader"></div></div>')
        listing = collect_react_listing(client, response.url, response)
        self.assertEqual(listing['urls'], [BASE + 'item-1/1.html', BASE + 'item-2/2.html',
                                           BASE + 'item-3/3.html'])
        self.assertEqual(listing['source_mapping']['products_path'], '/products')
        self.assertIn('customer=true', client.json.call_args_list[0].args[0])
        self.assertIn('start=2', client.json.call_args_list[1].args[0])
        validate_listing(listing, listing['urls'])

    def test_react_customer_product_raw_source_mapping_and_market_replay(self):
        card = {'id': '1470-1470', 'masterId': '1470', 'name': 'Mandelic Exfoliant',
                'orderable': True, 'size': '3 oz', 'image': BASE + 'images/1470.png',
                'description': 'Gentle exfoliant', 'url': '/item/1470.html',
                'price': {'current': {'value': 37}}}
        listing = {'url': BASE + 'category', 'source_format': 'react_customer_json',
                   'feeds': [{'products': [card]}]}
        customer = {'dataLayer': {'country_code': 'US', 'language_code': 'en_US',
                                  'currency': 'USD', 'ecom_page_type': 'product',
                                  'product_master_id': '1470', 'product_short_name': 'Mandelic'},
                    'variants': [
                        {'id': '1470-1470', 'orderable': True, 'quantityAvailable': 8,
                         'price': {'current': {'value': 37}}},
                        {'id': '1470-1477', 'orderable': False, 'quantityAvailable': 0,
                         'price': {'current': {'value': 15}}},
                    ]}
        client = Mock()
        seed_react_cards(client, {'AHA': listing})
        client.get.return_value = SimpleNamespace(
            url=BASE + 'item/1470.html', text='<div id="root"><div class="loader"></div></div>')
        client.json.return_value = customer
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = fetch_react_product(client, BASE + 'item/1470.html', root / 'evidence')
            mapping = sample['source_mapping']
            self.assertEqual(mapping['variants_path'], '/customer/variants')
            self.assertEqual(mapping['variant_fields']['price'], '/price/current/value')
            self.assertEqual(read_json(root / mapping['raw_file']), customer)
            self.assertEqual(product_market(root, sample), ('US', 'USD'))
            verify_source_mapping(root, sample)
            sample['product']['variants'][0]['price'] = 999
            with self.assertRaises(CrawlError):
                verify_source_mapping(root, sample)

    def test_csr_shell_routes_to_customer_json_without_adapter_failure(self):
        html = ('<html><body><div id="root"><div class="loader"></div></div>'
                '<script type="module">' + ('x' * 60_000) + '</script></body></html>')
        self.assertTrue(is_client_rendered_shell(html))
        client = Mock()
        client.get.return_value = SimpleNamespace(url=BASE + 'category', text=html)
        client.json.return_value = {
            'dataLayer': {'country_code': 'US', 'language_code': 'en_US',
                          'currency': 'USD', 'ecom_page_type': 'searchresults'},
            'paging': {'start': 0, 'size': 40, 'total': 1, 'current': 1},
            'products': [{'id': '1-10', 'masterId': '1', 'name': 'Item',
                          'image': BASE + '1.png', 'url': '/item/1.html',
                          'price': {'current': {'value': 10}}}]}
        listing = collect_listing(client, BASE + 'category')
        self.assertEqual(listing['source_format'], 'react_customer_json')
        self.assertEqual(listing['urls'], [BASE + 'item/1.html'])
        client.get.assert_called_once()

    def test_react_disclosure_panel_preserves_root_and_nested_ownership(self):
        html = '''<nav><ul><li><span role="button" aria-expanded="true"
          aria-controls="menu">Expand Shop All</span><div id="menu">
          <h2 class="HeaderNavigationstyles__FlyoutTitle-sc-x">Shop All</h2>
          <a href="/skin-care-products">Shop All Categories</a>
          <li class="HeaderNavigationstyles__SubItem-sc-x"><div><a href="/exfoliants">Exfoliants</a></div>
          <ul><li><a href="/exfoliants/aha">AHA Exfoliants</a></li>
          <li><a href="/exfoliants/bha">BHA Exfoliants</a></li></ul></li>
          <a href="/skin-care-products/sale">Sale</a></div></li></ul></nav>
          <footer><a href="/account">Account</a></footer>'''
        self.assertEqual(navigation(html, BASE), {
            'Shop All': BASE + 'skin-care-products',
            'Shop All,Shop All Exfoliants': BASE + 'exfoliants',
            'Shop All,Shop All Exfoliants,Shop All Exfoliants AHA Exfoliants': BASE + 'exfoliants/aha',
            'Shop All,Shop All Exfoliants,Shop All Exfoliants BHA Exfoliants': BASE + 'exfoliants/bha',
            'Shop All,Shop All Sale': BASE + 'skin-care-products/sale',
        })

    def test_react_customer_json_keeps_real_skus_inventory_sizes_and_gallery(self):
        schema = {'@type': 'Product', 'name': 'Exfoliant', 'sku': '1470',
                  'description': 'Gentle exfoliant', 'brand': {'name': 'Maker'}}
        html = '''<link rel="canonical" href="https://store.example/item/1470.html">
          <script type="application/ld+json">''' + json.dumps(schema) + '''</script>
          <div class="ProductDetailsMainInfoRowStudioOnestyles__SizeButtonWrapper-sc-x">
          <span class="ProductDetailsMainInfoRowStudioOnestyles__SizeName-sc-x">Full</span>
          <span class="ProductDetailsMainInfoRowStudioOnestyles__SizeVolume-sc-x">3 oz.</span></div>
          <div class="ProductDetailsMainInfoRowStudioOnestyles__SizeButtonWrapper-sc-x">
          <span class="ProductDetailsMainInfoRowStudioOnestyles__SizeName-sc-x">Travel</span>
          <span class="ProductDetailsMainInfoRowStudioOnestyles__SizeVolume-sc-x">1 oz.</span></div>
          <div class="ProductImageViewerStudioOnestyles__Image-sc-x"><img src="/1470.jpg"></div>'''
        source = {'format': 'react_customer_json', 'url': BASE + 'item/1470.html',
                  'rendered_html': html, 'customer': {
                      'dataLayer': {'product_master_id': '1470', 'currency': 'USD',
                                    'country_code': 'US', 'language_code': 'en_US'},
                      'variants': [
                          {'id': '1470-1', 'orderable': True, 'quantityAvailable': 8,
                           'price': {'current': {'value': 37}}},
                          {'id': '1470-2', 'orderable': False, 'quantityAvailable': 0,
                           'price': {'current': {'value': 15}}},
                      ]}}
        product = _normalize(source)
        self.assertEqual([v['id'] for v in product['variants']], ['1470-1', '1470-2'])
        self.assertEqual(product['variants'][0]['option1'], 'Full 3 oz.')
        self.assertFalse(product['variants'][1]['available'])
        self.assertEqual(product['images'][0]['variant_ids'], ['1470-1', '1470-2'])


    def test_sitegenesis_columns_keep_desktop_ownership_and_skip_gift_cards(self):
        tree = '''<ul class="top-level-container"><li><a href="javascript:void(0)">Women</a>
          <ul><li><a href="/all">Shop All</a></li><li><a href="/tops">Clothing</a>
          <span><a href="/shirts">Shirts</a></span></li><li><a href="/e-gift-cards">Gift Cards</a></li></ul></li></ul>'''
        html = '<nav class="hide-for-xlarge">' + tree.replace('Women', 'Mobile') + '</nav><nav>' + tree + '</nav>'
        self.assertEqual(navigation(html, BASE), {
            'Women': BASE + 'all', 'Women,Women Clothing': BASE + 'tops',
            'Women,Women Clothing,Women Clothing Shirts': BASE + 'shirts'})

    def test_sitegenesis_grid_uses_real_next_link(self):
        first = '<ul id="search-result-items"><li><div class="product-tile" data-itemid="red">' \
                '<a class="name-link" href="/red.html">Red</a></div></li></ul><link rel="next" href="?start=1&amp;sz=1">'
        last = first.split('<link')[0].replace('red', 'blue').replace('Red', 'Blue')
        listing = {'complete': True, 'urls': [BASE + 'red.html', BASE + 'blue.html'], 'pages': [
            {'url': BASE + 'tops', 'html': first}, {'url': BASE + 'tops?start=1&sz=1', 'html': last}]}
        validate_listing(listing, listing['urls'])
        listing['pages'].pop()
        with self.assertRaises(CrawlError):
            validate_listing(listing, listing['urls'])

    def test_gift_card_parent_label_keeps_real_gift_categories(self):
        html = '''<nav><ul class="top-level-container"><li><button>Gifts &amp; E-Gift Cards</button>
          <ul><li><a href="/gifts/all/">Shop All</a></li><li><a href="/gifts/for-her/">Gifts For Her</a></li>
          <li><a href="/e-gift-cards/">E-Gift Cards</a></li></ul></li></ul></nav>'''
        result = navigation(html, BASE)
        self.assertEqual(result, {
            'Gifts & E-Gift Cards': BASE + 'gifts/all/',
            'Gifts & E-Gift Cards,Gifts & E-Gift Cards Gifts For Her': BASE + 'gifts/for-her/'})

    def test_featured_name_collision_keeps_both_targets_and_owns_column_children(self):
        html = '''<nav><ul class="top-level-container"><li><button>Women</button>
          <ul class="feature-cat-container"><li><a href="/jeans-promo/">Jeans</a></li></ul>
          <ul class="menu-category-container"><li><a href="/denim/">Jeans</a>
          <span><a href="/denim/shorts/">Shorts</a></span></li></ul></li></ul></nav>'''
        self.assertEqual(navigation(html, BASE), {
            'Women,Women Jeans (Featured)': BASE + 'jeans-promo/',
            'Women,Women Jeans': BASE + 'denim/',
            'Women,Women Jeans,Women Jeans Shorts': BASE + 'denim/shorts/'})

    def test_html_variants_include_verified_sold_out_skus_and_color_images(self):
        product = _normalize(html_manifest())
        self.assertEqual([row['id'] for row in product['variants']], ['red-S', 'red-L', 'blue-L'])
        self.assertFalse(product['variants'][1]['available'])
        self.assertEqual(product['variants'][1]['inventory_quantity'], 0)
        self.assertEqual(product['images'][1]['variant_ids'], ['blue-L'])
        self.assertNotIn('<a', product['body_html'])
        self.assertTrue(supports(BASE, html_variant('red')))

    def test_html_variants_require_leaf_source_and_exact_selection(self):
        source = html_manifest()
        del source['responses'][HTML_RED + '&dwvar_master_size=L']
        with self.assertRaises(CrawlError):
            _normalize(source)
        source = html_manifest()
        source['responses'][HTML_RED + '&dwvar_master_size=L']['html'] = html_variant('red', 'S')
        with self.assertRaises(CrawlError):
            _normalize(source)
        source = html_manifest()
        source['responses'][HTML_BLUE]['html'] = html_variant('red')
        with self.assertRaises(CrawlError):
            _normalize(source)

    def test_html_product_metadata_excludes_recommended_products(self):
        source = html_manifest()
        recommendation = '<div class="swiper-slide"><input class="ga4-gtm-product-data" data-gtag="' \
            + escape(json.dumps({'item_id': 'another', 'price': 99}), quote=True) + '"></div>'
        source['html'] = source['html'].replace('<h1>Shirt</h1>', recommendation + '<h1>Shirt</h1>')
        product = _normalize(source)
        self.assertEqual(product['id'], 'master')
        self.assertEqual(len(product['variants']), 3)

    def test_same_parent_color_card_reuses_complete_source_and_keeps_identity_evidence(self):
        source = html_manifest()
        pages = {url: value['html'] for url, value in source['responses'].items()}
        pages[BASE + 'red-shirt.html'] = html_variant('red', canonical=BASE + 'red-shirt.html')
        pages[BASE + 'blue-shirt.html'] = html_variant('blue', canonical=BASE + 'blue-shirt.html')
        client = Mock()
        client.get.side_effect = lambda url, **kwargs: SimpleNamespace(url=url, text=pages[url])
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / 'evidence'
            first = fetch_product(client, BASE + 'red-shirt.html', evidence)
            calls = client.get.call_count
            second = fetch_product(client, BASE + 'blue-shirt.html', evidence)
            self.assertEqual(client.get.call_count, calls + 1)
            self.assertEqual(first['product'], second['product'])
            verify_source(directory, second)
            raw = read_json(Path(directory) / second['source_mapping']['file'])
            self.assertIn('family_source', raw)
            raw['html'] = raw['html'].replace('&quot;item_id&quot;: &quot;master&quot;',
                                              '&quot;item_id&quot;: &quot;different&quot;')
            with self.assertRaises(CrawlError):
                _normalize(raw)

    def test_column_navigation_and_home_exclusions(self):
        html = '''<nav><ul class="navbar-nav"><li><button>Shop</button>
          <div class="d-lg-none"><a href="/mobile">Mobile</a></div>
          <div class="dropdown-menu"><div><h2><a href="/cookware">Cookware</a></h2>
          <div class="dropdown-submenu"><a href="/ovens">Dutch Ovens</a></div>
          </div></div></li></ul></nav><main>
          <a href="/seasonal">Shop Now</a><a href="/pot/123US.html">Pot</a>
          <a href="/blog/recipe">Recipe</a></main>'''
        client = Mock()
        client.get.return_value = SimpleNamespace(text='<div class="product-grid"><div class="product-tile"></div></div>')
        self.assertEqual(navigation(html, BASE, client), {
            'Shop,Shop Cookware': BASE + 'cookware',
            'Shop,Shop Cookware,Shop Cookware Dutch Ovens': BASE + 'ovens',
            'seasonal': BASE + 'seasonal',
        })
        client.get.assert_called_once_with(BASE + 'seasonal', timeout=20)

    def test_home_editorial_and_product_recommendations_are_excluded(self):
        client = Mock()
        client.get.return_value = SimpleNamespace(text='<article>Gift guide</article><div class="product-tile"></div>')
        self.assertEqual(navigation('<main><a href="/guide">Guide</a></main>', BASE, client), {})

    def test_explicit_levels_keep_repeated_child_in_each_root(self):
        html = '<nav><ul class="menu-level-1">' + ''.join(
            '<li><a href="/' + name + '">' + name + '</a><ul class="menu-level-2">'
            '<li><a href="/new">New Arrivals</a></li></ul></li>'
            for name in ['Bags', 'Luggage']) + '</ul></nav>'
        result = navigation(html, BASE)
        self.assertIn('Bags,Bags New Arrivals', result)
        self.assertIn('Luggage,Luggage New Arrivals', result)

    def test_root_clears_selected_options_retains_declared_parameter(self):
        html = '<a href="' + RED_LARGE + '">Red</a>'
        self.assertEqual(_root_endpoint(html, BASE),
                         ENDPOINT + '?dwvar_master_color=&pid=master')

    def test_tree_keeps_all_real_skus_without_cartesian_product(self):
        source = manifest()
        remaining = {ROOT: source['responses'][ROOT]}
        calls = []

        def fetch(url):
            calls.append(url)
            return source['responses'][url]

        order, leaves = _variation_tree(ROOT, remaining, fetch)
        self.assertEqual(order, ['color', 'size'])
        self.assertEqual(leaves, [RED_SMALL, RED_LARGE, BLUE_LARGE])
        self.assertEqual(len(calls), 4)
        product = _normalize(source)
        self.assertEqual([v['id'] for v in product['variants']],
                         ['red-small', 'red-large', 'blue-large'])
        self.assertEqual(product['images'][1]['variant_ids'], ['blue-large'])

    def test_offline_replay_rejects_missing_leaf_and_price_tampering(self):
        source = manifest()
        product = _normalize(source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'evidence').mkdir()
            save_json(root / 'evidence/source.json', source)
            sample = {'url': source['url'], 'product': product,
                      'source_mapping': {'file': 'evidence/source.json'}}
            verify_source(root, sample)
            self.assertEqual(product_market(root, sample), ('US', 'USD'))
            sample['product']['variants'][0]['price'] = 19
            with self.assertRaises(CrawlError):
                verify_source(root, sample)
        del source['responses'][BLUE_LARGE]
        with self.assertRaises(CrawlError):
            _normalize(source)

    def test_non_us_response_is_rejected(self):
        source = manifest()
        source['responses'][BLUE_LARGE]['locale'] = 'en_CA'
        with self.assertRaises(CrawlError):
            _normalize(source)

    def test_listing_replays_order_queries_and_terminal_page(self):
        def tile(pid):
            return '<div class="product-tile" data-pid="' + pid + '">' \
                   '<a class="product-tile-image-link" href="/' + pid + '.html?color=red"></a></div>'
        first = tile('1') + '<button class="more" data-url="/next"></button>'
        second = tile('2')
        links = [BASE + '1.html?color=red', BASE + '2.html?color=red']
        listing = {'complete': True, 'urls': links, 'pages': [
            {'url': BASE + 'category', 'html': first},
            {'url': BASE + 'next', 'html': second}]}
        validate_listing(listing, links)
        incomplete = copy.deepcopy(listing)
        incomplete['pages'].pop()
        with self.assertRaises(CrawlError):
            validate_listing(incomplete, links)


if __name__ == '__main__':
    unittest.main()
