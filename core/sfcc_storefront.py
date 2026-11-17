"""SFRA public navigation, ordered grids and server-declared variant trees."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import nullcontext
from html import unescape
from pathlib import Path
import json
import re
import time
from decimal import Decimal, InvalidOperation
from bs4 import SoupStrainer
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .common import AdapterRequired, CrawlError, query, read_json, save_json
from .html_parser import parse_soup


def rendered_navigation(base, evidence_dir, timeout=30):
    """Expand accessible desktop disclosures for JS-shell SFCC stores.

    This is evidence acquisition, not structure guessing: React supplies the
    exact labels, owned aria panels and hrefs after hydration, then
    :func:`navigation` parses the saved HTML offline.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    documents = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                          locale='en-US', timezone_id='America/New_York')
            page = context.new_page()
            try:
                page.goto(base, wait_until='domcontentloaded', timeout=timeout * 1000)
            except BrowserTimeout:
                if not page.locator('nav:visible,[role="navigation"]:visible').count():
                    raise
            page.wait_for_function("document.readyState !== 'loading'", timeout=12000)
            page.wait_for_timeout(1200)
            # Some React headers expose navigation semantics on surrounding
            # lists rather than a literal <nav>; the disclosure contract and
            # top-of-page position identify the desktop controls reliably.
            controls = page.locator('[role="button"][aria-controls]')
            labels = []
            for index in range(controls.count()):
                control = controls.nth(index)
                text = ' '.join(control.inner_text().split())
                box = control.bounding_box()
                if (re.fullmatch(r'Expand\s+.+', text, re.I)
                        and box and box['y'] < 350
                        and not re.search(r'sign\s*in|bag|cart|account|search', text, re.I)):
                    labels.append(text)
            for index, text in enumerate(dict.fromkeys(labels)):
                control = page.get_by_text(text, exact=True).last
                control.evaluate('element => element.click()')
                page.wait_for_timeout(350)
                html = page.content()
                safe = re.sub(r'[^a-z0-9]+', '-', text.casefold()).strip('-')[:48]
                (evidence_dir / f'home-menu-{index:03d}-{safe}.html').write_text(
                    html, encoding='utf-8')
                try:
                    page.screenshot(path=str(evidence_dir / f'home-menu-{index:03d}-{safe}.png'),
                                    full_page=False, animations='disabled', timeout=3000)
                except BrowserTimeout:
                    pass
                documents.append(html)
        finally:
            browser.close()
    return documents


def supports(url, html=None):
    if html is not None:
        return bool(re.search(r'class=["\'][^"\']*(?:product-variations|product-tile)', html)
                    and ('/on/demandware.' in html or '/show-variation/' in html))
    return (urlsplit(url).hostname or '').removeprefix('www.') in {
        'lecreuset.com', 'shop.samsonite.com', 'cottonon.com'}


def _text(node):
    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''


def _key(labels):
    labels = [' '.join(label.replace(',', ' ').split()) for label in labels]
    return ','.join(' '.join(labels[:i + 1]) for i in range(len(labels)))


def _local(value, base):
    target = urljoin(base, unescape(value or ''))
    parts = urlsplit(target)
    if (not value or value.startswith('#') or parts.scheme not in {'http', 'https'}
            or parts.hostname != urlsplit(base).hostname):
        return ''
    return target


