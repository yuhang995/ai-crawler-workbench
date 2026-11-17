import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from core.bigcommerce_storefront import (
    collect_listing, fetch_product, navigation, parse_product, product_market,
    validate_listing, verify_source,
)
from core.common import CrawlError


BASE = 'https://shop.kingarthurbaking.com'
URL = BASE + '/items/flour'


def product_html(**changes):
    attributes = {'sku': '000123', 'base': True,
                  'price': {'without_tax': {'value': 6.95, 'currency': 'USD'}},
                  'stock': None, 'instock': True, 'purchasable': True}
    attributes.update(changes)
    return f'''<html lang="en"><head>
    <link rel="canonical" href="{URL}">
    <meta property="product:price:currency" content="USD">
    <meta property="product:price:amount" content="6.95"></head>
    <body><main data-currency-code="USD"><h1 class="productView-title">Flour</h1>
    <div data-product-sku>#000123</div>
    <form data-cart-item-add><input name="product_id" value="12">
    <div data-product-option-change></div></form>
    <div class="productView-images"><img data-main-image src="/small.jpg"
      srcset="/small.jpg 160w, /large.jpg 2560w">
      <img data-main-image src="data:image/gif;base64,abc"
      data-srcset="/back-small.jpg 160w, /back.jpg 2560w">
      <img src="/video.jpg"></div>
    <div id="tab-description"><p>Real <a href="/recipe">description</a>.</p></div>
    <script>var BCData = {json.dumps({'product_attributes': attributes})};</script>
    </main></body></html>'''


def listing_html(ids, next_page=''):
    cards = ''.join(f'''<li><article class="card" data-entity-id="{value}">
        <div class="card-title"><a href="/items/item-{value}" data-sku="s{value}">
        Item {value}</a></div></article></li>''' for value in ids)
    next_link = f'<li class="pagination-item--next"><a href="{next_page}">Next</a></li>' if next_page else ''
    return f'''<main><div id="product-listing-container"><ul class="productGrid">{cards}</ul></div>
    <ul class="pagination">{next_link}</ul></main>
    <aside><article class="card" data-entity-id="999"><div class="card-title">
    <a href="/items/recommendation">Recommendation</a></div></article></aside>'''


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return SimpleNamespace(url=url, text=self.pages[url])


