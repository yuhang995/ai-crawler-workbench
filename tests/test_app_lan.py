import http.client
import importlib.util
import inspect
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# Load the web boundary without importing crawler transports; these tests exercise app.py only.
_root = Path(__file__).resolve().parents[1]
_common = types.ModuleType('core.common')


class _CrawlError(RuntimeError):
    pass


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


_common.CrawlError = _CrawlError
_common.read_json = _read_json
_common.save_json = _save_json
_pipeline = types.ModuleType('core.pipeline')
_pipeline.run_job = lambda *args, **kwargs: None
_spec = importlib.util.spec_from_file_location('_app_lan_under_test', _root / 'app.py')
app = importlib.util.module_from_spec(_spec)
with patch.dict(sys.modules, {'core.common': _common, 'core.pipeline': _pipeline}):
    _spec.loader.exec_module(app)


class AuthorityValidationTests(unittest.TestCase):
    def test_loopback_private_ipv4_and_ipv6_are_allowed(self):
        accepted = (
            '127.0.0.1:8773',
            'localhost:8773',
            '10.0.0.8:8773',
            '172.16.20.4:8773',
            '192.168.1.25:8773',
            '[::1]:8773',
            '[fd12:3456::8]:8773',
        )
        for authority in accepted:
            with self.subTest(authority=authority):
                self.assertTrue(app._is_private_authority(authority, 8773))

    def test_public_invalid_and_wrong_port_hosts_are_rejected(self):
        rejected = (
            '',
            'localhost',
            'localhost:8000',
            'example.com:8773',
            '8.8.8.8:8773',
            '[2001:4860:4860::8888]:8773',
            '0.0.0.0:8773',
            'user@localhost:8773',
            'localhost:8773/path',
            '[::1',
        )
        for authority in rejected:
            with self.subTest(authority=authority):
                self.assertFalse(app._is_private_authority(authority, 8773))

    def test_origin_must_match_the_exact_private_host(self):
        self.assertTrue(app._is_same_private_origin(
            '192.168.1.25:8773', 'http://192.168.1.25:8773', 8773))
        self.assertTrue(app._is_same_private_origin(
            '[::1]:8773', 'http://[::1]:8773', 8773))
        self.assertTrue(app._is_same_private_origin('localhost:8773', None, 8773))
        self.assertFalse(app._is_same_private_origin(
            '192.168.1.25:8773', 'http://192.168.1.26:8773', 8773))
        self.assertFalse(app._is_same_private_origin(
            'localhost:8773', 'https://localhost:8773', 8773))
        self.assertFalse(app._is_same_private_origin(
            'localhost:8773', 'http://8.8.8.8:8773', 8773))


class WorkbenchLanTests(unittest.TestCase):
    def test_lan_detection_prefers_a_host_address_over_virtual_network_zero(self):
        class Probe:
            def connect(self, _target):
                pass

            def getsockname(self):
                return ('172.19.0.0', 43210)

            def close(self):
                pass

        addresses = [
            (app.socket.AF_INET, app.socket.SOCK_DGRAM, 0, '', ('172.19.0.0', 0)),
            (app.socket.AF_INET, app.socket.SOCK_DGRAM, 0, '', ('192.168.10.19', 0)),
        ]
        with patch.object(app.socket, 'socket', return_value=Probe()), \
                patch.object(app.socket, 'getaddrinfo', return_value=addresses):
            self.assertEqual(app.detect_lan_ip(), '192.168.10.19')

    def test_defaults_workers_and_submitter_normalisation(self):
        self.assertEqual(inspect.signature(app.serve).parameters['host'].default, '0.0.0.0')
        self.assertEqual(app.WORKERS._value, 4)
        self.assertEqual(app._clean_submitter('  小李\n 电商组  '), '小李 电商组')
        with self.assertRaises(ValueError):
            app._clean_submitter('x' * 81)
        with self.assertRaises(TypeError):
            app._clean_submitter(123)

    def test_private_lan_post_persists_submitter_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'RUNS', Path(directory)):
            with app.LOCK:
                app.JOBS.clear()
            server = app.LocalServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            payload = json.dumps({'site': 'example.com', 'submitter': '  小李  ', 'visual_check': True})
            try:
                with patch.object(app, 'execute') as execute:
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                    connection.request('POST', '/api/jobs', payload, {
                        'Host': f'192.168.1.25:{port}',
                        'Origin': f'http://192.168.1.25:{port}',
                        'Content-Type': 'application/json',
                    })
                    response = connection.getresponse()
                    data = json.loads(response.read())
                    connection.close()
                    self.assertEqual(response.status, 201)
                    self.assertEqual(data['submitter'], '小李')
                    execute.assert_called_once()
                saved = json.loads((Path(directory) / data['id'] / 'progress.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['submitter'], '小李')
                self.assertTrue(saved['visual_check'])
                self.assertNotIn('confirm', saved)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
                with app.LOCK:
                    app.JOBS.clear()

    def test_frontend_stores_and_displays_submitter(self):
        root = Path(app.__file__).resolve().parent
        html = (root / 'web' / 'index.html').read_text(encoding='utf-8')
        script = (root / 'web' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('name="submitter"', html)
        self.assertIn("localStorage.getItem('ai-crawler.submitter')", script)
        self.assertIn('submitter:form.submitter.value', script)
        self.assertIn('提交人：${esc(j.submitter)}', script)


if __name__ == '__main__':
    unittest.main()
