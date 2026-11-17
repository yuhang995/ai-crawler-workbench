import unittest

from core.brumate_listing import _read_page, _replay, validate_listing
from core.common import CrawlError


BASE = 'https://shop.example/collections/cups?color=red'


def page(slugs, number, total, has_next):
    cards = ''.join(f'<li data-comp="ProductItem"><a href="/products/{slug}">Photo</a>'
                    f'<a href="/products/{slug}">{slug}</a></li>' for slug in slugs)
    controls = (f'<nav><button aria-current="page">{number}</button>'
                f'<button aria-label="Next page" {"" if has_next else "disabled"}>Next</button></nav>')
    return {'url': BASE + f'&page={number}', 'html': (
        f'<main><a href="/products/recommendation">Outside grid</a><p>{total} Products</p>'
        f'<div data-comp="CollectionGrid"><ul>{cards}</ul>{controls}</div></main>')}


class NostoListingTests(unittest.TestCase):
    def test_complete_pages_are_in_display_order(self):
        pages = [page(['blue', 'red'], 1, 3, True), page(['green'], 2, 3, False)]
        links = _replay(pages, BASE, True)
        self.assertEqual([u.rsplit('/', 1)[-1] for u in links], ['blue', 'red', 'green'])
        for replacement in ([], [pages[0]], [pages[0], pages[0]],
                            [pages[0], page(['red'], 2, 3, False)],
                            [pages[0], dict(pages[1], url=BASE.replace('red', 'blue'))]):
            with self.assertRaises(CrawlError):
                _replay(replacement, BASE, True)

    def test_hydrating_or_cross_store_card_fails(self):
        for invalid in ('<li data-comp="ProductItem">Loading</li>',
                        '<li data-comp="ProductItem"><a href="https://foreign.test/products/r">R</a></li>'):
            html = '<p>1 Product</p><div data-comp="CollectionGrid">' + invalid + '</div>'
            with self.assertRaises(CrawlError):
                _read_page(html, BASE)

    def test_sample_must_match_observed_card_and_raw_links(self):
        pages = [page(['red'], 1, 1, False)]
        links = _replay(pages, BASE, True)
        listing = {'url': BASE, 'urls': links, 'products': [{'handle': 'red', 'id': '21'}],
                   'complete': True, 'pages': pages}
        sample = {'url': links[0], 'product': {'handle': 'red', 'id': '21'}}
        validate_listing(listing, links, [sample])
        with self.assertRaises(CrawlError):
            validate_listing(listing, links, [dict(sample, product={'handle': 'red', 'id': '12'})])
        with self.assertRaises(CrawlError):
            validate_listing(listing, ['https://shop.example/products/forged'])
