"""Fail-closed, versioned navigation release shared by platform and packages.

Candidate generation is not publication. Every distinct destination is checked
before downstream collection, and saved inputs are bound to one release.
"""
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, unquote

from .common import CrawlError, save_json, read_json, query

COLLECTION_PATH = r'(?:/[a-z]{2}(?:-[A-Za-z]{2})?)?/collections/[^/]+(?:/[^/]+)?/?'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _target(client, url, directory, index):
    from .shopify_fastpath import listing_dom, collect_listing
    p = urlsplit(url)
    result = {'url': url, 'passed': False, 'files': []}
    try:
        if not re.fullmatch(COLLECTION_PATH, p.path):
            raise CrawlError('不是标准 Shopify 商品集合入口，需要独立适配: ' + url)
        response = client.get(url)
        final = urlsplit(response.url)
        if final.hostname != p.hostname or not re.fullmatch(COLLECTION_PATH, final.path):
            raise CrawlError('分类重定向离开同域商品集合: ' + response.url)
        if unquote(final.path).rstrip('/') != unquote(p.path).rstrip('/') or final.query != p.query:
            result.update(requested_url=url, redirected_to=response.url, url=response.url)
            url, p = response.url, final
        html_file = directory / f'target-{index:03d}.html'
        html_file.write_text(response.text, encoding='utf-8')
        result['files'].append(html_file.name)
        slugs, _, _ = listing_dom(response.text, url)
        collection_path = p.path.split('/collections/')[0] + '/collections/' + p.path.split('/collections/')[1].split('/')[0]
        tagged = p.path.rstrip('/') != collection_path
        endpoint = p._replace(path=collection_path + '/products.json', query='', fragment='').geturl()
        raw = client.json(query(endpoint, limit=1, page=1))
        raw_file = directory / f'target-{index:03d}.json'
        save_json(raw_file, raw)
        result['files'].append(raw_file.name)
        if not isinstance(raw, dict) or not isinstance(raw.get('products'), list):
            raise CrawlError('分类 feed 不是 products 数组')
        if any(not isinstance(x, dict) or not x.get('id') or not x.get('handle') for x in raw['products']):
            raise CrawlError('分类 feed 商品身份无效')
        if raw['products'] and not slugs:
            from .shopify_fastpath import rendered_listing_pages
            rendered = rendered_listing_pages(url, full=False)
            html = rendered[url]
            slugs, _, _ = listing_dom(html, url)
            if not slugs:
                raise CrawlError('浏览器渲染后仍无法确认分类商品列表')
            rendered_file = directory / f'target-{index:03d}-rendered.html'
            rendered_file.write_text(html, encoding='utf-8')
            result['files'].append(rendered_file.name)
            result['render_required'] = True
        landing = not raw['products'] and bool(slugs)
        if not raw['products'] and not slugs and not re.search(r'no products|no results|0 products|没有商品', response.text, re.I):
            raise CrawlError('空分类缺少前端空状态证据')
        if landing:
            from .shopify_fastpath import resolve_landing_membership
            products, sources, cards = resolve_landing_membership(client, response.text, url)
            landing_file = directory / f'target-{index:03d}-landing.json'
            save_json(landing_file, {'url': url, 'visible_handles': slugs, 'membership_sources': sources, 'card_sources': cards})
            result['files'].append(landing_file.name)
        if tagged or p.query or p.fragment:
            listing = collect_listing(client, url)
            if not listing.get('complete'):
                raise CrawlError('筛选或锚点分类范围无法确认')
            listing_file = directory / f'target-{index:03d}-listing.json'
            save_json(listing_file, listing)
            result['files'].append(listing_file.name)
        result.update(passed=True, kind='collection_landing' if landing else 'product_collection', empty=not raw['products'] and not slugs,
                      front_product_count=len(slugs), endpoint=endpoint)
    except Exception as error:
        result['error'] = str(error)
    return result


