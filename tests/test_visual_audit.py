import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from core.common import CrawlError, save_json, read_json
from core.visual_audit import assess, run_visual_audit, require_visual_audit
from core.visual_capture import frame
from core.visual_capture import capture_visual_snapshots


class VisualAuditTests(unittest.TestCase):
    def fixture(self, root):
        evidence=root/'evidence';evidence.mkdir()
        node={'id':'node-1','text':'Menu','href':'/collections/all','bbox':{},'in_viewport':False,'in_header':True}
        save_json(evidence/'desktop.json',{'nodes':[node]})
        (evidence/'desktop.html').write_text('<header><nav hidden><a href="/collections/all">Menu</a></nav></header>')
        (evidence/'desktop.png').write_bytes(b'mocked screenshot')
        manifest={'frames':[{'id':'desktop','dom':'desktop.json','html':'desktop.html','png':'desktop.png'}],'coverage_complete':True,'stable':True}
        manifest['hashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in evidence.iterdir()}
        save_json(evidence/'visual-manifest.json',manifest)
        decision={'status':'passed','checked_frames':['desktop'],'leaked_mobile_roots':[],'missing_visual_sections':[],'uncertainties':[],'summary':'consistent'}
        return manifest,decision

    def execute(self, root, manifest, decision):
        categories={'Menu':'https://example.com/collections/all'}
        proof={'roots':[{'label':'Menu','kind':'menu','links':[]}]}
        def finish(*args,**kwargs):
            save_json(root/'visual_decision.json',decision)
        with patch('core.navigation_guard.capture_visual_snapshots',return_value=manifest),patch('core.visual_audit.shutil.which',return_value='codex.exe'),patch('core.visual_audit.subprocess.Popen') as popen:
            popen.return_value.returncode=0
            popen.return_value.communicate.side_effect=finish
            result=run_visual_audit(root,'https://example.com',categories,proof)
            args=popen.call_args.args[0]
            self.assertIn('read-only',args)
            self.assertIn('model_reasoning_effort=medium',args)
            self.assertIn(str((root/'evidence/desktop.png').resolve()),args)
            self.assertEqual(args.count('-i'),1)
            self.assertLessEqual(popen.return_value.communicate.call_args.kwargs['timeout'],90)
        return result,categories

    def test_pass_binds_images_and_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,decision=self.fixture(root)
            result,categories=self.execute(root,manifest,decision)
            self.assertTrue(result['passed'])
            require_visual_audit(root,categories)
            with self.assertRaises(CrawlError):require_visual_audit(root,{'Other':next(iter(categories.values()))})
            (root/'evidence/desktop.png').write_bytes(b'changed')
            with self.assertRaises(CrawlError):require_visual_audit(root,categories)

    def test_injected_mobile_menu_blocks_without_deleting_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,decision=self.fixture(root)
            decision['leaked_mobile_roots']=[{'label':'Menu','frame_id':'desktop','node_ids':['node-1'],'reason':'hidden mobile duplicate'}]
            with self.assertRaises(CrawlError):self.execute(root,manifest,decision)
            self.assertEqual(read_json(root/'visual_audit.json')['status'],'needs_repair')
            self.assertIn('Menu',read_json(root/'visual_candidate.json'))
            self.assertEqual(read_json(root/'navigation_release.json')['status'],'blocked')
            self.assertFalse((root/'url.json').exists())

    def test_incomplete_coverage_never_calls_ai(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,decision=self.fixture(root);manifest['coverage_complete']=False
            with patch('core.navigation_guard.capture_visual_snapshots',return_value=manifest),patch('core.visual_audit.subprocess.Popen') as popen:
                with self.assertRaises(CrawlError):run_visual_audit(root,'https://example.com',{}, {'roots':[]})
                popen.assert_not_called()
            self.assertEqual(read_json(root/'visual_audit.json')['status'],'inconclusive')

    def test_forged_nodes_and_unreviewed_frames_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,decision=self.fixture(root)
            decision['checked_frames']=[]
            with self.assertRaises(CrawlError):assess(decision,manifest,['Menu'],root/'evidence')
            decision['checked_frames']=['desktop']
            decision['missing_visual_sections']=[{'label':'Shop','frame_id':'desktop','node_ids':['fake'],'reason':'missing'}]
            with self.assertRaises(CrawlError):assess(decision,manifest,['Menu'],root/'evidence')

    def test_uncertain_desktop_drawer_is_not_deleted_or_passed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,decision=self.fixture(root)
            decision['uncertainties']=['Menu may be a legitimate desktop drawer']
            self.assertEqual(assess(decision,manifest,['Menu'],root/'evidence'),'inconclusive')

    def test_frame_pairs_html_png_and_dom(self):
        with tempfile.TemporaryDirectory() as d:
            page=MagicMock();node={'id':'n','text':'Shop','href':'/collections/all','bbox':{},'in_viewport':True}
            page.evaluate.side_effect=[[node],{'url':'https://example.com'},[node]]
            page.screenshot.side_effect=lambda **kw:Path(kw['path']).write_bytes(b'png')
            with patch('core.navigation_guard.stable_page_content',return_value='<nav>Shop</nav>'):
                result=frame(page,Path(d),'home-menu-001')
            self.assertTrue(result['stable'])
            for k in ('html','png','dom'):self.assertTrue((Path(d)/result[k]).exists())

    def test_capture_uses_desktop_market_and_reports_coverage(self):
        with tempfile.TemporaryDirectory() as d,patch('playwright.sync_api.sync_playwright') as playwright,patch('core.navigation_guard.wait_for_storefront'):
            browser=playwright.return_value.__enter__.return_value.chromium.launch.return_value
            page=browser.new_context.return_value.new_page.return_value
            page.url='https://example.com/'
            page.evaluate.return_value=900
            def fake_frame(page,out,name,full_page=False):
                record={'id':name,'html':name+'.html','png':name+'.png','dom':name+'.json','state':{'scroll_y':0,'height':900},'stable':True}
                for k in ('html','png','dom'):(Path(out)/record[k]).write_text('fixture')
                return record
            with patch('core.visual_capture.frame',side_effect=fake_frame):manifest=capture_visual_snapshots(page.url,d)
            self.assertTrue(manifest['coverage_complete'])
            self.assertEqual(browser.new_context.call_args.kwargs,{'viewport':{'width':1440,'height':1000},'locale':'en-US'})
            cookies=browser.new_context.return_value.add_cookies.call_args.args[0]
            self.assertEqual({c['value'] for c in cookies},{'US','USD'})
            self.assertEqual(len(manifest['hashes']),9)
            browser.close.assert_called_once()

    def test_missing_audit_blocks_publish_before_http(self):
        from core.navigation_release import publish
        with tempfile.TemporaryDirectory() as d:
            client=MagicMock()
            with self.assertRaises(Exception):publish(d,{}, {}, [],'https://example.com',client,visual_check=True)
            client.get.assert_not_called()
            self.assertEqual(read_json(Path(d)/'navigation_release.json')['status'],'blocked')

    def test_verified_mobile_copy_is_pruned_and_remaining_tree_published(self):
        from core.navigation_release import publish,require_release
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);manifest,decision=self.fixture(root)
            manifest['base_url']='https://example.com/'
            html='<header><nav hidden class="mobile drawer"><a href="/collections/mobile">Menu</a></nav><nav><a href="/collections/all">Shop</a></nav></header>'
            (root/'evidence/desktop.html').write_text(html)
            (root/'evidence/home.html').write_text(html)
            node={'id':'node-1','url':'https://example.com/collections/mobile','rendered':False,'in_header':True,'in_viewport':False,
                  'ancestors':[{'id':'mobile','class':'mobile drawer','display':'none'}]}
            save_json(root/'evidence/desktop.json',{'nodes':[node]})
            manifest['hashes']={name:hashlib.sha256((root/'evidence'/name).read_bytes()).hexdigest() for name in ('desktop.html','desktop.png','desktop.json')}
            save_json(root/'evidence/visual-manifest.json',manifest)
            categories={'Menu':'https://example.com/collections/mobile','Shop':'https://example.com/collections/all'}
            proof={'version':4,'method':'dom_tree_static','complete':True,'roots':[
                {'label':k,'url':v,'kind':'menu','links':[{'labels':[k],'url':v}]} for k,v in categories.items()]}
            decision['leaked_mobile_roots']=[{'label':'Menu','frame_id':'desktop','node_ids':['node-1'],'reason':'hidden mobile branch'}]
            def finish(*a,**kw):save_json(root/'visual_decision.json',decision)
            with patch('core.navigation_guard.capture_visual_snapshots',return_value=manifest),patch('core.visual_audit.shutil.which',return_value='codex.exe'),patch('core.visual_audit.subprocess.Popen') as popen:
                popen.return_value.returncode=0;popen.return_value.communicate.side_effect=finish
                audit=run_visual_audit(root,manifest['base_url'],categories,proof)
            self.assertEqual(audit['auto_pruned_roots'],['Menu'])
            self.assertEqual(list(categories),['Shop'])
            with patch('core.navigation_release._target',return_value={'url':categories['Shop'],'passed':True,'files':[]}):
                release=publish(root,categories,proof,[html],manifest['base_url'],MagicMock(),visual_check=True)
            self.assertEqual(release['status'],'approved')
            self.assertEqual(require_release(root)['status'],'approved')

    def test_main_carousel_moves_but_header_capture_stays_valid(self):
        with tempfile.TemporaryDirectory() as d:
            page=MagicMock()
            header={'id':'h','text':'Shop','href':'/collections/all','bbox':{'x':0},'in_viewport':True,'in_header':True}
            card={'id':'c','text':'Banner 1','href':'/collections/new','bbox':{'x':0},'in_viewport':True,'in_header':False}
            page.evaluate.side_effect=[[header,card],{'url':'https://example.com'},[header,dict(card,text='Banner 2',bbox={'x':200})]]
            page.screenshot.side_effect=lambda **kw:Path(kw['path']).write_bytes(b'png')
            with patch('core.navigation_guard.stable_page_content',return_value='<header>Shop</header>'):
                result=frame(page,Path(d),'home-desktop')
            self.assertTrue(result['stable']);self.assertEqual(result['attempts'],1)

    def test_header_changes_still_block_after_bounded_recapture(self):
        with tempfile.TemporaryDirectory() as d:
            page=MagicMock();node={'id':'h','text':'Shop','href':'/collections/all','bbox':{'x':0},'in_viewport':True,'in_header':True}
            page.evaluate.side_effect=([[node],{'url':'https://example.com'},[dict(node,text='Changed')]])*3
            page.screenshot.side_effect=lambda **kw:Path(kw['path']).write_bytes(b'png')
            with patch('core.navigation_guard.stable_page_content',return_value='<header>Shop</header>'):
                result=frame(page,Path(d),'home-desktop')
            self.assertFalse(result['stable']);self.assertEqual(page.screenshot.call_count,3)

