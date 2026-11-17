"""Browser regression with local, deterministic menus (no external website)."""
import threading
import unittest
from unittest.mock import MagicMock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from core.navigation_guard import browser_evidence,from_evidence,check,linked_static_evidence,dom_tree_static_evidence
from core.common import CrawlError
from core.navigation_browser_tool import stable_page_content as tool_stable_page_content

GOOD = """<header><button data-nav="factory"><a href="/pages/usa-factory">USA Factory</a></button><a href="#" aria-label="Wishlist" aria-haspopup="dialog"><span style="display:none">0</span></a><a href="/cart" aria-haspopup="dialog">0</a><nav><div><button aria-expanded="false" aria-controls="panel" onclick="let p=document.getElementById('panel');p.hidden=!p.hidden;this.setAttribute('aria-expanded',String(!p.hidden))">Bedding</button><div id="panel" hidden><h3>Bedding</h3><a href="/collections/bedding">Shop All</a><a href="/collections/sheets">Sheets</a><a href="/collections/logo" aria-label="NCAA"><img width="20" height="20" alt=""></a><a href="/collections/blankets"><span>NEW!</span><span class="card-title">Blankets</span><p>9 colors, 3 sizes</p></a></div></div></nav></header><main></main>"""
LINKED = """<header><nav><button aria-haspopup="dialog" data-open-drawer="drinkware" onclick="document.getElementById('drawer').hidden=false">Drinkware</button><button aria-haspopup="dialog" data-open-drawer="collections" onclick="document.getElementById('drawer').hidden=false">Collections</button></nav><div id="drawer" role="menu" hidden><a href="/collections/sibling">Sibling outside bound section</a><a href="/pages/vcard">Page excluded</a><section data-menu-handle="drinkware"><a href="/collections/bottles">Universal Buddy</a></section><section data-menu-handle="collections"><a href="/collections/college">NCAA</a></section></div></header><footer><nav><a href="/collections/bottles">Universal Buddy</a></nav></footer>"""

HOVER = """<style>.submenu{display:none}[aria-haspopup]:hover>.submenu{display:block}</style><header><nav><div aria-haspopup="true"><a href="#">Shop By</a><div class="submenu"><div aria-haspopup="true"><a href="/pages/skin">Skin Concern</a><div class="submenu"><a href="/collections/dry">Dryness</a><a href="/collections/acne">Acne</a></div></div></div></div></nav></header>"""

SECTION = """<header><nav><button aria-controls="panel" aria-expanded="false" onclick="document.getElementById('panel').hidden=false;this.setAttribute('aria-expanded','true')">Bikes</button><section id="panel" hidden><div class="section-header"><p class="section-header-title">Bikes</p><a href="/collections/all">Shop Kids Bikes</a><a href="/products/adult-bike">Shop Adult Bikes</a></div><a href="/products/balance">Balance</a></section></nav></header>"""

FLYOUT = """<style>dropdown-menu{display:block}.dropdown-panel{display:none}dropdown-menu:hover>.dropdown-panel{display:block}</style><header><nav><dropdown-menu><a href="/collections/all">Shop Products</a><div class="dropdown-panel"><dropdown-menu><a href="/collections/oral">Oral Care</a><div class="dropdown-panel"><span>The 7-Step Routine</span><a href="/collections/oil"><span aria-hidden="true">1</span>Oil Pulling</a><a href="/collections/floss">Manual Floss</a></div></dropdown-menu><dropdown-menu><a href="/collections/wellness">Wellness</a><div class="dropdown-panel"><a href="/collections/oils">Essential Oils</a></div></dropdown-menu></div></dropdown-menu></nav></header>"""
LINKED_STATIC = """<header><nav class="primary-nav desktop-only" aria-label="Primary navigation"><a href="/collections/new">New</a></nav><div class="desktop-subnav"><nav><a href="/collections/face" aria-controls="face-panel"><span class="menu-link-label">Face</span></a><a href="/collections/minis"><span class="menu-link-label">Minis</span></a></nav><section id="face-panel" class="mega-panel" hidden><a href="/collections/face"><span class="menu-link-label">Shop All Face</span></a><a href="/collections/foundation"><span class="menu-link-label">Foundation</span></a><a href="/products/recommended">Recommended product</a></section></div><div class="mobile-drawer"><a href="/collections/mobile-only" aria-controls="mobile-panel">Mobile</a><section id="mobile-panel"><a href="/collections/mobile-child">Child</a></section></div></header>"""
EXPLODING_HIDDEN = """<style>.mega-menu{display:none}</style><header><nav><ul><li class="header__linklist-item"><a href="/collections/games" aria-haspopup="true" aria-expanded="false">Games</a><div class="mega-menu" hidden aria-hidden="true"><div class="mega-menu__column"><h3>Collections</h3><a href="/collections/party-games">Party Games</a><a href="/collections/games">View All</a></div></div></li></ul></nav></header>"""
EMPTY = """<header><nav><button aria-controls="empty-panel" aria-expanded="false" onclick="document.getElementById('empty-panel').hidden=false;this.setAttribute('aria-expanded','true')">Shop</button><div id="empty-panel" hidden></div></nav></header>"""

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  body=EMPTY if self.path=='/empty' else FLYOUT.replace('dropdown-menu:hover','never:hover') if self.path=='/flyout-broken' else FLYOUT if self.path=='/flyout' else SECTION if self.path=='/section' else HOVER if self.path=='/hover' else LINKED if self.path=='/linked' else GOOD if self.path=='/good' else GOOD.replace('class="card-title"','class="copy"') if self.path=='/ambiguous' else GOOD.replace('onclick=', 'data-disabled-action=')
  self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(body.encode())

