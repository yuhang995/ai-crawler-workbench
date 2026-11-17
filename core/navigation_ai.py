"""Host-verified navigation; fallback models only organize a compact digest."""
import os
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin
from .html_parser import parse_soup as BeautifulSoup
from .common import CrawlError, canonical, read_json, save_json
from .ai_runtime import AI_SEMAPHORE

SKILL = Path(os.environ.get(
    'CATEGORY_JSON_SKILL',
    str(Path(__file__).resolve().parents[1] / 'skills/category-json'),
))
DECISION_SCHEMA = Path(__file__).with_name('navigation_decision_schema.json')
PITFALLS = Path(__file__).resolve().parents[1] / 'skills/category-json/pitfalls.md'


def selfcheck_inputs(root):
    import hashlib
    root = Path(root)
    paths = [root/name for name in ('category_candidate.json', 'navigation_decision.json',
             'navigation_review_scope.json', 'evidence/navigation-observed.json')]
    paths.extend(sorted((root/'evidence').glob('home*.html')))
    paths.extend(p for p in (root/'evidence'/name for name in ('nav_digest.json','nav-ledger.json','nav-prefetch.json')) if p.is_file())
    # codex -o may reformat the identical final decision after self-check.
    # Bind JSON semantics, not whitespace; bind source HTML byte-for-byte.
    return {p.relative_to(root).as_posix(): hashlib.sha256(
        json.dumps(read_json(p), sort_keys=True, ensure_ascii=False).encode('utf-8')
        if p.suffix == '.json' else p.read_bytes()).hexdigest() for p in paths}


def require_selfcheck(root):
    path = Path(root)/'navigation_selfcheck.json'
    if not path.is_file():
        raise CrawlError('AI 退出前没有运行 check_nav.py')
    result = read_json(path)
    if not result.get('passed') or result.get('inputs') != selfcheck_inputs(root):
        raise CrawlError('AI 离线自检未通过，或退出时修改了已检查产物；须重跑 check_nav.py')


def prepare_check_script(root):
    """Offline self-check imports the same host validator used after CLI exit."""
    root = Path(root)
    project = str(Path(__file__).resolve().parents[1])
    script = f'''import sys
import time
from pathlib import Path
sys.path.insert(0, {project!r})
from core.navigation_ai import load_navigation, selfcheck_inputs
from core.validator import validate_navigation
from core.common import read_json, save_json

if __name__ == '__main__':
    started = time.perf_counter()
    try:
        root = Path('.')
        candidate, proof, documents = load_navigation(root)
        base = read_json(root / 'navigation_review_scope.json')['base_url']
        validate_navigation(candidate, documents, base, evidence=proof)
        save_json(root / 'navigation_selfcheck.json', {{'passed': True, 'seconds': round(time.perf_counter()-started, 4), 'inputs': selfcheck_inputs(root)}})
        print('OK')
    except Exception as error:
        save_json(Path('navigation_selfcheck.json'), {{'passed': False, 'error': str(error), 'seconds': round(time.perf_counter()-started, 4)}})
        print('ERROR:', error)
        raise SystemExit(1)
'''
    (root / 'check_nav.py').write_text(script, encoding='utf-8')


