import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from core.simple_nav import validate_uploaded_categories


BASE = 'https://example.com/'


class UploadedCategoryFileTests(unittest.TestCase):
    def test_accepts_prefixed_tree_queries_and_exact_products_hub(self):
        supplied = {
            'Shop': BASE + 'collections/all',
            'Shop,Shop Hair': BASE + 'collections/hair?sort_by=best-selling',
            'Shop,Shop Hair,Shop Hair New': BASE + 'collections/hair/new',
            'Products': BASE + 'products',
        }
        self.assertEqual(validate_uploaded_categories(supplied, BASE), supplied)

    def test_rejects_cross_store_product_and_bad_parent_prefix(self):
        invalid = [
            {'Bad': 'https://evil.example/collections/all'},
            {'Bad': BASE + 'products/a-pdp'},
            {'Shop,Hair': BASE + 'collections/hair'},
        ]
        for supplied in invalid:
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                validate_uploaded_categories(supplied, BASE)

    def test_api_accepts_file_map_without_exposing_contents_in_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'RUNS', Path(directory)):
            with app.LOCK:
                app.JOBS.clear()
            server = app.LocalServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            categories = {'Shop': BASE + 'collections/all'}
            payload = json.dumps({'site': BASE, 'uploaded_categories': categories})
            try:
                with patch.object(app, 'execute') as execute:
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                    connection.request('POST', '/api/jobs', payload, {
                        'Host': f'127.0.0.1:{port}',
                        'Origin': f'http://127.0.0.1:{port}',
                        'Content-Type': 'application/json',
                    })
                    response = connection.getresponse()
                    data = json.loads(response.read())
                    connection.close()
                    self.assertEqual(response.status, 201)
                    self.assertEqual(data['uploaded_category_count'], 1)
                    self.assertNotIn('uploaded_categories', data)
                    execute.assert_called_once()
                    queued = app.JOBS[data['id']]
                    self.assertEqual(queued['uploaded_categories'], categories)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
                with app.LOCK:
                    app.JOBS.clear()

    def test_frontend_has_category_file_mode(self):
        root = Path(app.__file__).resolve().parent
        html = (root / 'web/index.html').read_text(encoding='utf-8')
        script = (root / 'web/app.js').read_text(encoding='utf-8')
        self.assertIn('name="use_category_file"', html)
        self.assertIn('name="category_file"', html)
        self.assertIn('uploaded_categories', script)


if __name__ == '__main__':
    unittest.main()
