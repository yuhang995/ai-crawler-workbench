import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app
from core.common import Client, TaskCancelled, load_proxy_pool


P1 = 'http://alice:secret-one@proxy-one.example:8001'
P2 = 'http://bob:secret-two@proxy-two.example:8002'


class WorkbenchOperationsTests(unittest.TestCase):
    def post(self, port, path, body):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        payload = json.dumps(body)
        connection.request('POST', path, payload, {
            'Host': f'127.0.0.1:{port}', 'Origin': f'http://127.0.0.1:{port}',
            'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_cancel_queued_job_sets_flag_and_public_status(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                app, 'RUNS', Path(directory)), patch.object(app, 'execute'):
            with app.LOCK:
                app.JOBS.clear()
            server = app.LocalServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, created = self.post(server.server_port, '/api/jobs', {
                    'site': 'https://cancel.example'})
                self.assertEqual(status, 201)
                status, cancelled = self.post(
                    server.server_port, f"/api/jobs/{created['id']}/cancel", {})
                self.assertEqual(status, 202)
                self.assertEqual(cancelled['status'], 'cancelled')
                job = app.JOBS[created['id']]
                self.assertTrue(job['cancel_event'].is_set())
                self.assertTrue(job['confirm'].is_set())
                self.assertEqual(json.loads((Path(directory) / created['id'] /
                                             'progress.json').read_text(encoding='utf-8'))['status'],
                                 'cancelled')
            finally:
                server.shutdown(); server.server_close(); thread.join(5)
                with app.LOCK:
                    app.JOBS.clear()

    def test_cleanup_keeps_active_and_recent_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'RUNS', Path(directory)):
            root = Path(directory)
            for index, name in enumerate(('old', 'recent', 'active')):
                path = root / name
                path.mkdir()
                (path / 'progress.json').write_text('{}', encoding='utf-8')
                os.utime(path, (100 + index, 100 + index))
            with app.LOCK:
                app.JOBS.clear()
                app.JOBS['active'] = {'status': 'listing'}
            result = app.cleanup_runs(keep_recent=1)
            self.assertEqual(result['removed'], ['old'])
            self.assertTrue((root / 'recent').is_dir())
            self.assertTrue((root / 'active').is_dir())
            with self.assertRaisesRegex(Exception, '运行中'):
                app.cleanup_runs(0, 'active')
            with app.LOCK:
                app.JOBS.clear()

    def test_proxy_pool_file_order_switch_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'working_proxies.txt'
            path.write_text(f'# ordered\n{P1}\n{P2}\n', encoding='utf-8')
            self.assertEqual(load_proxy_pool(path), [P1, P2])
        Client._proxy_pool_cursor.clear()
        sessions = [MagicMock(), MagicMock()]
        with patch('core.common.requests.Session', side_effect=sessions) as factory:
            client = Client(proxy_pool=[P1, P2])
            client._session('store.example')
            self.assertEqual(client.proxy, P1)
            self.assertTrue(client._advance_pool_proxy('store.example'))
            client._reset_session('store.example')
            self.assertEqual(client.proxy, P2)
        self.assertEqual(factory.call_args_list[0].kwargs['proxy'], P1)
        self.assertEqual(factory.call_args_list[1].kwargs['proxy'], P2)
        public = app.snapshot({'id': 'pool', 'site': 'https://store.example',
                               'status': 'queued', 'events': [], 'proxy': None,
                               'use_proxy_pool': True})
        encoded = json.dumps(public, ensure_ascii=False)
        self.assertNotIn('secret-one', encoded)
        self.assertNotIn('secret-two', encoded)
        self.assertIn('代理池', public['proxy_display'])

    def test_cancel_event_stops_client_before_network(self):
        cancelled = threading.Event(); cancelled.set()
        client = Client(cancel_event=cancelled)
        with patch('core.common.requests.Session') as session, self.assertRaises(TaskCancelled):
            client.get('https://example.com/')
        session.assert_not_called()

    def test_frontend_exposes_operations_and_proxy_pool(self):
        root = Path(app.__file__).resolve().parent
        html = (root / 'web/index.html').read_text(encoding='utf-8')
        script = (root / 'web/app.js').read_text(encoding='utf-8')
        self.assertIn('id="cleanup-jobs"', html)
        self.assertIn('name="use_proxy_pool"', html)
        self.assertIn('data-cancel', script)
        self.assertIn('/api/jobs/cleanup', script)


if __name__ == '__main__':
    unittest.main()
