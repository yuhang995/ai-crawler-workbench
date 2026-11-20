import copy
import html as html_lib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.common import CrawlError
from core.html_parser import parse_soup
from core.magento_storefront import (
    _normalize, collect_listing, fetch_product, navigation, product_market,
    validate_listing, verify_source,
)


def product_html(hyva=False):
    config = {
        'productId': '10',
        'attributes': {
            '1': {'label': 'Color', 'options': [{'id': 'r', 'label': 'Red'}, {'id': 'b', 'label': 'Blue'}]},
            '2': {'label': 'Size', 'options': [{'id': 's', 'label': 'Small'}, {'id': 'l', 'label': 'Large'}]},
        },
        'index': {'11': {'1': 'r', '2': 's'}, '12': {'1': 'b', '2': 'l'}},
        'sku': {'11': 'RED-S', '12': 'BLUE-L'},
        'optionPrices': {key: {'finalPrice': {'amount': price}, 'oldPrice': {'amount': 30}}
                         for key, price in [('11', 20), ('12', 25)]},
        'images': {key: [{'full': f'https://example.com/{key}.jpg', 'type': 'image'}] for key in ('11', '12')},
        'salable': [],
    }
    script = ('<script>initConfigurableOptions("10",' + json.dumps(config) + ', "default")</script>' if hyva
              else '<script type="text/x-magento-init">' + json.dumps({'*': {'swatches': {'jsonConfig': config}}}) + '</script>')
    return ('<body class="region-us page-product-configurable"><meta itemprop="priceCurrency" content="USD">'
            '<h1>Example Shirt</h1><form id="product_addtocart_form"><input name="product" value="10"></form>'
            '<div class="product-description-full"><p>Details <a href="/care">care</a></p></div>' + script + '</body>')


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.visited = []

    def get(self, url, **kwargs):
        self.visited.append(url)
        return SimpleNamespace(url=url, text=self.pages[url])


