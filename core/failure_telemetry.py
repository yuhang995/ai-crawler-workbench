"""Privacy-safe, opt-in failure reporting for the workbench.

Only compact diagnostics are sent. Raw pages, cookies, proxy credentials and
product data are deliberately excluded. Failed uploads remain in a local queue
and are retried after a later job.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import core.common as _common

read_json = _common.read_json
save_json = _common.save_json
redact_sensitive = getattr(_common, 'redact_sensitive', lambda value, proxy=None: str(value))

MAX_TEXT = 20_000
MAX_EVENTS = 30
_LOCK = threading.Lock()
_SECRET = re.compile(
    r"(?i)(authorization|cookie|set-cookie|password|passwd|proxy|token|api[_-]?key)"
    r"\s*[:=]\s*([^\s,;]+)"
)


def _safe_text(value, limit=MAX_TEXT):
    text = redact_sensitive(value)
    text = _SECRET.sub(lambda m: f"{m.group(1)}=***", str(text))
    return text[-limit:]


def _safe_site(value):
    try:
        parsed = urlsplit(value if '://' in value else 'https://' + value)
        return parsed.hostname or 'unknown'
    except (TypeError, ValueError):
        return 'unknown'


def _config(root):
    path = Path(root) / 'telemetry_config.json'
    if not path.is_file():
        return {}
    data = read_json(path)
    return data if isinstance(data, dict) and data.get('enabled') is True else {}


def _read_optional(path, default):
    try:
        return read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def build_failure_report(project_root, run_root, job, version):
    """Build the deliberately small diagnostic envelope sent to the server."""
    project_root, run_root = Path(project_root), Path(run_root)
    failure = _read_optional(run_root / 'failure.json', {})
    progress = _read_optional(run_root / 'progress.json', {})
    warnings = _read_optional(run_root / 'warnings.json', [])
    events = progress.get('events', []) if isinstance(progress, dict) else []
    if not isinstance(events, list):
        events = []
    error = _safe_text(failure.get('error') or 'unknown failure', 8_000)
    exception_type = _safe_text(failure.get('exception_type') or '', 200)
    stage = _safe_text(failure.get('stage') or progress.get('status') or 'unknown', 100)
    fingerprint = hashlib.sha256(
        f"{stage}\n{exception_type}\n{error[:2000]}".encode('utf-8', 'replace')
    ).hexdigest()
    install_path = project_root / '.installation_id'
    try:
        installation_id = install_path.read_text(encoding='utf-8').strip()
    except OSError:
        installation_id = ''
    if not installation_id:
        installation_id = uuid.uuid4().hex
        try:
            install_path.write_text(installation_id, encoding='utf-8')
        except OSError:
            installation_id = 'unpersisted'
    warning_items = warnings if isinstance(warnings, list) else warnings.get('warnings', []) if isinstance(warnings, dict) else []
    return {
        'schema_version': 1,
        'reported_at': int(time.time()),
        'installation_id': installation_id,
        'app_version': str(version),
        'run_id': str(job.get('id') or run_root.name),
        'site': _safe_site(str(job.get('site') or failure.get('url') or '')),
        'status': str(failure.get('status') or 'failed')[:50],
        'stage': stage,
        'exception_type': exception_type,
        'error': error,
        'traceback': _safe_text(failure.get('traceback') or '', MAX_TEXT),
        'seconds': failure.get('seconds'),
        'mode': {
            'all_categories': bool(job.get('all_categories')),
            'full_products': bool(job.get('full_products')),
            'force_ai': bool(job.get('force_ai')),
        },
        'events': [
            {'stage': _safe_text(x.get('stage', ''), 100),
             'message': _safe_text(x.get('message', ''), 1_000),
             'elapsed': x.get('elapsed')}
            for x in events[-MAX_EVENTS:] if isinstance(x, dict)
        ],
        'warnings': [_safe_text(x, 500) for x in warning_items[-20:]],
        'fingerprint': fingerprint,
    }


def _ssl_context(config):
    pem = config.get('ca_pem')
    if not isinstance(pem, str) or 'BEGIN CERTIFICATE' not in pem:
        raise ValueError('遥测配置缺少服务器证书')
    return ssl.create_default_context(cadata=pem)


def _send(config, report):
    body = json.dumps(report, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(body) > 128 * 1024:
        raise ValueError('失败诊断超过 128KB 限制')
    request = urllib.request.Request(
        config['endpoint'], data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + str(config['token']),
                 'User-Agent': 'AI-Crawler-Workbench/' + str(report.get('app_version', 'unknown'))},
    )
    with urllib.request.urlopen(request, timeout=15, context=_ssl_context(config)) as response:
        if response.status not in (200, 201, 202):
            raise OSError(f'failure collector HTTP {response.status}')


def submit_failure(project_root, run_root, job, version):
    """Queue the failure first, then best-effort flush all queued records."""
    project_root = Path(project_root)
    config = _config(project_root)
    if not config:
        return {'enabled': False}
    queue = project_root / 'telemetry_queue'
    queue.mkdir(exist_ok=True)
    report = build_failure_report(project_root, run_root, job, version)
    queued = queue / f"{report['run_id']}-{report['fingerprint'][:12]}.json"
    save_json(queued, report)
    sent = 0
    with _LOCK:
        for path in sorted(queue.glob('*.json'), key=lambda item: item.stat().st_mtime)[:25]:
            try:
                _send(config, read_json(path))
                path.unlink()
                sent += 1
            except (OSError, ValueError, KeyError, urllib.error.URLError):
                break
    return {'enabled': True, 'sent': sent, 'queued': len(list(queue.glob('*.json')))}


def submit_failure_async(project_root, run_root, job, version):
    thread = threading.Thread(
        target=submit_failure, args=(project_root, run_root, dict(job), version), daemon=True,
        name='failure-telemetry',
    )
    thread.start()
    return thread
