import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.common import CrawlError, read_json
from core.simple_nav import (AI_TIMEOUT_SECONDS, OUTLINE_COVERAGE_THRESHOLD,
                             SELF_HEAL_TIMEOUT_SECONDS,
                             _canonicalize_ai_roots, _reject_exploded_labels,
                             _repair_repeated_control_labels,
                             extract_url_json_with_ai,
                             slim_html_for_ai)


BASE = 'https://example.com/'


def menu(count=5):
    return '<header><nav><ul>' + ''.join(
        f'<li><a href="/collections/item-{index}">Item {index}</a></li>'
        for index in range(count)) + '</ul></nav></header>'


class SimpleNavSelfHealTests(unittest.TestCase):
    def test_split_desktop_navs_own_their_branches_and_remove_union_mobile_duplicate(self):
        html = '''<header>
        <nav class="desktop-main"><ul><li><div><a href="/collections/bags">Bags</a></div>
          <div><div><ul><li><a href="/collections/bags">Shop All - Bags</a></li>
          <li><a href="/collections/totes">Totes</a></li></ul></div>
          <div><a href="/collections/totes"><img src="promo.jpg">Promo Totes</a></div></div></li></ul></nav>
        <nav class="desktop-color"><a href="/pages/rewards">Join Rewards</a>
          <div><button>Shop by color</button><div><div>SHOP BY COLOR</div>
            <div><div>Smooth</div><div><a href="/collections/honey">Honey</a></div></div>
            <div><div>Suede</div><div><a href="/collections/taupe">Taupe</a></div></div>
          </div></div></nav>
        <div class="mobile-drawer"><nav>
          <a href="/collections/bags">Bags</a><a href="/collections/totes">Totes</a>
          <a href="/collections/honey">Honey</a><a href="/collections/taupe">Taupe</a>
        </nav></div></header>'''
        data = slim_html_for_ai(html, BASE)
        outline = data['navigation_outline']
        self.assertNotIn('Join Rewards', outline)
        self.assertIn('[a] Bags -> /collections/bags\n  [a] Shop All - Bags', outline)
        self.assertNotIn('Promo Totes', outline)
        self.assertIn('[label] Shop by color\n  [label] Smooth\n    [a] Honey', outline)
        self.assertIn('  [label] Suede\n    [a] Taupe', outline)
        self.assertNotIn('[label] SHOP BY COLOR', outline)
        self.assertEqual(data['removed_navigation_regions'][0]['role'], 'mobile')
        self.assertEqual(data['removed_navigation_regions'][0]['coverage'], 1.0)

    def test_nested_desktop_nav_subset_is_not_emitted_as_a_second_root_group(self):
        html = '''<header><nav class="header-menu__nav"><ul><li>
          <a href="/collections/shop-all">Shop</a>
          <div><nav class="header-menu__nav">
            <a href="/collections/all">Shop All Products</a>
            <a href="/collections/condiments">Condiments</a>
            <a href="/collections/mayo">Mayo</a>
          </nav></div>
        </li></ul></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        outline = data['navigation_outline']
        self.assertEqual(outline.count('[navigation desktop]'), 1)
        self.assertEqual(outline.count('[a] Shop All Products -> /collections/all'), 1)
        self.assertIn('[a] Shop -> /collections/shop-all', outline)
        self.assertIn('  [a] Shop All Products -> /collections/all', outline)
        self.assertIn('  [a] Condiments -> /collections/condiments', outline)
        self.assertIn('  [a] Mayo -> /collections/mayo', outline)
        self.assertEqual(data['navigation_coverage']['source_roots'], ['Shop'])

    def test_main_nav_carousel_is_not_a_second_header_menu(self):
        html = '''<header><nav><a href="/collections/all">Shop</a>
          <a href="/collections/sale">Sale</a></nav></header>
          <main><nav class="carousel"><a href="/collections/all">Shop All Products Big Promo</a>
          <a href="/collections/sale">Sale Save Up To 35%</a></nav></main>'''
        data = slim_html_for_ai(html, BASE)
        self.assertEqual(data['navigation_outline'].count('[navigation '), 1)
        self.assertNotIn('Big Promo', data['navigation_outline'])
        self.assertNotIn('Save Up To', data['navigation_outline'])
        self.assertEqual(data['home_cards'], [])

    def test_repeated_visual_spacer_text_is_not_part_of_menu_label(self):
        html = '''<header><nav><ul><li>
          <a href="/collections/all">Yarns<span class="Header__LinkSpacer">Yarns</span></a>
        </li></ul></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        self.assertIn('[a] Yarns -> /collections/all', data['navigation_outline'])
        self.assertNotIn('Yarns Yarns', data['navigation_outline'])
        self.assertEqual(data['navigation_coverage']['source_roots'], ['Yarns'])

    def test_section_header_inside_main_is_a_home_card_not_a_navigation_root(self):
        html = '''<header><nav><a href="/collections/all">Shop</a></nav></header>
          <main><section><header class="SectionHeader">
            <a href="/collections/tabletop"><h2>Bring Handmade Happiness to the Table</h2></a>
          </header></section></main>'''
        data = slim_html_for_ai(html, BASE)
        outline = data['navigation_outline']
        self.assertEqual(outline.count('[navigation '), 1)
        self.assertNotIn('Bring Handmade Happiness', outline)
        self.assertIn('[home] Bring Handmade Happiness to the Table -> /collections/tabletop',
                      data['text'])
        self.assertEqual(data['source_navigation_urls'], [BASE + 'collections/all'])

    def test_illegal_product_promo_does_not_own_valid_mega_menu_branches(self):
        html = '''<header><nav><ul><li><a href="/collections/new">New</a>
          <div class="MegaMenu__Inner">
            <div class="MegaMenu__Links">
              <div><span>Yarn</span><a href="/collections/new-yarns">New Yarns</a></div>
              <div><span>Patterns</span><a href="/collections/new-patterns">New Patterns</a></div>
            </div>
            <div class="MegaMenu__FeaturedImages"><div>
              <a href="/products/haven"><img src="haven.jpg">
                <p>New</p><p>Premier Haven</p><p>SHOP NOW</p>
              </a>
            </div></div>
          </div>
        </li></ul></nav></header>'''
        outline = slim_html_for_ai(html, BASE)['navigation_outline']
        self.assertNotIn('Premier Haven', outline)
        self.assertIn('[a] New -> /collections/new\n  [label] Yarn\n    [a] New Yarns', outline)
        self.assertIn('  [label] Patterns\n    [a] New Patterns', outline)

    def test_sibling_product_card_links_do_not_name_the_collection_menu(self):
        html = '''<header><nav><ul><li><a href="/collections/shop-all">Shop</a>
          <div class="mega-menu-panel">
            <div class="header-megamenu__card header-megamenu__card--product">
              <a class="header-megamenu__card-image" href="/products/avocado-oil">
                <img src="oil.jpg" alt="Pure Avocado Oil"></a>
              <a class="header-megamenu__card-title" href="/products/avocado-oil">PURE AVOCADO OIL</a>
              <p class="header-megamenu__card-description">Meet your new culinary companion!</p>
              <span class="price__regular">$18.99</span>
              <a class="header-megamenu__card-cta" href="/products/avocado-oil">SHOP NOW</a>
            </div>
            <div class="menu-columns">
              <div><span>Condiments</span>
                <a href="/collections/condiments">Shop All Condiments</a></div>
              <div><span>Cooking Essentials</span>
                <a href="/collections/cooking-essentials">Shop Cooking Essentials</a></div>
            </div>
          </div>
        </li></ul></nav></header>'''
        outline = slim_html_for_ai(html, BASE)['navigation_outline']
        self.assertNotIn('PURE AVOCADO OIL', outline)
        self.assertNotIn('Meet your new culinary companion!', outline)
        self.assertNotIn('$18.99', outline)
        self.assertNotIn('SHOP NOW', outline)
        self.assertIn('[a] Shop -> /collections/shop-all', outline)
        self.assertIn('[a] Shop All Condiments -> /collections/condiments', outline)
        self.assertIn('[a] Shop Cooking Essentials -> /collections/cooking-essentials', outline)

    def test_link_root_and_same_named_child_label_collapse_to_one_owner(self):
        html = '''<header><nav><ul><li>
          <a href="/collections/shop-all">Shop</a>
          <div><span>Shop</span><div class="menu-columns">
            <div><a href="/collections/condiments">Condiments</a></div>
            <div><a href="/collections/cooking-essentials">Cooking Essentials</a></div>
          </div></div>
        </li></ul></nav></header>'''
        outline = slim_html_for_ai(html, BASE)['navigation_outline']
        self.assertEqual(outline.count('[a] Shop -> /collections/shop-all'), 1)
        self.assertNotIn('\n  [label] Shop\n', outline)
        self.assertIn('\n  [a] Condiments -> /collections/condiments', outline)
        self.assertIn('\n  [a] Cooking Essentials -> /collections/cooking-essentials', outline)

    def test_root_li_with_image_cards_keeps_non_category_owner_context(self):
        html = '''<header><nav><ul><li class="site-header-nav__item">
          <a class="site-header-nav__parent-link" href="/collections"><span>Collections</span></a>
          <div class="site-header-nav__mega-menu">
            <div><span>FEATURED COLLABS</span><ul>
              <li><a href="/collections/realtree">Realtree</a></li>
              <li><a href="/collections/playboy">Playboy</a></li></ul></div>
            <a class="swiper-slide" href="/collections/affliction" aria-label="AFFLICTION">
              <img src="affliction.jpg">
              <span aria-hidden="true">FT IMG AFFLICTION</span>
              <span>AFFLICTION</span>
            </a>
          </div>
        </li></ul></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        outline = data['navigation_outline']
        self.assertIn('[label] Collections', outline)
        self.assertIn('  [label] FEATURED COLLABS', outline)
        self.assertIn('    [a] Realtree -> /collections/realtree', outline)
        self.assertIn('    [a] Playboy -> /collections/playboy', outline)
        self.assertIn('  [a] AFFLICTION -> /collections/affliction', outline)
        self.assertEqual(data['navigation_coverage']['source_roots'], ['Collections'])

    def test_aria_hidden_card_text_does_not_pollute_visible_category_label(self):
        html = '''<header><nav><a class="swiper-slide" href="/collections/affliction"
          aria-label="AFFLICTION"><img src="affliction.jpg">
          <span aria-hidden="true">FT IMG AFFLICTION</span>
          <span>AFFLICTION</span></a></nav></header>'''
        outline = slim_html_for_ai(html, BASE)['navigation_outline']
        self.assertEqual(outline.count('[a] AFFLICTION -> /collections/affliction'), 1)
        self.assertNotIn('FT IMG', outline)
        self.assertNotIn('AFFLICTION AFFLICTION', outline)

    def test_desktop_hidden_carousel_does_not_duplicate_desktop_menu_branches(self):
        html = '''<header><nav class="site-header-nav hidden desktop:block"><ul>
          <li class="site-header-nav__item">
            <a href="/collections"><span>Collections</span></a>
            <div class="site-header-nav__mega-menu">
              <a href="/collections/affliction"><span>AFFLICTION</span></a>
            </div>
          </li>
          <li><div class="desktop:hidden mobile-carousel">
            <a href="/collections/affliction"><span>AFFLICTION</span></a>
            <a href="/collections/bigface"><span>BIGFACE</span></a>
          </div></li>
        </ul></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        outline = data['navigation_outline']
        self.assertIn('[label] Collections\n  [a] AFFLICTION -> /collections/affliction',
                      outline)
        self.assertEqual(outline.count('[a] AFFLICTION -> /collections/affliction'), 1)
        self.assertNotIn('BIGFACE', outline)
        self.assertNotIn(BASE + 'collections/bigface', data['source_navigation_urls'])

    def test_mobile_only_home_strip_is_not_promoted_as_desktop_home_series(self):
        html = '''<header><nav><a href="/collections/menu">Menu Collection</a></nav></header>
        <main>
          <section class="mobile-categories-list hp-section">
            <a href="/collections/mobile-only"><h3>Mobile Only</h3></a>
          </section>
          <section class="image-grid hp-section">
            <a href="/collections/desktop-card"><h3>Desktop Card</h3></a>
          </section>
          <section style="display: none !important">
            <a href="/collections/inline-hidden"><h3>Inline Hidden</h3></a>
          </section>
          <section class="d-lg-none">
            <a href="/collections/responsive-hidden"><h3>Responsive Hidden</h3></a>
          </section>
        </main>'''
        data = slim_html_for_ai(html, BASE)
        self.assertEqual([card['url'] for card in data['home_cards']],
                         [BASE + 'collections/desktop-card'])
        self.assertIn('[home] Desktop Card -> /collections/desktop-card', data['text'])
        self.assertNotIn('Mobile Only', data['text'])
        self.assertNotIn('Inline Hidden', data['text'])
        self.assertNotIn('Responsive Hidden', data['text'])

    def test_category_href_whitespace_is_stripped_before_coverage_and_output(self):
        html = '''<header><nav><a href="/collections/the-simpsons ">The Simpsons</a>
          <a href="/collections/dc">DC Comics</a></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        expected = BASE + 'collections/the-simpsons'
        self.assertIn('[a] The Simpsons -> /collections/the-simpsons',
                      data['navigation_outline'])
        self.assertNotIn('/collections/the-simpsons ', data['navigation_outline'])
        self.assertIn(expected, data['valid_urls'])
        self.assertIn(expected, data['source_navigation_urls'])
        self.assertNotIn(expected + ' ', data['valid_urls'])
        self.assertEqual(data['navigation_coverage']['ratio'], 1.0)

    def test_visible_category_carousel_cards_are_siblings_not_parent_and_child(self):
        html = '''<header><nav><ul><li>
          <a href="/collections"><span>Collections</span></a>
          <div class="site-header-nav__mega-menu">
            <swiper-carousel><div class="swiper"><div class="swiper-wrapper">
              <a class="swiper-slide" href="/collections/affliction">
                <img src="affliction.jpg"><span>AFFLICTION</span></a>
              <a class="swiper-slide" href="/collections/whiskey-bent">
                <img src="whiskey.jpg"><span>WHISKEY BENT</span></a>
            </div></div></swiper-carousel>
          </div>
        </li></ul></nav></header>'''
        outline = slim_html_for_ai(html, BASE)['navigation_outline']
        self.assertIn('[label] Collections', outline)
        affliction = next(line for line in outline.splitlines()
                          if '-> /collections/affliction' in line)
        whiskey = next(line for line in outline.splitlines()
                       if '-> /collections/whiskey-bent' in line)
        self.assertEqual(len(affliction) - len(affliction.lstrip()),
                         len(whiskey) - len(whiskey.lstrip()))
        self.assertNotIn(
            '[a] AFFLICTION -> /collections/affliction\n    [a] WHISKEY BENT',
            outline)

    def test_normal_thumbnail_menu_link_wins_over_same_url_large_promo_card(self):
        html = '''<header><nav><ul><li>
          <a href="/collections/youth">Youth</a>
          <div class="menu-columns"><span>UNDERWEAR</span>
            <a class="header-thumb-card" href="/collections/youth-packs">
              <img src="packs-thumb.jpg"><span>Packs</span></a>
          </div>
          <swiper-carousel><div class="swiper-wrapper">
            <a class="swiper-slide" href="/collections/youth-packs">
              <img src="packs-large.jpg">
              <span aria-hidden="true">FT IMG PACKS</span><span>PACKS</span></a>
          </div></swiper-carousel>
        </li></ul></nav></header>'''
        outline = slim_html_for_ai(html, BASE)['navigation_outline']
        self.assertIn('[a] Packs -> /collections/youth-packs', outline)
        self.assertEqual(outline.count('-> /collections/youth-packs'), 1)
        self.assertNotIn('FT IMG', outline)

    def test_leaf_heavy_root_detector_does_not_force_self_heal_when_urls_are_complete(self):
        false_roots = ['Item 0'] + [f'Colour {index}' for index in range(10)]
        with patch('core.simple_nav._source_root_labels', return_value=false_roots):
            data = slim_html_for_ai(menu(), BASE)
        coverage = data['navigation_coverage']
        self.assertEqual(coverage['ratio'], 1.0)
        self.assertLess(coverage['root_ratio'], OUTLINE_COVERAGE_THRESHOLD)
        self.assertEqual(coverage['root_basis'], 'source_outline_intersection')
        self.assertEqual(coverage['effective_root_ratio'], 1.0)
        self.assertFalse(coverage['needs_self_heal'])

    def test_separate_desktop_root_bar_avoids_unnecessary_self_heal(self):
        html = '''<header>
        <nav class="hidden lg:flex"><ul>
          <li><a href="/collections/new">New</a></li>
          <li><a href="/pages/men">Men</a></li>
          <li><a href="/pages/women">Women</a></li>
          <li><a href="/collections/youth">Youth</a></li>
          <li><a href="/collections/gameday">Gameday</a></li>
          <li><a href="/pages/gifts">Gift Guides</a></li>
        </ul></nav>
        <div data-comp="DesktopMenu">
          <nav><ul><li><a href="/collections/new">New Arrivals</a></li></ul></nav>
          <nav><ul><li><a href="/collections/new">Men's New Arrivals</a></li>
            <li><a href="/collections/mens-polos">Polos</a></li></ul></nav>
          <nav><ul><li><a href="/collections/youth">Youth</a></li>
            <li><a href="/collections/womens-dresses">Dresses</a></li></ul></nav>
          <nav><ul><li><a href="/collections/gameday">Gameday Collection</a></li></ul></nav>
          <nav><ul><li><a href="/collections/new">Holiday Gifts</a></li></ul></nav>
        </div></header>'''
        data = slim_html_for_ai(html, BASE)
        coverage = data['navigation_coverage']
        expected = ['New', 'Men', 'Women', 'Youth', 'Gameday', 'Gift Guides']
        self.assertEqual(coverage['source_roots'], expected)
        self.assertEqual(coverage['primary_root_bar'], expected)
        self.assertEqual(coverage['ratio'], 1.0)
        self.assertGreaterEqual(coverage['root_ratio'], OUTLINE_COVERAGE_THRESHOLD)
        self.assertFalse(coverage['needs_self_heal'])

    def test_concatenated_sibling_labels_are_rejected(self):
        outline = '\n'.join([
            '[a] Item 0 -> /collections/item-0',
            '[a] Item 1 -> /collections/item-1',
            '[a] Item 2 -> /collections/item-2',
        ])
        categories = {'Item 0 Item 1 Item 2': BASE + 'collections/item-0'}
        with self.assertRaisesRegex(CrawlError, '混入多个兄弟菜单文本'):
            _reject_exploded_labels(categories, outline)

    def test_ancestor_words_do_not_turn_two_nested_headings_into_siblings(self):
        outline = '\n'.join([
            '[label] Men',
            '  [label] All Men',
            '    [label] Shop by Activity',
            '      [a] Golf -> /collections/golf',
        ])
        categories = {
            'Men,Men All Men Shop by Activity,Men All Men Shop by Activity Golf':
                BASE + 'collections/golf'}
        _reject_exploded_labels(categories, outline)

    def test_adjacent_nested_headings_may_be_flattened_to_three_levels(self):
        outline = '''[label] Phone Cases
  [label] Shop by iPhone Size
    [label] iPhone 17 Series
      [a] iPhone 17 Pro Max -> /collections/iphone-17-pro-max'''
        categories = {
            ('Phone Cases,Phone Cases Shop by iPhone Size iPhone 17 Series,'
             'Phone Cases Shop by iPhone Size iPhone 17 Series iPhone 17 Pro Max'):
                BASE + 'collections/iphone-17-pro-max'}
        _reject_exploded_labels(categories, outline)

    def test_adjacent_nested_headings_may_use_slash_when_flattened(self):
        outline = '''[label] Phone Cases
  [label] Shop by iPhone Size
    [label] iPhone 17 Series
      [a] iPhone 17 Pro Max -> /collections/iphone-17-pro-max'''
        categories = {
            ('Phone Cases,Phone Cases Shop by iPhone Size / iPhone 17 Series,'
             'Phone Cases Shop by iPhone Size / iPhone 17 Series iPhone 17 Pro Max'):
                BASE + 'collections/iphone-17-pro-max'}
        _reject_exploded_labels(categories, outline)

    def test_single_generic_desktop_root_owns_richer_drawer_tree(self):
        categories = {
            'Cocktails,Cocktails By spirit,Cocktails By spirit Vodka':
                BASE + 'collections/vodka',
            'Accessories,Accessories Shakers': BASE + 'collections/shakers',
        }
        fixed, warnings = _canonicalize_ai_roots(categories, ['Shop'])
        self.assertEqual(list(fixed), [
            'Shop,Shop Cocktails,Shop Cocktails By spirit Vodka',
            'Shop,Shop Accessories,Shop Accessories Shakers'])
        self.assertTrue(all(w['code'] == 'prepended_primary_root' for w in warnings))

    def test_drawer_leaves_attach_to_verified_desktop_aggregator_urls(self):
        categories = {
            'Shop,Shop Cocktail Makers': BASE + 'collections/cocktail-makers',
            'Shop,Shop Cocktails': BASE + 'collections/cocktails',
            'Shop,Shop Accessories': BASE + 'collections/barware-accessories',
            'By spirit,By spirit Vodka': BASE + 'collections/vodka-cocktails',
            'Shakers': BASE + 'collections/barware-accessories/accessories_shakers',
        }
        fixed, _ = _canonicalize_ai_roots(categories, ['Shop'])
        self.assertIn('Shop,Shop Cocktails,Shop Cocktails By spirit Vodka', fixed)
        self.assertIn('Shop,Shop Accessories,Shop Accessories Shakers', fixed)
        self.assertEqual({key.split(',', 1)[0] for key in fixed}, {'Shop'})

    def test_decomposed_svg_anchor_is_not_reused_as_home_card(self):
        html = '''<body><header><nav><a href="/collections/shop">Shop</a></nav></header>
        <main><svg><a href="/collections/stale">Stale SVG link</a></svg>
        <a href="/collections/visible">Visible collection</a></main></body>'''
        data = slim_html_for_ai(html, BASE)
        self.assertNotIn('Stale SVG link', data['text'])
        self.assertIn('/collections/visible', data['text'])

    def test_repeated_control_keys_use_sibling_card_titles(self):
        html = '''<div class="menu-card"><a href="/collections/full"></a>
          <div class="card-title">FULL SIZE</div></div>
        <div class="menu-card"><a href="/collections/mini"></a>
          <div class="card-title">MINI SIZE</div></div>'''
        categories = {
            'Shop,Shop Shop By Size': BASE + 'collections/full',
            'Shop,Shop Shop By Size (2)': BASE + 'collections/mini',
        }
        fixed, warnings = _repair_repeated_control_labels(categories, html, BASE)
        self.assertEqual(list(fixed), [
            'Shop,Shop Shop By Size,Shop Shop By Size FULL SIZE',
            'Shop,Shop Shop By Size,Shop Shop By Size MINI SIZE'])
        self.assertEqual([w['code'] for w in warnings],
                         ['restored_menu_card_title', 'restored_menu_card_title'])

    def test_template_strings_and_sibling_panels_preserve_roots_and_hierarchy(self):
        html = '''<header><nav><ul>
        <li><div><a href="#">FEATURED</a></div><template><div><ul>
          <li><div><span><a href="/collections/all"><span>SHOP ALL PRODUCTS</span></a></span></div></li>
          <li><div><span>SHOP BY COLLECTION</span></div><div><ul>
            <li><div><span><a href="/collections/cocoa"><span>Cocoa Butter</span></a></span></div></li>
          </ul></div></li>
        </ul></div></template></li>
        <li><div><a href="/collections/body">BODY</a></div><template><div><ul>
          <li><div><span><a href="/collections/body-oils"><span>Lotions + Oils</span></a></span></div></li>
        </ul></div></template></li>
        </ul></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        outline = data['navigation_outline']
        self.assertIn('[label] FEATURED\n  [a] SHOP ALL PRODUCTS -> /collections/all', outline)
        self.assertIn('  [label] SHOP BY COLLECTION\n    [a] Cocoa Butter -> /collections/cocoa', outline)
        self.assertIn('[a] BODY -> /collections/body\n  [a] Lotions + Oils -> /collections/body-oils', outline)
        self.assertEqual(data['navigation_coverage']['ratio'], 1.0)
        self.assertEqual(data['navigation_coverage']['root_ratio'], 1.0)
        self.assertEqual(data['navigation_coverage']['source_roots'], ['FEATURED', 'BODY'])
        self.assertFalse(data['navigation_coverage']['needs_self_heal'])
        self.assertIn('<template>', data['self_heal_html'])

    def test_primary_root_ledger_excludes_categoryless_learn_search_and_cart(self):
        html = '''<header>
        <nav class="desktop-primary"><ul>
          <li><button>Shop</button><div><a href="/collections/all">Shop All</a></div></li>
          <li><a href="/pages/learn">Learn</a></li>
          <li><a href="/products/e-gift-card">E-Gift Cards</a></li>
          <li><button>Toggle site search panel</button></li>
          <li><a href="/cart">0 items in cart</a></li>
        </ul></nav>
        <nav class="desktop-dialog"><div>Global Navigation Dialog</div>
          <a href="/collections/all">Shop All Gift Boxes</a></nav>
        </header>'''
        data = slim_html_for_ai(html, BASE)
        coverage = data['navigation_coverage']
        self.assertEqual(coverage['primary_root_bar'], ['Shop'])
        self.assertEqual(coverage['source_roots'], ['Shop'])
        self.assertEqual(coverage['effective_root_ratio'], 1.0)

    def alo_sibling_panel_data(self):
        html = '''<header><nav class="main-navbar">
        <div class="row d-md-none main-navbar__row__mobile">
        <div id="collapseMenu"><div><ul>
          <li id="women--mobile" class="main-nav__item__mobile">Women</li>
          <div class="women--mobile mobile-nav__second-level"><ul>
            <li><a href="/collections/womens-leggings">Leggings</a></li>
            <li class="d-none"><a href="/collections/womens-sale">Sale</a></li>
          </ul></div>
          <li id="men--mobile" class="main-nav__item__mobile">Men</li>
          <div class="men--mobile mobile-nav__second-level"><ul>
            <li><a href="/collections/mens-shorts">Shorts</a></li>
            <li class="d-none"><a href="/collections/mens-sale">Sale</a></li>
          </ul></div>
          <li id="accessories--mobile" class="main-nav__item__mobile">Accessories</li>
          <div class="accessories--mobile mobile-nav__second-level"><ul>
            <li><a href="/collections/hats">Hats</a></li>
          </ul></div>
          <li id="shoes--mobile" class="main-nav__item__mobile">Shoes</li>
          <div class="shoes--mobile mobile-nav__second-level"><ul>
            <li><a href="/collections/sneakers">Sneakers</a></li>
          </ul></div>
        </ul></div></div></div></nav></header>'''
        return slim_html_for_ai(html, BASE)

    def test_sibling_panels_use_shallow_trigger_labels_as_source_roots(self):
        data = self.alo_sibling_panel_data()
        coverage = data['navigation_coverage']
        self.assertEqual(coverage['source_roots'],
                         ['Women', 'Men', 'Accessories', 'Shoes'])
        self.assertEqual(coverage['outline_roots'], ['Women'])
        self.assertTrue(coverage['needs_self_heal'])
        hidden = {BASE + 'collections/womens-sale', BASE + 'collections/mens-sale'}
        self.assertTrue(hidden.isdisjoint(data['valid_urls']))
        self.assertTrue(hidden.isdisjoint(data['source_navigation_urls']))
        self.assertIn(BASE + 'collections/womens-leggings', data['source_navigation_urls'])
        self.assertIn(BASE + 'collections/mens-shorts', data['source_navigation_urls'])

    def test_complete_sibling_panel_self_heal_candidate_passes_root_validation(self):
        data = self.alo_sibling_panel_data()
        entries = [
            {'key': 'Women', 'url': BASE + 'collections/womens-leggings'},
            {'key': 'Men', 'url': BASE + 'collections/mens-shorts'},
            {'key': 'Accessories', 'url': BASE + 'collections/hats'},
            {'key': 'Shoes', 'url': BASE + 'collections/sneakers'},
        ]
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=self.launch_with(entries, [])):
            categories = extract_url_json_with_ai(directory, BASE, data)
            self.assertEqual(list(categories), ['Women', 'Men', 'Accessories', 'Shoes'])
            self.assertFalse(any('Sale' in key for key in categories))
            self.assertFalse(any(url.endswith(('/womens-sale', '/mens-sale'))
                                 for url in categories.values()))
            coverage = read_json(Path(directory) / 'evidence/navigation_coverage.json')
            self.assertEqual(coverage['final_ai_root_ratio'], 1.0)
            self.assertTrue(coverage['self_healed'])

    def broken_outline_data(self):
        with patch('core.simple_nav._outline', return_value=['[a] Item 0 -> /collections/item-0']):
            return slim_html_for_ai(menu(), BASE)

    @staticmethod
    def launch_with(entries, commands):
        def launch(command, **kwargs):
            commands.append(command)
            sandbox = Path(kwargs['cwd'])
            (sandbox / 'extract_custom.py').write_text('# isolated generated parser\n', encoding='utf-8')
            (sandbox / 'category_candidate.json').write_text(
                json.dumps({'entries': entries}), encoding='utf-8')
            process = MagicMock()
            process.returncode = 0
            return process
        return launch

    def test_low_coverage_runs_isolated_self_heal_and_persists_audit(self):
        data = self.broken_outline_data()
        self.assertEqual(data['navigation_coverage']['ratio'], .2)
        self.assertTrue(data['navigation_coverage']['needs_self_heal'])
        entries = [{'key': f'Item {index}', 'url': BASE + f'collections/item-{index}'}
                   for index in range(5)]
        commands = []
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=self.launch_with(entries, commands)) as popen:
            categories = extract_url_json_with_ai(directory, BASE, data)
            root = Path(directory)
            self.assertEqual(len(categories), 5)
            command = commands[0]
            self.assertNotIn('--sandbox', command)
            self.assertNotIn('--ignore-user-config', command)
            self.assertIn('--ignore-rules', command)
            self.assertIn('sandbox_workspace_write.network_access=false', command)
            self.assertIn('shell_environment_policy.inherit=none', command)
            self.assertIn('--approve-for-me', command)
            self.assertIn('--trust', command)
            self.assertNotIn('approval_policy=never', command)
            self.assertTrue(any(command[i:i + 2] == ['--enable', 'code_mode_host']
                                for i in range(len(command) - 1)))
            self.assertFalse(any(command[i:i + 2] == ['--disable', 'shell_tool']
                                 for i in range(len(command) - 1)))
            self.assertFalse(any(command[i:i + 2] == ['--disable', 'code_mode_host']
                                 for i in range(len(command) - 1)))
            child_env = popen.call_args.kwargs['env']
            self.assertNotIn('CODEX_CI', child_env)
            self.assertNotIn('CODEX_PERMISSION_PROFILE', child_env)
            self.assertEqual(len(commands), 1)
            self.assertFalse((root / 'evidence/self_heal_parser.py').exists())
            self.assertTrue((root / 'evidence/self_heal_candidate.json').is_file())
            report = read_json(root / 'evidence/self_heal_report.json')
            self.assertEqual(len(report['parser_sha256']), 64)
            self.assertFalse(report['generated_parser_retained'])
            self.assertEqual(report['sandbox_base'], 'read-only')
            self.assertEqual(report['approval'], 'automatic_review')
            self.assertEqual(report['write_scope'], 'temporary_workspace_only')
            self.assertTrue((root / 'evidence/navigation_self_healed.json').is_file())
            self.assertNotIn('self_heal_html', read_json(root / 'evidence/simple_nav_sources.json'))
            self.assertEqual(read_json(root / 'navigation_invocation.json')['invocation_modes'], ['self_heal'])
            self.assertEqual(read_json(root / 'navigation_invocation.json')['timeout_seconds'], 300)
            self.assertEqual(read_json(root / 'warnings.json')[0]['code'], 'navigation_self_healed')

    def test_self_heal_keeps_same_sandbox_process_during_timeout_grace(self):
        data = self.broken_outline_data()
        entries = [{'key': f'Item {index}', 'url': BASE + f'collections/item-{index}'}
                   for index in range(5)]
        holder = {}

        def launch(command, **kwargs):
            sandbox = Path(kwargs['cwd'])
            (sandbox / 'extract_custom.py').write_text('# isolated generated parser\n', encoding='utf-8')
            process = MagicMock()
            process.returncode = 0
            calls = {'count': 0}

            def communicate(*args, **call_kwargs):
                calls['count'] += 1
                if calls['count'] == 1:
                    raise subprocess.TimeoutExpired('codex', AI_TIMEOUT_SECONDS)
                (sandbox / 'category_candidate.json').write_text(
                    json.dumps({'entries': entries}), encoding='utf-8')
                return ('', None)

            process.communicate.side_effect = communicate
            holder['process'] = process
            return process

        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=launch):
            categories = extract_url_json_with_ai(directory, BASE, data)
            self.assertEqual(len(categories), 5)
            process = holder['process']
            waits = [call.kwargs['timeout'] for call in process.communicate.call_args_list]
            self.assertEqual(waits, [AI_TIMEOUT_SECONDS,
                                     SELF_HEAL_TIMEOUT_SECONDS - AI_TIMEOUT_SECONDS])
            report = read_json(Path(directory) / 'evidence/self_heal_report.json')
            self.assertTrue(report['soft_timeout_extended'])
            self.assertFalse(report['hard_timeout_recovered_candidate'])
            invocation = read_json(Path(directory) / 'navigation_invocation.json')
            self.assertEqual(invocation['invocations'], 1)
            self.assertEqual(invocation['timeout_seconds'], SELF_HEAL_TIMEOUT_SECONDS)

    def test_hard_timeout_accepts_existing_valid_candidate_without_running_parser(self):
        data = self.broken_outline_data()
        entries = [{'key': f'Item {index}', 'url': BASE + f'collections/item-{index}'}
                   for index in range(5)]

        def launch(command, **kwargs):
            sandbox = Path(kwargs['cwd'])
            (sandbox / 'extract_custom.py').write_text('# never executed by parent\n', encoding='utf-8')
            (sandbox / 'category_candidate.json').write_text(
                json.dumps({'entries': entries}), encoding='utf-8')
            process = MagicMock()
            process.returncode = -9
            process.communicate.side_effect = subprocess.TimeoutExpired('codex', AI_TIMEOUT_SECONDS)
            return process

        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=launch), patch(
                'core.simple_nav._terminate_ai_process') as terminate:
            categories = extract_url_json_with_ai(directory, BASE, data)
            self.assertEqual(len(categories), 5)
            terminate.assert_called_once()
            report = read_json(Path(directory) / 'evidence/self_heal_report.json')
            self.assertTrue(report['hard_timeout_recovered_candidate'])

    def test_leaf_heavy_raw_roots_use_source_outline_intersection(self):
        data = self.broken_outline_data()
        data['navigation_coverage'].update({
            'source_roots': ['Shop'] + [f'Colour {index}' for index in range(10)],
            'outline_roots': ['Shop'],
            'root_ratio': 1 / 11,
            'needs_self_heal': True,
        })
        entries = [{'key': 'Shop' if index == 0 else f'Shop,Shop Item {index}',
                    'url': BASE + f'collections/item-{index}'} for index in range(5)]
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=self.launch_with(entries, [])):
            categories = extract_url_json_with_ai(directory, BASE, data)
            self.assertEqual(len(categories), 5)
            coverage = read_json(Path(directory) / 'evidence/navigation_coverage.json')
            self.assertEqual(coverage['final_ai_root_basis'], 'source_outline_intersection')
            self.assertEqual(coverage['final_ai_root_ratio'], 1.0)

    def test_missing_primary_bar_does_not_treat_every_leaf_as_required_root(self):
        data = self.broken_outline_data()
        data['navigation_coverage'].update({
            'source_roots': [f'Leaf {index}' for index in range(120)],
            'primary_root_bar': [],
            'outline_roots': ['Search closed'],
            'root_ratio': 0.0,
            'needs_self_heal': True,
        })
        entries = [
            {'key': f'Owner {index % 5},Owner {index % 5} Item {index}',
             'url': BASE + f'collections/item-{index}'}
            for index in range(5)
        ]
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=self.launch_with(entries, [])):
            categories = extract_url_json_with_ai(directory, BASE, data)
            coverage = read_json(Path(directory) / 'evidence/navigation_coverage.json')
        self.assertEqual(len(categories), 5)
        self.assertEqual(coverage['final_ai_output_ratio'], 1.0)
        self.assertEqual(coverage['final_ai_root_ratio'], 1.0)
        self.assertEqual(coverage['final_ai_root_basis'],
                         'url_coverage_plus_structural_roots')
        self.assertEqual(set(key.split(',', 1)[0] for key in categories),
                         {f'Owner {index}' for index in range(5)})

    def test_leaf_heavy_primary_bar_does_not_treat_panel_leaves_as_roots(self):
        data = self.broken_outline_data()
        leaves = [f'Panel leaf {index}' for index in range(60)]
        data['navigation_coverage'].update({
            'source_roots': leaves,
            'primary_root_bar': leaves,
            'outline_roots': ['Women', 'Men', 'Kids', 'Gift Guide'],
            'root_ratio': 0.0,
            'needs_self_heal': True,
        })
        entries = [
            {'key': f'Owner {index % 4},Owner {index % 4} Item {index}',
             'url': BASE + f'collections/item-{index}'}
            for index in range(5)
        ]
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=self.launch_with(entries, [])):
            categories = extract_url_json_with_ai(directory, BASE, data)
            coverage = read_json(Path(directory) / 'evidence/navigation_coverage.json')
        self.assertEqual(len(categories), 5)
        self.assertEqual(coverage['final_ai_output_ratio'], 1.0)
        self.assertEqual(coverage['final_ai_root_ratio'], 1.0)
        self.assertEqual(coverage['final_ai_root_basis'],
                         'url_coverage_plus_structural_roots')

    def test_hard_timeout_without_candidate_remains_retryable_failure(self):
        data = self.broken_outline_data()

        def launch(command, **kwargs):
            sandbox = Path(kwargs['cwd'])
            (sandbox / 'extract_custom.py').write_text('# incomplete isolated parser\n', encoding='utf-8')
            process = MagicMock()
            process.pid = 123
            process.returncode = -9
            process.communicate.side_effect = subprocess.TimeoutExpired('codex', AI_TIMEOUT_SECONDS)
            return process

        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=launch), patch(
                'core.simple_nav._terminate_ai_process') as terminate:
            with self.assertRaisesRegex(CrawlError, '超过 300 秒且无可验证候选'):
                extract_url_json_with_ai(directory, BASE, data)
            terminate.assert_called_once()
            self.assertFalse((Path(directory) / 'url.json').exists())

    def test_incomplete_self_heal_never_publishes_partial_directory(self):
        data = self.broken_outline_data()
        entries = [{'key': 'Item 0', 'url': BASE + 'collections/item-0'}]
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=self.launch_with(entries, [])):
            with self.assertRaisesRegex(CrawlError, '拒绝发布不完整目录'):
                extract_url_json_with_ai(directory, BASE, data)
            root = Path(directory)
            self.assertFalse((root / 'url.json').exists())
            self.assertEqual(read_json(root / 'warnings.json')[0]['code'], 'navigation_self_heal_incomplete')

    def test_normal_ai_output_below_coverage_runs_one_self_heal_retry(self):
        data = slim_html_for_ai(menu(), BASE)
        commands = []

        def launch(command, **kwargs):
            commands.append(command)
            sandbox = Path(kwargs['cwd'])
            if '--sandbox' in command:
                output = Path(command[command.index('-o') + 1])
                output.write_text(json.dumps({'entries': [
                    {'key': 'Item 0', 'url': BASE + 'collections/item-0'}]}), encoding='utf-8')
            else:
                entries = [{'key': f'Item {index}',
                            'url': BASE + f'collections/item-{index}'}
                           for index in range(5)]
                (sandbox / 'extract_custom.py').write_text(
                    '# isolated generated parser\n', encoding='utf-8')
                (sandbox / 'category_candidate.json').write_text(
                    json.dumps({'entries': entries}), encoding='utf-8')
            process = MagicMock()
            process.returncode = 0
            return process

        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=launch):
            categories = extract_url_json_with_ai(directory, BASE, data)
            root = Path(directory)
            self.assertEqual(len(categories), 5)
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][commands[0].index('--sandbox') + 1], 'read-only')
            self.assertIn('--approve-for-me', commands[1])
            self.assertEqual(read_json(root / 'navigation_invocation.json')['invocation_modes'],
                             ['outline', 'self_heal'])
            warnings = read_json(root / 'warnings.json')
            self.assertEqual(warnings[0]['code'], 'navigation_self_healed')
            self.assertEqual(warnings[1]['code'], 'navigation_output_self_heal_retry')
            self.assertTrue((root / 'url.json').exists())

    def test_all_urls_flattened_under_wrong_root_are_rejected(self):
        html = '''<header><nav><ul>
        <li><a href="/collections/men">Men</a><ul>
          <li><a href="/collections/men-new">New</a></li>
          <li><a href="/collections/men-sale">Sale</a></li></ul></li>
        <li><a href="/collections/women">Women</a><ul>
          <li><a href="/collections/women-new">New</a></li>
          <li><a href="/collections/women-sale">Sale</a></li></ul></li>
        </ul></nav></header>'''
        data = slim_html_for_ai(html, BASE)
        entries = [{'key': f'Wrong,Wrong Item {index}', 'url': url}
                   for index, url in enumerate(data['source_navigation_urls'])]

        def launch(command, **kwargs):
            sandbox = Path(kwargs['cwd'])
            if '--sandbox' in command:
                output = Path(command[command.index('-o') + 1])
                output.write_text(json.dumps({'entries': entries}), encoding='utf-8')
            else:
                (sandbox / 'extract_custom.py').write_text('# isolated parser\n', encoding='utf-8')
                (sandbox / 'category_candidate.json').write_text(
                    json.dumps({'entries': entries}), encoding='utf-8')
            process = MagicMock()
            process.returncode = 0
            return process

        with tempfile.TemporaryDirectory() as directory, patch(
                'core.simple_nav.shutil.which', return_value='codex.exe'), patch(
                'core.simple_nav.subprocess.Popen', side_effect=launch):
            with self.assertRaisesRegex(CrawlError, '拒绝发布不完整目录'):
                extract_url_json_with_ai(directory, BASE, data)
            coverage = read_json(Path(directory) / 'evidence/navigation_coverage.json')
            self.assertEqual(coverage['final_ai_output_ratio'], 1.0)
            self.assertEqual(coverage['final_ai_root_ratio'], 0.0)
            self.assertFalse((Path(directory) / 'url.json').exists())


if __name__ == '__main__':
    unittest.main()