def navigation(html, base, client=None):
    """Use the storefront's explicit menu levels and column headings."""
    doc = parse_soup(html)
    for node in doc.select('script,style,.d-lg-none,nav.hide-for-xlarge,nav.hide-for-large,nav.show-for-small,nav.show-for-medium-only'):
        node.decompose()
    result = {}
    featured_entries = set()

    def add(labels, anchor):
        target = _local(anchor.get('href', ''), base) if anchor else ''
        excluded = (r'e?gifts?[-_\s]?cards?|/(?:account|cart|search|login)(?:/|\.|$)'
                    r'|/expert-advice/|/(?:beautypedia(?:-|/|$)|ingredient-dictionary|about-beautypedia)'
                    r'|/\d+\.html(?:\?|$)')
        if (target and all(labels) and not re.search(excluded, target, re.I)
                and not re.fullmatch(r'e?gifts?[-\s]?cards?', labels[-1], re.I)):
            key = _key(labels)
            featured = bool(anchor.find_parent(class_='feature-cat-container'))
            if key in result and result[key] != target and (featured or key in featured_entries):
                feature_key = _key([*labels[:-1], labels[-1] + ' (Featured)'])
                if featured:
                    result.setdefault(feature_key, target)
                else:
                    result.setdefault(feature_key, result[key])
                    result[key] = target
                    featured_entries.discard(key)
            else:
                result.setdefault(key, target)
                if featured:
                    featured_entries.add(key)

    # React storefronts can keep the SFCC backend while replacing the usual
    # SiteGenesis/SFRA class names.  Their accessible disclosure contract is
    # more stable than generated CSS-in-JS classes: a top-level ``Expand X``
    # control owns exactly one panel through aria-controls.  Read only that
    # owned panel, never the footer or the rest of the hydrated page.
    disclosure_controls = doc.select('[role="button"][aria-controls]')
    expanded_controls = [node for node in disclosure_controls
                         if str(node.get('aria-expanded', '')).casefold() == 'true']
    seen_react_panels = set()
    for control in expanded_controls or disclosure_controls:
        control_text = _text(control)
        match = re.fullmatch(r'(?:expand|open)\s+(.+)', control_text, re.I)
        if not match:
            continue
        root_label = match.group(1).strip()
        panel_id = control.get('aria-controls', '').strip()
        owner_scope = control.find_parent('li')
        panel = owner_scope.find(id=panel_id) if owner_scope else None
        if panel is None:
            panel = doc.find(id=panel_id)
        if not root_label or panel is None or id(panel) in seen_react_panels:
            continue
        seen_react_panels.add(id(panel))
        panel_title = panel.select_one('[class*="__FlyoutTitle-sc"],h1,h2,[role="heading"]')
        root_label = _text(panel_title) or root_label
        anchors = panel.select('a[href]')
        aggregator = next((a for a in anchors if re.fullmatch(
            r'(?:shop|view|see)\s+all(?:\s+.+)?', _text(a), re.I)), None)
        if aggregator:
            add([root_label], aggregator)
        consumed = set()
        # A nested sub-item list gives an explicit level-2 owner and level-3
        # leaves. Generated class suffixes vary, so match semantic fragments.
        for owner in panel.select('li[class*="__SubItem-sc"]'):
            direct = owner.find('a', href=True, recursive=False)
            if direct is None:
                wrapper = owner.find(recursive=False)
                direct = wrapper.find('a', href=True) if wrapper else None
            if direct is None:
                continue
            second = _text(direct)
            if not second:
                continue
            add([root_label, second], direct)
            consumed.add(id(direct))
            for leaf in owner.select('ul a[href]'):
                third = _text(leaf)
                if (third and third.casefold() != second.casefold()
                        and not re.fullmatch(r'(?:shop|view|see)\s+all', third, re.I)):
                    add([root_label, second, third], leaf)
                    consumed.add(id(leaf))
        for anchor in anchors:
            if id(anchor) in consumed or anchor is aggregator:
                continue
            title = _text(anchor)
            if (title and title.casefold() != root_label.casefold()
                    and not re.fullmatch(r'(?:shop|view|see)\s+all(?:\s+.+)?',
                                            title, re.I)):
                add([root_label, title], anchor)

    # SiteGenesis themes expose the root controls and their nested column
    # lists directly. The nearest list item owns links inside its spans.
    for root in doc.select('nav .top-level-container > li'):
        trigger = root.find(['a', 'button'], recursive=False)
        label = _text(trigger)
        if not label:
            continue
        add([label], trigger)
        for anchor in root.select('a[href]'):
            if anchor is trigger:
                continue
            title = _text(anchor)
            if not title:
                continue
            owners = []
            for owner in anchor.parents:
                if owner is root:
                    break
                if owner.name == 'li':
                    heading = owner.find(['a', 'button'], recursive=False)
                    if heading is not anchor and _text(heading):
                        owners.append(_text(heading))
            labels = [label] + list(reversed(owners))
            if not re.fullmatch(r'(?:shop|view|see)\s+all', title, re.I):
                labels.append(title)
            if len(labels) > 3:
                labels = labels[:2] + [' '.join(labels[2:])]
            add(labels, anchor)

    for root in doc.select('nav .menu-level-1 > li'):
        anchor = root.find('a', recursive=False)
        label = _text(anchor)
        if not anchor or not label:
            continue
        add([label], anchor)
        for child in root.select('.menu-level-2 > li > a, a[data-link-type="category"]'):
            title = _text(child)
            if title and not re.fullmatch(r'(?:shop|view)\s+all', title, re.I):
                if _local(child.get('href', ''), base) != _local(anchor.get('href', ''), base):
                    add([label, title], child)
    for root in doc.select('nav .navbar-nav > li'):
        trigger = root.find(['button', 'a'], recursive=False)
        label = _text(trigger)
        columns = root.select('.dropdown-menu h2')
        if not columns:
            # An explicit sale root is a catalog entry; editorial roots are not.
            if trigger and trigger.name == 'a' and re.fullmatch(r'specials|sale', label, re.I):
                add([label], trigger)
            continue
        for heading in columns:
            title = _text(heading)
            add([label, title], heading.find('a', href=True))
            owner = heading.parent
            for anchor in owner.select('.dropdown-submenu a[href]'):
                child = _text(anchor)
                if child and not re.fullmatch(r'(?:shop|view)\s+all', child, re.I):
                    add([label, title, child], anchor)
    main = doc.find('main') or doc.select_one('#maincontent, #main')
    if main and client is not None:
        candidates = []
        for anchor in main.select('a[href]'):
            target = _local(anchor['href'], base)
            if not target or target in result.values() or anchor.find_parent(class_='product-tile'):
                continue
            path = urlsplit(target).path
            if (re.search(r'/(?:on/|blog|about-|explore-|account|cart|search)|e?gifts?[-_]?cards?', path, re.I)
                    or re.search(r'/[^/]*\d[^/]*\.html$', path) or path == '/'):
                continue
            slug = path.rstrip('/').rsplit('/', 1)[-1].removesuffix('.html')
            if slug and (slug, target) not in candidates:
                candidates.append((slug, target))

        def confirmed_category(candidate):
            slug, target = candidate
            try:
                response = client.get(target, timeout=20)
                page = parse_soup(response.text)
                grid = page.select_one('.product-grid, #search-result-items')
                if grid and grid.select_one('.product-tile'):
                    return slug, target
            except CrawlError:
                return None
            return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            for confirmed in pool.map(confirmed_category, candidates):
                if confirmed:
                    result.setdefault(*confirmed)
    return result