def publish(root, categories, proof, documents, base, client, visual_check=False):
    if proof.get('method')=='shopify_practical':
        return publish_practical(root,categories,proof,documents,base,client,visual_check)
    from .validator import validate_navigation
    root = Path(root)
    save_json(root / 'navigation_release.json', {'version': 1, 'status': 'checking'})
    save_json(root / 'category_candidate.json', categories)
    try:
        visual_files = []
        if visual_check:
            from .visual_audit import require_visual_audit
            visual_files = require_visual_audit(root, categories)
        validate_navigation(categories, documents, base, evidence=proof)
        if proof.get('drawer_trigger_node') is not None:
            audit_drawer(root, base, proof)
        directory = root / 'evidence' / 'category-targets'
        directory.mkdir(parents=True, exist_ok=True)
        urls = list(dict.fromkeys(categories.values()))
        previous_interval = getattr(client, 'min_interval', 0.15)
        previous_parallel = getattr(client, 'parallel_pacing', False)
        client.min_interval = 0.2
        client.parallel_pacing = True
        started = time.perf_counter()
        try:
            # Four thread-local sessions, 0.2s per worker; 429 cooldown is shared.
            # map preserves source order, regardless of completion order.
            with ThreadPoolExecutor(max_workers=4) as pool:
                targets = list(pool.map(lambda pair: _target(client, pair[1], directory, pair[0]), enumerate(urls)))
        finally:
            client.min_interval = previous_interval
            client.parallel_pacing = previous_parallel
        save_json(root / 'navigation_targets.json', targets)
        failed = [t for t in targets if not t['passed']]
        if failed:
            raise CrawlError(f'目录目标验证失败 {len(failed)}/{len(targets)}: ' + failed[0]['error'])
        source_categories = dict(categories)
        canonical = {t.get('requested_url', t['url']): t['url'] for t in targets}
        categories.update({key: canonical[value] for key, value in source_categories.items()})
        save_json(root / 'navigation_source_categories.json', source_categories)
        save_json(root / 'evidence' / 'navigation-observed.json', proof)
        # Evidence documents must already exist; never bind an unsaved in-memory proof.
        saved = list((root / 'evidence').glob('home*.html'))
        if not saved or any(html not in {p.read_text(encoding='utf-8') for p in saved} for html in documents):
            raise CrawlError('目录来源文档尚未完整落盘')
        save_json(root / 'url.json', categories)
        inputs = [root / 'url.json', root / 'navigation_source_categories.json', root / 'evidence/navigation-observed.json', root / 'navigation_targets.json', *saved, *visual_files]
        inputs.extend(p for p in (root/'evidence'/name for name in ('nav_digest.json','nav-ledger.json','nav-prefetch.json')) if p.is_file())
        inputs.extend(directory / name for target in targets for name in target['files'])
        if proof.get('drawer_trigger_node') is not None:
            inputs.extend([root / 'evidence/drawer-audit.json', root / 'evidence/drawer-open.html'])
        hashes = {p.relative_to(root).as_posix(): digest(p) for p in inputs}
        version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        release = {'version': 1, 'status': 'approved', 'navigation_version': version,
                   'base_url': base, 'categories': len(categories), 'unique_targets': len(targets),
                   'method': proof['method'], 'visual_check_required': visual_check, 'target_validation_seconds': round(time.perf_counter() - started, 3), 'inputs': hashes}
        save_json(root / 'navigation_release.json', release)
        return release
    except Exception as error:
        save_json(root / 'navigation_release.json', {'version': 1, 'status': 'blocked', 'error': str(error)})
        raise


