import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.common import CrawlError, save_json
from core.menu_tree import build_tree, tree_data, verify_sources
from core.navigation_release import publish, require_release
from core.shopify_fastpath import run_links, run_products


BASE = 'https://example.com/'
HOME = '<header><nav aria-label="Main menu"><ul><li><a href="/collections/all">Shop</a></li></ul></nav></header>'


class Client:
    def get(self, url):
        return type('Response', (), {'url': url, 'text': '<main id="product-grid"><a href="/products/one">One</a></main>'})()

    def json(self, url):
        return {'products': [{'id': 1, 'handle': 'one'}]}


class ReleaseTests(unittest.TestCase):
    def test_publish_checks_all_targets_concurrently_in_source_order(self):
        import threading
        import time
        from core.common import read_json
        html = '<header><nav aria-label="Main menu"><ul>' + ''.join(
            f'<li><a href="/collections/c{i}">C{i}</a></li>' for i in range(8)) + '</ul></nav></header>'
        active = peak = 0
        lock = threading.Lock()
        def target(client, url, directory, index):
            nonlocal active, peak
            self.assertEqual(client.min_interval, 0.2)
            with lock:
                active += 1; peak = max(peak, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return {'url': url, 'passed': True, 'files': []}
        client = Client(); client.min_interval = 0.7
        with tempfile.TemporaryDirectory() as d, patch('core.navigation_release._target', side_effect=target):
            root = Path(d); (root/'evidence').mkdir()
            (root/'evidence/home.html').write_text(html, encoding='utf-8')
            proof = build_tree(html, BASE); data = tree_data(proof)
            publish(root, data, proof, [html], BASE, client)
            self.assertEqual([t['url'] for t in read_json(root/'navigation_targets.json')], list(data.values()))
            self.assertEqual(peak, 4)
            self.assertEqual(client.min_interval, 0.7)

    def test_tagged_target_uses_collection_feed_and_verifies_filtered_listing(self):
        from core.navigation_release import _target
        client = Client()
        seen = []
        client.json = lambda url: seen.append(url) or {'products': [{'id': 1, 'handle': 'one'}]}
        url = BASE + 'collections/all/tag'
        with tempfile.TemporaryDirectory() as d, patch('core.shopify_fastpath.collect_listing', return_value={'complete': True}) as listing:
            result = _target(client, url, Path(d), 0)
        self.assertTrue(result['passed'], result)
        self.assertIn('/collections/all/products.json?', seen[0])
        listing.assert_called_once_with(client, url)

    def test_collection_redirect_publishes_canonical_and_binds_source(self):
        from types import SimpleNamespace
        from core.common import read_json
        html = HOME.replace('/collections/all', '/collections/all/tag')
        client = Client()
        client.get = lambda url: SimpleNamespace(url=BASE + 'collections/all', text='<main><a href="/products/one">One</a></main>')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'evidence').mkdir()
            (root/'evidence/home.html').write_text(html, encoding='utf-8')
            proof = build_tree(html, BASE); data = tree_data(proof)
            publish(root, data, proof, [html], BASE, client)
            require_release(root)
            self.assertEqual(data['Shop'], BASE + 'collections/all')
            self.assertEqual(read_json(root/'navigation_source_categories.json')['Shop'], BASE + 'collections/all/tag')
            target = read_json(root/'navigation_targets.json')[0]
            self.assertEqual(target['redirected_to'], data['Shop'])

    def test_redirect_outside_collection_or_host_stays_blocked(self):
        from core.navigation_release import _target
        from types import SimpleNamespace
        for url in (BASE + 'pages/story', 'https://other.example/collections/all'):
            client = Client(); client.get = lambda _: SimpleNamespace(url=url, text='')
            with tempfile.TemporaryDirectory() as d:
                self.assertFalse(_target(client, BASE + 'collections/all', Path(d), 0)['passed'])

    def test_curated_landing_requires_explicit_cards_and_real_identity(self):
        from core.shopify_fastpath import resolve_landing_membership
        html = '<main><core-react-product-card data-handle="one"><a href="/products/one">One</a></core-react-product-card></main>'
        client = Client()
        client.json = lambda url: {'product': {'id': 1, 'handle': 'one', 'variants': [{'id': 11}]}}
        products, feeds, cards = resolve_landing_membership(client, html, BASE + 'collections/landing')
        self.assertEqual([p['handle'] for p in products], ['one'])
        self.assertEqual(feeds, [])
        self.assertEqual(cards[0]['endpoint'], BASE + 'products/one.json')
        with self.assertRaises(CrawlError):
            resolve_landing_membership(client, '<main><a href="/products/one">One</a></main>', BASE + 'collections/landing')
        with self.assertRaises(CrawlError):
            resolve_landing_membership(client, html.replace('data-handle="one"', 'data-handle="wrong"'), BASE + 'collections/landing')
        client.json = lambda url: {'product': {'id': 2, 'handle': 'other', 'variants': [{'id': 22}]}}
        with self.assertRaises(CrawlError):
            resolve_landing_membership(client, html, BASE + 'collections/landing')

    def test_json_identity_accepts_percent_encoding_but_not_another_product(self):
        from core.common import Client as HttpClient
        from types import SimpleNamespace
        client = HttpClient()
        response = SimpleNamespace(url=BASE + 'products/name%E2%84%A2.json', json=lambda: {'product': {'id': 1}})
        with patch.object(client, 'get', return_value=response):
            self.assertEqual(client.json(BASE + 'products/name™.json')['product']['id'], 1)
            with self.assertRaises(CrawlError):
                client.json(BASE + 'products/other.json')

    def test_full_validation_and_unused_default_title_slot(self):
        from test_core import fixture
        from core.shopify_fastpath import product_rows
        from core.validator import validate_samples
        samples, detail, listings, rows = fixture()
        for sample in samples:
            sample['product']['variants'][0]['option3'] = 'Default Title'
        rows = [row for sample in samples for row in product_rows(sample, ['Shop'])]
        self.assertEqual(validate_samples(detail, samples, listings, rows, full=True)['products'], 3)
        samples[0]['product']['variants'][0]['option3'] = 'Unspecified real option'
        with self.assertRaises(CrawlError):
            validate_samples(detail, samples, listings, rows, full=True)

    def test_confirmed_correction_requires_independent_reconstruction(self):
        from core.navigation_ai import validate_review_scope
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); data, proof = self.prepare(root)
            reward_key = 'Shop,Shop Rewards'
            save_json(root / 'navigation_review_scope.json', {'base_url': BASE, 'program_complete': True,
                'confirmed_candidate': {**data, reward_key: BASE + 'apps/rewards'},
                'confirmed_roots': ['Shop'], 'uncertain_candidate': {}, 'uncertain_roots': [], 'issues': []})
            save_json(root / 'navigation_decision.json', {'version': 2, 'complete': True, 'confirmed_unchanged': False,
                'resolved_issue_ids': [], 'unresolved': [], 'reviewed_roots': ['Shop']})
            proof['review'] = {'scope_version': 2, 'resolved_issue_ids': [], 'confirmed_unchanged': False,
                'reopened_confirmed': [{'key': reward_key, 'reason': 'Rewards are not collections', 'source_files': ['evidence/home.html']}]}
            self.assertTrue(validate_review_scope(root, data, proof)['complete'])
            with self.assertRaises(CrawlError):
                validate_review_scope(root, {}, proof)

    def test_landing_requires_all_visible_products_in_explicit_child_collections(self):
        from core.shopify_fastpath import landing_membership
        html = '<main><a href="/collections/child">Shop Child</a><a href="/products/one">One</a></main>'
        products, sources = landing_membership(Client(), html, BASE + 'collections/landing')
        self.assertEqual([p['handle'] for p in products], ['one'])
        self.assertEqual(sources[0]['url'], BASE + 'collections/child')
        with self.assertRaises(CrawlError):
            landing_membership(Client(), html.replace('/products/one', '/products/unverified'), BASE + 'collections/landing')
        with self.assertRaises(CrawlError):
            landing_membership(Client(), html.replace('/collections/child', '/pages/story'), BASE + 'collections/landing')

    def prepare(self, root):
        (root / 'evidence').mkdir()
        (root / 'evidence/home.html').write_text(HOME, encoding='utf-8')
        proof = build_tree(HOME, BASE)
        return tree_data(proof), proof

    def test_release_binds_json_and_source_evidence(self):
        for name in ('url.json', 'evidence/home.html', 'evidence/category-targets/target-000.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root = Path(d); data, proof = self.prepare(root)
                publish(root, data, proof, [HOME], BASE, Client())
                self.assertEqual(require_release(root)['status'], 'approved')
                with (root / name).open('a', encoding='utf-8') as f:
                    f.write(' ')
                with self.assertRaises(CrawlError):
                    require_release(root)

    def test_failed_target_never_publishes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); data, proof = self.prepare(root)
            client = Client(); client.json = lambda url: {'error': 'not a collection'}
            with self.assertRaises(CrawlError):
                publish(root, data, proof, [HOME], BASE, client)
            self.assertFalse((root / 'url.json').exists())
            with self.assertRaises(CrawlError):
                require_release(root)

    def test_standalone_collectors_refuse_unapproved_navigation_before_network(self):
        with tempfile.TemporaryDirectory() as d, patch('core.common.Client') as client:
            save_json(Path(d) / 'url.json', {'Shop': BASE + 'collections/all'})
            for collect in (run_links, run_products):
                with self.assertRaises(CrawlError):
                    collect(d)
            client.assert_not_called()

    def test_feature_title_and_rewards_filter(self):
        html = '<header><nav aria-label="Main navigation"><ul><li><div><button>Shop</button></div><div role="menu">'
        html += '<a href="/collections/am-pm"><h3><div><p><strong>AM &amp; PM</strong><br>Move forward, day to night.</p></div></h3></a>'
        html += '<a href="/apps/bubblehouse/rewards">Refer-a-Friend</a></div></li></ul></nav></header>'
        proof = build_tree(html, BASE)
        self.assertEqual(tree_data(proof), {'Shop,Shop AM & PM': BASE + 'collections/am-pm'})
        verify_sources(proof, html, BASE)

    def test_shared_drawer_has_explicit_primary_and_panel_ownership(self):
        html = '<header><button data-drawer-name="menu">Menu</button></header><div data-drawer-names="menu">'
        html += '<ul><li><button aria-controls="pets">Pets</button></li></ul><div id="pets">'
        html += '<a href="/collections/pet-beds">Beds</a><a href="/collections/pets">Shop All</a></div></div>'
        proof = build_tree(html, BASE)
        self.assertTrue(proof['complete'])
        self.assertEqual(tree_data(proof), {'Pets': BASE + 'collections/pets', 'Pets,Pets Beds': BASE + 'collections/pet-beds'})
        verify_sources(proof, html, BASE)
        self.assertIsNone(build_tree(html.replace('data-drawer-name="menu"', ''), BASE))