class NavigationBrowserTests(unittest.TestCase):
 def test_browser_tool_retries_page_content_during_navigation(self):
  page=MagicMock();page.content.side_effect=[RuntimeError('Page.content: page is navigating and changing the content.'),RuntimeError('page is navigating'),'stable html']
  self.assertEqual(tool_stable_page_content(page),'stable html')
  self.assertEqual(page.content.call_count,3)
  self.assertEqual(page.wait_for_timeout.call_count,2)

 def test_browser_tool_does_not_retry_unrelated_content_error(self):
  page=MagicMock();page.content.side_effect=RuntimeError('browser closed')
  with self.assertRaises(RuntimeError):tool_stable_page_content(page)
  self.assertEqual(page.content.call_count,1)

 def test_linked_static_desktop_panels_are_certified_without_mobile_or_products(self):
  proof=linked_static_evidence(LINKED_STATIC,'https://x/')
  self.assertEqual(proof['method'],'linked_static_dom')
  data=from_evidence(proof,LINKED_STATIC,'https://x/').data
  self.assertEqual(data,{
   'New':'https://x/collections/new',
   'Face':'https://x/collections/face',
   'Face,Face Foundation':'https://x/collections/foundation',
   'Minis':'https://x/collections/minis'})

 def test_hidden_static_megamenu_needs_no_hover_and_view_all_falls_back(self):
  proof=dom_tree_static_evidence(EXPLODING_HIDDEN,'https://x/')
  self.assertEqual(proof['method'],'owned_menu_tree')
  self.assertTrue(proof['complete'])
  data=from_evidence(proof,EXPLODING_HIDDEN,'https://x/').data
  self.assertIn('Games,Games Collections,Games Collections Party Games',data)
  self.assertFalse(any('View All' in key for key in data))

 def test_static_evidence_keeps_same_url_under_distinct_groups(self):
  html='''<header><nav><button aria-controls="panel" aria-expanded="false">Shop</button></nav><section id="panel" hidden><div class="menu-column"><h3>For Home</h3><a href="/collections/all">View All</a></div><div class="menu-column"><h3>For Travel</h3><a href="/collections/all">View All</a></div></section></header>'''
  proof=dom_tree_static_evidence(html,'https://x/')
  paths=[entry['labels'] for entry in proof['roots'][0]['links']]
  self.assertIn(['Shop','For Home'],paths)
  self.assertIn(['Shop','For Travel'],paths)

 def test_static_evidence_merges_only_explicit_outside_panel(self):
  html='''<header><nav><a href="/collections/shop" aria-controls="panel" aria-expanded="false">Shop</a></nav><section id="panel" hidden><h3>By Type</h3><a href="/collections/new">New</a></section></header>'''
  proof=dom_tree_static_evidence(html,'https://x/')
  data=from_evidence(proof,html,'https://x/').data
  self.assertEqual(data['Shop'],'https://x/collections/shop')
  self.assertEqual(data['Shop,Shop By Type,Shop By Type New'],'https://x/collections/new')

 def test_check_deduplicates_home_cta_already_proven_by_menu(self):
  html='''<header><nav><a href="/collections/shop">Shop All</a></nav></header>
  <main><section class="product-carousel"><h2>Handpicked Favorites</h2>
  <a href="/collections/shop">Shop All</a></section></main>'''
  proof={'version':4,'method':'dom_tree_static','complete':True,'roots':[{
   'label':'Shop All','url':'https://x/collections/shop','links':[{
    'labels':['Shop All'],'url':'https://x/collections/shop','model':False}]}]}
  self.assertTrue(check({'Shop All':'https://x/collections/shop'},proof,html,'https://x/')['passed'])

 @classmethod
 def setUpClass(cls):
  cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
  cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
 @classmethod
 def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
 def test_auto_open_retains_same_named_child_group(self):
  base='http://127.0.0.1:'+str(self.server.server_port)+'/good'
  proof,captures,html=browser_evidence(base)
  result=from_evidence(proof,html,base)
  self.assertIn('Bedding,Bedding Sheets',result.data)
  self.assertIn('Bedding,Bedding Blankets',result.data)
  self.assertFalse(any('9 colors' in k for k in result.data))
  self.assertIn('Bedding,Bedding NCAA',result.data)
  self.assertTrue(captures);self.assertTrue(check(result.data,proof,html,base)['passed'])
 def test_unopened_css_menu_uses_owned_dom_links(self):
  base='http://127.0.0.1:'+str(self.server.server_port)+'/broken'
  proof,_,html=browser_evidence(base)
  data=from_evidence(proof,html,base).data
  self.assertIn('Bedding,Bedding Sheets',data)
  self.assertFalse(any('/products/' in entry['url'] or '/pages/' in entry['url']
                       for root in proof['roots'] for entry in root['links']))
 def test_real_empty_shell_is_still_blocked(self):
  base='http://127.0.0.1:'+str(self.server.server_port)+'/empty'
  with self.assertRaises(CrawlError):browser_evidence(base)

 def test_description_without_title_is_blocked(self):
  base='http://127.0.0.1:'+str(self.server.server_port)+'/ambiguous'
  with self.assertRaises(CrawlError):browser_evidence(base)

 def test_shared_drawer_binding_and_footer_duplicate(self):
  base='http://127.0.0.1:'+str(self.server.server_port)+'/linked'
  proof,captures,html=browser_evidence(base)
  a=from_evidence(proof,html,base)
  self.assertEqual(list(a.data),['Drinkware,Drinkware Universal Buddy','Collections,Collections NCAA'])
  self.assertTrue(check(a.data,proof,html,base)['passed'])

 def test_parent_container_hover_opens_third_level(self):
  base='http://127.0.0.1:'+str(self.server.server_port)+'/hover'
  proof,_,html=browser_evidence(base)
  data=from_evidence(proof,html,base).data
  self.assertEqual(list(data),['Shop By,Shop By Skin Concern,Shop By Skin Concern Dryness','Shop By,Shop By Skin Concern,Shop By Skin Concern Acne'])
  self.assertTrue(check(data,proof,html,base)['passed'])

 def test_section_header_is_not_a_child_category(self):
  base='http://127.0.0.1:'+str(self.server.server_port)+'/section'
  proof,_,html=browser_evidence(base)
  data=from_evidence(proof,html,base).data
  self.assertIn('Bikes,Bikes Shop Kids Bikes',data)
  self.assertFalse(any('Bikes,Bikes Bikes' in k for k in data))
  self.assertTrue(check(data,proof,html,base)['passed'])

 def test_custom_element_nested_flyouts_are_complete(self):
  base='http://127.0.0.1:'+str(self.server.server_port)+'/flyout'
  proof,_,html=browser_evidence(base)
  data=from_evidence(proof,html,base).data
  self.assertIn('Shop Products,Shop Products Oral Care,Shop Products Oral Care Oil Pulling',data)
  self.assertIn('Shop Products,Shop Products Wellness,Shop Products Wellness Essential Oils',data)
  self.assertFalse(any('7-Step' in key for key in data))
  self.assertLess(list(data).index('Shop Products,Shop Products Oral Care,Shop Products Oral Care Oil Pulling'),list(data).index('Shop Products,Shop Products Wellness'))
  self.assertTrue(check(data,proof,html,base)['passed'])
  proof['roots'][0]['submenu_coverage']['observed_ids']=[]
  with self.assertRaises(CrawlError):check(data,proof,html,base)
 def test_hidden_unopened_descendants_use_bound_dom_hierarchy(self):
  base='http://127.0.0.1:'+str(self.server.server_port)+'/flyout-broken'
  proof,_,html=browser_evidence(base)
  data=from_evidence(proof,html,base).data
  self.assertIn('Shop Products,Shop Products Oral Care,Shop Products Oral Care Oil Pulling',data)
  self.assertIn('Shop Products,Shop Products Wellness,Shop Products Wellness Essential Oils',data)
