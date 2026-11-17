import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.common import CrawlError, read_json, save_json
from core.custom_site import ordered_product_urls, run_custom_sample, run_products
from core.shopify_fastpath import product_categories


class CustomProductIdentityTests(unittest.TestCase):
    def product(self, identities, calls):
        def fetch(client, url, evidence):
            calls.append(url)
            pid = identities[url]
            evidence.mkdir(parents=True, exist_ok=True)
            name = 'product-' + pid + '.json'
            save_json(evidence / name, {'url': url, 'id': pid})
            return {'url': url, 'source': 'json', 'product': {'id': pid},
                    'source_mapping': {'kind': 'magento_storefront', 'file': 'evidence/' + name}}
        return fetch

    def test_duplicate_parent_category_advances_and_keeps_every_card(self):
        urls = {name: 'https://store.example/' + name for name in ['a', 'a-red', 'a-blue', 'b', 'b-red', 'c', 'd']}
        identities = {urls[name]: name.split('-')[0] for name in urls}
        first = [urls[name] for name in ['a', 'a-red', 'a-blue']]
        second = [urls[name] for name in ['b', 'b-red', 'c', 'd']]
        calls = []
        listing = lambda client, url: {'format': 'magento_storefront', 'urls': first if url.endswith('/one') else second, 'complete': True}
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.custom_site._collectors', return_value=(listing, self.product(identities, calls))):
            root = Path(directory)
            selected, detail, listings, samples, attempts = run_custom_sample(
                Mock(), root, 'magento', [('one', 'https://store.example/one'), ('two', 'https://store.example/two')])
            self.assertEqual(list(selected), ['two'])
            self.assertEqual(detail, {'two': second})
            self.assertEqual([s['product']['id'] for s in samples], ['b', 'c', 'd'])
            self.assertEqual(attempts[0]['distinct_products'], 1)
            self.assertEqual(attempts[0]['status'], 'insufficient')
            self.assertEqual(ordered_product_urls(detail, listings)[:3], [urls[n] for n in ['b', 'c', 'd']])
            evidence = listings['two']['identity_sources']
            self.assertEqual(list(evidence), [urls['b'], urls['b-red']])
            for url, source in evidence.items():
                self.assertEqual(read_json(root / source['source_mapping']['file'])['url'], url)
            self.assertEqual(len(calls), 7)

    def test_run_products_persists_identity_proof_and_scans_to_third_parent(self):
        cards = ['https://store.example/' + name for name in ['a', 'a-red', 'b', 'c', 'later']]
        identities = dict(zip(cards, ['a', 'a', 'b', 'c', 'later']))
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_json(root / 'detail_url.json', {'cat': cards})
            save_json(root / 'listings.json', {'cat': {'format': 'magento_storefront', 'urls': cards}})
            save_json(root / 'config.json', {'base_url': 'https://store.example/'})
            save_json(root / 'detection.json', {'custom_platform': 'magento'})
            save_json(root / 'sample_scope.json', {})
            with patch('core.custom_site.Client'), patch('core.shopify_fastpath.export'), patch(
                    'core.custom_site._collectors', return_value=(None, self.product(identities, calls))):
                samples = run_products(root)
            self.assertEqual(calls, cards[:4])
            self.assertEqual([s['product']['id'] for s in samples], ['a', 'b', 'c'])
            listings = read_json(root / 'listings.json')
            self.assertEqual(listings['cat']['urls'], cards)
            self.assertEqual(ordered_product_urls({'cat': cards}, listings), [cards[i] for i in [0, 2, 3, 4]])
            self.assertEqual(read_json(root / 'detail_url.json'), {'cat': cards})

    def test_full_products_collects_each_parent_once_and_retains_alias_proof(self):
        cards = ['https://store.example/' + name for name in ['a', 'a-red', 'b']]
        identities = dict(zip(cards, ['a', 'a', 'b']))
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.custom_site._collectors', return_value=(lambda client, url: {'format': 'magento_storefront', 'urls': cards}, self.product(identities, []))):
            _, detail, listings, samples, _ = run_custom_sample(
                Mock(), Path(directory), 'magento', [('cat', 'https://store.example/cat')], full_products=True)
            self.assertEqual([s['product']['id'] for s in samples], ['a', 'b'])
            self.assertEqual(ordered_product_urls(detail, listings), [cards[0], cards[2]])

    def test_legacy_order_and_unrelated_identity_entry(self):
        detail = {'first': ['a', 'b'], 'second': ['a', 'c']}
        self.assertEqual(ordered_product_urls(detail, {'first': {}, 'second': {}}), ['a', 'b', 'c'])
        with self.assertRaises(CrawlError):
            ordered_product_urls({'cat': ['a']}, {'cat': {'identity_sources': {
                'other': {'url': 'other', 'product': {'id': '1'}, 'source_mapping': {'file': 'evidence/a'}}}}})

    def test_three_distinct_samples_do_not_duplicate_full_objects_in_listing(self):
        cards = ['https://store.example/' + name for name in ['a', 'b', 'c']]
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.custom_site._collectors', return_value=(lambda client, url: {'format': 'magento_storefront', 'urls': cards}, self.product(dict(zip(cards, ['a', 'b', 'c'])), []))):
            _, _, listings, _, _ = run_custom_sample(
                Mock(), Path(directory), 'magento', [('cat', 'https://store.example/cat')])
            self.assertNotIn('identity_sources', listings['cat'])

    def test_legacy_braun_keeps_original_source_paths(self):
        cards = ['https://us.braun.com/' + name for name in ['a', 'b', 'c']]
        fetch = Mock(side_effect=lambda client, url, evidence: {
            'url': url, 'product': {'id': url},
            'source_mapping': {'kind': 'braun_bigcommerce', 'file': 'evidence/original.html'}})
        with tempfile.TemporaryDirectory() as directory, patch(
                'core.custom_site._collectors', return_value=(lambda client, url: {'urls': cards}, fetch)):
            _, _, listings, samples, _ = run_custom_sample(
                Mock(), Path(directory), 'braun_mia', [('cat', 'https://us.braun.com/cat')])
            self.assertNotIn('identity_sources', listings['cat'])
            self.assertEqual(samples[0]['source_mapping']['file'], 'evidence/original.html')
            self.assertEqual(fetch.call_count, 3)

    def test_observed_color_aliases_keep_both_category_memberships(self):
        red = 'https://store.example/bottle?color=red'
        blue = 'https://store.example/bottle?color=blue'
        other = 'https://store.example/other'
        detail = {'Red collection': [red, other], 'Blue collection': [blue]}
        identities = {red: 'bottle', blue: 'bottle', other: 'other'}

        def listing(client, url):
            key = 'Red collection' if url.endswith('/red') else 'Blue collection'
            return {'format': 'magento_storefront', 'urls': detail[key], 'complete': True}

        with tempfile.TemporaryDirectory() as directory, patch(
                'core.custom_site._collectors', return_value=(listing, self.product(identities, []))):
            root = Path(directory)
            _, actual_detail, listings, samples, _ = run_custom_sample(
                Mock(), root, 'magento', [('Red collection', 'https://store.example/red'),
                                        ('Blue collection', 'https://store.example/blue')],
                all_categories=True, full_products=True)
            self.assertEqual(actual_detail, detail)
            self.assertEqual([s['product']['id'] for s in samples], ['bottle', 'other'])
            self.assertEqual(product_categories(samples[0], actual_detail, listings),
                             ['Red collection', 'Blue collection'])
            for key, url in [('Red collection', red), ('Blue collection', blue)]:
                source = listings[key]['identity_sources'][url]
                self.assertEqual(read_json(root / source['source_mapping']['file'])['url'], url)

    def test_forged_and_unproved_alias_urls_do_not_add_categories(self):
        original = 'https://store.example/bottle?color=red'
        forged = 'https://store.example/bottle?color=blue'
        unknown = 'https://store.example/bottle?color=green'
        sample = {'url': original, 'product': {'id': 'bottle'},
                  'source_mapping': {'kind': 'magento_storefront', 'file': 'evidence/original.json'}}
        detail = {'Original': [original], 'Forged': [forged], 'Unknown': [unknown]}
        listings = {'Forged': {'format': 'magento_storefront', 'identity_sources': {
            forged: {'url': 'https://store.example/unrelated', 'product': {'id': 'bottle'},
                     'source_mapping': {'kind': 'magento_storefront', 'file': 'evidence/unrelated.json'}}}}}
        self.assertEqual(product_categories(sample, detail, listings), ['Original'])
        self.assertEqual(product_categories(sample, detail, {}), ['Original'])


if __name__ == '__main__':
    unittest.main()
