import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.common import AdapterRequired, CrawlError
from core.pinia_storefront import (
    collect_listing, fetch_product, navigation, normalize, product_market,
    state_from_html, validate_listing, verify_source,
)


BASE = 'https://store.example'
PRODUCT_URL = BASE + '/training-bar'


def state_html(state, body='', country='US'):
    table = []

    def encode(value):
        if value is None:
            return -1
        index = len(table)
        table.append(None)
        if isinstance(value, dict):
            table[index] = {key: encode(item) for key, item in value.items()}
        elif isinstance(value, list):
            table[index] = [encode(item) for item in value]
        else:
            table[index] = value
        return index

    encode(state)
    initial = json.dumps({'pinia': json.dumps(table)})
    market = json.dumps({'l10n': {'country': country, 'currency': 'USD'}})
    return ('<html><body>' + body + '<script>window.__INITIAL_STATE__ = ' + initial
            + ';</script><script>window.config = ' + market + ';</script></body></html>')


def product_state():
    def image(path):
        return {'type': 'image', 'isInGallery': True, 'link': path}

    return {'products': {
        'byUrl': {'/training-bar': 10},
        'products': {
            '10': {'productId': 10, 'name': 'Training Bar', 'url': '/training-bar',
                   'type': 'grouped', 'childrenIds': [20], 'relatedIds': [99],
                   'attributes': {'manufacturer': 'Example'}, 'gallery': [image('/shared.jpg')],
                   'roguebuilder': {'slots': {'tabs': [
                       {'name': 'Block', 'props': {'tab': 'Product Description'},
                        'slots': {'default': [{'name': 'html', 'value': '<p>Steel <a href="/care">care</a>.</p>'}]}}
                   ]}}},
            '20': {'productId': 20, 'name': 'Finishes', 'type': 'configurable',
                   'childrenIds': [31, 32], 'relatedIds': [99],
                   'configurableAttributes': [{'code': 'finish', 'title': 'Finish'}]},
            '31': {'productId': 31, 'sku': 'BAR-RED', 'name': 'Red bar', 'type': 'simple',
                   'price': 20, 'special_price': 15, 'attributes': {'finish': 'Red'},
                   'gallery': [image('/red.jpg')]},
            '32': {'productId': 32, 'sku': 'BAR-BLUE', 'name': 'Blue bar', 'type': 'simple',
                   'price': 30, 'special_price': None, 'attributes': {'finish': 'Blue'},
                   'gallery': [image('/blue.jpg')]},
            '99': {'productId': 99, 'sku': 'RELATED', 'name': 'Unrelated bar', 'type': 'simple',
                   'price': 999, 'attributes': {'finish': 'Other'},
                   'gallery': [image('/unrelated.jpg')]},
        },
    }}


def product_html(state=None, country='US'):
    return state_html(state or product_state(),
                      '<img src="https://cdn.example/w_1500/shared.jpg">', country)


def listing_html(paths, total, next_page=''):
    state = {
        'categories': {'categories': {'10': {'categoryId': 10, 'url': '/bars'}}},
        'http': {'cache': {'current': {
            'cnstrc_request': {'browse_filter_name': 'group_id', 'browse_filter_value': '10'},
            'content': {'product': {'value': [{'url': path} for path in paths], 'total_item': total}},
        }, 'unrelated': {
            'cnstrc_request': {'browse_filter_name': 'group_id', 'browse_filter_value': '99'},
            'content': {'product': {'value': [{'url': '/recommendation'}], 'total_item': 1}},
        }}},
    }
    cards = '<div class="products-wrapper">' + ''.join(
        '<a class="hover-card" href="' + path + '">Product</a>' for path in paths) + '</div>'
    cards += '<aside><a class="hover-card" href="/recommendation">Recommended</a></aside>'
    if next_page:
        cards += '<a rel="next" href="' + next_page + '">Next</a>'
    return state_html(state, cards)


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return SimpleNamespace(url=url, text=self.pages[url])


