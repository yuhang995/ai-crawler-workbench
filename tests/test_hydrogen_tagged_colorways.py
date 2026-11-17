import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.common import CrawlError
from core.shopify_fastpath import (explicit_colorway_entries, hydrogen_product_from_html,
                                   merge_explicit_colorways)
from core.validator import _validate_shopify_import
from test_hydrogen_fastpath import product, stream


def grouped_page(second_tags):
    left = product('cup-red', '101', 'Red', '1001')
    right = product('cup-blue', '102', 'Blue', '1002')
    for member, tags in ((left, ['color::Berry Plaid']), (right, second_tags)):
        member['tags'] = tags
        member['options'] = [{'name': 'Title', 'optionValues': [{'name': 'Default Title'}]}]
        member['variants']['nodes'][0]['selectedOptions'] = [{'name': 'Title', 'value': 'Default Title'}]
    members = [left, right]
    seed = dict(left, initialGrouping={'title': 'Cup',
                'products': [{'id': m['id'], 'handle': m['handle']} for m in members],
                'allProducts': members})
    return stream({'loaderData': {'routes/($locale).products.$handle': {'product': seed}}})


class TaggedColourTests(unittest.TestCase):
    def test_merged_import_uses_actual_colour_only_with_bound_photo_and_price(self):
        html = grouped_page(['colorfilter::Blue', 'color::Moon Blue'])
        url = 'https://example.com/products/cup-red'
        sample = {'url': url, 'source': 'collection', 'product': hydrogen_product_from_html(html, 'cup-red')}
        client = SimpleNamespace(get=lambda url: SimpleNamespace(text=html))
        with tempfile.TemporaryDirectory() as dest:
            root = Path(dest)
            (root / 'evidence').mkdir()
            merge_explicit_colorways(client, [sample], {'Cups': [url]}, root)
            with (root / 'shopify_import.csv').open(encoding='utf-8-sig', newline='') as fh:
                rows = list(csv.DictReader(fh))
            variants = [v for v in rows if v['Variant SKU']]
            self.assertEqual([(v['Option1 Value'], v['Option2 Name']) for v in variants],
                             [('Berry Plaid', ''), ('Moon Blue', '')])
            self.assertEqual(variants[1]['Variant Image'], 'https://cdn.example/cup-blue.jpg')
            self.assertEqual(_validate_shopify_import(root, rows, [sample])['shopify_variants'], 2)
            with (root / 'wp_import_merged.csv').open(encoding='utf-8-sig', newline='') as fh:
                wp = list(csv.DictReader(fh))
            self.assertEqual(wp[0]['Attribute 1 value(s)'], 'Berry Plaid, Moon Blue')
            self.assertTrue(all(not row['Attribute 2 name'] for row in wp))
            self.assertEqual([row['Parent'] for row in wp[1:]], [wp[0]['SKU']] * 2)
            self.assertEqual([row['Images'] for row in wp[1:]],
                             ['https://cdn.example/cup-red.jpg', 'https://cdn.example/cup-blue.jpg'])
            self.assertIn('https://cdn.example/cup-blue.jpg', wp[0]['Images'])
            with (root / 'result_clean_merged.csv').open(encoding='utf-8-sig', newline='') as fh:
                source_rows = list(csv.DictReader(fh))
            self.assertEqual(source_rows[1]['Parent'], wp[0]['Product ID'])
            self.assertEqual(source_rows[1]['Images'], wp[0]['Images'])
            variants[1]['Variant Price'] = '0.10'
            with self.assertRaises(CrawlError):
                _validate_shopify_import(root, rows, [sample])

    def test_conflicting_tags_or_only_search_filters_cannot_assign_a_colour(self):
        for tags in (['color::A', 'color::B'], ['colorfilter::Blue']):
            with self.subTest(tags=tags):
                entries = explicit_colorway_entries(grouped_page(tags), 'cup-red')
                self.assertEqual(entries[1]['color'], '')