def audit_drawer(root, base, proof):
    """Prove a shared drawer is reachable in the desktop viewport.

    Static ARIA association proves ownership; this check proves the entry and
    its submenus are actually usable, rather than a dormant mobile template.
    """
    from playwright.sync_api import sync_playwright
    from .navigation_guard import wait_for_storefront
    from .html_parser import parse_soup as BeautifulSoup
    source = (Path(root) / 'evidence/home.html').read_text(encoding='utf-8')
    soup = BeautifulSoup(source, 'lxml')
    nodes = soup.find_all(True)
    controls = [nodes[r['source_node']] for r in proof['roots']]
    panel_ids = [i for c in controls for i in c.get('aria-controls', '').split()]
    observations = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000}, locale='en-US')
            page.set_default_timeout(8000)
            page.goto(base, wait_until='load', timeout=45000)
            page.wait_for_timeout(1000)
            wait_for_storefront(page)
            trigger = page.locator('header button[data-drawer-name="menu"]')
            if trigger.count() != 1 or not trigger.is_visible():
                raise CrawlError('共享抽屉没有可见的桌面主入口')
            for attempt in range(2):
                trigger.click()
                try:
                    page.locator('[data-drawer-names="menu"]').wait_for(state='visible', timeout=5000)
                    break
                except Exception:
                    if attempt:
                        raise
            drawer = page.locator('[data-drawer-names="menu"]')
            drawer.wait_for(state='visible')
            if drawer.get_attribute('aria-hidden') == 'true':
                raise CrawlError('抽屉未实际展开')
            (Path(root) / 'evidence/drawer-open.html').write_text(page.content(), encoding='utf-8')
            for panel_id in panel_ids:
                control = page.locator('[data-drawer-names="menu"] [aria-controls=' + json.dumps(panel_id) + ']')
                control.click()
                panel = page.locator('[id=' + json.dumps(panel_id) + ']')
                panel.wait_for(state='visible')
                visible = panel.locator('a[href]:visible').evaluate_all('(nodes) => nodes.map(n=>n.href)')
                if not visible:
                    raise CrawlError('抽屉子面板没有可见链接: ' + panel_id)
                from .menu_tree import category
                from .navigation_rules import normalize_url
                source_panel = soup.find(id=panel_id)
                expected = {category(a, base) for a in source_panel.select('a[href]')} - {None}
                if expected - {normalize_url(u) for u in visible}:
                    raise CrawlError('抽屉子面板存在未可见核对的分类: ' + panel_id)
                observations.append({'panel': panel_id, 'visible_links': visible})
                back = panel.locator('button[aria-label*="Back" i]:visible')
                if back.count():
                    back.first.click()
            save_json(Path(root) / 'evidence/drawer-audit.json', {'passed': True, 'viewport': [1440, 1000], 'panels': observations})
        finally:
            browser.close()


def require_release(root):
    root = Path(root).resolve()
    path = root / 'navigation_release.json'
    if not path.is_file():
        raise CrawlError('缺少目录验收版本，禁止商品采集')
    release = read_json(path)
    if release.get('method') == 'simple_nav':
        if release.get('status') != 'approved' or not read_json(root/'url.json'):
            raise CrawlError('未生成可用目录')
        return release
    if release.get('version') != 1 or release.get('status') != 'approved' or not release.get('inputs'):
        raise CrawlError('目录未通过验收，禁止商品采集')
    hashes = release['inputs']
    expected = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if release.get('navigation_version') != expected:
        raise CrawlError('目录版本记录被修改')
    required = {'url.json', 'evidence/navigation-observed.json', 'navigation_targets.json'}
    if release.get('visual_check_required'):
        required.update({'visual_audit.json','visual_candidate.json','evidence/visual-manifest.json'})
    if not required.issubset(hashes) or not any(k.startswith('evidence/home') for k in hashes):
        raise CrawlError('目录版本缺少源证据')
    for name, fingerprint in hashes.items():
        source = (root / name).resolve()
        if root not in source.parents or not source.is_file() or digest(source) != fingerprint:
            raise CrawlError('目录或源证据已变更，必须重新审核: ' + name)
    categories = read_json(root / 'url.json')
    if release.get('method')=='shopify_practical':
        proof=read_json(root/'evidence/navigation-observed.json')
        if proof.get('navigation_contract')!='menu_owned_v1':
            raise CrawlError('旧版目录仅核验链接、未核验菜单归属，须重新生成目录')
    targets = read_json(root / 'navigation_targets.json')
    if {t['url'] for t in targets if t.get('passed')} != set(categories.values()):
        raise CrawlError('目标验证未覆盖全部目录')
    if 'navigation_source_categories.json' in hashes:
        source = read_json(root / 'navigation_source_categories.json')
        canonical = {t.get('requested_url', t['url']): t['url'] for t in targets}
        if {k: canonical.get(v) for k, v in source.items()} != categories:
            raise CrawlError('规范化目录与源目录不一致')
    if release.get('visual_check_required'):
        from .visual_audit import require_visual_audit
        require_visual_audit(root, read_json(root/'navigation_source_categories.json'))
    return release


