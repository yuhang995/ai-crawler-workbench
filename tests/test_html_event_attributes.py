import copy
import gzip
import unittest
from pathlib import Path

from core.common import CrawlError
from core.html_parser import normalize_event_attributes, parse_soup
from core.menu_tree import build_tree, tree_data, verify_sources
from core.nav_extractor import extract_navigation
from core.validator import validate_navigation


class EventAttributeTests(unittest.TestCase):
    def test_real_digestive_support_category_is_not_customer_support(self):
        from core.nav_extractor import is_blacklisted
        self.assertFalse(is_blacklisted('https://example.com/collections/digestive-health', 'Dog,Dog Health,Dog Health Digestive Support'))
        self.assertTrue(is_blacklisted('https://example.com/collections/help', 'Shop,Shop Customer Support'))

    def test_arrow_handler_does_not_become_text_or_hide_following_href(self):
        html = '''<header><nav aria-label="Primary"><ul><li><a @click.prevent="() => { track('a > b'); }" href="/collections/dog?filter=a&amp;b=c" aria-label="Dog">Dog</a></li></ul></nav></header>'''
        soup = parse_soup(html)
        self.assertEqual(soup.a.get_text(), 'Dog')
        self.assertEqual(soup.a['href'], '/collections/dog?filter=a&b=c')
        self.assertEqual(soup.a['aria-label'], 'Dog')
        self.assertEqual(next(v for k,v in soup.a.attrs.items() if k.startswith('data-crawl-event-')), "() => { track('a > b'); }")
        normalized = normalize_event_attributes(html)
        self.assertEqual(normalize_event_attributes(normalized), normalized)
        data = extract_navigation(html, 'https://example.com/').data
        self.assertEqual(list(data), ['Dog'])
        proof = build_tree(html, 'https://example.com/')
        validate_navigation(data, [html], 'https://example.com/', proof)

    def test_literal_script_comments_and_regular_html_are_unchanged(self):
        for html in ('<a href="/collections/all">A &gt; B</a>',
                     '''<script>const sample = '<a @click="() => test()">';</script><!-- <a @click="bad"> -->'''):
            self.assertEqual(normalize_event_attributes(html), html)
        html = '''<!-- before -->\n<div @mouseenter='() => { emit("x"); }'>A &gt; B</div>\n<img @load="() => ready()" src="x"/>'''
        soup = parse_soup(html)
        self.assertEqual(soup.div.get_text(), 'A > B')
        self.assertEqual(soup.img['src'], 'x')

    def test_openfarm_saved_html_all_state_panels_have_source_ownership(self):
        html = gzip.decompress((Path(__file__).parent/'fixtures/openfarmpet-events.html.gz').read_bytes()).decode('utf-8')
        base = 'https://openfarmpet.com/'
        tree = build_tree(html, base)
        self.assertTrue(tree['complete'])
        self.assertEqual(tree['layout'], 'state_group_panels')
        data = tree_data(tree)
        self.assertIn('Dog,Dog Dry Food,Dog Dry Food RawMix Kibble', data)
        self.assertIn('Cat,Cat Dry Food,Cat Dry Food RawMix Kibble', data)
        self.assertIn('Dog,Dog Health & Wellness,Dog Health & Wellness GoodGut Kibble', data)
        self.assertGreater(len(data), 60)
        self.assertFalse(any('@click' in key or 'amplitude' in key or 'setActiveGroup' in key for key in data))
        verify_sources(tree, html, base)
        candidate = extract_navigation(html, base).data
        validate_navigation(candidate, [html], base, tree)
        forged = copy.deepcopy(tree)
        forged['roots'][0]['links'][1]['labels'][0] = 'Cat'
        with self.assertRaises(CrawlError): verify_sources(forged, html, base)

    def test_state_panel_must_have_a_matching_primary_control(self):
        html = '''<header><nav aria-label="Primary"><a @click="setActiveGroup('dog')" href="/collections/dog">Dog</a></nav><div data-nav_group="cat"><a href="/collections/cat">Cat</a></div></header>'''
        with self.assertRaises(CrawlError): build_tree(html, 'https://example.com/')
