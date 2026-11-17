"""Prepare deterministic navigation evidence and a narrow AI review scope."""
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from .html_parser import parse_soup as BeautifulSoup

from .common import read_json, save_json
from .nav_extractor import extract_navigation, is_blacklisted
from .navigation_guard import (
    browser_evidence,
    dom_tree_static_evidence,
    from_evidence,
    linked_static_evidence,
)


POLICY = Path(__file__).with_name('navigation_policy.json')


def _labels_from_key(key):
    levels = key.split(',')
    if not levels or not levels[0].strip():
        raise ValueError('empty category key')
    labels = [levels[0].strip()]
    for parent, level in zip(levels, levels[1:]):
        prefix = parent + ' '
        if not level.startswith(prefix) or not level[len(prefix):].strip():
            raise ValueError('parent prefix mismatch')
        labels.append(level[len(prefix):].strip())
    return labels


def _coverage_complete(root):
    if not root.get('links'):
        return False
    coverage = root.get('submenu_coverage')
    if not coverage:
        return True
    expected = {item.get('leafId') for item in coverage.get('expected', []) if item.get('leafId')}
    return expected.issubset(set(coverage.get('observed_ids', [])))


def _review_scope(candidate, proof, warnings, browser_issue, evidence_dir):
    roots = proof.get('roots', []) if proof else []
    menu_roots = [root.get('label', '').strip() for root in roots if root.get('label')]
    menu_root_names = {root.casefold() for root in menu_roots}
    reliable = {
        root['label'].casefold() for root in roots
        if _coverage_complete(root) and (proof.get('complete') or root.get('submenu_coverage'))
    }
    files = ['evidence/' + path.name for path in sorted(evidence_dir.glob('home*.html'))]
    parsed, key_flags = {}, {}
    for key, url in candidate.items():
        flags = []
        try:
            labels = _labels_from_key(key)
        except ValueError as error:
            labels = [key.split(',')[0].strip() or key]
            flags.append(str(error))
        if len(labels) > 3:
            flags.append('more_than_three_levels')
        distinct_same_name_child = bool(proof and proof.get('method') == 'owned_menu_tree' and any(
            root.get('url') and root['url'] != url and len(labels) == 2
            and labels[0].casefold() == labels[1].casefold()
            and any(entry['labels'] == labels and entry['url'] == url for entry in root.get('links', []))
            for root in roots))
        if not distinct_same_name_child and any(labels[i].casefold() == labels[i - 1].casefold() for i in range(1, len(labels))):
            flags.append('duplicate_adjacent_level')
        if any(re.search(r'\b\d+\s+(?:games?|products?|steps?|expansions?|bundles?)\b', label, re.I) for label in labels):
            flags.append('descriptive_count_in_label')
        if is_blacklisted(url, key):
            flags.append('blacklisted_url')
        root = labels[0]
        kind = 'menu' if root.casefold() in menu_root_names else 'home'
        parsed[key] = {'key': key, 'url': url, 'labels': labels, 'root': root, 'kind': kind}
        if flags:
            key_flags[key] = flags

    issues = []
    def add(kind, message, keys=(), roots=()):
        issues.append({
            'id': f'U{len(issues) + 1:03d}',
            'kind': kind,
            'message': message,
            'keys': list(keys),
            'roots': list(roots),
            'evidence_files': files,
        })

    menu_keys = [key for key, entry in parsed.items() if entry['kind'] == 'menu']
    home_keys = [key for key, entry in parsed.items() if entry['kind'] == 'home']
    if browser_issue:
        uncertain_roots = sorted({entry['root'] for entry in parsed.values()
                                  if entry['root'].casefold() not in reliable})
        affected = [key for key, entry in parsed.items() if entry['root'] in uncertain_roots]
        add('browser_incomplete', browser_issue, affected, uncertain_roots)
    if warnings and not (proof and proof.get('complete')):
        for warning in dict.fromkeys(warnings):
            add('static_warning', warning, menu_keys, sorted({parsed[key]['root'] for key in menu_keys}))
    menu_urls = {candidate[key] for key in menu_keys}
    duplicate_home = [key for key in home_keys if candidate[key] in menu_urls]
    if duplicate_home:
        add('homepage_duplicate', '首页区块候选与菜单 URL 重复，需消解重复来源。', duplicate_home,
            sorted({parsed[key]['root'] for key in duplicate_home}))
    for key, flags in key_flags.items():
        add('structural_label', f'{key}: {", ".join(flags)}', [key], [parsed[key]['root']])
    if not candidate:
        add('empty_candidate', '程序没有取得任何可审查的分类候选。')

    uncertain_keys = {key for issue in issues for key in issue['keys']}
    for key, entry in parsed.items():
        if entry['kind'] == 'menu' and entry['root'].casefold() not in reliable:
            uncertain_keys.add(key)
    confirmed = {key: url for key, url in candidate.items() if key not in uncertain_keys}
    uncertain = {key: url for key, url in candidate.items() if key in uncertain_keys}
    confirmed_roots = list(dict.fromkeys(parsed[key]['root'] for key in confirmed if parsed[key]['kind'] == 'menu'))
    uncertain_roots = list(dict.fromkeys(parsed[key]['root'] for key in uncertain if parsed[key]['kind'] == 'menu'))
    dynamic_issue_ids = [issue['id'] for issue in issues if issue['kind'] == 'browser_incomplete']
    dynamic_evidence_required = not bool(proof and proof.get('complete')) or bool(dynamic_issue_ids)
    return {
        'scope_version': 2,
        'policy_version': read_json(POLICY)['version'],
        'program_complete': bool(proof and proof.get('complete')),
        'dynamic_evidence_required': dynamic_evidence_required,
        'dynamic_issue_ids': dynamic_issue_ids,
        'confirmed_candidate': confirmed,
        'uncertain_candidate': uncertain,
        'confirmed_roots': confirmed_roots,
        'uncertain_roots': uncertain_roots,
        'issues': issues,
        'tree': list(parsed.values()),
        'evidence_files': files,
        'instructions': {
            'confirmed': '默认原样保留；实证冲突必须在 reopened_confirmed 重开并由程序独立核验，禁止静默修改。',
            'uncertain': '只核对这些分支；dynamic_evidence_required=true 时必须调用 browser_tool.py 并保存 home-menu-* 动态快照，其他情况仅在证据不足时补抓。',
            'corrections': '每项修正必须关联 issue_id，并引用 evidence/home*.html 原始证据。',
        },
    }


