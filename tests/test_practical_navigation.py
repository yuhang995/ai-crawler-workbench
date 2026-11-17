import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from core.common import CrawlError,save_json,read_json
from core.practical_navigation import build_practical,validate_practical,apply_edits
from core.shopify_inventory import parse_source,fetch_inventory
from core.navigation_release import publish,require_release
from test_workflow import FakeClient,Response,HOME,BASE


class InventoryClient(FakeClient):
    def get(self,url,**kwargs):
        if 'collections.json' in url:return Response(url,json.dumps({'collections':[{'handle':'shop','title':'Shop'},{'handle':'extra','title':'Extra'}]}))
        if '.xml' in url:return Response(url,'<urlset><url><loc>'+BASE+'/collections/extra</loc></url></urlset>')
        return super().get(url)


class PracticalTests(unittest.TestCase):
    def seed(self,root,extra=False):
        html='<script>Shopify.theme={};Shopify.country="US";Shopify.currency={"active":"USD"};</script><header><nav aria-label="Desktop"><a href="/collections/shop">Shop</a><a href="/pages/about">About</a></nav><header-drawer><summary>Menu Close</summary><a href="/collections/shop">Shop</a></header-drawer></header>'
        if extra:html=html.replace('</nav>','<a href="/collections/extra">Extra</a></nav>')
        save_json(root/'seed.json',{})
        (root/'evidence').mkdir();(root/'evidence/home.html').write_text(html,encoding='utf-8')
        inventory=fetch_inventory(InventoryClient(),BASE,root/'evidence')
        return build_practical(root,BASE,html,inventory)

    def test_inventory_never_adds_unobserved_menu_categories(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);categories,proof,documents=self.seed(root)
            self.assertNotIn(BASE+'/collections/extra',categories.values())
            self.assertFalse(any('Menu Close' in k or '/pages/' in u for k,u in categories.items()))
            self.assertIn('Menu Close',proof['auto_pruned_roots'])
            self.assertTrue(proof['menu_hierarchy_complete'])
            self.assertFalse(any(e['source']=='collections_feed_fallback' for e in proof['entries'].values()))
            validate_practical(categories,proof,documents,BASE)
            self.assertTrue(read_json(root/'warnings.json'))

    def test_sitemap_locale_variants_never_replace_us_root_urls(self):
        index={'url':BASE+'/sitemap.xml','kind':'xml','body':'<sitemapindex><sitemap><loc>'+BASE+'/sitemap_collections_1.xml?from=1</loc></sitemap><sitemap><loc>'+BASE+'/ga/sitemap_collections_1.xml?from=1</loc></sitemap></sitemapindex>'}
        self.assertEqual(parse_source(index,BASE)[1],[BASE+'/sitemap_collections_1.xml?from=1'])
        data={'url':BASE+'/sitemap_collections_1.xml','kind':'xml','body':'<urlset><url><loc>'+BASE+'/collections/extra</loc></url><url><loc>'+BASE+'/ga/collections/extra</loc></url></urlset>'}
        self.assertEqual(parse_source(data,BASE)[0]['extra']['url'],BASE+'/collections/extra')

    def test_header_ul_is_a_desktop_menu_without_nav_tag(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'evidence').mkdir()
            html='<header><ul class="custom-nav__top-links"><li><a href="/collections/shop">Shop</a></li><li><header-drawer><summary>Menu Close</summary><a href="/collections/shop">Shop All</a></header-drawer></li></ul></header>'
            (root/'evidence/home.html').write_text(html,encoding='utf-8')
            categories,proof,documents=build_practical(root,BASE,html,{'sources':[]})
            self.assertFalse(any('Menu Close' in k for k in categories));self.assertIn('Menu Close',proof['auto_pruned_roots'])

    def test_source_forgery_still_rejected_and_http_timeout_warns(self):
        class Timeout(InventoryClient):
            def get(self,url,**kw):raise CrawlError('request timeout')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);categories,proof,docs=self.seed(root)
            release=publish(root,categories,proof,docs,BASE,Timeout())
            self.assertEqual(release['status'],'approved');require_release(root)
            self.assertTrue(any(w['code']=='target_sample_warning' for w in read_json(root/'warnings.json')))
            forged=dict(categories,Fake=BASE+'/collections/not-real')
            with self.assertRaises(CrawlError):validate_practical(forged,proof,docs,BASE)

    def test_actual_access_block_remains_hard_failure(self):
        class Blocked(InventoryClient):
            def get(self,url,**kw):raise CrawlError('HTTP Error 403')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);categories,proof,docs=self.seed(root)
            with self.assertRaisesRegex(CrawlError,'403'):publish(root,categories,proof,docs,BASE,Blocked())

    def test_wrong_target_market_is_never_a_warning_pass(self):
        class Canada(InventoryClient):
            def get(self,url,**kw):return Response(url,'<script>Shopify.country="CA";Shopify.currency={"active":"CAD"};</script>')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);categories,proof,docs=self.seed(root)
            with self.assertRaisesRegex(CrawlError,'US/USD'):publish(root,categories,proof,docs,BASE,Canada())

    def test_definite_404_is_removed_and_remaining_source_revalidated(self):
        class Missing(InventoryClient):
            def get(self,url,**kw):
                if url.endswith('/extra'):raise CrawlError('HTTP Error 404')
                return super().get(url,**kw)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);categories,proof,docs=self.seed(root,extra=True)
            publish(root,categories,proof,docs,BASE,Missing());require_release(root)
            self.assertNotIn(BASE+'/collections/extra',categories.values());validate_practical(categories,proof,docs,BASE)

    def test_ambiguous_menu_cannot_be_replaced_by_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'evidence').mkdir()
            inventory=fetch_inventory(InventoryClient(),BASE,root/'evidence')
            with patch('core.practical_navigation.extract_navigation',side_effect=CrawlError('ambiguous root')):
                with self.assertRaises(CrawlError):build_practical(root,BASE,'<header></header>',inventory)
            self.assertFalse((root/'url.json').exists())

    def test_real_url_does_not_authorize_wrong_menu_name(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);categories,proof,docs=self.seed(root)
            forged={'Wrong Parent':next(iter(categories.values()))}
            with self.assertRaisesRegex(CrawlError,'归属'):validate_practical(forged,proof,docs,BASE)

    def test_legacy_link_only_proof_cannot_publish(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);categories,proof,docs=self.seed(root)
            proof.pop('navigation_contract')
            with self.assertRaisesRegex(CrawlError,'归属'):publish(root,categories,proof,docs,BASE,InventoryClient())

    def test_legacy_practical_pipeline_warns_and_packages_without_ai(self):
        from core.pipeline import run_job
        with tempfile.TemporaryDirectory() as d,patch('core.pipeline.Client',InventoryClient),patch('core.navigation_ai.run_navigation') as ai:
            result=run_job(BASE,d,navigation_mode='practical')
            ai.assert_not_called();self.assertTrue(result['validation']['passed']);self.assertTrue((Path(d)/'crawler.zip').is_file())

    def test_inconclusive_visual_audit_warns_without_false_pass(self):
        from core.pipeline import run_job
        def uncertain(root,*args):
            save_json(Path(root)/'visual_audit.json',{'status':'inconclusive','passed':False,'llm_invocations':0,'seconds':0})
            raise CrawlError('首页轮播变化，视觉未确定')
        with tempfile.TemporaryDirectory() as d,patch('core.pipeline.Client',InventoryClient),patch('core.visual_audit.run_visual_audit',side_effect=uncertain):
            result=run_job(BASE,d,visual_check=True,navigation_mode='practical')
            self.assertEqual(result['status'],'completed');self.assertEqual(result['visual_status'],'inconclusive')
            self.assertTrue(any(w['code']=='visual_audit_warning' for w in result['warnings']))
            self.assertFalse(read_json(Path(d)/'visual_audit.json')['passed'])

    def test_manual_edit_rebinds_proof_and_rejects_cross_domain(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);categories,proof,docs=self.seed(root)
            save_json(root/'confirmed_categories.json',{'Renamed':BASE+'/collections/extra'})
            revised,evidence=apply_edits(root,categories,proof,BASE,InventoryClient(),docs)
            self.assertEqual(list(revised),['Renamed']);validate_practical(revised,evidence,docs,BASE)
            save_json(root/'confirmed_categories.json',{'Bad':'https://evil.example/collections/extra'})
            with self.assertRaises(ValueError):apply_edits(root,categories,proof,BASE,InventoryClient(),docs)

    def test_manual_pipeline_samples_only_confirmed_range_and_packages(self):
        from core.pipeline import run_job
        with tempfile.TemporaryDirectory() as d,patch('core.pipeline.Client',InventoryClient),patch('core.practical_navigation.capture_preview'):
            root=Path(d);confirm=threading.Event()
            def emit(event):
                if event['stage']=='awaiting_confirmation':
                    self.assertFalse((root/'samples.json').exists())
                    save_json(root/'confirmed_categories.json',{'Only Extra':BASE+'/collections/extra'});confirm.set()
            result=run_job(BASE,root,manual=True,confirm=confirm,emit=emit,navigation_mode='practical')
            self.assertTrue(result['validation']['passed']);self.assertTrue((root/'crawler.zip').exists())
            self.assertEqual(read_json(root/'url.json'),{'Only Extra':BASE+'/collections/extra'})
            self.assertEqual(list(read_json(root/'detail_url.json')),['Only Extra'])

    def test_confirm_endpoint_accepts_edited_categories(self):
        import app
        with tempfile.TemporaryDirectory() as d,patch.object(app,'RUNS',Path(d)),patch.object(app,'JOBS',{}):
            job={'id':'test','site':BASE,'status':'awaiting_confirmation','confirm':threading.Event()}
            app.JOBS['test']=job
            server=app.LocalServer(('127.0.0.1',0),app.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                expected={'My Selection':BASE+'/collections/shop'}
                c.request('POST','/api/jobs/test/confirm',json.dumps({'categories':expected}),{'Content-Type':'application/json'})
                response=c.getresponse();response.read();c.close()
                self.assertEqual(response.status,200);self.assertTrue(job['confirm'].is_set())
                self.assertEqual(read_json(Path(d)/'test/confirmed_categories.json'),expected)
            finally:server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':unittest.main()
