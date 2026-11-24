import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.simple_nav import extract_url_json_with_ai, slim_html_for_ai


BASE = 'https://example.com/'


def links(count):
    return ''.join(f'<li><a href="/collections/item-{i}">Item {i}</a></li>' for i in range(count))


class SimpleNavOutlineTests(unittest.TestCase):
    def test_locale_home_preserves_context_for_root_relative_collections(self):
        data = slim_html_for_ai(
            '<header><nav><a href="/collections/bottles">Bottles</a></nav></header>',
            'https://example.com/en-us')
        self.assertIn('https://example.com/en-us/collections/bottles', data['valid_urls'])

    def test_menu_card_subtitle_requires_independent_visible_title(self):
        html = '''<header><nav><ul>
          <li><a href="/collections/mixes" title="Top Mixes">
            <div>Top <b>Mixes</b></div><div class="nav-linkSubTitle">Our best sellers</div></a></li>
          <li><a href="/collections/season" title="Browse">
            <span>Holiday</span><span class="subtitle">Collection</span></a></li>
          <li><a href="/collections/sweets" title="Sweets">
            <span hidden>Sweets</span><span>Local</span><span class="subtitle">Specialties</span></a></li>
        </ul></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        self.assertIn('[a] Top Mixes ->', data['navigation_outline'])
        self.assertIn('Top Mixes', data['navigation_coverage']['source_roots'])
        self.assertIn('[a] Holiday Collection ->', data['navigation_outline'])
        self.assertIn('[a] Local Specialties ->', data['navigation_outline'])

    def test_shared_header_alignment_is_not_a_mobile_nav(self):
        items = links(4)
        html = (f'<header class="header--mobile-left header--has-account">'
                f'<nav><ul>{items}</ul></nav>'
                f'<nav class="menu-drawer"><ul>{items}</ul></nav></header>')
        data = slim_html_for_ai(html, BASE)
        self.assertEqual(data['removed_navigation_regions'][0]['role'], 'mobile')
        self.assertEqual(data['navigation_outline'].count('[navigation'), 1)
        self.assertIn('[navigation desktop]', data['navigation_outline'])

    def test_direct_anchor_names_exclude_subtext_but_keep_real_name_parts(self):
        html = '''<nav><ul>
        <li><a href="/collections/signature">Signature<span class="menu__subtext___Abc">Buttery Soft</span></a></li>
        <li><a href="/collections/sunday">Sunday Knit<span class="menu__subtext___Xyz">New!</span></a></li>
        <li><a href="/collections/care">Fabric &amp; Laundry Care<span class="subtext">Top Recommendation</span></a></li>
        <li><a href="/collections/blends"><b>Local</b><span class="subtext">Blends</span></a></li>
        <li><a href="/collections/collection">Fine <b>Collection</b></a></li>
        <li><a href="/collections/sheets"><span class="subtext">Sheets</span></a></li>
        </ul></nav>'''
        data = slim_html_for_ai(html, BASE)
        labels = [line.split('[a] ')[1].split(' -> ')[0] for line in data['navigation_outline'].splitlines()
                  if '[a] ' in line]
        self.assertEqual(labels, ['Signature', 'Sunday Knit', 'Fabric & Laundry Care', 'Local Blends',
                                  'Fine Collection', 'Sheets'])

    def test_image_alt_matching_first_line_excludes_card_description(self):
        html = '''<header><nav><a href="/collections/mint">
          <img alt="Minted Cucumber" src="mint.svg">
          <span>Minted Cucumber</span><span>Cucumber aloe infused with mint.</span>
        </a></nav></header>'''
        outline = slim_html_for_ai(html, BASE)['navigation_outline']
        self.assertIn('[a] Minted Cucumber -> /collections/mint', outline)
        self.assertNotIn('Cucumber aloe infused', outline)

    def test_image_only_menu_card_uses_its_sibling_title_without_self_heal(self):
        html = '''<header><nav><details><summary>Shop</summary><div class="menu-panel">
          <div class="menu-card"><a href="/collections/all"><img alt=""></a>
            <div class="card-title">ALL PRODUCTS</div></div>
          <div class="menu-card"><a href="/collections/full"><img alt=""></a>
            <div class="card-title">FULL SIZE</div><p>Protein: 17g</p></div>
          <a href="/collections/bundle">Build A Bundle</a>
        </div></details></nav>
        <header-drawer class="mobile"><nav>
          <a href="/collections/all">ALL PRODUCTS</a>
          <a href="/collections/full">FULL SIZE</a>
          <a href="/collections/bundle">Build A Bundle</a>
        </nav></header-drawer></header>'''
        data = slim_html_for_ai(html, BASE)
        self.assertIn('[a] ALL PRODUCTS -> /collections/all', data['navigation_outline'])
        self.assertIn('[a] FULL SIZE -> /collections/full', data['navigation_outline'])
        self.assertNotIn('Protein: 17g', data['navigation_outline'])
        self.assertEqual(data['navigation_coverage']['ratio'], 1.0)
        self.assertFalse(data['navigation_coverage']['needs_self_heal'])

    def test_declared_sibling_menu_panels_keep_control_ownership(self):
        html = '''<header><nav><details><summary>Shop</summary>
          <a href="javascript:void(0)" menu_id="item_all">Shop All</a>
          <a href="javascript:void(0)" menu_id="item_size">Shop By Size</a>
          <div class="item_all_display"><div class="menu-card">
            <a href="/collections/all"><img alt=""></a><div class="card-title">ALL PRODUCTS</div></div></div>
          <div class="item_size_display"><div class="menu-card">
            <a href="/collections/full"><img alt=""></a><div class="card-title">FULL SIZE</div></div></div>
        </details></nav></header>'''
        outline = slim_html_for_ai(html, BASE)['navigation_outline']
        self.assertLess(outline.index('[label] Shop All'), outline.index('[a] ALL PRODUCTS -> /collections/all'))
        self.assertLess(outline.index('[label] Shop By Size'), outline.index('[a] FULL SIZE -> /collections/full'))

    def test_home_collection_trailing_slash_duplicate_preserves_filter_scope(self):
        html = '''<header><nav><a href="/collections/mixes">Mixes</a></nav></header>
            <main><a href="/collections/mixes/">Shop Now</a>
            <a href="/collections/mixes/?style=sour">Sour Mixes</a></main>'''
        data = slim_html_for_ai(html, BASE)
        self.assertEqual([card['url'] for card in data['home_cards']],
                         [BASE + 'collections/mixes/?style=sour'])
        self.assertIn(BASE + 'collections/mixes/', data['valid_urls'])

    def test_identical_mobile_copy_is_removed_but_source_ledger_stays_complete(self):
        items = links(6)
        html = (f'<header><header-menu><nav><button>Shop</button><ul>{items}</ul></nav></header-menu>'
                f'<header-drawer class="mobile"><nav><summary>Menu</summary><ul>{items}</ul></nav></header-drawer></header>'
                '<main><a href="/collections/home-only"><h2>Home Only</h2></a></main>')
        data = slim_html_for_ai(html, BASE)
        self.assertEqual(data['removed_navigation_regions'][0]['coverage'], 1.0)
        self.assertEqual(data['navigation_outline'].count('/collections/item-0'), 1)
        self.assertNotIn('header-drawer', data['text'])
        self.assertIn(BASE + 'collections/home-only', data['valid_urls'])
        self.assertIn('[home] Home Only -> /collections/home-only', data['text'])

    def test_drawer_is_retained_when_no_desktop_equivalent_exists(self):
        html = '<header><header-drawer class="mobile"><nav><summary>Menu</summary>' + links(3) + '</nav></header-drawer></header>'
        data = slim_html_for_ai(html, BASE)
        self.assertFalse(data['removed_navigation_regions'])
        self.assertIn('[navigation mobile]', data['text'])
        self.assertEqual(data['navigation_outline'].count('-> /collections/'), 3)

    def test_large_duplicate_navigation_stays_below_15kb_without_losing_links(self):
        items = links(188)
        html = (f'<header><header-menu><nav><button>Shop</button><ul>{items}</ul></nav></header-menu>'
                f'<header-drawer class="mobile"><nav><summary>Menu</summary><ul>{items}</ul></nav></header-drawer></header>')
        data = slim_html_for_ai(html, BASE)
        self.assertLessEqual(data['bytes'], 15 * 1024)
        self.assertEqual(len(data['valid_urls']), 188)
        self.assertEqual(data['navigation_outline'].count('-> /collections/'), 188)
        self.assertNotIn('<div', data['text'])

    def test_ai_timeout_is_210_seconds(self):
        data = slim_html_for_ai('<nav><a href="/collections/all">All</a></nav>', BASE)
        with tempfile.TemporaryDirectory() as directory:
            process = MagicMock()
            process.returncode = 0

            def launch(command, **kwargs):
                output = Path(command[command.index('-o') + 1])
                output.write_text(json.dumps({'entries': [{'key': 'All', 'url': BASE + 'collections/all'}]}), encoding='utf-8')
                return process

            with patch('core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                    'core.simple_nav.subprocess.Popen', side_effect=launch):
                extract_url_json_with_ai(directory, BASE, data)
            self.assertEqual(process.communicate.call_args.kwargs['timeout'], 210)

    def test_oversized_duplicate_outline_is_bounded_without_changing_source_ledger(self):
        repeated = ''.join(
            f'<a href="/collections/item-{index}">' + ('Long description ' * 80) + f'{index}</a>'
            for index in range(500))
        html = '<header><nav>' + repeated + repeated + '</nav></header>'
        data = slim_html_for_ai(html, BASE)
        self.assertLessEqual(data['bytes'], 40 * 1024)
        self.assertGreater(data['uncompressed_bytes'], data['bytes'])
        self.assertEqual(len(data['source_navigation_urls']), 500)

    def test_utility_category_like_links_are_excluded_from_coverage_denominator(self):
        html = '''<header><nav><a href="/collections/shop">Shop</a>
          <a href="/collections/new">New</a><a href="/collections/about-us">About</a>
          <a href="/collections/help">Help</a></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        self.assertEqual(set(data['coverage_source_urls']), {
            BASE + 'collections/shop', BASE + 'collections/new'})
        self.assertEqual(data['navigation_coverage']['source_urls'], 2)


if __name__ == '__main__':
    unittest.main()