def complete_proof_structure(root, candidate, proof):
    """Fill boilerplate only from independently reconstructed key/URL ownership.

    A real href alone never authorizes an invented parent or label. If the
    saved DOM is still ambiguous, the AI must provide its explicit proof.
    Review decisions/corrections remain mandatory and are checked afterwards.
    """
    from .navigation_guard import dom_tree_static_evidence, from_evidence, labels_from_key
    from .nav_extractor import extract_navigation, is_blacklisted
    from urllib.parse import urlsplit
    scope = read_json(root / 'navigation_review_scope.json')
    decision = read_json(root / 'navigation_decision.json')
    base = scope['base_url']
    if not isinstance(candidate, dict) or not candidate:
        raise CrawlError('自动补证缺少非空目录映射')
    owned, kinds = {}, {}
    for path in sorted((root / 'evidence').glob('home*.html')):
        html = path.read_text(encoding='utf-8')
        anchors = {canonical(urljoin(base, a['href'])) for a in BeautifulSoup(html, 'lxml').select('a[href]')}
        analysis = extract_navigation(html, base)
        tree = dom_tree_static_evidence(html, base, analysis)
        data = from_evidence(tree, html, base).data if tree and tree.get('complete') else analysis.data
        home_keys = {entry['key'] for entry in analysis.metadata.get('entries', []) if entry.get('source') == 'home'}
        if analysis.metadata.get('owned_tree'):
            from .menu_tree import tree_data
            home_keys.update(set(analysis.data) - set(tree_data(analysis.metadata['owned_tree'])))
        for key, url in data.items():
            if canonical(url) in anchors:
                owned.setdefault((key, url), []).append(path.relative_to(root).as_posix())
                kinds[key] = 'home' if key in home_keys else 'menu'
    generated = {}
    for key, url in candidate.items():
        labels = labels_from_key(key)
        if (not 1 <= len(labels) <= 3 or any(not x or x.isdigit() for x in labels)
                or is_blacklisted(url, key) or urlsplit(url).hostname != urlsplit(base).hostname):
            raise CrawlError('自动补证目录含非法路径或链接: ' + key)
        files = owned.get((key, url), [])
        if not files:
            raise CrawlError('无法从原始 DOM 独立确认目录归属，需明确审查证据: ' + key)
        item = generated.setdefault(labels[0], {'label': labels[0], 'url': candidate.get(labels[0]),
            'kind': kinds.get(key, 'menu'), 'links': [], 'source_files': []})
        item['links'].append({'labels': labels, 'url': url, 'model': False, 'source_files': files})
        item['source_files'] = sorted(set(item['source_files']) | set(files))
    proof.setdefault('version', 4)
    proof.setdefault('method', 'ai_skill')
    proof.setdefault('complete', decision.get('complete', False))
    if not proof.get('roots'):
        proof['roots'] = list(generated.values())
    else:
        for name, inferred in generated.items():
            matches = [r for r in proof['roots'] if r.get('label') == name]
            if not matches:
                proof['roots'].append(inferred)
            elif len(matches) == 1:
                existing = matches[0]
                for field in ('url', 'kind', 'source_files'):
                    existing.setdefault(field, inferred[field])
                links = existing.setdefault('links', [])
                present = {tuple(e.get('labels', [])) for e in links}
                links.extend(e for e in inferred['links'] if tuple(e['labels']) not in present)
    review = proof.setdefault('review', {})
    for name, value in {'complete': decision.get('complete', False), 'unresolved': decision.get('unresolved', []),
            'skill': 'category-json', 'scope_version': 2, 'resolved_issue_ids': decision.get('resolved_issue_ids', []),
            'confirmed_unchanged': decision.get('confirmed_unchanged', False)}.items():
        review.setdefault(name, value)
    if not review.get('branches'):
        review['branches'] = [{'label': r['label'], 'kind': r['kind'], 'source_files': r['source_files']}
                              for r in generated.values()]
    if 'corrections' not in review:
        files = sorted({name for r in generated.values() for name in r['source_files']})
        review['corrections'] = [{'issue_id': issue, 'action': 'replace', 'source_files': files,
            'reason': '程序从保存 DOM 重建目录归属；AI 决策：' + str(decision.get('summary', ''))}
            for issue in decision.get('resolved_issue_ids', [])]
    proof['structure_completed_by_python'] = True
    return proof


