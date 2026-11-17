import tempfile
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from core.common import Client, CrawlError, read_json, save_json
from core.menu_tree import build_tree, tree_data, label
from core.nav_extractor import extract_navigation, _is_generic_menu_label
from core.navigation_ai import load_navigation, prepare_check_script

BASE = 'https://example.com/'
HTML = '<header><nav><ul><li><a href="/collections/all">Shop</a><div><h3>Power</h3><a href="/collections/laptop">Laptop Power<span class="tag">New</span></a></div></li></ul></nav></header>'
CANDIDATE = {'Shop': BASE+'collections/all', 'Shop,Shop Power,Shop Power Laptop Power': BASE+'collections/laptop'}


def seed(root, snapshot=True):
    (root/'evidence').mkdir()
    (root/'evidence/home.html').write_text(HTML, encoding='utf-8')
    if snapshot:
        (root/'evidence/home-menu-001.html').write_text(HTML, encoding='utf-8')
    (root/'review.md').write_text('Verified saved menu.', encoding='utf-8')
    save_json(root/'category_candidate.json', CANDIDATE)
    save_json(root/'navigation_invocation.json', {'workflow_version': 2})
    save_json(root/'navigation_review_scope.json', {'base_url': BASE, 'program_complete': False,
        'confirmed_candidate': {'Shop': CANDIDATE['Shop']}, 'confirmed_roots': ['Shop'],
        'uncertain_roots': ['Shop'], 'issues': [{'id': 'U001', 'roots': ['Shop']}],
        'dynamic_evidence_required': True, 'dynamic_issue_ids': ['U001']})
    save_json(root/'navigation_decision.json', {'version': 2, 'complete': True, 'confirmed_unchanged': True,
        'resolved_issue_ids': ['U001'], 'unresolved': [], 'reviewed_roots': ['Shop'], 'summary': 'Verified Power group in snapshot.'})


class EnhancementTests(unittest.TestCase):
    def test_cta_badges_product_cards_and_real_names(self):
        from bs4 import BeautifulSoup
        for text in ('View All ›', 'Shop Now >', 'Explore →', 'Discover!', 'Learn More »'):
            self.assertTrue(_is_generic_menu_label(text))
        html = HTML.replace('</div>', '<a href="/collections/power">View All ›</a><a href="/products/featured">Featured</a></div>')
        for data in (tree_data(build_tree(html, BASE)), extract_navigation(html, BASE).data):
            self.assertIn('Shop,Shop Power,Shop Power Laptop Power', data)
            self.assertIn('Shop,Shop Power', data)
            self.assertFalse(any('New' in k or 'View All' in k or '/products/' in v for k, v in data.items()))
        for badge in ('badge', 'tag', 'pill', 'rebate', 'label-new'):
            node = BeautifulSoup(f'<a>Laptop Power<span class="{badge}">Offer</span><span>Free Engraving</span><span>4.9</span></a>', 'lxml').a
            self.assertEqual(label(node), 'Laptop Power')
        for name in ('New', 'Sale', 'New Releases', 'Laptop Power'):
            self.assertEqual(label(BeautifulSoup(f'<a><span>{name}</span></a>', 'lxml').a), name)

    def test_autofill_only_map_and_decision_without_ai_nested_proof(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); seed(root)
            with patch('core.common.Client.get', side_effect=AssertionError('offline check used network')):
                candidate, proof, _ = load_navigation(root)
            self.assertEqual(candidate, CANDIDATE)
            self.assertTrue(proof['structure_completed_by_python'])
            self.assertIn('evidence/home-menu-001.html', proof['review']['branches'][0]['source_files'])
            self.assertTrue((root/'evidence/navigation-observed.json').exists())

    def test_autofill_rejects_forgery_ownership_confirmed_changes_and_no_dynamic_snapshot(self):
        bad = [dict(CANDIDATE, Shop=BASE+'collections/forged'),
               {'Wrong,Wrong Laptop Power': BASE+'collections/laptop'},
               {'Shop': BASE+'collections/all', 'Shop,Shop Featured': BASE+'products/featured'},
               {'Shop': BASE+'collections/all', 'Shop,Wrong Power': BASE+'collections/laptop'},
               {'Shop,Shop Power,Shop Power Laptop Power': BASE+'collections/laptop'}]
        for candidate in bad:
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as d:
                root=Path(d); seed(root); save_json(root/'category_candidate.json', candidate)
                with self.assertRaises(CrawlError): load_navigation(root)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); seed(root, snapshot=False)
            with self.assertRaises(CrawlError): load_navigation(root)

    def test_check_script_failure_fix_and_ok_same_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); seed(root); prepare_check_script(root)
            save_json(root/'category_candidate.json', {'Shop': BASE+'collections/fake'})
            command = [sys.executable, '-X', 'utf8', 'check_nav.py']
            bad=subprocess.run(command,cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=20)
            self.assertNotEqual(bad.returncode,0)
            self.assertIn('ERROR:',bad.stdout)
            save_json(root/'category_candidate.json', CANDIDATE)
            good=subprocess.run(command,cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=20)
            self.assertEqual(good.returncode,0,good.stdout+good.stderr)
            self.assertEqual(good.stdout.strip(),'OK')
            self.assertTrue(read_json(root/'navigation_selfcheck.json')['passed'])
            from core.navigation_ai import require_selfcheck
            require_selfcheck(root)
            import json
            decision = read_json(root/'navigation_decision.json')
            (root/'navigation_decision.json').write_text(json.dumps(decision,separators=(',',':')), encoding='utf-8')
            require_selfcheck(root)
            save_json(root/'navigation_decision.json', {'complete': False})
            with self.assertRaises(CrawlError): require_selfcheck(root)

    def test_url_json_alias_and_explicit_negative_review_not_overridden(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); seed(root)
            (root/'category_candidate.json').rename(root/'url.json')
            self.assertEqual(load_navigation(root)[0],CANDIDATE)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); seed(root)
            save_json(root/'evidence/navigation-observed.json', {'review': {'complete': False}})
            with self.assertRaises(CrawlError): load_navigation(root)

    def test_429_cooldown_is_shared_and_pacing_does_not_hold_lock(self):
        client=Client(); client.parallel_pacing=True
        with patch('core.common.time.monotonic', return_value=100): client.backoff(3)
        self.assertEqual(client.retry_not_before,103)
        def sleep(seconds):
            self.assertEqual(seconds,3)
            self.assertTrue(client.rate_lock.acquire(blocking=False)); client.rate_lock.release()
        with patch('core.common.time.monotonic', side_effect=[100,103]), patch('core.common.time.sleep', side_effect=sleep):
            client.pace()
        self.assertAlmostEqual(client.local.next_request_at,103.15)

    def test_real_get_429_schedules_shared_backoff_then_retries(self):
        client=Client(); client.local.session=Mock()
        response=Mock(status_code=200,content=b'{}')
        limited=Mock(status_code=429,headers={'Retry-After':'2'})
        client.local.session.get.side_effect=[limited,response]
        with patch('core.common.public_url'),patch.object(client,'pace'),patch.object(client,'backoff') as backoff:
            self.assertIs(client.get(BASE),response)
        backoff.assert_called_once_with(2)
