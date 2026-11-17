import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from core.common import CrawlError, read_json, save_json
from core.custom_site import run_navigation
from core.pipeline import run_job
from core.simple_nav import clean_reviewed_categories


BASE = 'https://store.example/en-us'
CATALOG = {'Shavers': BASE + '/shaving', 'Accessories': BASE + '/replacement-parts'}
SELECTION = {'Parts': CATALOG['Accessories']}


class CustomNavigationReviewTests(unittest.TestCase):
    def test_confirm_download_and_retry_native_directory(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'RUNS', Path(folder)), \
                patch.object(app, 'JOBS', {}), patch('core.braun_navigation.mia_navigation', return_value=CATALOG), \
                patch.object(app, 'execute'):
            root = Path(folder) / 'job'
            run_navigation(root, BASE, '', 'braun_mia')
            job = {'id': 'job', 'site': BASE, 'status': 'awaiting_confirmation',
                   'confirm': threading.Event()}
            app.JOBS['job'] = job
            server = app.LocalServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(method, path, body=None):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    conn.request(method, path, json.dumps(body) if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
                    res = conn.getresponse()
                    return res.status, json.loads(res.read())
                finally:
                    conn.close()

            try:
                self.assertEqual(request('GET', '/api/download/job/url.json'), (200, CATALOG))
                status, _ = request('POST', '/api/jobs/job/confirm', {'categories': {
                    **SELECTION, 'Forged': BASE + '/shaving/product-123'}})
                self.assertEqual(status, 409)
                self.assertFalse(job['confirm'].is_set())
                self.assertEqual(request('POST', '/api/jobs/job/confirm', {'categories': SELECTION})[0], 200)
                self.assertEqual(read_json(root / 'confirmed_categories.json'), SELECTION)
                job['status'] = 'failed'
                self.assertEqual(request('POST', '/api/jobs/job/retry-artifacts', {})[0], 202)
                self.assertTrue(job['reuse_navigation'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_worker_preserves_edited_native_scope_and_method_on_legacy_resume(self):
        response = SimpleNamespace(url=BASE, text='fixture')
        client = SimpleNamespace(get=lambda *args, **kwargs: response)
        with tempfile.TemporaryDirectory() as folder, patch('core.pipeline.Client', return_value=client), \
                patch('core.detector.resolve_storefront', return_value=response), \
                patch('core.custom_site.identify_platform', return_value='braun_mia'), \
                patch('core.braun_navigation.mia_navigation', return_value=CATALOG), \
                patch('core.pipeline.detect', side_effect=CrawlError('reached product detection')) as detect:
            root = Path(folder)
            confirm = threading.Event()

            def edit(event):
                if event['stage'] == 'awaiting_confirmation':
                    save_json(root / 'confirmed_categories.json', SELECTION)
                    confirm.set()

            with self.assertRaisesRegex(CrawlError, 'reached product detection'):
                run_job(BASE, root, manual=True, confirm=confirm, emit=edit)
            self.assertEqual(detect.call_args.args[-1], SELECTION)
            self.assertEqual(read_json(root / 'navigation_report.json')['method'], 'custom_braun_mia')
            (root / 'evidence/simple_nav_sources.json').unlink()
            with self.assertRaisesRegex(CrawlError, 'reached product detection'):
                run_job(BASE, root, reuse_navigation=True)
            self.assertEqual(read_json(root / 'url.json'), SELECTION)
            self.assertEqual(read_json(root / 'navigation_report.json')['scope'], 'source_menu_and_merchandising_pages')

    def test_shopify_review_still_requires_a_legal_collection_path(self):
        with self.assertRaisesRegex(ValueError, '真实分类链接'):
            clean_reviewed_categories(SELECTION, {'valid_urls': list(CATALOG.values()),
                                                  'base_url': BASE}, 'simple_nav')
