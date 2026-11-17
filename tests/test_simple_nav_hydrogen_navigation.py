import unittest

from core.simple_nav import (
    _categories_from_hydrogen_collection_navigation,
    _categories_from_hydrogen_navigation,
)


BASE = 'https://example.com/'


class HydrogenNavigationTests(unittest.TestCase):
    def test_enriched_drawer_keeps_visible_roots_groups_and_filters(self):
        navigation = {'mainLinks': [
            {'label': 'Drinkware +', 'opensDrawer': True,
             'seeAllLink': '/collections/all', 'subCategories': [
                 {'label': 'Drinkware +', 'type': 'collection', 'collections': [
                     {'name': 'FreeSip® Family', 'link': '/collections/freesip',
                      'products': [{'title': 'Featured', 'handle': 'featured'}]},
                     {'name': 'Tumblers', 'link': '/collections/tumblers'},
                 ]},
                 {'label': 'Features', 'type': 'link', 'values': [
                     {'label': 'Leakproof',
                      'link': '/collections/all?features=Leak+Proof'},
                     {'label': 'Featured Product', 'link': '/products/item'},
                 ]},
             ]},
            {'label': 'Bags', 'link': '/collections/bags'},
            {'label': 'Hidden Locale', 'showOnLocale': False,
             'link': '/collections/hidden'},
        ]}
        found = _categories_from_hydrogen_collection_navigation(navigation, BASE)
        self.assertEqual(list(found)[:2], [
            'Drinkware +',
            'Drinkware +,Drinkware + FreeSip® Family',
        ])
        self.assertEqual(
            found['Drinkware +,Drinkware + Features,Drinkware + Features Leakproof'],
            BASE + 'collections/all?features=Leak+Proof')
        self.assertIn('Bags', found)
        self.assertFalse(any('Featured Product' in key or 'Hidden Locale' in key
                             for key in found))

    def test_desktop_groups_keep_collection_tree_and_drop_content(self):
        navigation = {'desktopNavItems': [
            {'emoji': '🔥', 'title': "What's Hot", 'displayWithUtility': False,
             'url': '/collections/new', 'submenu': [
                 {'title': 'Best Sellers', 'links': [
                     {'title': 'All Best Sellers', 'url': '/collections/best'},
                     {'title': 'Eyes Best Sellers', 'url': '/collections/eyes'},
                     {'title': 'Featured Product', 'url': '/products/item'},
                 ]}]},
            {'emoji': '🧁', 'title': 'Fragrance & Body',
             'displayWithUtility': False, 'url': '/pages/body', 'submenu': [
                 {'title': 'By Category', 'links': [
                     {'title': 'Body Cream', 'url': '/collections/body-cream'}]}]},
            {'title': 'Discover', 'displayWithUtility': True,
             'url': '/collections/should-not-appear'},
        ]}
        found = _categories_from_hydrogen_navigation(navigation, BASE)
        self.assertIn("🔥 What's Hot", found)
        self.assertEqual(
            found["🔥 What's Hot,🔥 What's Hot Best Sellers"],
            BASE + 'collections/best')
        self.assertIn(
            "🔥 What's Hot,🔥 What's Hot Best Sellers,🔥 What's Hot Best Sellers Eyes Best Sellers",
            found)
        self.assertIn(
            '🧁 Fragrance & Body,🧁 Fragrance & Body By Category,'
            '🧁 Fragrance & Body By Category Body Cream', found)
        self.assertFalse(any('/products/' in url or '/pages/' in url
                             for url in found.values()))
        self.assertFalse(any(key.startswith('Discover') for key in found))


if __name__ == '__main__':
    unittest.main()