def _grid(html, base):
    doc = parse_soup(html)
    grid = doc.select_one('.product-grid, #search-result-items') or doc
    entries = []
    for tile in grid.select('.product-tile'):
        anchor = tile.select_one('.pdp-link a[href], a.product-tile-image-link[href], a.name-link[href]')
        if anchor is None:
            continue
        target = _local(anchor['href'], base)
        pid = tile.get('data-pid') or tile.get('data-itemid') or urlsplit(target).path.rsplit('/', 1)[-1]
        if target and pid and all(entry['id'] != pid for entry in entries):
            entries.append({'id': pid, 'url': target})
    more = doc.select_one('button.more[data-url]')
    following = doc.select_one('[rel="next"][href]')
    next_url = more['data-url'] if more else following['href'] if following else ''
    return entries, _local(next_url, base)


def is_client_rendered_shell(html):
    """Identify a hydrated SFCC shell without mistaking a normal PLP for CSR."""
    html = html or ''
    anchors = len(re.findall(r'<a\b', html, re.I))
    product_anchors = len(re.findall(
        r'<a\b[^>]+href=["\'][^"\']*(?:/product|/p/|/[^/]+/\d+\.html)', html, re.I))
    loader = bool(re.search(
        r'<(?:div|main|section)[^>]+(?:class|id)=["\'][^"\']*'
        r'(?:loader|loading|app-root|__next|root)[^"\']*["\']', html, re.I))
    framework = bool(re.search(
        r'__NEXT_DATA__|data-reactroot|react-dom|webpackJsonp|/_next/static/|'
        r'<script[^>]+type=["\']module["\']', html, re.I))
    document = parse_soup(html)
    for node in document.select('script,style,svg,noscript,template'):
        node.decompose()
    visible = ' '.join(document.get_text(' ', strip=True).split())
    sparse_dom = anchors < 3 and len(visible) < 1200
    script_heavy_shell = len(html.encode('utf-8', errors='replace')) > 50_000 and sparse_dom
    return product_anchors < 3 and ((loader and sparse_dom) or (framework and script_heavy_shell))


def _react_market(payload, page_type):
    layer = payload.get('dataLayer') if isinstance(payload, dict) else None
    layer = layer if isinstance(layer, dict) else {}
    if (layer.get('country_code') != 'US' or layer.get('language_code') != 'en_US'
            or layer.get('currency') != 'USD' or layer.get('ecom_page_type') != page_type):
        raise CrawlError('SFCC React JSON 不是预期的 en_US/USD 页面')
    return layer


def _react_listing_page(payload, base):
    layer = _react_market(payload, 'searchresults')
    paging = payload.get('paging') or {}
    products = payload.get('products')
    if (not isinstance(products, list) or not isinstance(paging, dict)
            or not all(isinstance(paging.get(key), int) for key in ('start', 'size', 'total'))):
        raise AdapterRequired('SFCC React 分类 JSON 缺少 products/paging')
    entries = []
    for index, product in enumerate(products):
        if not isinstance(product, dict):
            raise CrawlError('SFCC React 分类商品不是对象')
        target = _local(product.get('url', ''), base)
        price = ((product.get('price') or {}).get('current') or {}).get('value')
        if (not target or not product.get('id') or not product.get('masterId')
                or not product.get('name') or not product.get('image') or price is None):
            raise CrawlError('SFCC React 分类商品缺少身份、URL、图片或价格')
        entries.append({'id': str(product['masterId']), 'url': target,
                        'index': index, 'product': product})
    if paging['total'] < paging['start'] + len(products):
        raise CrawlError('SFCC React 分类分页总数小于已返回范围')
    return entries, paging, layer


def collect_react_listing(client, url, response=None):
    """Collect a modern React SFCC PLP from its public ``customer=true`` JSON."""
    started = time.perf_counter()
    response = response or client.get(url, timeout=45)
    endpoint = query(response.url, customer='true')
    feeds, entries, seen, source_responses = [], [], set(), []
    next_start = 0
    while endpoint:
        payload = client.json(endpoint)
        page_entries, paging, _ = _react_listing_page(payload, response.url)
        if paging['start'] != next_start:
            raise CrawlError('SFCC React 分类分页起点不连续')
        fresh = [entry for entry in page_entries if entry['url'] not in seen]
        if page_entries and not fresh:
            raise CrawlError('SFCC React 分类分页没有新增商品')
        entries.extend(fresh)
        seen.update(entry['url'] for entry in fresh)
        feeds.append(payload)
        source_responses.append({'url': endpoint, 'response': payload})
        consumed = paging['start'] + len(page_entries)
        if consumed >= paging['total']:
            endpoint = ''
        else:
            if not page_entries or paging['size'] <= 0:
                raise CrawlError('SFCC React 分类分页无法前进')
            next_start = consumed
            endpoint = query(response.url, customer='true', start=next_start,
                             sz=paging['size'])
    if len(entries) != (feeds[-1]['paging']['total'] if feeds else 0):
        raise CrawlError('SFCC React 分类没有完整采集 declared total')
    cards = getattr(client, '_sfcc_react_cards', None)
    if not isinstance(cards, dict):
        cards = client._sfcc_react_cards = {}
    for entry in entries:
        cards[entry['url']] = entry['product']
    urls = [entry['url'] for entry in entries]
    return {'url': response.url, 'format': 'sfcc_storefront',
            'source_format': 'react_customer_json', 'complete': True,
            'urls': urls, 'catalog_urls': urls, 'products': [], 'feeds': feeds,
            'pages': [], 'source_responses': source_responses,
            'source_mapping': {
                'kind': 'sfcc_storefront', 'products_path': '/products',
                'paging_path': '/paging', 'country_path': '/dataLayer/country_code',
                'currency_path': '/dataLayer/currency',
                'language_path': '/dataLayer/language_code'},
            'first3_seconds': round(time.perf_counter() - started, 4)}


