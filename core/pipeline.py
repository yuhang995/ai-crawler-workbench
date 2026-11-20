"""Single pass task orchestration: detect → scope/sample → validate → package."""
import json
import gc
import hashlib
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit
from .common import (Client, CrawlError, AdapterRequired, save_json, read_json,
                     split_category_key, AccessChallenge, redact_proxy,
                     redact_sensitive, TaskCancelled)
from .nav_extractor import extract_navigation, needs_dynamic, expand_menus
from .detector import detect
from .shopify_fastpath import (collect_listing, fetch_product, export,
                               merge_explicit_colorways, _large_document_get,
                               ListingCollectionTimeout)
from .validator import validate, validate_navigation

PROJECT = Path(__file__).resolve().parents[1]

_RETRYABLE_SAMPLE_ERRORS = (
    '达到接口 250 变体上限，需非标完整性核验',
    'Hydrogen 配置型套装缺少独立可验证变体',
)


def retryable_sample_verification_error(error):
    """Return true only when changing the implicit sample can resolve the failure.

    Network, identity, price and source-validation failures describe the store or
    transport and must remain hard failures. A truncated variant feed or a
    configurable set without standalone variants is specific to one sample.
    """
    message = str(error)
    return any(marker in message for marker in _RETRYABLE_SAMPLE_ERRORS)


def compact_shopify_listing_evidence(listing, evidence_root=None):
    """Drop redundant rich product/HTML payloads from feed-order evidence.

    The validator needs immutable product identity, complete feed page sizes and
    the emitted handle order.  Full product descriptions/images are collected
    separately from each product endpoint, so repeating them across a hundred
    overlapping collections only wastes memory and can make json.dumps fail.
    """
    if evidence_root is not None:
        evidence_root = Path(evidence_root)
        evidence_root.mkdir(parents=True, exist_ok=True)
        for page in listing.get('pages', []):
            html = page.get('html')
            if not isinstance(html, str) or not html:
                continue
            fingerprint = hashlib.sha256(
                (str(page.get('url', '')) + '\0' + html).encode('utf-8')).hexdigest()[:24]
            target = evidence_root / f'{fingerprint}.html'
            if not target.exists():
                target.write_text(html, encoding='utf-8')
            page['html_file'] = 'evidence/listing-pages/' + target.name
            page['html'] = ''
    if (listing.get('order_evidence') != 'products_feed_fallback'
            or listing.get('membership_sources')):
        return listing
    compact = {key: value for key, value in listing.items()
               if key not in {'products', 'feeds', 'pages'}}
    compact['products'] = [
        {'id': product.get('id'), 'handle': product.get('handle')}
        for product in listing.get('products', [])]
    compact['feeds'] = []
    for feed in listing.get('feeds', []):
        saved = {key: value for key, value in feed.items() if key != 'products'}
        saved['products'] = [
            {'id': product.get('id'), 'handle': product.get('handle')}
            for product in feed.get('products', [])]
        compact['feeds'].append(saved)
    compact['pages'] = [
        {**{key: value for key, value in page.items() if key != 'html'}, 'html': ''}
        for page in listing.get('pages', [])]
    return compact


def default_sample_candidates(categories, limit=3):
    """Return distinct deepest leaves in stable menu order.

    A node is a leaf only when no emitted category path owns a child path.  URL
    aliases under different branches are useful in url.json, but retrying the
    same collection cannot improve a sample, so only the first URL is tried.
    """
    items = list(categories.items())
    leaves = []
    for order, (key, url) in enumerate(items):
        prefix = key + ','
        if not any(other.startswith(prefix) for other, _ in items):
            labels = split_category_key(key)
            leaf = labels[-1].casefold() if labels else ''
            # Broad "All" pages can contain hundreds of products and are poor
            # smoke-test samples.  Keep them eligible, but prefer an equally
            # deep, concrete leaf such as a shape, color or product family.
            broad = bool(re.search(r'(^|\s)(?:shop\s+)?all(?:\s|$)', leaf))
            leaves.append((-len(labels), broad, order, key, url))
    leaves.sort()
    result, seen_urls = [], set()
    for _, _, _, key, url in leaves:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        result.append((key, url))
        if len(result) == limit:
            break
    return result


def content_landing_parent_urls(categories):
    """Return URLs whose emitted navigation node owns real descendants."""
    items = list(categories.items())
    return {url for key, url in items
            if any(other.startswith(key + ',') for other, _ in items)}


def direct_shopify_product_scope(detection, categories):
    """Recognize a Shopify storefront whose complete shop scope is direct PDPs.

    Very small stores can link their only purchasable item from the primary
    navigation and expose no collection route. Detection can then retain
    ``shopify=True`` while classifying the shell as ``custom_site``. Those
    first-party product URLs belong on the deterministic Shopify product JSON
    path, not in generic AI repair.
    """
    if not detection.get('shopify') or not categories:
        return False
    return all(re.fullmatch(r'/products/[^/]+/?', urlsplit(url).path, re.I)
               for url in categories.values())


