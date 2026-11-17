import unittest
from core.menu_tree import build_tree,tree_data,verify_sources


class MenuDropdownTests(unittest.TestCase):
    def test_visible_swatch_label_is_not_replaced_by_aria_label(self):
        from core.menu_tree import label
        from core.html_parser import parse_soup
        a=parse_soup('<a aria-label="Browns"><span aria-hidden="true" class="block">Khaki/Browns</span></a>','lxml').a
        self.assertEqual(label(a),'Khaki/Browns')

    def test_component_owns_columns_and_explicit_shop_all_root(self):
        html='''<header><nav aria-label="Main Desktop Menu"><ul>
        <li><menu-dropdown><a href="#">Jeans</a><details><summary>Expand</summary><div>
        <div><h3>Shop by Wash</h3><a href="/collections/jeans?filter.color=black&amp;filter.color=blue">Blacks</a></div>
        <div><h3>Shop By Fit</h3><a href="/collections/jeans?filter.fit=Skinny">Skinny</a><a href="/collections/all-jeans">SHOP ALL JEANS</a></div>
        </div></details></menu-dropdown></li></ul></nav>
        <nav aria-label="Drawer Menu"><ul><li><a href="/collections/mobile-only">Menu Close</a></li></ul></nav></header>'''
        tree=build_tree(html,'https://example.com/');self.assertTrue(tree['complete'])
        data=tree_data(tree);verify_sources(tree,html,'https://example.com/')
        self.assertEqual(data['Jeans'],'https://example.com/collections/all-jeans')
        self.assertIn('Jeans,Jeans Shop By Fit,Jeans Shop By Fit Skinny',data)
        self.assertEqual(data['Jeans,Jeans Shop by Wash,Jeans Shop by Wash Blacks'],'https://example.com/collections/jeans?filter.color=black&filter.color=blue')
        self.assertFalse(any('Menu Close' in k for k in data))
