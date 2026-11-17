import unittest
import json
import re
from html import escape
from types import SimpleNamespace
from unittest.mock import patch

from core.common import CrawlError
from core.shopify_fastpath import (handle, hydrogen_collection_listing,
                                   hydrogen_product_from_html, product_rows)
from core.validator import validate_samples
from test_hydrogen_fastpath import product, stream


BASE = 'https://example.com'
COLLECTION = BASE + '/collections/linens/'
NEXT = COLLECTION + '?direction=next&cursor=two'


def cards_html(cards, cursor=None):
    nodes, anchors = [], []
    for index, (slug, parent_id, colour) in enumerate(cards, 1):
        route = f'{slug}/?color={colour}'
        nodes.append({'id': f'gid://shopify/Product/{index}', 'productItem': {
            'id': f'gid://shopify/Product/{parent_id}', 'handle': slug + '_',
            'targetRoute': route}})
        anchors.append('<a href="/products/' + escape(route) + '">Item</a>')
    connection = {'nodes': nodes, 'pageInfo': {'endCursor': cursor, 'hasNextPage': bool(cursor)}}
    payload = stream({'loaderData': {'routes/collections.$collectionHandle': {
        'collection': {'handle': 'linens', 'products': connection}}}})
    more = f'<a href="?direction=next&amp;cursor={cursor}">Load more</a>' if cursor else ''
    return ('<main><div class="product-grid">' + ''.join(anchors) + '</div>'
            + more + '</main>' + payload)


class ParentCardListingTests(unittest.TestCase):
    def setUp(self):
        self.first = cards_html([('linen', 10, 'White'), ('linen', 10, 'Blue'),
                                 ('bundle_', 20, 'White')], 'two')
        self.second = cards_html([('linen', 10, 'Yellow'), ('towel', 30, 'Red')])

    def listing(self, second=None):
        second = self.second if second is None else second

        def get(url, **kwargs):
            self.assertEqual(url, NEXT)
            return SimpleNamespace(text=second)

        return hydrogen_collection_listing(SimpleNamespace(get=get), COLLECTION, self.first)

    def samples(self, links):
        result, rows = [], []
        for index, url in enumerate(links, 1):
            source = product(handle(url), str(index * 10), 'White', str(index * 1000))
            html = stream({'loaderData': {'routes/products.$handle': {'product': source}}})
            sample = {'product': hydrogen_product_from_html(html, handle(url)),
                      'source': 'collection', 'url': url}
            rows.extend(product_rows(sample, ['Linens']))
            result.append(sample)
        return result, rows

    def test_parent_identity_and_first_visible_card_are_stable_across_pages(self):
        listing = self.listing()
        self.assertEqual(listing['urls'], [BASE + '/products/linen/?color=White',
                                          BASE + '/products/bundle_/?color=White',
                                          BASE + '/products/towel/?color=Red'])
        self.assertEqual([item['id'] for item in listing['products']], ['10', '20', '30'])
        samples, rows = self.samples(listing['urls'])
        self.assertEqual(validate_samples({'Linens': listing['urls']}, samples,
                                          {'Linens': listing}, rows), {'products': 3, 'variants': 3})

    def test_hidden_card_and_unobserved_cursor_cannot_prove_a_page(self):
        hidden = self.first.replace('<a href="/products/linen/?color=White">',
                                    '<a hidden href="/products/linen/?color=White">')
        for html in (hidden, self.first.replace('cursor=two', 'cursor=wrong')):
            with self.assertRaises(CrawlError):
                hydrogen_collection_listing(None, COLLECTION, html, full=False)

    def test_conflicting_identities_and_no_progress_cursor_fail(self):
        for html in (cards_html([('linen', 99, 'Red')]),
                     cards_html([('wrong', 10, 'Red')]), self.first):
            with self.assertRaises(CrawlError):
                self.listing(second=html)

    def test_offline_validator_checks_saved_pagination_and_observed_url(self):
        listing = self.listing()
        urls = list(listing['urls'])
        samples, rows = self.samples(urls)
        listing['pages'][1]['url'] = NEXT.replace('two', 'wrong')
        with self.assertRaisesRegex(CrawlError, '下一页链接'):
            validate_samples({'Linens': urls}, samples, {'Linens': listing}, rows)
        listing['pages'][1]['url'] = NEXT
        urls[0] = urls[0].replace('White', 'Blue')
        samples, rows = self.samples(urls)
        with self.assertRaisesRegex(CrawlError, '父商品与原始页面'):
            validate_samples({'Linens': urls}, samples, {'Linens': listing}, rows)

    def test_document_drops_cursor_replay_uses_real_rendered_grid_and_route_response(self):
        collection = {'handle': 'linens', 'products': {
            'nodes': [{'productItem': {'id': 'gid://shopify/Product/30',
                                       'targetRoute': 'towel/?color=Red'}}],
            'pageInfo': {'endCursor': 'last', 'hasNextPage': False}}}
        encoded = stream({'routes/collections.$collectionHandle': {'data': {'collection': collection}}})
        body = json.loads(re.search(r'enqueue\((.*)\);', encoded)[1])
        source = {'url': NEXT.replace('/?', '.data?') + '&_routes=routes%2Fcollections.%24collectionHandle',
                  'body': body}
        html = ('<main><a href="/cart/">Cart drawer</a></main>'
                '<main><div class="product-grid"><a href="/products/linen/?color=White">Old</a>'
                '<a href="/products/towel/?color=Red">New</a></div></main>')
        client = SimpleNamespace(get=lambda *args, **kw: SimpleNamespace(url=COLLECTION, text=self.first))
        with patch('core.shopify_fastpath._render_hydrogen_cursor_pages',
                   return_value={NEXT: {'html': html, 'router_response': source}}) as render:
            listing = hydrogen_collection_listing(client, COLLECTION, self.first)
        render.assert_called_once_with(COLLECTION, self.first)
        urls = listing['urls']
        samples, rows = self.samples(urls)
        self.assertEqual(validate_samples({'Linens': urls}, samples, {'Linens': listing}, rows)['products'], 3)
        source['url'] = source['url'].replace('cursor=two', 'cursor=first')
        with self.assertRaisesRegex(CrawlError, '来源与分页链接'):
            validate_samples({'Linens': urls}, samples, {'Linens': listing}, rows)


if __name__ == '__main__':
    unittest.main()
