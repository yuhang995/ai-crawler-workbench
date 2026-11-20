import unittest, copy, tempfile, csv, json
from pathlib import Path
from core.common import CrawlError
from core.nav_extractor import extract_navigation, is_blacklisted, needs_dynamic
from core.shopify_fastpath import (ordered_products, money, product_rows, export,
                                   collect_listing, listing_dom,
                                   explicit_colorway_entries,
                                   merge_explicit_colorways, category_leaf_label,
                                   primary_collection)
from core.validator import validate_navigation, validate_samples
from core.detector import observe_market, detect

BASE='https://example.com'
def product(i):
 return {'id':i,'handle':f'p{i}','title':f'Product {i}','variants':[{'id':i*10,'price':'12.50','compare_at_price':'20.00','option1':'Red','option2':'M','available':True}], 'options':[{'name':'Color'},{'name':'Size'}], 'images':[{'src':'https://cdn.example.com/red.jpg','variant_ids':[i*10]}]}
def fixture():
 ps=[product(i) for i in range(1,4)]
 html='<main id="product-grid">'+''.join(f'<a href="/products/p{i}">P{i}</a>' for i in range(1,4))+'</main>'
 urls=[BASE+f'/products/p{i}' for i in range(1,4)]
 samples=[{'url':u,'source':'json','product':p} for p,u in zip(ps,urls)]
 detail={'Shop':urls}
 listing={'Shop':{'complete':True,'feeds':[{'products':ps}],'pages':[{'url':BASE+'/collections/shop','html':html}]}}
 rows=[r for s in samples for r in product_rows(s,['Shop'])]
 return samples,detail,listing,rows