class PiniaStorefrontTests(unittest.TestCase):
    def test_flattened_references_preserve_booleans_and_numeric_literals(self):
        table = [{'enabled': True, 'disabled': False, 'count': 1, 'debt': 2,
                  'missing': -1, 'self': 0}, 0, -7]
        html = '<script>window.__INITIAL_STATE__ = ' + json.dumps({'pinia': json.dumps(table)}) + ';</script>'
        restored = state_from_html(html)
        self.assertIs(restored['enabled'], True)
        self.assertIs(restored['disabled'], False)
        self.assertEqual(restored['count'], 0)
        self.assertEqual(restored['debt'], -7)
        self.assertIsNone(restored['missing'])
        self.assertIs(restored['self'], restored)
        invalid = '<script>window.__INITIAL_STATE__ = ' + json.dumps({'pinia': '[{"bad": 8}]'}) + ';</script>'
        with self.assertRaises(AdapterRequired):
            state_from_html(invalid)

    def test_only_declared_children_define_skus_and_variant_images(self):
        product = normalize(product_html(), PRODUCT_URL)
        self.assertEqual([variant['sku'] for variant in product['variants']], ['BAR-RED', 'BAR-BLUE'])
        self.assertEqual([variant['option1'] for variant in product['variants']], ['Red', 'Blue'])
        self.assertEqual(product['variants'][0]['price'], '15.00')
        self.assertEqual(product['variants'][0]['compare_at_price'], '20.00')
        self.assertIsNone(product['variants'][1]['compare_at_price'])
        self.assertNotIn('available', product['variants'][0])
        images = {image['src']: image['variant_ids'] for image in product['images']}
        self.assertEqual(images['https://cdn.example/w_1500/red.jpg'], ['31'])
        self.assertEqual(images['https://cdn.example/w_1500/blue.jpg'], ['32'])
        self.assertNotIn('https://cdn.example/w_1500/unrelated.jpg', images)
        self.assertIn('care', product['body_html'])
        self.assertNotIn('<a', product['body_html'])

    def test_missing_declared_child_cannot_be_silently_truncated(self):
        state = product_state()
        del state['products']['products']['32']
        with self.assertRaises(AdapterRequired):
            normalize(product_html(state), PRODUCT_URL)

    def test_description_uses_product_tab_and_excludes_sku_scoped_blocks(self):
        state = product_state()
        tabs = state['products']['products']['10']['roguebuilder']['slots']['tabs']
        tabs[0]['slots']['default'].append({
            'name': 'Block', 'skus': ['BAR-RED'],
            'slots': {'default': [{'name': 'html', 'value': '<p>Red-only description.</p>'}]},
        })
        tabs.extend([
            {'name': 'Block', 'props': {'tab': 'Shipping'},
             'slots': {'default': [{'name': 'html', 'value': '<p>Shipping policy.</p>'}]}},
            {'name': 'Block', 'props': {'tab': 'Gear Specs'},
             'slots': {'default': [{'name': 'html', 'value': '<p>Technical table.</p>'}]}},
        ])
        product = normalize(product_html(state), PRODUCT_URL)
        self.assertIn('Steel', product['body_html'])
        self.assertIn('care', product['body_html'])
        self.assertNotIn('Red-only', product['body_html'])
        self.assertNotIn('Shipping policy', product['body_html'])
        self.assertNotIn('Technical table', product['body_html'])

    def test_source_replay_rejects_variant_price_identity_and_image_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = fetch_product(FakeClient({PRODUCT_URL: product_html()}), PRODUCT_URL, root / 'evidence')
            verify_source(root, sample)
            self.assertEqual(product_market(root, sample), ('US', 'USD'))
            for field, value in [('price', '0.01'), ('id', '99'), ('option1', 'Other')]:
                tampered = copy.deepcopy(sample)
                tampered['product']['variants'][0][field] = value
                with self.subTest(field=field), self.assertRaises(CrawlError):
                    verify_source(root, tampered)
            tampered = copy.deepcopy(sample)
            tampered['product']['images'][1]['variant_ids'] = ['32']
            with self.assertRaises(CrawlError):
                verify_source(root, tampered)
        with self.assertRaises(CrawlError):
            normalize(product_html(country='CA'), PRODUCT_URL)

    def test_listing_matches_active_category_and_follows_all_pages(self):
        first, second = BASE + '/bars', BASE + '/bars?page=2'
        client = FakeClient({first: listing_html(['/one', '/two'], 3, '?page=2'),
                             second: listing_html(['/three'], 3)})
        listing = collect_listing(client, first)
        expected = [BASE + value for value in ['/one', '/two', '/three']]
        self.assertEqual(client.calls, [first, second])
        self.assertEqual(listing['urls'], expected)
        validate_listing(listing, expected)
        listing['pages'].pop()
        with self.assertRaises(CrawlError):
            validate_listing(listing, expected)

    def test_listing_cannot_claim_completion_without_next_page_or_matching_cards(self):
        url = BASE + '/bars'
        with self.assertRaises(AdapterRequired):
            collect_listing(FakeClient({url: listing_html(['/one', '/two'], 3)}), url)
        mismatched = listing_html(['/one'], 1).replace('href="/one"', 'href="/other"')
        with self.assertRaises(AdapterRequired):
            collect_listing(FakeClient({url: mismatched}), url)

    def test_listing_supports_alternate_search_cache_and_identical_duplicates(self):
        url = BASE + '/bars'
        original = listing_html(['/one', '/two'], 2)
        state = state_from_html(original)
        cache = state['http']['cache']
        alternate = copy.deepcopy(cache['current'])
        del alternate['cnstrc_request']
        cache['alternate_search'] = alternate
        cache.pop('current')
        cards = '<div class="products-wrapper"><a class="hover-card" href="/one">One</a><a class="hover-card" href="/two">Two</a></div>'
        expected = [BASE + '/one', BASE + '/two']
        result = collect_listing(FakeClient({url: state_html(state, cards)}), url)
        self.assertEqual(result['urls'], expected)
        cache['duplicate'] = copy.deepcopy(alternate)
        result = collect_listing(FakeClient({url: state_html(state, cards)}), url)
        validate_listing(result, expected)
        cache['duplicate']['content']['product']['total_item'] = 3
        with self.assertRaises(AdapterRequired):
            collect_listing(FakeClient({url: state_html(state, cards)}), url)

    def test_navigation_keeps_declared_ancestry_and_requires_real_anchor(self):
        state = {'categories': {'menuItems': [
            {'id': 1, 'name': 'Strength', 'url': '/strength', 'parent': None},
            {'id': 2, 'name': 'Bars', 'url': '/bars', 'parent': 1},
            {'id': 3, 'name': 'Ghost', 'url': '/ghost', 'parent': 1},
        ]}, 'ui': {'topNavLinks': []}}
        html = state_html(state, '<header><a href="/strength">Strength</a><a href="/bars">Bars</a></header>')
        self.assertEqual(navigation(html, BASE), {
            'Strength': BASE + '/strength', 'Strength,Strength Bars': BASE + '/bars'})

    def test_navigation_adds_verified_homepage_categories_and_preserves_filter_queries(self):
        state = {'categories': {'menuItems': [
            {'id': 1, 'name': 'Strength', 'url': '/strength', 'parent': None},
        ]}}
        body = ('<header><a href="/strength">Strength</a></header><div id="main-content">'
                '<div class="cms-row"><div class="call-to-action"><h2>Sweatshirts</h2>'
                '<a href="/gear/sweatshirts"></a><a href="/gear/t-shirts">Shop Now</a></div>'
                '<a href="/deals?deal_type=sale">Sale</a>'
                '<a href="/deals?deal_type=bulk">Bulk</a>'
                '<a href="/strength?utm_campaign=homepage">Strength</a>'
                '<div class="product-card"><a href="/featured-bar">Featured Bar</a></div>'
                '<a href="/contact-us">Contact</a><a href="https://other.example/racks">Other</a>'
                '</div></div>')
        urls = ['/gear/sweatshirts', '/gear/t-shirts', '/deals?deal_type=sale', '/deals?deal_type=bulk']
        pages = {}
        for index, path in enumerate(urls):
            category_path = path.split('?', 1)[0]
            pages[BASE + path] = state_html({'categories': {
                'byUrl': {category_path: index},
                'categories': {str(index): {'categoryId': index, 'url': category_path,
                                            'isActive': True, 'isExcluded': True}},
            }})
        client = FakeClient(pages)
        with tempfile.TemporaryDirectory() as directory:
            categories = navigation(state_html(state, body), BASE, client=client, evidence_dir=directory)
            self.assertEqual(categories, {
                'Strength': BASE + '/strength',
                'sweatshirts': BASE + '/gear/sweatshirts',
                't-shirts': BASE + '/gear/t-shirts',
                'deals?deal_type=sale': BASE + '/deals?deal_type=sale',
                'deals?deal_type=bulk': BASE + '/deals?deal_type=bulk',
            })
            self.assertEqual(client.calls, list(pages))
            self.assertEqual(len(list(Path(directory).glob('home-category-*.html'))), 4)
            audit = json.loads((Path(directory) / 'pinia_homepage_audit.json').read_text(encoding='utf-8'))
            self.assertTrue(all(item['status'] == 'included' for item in audit['candidates']))

    def test_homepage_links_require_target_category_identity_and_real_href(self):
        state = {'categories': {'menuItems': [
            {'id': 1, 'name': 'Strength', 'url': '/strength', 'parent': None},
        ]}, 'cms': {'link': '/invented-series'}}
        body = ('<header><a href="/strength">Strength</a></header><main>'
                '<a href="/training-bar">Product</a><a href="/event">Learn More</a>'
                '<a href="/wrong-category">Bad identity</a><a href="/news/update">News</a></main>')
        pages = {
            BASE + '/training-bar': product_html(),
            BASE + '/event': state_html({'cms': {'name': 'Event'}}),
            BASE + '/wrong-category': state_html({'categories': {
                'byUrl': {'/wrong-category': 9},
                'categories': {'8': {'categoryId': 8, 'url': '/wrong-category'}},
            }}),
        }
        client = FakeClient(pages)
        result = navigation(state_html(state, body), BASE, client=client)
        self.assertEqual(result, {'Strength': BASE + '/strength'})
        self.assertEqual(client.calls, list(pages))


if __name__ == '__main__':
    unittest.main()