def normalize_proof_links(proof):
    """Canonicalize an AI artifact that used top-level links into roots[].links."""
    flat = proof.get('links')
    if flat is None:
        return proof, False
    if not isinstance(flat, list):
        raise CrawlError('AI 目录证据 links 必须为数组')
    roots = proof.get('roots', [])
    if not isinstance(roots, list) or not roots:
        raise CrawlError('AI 目录证据缺少 roots')
    index = {}
    folded = set()
    for root in roots:
        label = root.get('label') or root.get('root')
        if not label or label.casefold() in folded:
            raise CrawlError('AI 目录证据根目录为空或重复')
        folded.add(label.casefold())
        root['label'] = label
        root.setdefault('kind', 'menu')
        root.setdefault('links', [])
        index[label] = root
    nested_signatures = {
        (tuple(link.get('labels', [])), link.get('url'))
        for root in roots for link in root.get('links', [])
    }
    flat_signatures = {
        (tuple(entry.get('labels', [])), entry.get('url'))
        for entry in flat if isinstance(entry, dict)
    }
    if nested_signatures and nested_signatures != flat_signatures:
        raise CrawlError('AI 目录证据同时使用顶层和嵌套 links，且两者内容不一致')
    for entry in flat:
        if not isinstance(entry, dict) or not entry.get('url') or not entry.get('labels'):
            raise CrawlError('AI 顶层 links 存在无效目录记录')
        owner = entry.get('root') or entry['labels'][0]
        root = index.get(owner)
        if root is None or entry['labels'][0] != root['label']:
            raise CrawlError('AI 顶层 links 引用了不存在或不一致的根目录: ' + str(owner))
        if entry.get('key') and entry['key'] != _proof_key(entry['labels']):
            raise CrawlError('AI 顶层 links 的 key 与 labels 不一致: ' + entry['key'])
        item = {key: value for key, value in entry.items() if key not in {'key', 'root', 'kind'}}
        signature = (tuple(item['labels']), item['url'])
        existing = {(tuple(link.get('labels', [])), link.get('url')) for link in root['links']}
        if signature not in existing:
            root['links'].append(item)
    for root in roots:
        root.setdefault('url', next((item['url'] for item in root['links']
                                     if item.get('labels') == [root['label']]), None))
    proof.pop('links', None)
    return proof, True


def _proof_key(labels):
    return ','.join(' '.join(labels[:index + 1]) for index in range(len(labels)))


def _label_identity(value):
    return ' '.join(re.sub(r'[^\w]+', ' ', value.casefold(), flags=re.UNICODE).split())


def _snapshot_group_map(path, base):
    """Return URL -> visible desktop column headings from one immutable menu snapshot."""
    from .nav_extractor import is_blacklisted
    soup = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'lxml')
    groups = {}
    for listing in soup.find_all(['ul', 'ol']):
        classes = ' '.join(listing.get('class', []))
        if not re.search(r'grandchild|sub[-_ ]?menu', classes, re.I):
            continue
        parent = listing.parent
        parent_classes = ' '.join(parent.get('class', [])) if getattr(parent, 'attrs', None) is not None else ''
        if not re.search(r'column|child[-_ ]?list|menu', parent_classes, re.I):
            continue
        ancestry = ' '.join(
            str(node.get('id', '')) + ' ' + ' '.join(node.get('class', []))
            for node in [parent, *list(parent.parents)]
            if getattr(node, 'attrs', None) is not None
        )
        if re.search(r'mobile|drawer|accordion', ancestry, re.I):
            continue
        heading = None
        for sibling in parent.find_all(recursive=False):
            if sibling is listing:
                continue
            candidate = sibling if sibling.name in ('a', 'button', 'h2', 'h3', 'h4', 'h5', 'h6') else sibling.find(
                ['a', 'button', 'h2', 'h3', 'h4', 'h5', 'h6'], recursive=False)
            if candidate is not None:
                heading = candidate
                break
        if heading is None:
            continue
        group = ' '.join((heading.get('aria-label') or heading.get('title') or heading.get_text(' ', strip=True)).split())
        if not group or ',' in group or len(group) > 70 or group.casefold() in {'shop all', 'view all', 'see all'}:
            continue
        for anchor in listing.select('a[href]'):
            url = canonical(urljoin(base, anchor['href']))
            if is_blacklisted(url) or not re.search(r'/(?:collections|categories|category)/', url, re.I):
                continue
            leaf = _label_identity(' '.join(anchor.get_text(' ', strip=True).split()))
            if leaf:
                groups.setdefault((url, leaf), set()).add(group)
    return groups