class NavigationTests(unittest.TestCase):
 def test_template_panel_ownership_and_independent_guard(self):
  from core.navigation_rules import verify_primary_ownership
  h='<div class="header__links"><a href="#" data-meganav-handle="shop">Shop All</a><template><div data-menu-handle="shop"><ul class="meganav__list-parent"><li><h2>#</h2><ul><li><a href="/collections/all">Shop All Products</a></li></ul></li><li><h2>Product Lines</h2><ul><li><a href="/collections/hydration">Hydration</a></li></ul></li></ul></div></template></div>'
  a=extract_navigation(h,BASE)
  self.assertIn('Shop All,Shop All Product Lines,Shop All Product Lines Hydration',a.data)
  validate_navigation(a.data,[h],BASE)
  with self.assertRaises(CrawlError):verify_primary_ownership({'Hydration':BASE+'/collections/hydration'},h)
 def test_image_card_description_and_desktop_ownership(self):
  h='<li class="header__linklist-item"><a href="/collections/games">Games</a><div class="mega-menu__column"><h2>Collections</h2><a href="/collections/best">Best Sellers</a></div><a class="mega-menu__image-push" href="/collections/kittens"><h2>Exploding Kittens</h2><p>9 games, 3 expansions, 2 Bundles</p></a></li><nav><a href="/collections/best">Mobile Duplicate</a></nav>'
  a=extract_navigation(h,BASE)
  self.assertIn('Games,Games Collections,Games Collections Best Sellers',a.data)
  self.assertIn('Games,Games Exploding Kittens',a.data)
  self.assertFalse(any('9 games' in k or 'Mobile' in k for k in a.data))
  validate_navigation(a.data,[h],BASE)
 def test_specialized_menu_rejects_same_path_with_two_urls(self):
  h='''<li class="header__linklist-item"><a href="/collections/games">Games</a><div class="mega-menu__column"><h2>Collections</h2><a href="/collections/a">Same</a><a href="/collections/b">Same</a></div></li>'''
  with self.assertRaisesRegex(ValueError,'同一路径'):
   extract_navigation(h,BASE)
 def test_classic_columns_keep_primary_owner(self):
  h='<ul><li class="site-nav__expanded-item"><a href="/collections/all">All</a><div class="grid__item"><span class="site-nav__dropdown-link--top-level">Featured</span><a href="/collections/new">New Arrivals</a></div></li></ul>'
  a=extract_navigation(h,BASE)
  self.assertEqual(list(a.data),['All','All,All Featured','All,All Featured,All Featured New Arrivals'])
  validate_navigation(a.data,[h],BASE)
 def test_primary_columns_exclude_search_suggestions(self):
  h='''<nav><output id="search"><a href="/collections/hsa">HSA Eligible</a></output><menu aria-label="Main Navigation"><li><a href="/collections/women">Women</a><div><a class="submenu-test-link-list-heading" href="/collections/clothing">Clothing</a><menu><li><a href="/collections/sets">Lounge Sets</a></li></menu></div></li></menu></nav>'''
  a=extract_navigation(h,BASE)
  self.assertEqual(list(a.data),['Women','Women,Women Clothing','Women,Women Clothing,Women Clothing Lounge Sets'])
  validate_navigation(a.data,[h],BASE)
 def test_drawer_ownership_and_current_replacement(self):
  h='''<html data-crawl-menu-snapshot="current-session"><div class="sidebarMenu__nav"><button class="sidebarMenu__mainTrigger">Shop</button><div class="sidebarMenu__subNav"><a href="/collections/all">Shop All Workwear</a></div><div class="sidebarMenu__subNav"><button class="sidebarMenu__triggerText">Boots</button><div class="sidebarMenu__subMenu"><a class="sidebarMenu__shopAll" href="/collections/old">Shop All Boots</a><div data-crawl-replacement><a class="sidebarMenu__shopAll" href="/collections/boots">Shop All Boots</a><a href="/collections/safety">Safety Toe<span class="sidebarMenu__tag">NEW</span></a><a href="/products/marin">The Marin</a></div></div></div></div><main><div class="hero"><a href="/collections/fall-boots?utm_source=test">Shop Now</a></div><div class="faq"><p><a href="/collections/junk">Junk</a></p></div></main></html>'''
  a=extract_navigation(h,BASE)
  self.assertEqual(a.data['Shop,Shop Boots'],BASE+'/collections/boots')
  self.assertNotIn('Shop,Shop Boots,Shop Boots The Marin',a.data)
  self.assertEqual(a.data['fall-boots'],BASE+'/collections/fall-boots')
  self.assertFalse(any('old' in u or 'junk' in u for u in a.data.values()))
  validate_navigation(a.data,[h],BASE)
  broken=dict(a.data);broken['Safety Toe']=broken.pop('Shop,Shop Boots,Shop Boots Safety Toe')
  with self.assertRaises(CrawlError):validate_navigation(broken,[h],BASE)
 def test_contact_links_cleaned_before_validation(self):
  h='<nav><a href="/cdn-cgi/l/email-protection#123">Email Us</a><a href="/helpdesk">Contact Us</a><a href="/collections/boots">Boots</a></nav>'
  a=extract_navigation(h,BASE)
  self.assertEqual(a.data,{'Boots':BASE+'/collections/boots'})
  validate_navigation(a.data,[h],BASE)
 def test_blacklist(self):
  for p in ['account','login','register','cart','checkout','search','pages/team','blogs/news','collections/support','about-us','contact-us','faq','shipping','returns','privacy-policy','terms-of-service','collections/gift-card','collections/gift-guide','products/a','submit-a-ticket']:
   with self.subTest(p=p): self.assertTrue(is_blacklisted(BASE+'/'+p))
  self.assertFalse(is_blacklisted(BASE+'/collections/gifts'))
 def test_home_and_hierarchy(self):
  h='<nav><ul><li><a href="/collections/chairs">Chairs</a><ul><li><a href="/collections/rockers">Rockers</a></li><li><a href="/collections/chairs">View All</a></li></ul></li></ul></nav><main><div><a href="/collections/summer-sale">Shop Now</a></div></main><footer><a href="/collections/no">No</a></footer>'
  a=extract_navigation(h,BASE)
  self.assertEqual(a.data['Chairs,Chairs Rockers'],BASE+'/collections/rockers')
  self.assertEqual(a.data['summer-sale'],BASE+'/collections/summer-sale')
  self.assertFalse(any('View All' in k or 'No'==k for k in a.data))
  validate_navigation(a.data,[h],BASE)
 def test_home_generic_cta_uses_unique_block_heading_or_is_skipped(self):
  from core.navigation_rules import clean_soup, homepage_entries
  titled='''<main><section class="product-carousel"><header><h2>Handpicked Favorites</h2>
  <a href="/collections/shop">Shop All Products</a></header></section></main>'''
  self.assertEqual(homepage_entries(clean_soup(titled),BASE,[]),[{
   'key':'handpicked-favorites','url':BASE+'/collections/shop',
   'labels':['handpicked-favorites'],'source':'home'}])
  untitled='<main><section class="promo"><a href="/collections/shop">Shop All</a></section></main>'
  self.assertEqual(homepage_entries(clean_soup(untitled),BASE,[]),[])
 def test_menu_heading(self):
  h='<nav><li><button>Charging</button><div><h3>Chargers</h3><a href="/collections/chargers">View All</a></div></li></nav>'
  a=extract_navigation(h,BASE)
  self.assertIn('Charging,Charging Chargers',a.data)
 def test_evidence_and_fragments(self):
  h='<nav><a href="/collections/a#red">Red</a></nav>'
  a=extract_navigation(h,BASE);self.assertIn('#red',next(iter(a.data.values())))
  with self.assertRaises(CrawlError): validate_navigation({'Fake':BASE+'/collections/b'},[h],BASE)
 def test_dynamic_only_empty_panel(self):
  h='<nav><li><a href="/collections/a">A</a><ul><li><a href="/collections/b">B</a></li></ul></li><button aria-controls="menu-x" aria-expanded="false"></button><div id="menu-x"><a href="/collections/b">B</a></div></nav>'
  self.assertFalse(needs_dynamic(h,extract_navigation(h,BASE)))
  self.assertTrue(needs_dynamic(h.replace('id="menu-x"','id="other"'),extract_navigation(h,BASE)))
 def test_details_summary_owns_flat_children(self):
  h='<nav><details><summary>Shop</summary><div><a href="/collections/boots">Boots</a><a href="/collections/hats">Hats</a></div></details></nav>'
  a=extract_navigation(h,BASE)
  self.assertEqual(a.data,{
   'Shop,Shop Boots':BASE+'/collections/boots',
   'Shop,Shop Hats':BASE+'/collections/hats'})
  from core.navigation_guard import dom_tree_static_evidence,from_evidence
  proof=dom_tree_static_evidence(h,BASE)
  self.assertEqual([root['label'] for root in proof['roots']],['Shop'])
  self.assertEqual(from_evidence(proof,h,BASE).data,a.data)
 def test_parentless_view_all_duplicate_is_dropped_not_promoted(self):
  h='<nav><div><a href="/collections/boots">View All</a></div><a href="/collections/boots">Footwear</a></nav>'
  a=extract_navigation(h,BASE)
  self.assertEqual(a.data,{'Footwear':BASE+'/collections/boots'})
  self.assertFalse(a.warnings)
 def test_mobile_shallow_duplicate_does_not_override_desktop_tree(self):
  h='''<header><nav class="desktop"><a href="/collections/accessories">Accessories</a><li><a href="/collections/pizza">Pizza</a><div><a href="/collections/pizza#accessories">Accessories</a></div></li></nav><nav class="mobile-drawer"><a href="/collections/pizza#accessories">Accessories</a></nav></header>'''
  a=extract_navigation(h,BASE)
  self.assertEqual(a.data['Accessories'],BASE+'/collections/accessories')
  self.assertEqual(a.data['Pizza,Pizza Accessories'],BASE+'/collections/pizza#accessories')
 def test_empty_data_bound_megamenu_requires_dynamic(self):
  h='''<header><div class="header__links"><a href="/collections/shop" data-meganav-handle="shop">Shop</a><template><div data-menu-handle="shop"><ul class="meganav__list-parent"></ul></div></template></div></header>'''
  a=extract_navigation(h,BASE)
  self.assertTrue(needs_dynamic(h,a))
 def test_unresolved_parentless_view_all_requires_dynamic(self):
  h='<nav><a href="/collections/mystery">View All</a></nav>'
  a=extract_navigation(h,BASE)
  self.assertTrue(a.warnings)
  self.assertTrue(needs_dynamic(h,a))
  from core.navigation_guard import dom_tree_static_evidence
  self.assertIsNone(dom_tree_static_evidence(h,BASE,a))
 def test_gci_offline_primary_scope_has_no_search_footer_or_shop_all_pollution(self):
  path=Path(__file__).parents[1]/'runs/e2e-gci-all/evidence/home.html'
  if not path.is_file():self.skipTest('offline GCI replay not present')
  h=path.read_text(encoding='utf-8');base='https://gcioutdoor.com/'
  a=extract_navigation(h,base)
  self.assertEqual(a.metadata['primary_roots'],[
   'ROCKER CHAIRS','NON-ROCKER CHAIRS','TABLES & KITCHENS','SHADE','ACCESSORIES','SALE'])
  self.assertFalse(any('SHOP ALL' in key.upper() for key in a.data))
  self.assertFalse(any(name in key for key in a.data for name in ('Comfort Pro','Stowaway','NASCAR')))
  from core.navigation_guard import dom_tree_static_evidence,from_evidence
  proof=dom_tree_static_evidence(h,base,a)
  self.assertIsNotNone(proof)
  self.assertEqual(from_evidence(proof,h,base).data,a.data)

