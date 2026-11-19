import unittest
from core.shopify_fastpath import listing_dom
from core.nav_extractor import is_blacklisted
class FilteredGridTests(unittest.TestCase):
 def test_generated_pagination_preserves_repeated_filters(self):
  from core.common import query
  from urllib.parse import urlsplit,parse_qs
  url='https://example.test/collections/tops?filter.color=blue&filter.color=navy&page=1'
  parsed=parse_qs(urlsplit(query(url,page=2)).query)
  self.assertEqual(parsed['filter.color'],['blue','navy']);self.assertEqual(parsed['page'],['2'])
 def test_repeated_filter_and_sort_must_survive_browser_navigation(self):
  from core.shopify_fastpath import verify_listing_location
  from core.common import CrawlError
  url='https://example.test/collections/tops?filter.color=blue&filter.color=navy&sort_by=manual'
  verify_listing_location(url+'&page=2',url)
  for actual in ['https://example.test/collections/tops','https://example.test/collections/tops?filter.color=blue&sort_by=manual']:
   with self.assertRaises(CrawlError):verify_listing_location(actual,url)
 def test_same_domain_collection_canonical_redirect_is_allowed(self):
  from core.shopify_fastpath import verify_listing_location
  from core.common import CrawlError
  verify_listing_location('https://example.test/collections/eyeshadow-palettes',
                          'https://example.test/collections/palettes')
  with self.assertRaises(CrawlError):
   verify_listing_location('https://other.test/collections/eyeshadow-palettes',
                           'https://example.test/collections/palettes')
 def test_collection_grid_excludes_same_collection_products_in_faq(self):
  html='<main><div id="CollectionGrid"><a href="/products/skinny-black">Skinny</a></div><text-accordion><a href="/products/slim-black">Slim FAQ</a></text-accordion></main>'
  self.assertEqual(listing_dom(html,'https://example.test/collections/jeans?filter.fit=Skinny')[0],['skinny-black'])
 def test_empty_filtered_grid_does_not_fall_back_to_faq(self):
  html='<main><div id="CollectionGrid"></div><section><a href="/products/slim-black">Related</a></section></main>'
  self.assertEqual(listing_dom(html,'https://example.test/collections/jeans?filter.fit=Skinny')[0],[])
 def test_grid_excludes_faq_product_links(self):
  html='<main><div class="mod_x_ProductsGridWrapper"><a href="/products/bag">Bag</a></div><section class="faq"><a href="/products/apron">Apron</a></section></main>'
  self.assertEqual(listing_dom(html,'https://example.test/collections/wear?filter.p.x=Bags')[0],['bag'])
 def test_product_pages_always_excluded_from_categories(self):
  for u in ['https://x/pages/about','https://x/products/bag','https://x/collections/wear/products/bag']:
   self.assertTrue(is_blacklisted(u,allow_product=True))