def prepare_evidence(root, base):
    root = Path(root)
    evidence = root / 'evidence'
    evidence.mkdir(exist_ok=True)
    started = time.monotonic()
    html = (evidence / 'home.html').read_text(encoding='utf-8')
    warnings, candidate, proof, browser_issue = [], {}, None, None
    analysis = None
    try:
        analysis = extract_navigation(html, base)
        candidate = analysis.data
        warnings.extend(analysis.warnings)
    except Exception as error:
        warnings.append('静态候选: ' + str(error))

    if analysis is not None:
        proof = dom_tree_static_evidence(html, base, analysis)
        if proof:
            try:
                candidate = from_evidence(proof, html, base).data
                (evidence / 'home-program.html').write_text(html, encoding='utf-8')
            except Exception as error:
                warnings.append('静态 DOM 路径整理: ' + str(error))
                proof = None

    if proof is None:
        try:
            proof, captures, final = browser_evidence(base, timeout=15, out=evidence)
            for index, fragment in enumerate(captures):
                (evidence / f'home-menu-{index:03d}.html').write_text(fragment, encoding='utf-8')
            (evidence / 'home-program.html').write_text(final, encoding='utf-8')
            try:
                candidate = from_evidence(proof, final, base).data
            except Exception as error:
                browser_issue = '程序路径整理未完成: ' + str(error)
                warnings.append(browser_issue)
                proof = dict(proof, complete=False, issue=browser_issue)
                candidate = {}
        except Exception as error:
            browser_issue = '程序展开未完成: ' + str(error)
            linked = linked_static_evidence(html, base)
            if linked:
                proof = linked
                proof['browser_fallback_issue'] = browser_issue
                try:
                    candidate = from_evidence(proof, html, base).data
                    browser_issue = None
                except Exception as error:
                    warnings.append('静态关联菜单路径整理: ' + str(error))
            else:
                partial_file = evidence / 'menu-observation-partial.json'
                partial = read_json(partial_file) if partial_file.is_file() else {'roots': []}
                proof = {'version': 4, 'method': 'browser_partial', 'complete': False,
                         'roots': partial.get('roots', []), 'issue': browser_issue}
                for index, path in enumerate(sorted(evidence.glob('menu-observation-*.html'))):
                    target = evidence / f'home-menu-{index:03d}.html'
                    target.write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
                snapshots = sorted(evidence.glob('home-menu-*.html'))
                if proof['roots'] and snapshots:
                    try:
                        partial_candidate = from_evidence(proof, snapshots[-1].read_text(encoding='utf-8'), base).data
                        candidate.update(partial_candidate)
                    except Exception as error:
                        warnings.append('部分菜单路径整理: ' + str(error))
    save_json(root / 'program_candidate.json', candidate)
    save_json(evidence / 'program_navigation.json', proof)
    inventory = []
    for path in [evidence / 'home.html', *sorted(evidence.glob('home-menu-*.html'))]:
        soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'lxml')
        links = [{'label': anchor.get_text(' ', strip=True), 'url': urljoin(base, anchor['href'])}
                 for anchor in soup.select('header a[href],nav a[href],[role=navigation] a[href]')]
        inventory.append({'file': path.name, 'links': links})
    save_json(evidence / 'menu_link_inventory.json', inventory)
    save_json(root / 'navigation_policy_used.json', read_json(POLICY))
    scope = _review_scope(candidate, proof, warnings, browser_issue, evidence)
    scope['base_url'] = base
    save_json(root / 'navigation_review_scope.json', scope)
    if scope['issues']:
        from .navigation_prefetch import prefetch_navigation
        from .navigation_digest import build_digest
        prefetch_navigation(base,evidence)
        build_digest(root,base)
        scope['instructions']['uncertain']='只整理 nav_digest.json；浏览器取证由 Python 完成，AI 禁止操作浏览器或读取原始 HTML。'
        save_json(root/'navigation_review_scope.json',scope)
    report = {
        'mode': 'hybrid_scope_v2',
        'base_url': base,
        'candidate_count': len(candidate),
        'confirmed_count': len(scope['confirmed_candidate']),
        'uncertain_count': len(scope['uncertain_candidate']),
        'issue_count': len(scope['issues']),
        'program_expansion_complete': bool(proof and proof.get('complete')),
        'issues': [issue['message'] for issue in scope['issues']],
        'seconds': round(time.monotonic() - started, 3),
        'note': 'AI 只能补查疑点；程序确定项不可静默改写。',
    }
    save_json(root / 'navigation_preflight.json', report)
    return report
