"""Explicit network acceptance; discover cases from the user's local projects.

Does not execute old project code or use old JSON as the expected live menu.
Every attempt gets a fresh directory. Failed cases remain in the summary.
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.common import save_json
from core.pipeline import run_job


def discover(work):
    cases = {}
    for path in sorted(Path(work).glob('*/url.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                continue
            urls = [u for u in data.values() if isinstance(u, str) and '/collections/' in u]
            hosts = {urlsplit(u).hostname for u in urls} - {None}
            if len(hosts) == 1:
                host = next(iter(hosts))
                cases.setdefault(host.removeprefix('www.'), {'site': host, 'source': str(path)})
        except (ValueError, UnicodeError):
            continue
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', default='D:/pythonProject/work')
    parser.add_argument('--sites', nargs='+', default=['k18hair.com', 'jonesroadbeauty.com', 'lolablankets.com', 'explodingkittens.com'])
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--out', default=str(ROOT / 'runs' / ('shopify-acceptance-' + datetime.now().strftime('%Y%m%d-%H%M%S'))))
    args = parser.parse_args()
    root = Path(args.out).resolve(); root.mkdir(parents=True, exist_ok=False)
    cases = discover(args.work)
    save_json(root / 'inventory.json', list(cases.values()))
    results = []
    print('OUTPUT', root, flush=True)
    for name in args.sites:
        start = time.monotonic()
        case = cases.get(name.removeprefix('www.'))
        result = {'site': name, 'passed': False, 'scope': 'all_navigation_targets_and_one_full_listing_plus_three_products'}
        out = root / name
        try:
            if not case:
                raise RuntimeError('案例未出现在本地项目清单中')
            result['local_source'] = case['source']
            print('START', name, flush=True)
            if args.full:
                result['scope'] = 'all_navigation_targets_all_listings_all_products'
            report = run_job('https://' + case['site'], out, full_products=args.full, emit=lambda e: print(name, e['stage'], e['message'], flush=True))
            with tempfile.TemporaryDirectory() as tmp:
                with zipfile.ZipFile(out / 'crawler.zip') as archive:
                    archive.extractall(tmp)
                process = subprocess.run([sys.executable, 'verify.py'], cwd=tmp, capture_output=True, text=True, encoding='utf-8', timeout=120)
                if process.returncode:
                    raise RuntimeError(process.stderr)
            result.update(passed=True, report=report, zip_offline_verified=True)
        except Exception as error:
            result['error'] = str(error)
        result['seconds'] = round(time.monotonic() - start, 3)
        results.append(result)
        save_json(root / 'summary.json', {'all_passed': all(r['passed'] for r in results), 'completed': len(results), 'planned': len(args.sites), 'cases': results})
        print('RESULT', json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