def seed_react_cards(client, listings):
    cards = getattr(client, '_sfcc_react_cards', None)
    if not isinstance(cards, dict):
        cards = client._sfcc_react_cards = {}
    for listing in listings.values():
        if listing.get('source_format') != 'react_customer_json':
            continue
        for payload in listing.get('feeds', []):
            for entry in payload.get('products', []):
                target = _local(entry.get('url', ''), listing['url'])
                if target:
                    cards[target] = entry


def collect_listing(client, url):
    started = time.perf_counter()
    pages, entries, seen, visited = [], [], set(), set()
    next_url = url
    while next_url:
        if next_url in visited:
            raise CrawlError('SFCC 商品分页循环')
        visited.add(next_url)
        response = client.get(next_url, timeout=45)
        found, following = _grid(response.text, response.url)
        if not pages and len(found) < 3 and is_client_rendered_shell(response.text):
            return collect_react_listing(client, response.url, response)
        pages.append({'url': response.url, 'html': response.text})
        fresh = [entry for entry in found if entry['id'] not in seen]
        if following and not fresh:
            raise CrawlError('SFCC 商品分页没有新增商品')
        entries.extend(fresh)
        seen.update(entry['id'] for entry in fresh)
        next_url = following
    urls = [entry['url'] for entry in entries]
    return {'url': pages[0]['url'], 'format': 'sfcc_storefront', 'complete': True,
            'urls': urls, 'catalog_urls': urls, 'products': [], 'feeds': [],
            'pages': pages, 'first3_seconds': round(time.perf_counter() - started, 4)}


def validate_listing(listing, links):
    if listing.get('source_format') == 'react_customer_json':
        observed, expected_start, declared_total = [], 0, None
        for payload in listing.get('feeds', []):
            entries, paging, _ = _react_listing_page(payload, listing['url'])
            if paging['start'] != expected_start:
                raise CrawlError('SFCC React 保存的分页起点不连续')
            if declared_total is None:
                declared_total = paging['total']
            elif declared_total != paging['total']:
                raise CrawlError('SFCC React 分类分页总数发生变化')
            observed.extend(entry['url'] for entry in entries)
            expected_start += len(entries)
        if (declared_total != len(observed) or observed != links
                or listing.get('urls') != links or not listing.get('complete')):
            raise CrawlError('SFCC React 分类顺序、分页或总数与原始 JSON 不一致')
        return
    observed, ids = [], set()
    pages = listing.get('pages', [])
    if not pages or not listing.get('complete'):
        raise CrawlError('SFCC 商品列表缺少完整分页证据')
    for i, page in enumerate(pages):
        found, following = _grid(page['html'], page['url'])
        expected_next = pages[i + 1]['url'] if i + 1 < len(pages) else ''
        if following != expected_next:
            raise CrawlError('SFCC 分页链缺失或终页尚有下一页')
        for entry in found:
            if entry['id'] not in ids:
                observed.append(entry['url'])
                ids.add(entry['id'])
    if observed != links or listing.get('urls') != links:
        raise CrawlError('SFCC 商品顺序与原始网格不一致')


def _root_endpoint(html, base):
    pattern = r'(?:https?://[^"\s<]+)?/on/demandware\.store/[^"\s<]+/Product-Variation\?[^"\s<]+'
    for match in re.findall(pattern, html, re.I):
        endpoint = _local(match.rstrip("'"), base)
        if not endpoint:
            continue
        parts = urlsplit(endpoint)
        # Ask the published endpoint for the whole master. A selected PDP
        # size would otherwise conceal colors unavailable in that one size.
        declared = parse_qsl(parts.query, keep_blank_values=True)
        first_option = next((k for k, _ in declared if k.startswith('dwvar_')), None)
        params = [(k, '' if k == first_option else v) for k, v in declared
                  if not k.startswith('dwvar_') or k == first_option]
        if any(k == 'pid' and v for k, v in params):
            return urlunsplit(parts._replace(query=urlencode(params)))
    raise CrawlError('SFCC 商品缺少公开 Product-Variation 接口')


def _variation_tree(root_endpoint, responses, fetch=None):
    first = responses[root_endpoint]['product']
    order = [a['id'] for a in first.get('variationAttributes', [])]
    if len(order) > 3:
        raise CrawlError('SFCC 商品超过三个变体维度')
    states = [root_endpoint]
    for attribute_id in order:
        following = []
        for endpoint in states:
            attributes = responses[endpoint]['product'].get('variationAttributes', [])
            attribute = next((a for a in attributes if a['id'] == attribute_id), None)
            if attribute is None:
                raise CrawlError('SFCC 变体响应缺少已声明属性')
            branches = []
            for value in attribute.get('values', []):
                if value.get('hide') or value.get('hasVariants') is False:
                    continue
                if value.get('selected'):
                    branches.append(endpoint)
                elif value.get('url'):
                    target = _local(value['url'], root_endpoint)
                    if target and '/Product-Variation?' in target:
                        branches.append(target)
            if not branches:
                raise CrawlError('SFCC 变体属性没有可验证的分支')
            following.extend(branches)
        states = list(dict.fromkeys(following))
        missing = [endpoint for endpoint in states if endpoint not in responses]
        if missing and fetch is None:
            raise CrawlError('SFCC 变体源响应缺失，无法证明完整性')
        if missing:
            with ThreadPoolExecutor(max_workers=4) as pool:
                for endpoint, response in zip(missing, pool.map(fetch, missing)):
                    responses[endpoint] = response
    leaves = {}
    for endpoint in states:
        product = responses[endpoint].get('product', {})
        if (product.get('productType') not in {'variant', 'standard'}
                or not product.get('id') or not product.get('price', {}).get('sales')):
            raise CrawlError('SFCC 未取得真实 SKU 的独立报价')
        leaves.setdefault(str(product['id']), endpoint)
    return order, list(leaves.values())