class ProductTests(unittest.TestCase):
 def test_explicit_cross_product_colorways_merge_under_one_shopify_handle(self):
  red=product(1);red.update({'handle':'p-shirt-red','title':'Shirt - Red'})
  red['variants'][0].update({'option1':'Red','sku':'RED-M'})
  blue=product(2);blue.update({'handle':'p-shirt-blue','title':'Shirt - Blue'})
  blue['variants'][0].update({'option1':'Blue','sku':'BLUE-M'})
  html='<script>const x={"available-colors":'+json.dumps([
   {'name':'Red','handle':'p-shirt-red','id':'gid://shopify/Product/1',
    'productImage':'https://cdn.example.com/red-swatch.jpg'},
   {'name':'Blue','handle':'p-shirt-blue','id':'gid://shopify/Product/2',
    'productImage':'https://cdn.example.com/blue-swatch.jpg'}])+ '};</script>'
  class Response:
   text=html
  class Fake:
   def get(self,url):return Response()
   def json(self,url):
    if '/p-shirt-blue.' in url:return {'product':blue}
    raise CrawlError('unexpected '+url)
  seed={'url':BASE+'/products/p-shirt-red','source':'json','product':red}
  self.assertEqual([entry['handle'] for entry in explicit_colorway_entries(html,'p-shirt-red')],
                   ['p-shirt-red','p-shirt-blue'])
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'evidence').mkdir()
   export([seed],{'Shop':[seed['url']]},root)
   groups=merge_explicit_colorways(Fake(),[seed],{'Shop':[seed['url']]},root)
   self.assertEqual(len(groups),1)
   with (root/'shopify_import.csv').open(encoding='utf-8-sig') as handle:
    imported=list(csv.DictReader(handle))
   variants=[row for row in imported if row['Variant SKU']]
   self.assertEqual({row['Handle'] for row in imported},{'p-shirt'})
   self.assertEqual([row['Variant SKU'] for row in variants],['10','20'])
   self.assertEqual([row['Option1 Value'] for row in variants],['Red','Blue'])
   self.assertEqual([row['Variant Image'] for row in variants],
                    ['https://cdn.example.com/red-swatch.jpg',
                     'https://cdn.example.com/blue-swatch.jpg'])
   self.assertEqual([row['Title'] for row in variants],['Shirt',''])
   manifest=json.loads((root/'shopify_merge_groups.json').read_text(encoding='utf-8'))
   self.assertEqual(manifest['import_products'],1)
   self.assertEqual(manifest['source_products'],2)
   self.assertEqual(manifest['variants'],2)

 def test_gallery_surplus_and_unbound_variant(self):
  p=product(1)
  p['images']=[{'src':'https://cdn.example.com/a.jpg','variant_ids':[]},{'src':'https://cdn.example.com/b.jpg','variant_ids':[]}]
  sample={'url':BASE+'/products/p1','source':'json','product':p}
  with tempfile.TemporaryDirectory() as d:
   rows=export([sample],{'Shop':[sample['url']]},d)
   self.assertEqual(rows[0]['Variant Image'],'')
   self.assertEqual(len(rows[0]['Images'].split(',')),2)
   with open(Path(d)/'shopify_import.csv',encoding='utf-8-sig') as f: imported=list(csv.DictReader(f))
   self.assertEqual([r['Image Src'] for r in imported],[i['src'] for i in p['images']])
   self.assertEqual([r['Variant SKU'] for r in imported],['10',''])
   self.assertTrue(all(not r['Variant Image'] for r in imported))
 def test_observed_search_app_next(self):
  h='<main><a href="/products/p1">A</a></main><span class="next"><a title="Next" href="?v=products&pp=2">Next</a></span>'
  self.assertEqual(listing_dom(h,BASE+'/collections/a')[1],BASE+'/collections/a?v=products&pp=2')
 def test_product_href_template_whitespace(self):
  h='<main id="product-grid"><a href="/products/p3            ?variant=3">A</a><a href="/products/p2%20%20">B</a><a href="/products/p1   ">C</a></main>'
  self.assertEqual([p['id'] for p in ordered_products(h,[product(i) for i in range(1,4)],BASE+'/collections/a')],[3,2,1])
 def test_load_more_observed_pagination_parameter(self):
  h='<main id="product-grid"><a href="/products/p1">A</a></main><noscript><a href="/collections/a?page_c7d64ca9=2"> Load More </a></noscript>'
  self.assertEqual(listing_dom(h,BASE+'/collections/a')[1],BASE+'/collections/a?page_c7d64ca9=2')
 def test_theme_load_more_pages(self):
  h='<main><results-list><ul class="product-list-grid" ref="grid" data-last-page="2"><li data-page="1"><a href="/products/p1">A</a></li></ul><button ref="loadMoreButton">Load More</button></results-list></main>'
  self.assertEqual(listing_dom(h,BASE+'/collections/all'),(['p1'],BASE+'/collections/all?page=2',False))
  self.assertEqual(listing_dom(h.replace('data-page="1"','data-page="2"'),BASE+'/collections/all?page=2'),(['p1'],None,False))
 def test_intersection_order_and_variant_cards(self):
  h='<header><a href="/products/p1">nav</a></header><main><div class="recommendations"><a href="/products/p1">rec</a></div><div id="product-grid"><a href="/products/p3?variant=1">a</a><a href="/products/p3?variant=2">a</a><a href="/products/no">outside feed</a><a href="/products/p2">b</a><a href="/products/p1">c</a></div></main>'
  self.assertEqual([p['id'] for p in ordered_products(h,[product(i) for i in range(1,4)],BASE+'/collections/a')],[3,2,1])
 def test_malformed_header_ancestor_does_not_delete_main_grid(self):
  h='''<html><body><header><nav><a href="/products/nav-only">Navigation</a>
  <main id="MainContent"><div class="recommendations"><a href="/products/recommended">Recommended</a></div>
  <div class="products"><a href="/products/p2">P2</a><a href="/products/p1">P1</a></div></main>
  </nav></header></body></html>'''
  self.assertEqual(listing_dom(h,BASE+'/collections/a')[0],['p2','p1'])
 def test_price_units(self):
  self.assertEqual(money('12.50','json'),'12.50');self.assertEqual(money(1250,'js'),'12.50')
  self.assertEqual(money(12,'json'),'12.00')
  for v in [None,-1,'NaN','Infinity']:
   with self.assertRaises(CrawlError): money(v,'json')
 def test_images_and_real_variants(self):
  p=product(1);p['variants'].append({'id':11,'price':'14.00','option1':'Blue','option2':'L'})
  p['images'].append({'src':'https://cdn.example.com/blue.jpg','variant_ids':[11]})
  rows=product_rows({'product':p,'source':'json','url':BASE+'/products/p1'},['Shop'])
  self.assertEqual(len(rows),2);self.assertEqual(rows[1]['Variant Image'],'https://cdn.example.com/blue.jpg');self.assertEqual(len(rows[1]['Images'].split(',')),2)
  self.assertEqual(rows[0]['Sale price'],'12.50');self.assertEqual(rows[0]['Regular price'],'20.00')
 def test_exports(self):
  samples,detail,_,_=fixture()
  with tempfile.TemporaryDirectory() as d:
   export(samples,detail,d)
   for name in ['result.csv','result_clean.csv','shopify_import.csv']:
    with open(Path(d)/name,encoding='utf-8-sig') as f:self.assertEqual(len(list(csv.DictReader(f))),3)
   with open(Path(d)/'shopify_import.csv',encoding='utf-8-sig') as f:
    imported=list(csv.DictReader(f))
   self.assertEqual([row['Type'] for row in imported], ['Shop', 'Shop', 'Shop'])
   self.assertEqual([row['Collection'] for row in imported], ['Shop', 'Shop', 'Shop'])
   with open(Path(d)/'shopify_collection_assignments.csv',encoding='utf-8-sig') as f:
    assignments=list(csv.DictReader(f))
   self.assertEqual([(row['Handle'],row['Collection']) for row in assignments],
                    [('p1','Shop'),('p2','Shop'),('p3','Shop')])
 def test_shopify_collection_uses_deepest_visible_leaf(self):
  categories=['Coffee','Coffee,Coffee Organic Coffee',
              'Coffee,Coffee Organic Coffee,Coffee Organic Coffee Dark Roast']
  self.assertEqual(category_leaf_label(categories[-1]),'Dark Roast')
  self.assertEqual(primary_collection(categories),'Dark Roast')
 def test_fragment_scope(self):
  h='<main><section id="a"><a href="/products/p1">A</a></section><section id="b"><a href="/products/p2">B</a></section></main>'
  self.assertEqual(listing_dom(h,BASE+'/collections/a#b')[0],['p2'])
  with self.assertRaises(CrawlError):listing_dom(h,BASE+'/collections/a#missing')
 def test_pagination_duplicate_ids(self):
  class Response:
   def __init__(self,text):self.text=text
  class Fake:
   def get(self,url):return Response('<main id="product-grid"><a href="/products/p2">B</a><a href="/products/p3">C</a></main>' if 'page=2' in url else '<main id="product-grid"><a href="/products/p1">A</a><a href="/products/p2">B</a></main><a rel="next" href="?page=2">Next</a>')
   def json(self,url):return {'products':[product(i) for i in range(1,4)]}
  x=collect_listing(Fake(),BASE+'/collections/a')
  self.assertEqual([p['id'] for p in x['products']],[1,2,3]);self.assertTrue(x['complete'])