def collect_full_shopify_listings(client, categories, evidence_root=None,
                                   skip_non_collection_landings=False,
                                   progress=None, max_workers=4):
    """Collect every product-bearing collection and skip proven parent landings.

    A menu parent may be a campaign/collection landing with no finite product
    grid while its emitted child categories contain the actual catalog.  Only
    that structural failure is downgraded, and only when the URL owns a real
    descendant in the published navigation tree.
    """
    parents = content_landing_parent_urls(categories)
    has_real_collections = any('/collections/' in urlsplit(value).path
                               for value in categories.values())
    urls = list(dict.fromkeys(categories.values()))

    def collect_one(index, url):
        try:
            try:
                value = collect_listing(client, url, prefer_feed_order=True,
                                        max_seconds=180)
            except TypeError as error:
                # Historical adapters/test doubles expose the older signature.
                if 'unexpected keyword argument' not in str(error):
                    raise
                value = collect_listing(client, url, prefer_feed_order=True)
            value = compact_shopify_listing_evidence(value, evidence_root)
            return index, url, 'success', value, None
        except Exception as error:
            return index, url, 'error', None, error

    # Collection membership requests are independent. A bounded pool prevents
    # a 100+ category store from spending several seconds serially on every
    # overlapping menu collection while Client still owns global pacing,
    # cooldown and per-domain access recovery.
    outcomes = {}
    workers = max(1, min(int(max_workers or 1), 4, len(urls)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(collect_one, index, url): (index, url)
                   for index, url in enumerate(urls)}
        done = 0
        for future in as_completed(futures):
            index, url, status, value, error = future.result()
            outcomes[index] = (url, status, value, error)
            done += 1
            if progress:
                progress(done, len(urls), url, status)

    values, successful, warnings, deferred_empty, deferred_timeout = [], [], [], [], []
    for index in range(len(urls)):
        url, status, value, error = outcomes[index]
        if status == 'success':
            successful.append(url)
            values.append(value)
            continue
        if isinstance(error, ListingCollectionTimeout):
            deferred_timeout.append((url, str(error), error))
            continue
        if isinstance(error, AdapterRequired):
            message = str(error)
            landing = ('落地页既无完整子集合来源' in message
                       or '落地页商品未被显式子集合完整覆盖' in message)
            content_landing = (skip_non_collection_landings and has_real_collections
                               and '/collections/' not in urlsplit(url).path
                               and '/products/' not in urlsplit(url).path
                               and '/product/' not in urlsplit(url).path)
            empty_collection = (skip_non_collection_landings
                                and '/collections/' in urlsplit(url).path
                                and landing)
            if empty_collection:
                deferred_empty.append((url, message, error))
                continue
            if not content_landing and (url not in parents or not landing):
                raise error
            warnings.append({'code': 'content_landing_skipped',
                             'message': '父级导购页没有独立完整商品网格；已保留下级真实分类继续全量采集',
                             'url': url, 'reason': message})
            continue
        raise error
    if deferred_empty:
        if not values:
            raise deferred_empty[0][2]
        warnings.extend({
            'code': 'empty_or_landing_collection_skipped',
            'message': '集合 Feed 为空且前端无可验证商品；已保留其他正常集合继续全量采集',
            'url': url, 'reason': message,
        } for url, message, _ in deferred_empty)
    if deferred_timeout:
        if not values:
            raise deferred_timeout[0][2]
        warnings.extend({
            'code': 'listing_collection_timeout_skipped',
            'message': '单个超大分类超过 180 秒上限；已保留其他正常分类继续全量采集',
            'url': url, 'reason': message,
        } for url, message, _ in deferred_timeout)
    return successful, values, warnings


def prepare_runtime(root):
    dest = Path(root) / 'core'
    dest.mkdir(exist_ok=True)
    for name in ('__init__.py', 'simple_nav.py', 'custom_site.py', 'custom_navigation.py', 'jsonld_storefront.py', 'pinia_storefront.py', 'mobify_storefront.py', 'sfcc_storefront.py', 'magento_storefront.py', 'bigcommerce_storefront.py', 'braun_navigation.py', 'braun_listing.py', 'braun_product.py', 'brumate_navigation.py', 'brumate_listing.py', 'practical_navigation.py', 'shopify_inventory.py', 'html_parser.py', 'browser_screenshot.py', 'visual_audit.py', 'visual_capture.py', 'ai_runtime.py', 'common.py', 'shopify_fastpath.py', 'nav_extractor.py', 'menu_tree.py', 'navigation_rules.py', 'navigation_guard.py', 'navigation_release.py', 'detector.py', 'validator.py'):
        shutil.copyfile(PROJECT / 'core' / name, dest / name)
    for name, body in {
        'get_detail_url.py': "from pathlib import Path\nfrom core.common import read_json\nroot=Path(__file__).resolve().parent\ndetection=read_json(root/'detection.json')\nif detection.get('custom_platform') in {'demandware', 'braun_mia', 'magento', 'bigcommerce', 'pinia', 'mobify', 'jsonld'}:\n    from core.custom_site import run_links\nelse:\n    from core.shopify_fastpath import run_links\nif __name__ == '__main__':\n    run_links(root)\n",
        '3.ShopifyCrawl_sort.py': "import argparse\nfrom pathlib import Path\nfrom core.common import read_json\nroot=Path(__file__).resolve().parent\ndetection=read_json(root/'detection.json')\nif detection.get('custom_platform') in {'demandware', 'braun_mia', 'magento', 'bigcommerce', 'pinia', 'mobify', 'jsonld'}:\n    from core.custom_site import run_products\nelse:\n    from core.shopify_fastpath import run_products\nif __name__ == '__main__':\n    p=argparse.ArgumentParser()\n    p.add_argument('--all', action='store_true', help='采集 detail_url.json 的全部商品，默认仅3个')\n    a=p.parse_args()\n    run_products(root, a.all)\n",
        'verify.py': "from pathlib import Path\nfrom core.validator import validate\nif __name__ == '__main__':\n    print(validate(Path(__file__).resolve().parent))\n",
    }.items():
        (Path(root) / name).write_text(body, encoding='utf-8')
    (Path(root) / 'requirements.txt').write_text('curl_cffi>=0.7\nbeautifulsoup4>=4.12\nlxml>=5\nplaywright>=1.40\n', encoding='utf-8')
    (Path(root) / 'README.txt').write_text(
        'Python 3.10+。已包含验证样本和CSV，无需重采或新建venv。\n'
        '现有环境需安装 requirements.txt 中的库。\n'
        '动态商品网格需已有 Playwright Chromium；缺失时运行 python -m playwright install chromium。\n'
        'python verify.py：离线复验交付样本。\n'
        'python get_detail_url.py：按 url.json 全部分类刷新完整前端链接。\n'
        'python 3.ShopifyCrawl_sort.py：只重新采集前三个商品。\n'
        'python 3.ShopifyCrawl_sort.py --all：显式启动全部详情。\n'
        'sample_scope.json 说明本次实际采集的分类。未运行分类不宣称已验证。\n'
        'result.csv/result_clean.csv 每行一个真实变体；Stock 空表示未知，不伪造库存。\n'
        'shopify_import.csv 默认 draft，图片采用真实变体映射。\n', encoding='utf-8')
    shutil.copyfile(PROJECT / 'docs' / 'artifact_contract.md', Path(root) / 'artifact_contract.md')


def package(root, report=None, started=None):
    root = Path(root)
    target = root / 'crawler.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in sorted(root.rglob('*')):
            if path.is_file() and path != target and '__pycache__' not in path.parts and path.suffix != '.tmp' and not (report is not None and path.name == 'report.json'):
                z.write(path, path.relative_to(root).as_posix())
        if report is not None:
            report['seconds'] = round(time.perf_counter() - started, 3)
            if 'directory_cli_seconds' in report:
                report['total_seconds']=round(report['seconds']+(report['directory_cli_seconds'] if report.get('reused_directory') else 0),3)
            save_json(root / 'report.json', report)
            z.write(root / 'report.json', 'report.json')
    return target


def run_job(site, root, manual=False, confirm=None, emit=None, category=None, all_categories=False, force_ai=False, dynamic=True, navigation_mode="simple", reuse_navigation=False, full_products=False, visual_check=False, uploaded_categories=None, proxy=None, use_proxy_pool=False, cancel_event=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'evidence').mkdir(exist_ok=True)
    start = time.perf_counter()
    def event(stage, message, **extra):
        if stage != 'cancelled' and cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled('任务已由用户取消')
        data = {'stage': stage, 'message': message, 'elapsed': round(time.perf_counter() - start, 3), **extra}
        if emit:
            emit(data)
        return data
    try:
        save_json(root / 'navigation_release.json', {'version': 1, 'status': 'checking'})
        if full_products and manual:
            raise CrawlError('全自动全量模式不能同时启用手动确认')
        all_categories = all_categories or full_products
        # Keep compatibility with the small zero-argument Client doubles used
        # by adapters and the historical replay/unit-test suite.  New transport
        # options are passed only when the caller actually enables them.
        client_options = {}
        if proxy:
            client_options['proxy'] = proxy
        if use_proxy_pool:
            client_options['proxy_pool'] = True
        if cancel_event is not None:
            client_options['cancel_event'] = cancel_event
        client = Client(**client_options) if client_options else Client()
        if '://' not in site:
            site = 'https://' + site
        p = urlsplit(site)
        base = p._replace(path='/', query='', fragment='').geturl()
        event('detecting', '读取首页与 US/USD 市场')
        # Hydrogen documents commonly embed a multi-megabyte server data
        # stream.  A slow but progressing public response is not an access
        # block, so allow the homepage one large-document request budget.
        response = _large_document_get(client, base)
        from .detector import resolve_storefront
        requested = response
        response = resolve_storefront(client, base, response)
        if response.url != requested.url:
            (root / 'evidence' / 'requested-origin.html').write_text(requested.text,encoding='utf-8')
            save_json(root / 'origin_resolution.json', {'entry': base, 'requested':requested.url,'canonical':response.url,'same_store_verified':True})
        base, html = response.url, response.text
        (root / 'evidence' / 'home.html').write_text(html, encoding='utf-8')
        from .custom_site import identify_platform, SUPPORTED_PLATFORMS
        custom_platform = identify_platform(html)
        t = time.perf_counter()
        static_error = None
        from .simple_nav import NATIVE_NAV_METHODS
        saved_method = read_json(root/'navigation_report.json').get('method') if reuse_navigation else None
        if uploaded_categories is not None:
            from .simple_nav import validate_uploaded_categories, publish_simple
            event('navigation', '校验手动放入的分类文件；跳过自动扣目录')
            categories = validate_uploaded_categories(uploaded_categories, base)
            save_json(root / 'uploaded_url.json', categories)
            save_json(root / 'evidence' / 'simple_nav_sources.json', {
                'base_url': base, 'valid_urls': list(dict.fromkeys(categories.values())),
                'source': 'uploaded_category_file'})
            evidence = {'method': 'simple_nav', 'roots': [],
                        'source': 'uploaded_category_file'}
            documents = [html]
            publish_simple(root, categories, method='uploaded_category_file',
                           scope='uploaded_category_file_structure_and_same_origin')
        elif reuse_navigation and saved_method in NATIVE_NAV_METHODS:
            if saved_method != 'custom_' + (custom_platform or 'unknown'):
                raise CrawlError('当前站点与已保存目录的平台不一致')
            from .custom_site import run_navigation as run_custom_navigation
            event('navigation_resume', '核对当前站点原生分类，复用已选范围')
            categories, evidence, documents, _ = run_custom_navigation(
                root, base, html, custom_platform, client=client, selection=read_json(root/'url.json'))
        elif reuse_navigation and saved_method in {'simple_nav', 'uploaded_category_file'}:
            from .simple_nav import publish_simple
            categories = read_json(root/'url.json')
            evidence, documents = {'method': 'simple_nav', 'roots': [],
                                   'source': saved_method}, [html]
            publish_simple(root,categories,method=saved_method)
        elif reuse_navigation:
            from .navigation_ai import load_navigation
            event('navigation_resume', '复用已完成的目录证据，重新校验并继续商品采集；不再调用 CLI')
            categories, evidence, documents = load_navigation(root)
            from .navigation_guard import dom_tree_static_evidence, from_evidence
            fresh = dom_tree_static_evidence(html, base)
            if fresh and fresh.get('complete'):
                current = from_evidence(fresh, html, base).data
                if current != categories:
                    raise CrawlError('当前菜单已变化，不能复用旧目录；请运行新的完整目录任务')
                evidence, documents = fresh, [html]
            elif html not in documents:
                raise CrawlError('当前页面无法独立确认旧目录仍有效，禁止复用')
        elif navigation_mode == 'simple' and custom_platform:
            from .custom_site import run_navigation as run_custom_navigation
            event('navigation_custom', f'按 {custom_platform} 原生导航层级提取目录')
            categories, evidence, documents, _ = run_custom_navigation(
                root, base, html, custom_platform, client=client)
        elif navigation_mode == 'simple':
            from .brumate_navigation import has_pack_menu_data
            from .simple_nav import run_simple
            from .detector import SHOPIFY_MARKERS
            from .custom_navigation import navigation_source
            if (not any(marker.casefold() in html.casefold() for marker in SHOPIFY_MARKERS)
                    and '/collections/' not in html and navigation_source(html, base)['valid_urls']):
                from .custom_site import run_navigation as run_custom_navigation
                event('navigation_ai', '按真实导航来源恢复非 Shopify 目录')
                categories, evidence, documents, _ = run_custom_navigation(
                    root, base, html, 'unknown', client=client)
            elif has_pack_menu_data(html):
                event('navigation', '核对桌面导航与页面提供的菜单层级')
                categories, evidence, documents = run_simple(root, base, html)
            else:
                event('navigation_ai', '精简 HTML → 单次 AI 整理层级 → 真实链接过滤')
                categories, evidence, documents = run_simple(root, base, html)
        elif navigation_mode == 'practical':
            from .detector import collection_inventory
            from .practical_navigation import build_practical
            event('navigation_fastpath','按真实菜单核验名称与归属；集合底账仅辅助查验，不填入目录')
            inventory=collection_inventory(client,base,html,root/'evidence')
            categories,evidence,documents=build_practical(root,base,html,inventory)
        elif navigation_mode == 'ai':
            from .navigation_ai import run_navigation
            event('navigation_ai', '先用程序确认目录；证据完整则直通，仅真实疑点启动 GPT-5.6 Sol Medium CLI')
            categories, evidence, documents = run_navigation(root, base, emit=emit)
            if ((root/'navigation_timing.json').is_file()
                    and read_json(root/'navigation_timing.json').get('mode')=='deterministic_fastpath'):
                event('navigation_fastpath', '程序目录证据完整且无疑点，已跳过 AI CLI')
        else:
            from .navigation_guard import static_evidence, browser_evidence, from_evidence, check
            evidence = static_evidence(html, base)
            documents = [html]
            analysis = None
            static_error = None
            try:
                analysis = extract_navigation(html, base)
                if analysis.warnings:
                    raise CrawlError('; '.join(analysis.warnings))
                check(analysis.data, evidence, html, base)
            except (CrawlError, ValueError) as error:
                static_error = str(error)
            if static_error:
                save_json(root / 'navigation_diagnostic.json', {'static_issue':static_error,'status':'checking'})
                if not dynamic:
                    raise CrawlError('静态目录不能独立确认，且浏览器核对已关闭: '+static_error)
                event('navigation_check', '静态目录证据不足，自动展开主菜单及子菜单核对')
                try:
                    evidence, captures, final_html = browser_evidence(base, out=root / 'evidence')
                    for i, fragment in enumerate(captures):
                        (root / 'evidence' / f'home-menu-{i:03d}.html').write_text(fragment,encoding='utf-8')
                        documents.append(fragment)
                    (root / 'evidence' / 'home-verified.html').write_text(final_html,encoding='utf-8')
                    documents.append(final_html)
                    analysis = from_evidence(evidence, final_html, base)
                    check(analysis.data, evidence, final_html, base)
                except Exception as error:
                    save_json(root / 'navigation_diagnostic.json', {'static_issue':static_error,'browser_issue':str(error),'status':'blocked'})
                    raise CrawlError('目录核对未通过，已停止发布目录与运行包: '+str(error)) from error
            categories = analysis.data
        nav_seconds = time.perf_counter() - t
        save_json(root / 'evidence' / 'navigation-observed.json', evidence)
        simple = evidence.get('method') == 'simple_nav'
        if not simple:
            validate_navigation(categories, documents, base, evidence=evidence)
        if static_error:
            save_json(root / 'navigation_diagnostic.json', {'static_issue':static_error,'status':'verified','method':evidence['method']})
        if not simple:
            save_json(root / 'navigation_report.json', {'audit_version':4,'passed':True,'method':evidence['method'],'roots':[r['label'] for r in evidence['roots']]})
        from .navigation_release import publish, require_release
        practical=evidence.get('method')=='shopify_practical'
        if practical and manual:
            from .practical_navigation import capture_preview,apply_edits
            if confirm is None:raise CrawlError('手动模式需要确认事件')
            save_json(root/'url.json',categories)
            save_json(root/'navigation_review_scope.json',{'base_url':base})
            capture_preview(base,root)
            event('awaiting_confirmation','请对照截图勾选或编辑目录，确认后采集样本并打包',categories=categories)
            if not confirm.wait():raise CrawlError('确认等待被取消')
            if (root/'confirmed_categories.json').is_file():categories,evidence=apply_edits(root,categories,evidence,base,client,documents)
        if simple and manual:
            if confirm is None:raise CrawlError('手动模式需要确认事件')
            event('awaiting_confirmation', '请勾选或编辑目录，确认后采集样本并打包', categories=categories)
            if not confirm.wait():raise CrawlError('确认等待被取消')
            if (root/'confirmed_categories.json').is_file():
                from .simple_nav import clean_reviewed_categories,publish_simple
                report=read_json(root/'navigation_report.json')
                sources=read_json(root/'evidence/simple_nav_sources.json')
                categories,warnings=clean_reviewed_categories(read_json(root/'confirmed_categories.json'),sources,report['method'])
                publish_simple(root,categories,warnings,method=report['method'],scope=report['scope'])
        if visual_check and not simple:
            from .visual_audit import run_visual_audit
            event('visual_audit', '截取配对证据并执行视觉审核；实用模式疑点记入警告' if practical else '截取配对证据并执行只读视觉审核；疑点或覆盖不足会停止发布')
            try:run_visual_audit(root, base, categories, evidence)
            except CrawlError as error:
                if not practical:raise
                from .practical_navigation import warn
                warn(root,'visual_audit_warning',str(error))
        if simple:
            release = read_json(root/'navigation_release.json')
            event('navigation_targets', '已过滤非来源 URL 并规范键名；不运行旧版目录审查')
        else:
            event('navigation_targets', '核对全部目录来源并抽检代表性目标' if practical else '逐个验证分类目标及筛选范围；全部通过后发布目录版本')
            release = publish(root, categories, evidence, documents, base, client, visual_check=visual_check)
        require_release(root)
        detection = detect(client, base, html, categories)
        save_json(root / 'detection.json', detection)
        config = {'visual_check': visual_check, 'base_url': base, 'market': 'US', 'currency': 'USD', 'manual': manual,
                  'category_source': 'uploaded_category_file' if uploaded_categories is not None else 'automatic',
                  'navigation_version': release['navigation_version'],
                  'all_categories': all_categories, 'category': category, 'nav_seconds': round(nav_seconds, 4)}
        save_json(root / 'config.json', config)
        prepare_runtime(root)
        if (set(detection['market']['countries']) - {'US'} or set(detection['market']['currencies']) - {'USD'}):
            raise CrawlError('首页未明确确认 US/USD，停止以防价格市场错误')
        event('navigation', f'提取 {len(categories)} 个目录；站点类型 {detection["mode"]}', categories=categories)
        matching = {k: u for k, u in categories.items() if not category or k == category or u == category}
        if not matching:
            raise CrawlError('指定分类未出现在真实目录中')
        if all_categories:
            selected = matching
            sample_candidates = []
            selection_status = 'all_categories'
        elif category:
            selected = dict(list(matching.items())[:1])
            sample_candidates = list(selected.items())
            selection_status = 'explicit'
        else:
            # Three candidates are often all tiny seasonal leaves.  Keep the
            # same deepest-first order but inspect a bounded wider set before
            # declaring that the store cannot provide a three-product sample.
            sample_candidates = default_sample_candidates(categories, limit=12)
            if not sample_candidates:
                raise CrawlError('目录中没有可用于抽样的叶子分类')
            selected = dict(sample_candidates[:1])
            selection_status = 'planned'
        selection_attempts = []
        def write_sample_scope(status=selection_status):
            save_json(root / 'sample_scope.json', {
                'selected_categories': selected, 'navigation_categories': len(categories),
                'navigation_version': release['navigation_version'],
                'listing_scope': 'all' if all_categories else 'selected',
                'detail_scope': 'all_products' if full_products else 'first_3_products',
                'selection_status': status,
                'sample_candidates': [{'category': key, 'url': url} for key, url in sample_candidates],
                'selection_attempts': selection_attempts})
        write_sample_scope()
        direct_product_scope = direct_shopify_product_scope(detection, selected)
        def ai_fallback(reason):
            from .ai_worker import run_once
            event('ai', '非标路径：启动商品适配会话')
            # Provide observed collection HTML, not just an empty task directory.
            for i, u in enumerate(list(selected.values())[:2]):
                (root / 'evidence' / f'collection-{i}.html').write_text(client.get(u).text, encoding='utf-8')
            run_once(root, reason)
            return read_json(root / 'samples.json')

        def repair_sample(error):
            nonlocal selected
            from .ai_repair import repair_sample as recover
            event('ai_repair', '保存失败现场，AI 修复商品适配并在任务内自测')
            directory = root / 'evidence' / 'repair-sources'
            directory.mkdir(exist_ok=True)
            sources = []
            cached_responses = []
            prediagnosis = []
            for index, response in enumerate(list(client.cache.values())):
                content_type = response.headers.get('Content-Type', '')
                if 'json' not in content_type and 'html' not in content_type:
                    continue
                suffix = '.json' if 'json' in content_type else '.html'
                name = str(index) + suffix
                (directory / name).write_text(response.text, encoding='utf-8')
                sources.append({'url': response.url, 'file': 'evidence/repair-sources/' + name})
                text = response.text or ''
                size = len(text.encode('utf-8', errors='replace'))
                anchors = len(re.findall(r'<a\b', text, re.I))
                loader = bool(re.search(
                    r'<(?:div|main|section)[^>]+(?:class|id)=["\'][^"\']*'
                    r'(?:loader|loading|app-root|__next|root)[^"\']*["\']', text, re.I))
                spa_shell = bool(loader and anchors < 3) or bool(size > 50_000 and anchors < 3)
                cached_responses.append({
                    'url': response.url, 'file': 'evidence/repair-sources/' + name,
                    'content_type': content_type, 'bytes': size,
                    'anchor_tags': anchors, 'loader_or_root_marker': loader,
                    'suspected_spa_shell': spa_shell,
                })
                if spa_shell:
                    prediagnosis.append(
                        f'{response.url} 疑似 CSR/SPA 空壳：{size} bytes，{anchors} 个 <a>，'
                        f'loader/root={loader}')
            save_json(directory / 'index.json', sources)
            frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
            location = None
            if frames:
                frame = frames[-1]
                location = {'file': frame.filename, 'line': frame.lineno,
                            'function': frame.name, 'source': frame.line}
            context = {
                'error': str(error),
                'error_type': type(error).__name__,
                'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__)),
                'failure_location': location,
                'attempted_category_urls': list(dict.fromkeys(selected.values())),
                'cached_responses': cached_responses,
                'prediagnosis': prediagnosis,
            }
            save_json(root / 'evidence' / 'repair_failure_context.json', context)
            full_scope = read_json(root / 'sample_scope.json')
            recover(root, str(error))
            if full_products:
                # The model is deliberately validated on one category and
                # three parents.  Only the host may expand the adopted,
                # validated scripts back to the user's original full scope.
                save_json(root / 'sample_scope.json', dict(
                    full_scope, selection_status='repaired_full_running'))
                link_result = subprocess.run(
                    [sys.executable, str(root / 'get_detail_url.py')], cwd=root,
                    capture_output=True, text=True, encoding='utf-8', timeout=900)
                if link_result.returncode:
                    raise CrawlError('修复后全量列表采集失败: ' +
                                     (link_result.stderr or link_result.stdout)[-2000:])
                from .navigation_release import digest
                save_json(root / 'detail_release.json', {
                    'navigation_version': release['navigation_version'],
                    'sha256': digest(root / 'detail_url.json')})
                product_result = subprocess.run(
                    [sys.executable, str(root / 'crawl_products.py'), '--all'], cwd=root,
                    capture_output=True, text=True, encoding='utf-8', timeout=3600)
                if product_result.returncode:
                    raise CrawlError('修复后全量商品采集失败: ' +
                                     (product_result.stderr or product_result.stdout)[-2000:])
                from .ai_repair import _check_candidate
                _check_candidate(root, full_scope)
            detail = read_json(root / 'detail_url.json')
            selected = {key: categories[key] for key in detail}
            # Bind repaired detail scope to the already-published navigation.
            # Custom adapters used to do this in their caller, while the former
            # Shopify fallback bypassed the controlled repair path entirely.
            from .navigation_release import digest
            save_json(root / 'detail_release.json', {
                'navigation_version': release['navigation_version'],
                'sha256': digest(root / 'detail_url.json')})
            write_sample_scope('repaired')
            return read_json(root / 'samples.json')
        def standard_run():
            nonlocal selected
            listings, detail, samples = {}, {}, None
            event('listing', '按分类顺序读取前端网格与 JSON，共享限速和访问恢复状态')
            if direct_product_scope:
                # There is no collection listing to parse. The official product
                # JSON proves both identity and membership for this exact menu URL.
                unique = list(dict.fromkeys(selected.values()))
                with ThreadPoolExecutor(max_workers=min(3, len(unique))) as pool:
                    samples = list(pool.map(lambda u: fetch_product(client, u), unique))
                values = []
                by_sample = {sample['url']: sample for sample in samples}
                for url in unique:
                    product = by_sample[url]['product']
                    values.append({
                        'format': 'direct_product', 'url': url, 'urls': [url],
                        'products': [product], 'feeds': [{'products': [product]}],
                        'pages': [], 'card_sources': [], 'warnings': [],
                        'complete': True,
                    })
            # Only implicit single-category sampling retries. Explicit category
            # requests and all-category/full-product jobs retain their exact scope.
            elif not category and not all_categories:
                values = None
                last_retryable_error = None
                for key, url in sample_candidates:
                    selected = {key: url}
                    try:
                        value = collect_listing(client, url)
                    except AdapterRequired:
                        # A parser/adapter shape is shared by the storefront;
                        # retrying a dozen sibling categories only repeats the
                        # same structural failure and delays controlled repair.
                        write_sample_scope('adapter_required')
                        raise
                    except AccessChallenge:
                        # Access challenges are domain/session failures, not a
                        # property of an individual category.  Preserve the
                        # dedicated signal so the workbench can pause and offer
                        # manual browser confirmation instead of exhausting all
                        # sample candidates and reporting a generic failure.
                        write_sample_scope('needs_access_confirm')
                        raise
                    except CrawlError as error:
                        last_retryable_error = error
                        selection_attempts.append({'category': key, 'url': url,
                                                   'distinct_products': 0,
                                                   'status': 'unverifiable_listing',
                                                   'error': str(error)})
                        write_sample_scope('retrying')
                        continue
                    distinct = len(dict.fromkeys(value.get('urls', [])))
                    attempt = {'category': key, 'url': url,
                               'distinct_products': distinct}
                    if distinct < 3:
                        attempt['status'] = 'insufficient'
                        selection_attempts.append(attempt)
                        write_sample_scope('retrying')
                        continue

                    # A collection can have enough cards while its first products
                    # are still impossible to verify completely (for example a
                    # Shopify endpoint truncated at 250 variants).  In implicit
                    # sample mode this is a property of the sample candidate, not
                    # proof that the whole store failed.  Verify before selecting
                    # it and try the next deepest leaf without weakening product
                    # integrity.  Explicit/all-category scopes never enter here.
                    candidate_urls = list(dict.fromkeys(value['urls']))[:3]
                    candidate_products = {p['handle']: p for p in value['products']}
                    from .shopify_fastpath import handle
                    try:
                        with ThreadPoolExecutor(max_workers=3) as pool:
                            candidate_samples = list(pool.map(
                                lambda u: fetch_product(client, u, candidate_products.get(handle(u))),
                                candidate_urls))
                    except CrawlError as error:
                        if not retryable_sample_verification_error(error):
                            raise
                        last_retryable_error = error
                        attempt.update({'status': 'unverifiable_samples', 'error': str(error)})
                        selection_attempts.append(attempt)
                        write_sample_scope('retrying')
                        continue
                    attempt['status'] = 'selected'
                    selection_attempts.append(attempt)
                    values, samples = [value], candidate_samples
                    write_sample_scope('selected')
                    break
                if values is None:
                    write_sample_scope('failed')
                    message = '已按深度和菜单顺序尝试所有有界候选分类，均不足三个不同商品或无法完整核验'
                    if last_retryable_error is not None:
                        message += ': ' + str(last_retryable_error)
                    raise CrawlError(message)
            else:
                unique = list(dict.fromkeys(selected.values()))
                skipped_landing_warnings = []
                if full_products and all_categories:
                    unique, values, skipped_landing_warnings = collect_full_shopify_listings(
                        client, selected, root / 'evidence' / 'listing-pages',
                        skip_non_collection_landings=(detection['mode'] == 'standard_shopify'),
                        progress=lambda done, total, url, status: event(
                            'listing', f'全量分类进度 {done}/{total}',
                            progress=48 + int(12 * done / max(1, total)),
                            current_url=url, collection_status=status))
                    selected = {key: url for key, url in selected.items() if url in set(unique)}
                else:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        # Preserve the historical adapter call contract for
                        # explicit-category and bounded sample runs.
                        values = list(pool.map(lambda u: collect_listing(client, u), unique))
            unique = list(dict.fromkeys(selected.values()))
            by_url = dict(zip(unique, values))
            if practical:
                from .practical_navigation import warn
                for value in values:
                    for warning in value.get('warnings',[]):warn(root,warning['code'],warning['message'],url=warning.get('url'))
            elif simple:
                runtime_warnings = read_json(root/'warnings.json') if (root/'warnings.json').is_file() else []
                runtime_warnings.extend(skipped_landing_warnings if 'skipped_landing_warnings' in locals() else [])
                fingerprints = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in runtime_warnings}
                for value in values:
                    for warning in value.get('warnings', []):
                        marker = json.dumps(warning, ensure_ascii=False, sort_keys=True)
                        if marker not in fingerprints:
                            runtime_warnings.append(warning);fingerprints.add(marker)
                save_json(root/'warnings.json', runtime_warnings)
            for key, u in selected.items():
                listings[key] = (compact_shopify_listing_evidence(by_url[u])
                                 if full_products and all_categories else by_url[u])
                detail[key] = by_url[u]['urls']
            save_json(root / 'listings.json', listings)
            save_json(root / 'detail_url.json', detail)
            from .navigation_release import digest
            save_json(root / 'detail_release.json', {'navigation_version': release['navigation_version'],
                                                    'sha256': digest(root / 'detail_url.json')})
            urls = list(dict.fromkeys(u for links in detail.values() for u in links))[:3]
            if not urls or (not full_products and len(urls) != 3
                            and not direct_product_scope):
                raise CrawlError('所选分类不足三个不同商品，请选更大的分类')
            if samples is None:
                by_handle = {p['handle']: p for listing in values for p in listing['products']}
                from .shopify_fastpath import handle
                with ThreadPoolExecutor(max_workers=3) as pool:
                    samples = list(pool.map(lambda u: fetch_product(client, u, by_handle.get(handle(u))), urls))
            for sample in samples:
                source_html = sample.pop('source_html', None)
                if source_html is not None:
                    evidence_file = ('evidence/hydrogen-product-'
                                     + str(sample['product']['id']) + '.html')
                    (root / evidence_file).write_text(source_html, encoding='utf-8')
                    sample['source_mapping'] = {
                        'kind': 'hydrogen_devalue', 'file': evidence_file,
                        'handle': sample['product']['handle']}
                # Persist unmodified product response separately from normalized rows.
                save_json(root / 'evidence' / f'product-{sample["product"]["id"]}.json', sample['product'])
            save_json(root / 'samples.json', samples)
            export(samples, detail, root)
            merge_explicit_colorways(client, samples, detail, root)
            return samples
        deterministic_custom = (detection['mode'] == 'custom_site'
                                and detection.get('custom_platform') in SUPPORTED_PLATFORMS
                                and not force_ai)
        if deterministic_custom:
            if full_products and custom_platform == 'demandware':
                raise CrawlError('Demandware 适配器当前只验证 3 个样本，未开启全量')
            from .custom_site import run_custom_sample
            event('listing', f'{custom_platform} 按前台分类顺序采集商品')
            try:
                selected, detail, listings, samples, custom_attempts = run_custom_sample(
                    client, root, custom_platform, matching.items() if all_categories else sample_candidates,
                    all_categories=all_categories, full_products=full_products)
            except Exception as error:
                from .ai_repair import is_repairable
                if not is_repairable(error):
                    raise
                samples = repair_sample(error)
                # The controlled repair contract produces verified sample and
                # detail artifacts; materialize the ordinary delivery CSVs just
                # as the deterministic standard path would.
                export(samples, read_json(root / 'detail_url.json'), root)
                detail, listings = read_json(root / 'detail_url.json'), read_json(root / 'listings.json')
                custom_attempts = [{'status': 'ai_repaired', 'reason': str(error)}]
            selection_attempts.extend(custom_attempts)
            write_sample_scope('selected')
            from .navigation_release import digest
            save_json(root / 'detail_release.json', {
                'navigation_version': release['navigation_version'],
                'sha256': digest(root / 'detail_url.json')})
            export(samples, detail, root)
        elif (detection['mode'] == 'custom_site' and not detection.get('custom_platform')
              and not direct_product_scope and not force_ai):
            from .custom_navigation import detect_catalog_only
            catalog = detect_catalog_only(client, base, categories, root / 'evidence')
            save_json(root / 'catalog_only.json', catalog)
            if catalog.get('catalog_only'):
                listing_url = catalog.get('listing_url')
                listing_urls = catalog.get('listing_urls') or ([listing_url] if listing_url else [])
                allowed_listing_urls = {str(value).rstrip('/') for value in listing_urls}
                verified = ({key: value for key, value in categories.items()
                             if value.rstrip('/') in allowed_listing_urls}
                            if allowed_listing_urls else {})
                if verified and len(verified) < len(categories):
                    from .simple_nav import publish_simple
                    method = read_json(root / 'navigation_report.json').get('method', 'simple_nav')
                    release = publish_simple(
                        root, verified,
                        warnings=[{'code': 'pruned_content_navigation',
                                   'removed': len(categories) - len(verified)}],
                        method=method, scope='verified_product_listing_only')
                    categories = verified
                event('catalog_only', '商品页没有公开价格或购买变体，已剔除内容导航并停止商品 AI',
                      categories=categories)
                raise CrawlError(
                    '目录型品牌站没有公开价格或购买变体；已保存 catalog_only.json，'
                    '不启动商品 AI，避免编造价格')
            samples = repair_sample(AdapterRequired(detection['mode']))
        elif (detection['mode'] not in {'standard_shopify', 'hydrogen_shopify'}
              and not direct_product_scope) or force_ai:
            samples = repair_sample(AdapterRequired('explicit custom handling' if force_ai else detection['mode']))
        else:
            if not detection['market']['confirmed']:
                raise CrawlError('标准 Shopify 首页未确认 US/USD')
            try:
                samples = standard_run()
            except Exception as error:
                # Shopify/Hydrogen structural drift needs the same bounded,
                # evidence-backed repair loop as custom storefronts.  The old
                # ai_worker fallback did not receive the failure traceback,
                # cached responses or SPA diagnosis, so it repeatedly explored
                # the repository and timed out.  Integrity/access failures stay
                # fail-closed through is_repairable().
                from .ai_repair import is_repairable
                if not is_repairable(error):
                    raise
                samples = repair_sample(error)
                export(samples, read_json(root / 'detail_url.json'), root)
        if (full_products
                and (detection['mode'] in {'standard_shopify', 'hydrogen_shopify'}
                     or direct_product_scope)
                and not force_ai):
            from .shopify_fastpath import run_products
            event('products', '目录和列表已通过，自动采集全部去重商品；保存可续跑断点')
            result = run_products(root, all_products=True)
            samples = read_json(root / 'samples.json')
        else:
            result = validate(root)
        preview = [{'id': str(s['product']['id']), 'title': s['product']['title'], 'url': s['url'],
                    'variants': len(s['product']['variants'])} for s in samples[:3]]
        event('preview', '目录与全部商品已通过校验' if full_products else '目录与前 3 个商品样本已通过结果校验', samples=preview, validation=result)
        if manual and not practical and not simple:
            if confirm is None:
                raise CrawlError('手动模式需要确认事件')
            event('awaiting_confirmation', '请在网页确认范围，确认后继续生成交付包', samples=preview)
            # Manual pause occurs after exactly three samples, before delivery/any further work.
            if not confirm.wait():
                raise CrawlError('确认等待被取消')
        event('packaging', '直接打包当前验证产物，不建立 venv、不重采样本')
        report = {'status': 'completed', 'site': base, 'mode': detection['mode'], 'manual': manual,
                  'proxy': redact_proxy(proxy),
                  'detail_scope': 'all_products' if full_products else 'first_3_products',
                  'navigation_version': release['navigation_version'],
                  'seconds': round(time.perf_counter() - start, 3), 'nav_seconds': round(nav_seconds, 4),
                  'validation': result, 'samples': preview, 'llm_invocations': int((root / 'ai_invocation.json').exists()) + int((root / 'navigation_invocation.json').exists())}
        report['warnings']=read_json(root/'warnings.json') if (root/'warnings.json').exists() else []
        repair_path = root / 'ai_repair.json'
        if repair_path.is_file():
            repair = read_json(repair_path)
            report['ai_repair'] = repair
        report['selected_sample_categories']=list(selected)
        report['sample_category_attempts']=selection_attempts
        report['navigation_validation_scope']=release.get('validation_scope','all_targets')
        if (root/'navigation_ai.log').exists() and (root/'navigation_invocation.json').exists():
            report['directory_cli_seconds']=round((root/'navigation_ai.log').stat().st_mtime-(root/'navigation_invocation.json').stat().st_mtime,3)
            report['reused_directory']=reuse_navigation
            report['seconds_scope']='artifact_resume' if reuse_navigation else 'whole_job'
        if (root/'navigation_timing.json').exists():
            navigation_timing=read_json(root/'navigation_timing.json')
            report['directory_cli_seconds']=navigation_timing['cli_seconds']
            report['navigation_mode']=navigation_timing.get('mode','hybrid_scope_v2')
            report['directory_reasoning_effort']=navigation_timing.get('reasoning_effort')
            report['directory_model']=navigation_timing.get('model')
            report['llm_invocations']=int((root/'ai_invocation.json').exists())+int(
                navigation_timing.get('llm_invocations',int((root/'navigation_invocation.json').exists())))
            if (root/'navigation_preflight.json').exists():
                report['program_navigation_seconds']=read_json(root/'navigation_preflight.json')['seconds']
            if (root/'navigation_review_scope.json').exists():
                navigation_scope=read_json(root/'navigation_review_scope.json')
                report['program_confirmed_categories']=len(navigation_scope.get('confirmed_candidate',{}))
                report['ai_review_categories']=len(navigation_scope.get('uncertain_candidate',{}))
                report['ai_review_issues']=len(navigation_scope.get('issues',[]))
        if visual_check and not simple:
            visual=read_json(root/'visual_audit.json')
            report['visual_status']=visual.get('status')
            report['visual_seconds']=visual.get('seconds')
            report['visual_llm_invocations']=visual.get('llm_invocations',0)
            report['llm_invocations']+=report['visual_llm_invocations']
        listing_info = read_json(root / 'listings.json')
        if repair_path.is_file():
            report['llm_invocations'] += report['ai_repair'].get('invocations', 0)
        report['listing_categories'] = len(listing_info)
        report['listing_links'] = sum(len(v) for v in read_json(root / 'detail_url.json').values())
        first = next(iter(listing_info.values()))
        report['first3_seconds'] = first.get('first3_seconds')
        report['first3_cpu_seconds'] = first.get('first3_cpu_seconds')
        save_json(root / 'report.json', report)
        package(root, report, start)
        event('completed', '验证通过，独立运行包已生成', report=report)
        return report
    except TaskCancelled as e:
        cancelled = {'status': 'cancelled', 'error': str(e),
                     'seconds': round(time.perf_counter() - start, 3)}
        save_json(root / 'cancelled.json', cancelled)
        event('cancelled', str(e))
        raise
    except AccessChallenge as e:
        state = read_json(root / 'navigation_release.json')
        if state.get('status') != 'approved':
            save_json(root / 'navigation_release.json', {'version': 1, 'status': 'blocked',
                      'error': redact_sensitive(e, proxy)})
        failure = {'status': 'needs_access_confirm', 'stage': 'access',
                   'error': redact_sensitive(e, proxy), 'url': getattr(e, 'url', None) or site,
                   'exception_type': type(e).__name__,
                   'traceback': redact_sensitive(traceback.format_exc(), proxy)[-20_000:],
                   'proxy': redact_proxy(proxy),
                   'seconds': round(time.perf_counter() - start, 3)}
        save_json(root / 'failure.json', failure)
        event('needs_access_confirm', failure['error'], access_url=failure['url'])
        raise
    except Exception as e:
        state = read_json(root / 'navigation_release.json')
        if state.get('status') != 'approved':
            save_json(root / 'navigation_release.json', {'version': 1, 'status': 'blocked', 'error': redact_sensitive(e, proxy)})
        message = redact_sensitive(e, proxy) or type(e).__name__
        failure = {'status': 'failed', 'error': message,
                   'exception_type': type(e).__name__,
                   'traceback': redact_sensitive(traceback.format_exc(), proxy)[-20_000:],
                   'proxy': redact_proxy(proxy), 'seconds': round(time.perf_counter() - start, 3)}
        save_json(root / 'failure.json', failure)
        event('failed', message)
        raise
    finally:
        if 'client' in locals():
            clear_cache = getattr(client, 'clear_cache', None)
            if callable(clear_cache):
                clear_cache()
            elif hasattr(client, 'cache'):
                client.cache.clear()
        gc.collect()