def _gallery(product, base):
    groups = product.get('images') or {}
    for name in ('hi-res', 'large', 'medium', 'small'):
        items = groups.get(name)
        if isinstance(items, list) and items:
            return list(dict.fromkeys(urljoin(base, item.get('absURL') or item.get('httpsURL')
                                              or item.get('url', '')) for item in items))
    return []


def _html_product(html, base):
    """Read SiteGenesis product state from its own PDP controls and data."""
    doc = parse_soup(html, parse_only=SoupStrainer(id='pdpMain'))
    content = doc.select_one('#product-content') or doc.select_one('#pdpMain')
    tag = content.find('input', class_='ga4-gtm-product-data', recursive=False) if content else None
    if tag is None:
        raise CrawlError('SFCC HTML 商品缺少自身的商品数据')
    try:
        metadata = json.loads(tag['data-gtag'])
    except (ValueError, TypeError) as error:
        raise CrawlError('SFCC HTML 商品数据格式无效') from error
    if not metadata.get('item_id') or not metadata.get('item_name'):
        raise CrawlError('SFCC HTML 商品缺少父商品身份')
    if (not re.search(r'CQuotient\.locale\s*=\s*["\']en_US["\']', html)
            or not re.search(r'"ecommerce"\s*:\s*\{\s*"currency"\s*:\s*"USD"', html)):
        raise CrawlError('SFCC HTML 商品不是 en_US/USD 市场')

    def amount(selector):
        node = doc.select_one(selector)
        if node is None:
            return None
        value = re.sub(r'[^\d.]', '', node.get_text('', strip=True))
        try:
            return str(Decimal(value))
        except InvalidOperation as error:
            raise CrawlError('SFCC HTML 商品价格无法解析') from error

    price = amount('.product-price .price-sales')
    original = amount('.product-price .price-standard')
    price = price or original
    if price is None or Decimal(price) != Decimal(str(metadata.get('price'))):
        raise CrawlError('SFCC HTML 商品报价与本页商品数据不一致')
    gallery = []
    for anchor in doc.select('.product-thumbnails a.thumbnail-link[href]'):
        target = _local(anchor['href'], base)
        if target and target not in gallery:
            gallery.append(target)
    if not gallery:
        raise CrawlError('SFCC HTML 商品缺少自身图库')
    colors = []
    for node in doc.select('.swatches.color > li'):
        anchor = node.select_one('a[href]')
        target = _local(anchor['href'], base) if anchor else ''
        if target:
            colors.append({'url': target, 'selected': 'selected' in node.get('class', [])})
    sizes = []
    for node in doc.select('.swatches.online.size > li'):
        anchor = node.select_one('a')
        value = (anchor.get('data-size') or _text(node)) if anchor else ''
        if not value:
            continue
        sizes.append({'value': value,
                      'url': _local(anchor.get('href', ''), base),
                      'selected': 'selected' in node.get('class', []),
                      'available': 'unselectable' not in node.get('class', [])})
    pid = doc.select_one('input[name="pid"]')
    stock = doc.select_one('#rt_ats')
    from .shopify_fastpath import clean_description
    description = doc.select_one('#description-tab')
    return {'metadata': metadata, 'colors': colors, 'sizes': sizes,
            'sku': pid.get('value', '') if pid else '', 'price': price,
            'compare_at_price': original, 'images': gallery,
            'stock': stock.get('value', '') if stock else '',
            'description': clean_description(str(description)) if description else ''}


