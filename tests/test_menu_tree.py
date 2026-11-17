import copy
import unittest

from core.common import CrawlError
from core.menu_tree import build_tree, tree_data, verify_sources
from core.nav_extractor import extract_navigation
from core.navigation_guard import dom_tree_static_evidence
from core.validator import validate_navigation

BASE = 'https://example.com/'


def menu(body):
    return '<header><nav aria-label="Main menu"><ul>' + body + '</ul></nav></header>'


class OwnedMenuTests(unittest.TestCase):
    def test_animated_button_duplicate_is_not_part_of_title(self):
        html = menu('<li><a href="/collections/all"><span class="btn-text">Shop</span>'
                    '<span class="btn-text btn-duplicate">Shop</span></a></li>')
        proof = build_tree(html, BASE)
        self.assertEqual(tree_data(proof), {'Shop': BASE + 'collections/all'})
        verify_sources(proof, html, BASE)
        distinct = html.replace('btn-duplicate">Shop', 'btn-duplicate">Outlet')
        self.assertEqual(list(tree_data(build_tree(distinct, BASE))), ['Shop Outlet'])

    def test_generic_cta_punctuation_uses_group(self):
        from core.menu_tree import generic
        for title in ('Shop All ›', '→ Shop Now »', 'Explore', 'Discover!', 'Learn More →'):
            self.assertTrue(generic(title))
            html = menu('<li><button>Products</button><div><h3>Power Banks</h3>'
                        '<a href="/collections/banks">' + title + '</a></div></li>')
            proof = build_tree(html, BASE)
            data = tree_data(proof)
            self.assertIn('Products,Products Power Banks', data)
            self.assertFalse(any(title in key for key in data))
        self.assertFalse(generic('Discover Collection'))

    def test_header_fallback_ranks_links_and_ignores_mobile(self):
        html = '<header class="header--mobile-center"><div class="mobile"><nav><ul>'
        html += ''.join('<li><a href="/collections/m%d">Mobile</a></li>' % i for i in range(5))
        html += '</ul></nav></div><nav><ul><li><a href="/collections/one">One</a></li></ul></nav>'
        html += '<nav><ul><li><a href="/collections/a">A</a></li><li><a href="/collections/b">B</a></li></ul></nav></header>'
        proof = build_tree(html, BASE)
        self.assertEqual(tree_data(proof), {'A': BASE + 'collections/a', 'B': BASE + 'collections/b'})
        verify_sources(proof, html, BASE)

    def test_nested_details_preserve_root_and_section_ownership(self):
        html = menu('<li><details><summary><a href="/collections/tools">Tools</a></summary>'
                    '<nav-menu><details><summary><div><a href="/collections">Activities</a></div></summary>'
                    '<ul><li><a href="/collections/camp">Camp<span class="badge">New!</span></a></li></ul>'
                    '<a class="main-nav__item--go" href="/collections">Go to Activities</a>'
                    '</details></nav-menu></details></li>'
                    '<li><details><summary><a href="/collections/knives">Knives</a></summary>'
                    '<details><summary><a href="/collections">Activities</a></summary>'
                    '<a href="/collections/camp">Camp</a></details></details></li>')
        tree = build_tree(html, BASE)
        self.assertTrue(tree['complete'])
        data = tree_data(tree)
        self.assertEqual(set(data), {'Tools',
            'Tools,Tools Activities,Tools Activities Camp', 'Knives',
            'Knives,Knives Activities,Knives Activities Camp'})
        verify_sources(tree, html, BASE)
        forged = copy.deepcopy(tree)
        forged['roots'][0]['links'][-1]['labels'] = ['Knives', 'Activities', 'Camp']
        with self.assertRaises(CrawlError):
            verify_sources(forged, html, BASE)

    def test_empty_details_is_not_complete(self):
        html = menu('<li><details><summary><a href="/collections/all">Shop</a></summary></details></li>')
        self.assertFalse(build_tree(html, BASE)['complete'])

    def test_disclosure_footer_different_destination_keeps_own_label(self):
        html = menu('<li><details><summary><a href="/collections/all">Shop</a></summary>'
                    '<a class="main-nav__item--go" href="/collections/new">New Arrivals</a></details></li>')
        tree = build_tree(html, BASE)
        self.assertIn('Shop,Shop New Arrivals', tree_data(tree))
        verify_sources(tree, html, BASE)

    def test_disclosure_component_keeps_same_named_distinct_child(self):
        html = '<header><div class="header__desktop"><nav class="header__menu">'
        html += '<div><a href="/collections/best">Best</a></div>'
        html += '<hover-disclosure aria-controls="p" aria-expanded="false"><a href="/collections/all-sleep">Sleep Bags</a>'
        html += '<div id="p"><a href="/collections/sleep"><h2>Sleep Bags</h2></a>'
        html += '<a href="/collections/all-sleep">Shop All Sleep Bags</a></div></hover-disclosure></nav></div></header>'
        tree = build_tree(html, BASE)
        self.assertTrue(tree['complete'])
        data = tree_data(tree)
        self.assertEqual(data['Sleep Bags'], BASE+'collections/all-sleep')
        self.assertEqual(data['Sleep Bags,Sleep Bags Sleep Bags'], BASE+'collections/sleep')
        validate_navigation(data, [html], BASE, tree)
        from core.hybrid_navigation import _review_scope
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            scope = _review_scope(data, tree, [], None, Path(d))
        self.assertEqual(scope['issues'], [])

    def test_component_empty_target_cannot_use_parent_link_as_evidence(self):
        html = '<header><div class="desktop"><nav><hover-disclosure aria-controls="p">'
        html += '<a href="/collections/all">Shop</a><div id="p"></div></hover-disclosure></nav></div></header>'
        self.assertFalse(build_tree(html, BASE)['complete'])

    def test_column_anchor_heading_keeps_men_and_women_separate(self):
        html = '<header><div class="desktop"><nav><hover-disclosure><a href="#">Adult</a>'
        for group in ('Women', 'Men'):
            html += '<div class="dropdown__family"><a class="navlink--child" href="#">'+group+'</a>'
            html += '<a class="navlink--grandchild" href="/collections/'+group.lower()+'">Pajamas</a></div>'
        html += '</hover-disclosure></nav></div></header>'
        tree = build_tree(html, BASE)
        self.assertEqual(set(tree_data(tree)), {'Adult,Adult Women,Adult Women Pajamas', 'Adult,Adult Men,Adult Men Pajamas'})
        verify_sources(tree, html, BASE)

    def test_image_alias_borrows_only_exact_observed_url_label(self):
        html = menu('<li><a href="/collections/all">Shop</a><a href="/collections/new"><img alt="Long marketing description"></a>'
                    '<a href="/collections/new">New</a></li>')
        tree = build_tree(html, BASE)
        self.assertTrue(tree['complete'])
        self.assertEqual(len(tree_data(tree)), 2)
        verify_sources(tree, html, BASE)
        bad = menu('<li><a href="/collections/all">Shop</a><a href="/collections/unknown"><img alt="Guess this title"></a></li>')
        self.assertFalse(build_tree(bad, BASE)['complete'])

    def test_search_mobile_and_toggle_are_not_categories(self):
        html = '<header><details><summary><span class="sr-only">Toggle navigation</span></summary>'
        html += menu('<li><a href="/collections/all">Shop Now</a><ul><li><a href="/collections/new">New</a></li></ul></li>')
        html += '</details><div class="predictive-search"><a href="/collections/no">Suggested</a></div>'
        html += '<div class="mobile-drawer"><nav><a href="/collections/mobile">Mobile</a></nav></div></header>'
        data = tree_data(build_tree(html, BASE))
        self.assertEqual(data, {'Shop Now': BASE+'collections/all', 'Shop Now,Shop Now New': BASE+'collections/new'})

    def test_description_and_definition_list_heading(self):
        html = menu('<li><a href="/pages/underwear">Underwear</a><dl><dt>By Absorbency</dt><dd>'
                    '<a data-label="Ultra" href="/collections/ultra"><span>Ultra</span><span>Absorbs 7 tampons</span></a>'
                    '</dd></dl></li>')
        self.assertEqual(tree_data(build_tree(html, BASE)),
                         {'Underwear,Underwear By Absorbency,Underwear By Absorbency Ultra': BASE+'collections/ultra'})

    def test_same_url_in_two_groups_is_preserved_without_invented_parent(self):
        html = menu('<li><a href="/collections/all">Shop</a><section><h3>By Size</h3>'
                    '<a href="/collections/featured">Featured</a></section><section><h3>By Color</h3>'
                    '<a href="/collections/featured">Featured</a></section></li>')
        data = tree_data(build_tree(html, BASE))
        self.assertEqual(len(data), 3)
        self.assertIn('Shop,Shop By Size,Shop By Size Featured', data)
        self.assertIn('Shop,Shop By Color,Shop By Color Featured', data)
        self.assertNotIn('Shop,Shop By Size', data)

    def test_comma_is_serialized_only_at_boundary(self):
        html = menu('<li><a href="/pages/offers">Offers</a><a href="/collections/bras">Buy 2 Bras, Get 15% off</a></li>')
        tree = build_tree(html, BASE)
        self.assertEqual(tree['roots'][0]['links'][0]['display_labels'][-1], 'Buy 2 Bras, Get 15% off')
        data = tree_data(tree)
        self.assertIn('Offers,Offers Buy 2 Bras Get 15% off', data)
        validate_navigation(data, [html], BASE, tree)

    def test_empty_bound_panel_never_certified_from_own_link(self):
        html = menu('<li><a href="/collections/all" aria-controls="lazy">Shop</a><div id="lazy"></div></li>')
        tree = build_tree(html, BASE)
        self.assertFalse(tree['complete'])
        self.assertIsNone(dom_tree_static_evidence(html, BASE, extract_navigation(html, BASE)))

    def test_generic_child_collapses_but_top_level_shop_all_remains(self):
        html = menu('<li><a href="/collections/all">Shop All</a><section><h3>Bras</h3>'
                    '<a href="/collections/bras">Shop All Bras</a><a href="/collections/bras">Shop all Bras</a>'
                    '</section></li>')
        data = tree_data(build_tree(html, BASE))
        self.assertEqual(data, {'Shop All': BASE+'collections/all','Shop All,Shop All Bras': BASE+'collections/bras'})

    def test_accessible_overlay_cards_stay_under_owner_and_root_promo_collapses(self):
        html = menu(
            '<li><a href="/collections/shop">Shop All</a>'
            '<button aria-controls="shop-panel" aria-expanded="false">'
            '<span class="sr-only">Show submenu for Shop All</span></button>'
            '<div id="shop-panel" role="menu">'
            '<a href="/collections/shop">Shop All Products</a>'
            '<a href="/collections/new">Shop New Arrivals</a>'
            '<a href="/collections/shop"><img src="promo.jpg"><p>FREE gift with orders $80+</p></a>'
            '<ul><li><a class="absolute" href="/collections/face">'
            '<span class="sr-only">Face</span></a><div>Face</div></li></ul>'
            '</div></li>'
            '<li><a href="/collections/new">New</a></li>')
        tree = build_tree(html, BASE)
        self.assertTrue(tree['complete'])
        data = tree_data(tree)
        self.assertEqual(data, {
            'Shop All': BASE+'collections/shop',
            'Shop All,Shop All Shop New Arrivals': BASE+'collections/new',
            'Shop All,Shop All Face': BASE+'collections/face',
            'New': BASE+'collections/new',
        })
        self.assertFalse(any('FREE gift' in key for key in data))
        verify_sources(tree, html, BASE)
        proof = dom_tree_static_evidence(html, BASE, extract_navigation(html, BASE))
        self.assertEqual(proof['method'], 'owned_menu_tree')

    def test_source_audit_rejects_omission_and_fabricated_label(self):
        html = menu('<li><a href="/collections/all">Shop</a><ul><li><a href="/collections/new">New</a></li></ul></li>')
        original = build_tree(html, BASE)
        verify_sources(original, html, BASE)
        omitted = copy.deepcopy(original)
        omitted['roots'][0]['links'].pop()
        with self.assertRaises(CrawlError): verify_sources(omitted, html, BASE)
        forged = copy.deepcopy(original)
        forged['roots'][0]['links'][1]['labels'][-1] = 'Invented'
        with self.assertRaises(CrawlError): verify_sources(forged, html, BASE)

    def test_bound_external_panel_and_alias_and_filter_preserved(self):
        html = menu('<li><button aria-controls="p">Shop</button></li>')
        html += '<section id="p"><a href="https://www.example.com/collections/all?filter.v.option.color=Red#grid">Red</a></section>'
        tree = build_tree(html, BASE)
        self.assertTrue(tree['complete'])
        self.assertEqual(tree_data(tree), {'Shop,Shop Red': BASE+'collections/all?filter.v.option.color=Red#grid'})
        validate_navigation(tree_data(tree), [html], BASE, tree)


if __name__ == '__main__':
    unittest.main()
