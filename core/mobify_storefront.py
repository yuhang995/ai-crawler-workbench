"""Salesforce PWA Kit/Mobify storefronts backed by embedded ``mobify-data``.

The embedded state is authoritative for the selected site/locale, Algolia
listing query and PDP Commerce API response.  This adapter never treats a
search index as a site-wide catalog: it replays only the exact query embedded
by the requested PLP and follows every declared page in order.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

from .common import CrawlError, canonical, save_json
from .html_parser import parse_soup


KIND = 'mobify_storefront'


def supports(html):
    return bool(re.search(r'<script[^>]+id=["\']mobify-data["\']', html, re.I)
                and '"__PRELOADED_STATE__"' in html)


def _state(html):
    node = parse_soup(html, 'lxml').find('script', id='mobify-data')
    if node is None or not (node.string or node.get_text()):
        raise CrawlError('Mobify 页面缺少 mobify-data')
    try:
        data = json.loads(node.string or node.get_text())
    except (TypeError, ValueError) as error:
        raise CrawlError('mobify-data 不是有效 JSON') from error
    if not isinstance(data.get('__PRELOADED_STATE__'), dict):
        raise CrawlError('mobify-data 缺少预加载状态')
    return data


def _queries(data):
    queries = data.get('__PRELOADED_STATE__', {}).get('__reactQuery', {}).get('queries')
    if not isinstance(queries, list):
        raise CrawlError('Mobify 页面缺少 React Query 证据')
    return queries


def _query_data(query):
    return query.get('state', {}).get('data') if isinstance(query, dict) else None


def _prefix(base):
    parts = urlsplit(base)
    match = re.match(r'^(/[a-z]{2}/[a-z]{2}-[a-z]{2})(?:/|$)', parts.path, re.I)
    return match.group(1) if match else ''


def _internal(value, base):
    if not isinstance(value, str) or not value:
        return ''
    if value.startswith('/') and _prefix(base) and not value.startswith(_prefix(base) + '/'):
        value = _prefix(base) + value
    target = urljoin(base, value)
    if urlsplit(target).hostname != urlsplit(base).hostname:
        return ''
    return target


def _key(labels):
    labels = [' '.join(str(label).replace(',', ' ').split()) for label in labels if str(label).strip()]
    return ','.join(' '.join(labels[:i + 1]) for i in range(len(labels)))


def _header(data):
    for query in _queries(data):
        value = _query_data(query)
        if isinstance(value, dict) and isinstance(value.get('main_nav'), list):
            return value
    raise CrawlError('Mobify 首页缺少 main_nav')


def _visible_category_navigation(html, base):
    """Recover newer PWA Kit menus from the rendered header links.

    Recent PWA Kit builds expose ``nav-categories`` instead of Contentstack's
    legacy ``main_nav``.  The server-rendered header still contains the exact
    visible category anchors, so use their URL-prefix ownership rather than
    importing every (including hidden) category from the backend state.
    """
    soup = parse_soup(html, 'lxml')
    header = soup.find('header')
    if header is None:
        return {}
    observed = []
    seen = set()
    for anchor in header.select('a[href]'):
        target = _internal(anchor.get('href'), base)
        path = urlsplit(target).path.rstrip('/') if target else ''
        if (not target or '/category/' not in path.casefold()
                or path in seen):
            continue
        label = ' '.join(anchor.get_text(' ', strip=True).split())
        if not label:
            continue
        seen.add(path)
        observed.append((path, label, target))
    by_path = {path: label for path, label, _ in observed}
    result = {}
    for path, label, target in observed:
        category_root, tail = path.split('/category/', 1)
        segments = [part for part in tail.split('/') if part]
        labels = []
        for index in range(len(segments)):
            prefix = category_root + '/category/' + '/'.join(segments[:index + 1])
            parent = by_path.get(prefix)
            if parent:
                labels.append(parent)
        if not labels:
            labels = [label]
        elif labels[-1] != label:
            labels.append(label)
        # Preserve root and immediate group; flatten deeper visible leaves to
        # the third level as required by the category JSON contract.
        if len(labels) > 3:
            labels = labels[:2] + [labels[-1]]
        result.setdefault(_key(labels), target)
    return result


def navigation(html, base, client=None, evidence_dir=None):
    """Read Contentstack's explicit menu; omit the editorial Explore column."""
    data = _state(html)
    try:
        header = _header(data)
    except CrawlError:
        result = _visible_category_navigation(html, base)
        if not result:
            raise
        if evidence_dir:
            save_json(Path(evidence_dir) / 'mobify-navigation.json', {
                'source': 'visible_header_category_links',
                'categories': result,
            })
        return result
    result = {}
    for menu in header['main_nav']:
        for root in menu.get('link', []) if isinstance(menu, dict) else []:
            label = ' '.join(str(root.get('title') or '').split())
            target = _internal((root.get('url_custom') or {}).get('internal'), base)
            if not label or not target or re.search(r'gift[-_ ]?card', target, re.I):
                continue
            result.setdefault(_key([label]), target)
            # ``secondary_links`` is displayed under the generic heading
            # Explore and mixes guides/deals with repeated Shop All links.
            # The visual product taxonomy is the sibling ``link`` card list.
            for child in root.get('link', []) or []:
                title = ' '.join(str(child.get('title') or '').split())
                child_url = _internal((child.get('url_custom') or {}).get('internal'), base)
                if (title and child_url and not re.fullmatch(r'(?:shop|view)\s+all', title, re.I)
                        and child_url != target
                        and '/p/' not in urlsplit(child_url).path
                        and not re.search(r'gift[-_ ]?card', child_url, re.I)):
                    result.setdefault(_key([label, title]), child_url)
    if not result:
        raise CrawlError('Mobify 菜单没有商品分类')
    if evidence_dir:
        save_json(Path(evidence_dir) / 'mobify-navigation.json', header)
    return result