def restore_snapshot_groups(root, candidate, proof, base):
    """Restore omitted mega-menu column headings when raw desktop DOM is unambiguous."""
    root = Path(root).resolve()
    changes = []
    replacements = {}
    for menu_root in proof.get('roots', []):
        root_label = menu_root.get('label')
        sources = menu_root.get('source_files', [])
        maps = []
        for source in sources:
            path = (root / source).resolve()
            if path.parent == (root / 'evidence').resolve() and path.name.startswith('home-menu-') and path.suffix.lower() == '.html' and path.is_file():
                maps.append(_snapshot_group_map(path, base))
        if not maps:
            continue
        known_groups = {group for mapping in maps for values in mapping.values() for group in values}
        for entry in menu_root.get('links', []):
            labels = entry.get('labels', [])
            if len(labels) not in (2, 3) or labels[0] != root_label:
                continue
            candidates = set()
            for mapping in maps:
                candidates.update(mapping.get((canonical(entry['url']), _label_identity(labels[-1])), set()))
            candidates.discard(root_label)
            candidates.discard(labels[-1])
            current_group = labels[1] if len(labels) == 3 else None
            if len(candidates) == 1:
                group = next(iter(candidates))
                new_labels = [root_label, group, labels[-1]]
            elif current_group in known_groups:
                new_labels = [root_label, labels[-1]]
                group = None
            else:
                continue
            old_key = _proof_key(labels)
            new_key = _proof_key(new_labels)
            if new_key == old_key:
                continue
            if old_key not in candidate or candidate[old_key] != entry['url']:
                raise CrawlError('动态菜单分组无法与 AI 候选对应: ' + old_key)
            entry['labels'] = new_labels
            replacements[old_key] = new_key
            changes.append({'old_key': old_key, 'new_key': new_key, 'url': entry['url'], 'group': group})
    if not changes:
        return candidate, proof, changes
    updated = {}
    for key, url in candidate.items():
        target = replacements.get(key, key)
        if target in updated and updated[target] != url:
            raise CrawlError('恢复动态菜单分组后发生目录键冲突: ' + target)
        updated[target] = url
    return updated, proof, changes