def _html_variation_tree(manifest, fetch=None):
    """Follow color links, then verify every size declared for that color."""
    root_url = manifest['url']
    responses = manifest['responses']
    root = _html_product(manifest['html'], root_url)
    states = [(color['url'], color['selected']) for color in root['colors']]
    if not states:
        states = [(root_url, True)]
    states = list(dict.fromkeys(states))

    def load(urls):
        missing = [url for url in dict.fromkeys(urls) if url not in responses]
        if missing and fetch is None:
            raise CrawlError('SFCC HTML 变体源响应缺失，无法证明完整性')
        if missing:
            with ThreadPoolExecutor(max_workers=4) as pool:
                for url, response in zip(missing, pool.map(fetch, missing)):
                    if not _local(response.url, root_url):
                        raise CrawlError('SFCC HTML 变体跳转到了其他站点')
                    responses[url] = {'url': response.url, 'html': response.text}

    load([url for url, selected in states if not selected])
    leaves = []
    for url, selected in states:
        color = root if selected else _html_product(responses[url]['html'], responses[url]['url'])
        if str(color['metadata']['item_id']) != str(root['metadata']['item_id']):
            raise CrawlError('SFCC 颜色分支跳转到了其他父商品')
        expected_color = next((value for key, value in parse_qsl(urlsplit(url).query)
                               if key.endswith('_color')), '')
        if expected_color and color['metadata'].get('item_variant') != expected_color:
            raise CrawlError('SFCC 颜色响应未选中请求的颜色')
        template = next((size['url'] for size in color['sizes']
                         if any(key.endswith('_size') for key, _ in parse_qsl(urlsplit(size['url']).query))), '')
        for size in color['sizes']:
            target = size['url']
            if size['selected']:
                target = root_url if selected else url
            elif not target:
                # Sold-out controls retain a real size value but omit href.
                # Reuse this color's declared size parameter and demand that
                # its response select that size and identify a real SKU.
                if not template:
                    raise CrawlError('SFCC 缺货尺码缺少可验证的选择参数')
                parts = urlsplit(template)
                params = [(key, size['value'] if key.endswith('_size') else value)
                          for key, value in parse_qsl(parts.query, keep_blank_values=True)]
                target = urlunsplit(parts._replace(query=urlencode(params)))
            leaves.append((target, color['metadata']['item_variant'], size['value']))
        if not color['sizes']:
            leaves.append((root_url if selected else url, color['metadata'].get('item_variant', ''), ''))
    load([url for url, _, _ in leaves if url != root_url])
    variants, images = {}, {}
    for url, color_id, size_value in leaves:
        record = root if url == root_url else _html_product(responses[url]['html'], responses[url]['url'])
        metadata = record['metadata']
        selected = [size for size in record['sizes'] if size['selected']]
        if (str(metadata['item_id']) != str(root['metadata']['item_id'])
                or metadata.get('item_variant', '') != color_id
                or (size_value and [item['value'] for item in selected] != [size_value])):
            raise CrawlError('SFCC HTML 变体响应未选中请求的真实组合')
        sku = record['sku']
        if not sku or (size_value and sku in {str(metadata['item_id']), color_id}):
            raise CrawlError('SFCC HTML 变体未返回独立 SKU')
        variant = {'id': sku, 'sku': sku, 'price': record['price'],
                   'compare_at_price': record['compare_at_price'],
                   'source_variant_url': url, 'featured_image': {'src': record['images'][0]},
                   'option1': metadata.get('color', '')}
        if size_value:
            variant['option2'] = size_value
            variant['available'] = selected[0]['available']
        if record['stock'] != '':
            quantity = Decimal(record['stock'])
            if quantity < 0 or quantity != quantity.to_integral_value():
                raise CrawlError('SFCC HTML 变体库存数量无效')
            variant['inventory_quantity'] = int(quantity)
        if sku in variants and variants[sku] != variant:
            raise CrawlError('SFCC HTML 相同 SKU 的属性或价格冲突')
        variants.setdefault(sku, variant)
        for image in record['images']:
            if sku not in images.setdefault(image, []):
                images[image].append(sku)
    if not variants:
        raise CrawlError('SFCC HTML 没有完整的真实变体')
    options = [{'name': 'Color'}]
    if any(size for _, _, size in leaves):
        options.append({'name': 'Size'})
    metadata = root['metadata']
    return {'id': str(metadata['item_id']), 'handle': str(metadata['item_id']),
            'title': metadata['item_name'], 'body_html': root['description'],
            'vendor': metadata.get('item_brand', ''), 'currency': 'USD',
            'options': options, 'variants': list(variants.values()),
            'images': [{'src': url, 'variant_ids': ids} for url, ids in images.items()]}


def _normalize(manifest):
    if manifest.get('format') == 'react_customer_json':
        return _react_customer_product(manifest)
    if manifest.get('format') == 'sitegenesis_html':
        if manifest.get('family_source'):
            source = manifest['family_source']
            if (source.get('format') != 'sitegenesis_html' or source.get('family_source')
                    or urlsplit(source['url']).hostname != urlsplit(manifest['url']).hostname):
                raise CrawlError('SFCC 同父色款缺少完整同站商品源')
            _verify_html_origin(manifest)
            _verify_html_origin(source)
            product = _html_variation_tree(source)
            _verify_html_family(_html_product(manifest['html'], manifest['url']), product)
            return product
        return _html_variation_tree(manifest)
    endpoint, responses = manifest['endpoint'], manifest['responses']
    order, leaves = _variation_tree(endpoint, responses)
    raw = responses[endpoint]['product']
    master = dict(parse_qsl(urlsplit(endpoint).query))['pid']
    variants, images = [], {}
    for leaf in leaves:
        record = responses[leaf]
        product = record['product']
        price = product['price']
        if record.get('locale') != 'en_US' or price['sales'].get('currency') != 'USD':
            raise CrawlError('SFCC 商品不是 en_US/USD 市场')
        selected = {}
        for attribute in product.get('variationAttributes', []):
            values = [v for v in attribute.get('values', []) if v.get('selected')]
            if len(values) != 1:
                raise CrawlError('SFCC 真实 SKU 未唯一选中每个属性')
            selected[attribute['id']] = values[0]['displayValue']
        pid = str(product['id'])
        gallery = _gallery(product, manifest['url'])
        if not gallery:
            raise CrawlError('SFCC SKU 缺少源商品图片')
        for image in gallery:
            images.setdefault(image, []).append(pid)
        variant = {'id': pid, 'sku': str(product.get('productNumber') or pid),
                   'price': price['sales']['value'],
                   'compare_at_price': (price.get('list') or {}).get('value'),
                   'available': bool(product.get('available')),
                   'inventory_quantity': product.get('availability', {}).get('quantityAvailableToSell', ''),
                   'featured_image': {'src': gallery[0]},
                   'source_variant_url': urljoin(manifest['url'], product.get('selectedProductUrl') or manifest['url'])}
        for i, name in enumerate(order, 1):
            variant['option' + str(i)] = selected[name]
        variants.append(variant)
    labels = {a['id']: a.get('displayName') or a['id'].title() for a in raw.get('variationAttributes', [])}
    return {'id': master, 'handle': master, 'title': raw['productName'],
            'body_html': raw.get('longDescription') or raw.get('shortDescription') or '',
            'vendor': raw.get('brand') or '', 'currency': 'USD',
            'options': [{'name': labels[name]} for name in order], 'variants': variants,
            'images': [{'src': src, 'variant_ids': ids} for src, ids in images.items()]}


