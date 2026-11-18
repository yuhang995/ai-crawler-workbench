import unittest
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.common import (AccessChallenge, Client, is_access_challenge_response,
                         manual_confirmation_ready, retry_after_seconds)
import app


BASE = 'https://example.com/'


def response(status=200, text='<html><nav>Shop</nav></html>', headers=None):
    item = MagicMock()
    item.status_code = status
    item.text = text
    item.content = text.encode()
    item.headers = headers or {'content-type': 'text/html'}
    item.url = BASE
    if status >= 400:
        from curl_cffi.requests import RequestsError
        item.raise_for_status.side_effect = RequestsError('HTTP Error ' + str(status))
    else:
        item.raise_for_status.return_value = None
    return item


class AccessRecoveryTests(unittest.TestCase):
    def test_retry_after_delta_and_http_date_are_bounded(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds('12', now), 12)
        self.assertEqual(retry_after_seconds('Thu, 01 Jan 2026 00:00:30 GMT', now), 30)
        self.assertEqual(retry_after_seconds('Thu, 01 Jan 2026 00:05:00 GMT', now), 45)
        self.assertIsNone(retry_after_seconds('not-a-date', now))

    def test_header_and_short_html_challenges_are_detected(self):
        header = response(headers={'cf-mitigated': 'challenge', 'content-type': 'text/html'})
        body = response(text='<html><title>Just a moment...</title></html>')
        normal = response(text='<html><title>Store</title><nav>Shop</nav></html>')
        self.assertTrue(is_access_challenge_response(header))
        self.assertTrue(is_access_challenge_response(body))
        self.assertFalse(is_access_challenge_response(normal))

    def test_complete_storefront_with_dormant_challenge_scripts_is_not_blocked(self):
        normal = response(text='''<html><title>Vionic Shoes</title>
            <script src="/challenge-platform/script.js"></script>
            <script>window.captcha = {turnstile: true}</script>
            <nav>Women Men Shoes</nav><main>Complete public storefront</main></html>''')
        self.assertFalse(is_access_challenge_response(normal))

    def test_large_storefront_with_embedded_turnstile_widget_is_not_challenge(self):
        normal = response(text='''<html><title>Large Store</title>
            <main>''' + ('Visible catalog product navigation ' * 900) + '''</main>
            <form id="newsletter"><div class="cf-turnstile"></div></form></html>''')
        self.assertGreater(len('Visible catalog product navigation ' * 900), 20_000)
        self.assertFalse(is_access_challenge_response(normal))

    def test_empty_and_loading_html_cannot_finish_manual_confirmation(self):
        empty = response(text='')
        shell = response(text='<html><head><title></title></head><body></body></html>')
        loading = response(text='<html><title>Loading</title><body>Please wait...</body></html>')
        complete = response(text='<html><title>Store</title><body><nav>Shop categories</nav><main>'
                            + ('Real catalog content and products. ' * 12)
                            + '</main></body></html>')
        self.assertFalse(manual_confirmation_ready(empty))
        self.assertFalse(manual_confirmation_ready(shell))
        self.assertFalse(manual_confirmation_ready(loading))
        self.assertTrue(manual_confirmation_ready(complete))

    def test_access_confirmation_automatically_requeues_same_job(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'RUNS', Path(directory)), patch.object(
                app, 'execute') as execute:
            root = Path(directory) / 'job'
            root.mkdir()
            (root / 'failure.json').write_text('{"status":"needs_access_confirm"}', encoding='utf-8')
            job = {'id': 'job', 'site': BASE, 'status': 'access_confirmed',
                   'events': [], 'confirm': threading.Event(), 'proxy': None,
                   'uploaded_categories': None}
            self.assertTrue(app.resume_after_access_confirmation(job))
            deadline = time.time() + 2
            while not execute.called and time.time() < deadline:
                time.sleep(0.01)
            execute.assert_called_once_with(job)
            self.assertEqual(job['status'], 'queued')
            self.assertTrue((root / 'previous-access-failure.json').is_file())

    def test_challenge_is_never_cached_and_requires_human(self):
        client = Client()
        session = MagicMock()
        session.get.return_value = response(text='<html><title>Verify you are human</title></html>')
        client.local.session = session
        with patch('core.common.time.sleep'), patch.object(
                client, 'warm_browser_session', side_effect=AccessChallenge('人工验证')):
            with self.assertRaises(AccessChallenge):
                client.get(BASE, attempts=1)
        self.assertEqual(client.cache, {})

    def test_transient_challenge_is_rechecked_before_browser_recovery(self):
        client = Client()
        session = MagicMock()
        challenged = response(text='<html><title>Verify you are human</title></html>')
        allowed = response(text='<html><title>Store</title><nav>Shop products</nav></html>')
        session.get.side_effect = [challenged, allowed]
        client.local.session = session
        with patch('core.common.time.sleep') as pause, patch.object(
                client, 'pace'), patch.object(client, 'warm_browser_session') as warm:
            actual = client.get(BASE, attempts=1)
        self.assertIs(actual, allowed)
        self.assertEqual(pause.call_args_list[0].args, (2.0,))
        warm.assert_not_called()
        self.assertEqual(session.get.call_count, 2)

    def test_generic_403_warms_once_then_replays_same_request(self):
        client = Client()
        session = MagicMock()
        denied = response(status=403, text='<html><title>Forbidden</title></html>')
        allowed = response()
        session.get.side_effect = [denied, allowed]
        client.local.session = session
        with patch.object(client, 'warm_browser_session') as warm:
            actual = client.get(BASE, attempts=1)
        self.assertIs(actual, allowed)
        warm.assert_called_once_with(BASE)
        self.assertEqual(session.get.call_count, 2)

    def test_new_transport_prefers_firefox(self):
        session = MagicMock()
        with patch('core.common.requests.Session', return_value=session) as factory:
            actual = Client()._new_session('example.com')
        self.assertIs(actual, session)
        factory.assert_called_once_with(impersonate='firefox')


if __name__ == '__main__':
    unittest.main()
