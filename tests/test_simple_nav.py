import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.simple_nav import (slim_html_for_ai, clean_categories, publish_simple,
                             extract_url_json_with_ai, run_simple)
from core.common import Client, CrawlError, read_json

BASE = 'https://example.com/'


class SimpleNavTests(unittest.TestCase):
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