def _react_customer_product(manifest):
    """Normalize the public React PDP plus its ``?customer=true`` response."""
    customer = manifest.get('customer') or {}
    layer = customer.get('dataLayer') or {}
    variants_raw = customer.get('variants') or []
    listing_product = manifest.get('listing_product') or {}
    doc = parse_soup(manifest.get('rendered_html', ''))
    canonical_node = doc.select_one('link[rel="canonical"][href]')
    canonical = _local(canonical_node.get('href', ''), manifest['url']) if canonical_node else ''
    if canonical:
        if urlsplit(canonical).path != urlsplit(manifest['url']).path:
            raise CrawlError('SFCC React 商品 canonical 与请求身份不一致')
    elif (not listing_product
          or _local(listing_product.get('url', ''), manifest['url']) != manifest['url']
          or str(listing_product.get('masterId', '')) != str(layer.get('product_master_id', ''))):
        raise CrawlError('SFCC React 商品缺少 canonical 或分类卡片身份依据')
    schemas = []
    for script in doc.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text())
            schemas.extend(value if isinstance(value, list) else [value])
        except (ValueError, TypeError):
            continue
    schema = next((value for value in schemas if isinstance(value, dict)
                   and value.get('@type') == 'Product'), {})
    master = str(layer.get('product_master_id') or schema.get('sku') or '')
    title = str(schema.get('name') or listing_product.get('name')
                or layer.get('product_short_name') or '').strip()
    if not master or not title or not variants_raw:
        raise CrawlError('SFCC React 商品响应缺少身份或变体')
    currency = str(layer.get('currency') or '')
    if layer.get('country_code') != 'US' or layer.get('language_code') != 'en_US' or currency != 'USD':
        raise CrawlError('SFCC React 商品不是 en_US/USD 市场')

    sizes = []
    for node in doc.select('[class*="__SizeButtonWrapper-sc"]'):
        name = _text(node.select_one('[class*="__SizeName-sc"]'))
        volume = _text(node.select_one('[class*="__SizeVolume-sc"]'))
        value = ' '.join(part for part in (name, volume) if part)
        # Preserve an explicitly rendered blank selector as blank. Some
        # storefront campaigns expose an orderable sample SKU without a
        # customer-facing size label; the SKU must not be dropped or renamed.
        if value not in sizes:
            sizes.append(value)
    if sizes and len(sizes) != len(variants_raw):
        raise CrawlError('SFCC React 商品规格与变体数量不一致')
    variants = []
    for index, raw in enumerate(variants_raw):
        price = ((raw.get('price') or {}).get('current') or {}).get('value')
        if not raw.get('id') or price is None:
            raise CrawlError('SFCC React 变体缺少 SKU 或价格')
        variant = {'id': str(raw['id']), 'sku': str(raw['id']), 'price': price,
                   'available': bool(raw.get('orderable')),
                   'inventory_quantity': raw.get('quantityAvailable', '')}
        if sizes and sizes[index]:
            variant['option1'] = sizes[index]
        variants.append(variant)
    gallery = []
    for image in doc.select('[class*="ProductImageViewer"] img[src]'):
        target = _local(image.get('src', ''), manifest['url'])
        if target and target not in gallery:
            gallery.append(target)
    if not gallery:
        source_images = schema.get('image') or []
        if isinstance(source_images, str):
            source_images = [source_images]
        gallery = list(dict.fromkeys(_local(value, manifest['url']) for value in source_images
                                     if _local(value, manifest['url'])))
    if not gallery:
        listing_image = _local(listing_product.get('image', ''), manifest['url'])
        if listing_image:
            gallery = [listing_image]
    if not gallery:
        raise CrawlError('SFCC React 商品缺少自身图库')
    brand = schema.get('brand') or ''
    if isinstance(brand, dict):
        brand = brand.get('name', '')
    description = schema.get('description') or listing_product.get('description') or ''
    if isinstance(description, list):
        description = ' '.join(
            str(value) for block in description if isinstance(block, dict)
            for value in block.get('text', []) if isinstance(value, str))
    return {'id': master,
            'handle': urlsplit(manifest['url']).path.rstrip('/').rsplit('/', 1)[-1].removesuffix('.html'),
            'title': title, 'body_html': str(description),
            'vendor': str(brand), 'currency': currency,
            'options': [{'name': 'Size'}] if any(sizes) else [], 'variants': variants,
            'images': [{'src': src, 'variant_ids': [v['id'] for v in variants]}
                       for src in gallery]}


def _verify_html_family(record, product):
    metadata = record['metadata']
    if (str(metadata['item_id']) != product['id'] or metadata['item_name'] != product['title']
            or metadata.get('color', '') not in {variant.get('option1', '') for variant in product['variants']}):
        raise CrawlError('SFCC 色款页面与已验证的父商品不一致')


