import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.failure_telemetry import build_failure_report, submit_failure


class FailureTelemetryTests(unittest.TestCase):
    def test_report_is_small_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); run = root / 'runs' / 'abc'; run.mkdir(parents=True)
            (run / 'failure.json').write_text(json.dumps({
                'status': 'failed', 'stage': 'products', 'exception_type': 'RuntimeError',
                'error': 'password=hunter2 Cookie: secret', 'traceback': 'token=abc123',
            }), encoding='utf-8')
            (run / 'progress.json').write_text(json.dumps({'events': []}), encoding='utf-8')
            report = build_failure_report(root, run, {'id':'abc','site':'https://shop.test/path?x=1'}, '1.0')
            text = json.dumps(report)
            self.assertEqual(report['site'], 'shop.test')
            self.assertNotIn('hunter2', text)
            self.assertNotIn('abc123', text)
            self.assertLess(len(text), 128 * 1024)

    def test_failed_upload_stays_queued(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); run = root / 'runs' / 'abc'; run.mkdir(parents=True)
            (root / 'telemetry_config.json').write_text(json.dumps({
                'enabled': True, 'endpoint': 'https://example.invalid/v1/failures',
                'token': 'secret', 'ca_pem': '-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----'}), encoding='utf-8')
            (run / 'failure.json').write_text('{"status":"failed","error":"boom"}', encoding='utf-8')
            with patch('core.failure_telemetry._send', side_effect=OSError('offline')):
                result = submit_failure(root, run, {'id':'abc','site':'shop.test'}, '1.0')
            self.assertEqual(result['queued'], 1)


if __name__ == '__main__':
    unittest.main()
