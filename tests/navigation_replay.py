"""Portable historical regression, deliberately separate from live acceptance."""
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.nav_extractor import extract_navigation
from core.navigation_guard import dom_tree_static_evidence, from_evidence
from core.validator import validate_navigation


def main():
    results = []
    for path in sorted((ROOT / 'tests/fixtures/navigation').glob('*.html.gz')):
        site = path.name.removesuffix('.html.gz')
        html = gzip.decompress(path.read_bytes()).decode('utf-8')
        base = 'https://' + site + '/'
        result = {'site': site, 'passed': False, 'scope': 'historical_navigation_only'}
        try:
            analysis = extract_navigation(html, base)
            proof = dom_tree_static_evidence(html, base, analysis)
            if not proof or not proof['complete']:
                raise AssertionError('Incomplete navigation')
            data = from_evidence(proof, html, base).data
            validate_navigation(data, [html], base, proof)
            # Regression expectations come from the previously diagnosed
            # defects, not a generated copy of the parser's entire output.
            if site == 'maryruthorganics.com':
                assert 'Shop,Shop AM & PM' in data
                assert not any('rewards' in url or '/apps/' in url for url in data.values())
                assert not any('Move forward' in key for key in data)
            if site == 'lolablankets.com':
                assert 'Blankets,Blankets Featured collections,Blankets Featured collections Neutrals Collection' in data
                assert 'Pets,Pets Pet Beds' in data
            if site == 'kytebaby.com':
                assert data['Sleep Bags'] != data['Sleep Bags,Sleep Bags Sleep Bags']
            if site == 'leatherman.com':
                assert 'Multi-tools,Multi-tools Activities,Multi-tools Activities Camp' in data
                assert 'Knives,Knives Activities,Knives Activities Camp' in data
                assert not any(' Menu' in key or 'Time-tested' in key for key in data)
            if site == 'k18hair.com':
                assert not any('Toggle' in key for key in data)
            result.update(passed=True, categories=len(data))
        except Exception as error:
            result['error'] = str(error)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    (ROOT / 'docs/navigation_replay_results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if results and all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
