"""Explicit online acceptance: two sites, one complete category + 3 products each."""
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.pipeline import run_job
from core.common import save_json


def run(domain):
    out = ROOT / 'runs' / 'acceptance' / domain
    start = time.perf_counter()
    report = run_job('https://' + domain, out)
    wall = round(time.perf_counter() - start, 3)
    # A separate Python process only imports files from the extracted ZIP.
    with tempfile.TemporaryDirectory() as temp:
        with zipfile.ZipFile(out / 'crawler.zip') as z:
            z.extractall(temp)
        result = subprocess.run([sys.executable, 'verify.py'], cwd=temp, capture_output=True, text=True, encoding='utf-8')
        if result.returncode:
            raise RuntimeError(result.stderr)
    report['wall_seconds'] = wall
    report['zip_offline_verified'] = True
    report['zip_bytes'] = (out / 'crawler.zip').stat().st_size
    return report


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(run, ['gcioutdoor.com', 'blackstoneproducts.com']))
    save_json(ROOT / 'docs' / 'e2e_report.json', reports)
    print(json.dumps(reports, ensure_ascii=False, indent=2))