def _search(data):
    for query in _queries(data):
        value = _query_data(query)
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            if (isinstance(item, dict) and isinstance(item.get('hits'), list)
                    and all(key in item for key in ('page', 'nbPages', 'nbHits', 'hitsPerPage', 'index', 'params'))):
                return item
    raise CrawlError('Mobify 分类页缺少完整 Algolia 查询')


def _hit_url(hit, base):
    slug, sku = str(hit.get('urlSlug') or ''), str(hit.get('productId') or '')
    if (not slug.strip() or slug != slug.strip() or '/' in slug or '..' in slug
            or any(ord(char) < 32 for char in slug) or not sku):
        raise CrawlError('Algolia 商品缺少公开 slug 或 SKU')
    return _internal('/p/' + quote(slug, safe='-._~'), base) + '?' + urlencode({'sku': sku})


def _post_search(client, endpoint, headers, params):
    # Reuse the Client's warmed storefront session and pacing.  Algolia's
    # public search key is embedded in the page; it is not an account secret.
    client.pace()
    session = getattr(getattr(client, 'local', None), 'session', None)
    if session is None:
        raise CrawlError('Mobify Algolia 请求缺少已预热会话')
    response = session.post(endpoint, json={'params': params}, headers=headers, timeout=client.timeout)
    if response.status_code != 200:
        raise CrawlError('Mobify Algolia 分类分页 HTTP ' + str(response.status_code))
    try:
        value = response.json()
    except ValueError as error:
        raise CrawlError('Mobify Algolia 分类分页不是 JSON') from error
    return value


def collect_listing(client, url):
    response = client.get(url, timeout=45)
    data = _state(response.text)
    first = _search(data)
    config = data.get('__CONFIG__', {}).get('app', {}).get('algolia', {})
    app, key = config.get('appId'), config.get('apiKey')
    if not isinstance(app, str) or not isinstance(key, str) or not app or not key:
        raise CrawlError('Mobify 页面缺少公开 Algolia 配置')
    endpoint = f'https://{app.lower()}-dsn.algolia.net/1/indexes/{first["index"]}/query'
    headers = {'x-algolia-application-id': app, 'x-algolia-api-key': key,
               'content-type': 'application/json'}
    pages, products = [], []
    seen = set()
    total_pages = first['nbPages']
    if not all(isinstance(v, int) and v >= 0 for v in (first['page'], total_pages, first['nbHits'], first['hitsPerPage'])):
        raise CrawlError('Mobify Algolia 分页元数据无效')
    for page in range(total_pages):
        if page == first['page']:
            value = first
        else:
            pairs = parse_qsl(first['params'], keep_blank_values=True)
            pairs = [(name, str(page) if name == 'page' else val) for name, val in pairs]
            if not any(name == 'page' for name, _ in pairs):
                pairs.append(('page', str(page)))
            value = _post_search(client, endpoint, headers, urlencode(pairs))
        if (value.get('page') != page or value.get('nbPages') != total_pages
                or value.get('nbHits') != first['nbHits'] or not isinstance(value.get('hits'), list)):
            raise CrawlError('Mobify Algolia 分类分页范围发生变化')
        urls = []
        for hit in value['hits']:
            target = _hit_url(hit, response.url)
            identity = str(hit.get('objectID') or hit.get('productId') or '')
            if not identity or identity in seen:
                if identity in seen:
                    continue
                raise CrawlError('Mobify Algolia 商品身份缺失')
            seen.add(identity); products.append(target); urls.append(target)
        pages.append({'page': page, 'response': value, 'urls': urls})
    if len(products) != first['nbHits']:
        raise CrawlError(f'Mobify 分类未完整翻页: {len(products)}/{first["nbHits"]}')
    return {'url': response.url, 'format': KIND, 'complete': True,
            'urls': products, 'catalog_urls': products, 'products': [],
            'pages': pages, 'total': first['nbHits'], 'index': first['index']}


