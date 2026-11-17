import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from core.hybrid_navigation import prepare_evidence
from core.common import read_json


class HybridTests(unittest.TestCase):
    def setUp(self):
        mocked=patch('core.navigation_prefetch.prefetch_navigation',return_value={'complete':True,'frames':[]})
        mocked.start();self.addCleanup(mocked.stop)

    def test_hidden_static_dom_fastpath_skips_browser_and_confirms_home(self):
        html='''<style>.mega-menu{display:none}</style><header><nav><ul><li class="header__linklist-item"><a href="/collections/games">Games</a><div class="mega-menu" hidden><div class="mega-menu__column"><h3>Collections</h3><a href="/collections/party">Party Games</a><a href="/collections/games">View All</a></div></div></li></ul></nav></header><main><div class="collection-card"><a href="/collections/bundles">Bundles</a></div></main>'''
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'evidence').mkdir();(root/'evidence/home.html').write_text(html,encoding='utf-8')
            with patch('core.hybrid_navigation.browser_evidence') as browser:
                result=prepare_evidence(root,'https://x/')
            browser.assert_not_called()
            self.assertTrue(result['program_expansion_complete'])
            proof=read_json(root/'evidence/program_navigation.json')
            self.assertEqual(proof['method'],'owned_menu_tree')
            from core.menu_tree import verify_sources
            verify_sources(proof, html, 'https://x/')
            scope=read_json(root/'navigation_review_scope.json')
            self.assertFalse(scope['issues'])
            self.assertFalse(scope['uncertain_candidate'])
            self.assertEqual(scope['confirmed_candidate']['bundles'],'https://x/collections/bundles')

    def test_program_candidate_is_review_input_not_publication(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'evidence').mkdir();(root/'evidence/home.html').write_text('<header><a href="/collections/all">Shop</a></header>',encoding='utf-8')
            proof={'complete':True,'roots':[{'label':'Shop','links':[{'labels':['Shop','Boots'],'url':'https://x/collections/boots'}]}]};html='<nav><a href="/collections/boots">Boots</a></nav>'
            with patch('core.hybrid_navigation.extract_navigation',return_value=SimpleNamespace(data={},warnings=[])),patch('core.hybrid_navigation.browser_evidence',return_value=(proof,[html],html)),patch('core.hybrid_navigation.from_evidence',return_value=SimpleNamespace(data={'Shop,Shop Boots':'https://x/collections/boots'})):
                result=prepare_evidence(root,'https://x')
            self.assertTrue(result['program_expansion_complete'])
            self.assertFalse((root/'url.json').exists())
            self.assertEqual((root/'evidence/home-menu-000.html').read_text(encoding='utf-8'),html)
            self.assertEqual(read_json(root/'program_candidate.json'),{'Shop,Shop Boots':'https://x/collections/boots'})
            scope=read_json(root/'navigation_review_scope.json')
            self.assertEqual(scope['confirmed_candidate'],{'Shop,Shop Boots':'https://x/collections/boots'})
            self.assertEqual(scope['uncertain_candidate'],{})

    def test_failure_keeps_partial_evidence_for_ai(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'evidence').mkdir();(root/'evidence/home.html').write_text('<header/>',encoding='utf-8')
            (root/'evidence/menu-observation-1.html').write_text('<nav>partial</nav>',encoding='utf-8')
            with patch('core.hybrid_navigation.extract_navigation',return_value=SimpleNamespace(data={},warnings=['ambiguous parent'])),patch('core.hybrid_navigation.browser_evidence',side_effect=RuntimeError('drawer unresolved')):
                result=prepare_evidence(root,'https://x')
            self.assertFalse(result['program_expansion_complete'])
            self.assertTrue(any('drawer unresolved' in s for s in result['issues']))
            self.assertTrue((root/'evidence/home-menu-000.html').exists())
            self.assertFalse((root/'url.json').exists())
            scope=read_json(root/'navigation_review_scope.json')
            self.assertEqual(scope['confirmed_candidate'],{})
            self.assertTrue(scope['issues'])

    def test_browser_path_normalization_failure_cannot_remain_complete(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'evidence').mkdir();(root/'evidence/home.html').write_text('<header/>',encoding='utf-8')
            proof={'version':4,'method':'browser_visible','complete':True,
                   'roots':[{'label':'Shop','links':[{'labels':['Shop'],'url':'https://x/collections/all'}]}]}
            with patch('core.hybrid_navigation.extract_navigation',return_value=SimpleNamespace(data={},warnings=[])),patch('core.hybrid_navigation.browser_evidence',return_value=(proof,['<nav/>'],'<nav/>')),patch('core.hybrid_navigation.from_evidence',side_effect=ValueError('conflicting paths')):
                result=prepare_evidence(root,'https://x')
            self.assertFalse(result['program_expansion_complete'])
            self.assertTrue(result['issues'])
            saved=read_json(root/'evidence/program_navigation.json')
            self.assertFalse(saved['complete'])
            self.assertEqual(read_json(root/'program_candidate.json'),{})

    def test_duplicate_level_is_uncertain(self):
        from core.hybrid_navigation import _review_scope
        with tempfile.TemporaryDirectory() as d:
            evidence=Path(d);(evidence/'home.html').write_text('<a href="/collections/bikes">Bikes</a>',encoding='utf-8')
            proof={'complete':True,'roots':[{'label':'Bikes','links':[{'labels':['Bikes','Bikes'],'url':'https://x/collections/bikes'}]}]}
            scope=_review_scope({'Bikes,Bikes Bikes':'https://x/collections/bikes'},proof,[],None,evidence)
            self.assertEqual(scope['confirmed_candidate'],{})
            self.assertIn('Bikes,Bikes Bikes',scope['uncertain_candidate'])
            self.assertTrue(any(i['kind']=='structural_label' for i in scope['issues']))

    def test_partial_failure_preserves_root_with_complete_coverage(self):
        from core.hybrid_navigation import _review_scope
        with tempfile.TemporaryDirectory() as d:
            evidence=Path(d);(evidence/'home-menu-001.html').write_text('<a href="/collections/all">Shop</a>',encoding='utf-8')
            proof={'complete':False,'roots':[{'label':'Shop','links':[{'labels':['Shop'],'url':'https://x/collections/all'}],'submenu_coverage':{'expected':[{'leafId':'a'}],'observed_ids':['a']}}]}
            scope=_review_scope({'Shop':'https://x/collections/all'},proof,[],'later root failed',evidence)
            self.assertEqual(scope['confirmed_candidate'],{'Shop':'https://x/collections/all'})
            self.assertEqual(scope['uncertain_candidate'],{})

    def test_policy_contains_no_domain_exceptions(self):
        policy=read_json(Path(__file__).parents[1]/'core/navigation_policy.json')
        text=str(policy).lower()
        self.assertNotIn('.com',text)
        self.assertNotIn('himalaya',text)
