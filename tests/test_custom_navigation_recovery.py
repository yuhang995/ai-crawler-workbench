import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.common import AdapterRequired
from core.custom_navigation import (
    audit_navigation, catalog_url, detect_catalog_only, navigation_source,
    recover_navigation,
)
from core.custom_site import run_navigation
from core.simple_nav import PROMPT, SELF_HEAL_PROMPT


BASE = 'https://store.example/'


def menu_html(count):
    links = ''.join(
        f'<a href="/dog/toys/type-{index}/">Type {index}</a>'
        for index in range(count)
    )
    return ('<header><nav><button data-collapsible="dog-panel">Dog</button>'
            '<div id="dog-panel">' + links + '</div></nav></header>')


def categories_for(count):
    return {f'Dog,Dog Type {index}': BASE + f'dog/toys/type-{index}/'
            for index in range(count)}


def fake_cli(categories, processes):
    def launch(command, **kwargs):
        output = Path(command[command.index('-o') + 1])
        output.write_text(json.dumps({'entries': [
            {'key': key, 'url': url} for key, url in categories.items()
        ]}), encoding='utf-8')
        process = MagicMock(returncode=0)
        processes.append(process)
        return process
    return launch


class CustomNavigationRecoveryTests(unittest.TestCase):
    def test_custom_policy_keeps_catalog_paths_and_filters_non_catalog_urls(self):
        allowed = ['dog/toys/', 'weightlifting/bars', 'baking-pans.html',
                   'shop?cgid=equipment', 'c/kitchen/', 'collections/bowls',
                   'products/']
        blocked = ['account', 'account/login', 'products/toy', 'product/toy.html',
                   'cart', 'checkout', 'pages/about', 'gift-card', 'giftcards/',
                   'category/gift_card', 'warranty', 'dog/toys/#mobile']
        blocked.extend(['about-us/', 'contact-us/', 'find-a-retailer/', 'where-to-buy/',
                        'careers/', 'press/', 'news/', 'faq/'])
        for path in allowed:
            with self.subTest(path=path):
                self.assertTrue(catalog_url(BASE + path, BASE))
        for path in blocked:
            with self.subTest(path=path):
                self.assertFalse(catalog_url(BASE + path, BASE))
        self.assertFalse(catalog_url('https://other.example/dog/toys/', BASE))
        self.assertFalse(catalog_url('https://user@store.example/dog/toys/', BASE))

    def test_catalog_only_requires_three_products_and_no_commerce_evidence(self):
        listing = BASE + 'products/'
        links = [BASE + f'product/item-{index}/' for index in range(1, 4)]
        listing_html = '<main>' + ''.join(
            f'<a href="{url}">Item</a>' for url in links) + '</main>'

        class Client:
            def __init__(self, priced=False, small=False):
                self.priced = priced
                self.small = small
            def get(self, url, **kwargs):
                if url == listing:
                    html = listing_html.replace(links[2], '/about/') if self.small else listing_html
                    return SimpleNamespace(url=url, text=html)
                price = '<meta itemprop="price" content="12.00">' if self.priced else ''
                return SimpleNamespace(url=url, text='<main><h1>Item</h1>' + price + '</main>')

        with tempfile.TemporaryDirectory() as directory:
            result = detect_catalog_only(Client(), BASE, {'Products': listing}, directory)
            self.assertTrue(result['catalog_only'])
            self.assertEqual(result['product_links'], links)
            self.assertEqual(len(list(Path(directory).glob('catalog-only-product-*.html'))), 3)
        self.assertFalse(detect_catalog_only(
            Client(priced=True), BASE, {'Products': listing})['catalog_only'])
        self.assertFalse(detect_catalog_only(
            Client(small=True), BASE, {'Products': listing})['catalog_only'])

    def test_gatsby_page_data_recovers_js_only_product_grid(self):
        listing = BASE + 'our-teas/'
        products = [BASE + f'our-teas/item-{index}/' for index in range(1, 4)]

        class Response(SimpleNamespace):
            def json(self):
                return self.payload

        class Client:
            def get(self, url, **kwargs):
                if '/page-data/' in url:
                    return Response(url=url, text='', payload={'result': {'products': [
                        {'slug': f'our-teas/item-{index}/', 'productId': str(index)}
                        for index in range(1, 4)]}})
                if url == listing:
                    return Response(url=url, text='<meta name="generator" content="Gatsby 4">')
                return Response(url=url, text='<main><h1>Tea</h1></main>')

        result = detect_catalog_only(Client(), BASE, {'Our Teas': listing})
        self.assertTrue(result['catalog_only'])
        self.assertEqual(result['product_links'], products)
        self.assertEqual(result['listing_source'], 'gatsby_page_data')

    def test_ai_instructions_distinguish_product_index_from_product_details(self):
        self.assertIn('精确的 /products/ 商品中心首页', PROMPT)
        self.assertIn('精确 /products/', SELF_HEAL_PROMPT)
        self.assertIn('/products/<slug>', PROMPT)

    def test_raw_source_preserves_panel_relationships_and_real_custom_links(self):
        html = (menu_html(2) + '<main><a href="/unrelated/">Other</a></main>'
                '<footer><a href="/footer-category/">Footer</a></footer>')
        source = navigation_source(html, BASE)
        self.assertEqual(source['url_policy'], 'custom')
        self.assertEqual(source['valid_urls'], list(categories_for(2).values()))
        self.assertIn('data-collapsible="dog-panel"', source['navigation_html'])
        self.assertIn('id="dog-panel"', source['navigation_html'])
        self.assertNotIn('/unrelated/', source['navigation_html'])
        self.assertNotIn('/footer-category/', source['navigation_html'])

    def test_empty_candidate_fails_even_when_source_contains_no_catalog_links(self):
        for html, expected in [(menu_html(2), 2), ('<header>Menu</header>', 0)]:
            with self.subTest(source_urls=expected):
                audit = audit_navigation({}, html, BASE)
                self.assertFalse(audit['passed'])
                self.assertEqual(audit['source_urls'], expected)
                self.assertEqual(audit['matched_urls'], 0)
                self.assertEqual(audit['ratio'], 0)

    def test_semantic_desktop_nav_outside_utility_header_is_the_audit_source(self):
        html = ('<header><a href="/delivery-methods.html">Delivery</a>'
                '<a href="/account/">Account</a></header>'
                '<nav class="hide-for-xlarge"><a href="/kids/">Kids</a></nav>'
                '<nav class="show-for-xlarge"><a href="/women/">Women</a>'
                '<a href="/men/">Men</a></nav>')
        source = navigation_source(html, BASE)
        self.assertEqual(set(source['valid_urls']), {BASE + 'women/', BASE + 'men/'})
        audit = audit_navigation({'Women': BASE + 'women/', 'Men': BASE + 'men/'}, html, BASE)
        self.assertTrue(audit['passed'])
        self.assertEqual(audit['matched_urls'], 2)

    def test_drawer_only_navigation_and_external_controlled_panel_are_retained(self):
        html = ('<nav class="mobile-drawer"><a href="/women/">Women</a>'
                '<button aria-controls="men-panel">Men</button>'
                '<button aria-controls="store-switcher">Country</button></nav>'
                '<div id="men-panel"><a href="/men/shirts/">Shirts</a></div>'
                '<div id="store-switcher"><a href="/en-ca/">Canada</a></div>')
        source = navigation_source(html, BASE)
        self.assertEqual(set(source['valid_urls']), {BASE + 'women/', BASE + 'men/shirts/'})
        self.assertIn('id="men-panel"', source['navigation_html'])

    def test_header_menu_list_is_retained_when_nav_tags_are_utility_menus(self):
        html = ('<header><nav id="megamenu-mobile-drawer" aria-label="Main navigation">'
                '<ul class="c-mega-nav__list"><li><a href="/bottles/">Bottles</a>'
                '<ul><li><a href="/bottles/kids/">Kids</a></li></ul></li></ul></nav>'
                '<nav aria-label="Utility navigation"><a href="/contact/">Contact</a></nav>'
                '<nav class="header-customer-menu-nav"><a href="/rewards/">Rewards</a></nav></header>')
        source = navigation_source(html, BASE)
        self.assertEqual(set(source['valid_urls']), {BASE + 'bottles/', BASE + 'bottles/kids/'})

    def test_explicit_menu_links_exclude_sibling_product_cards_and_tracking_duplicates(self):
        html = ('<nav><ul class="main__list"><li>'
                '<a data-menu="women" href="/women.html?icid=nav">Women</a>'
                '<a data-menu="women-again" href="/women.html?icid=column">All women</a>'
                '<a data-menu="wide" href="/women.html?width=wide&amp;icid=nav">Wide</a>'
                '<div class="card"><a href="/a-single-shoe.html?color=red">Shoe</a></div>'
                '</li></ul></nav>')
        source = navigation_source(html, BASE)
        self.assertNotIn('single-shoe', source['navigation_html'])
        audit = audit_navigation({'Women': BASE + 'women.html',
                                  'Wide': BASE + 'women.html?width=wide'}, html, BASE)
        self.assertEqual(audit['source_urls'], 2)
        self.assertEqual(audit['matched_urls'], 2)
        self.assertTrue(audit['passed'])
        self.assertFalse(audit_navigation({'Women': BASE + 'women.html'}, html, BASE)['passed'])

    def test_product_source_markers_are_not_required_category_links(self):
        html = ('<nav><a href="/shoes/">Shoes</a>'
                '<a data-link-type="editorial" href="/story/">Story</a>'
                '<div data-product-id="123"><a href="/single-shoe.html">Shoe</a></div></nav>')
        source = navigation_source(html, BASE)
        self.assertEqual(source['valid_urls'], [BASE + 'shoes/'])

    def test_kong_style_seven_of_twenty_three_is_detected_as_incomplete(self):
        audit = audit_navigation(categories_for(7), menu_html(23), BASE)
        self.assertFalse(audit['passed'])
        self.assertEqual(audit['source_urls'], 23)
        self.assertEqual(audit['matched_urls'], 7)
        self.assertEqual(audit['ratio'], round(7 / 23, 4))
        self.assertEqual(set(audit['missing_urls']),
                         set(categories_for(23).values()) - set(categories_for(7).values()))
        seven_missing = audit_navigation(categories_for(16), menu_html(23), BASE)
        self.assertEqual(len(seven_missing['missing_urls']), 7)

    def test_empty_native_result_runs_raw_ai_recovery_and_publishes_custom_urls(self):
        expected = categories_for(2)
        processes = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('core.custom_site._native_navigation', return_value=({}, 'native')), \
                 patch('core.simple_nav.shutil.which', return_value='codex.exe'), \
                 patch('core.simple_nav.subprocess.Popen', side_effect=fake_cli(expected, processes)) as cli:
                categories, evidence, documents, release = run_navigation(
                    root, BASE, menu_html(2), 'bigcommerce')
            self.assertEqual(cli.call_count, 1)
            self.assertEqual(categories, expected)
            self.assertEqual(json.loads((root / 'url.json').read_text(encoding='utf-8')), expected)
            self.assertFalse(json.loads((root / 'navigation_native_audit.json').read_text(encoding='utf-8'))['passed'])
            self.assertEqual(json.loads((root / 'navigation_repair.json').read_text(encoding='utf-8'))['status'], 'completed')
            self.assertEqual(release['status'], 'approved')
            self.assertIn('NAVIGATION HTML', processes[0].communicate.call_args.args[0])
            self.assertIn('data-collapsible="dog-panel"', processes[0].communicate.call_args.args[0])

    def test_incomplete_native_result_runs_recovery_for_all_twenty_three_links(self):
        expected = categories_for(23)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('core.custom_site._native_navigation', return_value=(categories_for(7), 'native')), \
                 patch('core.simple_nav.shutil.which', return_value='codex.exe'), \
                 patch('core.simple_nav.subprocess.Popen', side_effect=fake_cli(expected, [])) as cli:
                categories, _, _, release = run_navigation(root, BASE, menu_html(23), 'bigcommerce')
            self.assertEqual(cli.call_count, 1)
            self.assertEqual(categories, expected)
            self.assertEqual(release['status'], 'approved')
            audit = json.loads((root / 'navigation_native_audit.json').read_text(encoding='utf-8'))
            self.assertEqual(audit['matched_urls'], 7)
            self.assertEqual(audit['source_urls'], 23)

    def test_raw_ai_result_discards_unknown_url_while_retaining_real_custom_paths(self):
        expected = categories_for(2)
        candidate = dict(expected, Invented=BASE + 'dog/toys/never-in-source/')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('core.simple_nav.shutil.which', return_value='codex.exe'), \
                 patch('core.simple_nav.subprocess.Popen', side_effect=fake_cli(candidate, [])) as cli:
                result = recover_navigation(root, BASE, menu_html(2), AdapterRequired('incomplete'))
            self.assertEqual(cli.call_count, 1)
            self.assertEqual(result, expected)
            warnings = json.loads((root / 'warnings.json').read_text(encoding='utf-8'))
            self.assertTrue(any(warning['code'] == 'removed_url'
                                and warning['url'] == candidate['Invented'] for warning in warnings))
            self.assertEqual(json.loads((root / 'url.json').read_text(encoding='utf-8')), expected)


if __name__ == '__main__':
    unittest.main()
