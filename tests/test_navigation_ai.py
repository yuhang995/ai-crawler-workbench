import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch,MagicMock
from core.navigation_ai import run_navigation,validate_review_scope
from core.common import CrawlError,save_json,read_json

class NavigationAITests(unittest.TestCase):
 def test_navigation_timeout_default_environment_and_explicit(self):
  from core.navigation_ai import navigation_timeout
  with patch.dict('os.environ',{},clear=True):self.assertEqual(navigation_timeout(),180)
  with patch.dict('os.environ',{'NAVIGATION_TIMEOUT_SECONDS':'900'}):
   self.assertEqual(navigation_timeout(),180)
   self.assertEqual(navigation_timeout(1200),180)
  for invalid in ('nan','',0,-1,True):
   with self.subTest(invalid=invalid),self.assertRaises(CrawlError):navigation_timeout(invalid)

 def test_navigation_and_product_cli_share_one_semaphore(self):
  from core.navigation_ai import AI_SEMAPHORE as navigation_semaphore
  from core.ai_worker import AI_SEMAPHORE as product_semaphore
  self.assertIs(navigation_semaphore,product_semaphore)

 def test_decision_schema_uses_only_strict_output_supported_keywords(self):
  schema=json.loads((Path('core')/'navigation_decision_schema.json').read_text(encoding='utf-8'))
  serialized=json.dumps(schema)
  self.assertNotIn('uniqueItems',serialized)
  self.assertNotIn('"const"',serialized)
  self.assertNotIn('"$schema"',serialized)

 def test_one_session_marker_and_workspace_command(self):
  with tempfile.TemporaryDirectory() as d:
   skill=Path(d)/'fake-category-json';skill.mkdir();(skill/'SKILL.md').write_text('# test skill',encoding='utf-8')
   def prepare(root,base):
    evidence=Path(root)/'evidence';evidence.mkdir()
    (evidence/'home.html').write_text('<header><nav><a href="/collections/all">Shop</a></nav></header>',encoding='utf-8')
    (evidence/'home-desktop.png').write_bytes(b'png')
    save_json(Path(root)/'navigation_review_scope.json',{'program_complete':False,'confirmed_candidate':{},'uncertain_candidate':{},'issues':[{'id':'U001'}]})
   with patch('core.hybrid_navigation.prepare_evidence',side_effect=prepare),patch('core.navigation_ai.SKILL',skill),patch('core.visual_audit.fallback_images',return_value=[Path(d)/'desktop.png']),patch('core.navigation_ai.shutil.which',return_value='codex.exe'),patch('core.navigation_ai.subprocess.Popen') as popen,patch.dict('os.environ',{},clear=True):
    process=MagicMock();process.returncode=1;popen.return_value=process
    with self.assertRaises(CrawlError):run_navigation(d,'https://test.invalid',timeout=999)
    args=popen.call_args.args[0]
    self.assertEqual(popen.call_args.kwargs['env']['PYTHONUTF8'],'1')
    self.assertIn('-i',args);self.assertTrue(any(str(arg).endswith('screen-0.png') for arg in args))
    self.assertIn('read-only',args);self.assertIn('shell_tool',args);self.assertIn('browser_use',args)
    self.assertIn('gpt-5.6-sol',args)
    self.assertIn('model_reasoning_effort=medium',args);self.assertIn('--output-schema',args)
    self.assertLessEqual(process.communicate.call_args.kwargs['timeout'],180)
    self.assertTrue((Path(d)/'.agents/skills/category-json/SKILL.md').is_file())
    self.assertNotIn('browser_tool.py',(Path(d)/'navigation_prompt.txt').read_text(encoding='utf-8'))
    self.assertTrue((Path(d)/'check_nav.py').is_file())
    self.assertTrue((Path(d)/'.agents/skills/category-json/pitfalls.md').is_file())
    prompt=(Path(d)/'navigation_prompt.txt').read_text(encoding='utf-8')
    self.assertIn('禁止调用任何工具',prompt)
    self.assertIn('nav_digest.json',prompt)
    with self.assertRaises(CrawlError):run_navigation(d,'https://test.invalid')
    self.assertEqual(popen.call_count,1)

 def test_zero_issue_scope_skips_cli_and_missing_default_skill(self):
  candidate={'Shop':'https://test.invalid/collections/all'}
  proof={'version':4,'method':'dom_tree_static','complete':True,'roots':[{'label':'Shop','url':candidate['Shop'],'kind':'menu','links':[{'labels':['Shop'],'url':candidate['Shop'],'model':False}]}]}
  def prepare(root,base):
   root=Path(root);(root/'evidence').mkdir(exist_ok=True)
   (root/'evidence/home.html').write_text('<nav><a href="/collections/all">Shop</a></nav>',encoding='utf-8')
   save_json(root/'program_candidate.json',candidate)
   save_json(root/'evidence/program_navigation.json',proof)
   save_json(root/'navigation_review_scope.json',{'program_complete':True,'confirmed_candidate':candidate,'uncertain_candidate':{},'issues':[]})
   save_json(root/'navigation_preflight.json',{'seconds':0.01})
  with tempfile.TemporaryDirectory() as d,patch('core.hybrid_navigation.prepare_evidence',side_effect=prepare),patch('core.navigation_ai.SKILL',Path(d)/'missing-skill'),patch('core.navigation_ai.shutil.which') as which,patch('core.navigation_ai.subprocess.Popen') as popen:
   loaded,observed,documents=run_navigation(d,'https://test.invalid')
   self.assertEqual(loaded,candidate);self.assertEqual(observed,proof);self.assertEqual(len(documents),1)
   which.assert_not_called();popen.assert_not_called()
   self.assertFalse((Path(d)/'navigation_invocation.json').exists())
   timing=read_json(Path(d)/'navigation_timing.json')
   self.assertEqual(timing['cli_seconds'],0);self.assertEqual(timing['mode'],'deterministic_fastpath');self.assertEqual(timing['llm_invocations'],0)
   reused,_,_=__import__('core.navigation_ai',fromlist=['load_navigation']).load_navigation(d)
   self.assertEqual(reused,candidate)

 def test_comma_labels_are_serialized_without_extra_levels(self):
  from core.navigation_ai import serialize_paths
  proof={'roots':[{'label':'Bakeware','links':[{'labels':['Bakeware','Set, 6-PC'],'url':'https://x/collections/set'}]}]}
  candidate={'Bakeware,Bakeware Set, 6-PC':'https://x/collections/set'}
  result,changes=serialize_paths(candidate,proof)
  self.assertEqual(list(result),['Bakeware,Bakeware Set 6-PC'])
  self.assertEqual(proof['roots'][0]['links'][0]['display_labels'],['Bakeware','Set, 6-PC'])
  self.assertEqual(len(changes),1)
 def test_ai_final_candidate_rejects_page_and_product_urls(self):
  from core.navigation_ai import serialize_paths
  for url in ('https://x/pages/about','https://x/products/widget'):
   proof={'roots':[{'label':'Shop','links':[{'labels':['Shop'],'url':url}]}]}
   with self.assertRaises(CrawlError):serialize_paths({'Shop':url},proof)
 def test_ai_check_does_not_rerun_static_home_inference(self):
  from core.navigation_guard import check
  proof={'version':4,'method':'ai_skill','complete':True,'review':{'complete':True,'unresolved':[],'skill':'category-json','branches':['Shop']},'roots':[{'label':'Shop','url':'https://x/collections/all','links':[{'labels':['Shop'],'url':'https://x/collections/all'}]}]}
  with patch('core.navigation_guard.homepage_entries',side_effect=AssertionError('old inference used')):
   self.assertTrue(check({'Shop':'https://x/collections/all'},proof,'<a href="/collections/all">Shop</a>','https://x')['passed'])

 def test_scope_locks_confirmed_candidate_and_resolves_every_issue(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'evidence').mkdir();(root/'evidence/home-menu-001.html').write_text('<a href="/collections/sale">Sale</a>',encoding='utf-8')
   save_json(root/'navigation_review_scope.json',{'base_url':'https://x/','confirmed_candidate':{'Shop':'https://x/collections/all'},'uncertain_candidate':{'sale':'https://x/collections/sale'},'confirmed_roots':['Shop'],'uncertain_roots':[],'issues':[{'id':'U001'}]})
   save_json(root/'navigation_decision.json',{'version':2,'complete':True,'confirmed_unchanged':True,'resolved_issue_ids':['U001'],'unresolved':[],'reviewed_roots':['Shop'],'summary':'ok'})
   proof={'roots':[{'label':'Shop','links':[{'labels':['Shop'],'url':'https://x/collections/all'}]}],'review':{'scope_version':2,'resolved_issue_ids':['U001'],'confirmed_unchanged':True,'corrections':[{'issue_id':'U001','source_files':['evidence/home-menu-001.html'],'reason':'checked'}]}}
   candidate={'Shop':'https://x/collections/all','sale':'https://x/collections/sale'}
   self.assertTrue(validate_review_scope(root,candidate,proof)['complete'])
   candidate['Shop']='https://x/collections/changed'
   with self.assertRaises(CrawlError):validate_review_scope(root,candidate,proof)

 def test_scope_rejects_new_url_outside_declared_source(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'evidence').mkdir();(root/'evidence/home-menu-001.html').write_text('<a href="/collections/real">Real</a>',encoding='utf-8')
   save_json(root/'navigation_review_scope.json',{'base_url':'https://x/','confirmed_candidate':{},'uncertain_candidate':{'old':'https://x/collections/old'},'confirmed_roots':[],'uncertain_roots':['Shop'],'issues':[{'id':'U001'}]})
   save_json(root/'navigation_decision.json',{'version':2,'complete':True,'confirmed_unchanged':True,'resolved_issue_ids':['U001'],'unresolved':[],'reviewed_roots':['Shop'],'summary':'ok'})
   proof={'roots':[{'label':'Shop','links':[{'labels':['Shop'],'url':'https://x/collections/invented'}]}],'review':{'scope_version':2,'resolved_issue_ids':['U001'],'confirmed_unchanged':True,'corrections':[{'issue_id':'U001','source_files':['evidence/home-menu-001.html'],'reason':'replace'}]}}
   with self.assertRaises(CrawlError):validate_review_scope(root,{'Shop':'https://x/collections/invented'},proof)

 def test_scope_rejects_bad_version_and_duplicate_decision_items(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'evidence').mkdir();(root/'evidence/home-menu-001.html').write_text('<a href="/collections/all">Shop</a>',encoding='utf-8')
   save_json(root/'navigation_review_scope.json',{'base_url':'https://x/','confirmed_candidate':{},'uncertain_candidate':{'Shop':'https://x/collections/all'},'confirmed_roots':[],'uncertain_roots':['Shop'],'issues':[{'id':'U001'}]})
   proof={'roots':[{'label':'Shop','links':[{'labels':['Shop'],'url':'https://x/collections/all'}]}],'review':{'scope_version':2,'resolved_issue_ids':['U001'],'confirmed_unchanged':True,'corrections':[{'issue_id':'U001','source_files':['evidence/home-menu-001.html'],'reason':'checked'}]}}
   base={'version':1,'complete':True,'confirmed_unchanged':True,'resolved_issue_ids':['U001'],'unresolved':[],'reviewed_roots':['Shop'],'summary':'ok'}
   save_json(root/'navigation_decision.json',base)
   with self.assertRaises(CrawlError):validate_review_scope(root,{'Shop':'https://x/collections/all'},proof)
   base['version']=2;base['resolved_issue_ids']=['U001','U001']
   save_json(root/'navigation_decision.json',base)
   with self.assertRaises(CrawlError):validate_review_scope(root,{'Shop':'https://x/collections/all'},proof)

 def test_browser_incomplete_scope_requires_dynamic_snapshot_and_citation(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'evidence').mkdir();(root/'evidence/home.html').write_text('<a href="/collections/all">Shop</a>',encoding='utf-8')
   save_json(root/'navigation_review_scope.json',{'base_url':'https://x/','program_complete':False,'confirmed_candidate':{},'uncertain_candidate':{'Shop':'https://x/collections/all'},'confirmed_roots':[],'uncertain_roots':['Shop'],'issues':[{'id':'U001','kind':'browser_incomplete'}]})
   save_json(root/'navigation_decision.json',{'version':2,'complete':True,'confirmed_unchanged':True,'resolved_issue_ids':['U001'],'unresolved':[],'reviewed_roots':['Shop'],'summary':'ok'})
   candidate={'Shop':'https://x/collections/all'}
   proof={'roots':[{'label':'Shop','links':[{'labels':['Shop'],'url':'https://x/collections/all'}]}],'review':{'scope_version':2,'resolved_issue_ids':['U001'],'confirmed_unchanged':True,'branches':[{'root':'Shop','kind':'menu','source_files':['evidence/home.html']}],'corrections':[{'issue_id':'U001','source_files':['evidence/home.html'],'reason':'static only'}]}}
   with self.assertRaises(CrawlError):validate_review_scope(root,candidate,proof)
   (root/'evidence/home-menu-001.html').write_text('<a href="/collections/all">Shop</a>',encoding='utf-8')
   with self.assertRaises(CrawlError):validate_review_scope(root,candidate,proof)
   proof['review']['corrections'][0]['source_files']=['evidence/home-menu-001.html']
   proof['review']['branches'][0]['source_files']=['evidence/home-menu-001.html']
   self.assertTrue(validate_review_scope(root,candidate,proof)['complete'])

 def test_scope_v2_artifacts_load_end_to_end(self):
  from core.navigation_ai import load_navigation
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'evidence').mkdir();html='<nav><a href="/collections/all">Shop</a></nav>'
   (root/'evidence/home.html').write_text(html,encoding='utf-8');(root/'evidence/home-menu-001.html').write_text(html,encoding='utf-8');(root/'review.md').write_text('confirmed',encoding='utf-8')
   save_json(root/'navigation_invocation.json',{'workflow_version':2})
   save_json(root/'navigation_review_scope.json',{'base_url':'https://x/','confirmed_candidate':{'Shop':'https://x/collections/all'},'uncertain_candidate':{},'confirmed_roots':['Shop'],'uncertain_roots':[],'issues':[]})
   save_json(root/'navigation_decision.json',{'version':2,'complete':True,'confirmed_unchanged':True,'resolved_issue_ids':[],'unresolved':[],'reviewed_roots':['Shop'],'summary':'confirmed'})
   save_json(root/'category_candidate.json',{'Shop':'https://x/collections/all'})
   proof={'version':4,'method':'ai_skill','complete':True,'review':{'complete':True,'unresolved':[],'skill':'category-json','branches':['Shop'],'scope_version':2,'resolved_issue_ids':[],'confirmed_unchanged':True,'corrections':[]},'roots':[{'label':'Shop','url':'https://x/collections/all','links':[{'labels':['Shop'],'url':'https://x/collections/all','model':False}]}]}
   save_json(root/'evidence/navigation-observed.json',proof)
   candidate,loaded,documents=load_navigation(root)
   self.assertEqual(candidate,{'Shop':'https://x/collections/all'});self.assertEqual(loaded['method'],'ai_skill');self.assertEqual(len(documents),2)

 def test_scope_v2_normalizes_top_level_proof_links(self):
  from core.navigation_ai import load_navigation
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'evidence').mkdir();html='<nav><a href="/collections/all">Shop</a></nav>'
   (root/'evidence/home.html').write_text(html,encoding='utf-8');(root/'review.md').write_text('confirmed',encoding='utf-8')
   save_json(root/'navigation_invocation.json',{'workflow_version':2})
   save_json(root/'navigation_review_scope.json',{'base_url':'https://x/','program_complete':True,'confirmed_candidate':{'Shop':'https://x/collections/all'},'uncertain_candidate':{},'confirmed_roots':['Shop'],'uncertain_roots':[],'issues':[]})
   save_json(root/'navigation_decision.json',{'version':2,'complete':True,'confirmed_unchanged':True,'resolved_issue_ids':[],'unresolved':[],'reviewed_roots':['Shop'],'summary':'confirmed'})
   save_json(root/'category_candidate.json',{'Shop':'https://x/collections/all'})
   proof={'version':4,'method':'ai_skill','complete':True,'review':{'complete':True,'unresolved':[],'skill':'category-json','branches':['Shop'],'scope_version':2,'resolved_issue_ids':[],'confirmed_unchanged':True,'corrections':[]},'roots':[{'root':'Shop','label':'Shop','kind':'menu'}],'links':[{'key':'Shop','root':'Shop','kind':'menu','labels':['Shop'],'url':'https://x/collections/all','model':False}]}
   save_json(root/'evidence/navigation-observed.json',proof)
   candidate,loaded,_=load_navigation(root)
   self.assertEqual(candidate,{'Shop':'https://x/collections/all'})
   self.assertEqual(loaded['roots'][0]['links'][0]['labels'],['Shop'])
   self.assertNotIn('links',read_json(root/'evidence/navigation-observed.json'))

 def test_top_level_proof_links_reject_unknown_root(self):
  from core.navigation_ai import normalize_proof_links
  proof={'roots':[{'label':'Shop'}],'links':[{'root':'Other','labels':['Other'],'url':'https://x/collections/other'}]}
  with self.assertRaises(CrawlError):normalize_proof_links(proof)

 def test_top_level_proof_links_reject_key_mismatch_and_mixed_layout(self):
  from core.navigation_ai import normalize_proof_links
  bad_key={'roots':[{'label':'Shop'}],'links':[{'key':'Wrong','root':'Shop','labels':['Shop'],'url':'https://x/collections/all'}]}
  with self.assertRaises(CrawlError):normalize_proof_links(bad_key)
  mixed={'roots':[{'label':'Shop','links':[{'labels':['Shop'],'url':'https://x/collections/all'}]}],'links':[{'root':'Shop','labels':['Shop','Sale'],'url':'https://x/collections/sale'}]}
  with self.assertRaises(CrawlError):normalize_proof_links(mixed)

 def test_dynamic_snapshot_restores_visible_column_heading(self):
  from core.navigation_ai import restore_snapshot_groups
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'evidence').mkdir()
   html='''<header><div class="desktop mega-menu-column"><div class="child-item"><a href="/">By Collection</a></div><ul class="grandchild-list"><li><a href="/collections/playmates">Playmate</a></li></ul></div><div class="mobile drawer"><div class="column"><div><a href="/">Wrong Mobile Group</a></div><ul class="grandchild-list"><li><a href="/collections/playmates">Playmate®</a></li></ul></div></div></header>'''
   (root/'evidence/home-menu-hard.html').write_text(html,encoding='utf-8')
   candidate={'Hard Coolers,Hard Coolers Playmate®':'https://x/collections/playmates'}
   proof={'roots':[{'label':'Hard Coolers','source_files':['evidence/home-menu-hard.html'],'links':[{'labels':['Hard Coolers','Playmate®'],'url':'https://x/collections/playmates'}]}]}
   candidate,proof,changes=restore_snapshot_groups(root,candidate,proof,'https://x/')
   expected='Hard Coolers,Hard Coolers By Collection,Hard Coolers By Collection Playmate®'
   self.assertEqual(candidate,{expected:'https://x/collections/playmates'})
   self.assertEqual(proof['roots'][0]['links'][0]['labels'],['Hard Coolers','By Collection','Playmate®'])
   self.assertEqual(len(changes),1)
