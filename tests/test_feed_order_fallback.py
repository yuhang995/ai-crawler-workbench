import unittest
import threading
from unittest.mock import MagicMock,patch

from playwright.sync_api import TimeoutError as BrowserTimeout

from core.common import AdapterRequired, CrawlError
from core.shopify_fastpath import (collect_listing,embedded_product_handles,
                                   goto_listing,RenderedPages,route_listing_assets,
                                   detail_shopify_origin)
from core.validator import source_order
from core.pipeline import compact_shopify_listing_evidence
from core.pipeline import collect_full_shopify_listings


BASE='https://example.com/collections/shop'


def product(slug,number):
    return {'id':number,'handle':slug,'title':slug,'variants':[{'id':number*10,'price':'1.00'}]}


class Response:
    def __init__(self,text,url=BASE):self.text=text;self.url=url


class Client:
    def __init__(self,html,products):self.html=html;self.products=products
    def get(self,url):return Response(self.html,url)
    def json(self,url):return {'products':self.products}


class DetailOriginTests(unittest.TestCase):
    def test_standard_theme_ignores_stale_declared_myshopify_origin(self):
        detection = {'mode': 'standard_shopify', 'headless_shopify': False,
                     'shopify_origin': 'https://old-store.myshopify.com'}
        self.assertEqual(detail_shopify_origin(detection), '')

    def test_hydrogen_keeps_verified_separate_data_origin(self):
        detection = {'mode': 'hydrogen_shopify', 'headless_shopify': True,
                     'shopify_origin': 'https://brand.myshopify.com'}
        self.assertEqual(detail_shopify_origin(detection),
                         'https://brand.myshopify.com')