def validate_listing(listing, links):
    pages = listing.get('pages')
    if not listing.get('complete') or not isinstance(pages, list) or not pages:
        raise CrawlError('Mobify 分类缺少完整分页证据')
    observed, total = [], None
    for index, page in enumerate(pages):
        raw = page.get('response')
        if not isinstance(raw, dict) or raw.get('page') != index or raw.get('nbPages') != len(pages):
            raise CrawlError('Mobify 分类分页证据无效')
        total = raw.get('nbHits') if total is None else total
        if raw.get('nbHits') != total:
            raise CrawlError('Mobify 分类分页总数不一致')
        current = [_hit_url(hit, listing['url']) for hit in raw.get('hits', [])]
        if current != page.get('urls'):
            raise CrawlError('Mobify 分类页商品顺序被修改')
        observed.extend(current)
    if len(observed) != total or observed != links or listing.get('urls') != links:
        raise CrawlError('Mobify 商品列表与原始 Algolia 顺序不一致')


def _pdp(data):
    for query in _queries(data):
        value = _query_data(query)
        if (isinstance(value, dict) and isinstance(value.get('product'), dict)
                and isinstance(value.get('CTS'), dict)):
            return value
    raise CrawlError('Mobify PDP 缺少商品响应')


def _images(product, cts):
    result = []
    for group in product.get('imageGroups', []) or []:
        for image in group.get('images', []) or []:
            value = image.get('link')
            if value:
                result.append(value.replace('http://', 'https://', 1))
    def walk(value):
        if isinstance(value, dict):
            secure = value.get('secure_url')
            if isinstance(secure, str) and secure:
                result.append(secure)
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    # Only the PDP gallery block, never value-prop icons or recommendations.
    walk(cts.get('images', []))
    return list(dict.fromkeys(result))


def parse_product(data, url):
    value = _pdp(data)
    raw, cts = value['product'], value['CTS']
    sku = str(raw.get('id') or '')
    requested = dict(parse_qsl(urlsplit(url).query)).get('sku')
    if not sku or requested != sku or raw.get('currency') != 'USD':
        raise CrawlError('Mobify PDP 商品身份或 USD 市场不一致')
    price = raw.get('price')
    inventory = raw.get('inventory')
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not isinstance(inventory, dict):
        raise CrawlError('Mobify PDP 缺少价格或库存')
    images = _images(raw, cts)
    if not images:
        raise CrawlError('Mobify PDP 缺少商品图集')
    slug = str(raw.get('c_urlSlug') or urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1])
    variant = {'id': sku, 'sku': sku, 'price': format(price, '.2f'),
               'compare_at_price': None,
               'available': bool(inventory.get('orderable')),
               'inventory_quantity': inventory.get('stockLevel', ''),
               'featured_image': {'src': images[0]}, 'source_variant_url': url}
    return {'id': sku, 'handle': slug, 'title': str(raw.get('name') or ''),
            'currency': 'USD', 'vendor': str(raw.get('brand') or ''),
            'product_type': str(raw.get('c_productType') or ''),
            'body_html': str(raw.get('shortDescription') or ''),
            'options': [], 'variants': [variant],
            'images': [{'src': image, 'variant_ids': [sku] if i == 0 else []}
                       for i, image in enumerate(images)]}


def fetch_product(client, url, evidence):
    response = client.get(url, timeout=45)
    data = _state(response.text)
    product = parse_product(data, url)
    requested, final = urlsplit(url), urlsplit(response.url)
    if (requested.hostname != final.hostname or '/p/' not in final.path
            or dict(parse_qsl(requested.query)).get('sku') != product['id']):
        raise CrawlError('Mobify PDP 跳转到了另一商品')
    directory = Path(evidence); directory.mkdir(parents=True, exist_ok=True)
    name = 'mobify-product-' + re.sub(r'[^A-Za-z0-9_.-]', '-', product['id']) + '.json'
    save_json(directory / name, {'url': url, 'endpoint': response.url, 'mobify_data': data})
    return {'url': url, 'endpoint': response.url, 'source': 'json', 'product': product,
            'source_mapping': {'kind': KIND, 'file': 'evidence/' + name}}


def _evidence(root, sample):
    path = (Path(root) / sample['source_mapping']['file']).resolve()
    evidence = (Path(root) / 'evidence').resolve()
    if not path.is_relative_to(evidence):
        raise CrawlError('Mobify 商品证据路径无效')
    raw = json.loads(path.read_text(encoding='utf-8'))
    if (raw.get('url') != sample['url'] or raw.get('endpoint') != sample.get('endpoint')
            or not isinstance(raw.get('mobify_data'), dict)):
        raise CrawlError('Mobify 商品证据身份无效')
    return raw['mobify_data']


def verify_source(root, sample):
    evidence = _evidence(root, sample)
    if (sample.get('source') != 'json'
            or urlsplit(sample.get('endpoint', '')).hostname != urlsplit(sample['url']).hostname
            or parse_product(evidence, sample['url']) != sample.get('product')):
        raise CrawlError('Mobify 商品与原始 SKU、价格、库存或图片不一致')


def product_market(root, sample):
    product = parse_product(_evidence(root, sample), sample['url'])
    return 'US', product['currency']
