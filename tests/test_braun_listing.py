import json
import unittest
from types import SimpleNamespace

from core.braun_listing import collect_listing, validate_listing
from core.common import CrawlError


PAGE = 'https://us.braun.com/en-us/shavers'


def response(page, body):
    state = {'props': {'pageProps': {'content': {'page': page}}}}
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(state) + '</script>'
    return SimpleNamespace(url=PAGE, text=html + '<main>' + body + '</main>')


class ListingTests(unittest.TestCase):
    def test_grid_order_and_article_banner(self):
        items = [{'sku': 'a', 'slug': '/products/a'}, {'sku': 'b', 'slug': '/products/b'}]
        body = ('<div class="styles_totalProductsLabel__1"><b>2</b> Products</div>'
                '<div class="styles_plpGrid__1">'
                '<div class="styles_productCard__1" data-action-detail="Shop Now"></div>'
                '<div class="styles_productCard__1" data-action-detail="/products/b"></div>'
                '<div class="styles_productCard__1" data-action-detail="/products/a"></div></div>')
        raw = response({'isListingPage': True, 'algoliaInitialResults': items}, body)
        listing = collect_listing(SimpleNamespace(get=lambda *args, **kwargs: raw), PAGE)
        urls = ['https://us.braun.com/en-us/products/' + slug for slug in ('b', 'a')]
        self.assertEqual(listing['urls'], urls)
        validate_listing(listing, urls)
        with self.assertRaises(CrawlError):
            validate_listing(listing, urls[::-1])
        listing['pages'][0]['html'] = raw.text.replace('<b>2</b>', '<b>3</b>')
        with self.assertRaises(CrawlError):
            validate_listing(listing, urls)

    def test_series_requires_its_visible_buy_card_and_excludes_recommendations(self):
        page = {'categoryMegaPDPs': [{'pdpLink': {'fields': {'sku': '1', 'internalLink': '/a'}}}],
                'algoliaInitialResults': [{'sku': '9', 'slug': '/recommendation'}]}
        body = ('<div class="styles_cards__1"><a class="event_buy_now" href="/a">Buy</a></div>'
                '<aside><a class="event_buy_now" href="/recommendation">Recommended</a></aside>')
        client = SimpleNamespace(get=lambda *args, **kwargs: response(page, body))
        self.assertEqual(collect_listing(client, PAGE)['urls'], ['https://us.braun.com/en-us/a'])
        body = body.replace('class="styles_cards__1"', 'class="styles_cards__1" hidden')
        with self.assertRaises(CrawlError):
            collect_listing(client, PAGE)

    def test_curated_shelves_require_real_sku_links(self):
        block = {'fields': {'contentType': 'shelf007', 'products': [
            {'fields': {'sku': 'sku1', 'internalLink': '/model-1'}}]}}
        page = {'pageContent': [{'fields': {'sectionContent': [block, block]}}]}
        body = ('<div class="styles_productList__s"><div class="productShelfCard">'
                '<a href="/en-us/model-1">Buy</a></div></div>')
        client = SimpleNamespace(get=lambda *args, **kwargs: response(page, body))
        self.assertEqual(collect_listing(client, PAGE)['source_skus'],
                         {'https://us.braun.com/en-us/model-1': 'sku1'})
        body = body.replace('/en-us/model-1', '/en-gb/model-1')
        with self.assertRaises(CrawlError):
            collect_listing(client, PAGE)
