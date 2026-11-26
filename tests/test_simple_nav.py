import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.simple_nav import (slim_html_for_ai, clean_categories, publish_simple,
                             extract_url_json_with_ai, run_simple,
                             _rendered_body_category_digest)
from core.common import Client, CrawlError, read_json

BASE = 'https://example.com/'


class SimpleNavTests(unittest.TestCase):
    def test_rendered_menu_is_additive_even_when_snapshot_has_more_links(self):
        from core.simple_nav import _merge_navigation_data

        static = slim_html_for_ai(
            '<nav><a href="/collections/static">Static child</a></nav>', BASE)
        rendered = slim_html_for_ai(
            '<nav><a href="/collections/new">New child</a>'
            '<a href="/collections/other">Other child</a></nav>', BASE)
        merged = _merge_navigation_data(static, rendered, BASE)
        self.assertEqual(set(merged['source_navigation_urls']), {
            BASE + 'collections/static', BASE + 'collections/new', BASE + 'collections/other'})
        self.assertIn('Static child', merged['text'])
        self.assertIn('New child', merged['text'])
        self.assertEqual(merged['navigation_coverage']['ratio'], 1.0)

    def test_custom_menu_retains_product_category_paths_and_column_ownership(self):
        from core.simple_nav import _custom_navigation_for_ai

        html = """<nav><ul><li><button>Shop</button><div><h3>By Benefit</h3><ul>
            <li><a href="/products/benefits/sleep">Sleep</a></li>
            <li><a href="/products/probiotics">Probiotics</a></li>
            </ul></div></li></ul></nav>
            <main><a href="/products/featured">Featured product</a></main>"""
        snapshot = '<nav><button>Shop</button></nav>'
        result = _custom_navigation_for_ai(html, BASE, [snapshot])
        self.assertEqual(set(result['valid_urls']), {
            BASE + 'products/benefits/sleep', BASE + 'products/probiotics'})
        self.assertRegex(result['navigation_outline'],
                         r'(?m)^  \[label\] By Benefit$\n^    \[a\] Sleep')
        shopify = html.replace('<nav>', '<nav><a href="/collections/all">Shop All</a>', 1)
        result = _custom_navigation_for_ai(shopify, BASE)
        self.assertNotIn(BASE + 'products/probiotics', result['valid_urls'])

    def test_category_label_ignores_decorative_badge(self):
        from bs4 import BeautifulSoup
        from core.simple_nav import _label
        for html, label in [
            ('<a>Ready to Cook <span class="menu-badge">NEW</span></a>', 'Ready to Cook'),
            ('<a><span class="label">New Arrivals</span></a>', 'New Arrivals'),
            ('<a>Sale</a>', 'Sale'),
        ]:
            self.assertEqual(_label(BeautifulSoup(html, 'html.parser').a), label)

    def test_desktop_menu_keeps_distinct_filter_queries_and_drops_tracking(self):
        html = '''<header><nav><a href="/collections/bath">Bath</a>
          <a href="/collections/bath?age_stage=Baby&amp;utm_source=menu&amp;_pos=1">Baby</a>
          <a href="/collections/bath?age_stage=Toddler&amp;msclkid=ad">Toddler</a>
          <a href="/collections/bath?filter.color=red&amp;filter.color=blue&amp;ref=nav">Colors</a>
          </nav></header>'''
        data = slim_html_for_ai(html, BASE)
        expected = {
            'Bath': BASE + 'collections/bath',
            'Bath,Bath Baby': BASE + 'collections/bath?age_stage=Baby',
            'Bath,Bath Toddler': BASE + 'collections/bath?age_stage=Toddler',
            'Bath,Bath Colors': BASE + 'collections/bath?filter.color=red&filter.color=blue',
        }
        self.assertEqual(set(data['valid_urls']), set(expected.values()))
        self.assertEqual(clean_categories(expected, data['valid_urls'], BASE)[0], expected)
        for url in expected.values():
            self.assertIn(url.removeprefix(BASE.rstrip('/')), data['text'])

    def test_custom_menu_filter_urls_are_distinct(self):
        from core.custom_navigation import navigation_source
        html = '''<header><nav>
          <a href="/bath-diapering">Bath</a>
          <a href="/bath-diapering?age_stage=Baby&amp;utm_campaign=nav">Baby</a>
          <a href="/bath-diapering?age_stage=Toddler&amp;srsltid=ad">Toddler</a>
          </nav></header>'''
        urls = navigation_source(html, BASE)['valid_urls']
        self.assertEqual(set(urls), {BASE + 'bath-diapering',
                                     BASE + 'bath-diapering?age_stage=Baby',
                                     BASE + 'bath-diapering?age_stage=Toddler'})

    def test_singular_collection_route_is_a_real_category(self):
        from core.simple_nav import legal_url
        self.assertTrue(legal_url('https://example.com/collection/all-products',
                                  'https://example.com/'))

    def test_nested_product_range_is_category_but_pdp_is_not(self):
        from core.simple_nav import legal_url
        self.assertTrue(legal_url('https://example.com/products/play-kits/baby',
                                  'https://example.com/'))
        self.assertFalse(legal_url('https://example.com/products/play-kits',
                                   'https://example.com/'))

    def test_shop_labelled_page_landings_form_verified_catalog(self):
        from core.simple_nav import _shopify_catalog_landing_navigation
        html = '''<script>Shopify.currency={"active":"USD"}</script>
          <header><nav><a href="/pages/paper">Shop Paper</a>
          <a href="/pages/towels">Shop Towels</a>
          <a href="/pages/about">About</a></nav></header>'''
        self.assertEqual(_shopify_catalog_landing_navigation(html, 'https://example.com/'), {
            'Shop Paper': 'https://example.com/pages/paper',
            'Shop Towels': 'https://example.com/pages/towels'})
    def test_empty_static_navigation_uses_one_rendered_dom_pass(self):
        static = '<html><header><button>Shop</button></header><main></main></html>'
        rendered = '''<html><header><nav><a href="/collections/all">Shop All</a>
            <a href="/collections/new">New</a></nav></header></html>'''
        expected = {'Shop All': BASE + 'collections/all', 'New': BASE + 'collections/new'}
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.rendered_html', return_value=[rendered]) as render, patch(
                'core.simple_nav.extract_url_json_with_ai', return_value=expected) as extract:
            categories, _, _ = run_simple(directory, BASE, static)
        self.assertEqual(categories, expected)
        render.assert_called_once()
        data = extract.call_args.args[2]
        self.assertEqual(set(data['source_navigation_urls']), set(expected.values()))

    def test_thin_static_navigation_also_uses_rendered_dom_pass(self):
        static = '''<header><nav><a href="/collections/one">One</a>
          <a href="/collections/two">Two</a></nav></header>'''
        rendered = '''<header><nav><a href="/collections/one">One</a>
          <a href="/collections/two">Two</a><a href="/collections/three">Three</a>
          <a href="/collections/four">Four</a></nav></header>'''
        expected = {'One': BASE + 'collections/one', 'Two': BASE + 'collections/two',
                    'Three': BASE + 'collections/three', 'Four': BASE + 'collections/four'}
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.rendered_html', return_value=[rendered]) as render, patch(
                'core.simple_nav.extract_url_json_with_ai', return_value=expected) as extract:
            categories, _, _ = run_simple(directory, BASE, static)
        self.assertEqual(categories, expected)
        render.assert_called_once()
        self.assertEqual(len(extract.call_args.args[2]['source_navigation_urls']), 4)

    def test_rendered_body_fallback_keeps_main_and_footer_categories_only(self):
        html = '''<body><div id="app"><a href="/collections/new">New</a></div>
          <main><a href="/collections/shoes">Shoes</a><a href="/products/p1">P1</a></main>
          <footer><a href="/collections/sale">Sale</a><a href="/pages/about">About</a></footer></body>'''
        lines, urls = _rendered_body_category_digest(html, BASE)
        self.assertEqual(urls, [BASE + 'collections/new', BASE + 'collections/shoes',
                                BASE + 'collections/sale'])
        self.assertTrue(any('[home] Sale' in line for line in lines))

    def test_single_product_store_does_not_launch_render_fallback(self):
        html = '<header><nav><a href="/products/only-product">Shop</a></nav></header>'
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.rendered_html') as render:
            categories, _, _ = run_simple(directory, BASE, html)
        self.assertEqual(categories, {'Shop': BASE + 'products/only-product'})
        render.assert_not_called()

    def test_shopify_page_builder_catalog_landings_are_deterministic(self):
        html = '''<script>Shopify.currency = {"active":"USD"};</script>
        <header><nav class="desktop-nav small-hide medium-hide"><a href="/pages/sale">Shop</a>
        <a href="/blogs/news">Learn</a></nav><header-drawer>
        <a href="/pages/sale#vitamins">Daily Vitamins</a>
        <a href="/pages/shop-by-age#kids">Kids (4-12)</a>
        <a href="/pages/about">About Us</a></header-drawer></header>
        <main><a href="/products/one">One</a><a href="/products/two">Two</a>
        <a href="/products/three">Three</a></main>'''
        with tempfile.TemporaryDirectory() as directory:
            categories, evidence, _ = run_simple(directory, BASE, html)
            self.assertEqual(categories, {
                'Shop': BASE + 'pages/sale',
                'Shop,Shop Daily Vitamins': BASE + 'pages/sale#vitamins',
                'Shop,Shop Kids (4-12)': BASE + 'pages/shop-by-age#kids',
            })
            self.assertEqual(evidence['source'], 'shopify_catalog_landing_navigation')
            self.assertFalse(any('/products/' in url or '/pages/about' in url
                                 for url in categories.values()))

    def test_single_product_primary_shop_entry_is_deterministic_directory(self):
        html = '''<header><nav><a href="/products/variety-pack">Shop All</a>
        <a href="/pages/stores">Find in Store</a></nav></header>
        <main><a href="/products/featured">Featured card</a></main>'''
        with tempfile.TemporaryDirectory() as directory:
            categories, evidence, _ = run_simple(directory, BASE, html)
            self.assertEqual(categories, {'Shop All': BASE + 'products/variety-pack'})
            self.assertEqual(evidence['source'], 'single_product_primary_navigation')
            self.assertTrue(read_json(Path(directory) / 'navigation_report.json')['passed'])

    def test_multiple_product_promos_never_become_directory(self):
        html = '''<header><nav><a href="/products/a">Shop All</a>
        <a href="/products/b">Buy Now</a></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        self.assertEqual(data['valid_urls'], [])

    def test_headless_div_page_keeps_home_cards_and_header_root_triggers(self):
        html = '''<body><div id="app"><header><nav>
        <button>Menu</button><button>Shop By</button><button>Gifts</button>
        </nav></header><div class="page"><section>
        <a href="/collections/earrings"><span>Earrings</span></a>
        <a href="/collections/necklaces"><span>Necklaces</span></a>
        </section></div><footer><a href="/collections/footer-only">Footer</a></footer>
        </div></body>'''
        data = slim_html_for_ai(html, BASE)
        self.assertFalse(data['has_static_navigation'])
        self.assertEqual([card['url'] for card in data['home_cards']], [
            BASE + 'collections/earrings', BASE + 'collections/necklaces'])
        self.assertIn('[root] Shop By', data['text'])
        self.assertIn('[root] Gifts', data['text'])
        self.assertNotIn('[root] Menu', data['text'])
        self.assertNotIn('footer-only', data['text'])

    def test_slim_preserves_drawer_hierarchy_panels_filters_and_source_urls(self):
        html = '''<script>fake</script><header><header-drawer><nav><div class="col" data-huge="noise">
        <h3>Men</h3><a href="/collections/new?filter=a&amp;filter=b">New</a></div></nav></header-drawer>
        <button aria-controls="panel">Women</button></header><div id="panel"><a href="/collections/women">All</a></div>
        <main><article><h2>Summer</h2><a href="/collections/summer">Shop now</a><img src="x"></article></main>
        <footer><a href="/collections/footer-only">Footer</a></footer><a href="/products/no">No</a>'''
        data = slim_html_for_ai(html, BASE)
        self.assertTrue(data['has_static_navigation'])
        self.assertIn('header-drawer', data['navigation_html'])
        self.assertIn('id="panel"', data['navigation_html'])
        self.assertNotIn('data-huge', data['text'])
        self.assertNotIn('<footer', data['text'])
        self.assertEqual(data['home_cards'][0]['title'], 'Summer')
        self.assertIn(BASE+'collections/new?filter=a&filter=b', data['valid_urls'])
        self.assertNotIn(BASE+'products/no', data['valid_urls'])

    def test_filter_does_not_abort_and_keeps_cross_root_same_url(self):
        url = BASE+'collections/new'
        result, warnings = clean_categories({'Men,New':url,'Women,New':url,'Bogus':BASE+'collections/fake',
                                             'Bad':BASE+'collections/gift-card'}, [url,BASE+'collections/gift-card'],BASE)
        self.assertEqual(result,{'Men,Men New':url,'Women,Women New':url})
        self.assertEqual(sum(w['code']=='removed_url' for w in warnings),2)

    def test_display_comma_is_preserved_inside_category_labels(self):
        powders = BASE + 'collections/powders'
        collagen = BASE + 'collections/collagen'
        result, warnings = clean_categories([
            {'key': 'Shop,Collagen, Whey & Supplements', 'url': powders},
            {'key': 'Shop,Collagen, Whey & Supplements,Collagen Peptides & Drink Mixes',
             'url': collagen},
        ], [powders, collagen], BASE)
        self.assertEqual(result, {
            'Shop,Shop Collagen, Whey & Supplements': powders,
            ('Shop,Shop Collagen, Whey & Supplements,'
             'Shop Collagen, Whey & Supplements Collagen Peptides & Drink Mixes'): collagen,
        })
        self.assertFalse(any('Shop Collagen,Shop Collagen Whey' in key for key in result))
        from core.common import split_category_key
        self.assertEqual(split_category_key('Shop,Shop Collagen, Whey & Supplements'),
                         ['Shop', 'Shop Collagen, Whey & Supplements'])

    def test_empty_is_not_falsely_approved(self):
        with tempfile.TemporaryDirectory() as d:
            publish_simple(d,{})
            self.assertFalse(read_json(Path(d)/'navigation_report.json')['passed'])

    def test_same_slug_with_distinct_filters_never_silently_overwrites(self):
        urls=[BASE+'collections/jeans',BASE+'collections/jeans?filter=a',BASE+'collections/jeans?filter=b']
        result,warnings=clean_categories([{'key':'jeans','url':u} for u in urls],urls,BASE)
        self.assertEqual(list(result.values()),urls)
        self.assertEqual(list(result),['jeans','jeans (2)','jeans (3)'])

    def test_redundant_shop_all_cta_collapses_only_when_parent_has_same_url(self):
        body = BASE + 'collections/body'
        all_products = BASE + 'collections/all'
        result, warnings = clean_categories({
            'BODY': body,
            'BODY,SHOP ALL BODY': body,
            'FEATURED,SHOP ALL PRODUCTS': all_products,
        }, [body, all_products], BASE)
        self.assertEqual(result, {
            'BODY': body,
            'FEATURED,FEATURED SHOP ALL PRODUCTS': all_products,
        })
        self.assertEqual([warning['code'] for warning in warnings].count('collapsed_redundant_cta'), 1)

    def test_redundant_parent_named_shop_all_alias_is_collapsed(self):
        bags = BASE + 'collections/bags'
        result, warnings = clean_categories({
            'Womens': BASE + 'collections/womens',
            'Womens,Womens Leather Bags': bags,
            'Womens,Womens Leather Bags,Womens Leather Bags Leather Bags': bags,
        }, [BASE + 'collections/womens', bags], BASE)
        self.assertEqual(result, {
            'Womens': BASE + 'collections/womens',
            'Womens,Womens Leather Bags': bags,
        })
        self.assertEqual(warnings[-1]['code'], 'collapsed_redundant_alias')

    def test_single_cli_readonly_schema_stdin_no_images_or_tools(self):
        data=slim_html_for_ai('<header><a href="/collections/all">Shop</a></header>',BASE)
        with tempfile.TemporaryDirectory() as d:
            def launch(command, **kw):
                # Avoid shell wrapper in this unit test; Windows wrapper is tested live.
                output=Path(command[command.index('-o')+1])
                output.write_text(json.dumps({'entries':[{'key':'Shop','url':BASE+'collections/all'}]}),encoding='utf-8')
                proc=MagicMock();proc.returncode=0
                return proc
            with patch('core.simple_nav.shutil.which',return_value='codex.exe'),patch('core.simple_nav.subprocess.Popen',side_effect=launch) as popen:
                result=extract_url_json_with_ai(d,BASE,data)
            command=popen.call_args.args[0]
            self.assertEqual(popen.call_count,1)
            self.assertIn('--ignore-user-config',command)
            self.assertIn('read-only',command)
            self.assertIn('--output-schema',command)
            self.assertNotIn('-i',command)
            for feature in ('shell_tool','apps','plugins','browser_use','multi_agent'):
                self.assertIn(feature,command)
            self.assertEqual(result,{'Shop':BASE+'collections/all'})

    def test_default_pipeline_bypasses_all_old_navigation_gates(self):
        from core.pipeline import run_job
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from test_practical_navigation import InventoryClient,BASE as SITE
        def ai(root,base,data):
            categories={'Shop':SITE+'/collections/shop'}
            publish_simple(root,categories)
            return categories
        with tempfile.TemporaryDirectory() as d,patch('core.pipeline.Client',InventoryClient),\
             patch('core.simple_nav.extract_url_json_with_ai',side_effect=ai),\
             patch('core.pipeline.validate_navigation',side_effect=AssertionError('old nav gate')),\
             patch('core.validator.validate_navigation',side_effect=AssertionError('old nav gate')),\
             patch('core.navigation_release.publish',side_effect=AssertionError('old publish')),\
             patch('core.visual_audit.run_visual_audit',side_effect=AssertionError('old visual')):
            result=run_job(SITE,d,visual_check=True)
            self.assertEqual(result['status'],'completed')
            self.assertTrue((Path(d)/'crawler.zip').is_file())
            with patch('core.simple_nav.run_simple',side_effect=AssertionError('resume must not call AI')):
                resumed=run_job(SITE,d,reuse_navigation=True)
            self.assertEqual(resumed['status'],'completed')

    def test_browser_recovery_opens_collection_then_fetches_and_saves_cookies(self):
        url=BASE+'collections/all/products.json?limit=250'
        pw=MagicMock();page=pw.chromium.launch.return_value.new_context.return_value.new_page.return_value
        page.url=BASE+'collections/all'
        page.evaluate.return_value={'status':200,'url':url,'body':'{"products":[]}'}
        context=pw.chromium.launch.return_value.new_context.return_value
        context.cookies.return_value=[{'name':'signal','value':'yes','domain':'example.com'}]
        manager=MagicMock();manager.__enter__.return_value=pw
        with patch('playwright.sync_api.sync_playwright',return_value=manager):
            client=Client();self.assertEqual(client.browser_json(url),{'products':[]})
        page.goto.assert_called_once_with(BASE+'collections/all',wait_until='commit',timeout=10000)
        self.assertIn("credentials:'same-origin'",page.evaluate.call_args.args[0])
        self.assertEqual(client.browser_cookies[0]['name'],'signal')

    def test_recovery_budget_is_per_page_not_consumed_by_first_probe(self):
        client=Client();first=BASE+'collections/all/products.json?page=1';second=BASE+'collections/all/products.json?page=2'
        with patch.object(client,'get',side_effect=CrawlError('HTTP Error 403')),patch.object(client,'browser_json',return_value={'products':[]}) as browser:
            client.json(first);client.json(second)
            self.assertEqual(browser.call_count,2)
            with self.assertRaises(CrawlError):client.json(first)

    def test_browser_recovery_does_not_claim_403_or_other_origin_success(self):
        for status,actual in [(403,BASE+'collections/all/products.json'),(200,'https://evil.example/products.json')]:
            pw=MagicMock();page=pw.chromium.launch.return_value.new_context.return_value.new_page.return_value
            page.url=BASE+'collections/all';page.evaluate.return_value={'status':status,'url':actual,'body':'{}'}
            manager=MagicMock();manager.__enter__.return_value=pw
            with patch('playwright.sync_api.sync_playwright',return_value=manager),self.assertRaises(CrawlError):
                Client().browser_json(BASE+'collections/all/products.json')
