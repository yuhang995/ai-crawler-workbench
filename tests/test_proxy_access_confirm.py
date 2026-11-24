import json
import http.client
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app
from core.common import (Client, playwright_proxy, playwright_proxy_context, redact_proxy,
                         redact_sensitive, validate_proxy)


PROXY = 'http://alice:secret@example.net:8080'


class ProxyAndAccessConfirmTests(unittest.TestCase):
    def test_proxy_validation_playwright_mapping_and_redaction(self):
        self.assertEqual(validate_proxy(PROXY), PROXY)
        self.assertEqual(redact_proxy(PROXY), 'http://alice:***@example.net:8080')
        self.assertNotIn('secret', redact_sensitive('failed via ' + PROXY, PROXY))
        self.assertEqual(playwright_proxy(PROXY), {
            'server': 'http://example.net:8080',
            'username': 'alice', 'password': 'secret'})
        for invalid in ('example.net:8080', 'ftp://example.net:21',
                        'http://example.net/path'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_proxy(invalid)

    def test_authenticated_socks_proxy_uses_password_free_local_browser_relay(self):
        secret = 'socks5://alice:secret@127.0.0.1:9999'
        with playwright_proxy_context(secret) as mapped:
            self.assertRegex(mapped['server'], r'^http://127\.0\.0\.1:\d+$')
            self.assertNotIn('username', mapped)
            self.assertNotIn('password', mapped)
            self.assertNotIn('secret', mapped['server'])

    def test_curl_session_receives_the_explicit_proxy(self):
        session = MagicMock()
        with patch('core.common.requests.Session', return_value=session) as factory:
            Client(proxy=PROXY)._new_session('store.example')
        self.assertEqual(factory.call_args.kwargs['impersonate'], 'firefox')
        self.assertEqual(factory.call_args.kwargs['proxy'], PROXY)
        self.assertTrue(factory.call_args.kwargs['verify'].endswith('cacert.pem'))

    def test_domain_browser_state_round_trips_without_proxy_password(self):
        cookies = [{'name': 'clearance', 'value': 'ok', 'domain': '.store.example',
                    'path': '/', 'expires': -1, 'httpOnly': True, 'secure': True,
                    'sameSite': 'None'}]
        with tempfile.TemporaryDirectory() as directory:
            first = Client(proxy=PROXY, state_dir=directory)
            first._store_browser_state('store.example', cookies, 'Browser UA')
            path = first._state_path('store.example')
            raw = path.read_text(encoding='utf-8')
            self.assertNotIn('secret', raw)
            second = Client(proxy=PROXY, state_dir=directory)
            second._load_browser_state('store.example')
            self.assertEqual(second.browser_user_agents['store.example'], 'Browser UA')
            self.assertEqual(second.browser_cookies[0]['name'], 'clearance')

    def test_job_snapshot_never_exposes_or_persists_proxy_password(self):
        job = {'id': 'one', 'site': 'https://store.example', 'proxy': PROXY,
               'status': 'queued', 'events': [], 'confirm': MagicMock()}
        public = app.snapshot(job)
        self.assertNotIn('proxy', public)
        self.assertEqual(public['proxy_display'], 'http://alice:***@example.net:8080')
        self.assertNotIn('secret', json.dumps(public))

    def test_frontend_has_proxy_and_access_confirmation_controls(self):
        root = Path(app.__file__).resolve().parent
        html = (root / 'web/index.html').read_text(encoding='utf-8')
        script = (root / 'web/app.js').read_text(encoding='utf-8')
        self.assertIn('name="proxy" type="password"', html)
        self.assertIn('data-open-access', script)
        self.assertIn('data-continue-access', script)
        self.assertIn('needs_access_confirm', script)

    def test_access_confirmation_endpoint_persists_then_allows_continue(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'RUNS', Path(directory)):
            root = Path(directory) / 'job'
            root.mkdir()
            (root / 'failure.json').write_text(json.dumps({
                'status': 'needs_access_confirm', 'url': 'https://store.example/'}), encoding='utf-8')
            job = {'id': 'job', 'site': 'https://store.example/', 'proxy': PROXY,
                   'manual': False, 'all_categories': False, 'force_ai': False,
                   'full_products': False, 'visual_check': False,
                   'uploaded_categories': None, 'status': 'needs_access_confirm',
                   'events': [], 'confirm': threading.Event()}
            with app.LOCK:
                app.JOBS.clear()
                app.JOBS['job'] = job
            server = app.LocalServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port

            def post(path):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('POST', path, '{}', {
                    'Host': f'127.0.0.1:{port}', 'Origin': f'http://127.0.0.1:{port}',
                    'Content-Type': 'application/json'})
                response = connection.getresponse()
                data = json.loads(response.read())
                connection.close()
                return response.status, data

            browser = MagicMock()
            browser.manual_browser_confirm.return_value = {'confirmed': True}
            try:
                with patch.object(app, 'Client', return_value=browser), patch.object(
                        app, 'execute') as execute:
                    self.assertEqual(post('/api/jobs/job/open-access-confirm')[0], 202)
                    deadline = time.time() + 3
                    while job['status'] != 'queued' and time.time() < deadline:
                        time.sleep(0.02)
                    self.assertEqual(job['status'], 'queued')
                    browser.manual_browser_confirm.assert_called_once()
                    deadline = time.time() + 2
                    while not execute.called and time.time() < deadline:
                        time.sleep(0.02)
                    execute.assert_called_once_with(job)
                self.assertTrue((root / 'previous-access-failure.json').is_file())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
                with app.LOCK:
                    app.JOBS.clear()


if __name__ == '__main__':
    unittest.main()
