import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from core.pipeline import run_job
from core.ai_worker import run_once
from core.common import CrawlError, save_json
from core.validator import verify_source_mapping
from test_core import product, BASE

HOME='<script>Shopify.country="US";Shopify.currency={"active":"USD"};</script><nav><li><a href="/collections/shop">Shop</a><ul><li><a href="/collections/other">Other</a></li></ul></li></nav>'
GRID='<main id="product-grid">'+''.join(f'<a href="/products/p{i}">P{i}</a>' for i in range(1,4))+'</main>'
class Response:
 def __init__(self,url,text):self.url=url;self.text=text
 def json(self):return json.loads(self.text)
class FakeClient:
 def get(self,url):return Response(url,HOME if url.rstrip('/')==BASE else GRID)
 def json(self,url):
  if '/products/p' in url:
   return {'product':product(int(url.split('/products/p')[1].split('.')[0]))}
  return {'products':[product(i) for i in range(1,4)]}

class WorkflowTests(unittest.TestCase):
 def test_image_transform_urls_keep_literal_commas(self):
  original_product = product
  def transformed_product(index):
   result = original_product(index)
   result['images'] = [{'src': 'https://cdn.example.com/f_auto,q_auto,w_1500/product.jpg'}]
   return result
  with tempfile.TemporaryDirectory() as directory, patch('core.pipeline.Client', FakeClient), patch('test_workflow.product', side_effect=transformed_product):
   report = run_job(BASE, directory, navigation_mode='deterministic')
   self.assertTrue(report['validation']['passed'])
   from core.validator import validate
   path = Path(directory) / 'result.csv'
   content = path.read_text(encoding='utf-8-sig')
   path.write_text(content.replace('w_1500/product.jpg', 'w_100/product.jpg'), encoding='utf-8-sig')
   with self.assertRaises(CrawlError):
    validate(Path(directory))

 def test_artifact_retry_uses_audited_canonical_base(self):
  from app import artifact_base
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   save_json(root/'navigation_review_scope.json',{'base_url':'https://www.example.com/'})
   self.assertEqual(artifact_base(root,'example.com'),'https://www.example.com/')

 def test_unlinked_parent_keeps_paths_without_invented_url(self):
  from core.navigation_guard import VERSION,from_evidence,check
  proof={'version':VERSION,'complete':True,'method':'browser_visible','roots':[{'label':'Drinkware','url':None,'links':[{'labels':['Drinkware','Bottles'],'url':BASE+'/collections/bottles'},{'labels':['Drinkware','By Size','Large'],'url':BASE+'/collections/large'}]}]}
  a=from_evidence(proof,HOME,BASE)
  self.assertNotIn('Drinkware',a.data);self.assertNotIn('Drinkware,Drinkware By Size',a.data)
  self.assertIn('Drinkware,Drinkware By Size,Drinkware By Size Large',a.data)
  self.assertTrue(all(isinstance(u,str) for u in a.data.values()))
  self.assertTrue(check(a.data,proof,HOME,BASE)['passed'])
 def test_ambiguous_static_auto_browser_recovery(self):
  from core.navigation_guard import static_evidence
  proof=static_evidence(HOME,BASE);proof['method']='browser_visible'
  with tempfile.TemporaryDirectory() as d,patch('core.pipeline.Client',FakeClient),patch('core.pipeline.extract_navigation',side_effect=ValueError('ambiguous owner')),patch('core.navigation_guard.browser_evidence',return_value=(proof,[HOME],HOME)) as browser:
   report=run_job(BASE,d,navigation_mode='deterministic')
   browser.assert_called_once();self.assertTrue(report['validation']['passed'])
   self.assertEqual(json.loads((Path(d)/'navigation_report.json').read_text())['audit_version'],4)
 def test_unresolved_navigation_never_published_or_sent_to_ai(self):
  with tempfile.TemporaryDirectory() as d,patch('core.pipeline.Client',FakeClient),patch('core.pipeline.extract_navigation',side_effect=ValueError('ambiguous owner')),patch('core.navigation_guard.browser_evidence',side_effect=CrawlError('drawer did not open')),patch('core.ai_worker.run_once') as ai:
   with self.assertRaises(CrawlError):run_job(BASE,d,navigation_mode='deterministic')
   ai.assert_not_called()
   self.assertFalse((Path(d)/'url.json').exists());self.assertFalse((Path(d)/'crawler.zip').exists())
   self.assertEqual(json.loads((Path(d)/'navigation_diagnostic.json').read_text())['status'],'blocked')
 def test_independent_evidence_rejects_flattened_and_missing_entries(self):
  from core.navigation_guard import static_evidence,check
  proof=static_evidence(HOME,BASE)
  for bad in [{'Shop':BASE+'/collections/shop','Other':BASE+'/collections/other'},{'Shop':BASE+'/collections/shop'}]:
   with self.assertRaises(CrawlError):check(bad,proof,HOME,BASE)
 def test_shared_url_does_not_hide_missing_branch(self):
  from core.navigation_guard import check
  proof={'version':4,'complete':True,'method':'browser_visible','roots':[{'label':'Shop','url':BASE+'/collections/all','links':[{'labels':['Shop'],'url':BASE+'/collections/all'},{'labels':['Shop','Clothing','Featured'],'url':BASE+'/collections/featured'},{'labels':['Shop','Gifts','Featured'],'url':BASE+'/collections/featured'}]}]}
  bad={'Shop':BASE+'/collections/all','Shop,Shop Clothing,Shop Clothing Featured':BASE+'/collections/featured'}
  with self.assertRaises(CrawlError):check(bad,proof,HOME,BASE)
 def test_group_uses_observed_aggregator_and_dom_order(self):
  from core.navigation_guard import from_evidence,check
  proof={'version':4,'complete':True,'method':'browser_visible','roots':[{'label':'Shop','url':BASE+'/collections/all','links':[{'labels':['Shop'],'url':BASE+'/collections/all'},{'labels':['Shop','Sale'],'url':BASE+'/collections/sale','order':[9]},{'labels':['Shop','Boots','Shop All Boots'],'url':BASE+'/collections/boots','order':[2,1]}]}]}
  a=from_evidence(proof,HOME,BASE)
  self.assertEqual(a.data['Shop,Shop Boots'],BASE+'/collections/boots')
  self.assertEqual(list(a.data)[1],'Shop,Shop Boots')
  self.assertTrue(check(a.data,proof,HOME,BASE)['passed'])
 def test_auto_zip_no_cli_and_offline_validation(self):
  with tempfile.TemporaryDirectory() as d,patch('core.pipeline.Client',FakeClient),patch('core.ai_worker.run_once') as ai:
   report=run_job(BASE,d,navigation_mode='deterministic')
   self.assertEqual(report['llm_invocations'],0);ai.assert_not_called()
   with zipfile.ZipFile(Path(d)/'crawler.zip') as z:
    self.assertTrue({'url.json','detail_url.json','3.ShopifyCrawl_sort.py','get_detail_url.py','samples.json','result.csv','core/common.py'}.issubset(z.namelist()))
   self.assertTrue(report['validation']['passed'])
 def test_manual_wait_and_confirm(self):
  with tempfile.TemporaryDirectory() as d,patch('core.pipeline.Client',FakeClient):
   waiting=threading.Event();confirm=threading.Event();errors=[]
   def emit(e):
    if e['stage']=='awaiting_confirmation':waiting.set()
   def execute():
    try:run_job(BASE,d,navigation_mode='deterministic',manual=True,confirm=confirm,emit=emit)
    except Exception as e:errors.append(e)
   t=threading.Thread(target=execute);t.start()
   try:
    self.assertTrue(waiting.wait(5));self.assertFalse((Path(d)/'crawler.zip').exists())
    self.assertTrue((Path(d)/'samples.json').exists())
   finally:
    confirm.set();t.join(5)
   self.assertFalse(errors);self.assertFalse(t.is_alive());self.assertTrue((Path(d)/'crawler.zip').is_file())
 def test_ai_exactly_once_even_on_failure(self):
  with tempfile.TemporaryDirectory() as d,patch('core.ai_worker.shutil.which',return_value='codex.exe'),patch('core.ai_worker.subprocess.Popen') as popen:
   proc=MagicMock();proc.returncode=1;popen.return_value=proc
   with self.assertRaises(CrawlError):run_once(d,'test')
   with self.assertRaises(CrawlError):run_once(d,'test again')
   self.assertEqual(popen.call_count,1)
 def test_custom_raw_mapping(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p=product(1)
   raw={'data':{'id':1,'name':p['title'],'skus':[{'sku':10,'price':'12.50','was':'20.00','color':'Red','size':'M'}],'images':[{'url':'https://cdn.example.com/red.jpg'}]}}
   save_json(root/'evidence/raw.json',raw)
   s={'url':BASE+'/item1','product':p,'source_mapping':{'file':'evidence/raw.json','product_fields':{'id':'/data/id','title':'/data/name'},'variants_path':'/data/skus','variant_fields':{'id':'/sku','price':'/price','compare_at_price':'/was','option1':'/color','option2':'/size'},'image_paths':['/data/images/0/url']}}
   verify_source_mapping(root,s)
   p['variants'][0]['price']='999'
   with self.assertRaises(CrawlError):verify_source_mapping(root,s)

if __name__=='__main__':unittest.main()

class AINavigationWorkflowTests(unittest.TestCase):
 def test_explicit_ai_directory_uses_existing_product_pipeline(self):
  from core.navigation_guard import static_evidence
  from core.nav_extractor import extract_navigation
  proof=static_evidence(HOME,BASE);proof['method']='ai_skill';proof['review']={'skill':'category-json','complete':True,'unresolved':[],'branches':['Shop']}
  def directory(root,base,emit=None):
   save_json(Path(root)/'navigation_invocation.json',{'invocations':1})
   return extract_navigation(HOME,BASE).data,proof,[HOME]
  with tempfile.TemporaryDirectory() as d,patch('core.pipeline.Client',FakeClient),patch('core.navigation_ai.run_navigation',side_effect=directory) as nav,patch('core.navigation_guard.browser_evidence') as legacy:
   report=run_job(BASE,d,navigation_mode='ai')
   nav.assert_called_once();legacy.assert_not_called();self.assertEqual(report['llm_invocations'],1);self.assertTrue(report['validation']['passed'])
 def test_incomplete_ai_review_cannot_publish(self):
  from core.navigation_guard import check,static_evidence
  proof=static_evidence(HOME,BASE);proof['method']='ai_skill'
  with self.assertRaises(CrawlError):check({'Shop':BASE+'/collections/shop'},proof,HOME,BASE)