def validate_review_scope(root, candidate, proof):
    """Keep deterministic facts immutable and require evidence for every AI delta."""
    root = Path(root).resolve()
    scope = read_json(root / 'navigation_review_scope.json')
    decision = read_json(root / 'navigation_decision.json')
    if decision.get('version') != 2:
        raise CrawlError('AI 目录决策版本无效')
    confirmed = scope.get('confirmed_candidate', {})
    changed_confirmed = {key for key, url in confirmed.items() if candidate.get(key) != url}
    reopened = proof.get('review', {}).get('reopened_confirmed', [])
    if changed_confirmed != {item.get('key') for item in reopened}:
        raise CrawlError('AI 修改或删除了程序已确认的目录条目，且没有逐项重开审核')
    # A model cannot unlock facts by asserting that it has checked them.
    # Reopened items must agree with a reconstructable source-owned tree.
    from .menu_tree import build_tree, tree_data
    for item in reopened:
        files = item.get('source_files', [])
        if not files or not item.get('reason'):
            raise CrawlError('重开目录缺少原始证据或理由')
        verified = False
        for name in files:
            path = (root / name).resolve()
            if path.parent != (root / 'evidence').resolve() or not path.name.startswith('home') or path.suffix != '.html' or not path.is_file():
                raise CrawlError('重开目录引用了无效来源')
            tree = build_tree(path.read_text(encoding='utf-8'), scope['base_url'])
            if tree and tree['complete']:
                authoritative = tree_data(tree)
                owner = item['key'].split(',')[0]
                expected = {k: v for k, v in authoritative.items() if k.split(',')[0] == owner}
                actual = {k: v for k, v in candidate.items() if k.split(',')[0] == owner}
                if expected == actual:
                    verified = True
        if not verified:
            raise CrawlError('程序无法独立验证已确认目录的修正')
    issue_ids = {issue['id'] for issue in scope.get('issues', [])}
    resolved_items = decision.get('resolved_issue_ids', [])
    if len(resolved_items) != len(set(resolved_items)):
        raise CrawlError('AI 目录决策包含重复疑点编号')
    resolved = set(resolved_items)
    if not decision.get('complete') or decision.get('unresolved') or resolved != issue_ids:
        raise CrawlError('AI 没有完整解决目录疑点')
    if decision.get('confirmed_unchanged') != (not changed_confirmed):
        raise CrawlError('AI 未确认程序确定项保持不变')
    review = proof.get('review', {})
    if review.get('scope_version') != 2 or set(review.get('resolved_issue_ids', [])) != issue_ids:
        raise CrawlError('AI 目录证据没有覆盖全部疑点编号')
    if review.get('confirmed_unchanged') != (not changed_confirmed):
        raise CrawlError('AI 核对记录未锁定程序确定项')
    reviewed_items = decision.get('reviewed_roots', [])
    if len(reviewed_items) != len(set(reviewed_items)):
        raise CrawlError('AI 目录决策包含重复主导航根目录')
    reviewed = set(reviewed_items)
    confirmed_roots = set(scope.get('confirmed_roots', []))
    required_roots = set(scope.get('uncertain_roots', [])) | {name for issue in scope.get('issues', []) for name in issue.get('roots', [])}
    required_roots.update(k.split(',')[0] for k in changed_confirmed)
    final_menu_roots = {item.get('label') for item in proof.get('roots', []) if item.get('kind') != 'home'}
    if not required_roots.issubset(reviewed) or not (final_menu_roots - confirmed_roots).issubset(reviewed):
        raise CrawlError('AI 没有逐个确认主导航根目录')
    corrections = review.get('corrections', [])
    correction_ids = {item.get('issue_id') for item in corrections}
    if issue_ids and not issue_ids.issubset(correction_ids):
        raise CrawlError('AI 疑点修正记录不完整')
    program = {**scope.get('confirmed_candidate', {}), **scope.get('uncertain_candidate', {})}
    changed_urls = {canonical(url) for key, url in candidate.items() if program.get(key) != url}
    source_urls = set()
    evidence_root = (root / 'evidence').resolve()
    for item in [*proof.get('roots', []), *review.get('branches', [])]:
        if not isinstance(item, dict):
            continue
        for name in item.get('source_files', []):
            path = (root / name).resolve()
            if path.parent != evidence_root or not path.name.startswith('home') or path.suffix != '.html' or not path.is_file():
                raise CrawlError('菜单分支引用了无效证据文件: ' + str(name))
    dynamic_issue_ids = set(scope.get('dynamic_issue_ids', [])) or {item['id'] for item in scope.get('issues', []) if item.get('kind') == 'browser_incomplete'}
    dynamic_required = bool(scope.get('dynamic_evidence_required', scope.get('program_complete') is False or dynamic_issue_ids))
    dynamic_snapshots = list(evidence_root.glob('home-menu-*.html'))
    if dynamic_required and not dynamic_snapshots:
        raise CrawlError('程序菜单展开未完成，AI 必须补抓并引用 home-menu-* 动态快照')
    dynamic_cited = set()
    for correction in corrections:
        if correction.get('issue_id') not in issue_ids:
            raise CrawlError('AI 修正引用了不存在的疑点编号')
        files = correction.get('source_files', [])
        if not files or not correction.get('reason'):
            raise CrawlError('AI 修正缺少证据文件或理由')
        for name in files:
            path = (root / name).resolve()
            if path.parent != evidence_root or not path.name.startswith('home') or path.suffix.lower() != '.html' or not path.is_file():
                raise CrawlError('AI 修正引用了无效证据文件: ' + str(name))
            soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'lxml')
            source_urls.update(canonical(urljoin(scope.get('base_url', ''), anchor['href']))
                               for anchor in soup.select('a[href]'))
            if path.name.startswith('home-menu-'):
                dynamic_cited.add(correction.get('issue_id'))
    if dynamic_issue_ids - dynamic_cited:
        raise CrawlError('浏览器未完成疑点的修正记录必须引用 home-menu-* 动态快照')
    if dynamic_required:
        for branch in review.get('branches', []):
            if isinstance(branch, dict) and branch.get('kind') != 'home' and not any(Path(name).name.startswith('home-menu-') for name in branch.get('source_files', [])):
                raise CrawlError('动态核对任务的菜单分支必须引用 home-menu-* 快照')
    if changed_urls - source_urls:
        raise CrawlError('AI 新增或改名目录的 URL 未出现在声明的原始证据文件中')
    return decision

def serialize_paths(candidate, proof):
    """Keep display labels in evidence; reserve commas exclusively for path separators."""
    from .nav_extractor import is_blacklisted
    result={};covered=set();changes=[]
    for root in proof.get('roots',[]):
        root['label']=' '.join(root['label'].replace(',',' ').split())
        for entry in root.get('links',[]):
            raw=entry.get('display_labels',entry['labels'])
            old=','.join(' '.join(raw[:i+1]) for i in range(len(raw)))
            if candidate.get(old)!=entry['url']:raise CrawlError('AI 候选目录与原始路径记录不一致: '+old)
            covered.add(old)
            from .nav_extractor import is_blacklisted
            if is_blacklisted(entry['url']):raise CrawlError('AI 最终目录仍包含黑名单链接: '+entry['url'])
            labels=[' '.join(label.replace(',',' ').split()) for label in raw]
            key=','.join(' '.join(labels[:i+1]) for i in range(len(labels)))
            if key in result and result[key]!=entry['url']:raise CrawlError('目录格式化后同名冲突: '+key)
            if labels!=raw:
                entry['display_labels']=raw
                changes.append({'original':old,'key':key})
            entry['labels']=labels;result[key]=entry['url']
    if covered!=set(candidate):raise CrawlError('AI 审查记录遗漏候选目录')
    proof['roots']=[r for r in proof.get('roots',[]) if r.get('links')]
    return result,changes


