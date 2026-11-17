import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.brumate_navigation import pack_navigation
from core.simple_nav import run_simple
from test_hydrogen_fastpath import stream


BASE = 'https://example.com/'


def home():
    link = lambda text, url: {'link': {'text': text, 'url': url}}
    leaves = [link('Bottle', '/collections/bottles?size=large'),
              link('Featured product', '/products/one'),
              link('Canadian bottle', '/en-ca/collections/bottles'),
              link('Other store', 'https://other.com/collections/bottles')]
    items = [{'navItem': {'text': 'Drinkware', 'url': '/collections/drinkware'},
              'links': [dict(link('Hydration', '/pages/landing'), sublinks=leaves)],
              'featuredImage': link('Shop All Drinkware', '/collections/drinkware')},
             {'navItem': {'text': 'Shop By', 'url': ''},
              'links': [dict(link('Shop By Size', '/collections/sizes'), sublinks=leaves)]}]
    settings = {'header': {'menu': {'navItems': items}}}
    loader = {'siteSettings': {'data': {'siteSettings': {'settings': settings}}}}
    return ('<header><nav><ul><li><a href="/collections/drinkware" aria-label="Drinkware">'
            'Drinkware</a></li><li><div aria-label="Shop By">Shop By</div></li></ul></nav></header>'
            '<main><a href="/collections/bottles?size=large">Bottle</a>'
            '<a href="/collections/holiday">Shop</a>'
            '<div class="lg:hidden"><a href="/collections/drawer">Mobile</a></div></main>'
            + stream({'loaderData': {'root': loader}}))


class PackHeaderTests(unittest.TestCase):
    def test_hydrated_children_keep_visible_root_and_owned_column(self):
        with tempfile.TemporaryDirectory() as d, patch(
                'core.simple_nav.extract_url_json_with_ai', side_effect=AssertionError):
            root = Path(d)
            data, _, _ = run_simple(root, BASE, home())
            sources = json.loads((root / 'evidence/simple_nav_sources.json').read_text('utf-8'))
            self.assertEqual(data, {
                'Drinkware': BASE + 'collections/drinkware',
                'Drinkware,Drinkware Hydration,Drinkware Hydration Bottle': BASE + 'collections/bottles?size=large',
                'Shop By,Shop By Size': BASE + 'collections/sizes',
                'Shop By,Shop By Size,Shop By Size Bottle': BASE + 'collections/bottles?size=large',
                'holiday': BASE + 'collections/holiday',
            })
            self.assertEqual(sources['valid_urls'], list(dict.fromkeys(data.values())))
            self.assertEqual(json.loads((root / 'navigation_timing.json').read_text())['llm_invocations'], 0)

    def test_unknown_header_bar_does_not_activate_pack_path(self):
        self.assertIsNone(pack_navigation(home().replace('aria-label="Shop By"',
                                                         'aria-label="Other"'), BASE))