class FeedOrderFallbackTests(unittest.TestCase):
    def test_shopify_page_builder_landing_replays_finite_product_grid(self):
        url = 'https://example.com/pages/sale#vitamins'
        products = [product('a', 1), product('b', 2), product('c', 3)]
        html = ('<main><section id="vitamins"><ul id="product-grid">'
                + ''.join(f'<li><a href="/products/{p["handle"]}">{p["handle"]}</a></li>'
                          for p in products)
                + '</ul></section></main>')

        class LandingClient:
            def get(self, target, **_kwargs):
                return Response(html, target)

            def json(self, target, **_kwargs):
                slug = target.rsplit('/products/', 1)[1].removesuffix('.json')
                match = next(item for item in products if item['handle'] == slug)
                return {'product': match}

        listing = collect_listing(LandingClient(), url, full=True)
        self.assertEqual(listing['format'], 'shopify_landing_grid')
        self.assertEqual([p['handle'] for p in listing['products']], ['a', 'b', 'c'])
        self.assertEqual(listing['urls'], [
            'https://example.com/products/a', 'https://example.com/products/b',
            'https://example.com/products/c'])
        self.assertEqual(listing['card_source_kind'], 'observed_grid')

    def test_compact_full_store_evidence_retains_validator_identity_and_order(self):
        products=[dict(product('a',1),body_html='x'*10000),
                  dict(product('b',2),body_html='y'*10000),
                  dict(product('c',3),body_html='z'*10000)]
        listing={'url':BASE,'products':products,'urls':[],
                 'warnings':[{'code':'listing_order_feed_fallback'}],
                 'order_evidence':'products_feed_fallback','feed_page_size':50,
                 'membership_sources':[],
                 'feeds':[{'products':products}],
                 'pages':[{'url':BASE,'handles':['a','b','c'],'html':'h'*50000}]}
        compact=compact_shopify_listing_evidence(listing)
        self.assertEqual(source_order(compact),['a','b','c'])
        self.assertEqual(compact['pages'][0]['html'],'')
        self.assertNotIn('body_html',compact['feeds'][0]['products'][0])
    def test_collection_html_warms_same_session_before_products_feed(self):
        products=[product('a',1),product('b',2),product('c',3)]
        html='<main id="product-grid">' + ''.join(
            f'<a href="/products/{item["handle"]}">{item["handle"]}</a>' for item in products) + '</main>'

        class WarmSessionClient:
            def __init__(self):
                self.warmed=False
                self.calls=[]
                self.thread_ids=[]

            def get(self,url):
                self.calls.append(('html',url))
                self.thread_ids.append(threading.get_ident())
                self.warmed=True
                return Response(html,url)

            def json(self,url):
                self.calls.append(('json',url))
                self.thread_ids.append(threading.get_ident())
                if not self.warmed:
                    raise CrawlError('HTTP Error 403: collection session cookie missing')
                return {'products':products}

        client=WarmSessionClient()
        result=collect_listing(client,BASE)
        self.assertEqual([kind for kind,_ in client.calls[:2]],['html','json'])
        self.assertEqual(len(set(client.thread_ids)),1)
        self.assertEqual([item['handle'] for item in result['products']],['a','b','c'])

    def test_malformed_header_ancestor_collects_without_browser(self):
        products=[product('a',1),product('b',2),product('c',3)]
        html='''<html><body><header><nav><a href="/products/nav-only">Navigation</a>
          <main id="MainContent"><div class="products"><a href="/products/c">C</a>
          <a href="/products/a">A</a><a href="/products/b">B</a></div></main>
          </nav></header></body></html>'''
        with patch('core.shopify_fastpath.rendered_listing_pages',side_effect=AssertionError('browser called')):
            result=collect_listing(Client(html,products),BASE)
        self.assertEqual([item['handle'] for item in result['products']],['c','a','b'])
        self.assertEqual(result['warnings'],[])

    def test_embedded_handles_use_main_order_and_ignore_header(self):
        products=[product('a',1),product('b',2),product('c',3)]
        html='''<header><a href="/products/a">A</a></header><main><script>
          {"handle":"c"},{"handle":"a"},{"url":"/products/b"}
        </script></main>'''
        self.assertEqual(embedded_product_handles(html,products,BASE),['c','a','b'])

    def test_embedded_order_skips_browser(self):
        products=[product('a',1),product('b',2),product('c',3)]
        html='<main><script>{"handle":"b"},{"handle":"c"},{"handle":"a"}</script></main>'
        with patch('core.shopify_fastpath.rendered_listing_pages',side_effect=AssertionError('browser called')):
            result=collect_listing(Client(html,products),BASE)
        self.assertEqual([p['handle'] for p in result['products']],['b','c','a'])
        self.assertEqual(result['warnings'],[])

    def test_paginated_feed_rebuilds_embedded_order_after_all_products_arrive(self):
        products=[product(f'p{i}',i) for i in range(1,53)]
        order=['p3','p1','p2','p52','p51']+[f'p{i}' for i in range(4,51)]
        html='<main><script>'+','.join('{"handle":"'+slug+'"}' for slug in order)+'</script></main>'
        class PagedClient:
            def get(self,url): return Response(html,url)
            def json(self,url):
                return {'products': products[50:] if 'page=2' in url else products[:50]}
        with patch('core.shopify_fastpath.rendered_listing_pages',
                   side_effect=AssertionError('browser called')):
            result=collect_listing(PagedClient(),BASE)
        self.assertEqual([p['handle'] for p in result['products']],order)
        self.assertEqual(source_order(result),order)

    def test_unfiltered_browser_timeout_uses_feed_order_with_warning(self):
        products=[product('a',1),product('b',2),product('c',3)]
        with patch('core.shopify_fastpath.rendered_listing_pages',
                   side_effect=AssertionError('SPA feed fallback must not launch browser')):
            result=collect_listing(Client('<main></main>',products),BASE)
        self.assertEqual([p['handle'] for p in result['products']],['a','b','c'])
        self.assertEqual(result['warnings'][0]['code'],'spa_feed_order_fallback')
        self.assertEqual(result['warnings'][0]['reason'],'static_spa_grid_empty')

    def test_full_store_mode_uses_authoritative_feed_without_per_category_browser(self):
        products=[product('a',1),product('b',2),product('c',3)]
        with patch('core.shopify_fastpath.rendered_listing_pages',
                   side_effect=AssertionError('browser called')):
            result=collect_listing(
                Client('<main></main>',products),BASE,
                prefer_feed_order=True)
        self.assertEqual([p['handle'] for p in result['products']],['a','b','c'])
        self.assertEqual(result['order_evidence'],'products_feed_fallback')
        self.assertEqual(result['warnings'][0]['reason'],'full_store_feed_order')

    def test_full_store_feed_uses_250_item_pages(self):
        products=[product(f'p{i}',i) for i in range(1,253)]
        html='<main></main>'

        class PagedClient:
            def __init__(self): self.urls=[]
            def get(self,url,**kwargs): return Response(html,url)
            def json(self,url,**kwargs):
                self.urls.append(url)
                return {'products': products[250:] if 'page=2' in url else products[:250]}

        client=PagedClient()
        result=collect_listing(client,BASE,prefer_feed_order=True,max_seconds=180)
        self.assertEqual(result['feed_page_size'],250)
        self.assertIn('limit=250',client.urls[0])
        self.assertEqual(len(result['products']),252)

    def test_full_store_mode_survives_collection_html_transport_failure(self):
        products=[product('a',1),product('b',2),product('c',3)]
        client=MagicMock()
        client.get.side_effect=CrawlError('TLS connection closed')
        client.json.return_value={'products':products}
        result=collect_listing(client,BASE,prefer_feed_order=True)
        self.assertEqual([p['handle'] for p in result['products']],['a','b','c'])
        self.assertEqual(result['order_evidence'],'products_feed_fallback')
        self.assertEqual(result['warnings'][0]['code'],'listing_html_feed_only_fallback')

    def test_full_store_skips_only_proven_parent_content_landing(self):
        categories = {
            'Shop': BASE,
            'Shop,Shop Boots': BASE + '-boots',
            'Sale': BASE + '-sale',
        }
        child = {'url': BASE + '-boots', 'urls': [BASE + '/products/a'],
                 'products': [product('a', 1)], 'warnings': []}
        sale = {'url': BASE + '-sale', 'urls': [BASE + '/products/b'],
                'products': [product('b', 2)], 'warnings': []}
        def collect(client, url, prefer_feed_order=False):
            if url == BASE:
                raise AdapterRequired('落地页既无完整子集合来源，也无完整、有限的显式商品卡片')
            return child if url.endswith('-boots') else sale
        with patch('core.pipeline.collect_listing', side_effect=collect):
            urls, values, warnings = collect_full_shopify_listings(object(), categories)
        self.assertEqual(urls, [BASE + '-boots', BASE + '-sale'])
        self.assertEqual(values, [child, sale])
        self.assertEqual(warnings[0]['code'], 'content_landing_skipped')

    def test_full_store_never_skips_a_leaf_adapter_failure(self):
        categories = {'Shop,Shop Boots': BASE + '-boots'}
        with patch('core.pipeline.collect_listing', side_effect=AdapterRequired(
                '落地页既无完整子集合来源，也无完整、有限的显式商品卡片')):
            with self.assertRaises(AdapterRequired):
                collect_full_shopify_listings(object(), categories)

    def test_tag_scoped_collection_never_broadens_after_html_failure(self):
        client=MagicMock()
        client.get.side_effect=CrawlError('TLS connection closed')
        with self.assertRaises(CrawlError):
            collect_listing(client,BASE+'/sale',prefer_feed_order=True)
        client.json.assert_not_called()

    def test_successful_render_without_cards_uses_unfiltered_feed_order(self):
        products=[product('a',1),product('b',2),product('c',3)]
        rendered=RenderedPages();rendered[BASE]='<div><product-grid></product-grid></div>'
        with patch('core.shopify_fastpath.rendered_listing_pages',
                   side_effect=AssertionError('browser should not run for an unfiltered SPA shell')):
            result=collect_listing(Client('<div></div>',products),BASE)
        self.assertEqual([p['handle'] for p in result['products']],['a','b','c'])
        self.assertEqual(result['order_evidence'],'products_feed_fallback')
        self.assertEqual(result['warnings'][0]['code'],'spa_feed_order_fallback')
        self.assertEqual(result['warnings'][0]['reason'],'static_spa_grid_empty')

    def test_unfiltered_opaque_load_more_uses_complete_feed_with_warning(self):
        products=[product('a',1),product('b',2),product('c',3)]
        html='''<main id="product-grid">
          <a href="/products/c">C</a><a href="/products/a">A</a>
          <button class="load-more">Load more</button></main>'''
        with patch('core.shopify_fastpath.rendered_listing_pages',
                   side_effect=AssertionError('browser called')):
            result=collect_listing(Client(html,products),BASE)
        self.assertEqual([p['handle'] for p in result['products']],['a','b','c'])
        self.assertEqual(result['order_evidence'],'products_feed_fallback')
        self.assertEqual(result['warnings'][0]['reason'],'dynamic_load_more')

    def test_filtered_browser_timeout_never_broadens_to_feed(self):
        products=[product('a',1),product('b',2),product('c',3)]
        with patch('core.shopify_fastpath.rendered_listing_pages',side_effect=BrowserTimeout('grid timeout')):
            with self.assertRaises(BrowserTimeout):
                collect_listing(Client('<main></main>',products),BASE+'?filter.color=blue')

    def test_tracking_query_does_not_turn_unfiltered_collection_into_filtered_scope(self):
        products=[product('a',1),product('b',2),product('c',3)]
        with patch('core.shopify_fastpath.rendered_listing_pages',
                   side_effect=AssertionError('tracking-only URL must use verified feed')):
            result=collect_listing(Client('<main></main>',products),
                                   BASE+'?abralink=campaign')
        self.assertEqual([p['handle'] for p in result['products']], ['a','b','c'])
        self.assertEqual(result['order_evidence'], 'products_feed_fallback')

    def test_validator_accepts_tracking_query_feed_fallback(self):
        products=[product('a',1),product('b',2),product('c',3)]
        with patch('core.shopify_fastpath.rendered_listing_pages',
                   side_effect=AssertionError('tracking-only URL must use verified feed')):
            result=collect_listing(Client('<main></main>',products),
                                   BASE+'?abralink=campaign')
        self.assertEqual(source_order(result), ['a','b','c'])

    def test_full_filtered_browser_timeout_downgrades_to_verified_feed(self):
        products=[product('a',1),product('b',2),product('c',3)]
        with patch('core.shopify_fastpath.rendered_listing_pages',
                   side_effect=BrowserTimeout('Locator.wait_for timeout')):
            result=collect_listing(Client('<main></main>',products),
                                   BASE+'?filter.color=blue', prefer_feed_order=True)
        self.assertEqual([p['handle'] for p in result['products']], ['a','b','c'])
        self.assertEqual(result['order_evidence'], 'products_feed_fallback')
        self.assertEqual(result['warnings'][0]['code'], 'listing_order_feed_fallback')
        self.assertIn('Playwright', result['warnings'][0]['message'])

    def test_goto_uses_commit_and_12_second_element_wait(self):
        page=MagicMock();page.url=BASE;warnings=[]
        goto_listing(page,BASE,warnings)
        page.goto.assert_called_once_with(BASE,wait_until='commit',timeout=12000)
        page.locator.return_value.first.wait_for.assert_called_once_with(timeout=12000)

    def test_route_blocks_heavy_and_trackers_but_keeps_first_party_data(self):
        page=MagicMock();route_listing_assets(page,BASE)
        callback=page.route.call_args.args[1]
        for url,kind,blocked in [('https://example.com/a.jpg','image',True),
                                 ('https://www.google-analytics.com/g.js','script',True),
                                 ('https://example.com/products.json','xhr',False)]:
            route=MagicMock();request=MagicMock(url=url,resource_type=kind)
            callback(route,request)
            self.assertEqual(route.abort.called,blocked)
            self.assertEqual(route.continue_.called,not blocked)

    def test_validator_accepts_saved_unfiltered_feed_fallback_and_rejects_scope(self):
        products=[product('a',1),product('b',2),product('c',3)]
        listing={'url':BASE,'feeds':[{'products':products}],'feed_page_size':50,
                 'pages':[{'url':BASE,'html':'<main></main>','handles':['a','b','c']}],
                 'warnings':[{'code':'listing_order_feed_fallback'}],
                 'order_evidence':'products_feed_fallback'}
        self.assertEqual(source_order(listing),['a','b','c'])
        listing['url']=BASE+'?compatible-with=Phone'
        with self.assertRaises(CrawlError):source_order(listing)

    def test_validator_recomputes_embedded_order(self):
        products=[product('a',1),product('b',2),product('c',3)]
        html='<main><script>{"handle":"c"},{"handle":"a"},{"handle":"b"}</script></main>'
        listing={'url':BASE,'feeds':[{'products':products}],'feed_page_size':50,
                 'pages':[{'url':BASE,'html':html,'handles':['c','a','b']}],
                 'warnings':[],'order_evidence':'embedded_handles'}
        self.assertEqual(source_order(listing),['c','a','b'])


if __name__=='__main__':unittest.main()