def _fastpath_scope(root):
    """Return a fully deterministic scope, or None when AI review is required."""
    path=Path(root)/'navigation_review_scope.json'
    if not path.is_file():
        return None
    scope=read_json(path)
    if (scope.get('program_complete') is True and not scope.get('issues')
            and not scope.get('uncertain_candidate')):
        return scope
    return None


def _home_documents(root):
    documents=[path.read_text(encoding='utf-8') for path in sorted((Path(root)/'evidence').glob('home*.html'))]
    if not documents:
        raise CrawlError('确定性目录缺少首页或菜单 HTML 证据')
    return documents


def _finalize_deterministic(root):
    """Publish program evidence without creating any CLI invocation marker."""
    root=Path(root).resolve()
    scope=_fastpath_scope(root)
    if scope is None:
        return None
    program_path=root/'program_candidate.json'
    proof_path=root/'evidence/program_navigation.json'
    if not program_path.is_file() or not proof_path.is_file():
        raise CrawlError('确定性目录缺少程序候选或导航证据')
    candidate=read_json(program_path)
    confirmed=scope.get('confirmed_candidate',{})
    if not candidate or candidate!=confirmed:
        raise CrawlError('确定性目录范围与程序候选不一致')
    proof=read_json(proof_path)
    if not isinstance(proof,dict) or proof.get('version')!=4 or proof.get('complete') is not True or not proof.get('roots'):
        raise CrawlError('确定性目录证据不完整')
    documents=_home_documents(root)
    save_json(root/'category_candidate.json',candidate)
    save_json(root/'evidence/navigation-observed.json',proof)
    (root/'review.md').write_text(
        '# Deterministic navigation review\n\n'
        '程序证据已完整覆盖目录；无未决疑点，未启动 Codex CLI。\n',encoding='utf-8')
    save_json(root/'navigation_timing.json',{
        'cli_seconds':0,
        'mode':'deterministic_fastpath',
        'model':None,
        'reasoning_effort':None,
        'llm_invocations':0,
    })
    return candidate,proof,documents


def navigation_timeout(timeout=None):
    value=os.environ.get('NAVIGATION_TIMEOUT_SECONDS','180') if timeout is None else timeout
    try:
        seconds=int(value)
    except (ValueError,TypeError,OverflowError):
        raise CrawlError('NAVIGATION_TIMEOUT_SECONDS 必须为正整数秒')
    if seconds<=0 or isinstance(value,bool):
        raise CrawlError('目录 CLI 超时时间必须大于 0 秒')
    return min(seconds,180)


def run_navigation(root, base, emit=None, timeout=None):
    root=Path(root).resolve()
    timeout=navigation_timeout(timeout)
    if (root/'navigation_invocation.json').exists():
        raise CrawlError('本任务目录已调用目录 AI，不重复启动')
    from .hybrid_navigation import prepare_evidence
    prepare_evidence(root,base)
    deterministic=_finalize_deterministic(root)
    if deterministic is not None:return deterministic
    if not (root/'evidence/nav_digest.json').is_file():
        from .navigation_digest import build_digest
        build_digest(root,base)
    from .navigation_structurer import run_structurer
    return run_structurer(root,base,timeout=timeout)


