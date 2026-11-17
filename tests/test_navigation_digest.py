import json
import tempfile
import unittest
from pathlib import Path
from core.common import save_json,read_json,CrawlError
from core.navigation_digest import build_digest,candidate_proof


class DigestTests(unittest.TestCase):
    def test_windows_newlines_do_not_invalidate_unchanged_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.seed(root)
            path=root/'evidence/home-menu-001.html';path.write_bytes(path.read_bytes()+b'\r\n')
            digest=build_digest(root,'https://example.com/')
            entries=[{'labels':['MEN','New Arrivals'],'url_id':digest['urls'].index('/collections/mens-new')},
                     {'labels':['WOMEN','New Arrivals'],'url_id':digest['urls'].index('/collections/womens-new')},
                     {'labels':['Gifts'],'url_id':digest['urls'].index('/collections/gifts')}]
            self.assertEqual(len(candidate_proof(root,entries)[0]),3)

    def test_footer_duplicate_does_not_hide_newly_opened_menu_ownership(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'evidence').mkdir()
            link={'text':"Women's Apparel",'url':'https://example.com/collections/apparel'}
            initial='<header><button>Women</button></header><footer><a href="/collections/apparel">Women\'s Apparel</a></footer>'
            opened='<header><button>Women</button><div><h3>Apparel</h3><a href="/collections/apparel">Women\'s Apparel</a></div></header>'
            (root/'evidence/home-desktop.html').write_text(initial,encoding='utf-8');(root/'evidence/home-menu-001.html').write_text(opened,encoding='utf-8')
            save_json(root/'evidence/nav-prefetch.json',{'complete':True,'frames':[{'html':'home-desktop.html','visible_links':[link]}, {'html':'home-menu-001.html','owner':'Women','visible_links':[link,link]}]})
            save_json(root/'navigation_review_scope.json',{'base_url':'https://example.com/','confirmed_candidate':{},'issues':[]})
            digest=build_digest(root,'https://example.com/')
            candidate,_,_=candidate_proof(root,[{'labels':['Women','Apparel',"Women's Apparel"],'url_id':0}])
            self.assertIn("Women,Women Apparel,Women Apparel Women's Apparel",candidate)

    def seed(self,root):
        (root/'evidence').mkdir()
        html='<script>'+('irrelevantJS'*30000)+'</script><style>.junk{}</style><header><nav><ul><li><button aria-controls="men">MEN</button><div id="men"><h3>New</h3><a href="/collections/mens-new">New Arrivals</a></div></li><li><button>WOMEN</button><div><h3>New</h3><a href="/collections/womens-new">New Arrivals</a></div></li></ul></nav><svg><text>Vector noise</text></svg></header><main><a href="/collections/gifts">Gifts</a><a href="/products/sample">Sample</a></main><footer><a href="/collections/footer-only">Footer</a></footer>'
        (root/'evidence/home-menu-001.html').write_text(html,encoding='utf-8')
        save_json(root/'navigation_review_scope.json',{'base_url':'https://example.com/','confirmed_candidate':{},'issues':[]})
        return build_digest(root,'https://example.com/')

    def test_digest_removes_noise_without_losing_gender_ownership(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);digest=self.seed(root)
            data=(root/'evidence/nav_digest.json').read_text(encoding='utf-8')
            self.assertLess(len(data.encode()),5000)
            for noise in ('irrelevantJS','Vector noise','footer-only','/products/'):
                self.assertNotIn(noise,data)
            entries=[{'labels':['MEN','New','New Arrivals'],'url_id':digest['urls'].index('/collections/mens-new')},
                     {'labels':['WOMEN','New','New Arrivals'],'url_id':digest['urls'].index('/collections/womens-new')},
                     {'labels':['Gifts'],'url_id':digest['urls'].index('/collections/gifts')}]
            candidate,proof,decision=candidate_proof(root,entries)
            self.assertEqual(len(candidate),3)
            self.assertEqual(len(proof['roots']),3)
            self.assertTrue(decision['complete'])

    def test_forged_url_owner_and_confirmed_edit_are_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);digest=self.seed(root)
            bad=[{'labels':['MEN','New Arrivals'],'url_id':999}]
            with self.assertRaises(CrawlError):candidate_proof(root,bad)
            bad=[{'labels':['WOMEN','New Arrivals'],'url_id':digest['urls'].index('/collections/mens-new')}]
            with self.assertRaises(CrawlError):candidate_proof(root,bad)
            scope=read_json(root/'navigation_review_scope.json');scope['confirmed_candidate']={'MEN,MEN New Arrivals':'https://example.com/collections/mens-new'};save_json(root/'navigation_review_scope.json',scope)
            bad=[{'labels':['MEN','New Arrivals'],'url_id':digest['urls'].index('/collections/womens-new')}]
            with self.assertRaises(CrawlError):candidate_proof(root,bad)

    def test_changed_raw_html_invalidates_digest(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.seed(root)
            (root/'evidence/home-menu-001.html').write_text('<header/>')
            with self.assertRaises(CrawlError):candidate_proof(root,[])
