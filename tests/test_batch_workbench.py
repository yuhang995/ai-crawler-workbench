import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class BatchWorkbenchTests(unittest.TestCase):
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

    def test_batch_submission_creates_independent_jobs_and_keeps_single_contract(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'RUNS', Path(directory)), \
                patch.object(app, 'execute'):
            with app.LOCK:
                app.JOBS.clear()
            server = app.LocalServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, batch = self.post(server.server_port, '/api/jobs', {
                    'site': 'https://one.example\nhttps://two.example'})
                self.assertEqual(status, 201)
                self.assertEqual(batch['count'], 2)
                self.assertEqual({item['site'] for item in batch['jobs']},
                                 {'https://one.example', 'https://two.example'})
                self.assertTrue(all(item['progress'] == 2 for item in batch['jobs']))
                status, single = self.post(server.server_port, '/api/jobs', {
                    'site': 'https://three.example'})
                self.assertEqual(status, 201)
                self.assertIn('id', single)
                self.assertNotIn('jobs', single)
            finally:
                server.shutdown(); server.server_close(); thread.join(5)
                with app.LOCK:
                    app.JOBS.clear()

    def test_progress_is_monotonic_and_frontend_contains_progress_and_update_controls(self):
        job = {'id': 'x', 'site': 'https://x.example', 'status': 'products',
               'progress': 48, 'events': [], 'confirm': threading.Event()}
        self.assertEqual(app.snapshot(job)['progress'], 72)
        job.update(status='failed', progress=72)
        self.assertEqual(app.snapshot(job)['progress'], 72)
        root = Path(app.__file__).resolve().parent
        html = (root / 'web/index.html').read_text(encoding='utf-8')
        script = (root / 'web/app.js').read_text(encoding='utf-8')
        self.assertIn('id="overall-progress"', html)
        self.assertIn('id="check-update"', html)
        self.assertIn('textarea name="site"', html)
        self.assertIn('/api/update/check', script)
        self.assertIn('job-progress', script)

    def test_batch_limits_and_category_file_are_defensive(self):
        with self.assertRaises(ValueError):
            app._sites('\n'.join(f'https://{i}.example' for i in range(21)))
        self.assertEqual(app._sites('https://a.example, https://a.example\nhttps://b.example'),
                         ['https://a.example', 'https://b.example'])


if __name__ == '__main__':
    unittest.main()