class ValidationTests(unittest.TestCase):
 def test_valid(self):
  s,d,l,r=fixture();self.assertEqual(validate_samples(d,s,l,r)['variants'],3)
 def test_reject_wrong_order(self):
  s,d,l,r=fixture();d['Shop'].reverse()
  with self.assertRaises(CrawlError):validate_samples(d,s,l,r)
 def test_reject_fake_variant(self):
  s,d,l,r=fixture();r.append(dict(r[0],**{'Variant ID':'999'}))
  with self.assertRaises(CrawlError):validate_samples(d,s,l,r)
 def test_reject_bad_fields(self):
  for field,value in [('Name','fake'),('Regular price','1.00'),('Images','https://fake.com/p.jpg'),('Attribute 1 value(s)','Blue'),('External URL',BASE+'/products/fake')]:
   s,d,l,r=fixture();r[0][field]=value
   with self.subTest(field=field),self.assertRaises(CrawlError):validate_samples(d,s,l,r)
 def test_reject_repeat_product(self):
  s,d,l,r=fixture();s[1]['product']['id']=s[0]['product']['id']
  with self.assertRaises(CrawlError):validate_samples(d,s,l,r)
 def test_market(self):
  self.assertTrue(observe_market('Shopify.country = "US"; Shopify.currency = {"active":"USD"};')['confirmed'])
  self.assertFalse(observe_market('Shopify.country = "CA"; Shopify.currency = {"active":"USD"};')['confirmed'])

