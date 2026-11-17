import json
import unittest

from core.shopify_fastpath import hydrogen_market


def document(frames):
    return ''.join('<script>window.__reactRouterContext.streamController.enqueue('
                   + json.dumps(frame) + ');</script>' for frame in frames)


class DeferredMarketTests(unittest.TestCase):
    def test_market_from_the_resolved_shop_promise_only(self):
        # An unrelated loader resolves first. Its records take up offsets in
        # the same table, but its currency is not evidence for the market.
        base = json.dumps([{'_1': 2}, 'loaderData', {'_3': 4}, 'root',
                           {'_5': 6, '_7': 8, '_11': 12}, 'shop', ['P', 6],
                           'consent', {'_9': 10}, 'country', 'US', 'cart', ['P', 12]])
        other = 'P12:[{"_14":15},"currency","USD"]'
        shop = 'P6:[{"_14":17},"CAD"]'
        self.assertEqual(hydrogen_market(document([base, other])),
                         {'country': 'US', 'currency': ''})
        self.assertEqual(hydrogen_market(document([base, other, shop])),
                         {'country': 'US', 'currency': 'CAD'})

    def test_root_layout_consent_and_shop_analytics_confirm_market(self):
        frame = json.dumps([{'_1': 2}, 'loaderData', {'_3': 4, '_5': 6},
                            'root', {}, 'root-layout', {'_7': 8, '_11': 12},
                            'consent', {'_9': 10}, 'country', 'US',
                            'shopAnalyticsPromise', {'_13': 14}, 'currency', 'USD'])
        self.assertEqual(hydrogen_market(document([frame])),
                         {'country': 'US', 'currency': 'USD'})
