"""Small HTTPS collector deployed outside the crawler workbench."""
import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import ssl
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAX_BODY = 128 * 1024
RATE_LIMIT = 60
RATE_WINDOW = 3600
ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DB = DATA / 'failures.db'
TOKEN = os.environ.get('FAILURE_COLLECTOR_TOKEN', '')
HITS = defaultdict(deque)
LOCK = threading.Lock()


def init_db():
    DATA.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS failures (
            id INTEGER PRIMARY KEY, received_at INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, site TEXT NOT NULL, stage TEXT,
            exception_type TEXT, app_version TEXT, installation_id TEXT,
            run_id TEXT, payload TEXT NOT NULL,
            UNIQUE(installation_id, run_id, fingerprint)
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_failures_fingerprint ON failures(fingerprint)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_failures_received ON failures(received_at)')


def limited(address):
    now = time.time()
    with LOCK:
        queue = HITS[address]
        while queue and queue[0] < now - RATE_WINDOW:
            queue.popleft()
        if len(queue) >= RATE_LIMIT:
            return True
        queue.append(now)
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = 'FailureCollector/1'
    def log_message(self, fmt, *args):
        print('%s %s' % (self.address_string(), fmt % args), flush=True)

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path == '/health':
            return self.reply(200, {'ok': True, 'service': 'failure-collector'})
        return self.reply(404, {'error': 'not found'})

    def do_POST(self):
        if urlsplit(self.path).path != '/v1/failures':
            return self.reply(404, {'error': 'not found'})
        expected = 'Bearer ' + TOKEN
        if not TOKEN or not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
            return self.reply(401, {'error': 'unauthorized'})
        if limited(self.client_address[0]):
            return self.reply(429, {'error': 'rate limited'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            size = 0
        if size <= 0 or size > MAX_BODY:
            return self.reply(413, {'error': 'invalid body size'})
        try:
            payload = json.loads(self.rfile.read(size))
            required = {'schema_version','fingerprint','site','run_id','installation_id','error'}
            if not isinstance(payload, dict) or not required.issubset(payload):
                raise ValueError('missing fields')
            site = str(payload['site'])[:253]
            if '/' in site or '\\' in site:
                raise ValueError('invalid site')
            packed = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            with sqlite3.connect(DB, timeout=10) as db:
                cursor = db.execute('''INSERT OR IGNORE INTO failures
                    (received_at,fingerprint,site,stage,exception_type,app_version,
                     installation_id,run_id,payload) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (int(time.time()), str(payload['fingerprint'])[:64], site,
                     str(payload.get('stage',''))[:100], str(payload.get('exception_type',''))[:200],
                     str(payload.get('app_version',''))[:50], str(payload['installation_id'])[:100],
                     str(payload['run_id'])[:100], packed))
                created = cursor.rowcount == 1
            daily = DATA / time.strftime('failures-%Y-%m-%d.jsonl')
            if created:
                with LOCK, daily.open('a', encoding='utf-8') as stream:
                    stream.write(packed + '\n')
            return self.reply(201 if created else 200, {'ok': True, 'created': created})
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return self.reply(400, {'error': str(error)[:200]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=43872)
    parser.add_argument('--cert', required=True)
    parser.add_argument('--key', required=True)
    args = parser.parse_args()
    if not TOKEN:
        raise SystemExit('FAILURE_COLLECTOR_TOKEN is required')
    init_db()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