class DetectorTests(unittest.TestCase):
 def test_branded_shopify_feed_precedes_unverified_myshopify_origin(self):
  class BrandedOnly:
   def __init__(self): self.urls=[]
   def json(self,u):
    self.urls.append(u)
    if 'brand.com/collections/shop/products.json' in u:
     return {'products':[product(1)]}
    raise CrawlError('origin blocked')
  client=BrandedOnly()
  html='Shopify.theme={}; https://brand.myshopify.com'
  found=detect(client,'https://brand.com/',html,{'Shop':'https://brand.com/collections/shop/'})
  self.assertEqual(found['mode'],'standard_shopify')
  self.assertIn('brand.com/collections/shop/products.json',client.urls[0])
  self.assertFalse(hasattr(client,'shopify_origin'))

 def test_gatsby_headless_shopify_uses_embedded_origin_and_us_cart(self):
  class Reply:
   def json(self): return {'currency':'USD'}
  class Headless:
   def json(self,u):
    self.feed_url=u
    return {'products':[product(1)]}
   def get(self,u):
    self.cart_url=u
    return Reply()
  client=Headless()
  html='window.TriplePixelData={TripleName:"brand.myshopify.com",plat:"SHOPIFY",isHeadless:true}; (USD)'
  found=detect(client,'https://brand.com/',html,{'Shop':'https://brand.com/collections/shop/'})
  self.assertEqual(found['mode'],'standard_shopify')
  self.assertEqual(found['shopify_origin'],'https://brand.myshopify.com')
  self.assertIn('brand.myshopify.com/collections/shop/products.json',client.feed_url)
  self.assertTrue(found['market']['confirmed'])

 def test_routes(self):
  class NoFeed:
   def json(self,u):raise CrawlError('no endpoint')
  class Feed:
   def json(self,u):return {'products':[product(1)]}
  cats={'Shop':BASE+'/collections/shop'}
  for html,mode in [('plain HTML','custom_site'),('Shopify.theme algoliasearch','search_app_shopify'),('Shopify.theme endpoint: "/api/graphql.json"','graphql_shopify')]:
   with self.subTest(mode=mode):self.assertEqual(detect(NoFeed(),BASE,html,cats)['mode'],mode)
  self.assertEqual(detect(Feed(),BASE,'Shopify.theme algoliasearch',cats)['mode'],'standard_shopify')

if __name__=='__main__':unittest.main()