def _verify_html_origin(manifest):
    doc = parse_soup(manifest['html'], parse_only=SoupStrainer('link', rel='canonical'))
    node = doc.find('link')
    canonical = _local(node.get('href', ''), manifest['url']) if node else ''
    if (not canonical or unquote(urlsplit(canonical).path) != unquote(urlsplit(manifest['url']).path)):
        raise CrawlError('SFCC HTML 商品 canonical 与当前页面身份不一致')


def fetch_product(client, url, evidence):
    page = client.get(url, timeout=45)
    if 'ga4-gtm-product-data' in page.text and 'product-variations' in page.text:
        endpoint = page.url
        manifest = {'url': page.url, 'endpoint': endpoint, 'html': page.text,
                    'format': 'sitegenesis_html', 'responses': {}}
        _verify_html_origin(manifest)
        record = _html_product(page.text, page.url)
        lock = getattr(client, 'lock', None)
        lock = lock if hasattr(lock, '__enter__') else nullcontext()
        key = (urlsplit(page.url).hostname, str(record['metadata']['item_id']))
        with lock:
            families = getattr(client, '_sfcc_families', None)
            if not isinstance(families, dict):
                families = client._sfcc_families = {}
            cached = families.get(key)
        if cached:
            source, cached_product = cached
            _verify_html_family(record, cached_product)
            manifest['family_source'] = source
            product = deepcopy(cached_product)
        else:
            product = _html_variation_tree(manifest, client.get)
            with lock:
                families.setdefault(key, (manifest, deepcopy(product)))
    else:
        endpoint = _root_endpoint(page.text, page.url)
        responses = {endpoint: client.json(endpoint)}
        _variation_tree(endpoint, responses, client.json)
        manifest = {'url': page.url, 'endpoint': endpoint, 'html': page.text, 'responses': responses}
        product = _normalize(manifest)
    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    name = 'sfcc-' + re.sub(r'[^A-Za-z0-9_.-]', '-', product['id']) + '.json'
    save_json(evidence / name, manifest)
    return {'url': url, 'source': 'json', 'endpoint': endpoint, 'product': product,
            'source_mapping': {'kind': 'sfcc_storefront', 'file': 'evidence/' + name}}


def fetch_react_product(client, url, evidence, page=None):
    """Fetch one modern React SFCC PDP without rendering or guessing variants."""
    page = page or client.get(url, timeout=45)
    endpoint = query(page.url, customer='true')
    customer = client.json(endpoint)
    _react_market(customer, 'product')
    cards = getattr(client, '_sfcc_react_cards', {})
    card = cards.get(page.url) or cards.get(url) or {}
    manifest = {'format': 'react_customer_json', 'url': page.url,
                'customer_url': endpoint, 'customer': customer,
                'listing_product': card, 'rendered_html': '', 'raw_html': page.text}
    product = _normalize(manifest)
    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^A-Za-z0-9_.-]', '-', product['id'])
    raw_name = 'sfcc-react-' + safe + '-customer.json'
    manifest_name = 'sfcc-react-' + safe + '.json'
    save_json(evidence / raw_name, customer)
    manifest['customer_file'] = 'evidence/' + raw_name
    save_json(evidence / manifest_name, manifest)
    return {'url': page.url, 'source': 'json', 'endpoint': endpoint, 'product': product,
            'source_mapping': {
                'kind': 'sfcc_storefront', 'file': 'evidence/' + manifest_name,
                'raw_file': 'evidence/' + raw_name,
                'product_fields': {'id': '/customer/dataLayer/product_master_id',
                                   'title': '/listing_product/name'},
                'variants_path': '/customer/variants',
                'variant_fields': {'id': '/id', 'price': '/price/current/value'},
                'country_path': '/customer/dataLayer/country_code',
                'currency_path': '/customer/dataLayer/currency',
                'language_path': '/customer/dataLayer/language_code',
                'image_paths': ['/listing_product/image']}}


def _manifest(root, sample):
    root = Path(root).resolve()
    path = (root / sample['source_mapping']['file']).resolve()
    if not path.is_relative_to(root / 'evidence'):
        raise CrawlError('SFCC 源响应不在 evidence 内')
    manifest = read_json(path)
    if manifest.get('format') == 'sitegenesis_html':
        _verify_html_origin(manifest)
    if manifest.get('format') == 'react_customer_json':
        if (urlsplit(manifest['url']).hostname != urlsplit(sample['url']).hostname
                or urlsplit(manifest['url']).path != urlsplit(sample['url']).path):
            raise CrawlError('SFCC React 商品证据与请求身份不一致')
        return manifest
    if ((manifest.get('format') != 'sitegenesis_html'
         and _root_endpoint(manifest['html'], manifest['url']) != manifest['endpoint'])
            or urlsplit(manifest['url']).hostname != urlsplit(sample['url']).hostname
            or urlsplit(manifest['url']).path != urlsplit(sample['url']).path):
        raise CrawlError('SFCC 商品证据与请求身份不一致')
    return manifest


def verify_source(root, sample):
    manifest = _manifest(root, sample)
    mapping = sample.get('source_mapping') or {}
    if manifest.get('format') == 'react_customer_json':
        raw_path = (Path(root).resolve() / mapping.get('raw_file', '')).resolve()
        if (not raw_path.is_relative_to(Path(root).resolve() / 'evidence')
                or read_json(raw_path) != manifest.get('customer')):
            raise CrawlError('SFCC React 原始 customer JSON 缺失或发生变化')
    if _normalize(manifest) != sample['product']:
        raise CrawlError('SFCC 规范化商品与真实变体响应不一致')


def product_market(root, sample):
    _normalize(_manifest(root, sample))
    return 'US', 'USD'