def load_navigation(root):
    root=Path(root).resolve()
    marker=read_json(root/'navigation_invocation.json') if (root/'navigation_invocation.json').is_file() else {}
    if marker.get('workflow_version')==3:
        from .navigation_digest import candidate_proof
        from .navigation_guard import labels_from_key
        candidate=read_json(root/'category_candidate.json')
        proof=read_json(root/'evidence/navigation-observed.json')
        urls=read_json(root/'evidence/nav-ledger.json')['urls']
        entries=[{'labels':labels_from_key(k),'url_id':urls.index(v)} for k,v in candidate.items()]
        rebuilt,_,_=candidate_proof(root,entries)
        if rebuilt!=candidate:raise CrawlError('目录与已确认范围不一致')
        validate_review_scope(root,candidate,proof)
        return candidate,proof,_home_documents(root)
    required=[root/'category_candidate.json',root/'evidence/navigation-observed.json',root/'review.md']
    timing=read_json(root/'navigation_timing.json') if (root/'navigation_timing.json').is_file() else {}
    if timing.get('mode')=='deterministic_fastpath':
        if (root/'navigation_invocation.json').exists():
            raise CrawlError('确定性直通产物不应包含 CLI 调用标记')
        if not all(path.is_file() for path in required):
            raise CrawlError('确定性目录缺少必要产物')
        scope=_fastpath_scope(root)
        if scope is None:
            raise CrawlError('确定性目录范围已发生变化，不能直接复用')
        candidate=read_json(required[0])
        proof=read_json(required[1])
        if candidate!=scope.get('confirmed_candidate',{}):
            raise CrawlError('确定性目录产物与已确认范围不一致')
        if proof.get('version')!=4 or proof.get('complete') is not True or not proof.get('roots'):
            raise CrawlError('确定性目录证据不完整')
        return candidate,proof,_home_documents(root)
    marker=read_json(root/'navigation_invocation.json') if (root/'navigation_invocation.json').is_file() else {}
    if marker.get('workflow_version',0)>=2:
        if not required[0].is_file() and (root/'url.json').is_file():
            save_json(required[0], read_json(root/'url.json'))
        required.remove(root/'evidence/navigation-observed.json')
    if marker.get('workflow_version',0)>=2:required.extend([root/'navigation_decision.json',root/'navigation_review_scope.json'])
    if not all(p.is_file() for p in required):raise CrawlError('目录 CLI 缺少必要产物，请查看 navigation_ai.log')
    documents=[p.read_text(encoding='utf-8') for p in sorted((root/'evidence').glob('home*.html'))]
    if marker.get('workflow_version',0)<2 and not list((root/'evidence').glob('home-menu-*.html')):raise CrawlError('缺少实际展开菜单的浏览器快照')
    proof_path = root/'evidence/navigation-observed.json'
    proof=read_json(proof_path) if proof_path.is_file() else {}
    raw_candidate=read_json(root/'category_candidate.json')
    proof, proof_normalized = normalize_proof_links(proof)
    completed = False
    if marker.get('workflow_version',0)>=2 and (not proof.get('roots') or not proof.get('review',{}).get('branches')
            or {_proof_key(e.get('labels', [])) for r in proof.get('roots', []) for e in r.get('links', [])} != set(raw_candidate)):
        proof = complete_proof_structure(root, raw_candidate, proof)
        completed = True
    if proof_normalized:
        save_json(proof_path, proof)
    review=proof.get('review',{})
    if proof.get('method')!='ai_skill' or not review.get('complete') or review.get('unresolved') or not review.get('branches'):raise CrawlError('AI 目录核对记录不完整')
    scope=read_json(root/'navigation_review_scope.json') if (root/'navigation_review_scope.json').is_file() else {}
    raw_candidate,proof,group_changes=restore_snapshot_groups(root,raw_candidate,proof,scope.get('base_url',''))
    if group_changes:
        save_json(required[0],raw_candidate)
        save_json(proof_path,proof)
        save_json(root/'group_formatting.json',group_changes)
        with (root/'review.md').open('a',encoding='utf-8') as f:
            f.write('\n\n平台证据格式化说明：根据动态桌面菜单快照恢复被省略的可见列标题；转换记录见 group_formatting.json。\n')
    if marker.get('workflow_version',0)>=2:validate_review_scope(root,raw_candidate,proof)
    candidate,changes=serialize_paths(raw_candidate,proof)
    if completed:
        save_json(proof_path,proof)
    if changes:
        save_json(root/'label_formatting.json',changes)
        with (root/'review.md').open('a',encoding='utf-8') as f:f.write('\n\n平台格式化说明：标签中的逗号替换为空格，逗号仅作为层级分隔符；原始显示名称保存在 display_labels，转换记录见 label_formatting.json。\n')
    return candidate,proof,documents