def publish_practical(root,categories,proof,documents,base,client,visual_check=False):
    """Every URL has source evidence; at most four representative HTTP checks."""
    from .practical_navigation import validate_practical,warn,make_proof,us_collection_url
    from .shopify_inventory import collection_handle
    root=Path(root);started=time.perf_counter()
    if proof.get('navigation_contract')!='menu_owned_v1':raise CrawlError('缺少菜单归属核验，禁止发布仅有底账链接的目录')
    validate_practical(categories,proof,documents,base)
    urls=list(dict.fromkeys(categories.values()))
    indexes=sorted({0,len(urls)//3,2*len(urls)//3,len(urls)-1})
    directory=root/'evidence/category-targets';directory.mkdir(parents=True,exist_ok=True)
    def sample(index):
        url=urls[index];item={'url':url,'passed':False,'files':[],'check':'http_sample'}
        try:
            response=client.get(url,attempts=1,timeout=5)
            if not collection_handle(response.url,base):raise CrawlError('目标跳出同域商品集合')
            from .detector import observe_market
            market=observe_market(response.text)
            if not us_collection_url(response.url) or set(market['countries'])-{'US'} or set(market['currencies'])-{'USD'}:
                item['market_mismatch']=True
                raise CrawlError('分类目标不是 US/USD 市场')
            name=f'target-{index:03d}.html';(directory/name).write_text(response.text,encoding='utf-8')
            item.update(passed=True,files=[name])
            if response.url!=url:item.update(requested_url=url,redirected_to=response.url,url=response.url)
        except Exception as error:item['error']=str(error)
        return index,item
    previous=getattr(client,'parallel_pacing',False);client.parallel_pacing=True
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:sampled=dict(pool.map(sample,indexes))
    finally:client.parallel_pacing=previous
    for index,item in sampled.items():
        if item['passed']:continue
        if item.get('market_mismatch'):raise CrawlError(item['error'])
        if re.search(r'\b403\b|\b429\b|cloudflare|captcha',item['error'],re.I):raise CrawlError('分类访问阻塞: '+item['error'])
        if re.search(r'HTTP (?:Error )?(?:404|410)\b',item['error']):
            warn(root,'invalid_target_removed','目标明确不存在，移除该分类',url=urls[index])
            continue
        # A timeout is not evidence of nonexistence; preserve source-confirmed URL.
        warn(root,'target_sample_warning',item['error'],url=urls[index])
        item.update(passed=True,check='source_only_after_http_warning')
    targets=[sampled.get(i,{'url':url,'passed':True,'files':[],'check':'source_inventory_or_observed_href'}) for i,url in enumerate(urls)]
    removed={t['url'] for t in targets if not t['passed']}
    if removed:
        for key,url in list(categories.items()):
            if url in removed:categories.pop(key)
        if not categories:raise CrawlError('商品分类全部为空或不存在')
        updated=make_proof(categories,proof['inventory'],{k:proof['entries'][k] for k in categories},proof.get('auto_pruned_roots',[]),proof.get('manual_sources',[]))
        for name in ('navigation_contract','structural_categories','structural_proof','manual_confirmation'):
            if name in proof:updated[name]=proof[name]
        proof.clear();proof.update(updated)
        targets=[t for t in targets if t['passed']]
    source=dict(categories);canonical_urls={t.get('requested_url',t['url']):t['url'] for t in targets}
    categories.update({k:canonical_urls[v] for k,v in source.items()})
    for t in targets:
        if t.get('redirected_to'):warn(root,'collection_redirect','分类规范化跳转',url=t['requested_url'],redirected_to=t['url'])
    save_json(root/'navigation_source_categories.json',source)
    save_json(root/'category_candidate.json',source)
    save_json(root/'url.json',categories)
    save_json(root/'evidence/navigation-observed.json',proof)
    save_json(root/'navigation_targets.json',targets)
    paths=[root/n for n in ('url.json','navigation_source_categories.json','evidence/navigation-observed.json','navigation_targets.json')]
    paths+=list((root/'evidence').glob('home*.html'))
    paths+=[directory/name for t in targets for name in t['files']]
    if (root/'warnings.json').is_file():save_json(root/'navigation_warnings.json',read_json(root/'warnings.json'))
    paths+=[p for p in (root/'evidence/shopify_collections.json',root/'navigation_warnings.json',root/'manual_navigation_change.json',root/'visual_audit.json') if p.is_file()]
    hashes={p.relative_to(root).as_posix():digest(p) for p in paths}
    version=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
    release={'version':1,'status':'approved','method':'shopify_practical','navigation_version':version,'base_url':base,
             'categories':len(categories),'unique_targets':len(targets),'http_sampled_targets':len(indexes),
             'validation_scope':'desktop_menu_ownership_and_source_urls_plus_representative_http; product validation follows',
             'visual_check_required':False,'visual_requested':visual_check,'target_validation_seconds':round(time.perf_counter()-started,3),'inputs':hashes}
    save_json(root/'navigation_release.json',release)
    return release
