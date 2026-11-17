import json
import unittest
from types import SimpleNamespace

from core.braun_navigation import is_mia_storefront, mia_navigation


def entry(kind, **fields):
    return {'fields': dict(contentType=kind, **fields)}


def page(kind, slug):
    return entry(kind, internalLink='/' + slug)


def link(label, target):
    return entry('miaLink', text=label, internalLink=target)


def document(content, site='Braun'):
    props = {'settings': {'siteName': site}, 'content': content}
    data = {'props': {'pageProps': props}, 'assetPrefix': '/en-us'}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>'


class BraunNavigationTests(unittest.TestCase):
    def test_only_displayed_shelves_and_typed_collections_enter_catalog(self):
        base = 'https://shop.example/en-us'
        collection = page('miaCategoryPage', 'grooming')
        header = entry('miaHeader', menuItems=[
            entry('menuItemV2', menuText='Products', additionalMenuCardsTitle='Categories',
                  menuCardText='Most Popular', links=[
                      link('Shavers', collection),
                      link('View all products', page('miaCategoryPage', 'all-products')),
                  ], menuCards=[
                      entry('callout', title='Shavers', callToActions=[link('Buy', collection)]),
                      entry('callout', title='Kit', callToActions=[link('Buy', page('product', 'kit'))]),
                  ]),
            entry('menuItemV2', menuText='Deals', page=page('contentPage', 'deals')),
            entry('menuItemV2', menuText='Care+', page=page('contentPage', 'care')),
            entry('menuItemV2', menuText='Advice', page=page('article', 'tips')),
        ])
        cat_card = entry('miaCategoryShelfCard', title='Shavers', internalLink=collection)
        new_card = entry('miaCategoryShelfCard', title='Brushes',
                         internalLink=page('miaCategoryPage', 'brushes'))
        wrong_country = entry('miaCategoryShelfCard', title='International',
                              internalLink=page('miaCategoryPage', 'de-de/range'))
        shelf = entry('miaCategoryShelf', categories=[entry('group', bigCards=[cat_card],
                                      smallCards=[new_card, wrong_country])])
        section = entry('miaSectionContainer', sectionContent=[shelf])
        homepage = document({'headerMenu': header, 'page': {'pageContent': [section]},
                             'footerMenu': entry('miaHeader', menuItems=[
                                 entry('menuItemV2', menuText='Discontinued',
                                       page=page('miaCategoryPage', 'old'))])})
        product_shelf = entry('shelf007', products=[
            entry('product', sku='123', internalLink='/a-product')])
        calls = []

        def get(url):
            calls.append(url)
            page_content = [entry('sectionContainer', sectionContent=[product_shelf])] if url.endswith('/deals') else []
            return SimpleNamespace(url=url, text=document({
                'page': {'pageContent': page_content}, 'headerMenu': product_shelf}))

        data = mia_navigation(homepage, base, client=SimpleNamespace(get=get))
        self.assertEqual(list(data.items()), [
            ('Products,Products Categories,Products Categories Shavers', base + '/grooming'),
            ('Products', base + '/all-products'),
            ('Products,Products Most Popular,Products Most Popular Shavers', base + '/grooming'),
            ('Deals', base + '/deals'), ('brushes', base + '/brushes')])
        self.assertEqual(calls, [base + '/deals', base + '/care'])

    def test_detection_requires_both_braun_and_mia_header(self):
        content = {'headerMenu': entry('miaHeader', menuItems=[entry('menuItemV2')])}
        self.assertTrue(is_mia_storefront(document(content)))
        self.assertFalse(is_mia_storefront(document(content, site='Other store')))
        self.assertFalse(is_mia_storefront(document({'headerMenu': entry('unrelatedHeader')})))