class BigCommerceStorefrontTests(unittest.TestCase):
    def test_homepage_categories_are_verified_without_menu_prefix_restrictions(self):
        html = '''<nav class="navPages"><ul class="navPages-list">
        <li><a href="/dog-toys/">Dog Toys</a></li>
        <li><a href="/contact-us/">Contact</a></li></ul></nav><main>
        <a class="image-card__link" href="/new-products/">New Products</a>
        <a href="/new-products/">Shop for Gifts</a>
        <a href="/tailored-for-every-dog/outdoor-play/">Outdoor Play</a>
        <a href="/new-products/?type=holiday">Holiday</a>
        <a href="/dog-toys/?utm_source=home">Toys</a>
        <article class="card"><a href="/single-toy/">Toy</a></article>
        <a href="/about-us/">About</a><a href="/news/">News</a>
        <a href="/event/">Event</a></main>'''
        paths = ['/new-products/', '/tailored-for-every-dog/outdoor-play/',
                 '/new-products/?type=holiday']
        client = FakeClient({**{BASE + path: listing_html([1, 2, 3]) for path in paths},
                             BASE + '/event/': '<main><h1>Community Event</h1></main>'})
        with tempfile.TemporaryDirectory() as directory:
            result = navigation(html, BASE, client=client, evidence_dir=directory)
            self.assertEqual(result, {
                'Dog Toys': BASE + '/dog-toys/',
                'new-products': BASE + '/new-products/',
                'outdoor-play': BASE + '/tailored-for-every-dog/outdoor-play/',
                'new-products?type=holiday': BASE + '/new-products/?type=holiday',
            })
            self.assertEqual(client.calls, [BASE + path for path in paths] + [BASE + '/event/'])
            self.assertEqual(len(list(Path(directory).glob('home-category-bigcommerce-*.html'))), 3)

    def test_homepage_product_page_with_recommendations_is_excluded(self):
        html = '''<nav class="navPages"><ul class="navPages-list">
        <li><a href="/dog-toys/">Dog Toys</a></li></ul></nav>
        <main><a href="/single-toy/">Featured Toy</a></main>'''
        client = FakeClient({BASE + '/single-toy/': product_html() + listing_html([1, 2, 3])})
        self.assertEqual(navigation(html, BASE, client=client), {'Dog Toys': BASE + '/dog-toys/'})

    def test_accordion_product_description_keeps_overview_features_and_bundle_contents(self):
        html = product_html().replace('id="tab-description"', 'id="unused-description"')
        html = html.replace('</main>', '''<section class="description-accordion">
        <div id="overview-accordion"><p>Soft toy <a href="/care">care</a>.</p></div>
        <div class="description-accordion__container--features">
          <div class="description-accordion__content"><ul><li>Reinforced fabric</li></ul></div></div>
        <div class="description-accordion__container--summary">
          <div class="description-accordion__content"><p>Includes a treat toy</p></div></div>
        <div class="description-accordion__container--shipping">
          <div class="description-accordion__content">Unrelated shipping policy</div></div>
        </section></main>''')
        product = parse_product(html, URL)
        self.assertIn('Soft toy', product['body_html'])
        self.assertIn('Reinforced fabric', product['body_html'])
        self.assertIn('Includes a treat toy', product['body_html'])
        self.assertNotIn('<a ', product['body_html'])
        self.assertNotIn('Unrelated shipping policy', product['body_html'])

    def test_collapsible_menu_keeps_subcategory_ownership(self):
        html = '''<nav class="navPages"><ul class="navPages-list"><li>
        <a href="/dog-toys/" data-collapsible="dogs">Dog Toys</a>
        <div id="dogs"><ul class="navPage-subMenu-list">
        <li><a href="/dog-toys/">All Dog Toys</a></li>
        <li><a href="/dog-toys/rubber/">Rubber</a></li></ul></div></li>
        <li><a href="/collections/" data-collapsible="collections">Collections</a>
        <div id="collections"><ul class="navPage-subMenu-list">
        <li><a href="/collections/holiday/">Holiday</a></li></ul></div></li>
        </ul></nav><main><a href="/collections/holiday/">Holiday</a></main>'''
        self.assertEqual(navigation(html, BASE), {
            'Dog Toys': BASE + '/dog-toys/',
            'Dog Toys,Dog Toys Rubber': BASE + '/dog-toys/rubber/',
            'Collections': BASE + '/collections/',
            'Collections,Collections Holiday': BASE + '/collections/holiday/',
        })

    def configurable_source(self):
        html = product_html()
        options = ''
        for name, title, values in [('1', 'Size', [('10', 'Small'), ('20', 'Large')]),
                                    ('2', 'Color', [('1', 'Red'), ('2', 'Blue')])]:
            options += '<div data-product-attribute="set-rectangle"><label class="form-label">' + title + '</label>'
            for index, (value, label) in enumerate(values):
                checked = 'checked' if index == 0 else ''
                options += (f'<input type="radio" name="attribute[{name}]" value="{value}" '
                            f'id="opt-{name}-{value}" {checked}><label for="opt-{name}-{value}">{label}</label>')
            options += '</div>'
        html = html.replace('<div data-product-option-change></div>', options)
        context = json.dumps(json.dumps({'countryCode': 'US', 'zoomSize': '1280x1280'}))
        html = html.replace('</body>', '<script>window.stencilBootstrap("product", ' + context + ');</script></body>')
        responses = {}
        for size, color, sku, identifier in [('10', '1', '000123', 101), ('20', '2', '000456', 102)]:
            query = {'attribute[1]': size}
            endpoint = BASE + '/remote/v1/product-attributes/12?' + urlencode(query)
            responses[endpoint] = {'data': {'disabled_option_values': ['2' if color == '1' else '1']}}
            query['attribute[2]'] = color
            endpoint = BASE + '/remote/v1/product-attributes/12?' + urlencode(query)
            responses[endpoint] = {'data': {'sku': sku, 'v3_variant_id': identifier, 'base': False,
                'price': {'without_tax': {'value': 6.95, 'currency': 'USD'}},
                'image': {'data': BASE + '/images/{:size}/' + sku + '.jpg'},
                'instock': color == '1', 'purchasable': True, 'stock': None}}
        return html, responses

    def test_attribute_tree_preserves_only_server_verified_combinations(self):
        html, responses = self.configurable_source()
        product = parse_product(html, URL, responses)
        self.assertEqual([(v['option1'], v['option2']) for v in product['variants']],
                         [('Small', 'Red'), ('Large', 'Blue')])
        self.assertEqual([v['sku'] for v in product['variants']], ['000123', '000456'])
        self.assertFalse(product['variants'][1]['available'])
        self.assertEqual(product['variants'][1]['featured_image']['src'], BASE + '/images/1280x1280/000456.jpg')
        incomplete = copy.deepcopy(responses)
        incomplete.pop(next(reversed(incomplete)))
        with self.assertRaises(CrawlError):
            parse_product(html, URL, incomplete)
        duplicate = copy.deepcopy(responses)
        duplicate[next(reversed(duplicate))]['data']['v3_variant_id'] = 101
        with self.assertRaises(CrawlError):
            parse_product(html, URL, duplicate)
        base_only = copy.deepcopy(responses)
        base_only[next(reversed(base_only))]['data']['base'] = True
        with self.assertRaises(CrawlError):
            parse_product(html, URL, base_only)

    def test_configurable_fetch_replays_response_and_current_public_csrf_headers(self):
        html, responses = self.configurable_source()
        client = FakeClient({URL: html})
        client.local = SimpleNamespace(session=SimpleNamespace(cookies={'XSRF-TOKEN': 'test-session-token'}))
        sent = []
        def get_json(endpoint, headers=None):
            sent.append((endpoint, headers))
            return responses[endpoint]
        client.json = get_json
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sample = fetch_product(client, URL, root / 'evidence')
            verify_source(root, sample)
            self.assertEqual(product_market(root, sample), ('US', 'USD'))
            self.assertEqual(len(sent), 4)
            self.assertEqual(sent[0][1]['X-XSRF-TOKEN'], 'test-session-token')
            evidence = root / sample['source_mapping']['file']
            self.assertNotIn('test-session-token', evidence.read_text(encoding='utf-8'))
            sample['product']['variants'][1]['option2'] = 'Red'
            with self.assertRaises(CrawlError):
                verify_source(root, sample)

    def test_sku_named_galleries_bind_every_image_to_its_variant(self):
        html, responses = self.configurable_source()
        named_galleries = ''.join(
            f'<div class="productView-thumbnails" data-carousel="{sku}">'
            f'<a data-image-gallery-item href="/{sku}-front.jpg"></a>'
            f'<a data-image-gallery-item href="/{sku}-back.jpg"></a></div>'
            for sku in ['000123', '000456'])
        html = html.replace('<img src="/video.jpg">', named_galleries)
        product = parse_product(html, URL, responses)
        sources = {image['src']: image['variant_ids'] for image in product['images']}
        self.assertEqual(sources[BASE + '/000123-back.jpg'], ['000123'])
        self.assertEqual(sources[BASE + '/000456-back.jpg'], ['000456'])

    def test_market_uses_current_storefront_context(self):
        html, responses = self.configurable_source()
        public = 'https://www.kongcompany.com'
        html = html.replace(BASE, public)
        responses = {key.replace(BASE, public): value for key, value in responses.items()}
        url = URL.replace(BASE, public)
        client = FakeClient({url: html})
        client.json = lambda endpoint, headers=None: responses[endpoint]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sample = fetch_product(client, url, root / 'evidence')
            self.assertEqual(product_market(root, sample), ('US', 'USD'))
            path = root / sample['source_mapping']['file']
            raw = json.loads(path.read_text(encoding='utf-8'))
            raw['html'] = raw['html'].replace('\\"US\\"', '\\"CA\\"')
            path.write_text(json.dumps(raw), encoding='utf-8')
            with self.assertRaises(CrawlError):
                product_market(root, sample)

    def test_controlled_menu_keeps_real_non_shopify_paths(self):
        html = '''<nav class="navPages"><ul class="navPages-list"><li>
        <a href="/" data-controls="shop"><span class="menulink">Shop</span></a>
        <div id="shop"><ul class="navPage-subMenu-list">
        <li class="responsive-menu-level-1"><a href="/">Shop</a></li>
        <li><a href="/flours" data-controls="flours">Flours</a>
        <div id="flours"><ul><li class="responsive-menu-level-1">
        <a class="navPage-subMenu-action" href="/flours">Flours</a></li></ul>
        <div class="menu-section"><a class="section-title" href="/flours">Flours</a>
        <a class="navPage-subMenu-action" href="/flours/signature">Signature Flour</a>
        <a class="navPage-subMenu-action" href="/flours?menu-heading">Shop All</a></div>
        <div class="menu-section"><span class="section-title">Flour For</span>
        <a class="navPage-subMenu-action" href="/flours/pasta">Pasta</a></div>
        <a class="navPage-subMenu-action" href="/items/flour">Product</a>
        <a class="navPage-subMenu-action" href="/gifts/gift-cards">Gift Cards</a>
        </div></li></ul></div></li>
        <li><a href="https://www.kingarthurbaking.com/recipes">Recipes</a></li>
        </ul></nav><main><a href="/flours/special">Special</a></main>'''
        categories = navigation(html, BASE, client=None, evidence_dir=None)
        self.assertEqual(categories, {
            'Shop,Shop Flours': BASE + '/flours',
            'Shop,Shop Flours,Shop Flours Signature Flour': BASE + '/flours/signature',
            'Shop,Shop Flours,Shop Flours Flour For Pasta': BASE + '/flours/pasta',
            'special': BASE + '/flours/special',
        })

    def test_listing_follows_pages_in_card_order_and_excludes_recommendations(self):
        url = BASE + '/flours/signature'
        client = FakeClient({url: listing_html([2, 1], '?page=2'),
                             url + '?page=2': listing_html([1, 3])})
        listing = collect_listing(client, url)
        self.assertEqual([p['id'] for p in listing['products']], ['2', '1', '3'])
        self.assertEqual(client.calls, [url, url + '?page=2'])
        self.assertTrue(listing['complete'])
        validate_listing(listing, listing['urls'])
        with self.assertRaises(CrawlError):
            validate_listing(listing, list(reversed(listing['urls'])))
        altered = copy.deepcopy(listing)
        altered['products'][0]['id'] = '999'
        with self.assertRaises(CrawlError):
            validate_listing(altered, altered['urls'])

    def test_unfinished_or_repeated_listing_fails(self):
        url = BASE + '/flours/signature'
        client = FakeClient({url: listing_html([1], '?page=2'),
                             url + '?page=2': listing_html([1])})
        with self.assertRaises(CrawlError):
            collect_listing(client, url)
        value = {'url': url, 'complete': True, 'pages': [
            {'url': url, 'html': listing_html([1], '?page=2')}],
            'urls': [BASE + '/items/item-1'], 'products': []}
        with self.assertRaises(CrawlError):
            validate_listing(value, value['urls'])

    def test_actual_sku_price_stock_and_gallery(self):
        product = parse_product(product_html(), URL)
        self.assertEqual(product['id'], '12')
        variant = product['variants'][0]
        self.assertEqual(variant['id'], '000123')
        self.assertEqual(variant['price'], '6.95')
        self.assertTrue(variant['available'])
        self.assertEqual(variant['inventory_quantity'], '')
        self.assertEqual(variant['source_variant_url'], URL)
        self.assertEqual(product['images'], [{'src': BASE + '/large.jpg'}, {'src': BASE + '/back.jpg'}])
        self.assertNotIn('<a ', product['body_html'])
        self.assertIn('description', product['body_html'])
        out = parse_product(product_html(instock=False), URL)
        self.assertFalse(out['variants'][0]['available'])

    def test_unproven_price_sku_currency_and_option_combinations_fail(self):
        for html in (product_html(sku='999'),
                     product_html(price={'without_tax': {'value': 1, 'currency': 'USD'}}),
                     product_html(price={'without_tax': {'value': 6.95, 'currency': 'CAD'}}),
                     product_html().replace('<div data-product-option-change>',
                                            '<div data-product-option-change><select name="attribute[2]"></select>')):
            with self.subTest(html=html[:30]), self.assertRaises(CrawlError):
                parse_product(html, URL)

    def test_saved_source_is_replayed_and_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sample = fetch_product(FakeClient({URL: product_html()}), URL, root / 'evidence')
            verify_source(root, sample)
            self.assertEqual(product_market(root, sample), ('US', 'USD'))
            sample['product']['variants'][0]['price'] = '1.00'
            with self.assertRaises(CrawlError):
                verify_source(root, sample)
            sample['source_mapping']['file'] = '../outside.html'
            with self.assertRaises(CrawlError):
                verify_source(root, sample)


if __name__ == '__main__':
    unittest.main()