class MagentoStorefrontTests(unittest.TestCase):
    def test_hyva_simple_product_uses_form_sku_and_price_box(self):
        source = '''<body class="region-us catalog-product-view">
        <meta property="product:price:currency" content="USD">
        <h1>Care Kit</h1><form id="product_addtocart_form" data-product-sku="CARE-15">
        <input name="product" value="23069"><span data-price-type="finalPrice"
        data-price-amount="14.95"></span></form>
        <img data-index="0" src="https://example.com/media/catalog/product/care.jpg">
        </body>'''
        product = _normalize({'url': 'https://example.com/care-kit', 'html': source})
        self.assertEqual(product['variants'][0]['sku'], 'CARE-15')
        self.assertEqual(product['variants'][0]['price'], '14.95')
        self.assertEqual(product['images'][0]['src'],
                         'https://example.com/media/catalog/product/care.jpg')

    def test_data_attribute_config_four_dimensions_and_missing_images(self):
        soup = parse_soup(product_html())
        config = json.loads(soup.find('script').string)['*']['swatches']['jsonConfig']
        config['attributes']['3'] = {'label': 'Width', 'options': [{'id': 'w', 'label': 'Wide'}]}
        config['attributes']['4'] = {'label': 'Calf', 'options': [{'id': 'c', 'label': 'Regular'}]}
        for values in config['index'].values():
            values.update({'3': 'w', '4': 'c'})
        config['images'] = []
        config['color_variants'] = {'r': {'gallery': [{'full': 'https://example.com/red.jpg'}]}}
        config['in_stock_products'] = {'11': {'qty': 0}, '12': {'qty': 3}}
        soup.find('script').decompose()
        config_node = '<div data-product-id="10" data-config="' + html_lib.escape(json.dumps(config), quote=True) + '"></div>'
        source = str(soup).replace('</body>', config_node + '</body>')
        result = _normalize({'url': 'https://example.com/shoe', 'html': source})
        self.assertEqual(len(result['variants']), 2)
        self.assertEqual(result['options'][-1]['name'], 'Width / Calf')
        self.assertEqual(result['variants'][1]['option3'], 'Wide / Regular')
        self.assertEqual(result['variants'][1]['source_options']['Calf'], 'Regular')
        self.assertEqual(result['missing_variant_images'], ['12'])
        self.assertNotIn('featured_image', result['variants'][1])
        self.assertEqual(result['images'][0]['variant_ids'], ['11'])
        self.assertFalse(result['variants'][0]['available'])
        self.assertEqual(result['variants'][1]['inventory_quantity'], 3)

    def test_simple_product_uses_its_bound_gallery_and_source_replay(self):
        source = '''<body class="region-us catalog-product-view">
        <h1>Headphones</h1><form id="product_addtocart_form"><input name="product" value="51"></form>
        <meta itemprop="sku" content="HEADPHONE-51"><meta itemprop="price" content="99">
        <meta itemprop="priceCurrency" content="USD"><meta itemprop="availability" content="https://schema.org/InStock">
        <div id="gallery" x-data="initGallery"></div><div itemprop="description">Clear sound <a href="/help">details</a>.</div>
        <script>function initGallery() { return { images: [{"full":"https://example.com/front.jpg","type":"image"},
            {"full":"https://example.com/back.jpg","type":"image"}, {"full":"https://example.com/video.jpg","type":"video"}] }; }</script></body>'''
        with tempfile.TemporaryDirectory() as directory:
            sample = fetch_product(FakeClient({'https://example.com/headphones': source}),
                                   'https://example.com/headphones', Path(directory) / 'evidence')
            self.assertEqual(sample['product']['variants'][0]['sku'], 'HEADPHONE-51')
            self.assertEqual(len(sample['product']['images']), 2)
            self.assertEqual(sample['product']['options'], [])
            verify_source(directory, sample)
            sample['product']['variants'][0]['price'] = '1'
            with self.assertRaises(CrawlError):
                verify_source(directory, sample)

    def test_dynamic_menu_get_preserves_group_ownership_and_evidence(self):
        source = '''<nav id="replaceNav" class="navigation"><ul class="nav__list">
        <li><a href="/en-us/headphones">Headphones</a></li></ul></nav>
        <script>const BASE_URL = 'https://example.com/en-us/';
        fetch(BASE_URL + '/menu/menu/index').then(data => { document.getElementById('replaceNav').innerHTML = data; });</script>'''
        menu = '''<ul class="nav__list"><li><a href="/en-us/headphones">Headphones</a>
        <div class="nav__parent-container"><span class="nav__sub-title">Best For</span>
        <div><a class="nav__inner-link" href="/en-us/headphones/travel">Travel</a></div></div>
        <a class="nav__inner-link" href="/en-us/headphones/all">Browse All Headphones</a>
        <a class="nav__product-container" href="/en-us/featured-headphone">Featured</a></li></ul>'''
        grid = '<div class="products-grid"><a class="product-item-link" href="/one">One</a></div>'
        client = FakeClient({'https://example.com/en-us/menu/menu/index': menu,
                             'https://example.com/en-us/headphones/travel': grid,
                             'https://example.com/en-us/headphones/all': grid})
        with tempfile.TemporaryDirectory() as directory:
            result = navigation(source, 'https://example.com/', client=client, evidence_dir=directory)
            self.assertEqual(result, {'Headphones': 'https://example.com/en-us/headphones',
                                     'Headphones,Headphones All Headphones': 'https://example.com/en-us/headphones/all',
                                     'Headphones,Headphones Best For,Headphones Best For Travel': 'https://example.com/en-us/headphones/travel'})
            self.assertEqual(len(list(Path(directory).glob('home-menu-native-*.html'))), 1)
            self.assertIn('/en-us/headphones/travel', next(Path(directory).glob('home-menu-native-*.html')).read_text(encoding='utf-8'))

    def test_tabbed_menu_preserves_root_and_nested_groups_and_verifies_untyped_links(self):
        source = '''<nav class="navigation"><ul class="nav__list"><li><a href="/microphones">Microphones</a>
        <div class="nav__parent-container"><a class="nav__inner-link nav__inner-link--parent" href="/microphones/wired">Wired</a>
        <div class="nav__parent-container"><span class="nav__sub-title">Best For</span>
        <a class="nav__inner-link" data-url="vocals" href="/microphones/wired/vocals">Vocals</a></div>
        <a class="nav__inner-link" href="/microphones/wired/all">Browse All Microphones</a></div>
        <div class="nav__parent-container"><a class="nav__inner-link nav__inner-link--parent" href="/microphones/wireless">Wireless</a>
        <a class="nav__inner-link" href="/microphones/wireless">Browse All Wireless Systems</a></div>
        <div class="nav__parent-container"><span class="nav__sub-title">More</span>
        <a class="nav__inner-link" href="/technical-help">Technical Help</a>
        <a class="nav__inner-link" href="/featured-mic">Featured Mic</a></div>
        </li></ul></nav>'''
        grid = '<div class="products-grid"><a class="product-item-link" href="/one">One</a></div>'
        client = FakeClient({'https://example.com/microphones/wired/all': grid,
                             'https://example.com/microphones/wireless': grid,
                             'https://example.com/technical-help': '<article>Support</article>',
                             'https://example.com/featured-mic': grid + '<form id="product_addtocart_form"></form>'})
        result = navigation(source, 'https://example.com/', client=client)
        self.assertEqual(result, {
            'Microphones': 'https://example.com/microphones',
            'Microphones,Microphones Wired': 'https://example.com/microphones/wired',
            'Microphones,Microphones Wired,Microphones Wired Best For Vocals': 'https://example.com/microphones/wired/vocals',
            'Microphones,Microphones Wired,Microphones Wired All Microphones': 'https://example.com/microphones/wired/all',
            'Microphones,Microphones Wireless': 'https://example.com/microphones/wireless'})

    def test_div_product_link_cards_and_source_saved_before_parse_failure(self):
        url = 'https://example.com/list'
        source = '''<div class="products-grid"><div class="product__link">
        <a class="product__image-link" href="/first">First</a><a href="/first">Buy</a>
        </div><div class="product__link"><a class="product__image-link" href="/second">Second</a></div></div>'''
        listing = collect_listing(FakeClient({url: source}), url)
        self.assertEqual(listing['urls'], ['https://example.com/first', 'https://example.com/second'])
        validate_listing(listing, listing['urls'])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CrawlError):
                fetch_product(FakeClient({url: '<html>Missing product</html>'}), url, Path(directory) / 'evidence')
            saved = list((Path(directory) / 'evidence').glob('magento-*.json'))
            self.assertEqual(len(saved), 1)
            self.assertIn('Missing product', saved[0].read_text(encoding='utf-8'))

    def test_both_public_configs_preserve_actual_combinations(self):
        for hyva in (False, True):
            product = _normalize({'url': 'https://example.com/shirt', 'html': product_html(hyva)})
            self.assertEqual(len(product['variants']), 2)
            self.assertEqual([(v['option1'], v['option2']) for v in product['variants']], [('Red', 'Small'), ('Blue', 'Large')])
            self.assertEqual(product['variants'][1]['price'], '25')
            self.assertNotIn('available', product['variants'][0])
            self.assertEqual(product['images'][0]['variant_ids'], ['11'])
            self.assertNotIn('<a', product['body_html'])

    def test_product_overview_is_a_description_source(self):
        source = product_html().replace('class="product-description-full"', 'class="product attribute overview"')
        source = source.replace('<p>Details', '<div class="value"><p>Details').replace('</p></div>', '</p></div></div>')
        result = _normalize({'url': 'https://example.com/shirt', 'html': source})
        self.assertIn('Details care', parse_soup(result['body_html']).get_text(' ', strip=True))
        self.assertNotIn('<a', result['body_html'])

    def test_cms_category_product_cards_keep_order_and_exclude_related_cards(self):
        url = 'https://example.com/live-music'
        cards = '<div class="banner__products"><div class="product__link"><a class="product__image-link" href="/one">One</a></div></div>'
        html = '<main>' + cards + cards.replace('/one', '/two') + '<div class="related">' + cards.replace('/one', '/other') + '</div></main>'
        listing = collect_listing(FakeClient({url: html}), url)
        self.assertEqual(listing['urls'], ['https://example.com/one', 'https://example.com/two'])
        validate_listing(listing, listing['urls'])

    def test_cms_parent_with_real_category_links_is_kept(self):
        html = '''<nav class="main navigation"><ul class="main__list"><li>
        <a class="main__link" href="/womens-shoes">Women</a><ul>
        <li><a class="main__inner-link" href="/women/sandals.html">Sandals</a></li>
        <li><a class="main__inner-link" href="/women/boots.html?icid=nav">Boots</a></li>
        <li><a class="main__inner-link" href="/research">Research</a></li></ul></li></ul></nav>
        <main><a href="/women/boots.html">Boots</a></main>'''
        page = '<main><a href="/women/sandals.html">Shop Sandals</a><a href="/women/boots.html">Shop Boots</a></main>'
        editorial = page.replace('<main>', '<main><div class="seo">').replace('</main>', '</div></main>')
        result = navigation(html, 'https://example.com/', client=FakeClient({
            'https://example.com/womens-shoes': page,'https://example.com/research': editorial}))
        self.assertEqual(result['Women'], 'https://example.com/womens-shoes')
        self.assertNotIn('Women,Women Research', result)
        self.assertNotIn('boots', result)

    def test_missing_config_cannot_be_misrepresented_as_simple(self):
        source = '<body class="region-us"><meta itemprop="priceCurrency" content="USD"><input name="product" value="10"></body>'
        with self.assertRaises(CrawlError):
            _normalize({'url': 'https://example.com/shirt', 'html': source})

    def test_source_verifies_price_identity_and_market(self):
        url = 'https://example.com/shirt'
        with tempfile.TemporaryDirectory() as directory:
            sample = fetch_product(FakeClient({url: product_html()}), url, Path(directory) / 'evidence')
            verify_source(directory, sample)
            self.assertEqual(product_market(directory, sample), ('US', 'USD'))
            corrupted = copy.deepcopy(sample)
            corrupted['product']['variants'][0]['price'] = '1'
            with self.assertRaises(CrawlError):
                verify_source(directory, corrupted)

    def test_market_interface_matches_registry_consumer(self):
        from core.custom_site import source_adapter

        url = 'https://example.com/shirt'
        with tempfile.TemporaryDirectory() as directory:
            sample = fetch_product(FakeClient({url: product_html()}), url, Path(directory) / 'evidence')
            adapter = source_adapter(sample['source_mapping']['kind'])
            country, currency = adapter.product_market(directory, sample)
            self.assertEqual(country, 'US')
            self.assertEqual(currency, 'USD')

    def test_grid_follows_real_pagination_excluding_recommendations(self):
        first, second = 'https://example.com/shop', 'https://example.com/shop?p=2'
        def grid(path):
            return '<div class="products-grid"><a class="product-item-link" href="' + path + '">Product</a></div>'
        pages = {first: grid('/one') + '<div class="related">' + grid('/recommendation') + '</div><div class="pages"><a class="next" href="?p=2">Next</a></div>', second: grid('/two')}
        client = FakeClient(pages)
        listing = collect_listing(client, first)
        self.assertEqual(listing['urls'], ['https://example.com/one', 'https://example.com/two'])
        self.assertEqual(client.visited, [first, second])
        validate_listing(listing, listing['urls'])
        listing['pages'].pop()
        with self.assertRaises(CrawlError):
            validate_listing(listing, listing['urls'])

    def test_navigation_keeps_parent_ownership_and_ignores_utility_groups(self):
        html = '''<ul id="primaryNav"><li><a href="/mens.html">Men</a>
        <div class="dropdown-section"><a class="section-heading">Pants</a>
        <ul class="sub-menu"><li><a href="/mens/cargo.html">Cargo</a></li></ul></div></li>
        <li><a href="#">Support</a><div class="dropdown-section"><a class="section-heading" href="/help">Help</a></div></li></ul>'''
        self.assertEqual(navigation(html, 'https://example.com/'), {
            'Men': 'https://example.com/mens.html', 'Men,Men Pants,Men Pants Cargo': 'https://example.com/mens/cargo.html'})

    def test_homepage_categories_require_real_grids_and_keep_query_filters(self):
        base = 'https://example.com/'
        html = '''<ul id="primaryNav"><li><a href="/mens.html">Men</a></li></ul>
        <main><a href="/mens.html">Men again</a><a href="/promotions/weekly.html">Weekly</a>
        <a href="/series.html?gender=1%2C2">For him</a><a href="/series.html?gender=3">For her</a>
        <a href="/product.html">Featured product</a><a href="/account">Account</a>
        <div class="product-item"><a href="/recommendation.html">Recommended product</a></div>
        <div class="widget-product-carousel"><a href="/carousel-product.html">Carousel product</a></div></main>'''
        grid = '<body class="catalog-category-view"><div class="products-grid"><a class="product-item-link" href="/one.html">Product</a></div></body>'
        pages = {base + path: grid for path in ['promotions/weekly.html', 'series.html?gender=1%2C2', 'series.html?gender=3']}
        pages[base + 'product.html'] = grid.replace('catalog-category-view', 'catalog-product-view')
        client = FakeClient(pages)
        result = navigation(html, base, client=client)
        self.assertEqual(result, {
            'Men': base + 'mens.html', 'weekly': base + 'promotions/weekly.html',
            'series?gender=1%2C2': base + 'series.html?gender=1%2C2',
            'series?gender=3': base + 'series.html?gender=3',
        })
        self.assertCountEqual(client.visited, list(pages))
        self.assertEqual(navigation(html, base), {'Men': base + 'mens.html'})


if __name__ == '__main__':
    unittest.main()
