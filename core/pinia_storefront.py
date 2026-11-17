"""Public Pinia storefront state, declared category ancestry and SKU children."""
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit, unquote

from .common import AdapterRequired, CrawlError, canonical, read_json, save_json
from .html_parser import parse_soup
from .shopify_fastpath import clean_description, money


def state_from_html(html):
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*', html)
    if not match:
        raise AdapterRequired('页面缺少公开 Pinia 初始状态')
    try:
        initial = json.JSONDecoder().raw_decode(html[match.end():])[0]
        table = json.loads(initial['pinia'])
        memo = {}

        def restore(index):
            if type(index) is not int:
                return index
            if index < 0:
                return None
            if index >= len(table):
                raise ValueError('Pinia reference outside table')
            if index in memo:
                return memo[index]
            value = table[index]
            if isinstance(value, dict):
                target = {}
                memo[index] = target
                target.update((key, restore(item)) for key, item in value.items())
                return target
            if isinstance(value, list):
                target = []
                memo[index] = target
                target.extend(restore(item) for item in value)
                return target
            return value

        return restore(0)
    except (ValueError, KeyError, TypeError, RecursionError) as error:
        raise AdapterRequired('Pinia 初始状态无法解码') from error


def _homepage_category_identity(state, url):
    path = urlsplit(url).path.strip('/')
    products = state.get('products', {}).get('byUrl', {})
    if any(urlsplit(value).path.strip('/') == path for value in products):
        return False
    categories = state.get('categories', {})
    identifiers = {str(value) for key, value in categories.get('byUrl', {}).items()
                   if urlsplit(key).path.strip('/') == path}
    for category in categories.get('categories', {}).values():
        if (category.get('categoryId') is not None
                and category.get('isActive', True)
                and urlsplit(category.get('url', '')).path.strip('/') == path
                and (not identifiers or str(category['categoryId']) in identifiers)):
            return True
    return False


def _homepage_categories(soup, state, base, result, client, evidence_dir):
    from .custom_navigation import catalog_url
    candidates, audit = [], []
    menu_urls = set(result.values())
    canonical_menu_urls = {canonical(value) for value in menu_urls}
    selectors = ('main a[href], [role="main"] a[href], #main-content a[href], '
                 '.cms-row a[href], .call-to-action a[href]')
    for anchor in soup.select(selectors):
        target = urljoin(base, anchor['href'])
        if (canonical(target) in canonical_menu_urls or not catalog_url(target, base)
                or anchor.find_parent(['header', 'footer']) is not None
                or re.search(r'/(?:news|contact(?:-us)?|about(?:-us)?|careers|shippingtracker)(?:/|$)',
                             urlsplit(target).path, re.I)):
            continue
        classes = {name for node in [anchor, *anchor.parents]
                   for name in node.get('class', [])}
        if classes.intersection({'product-card', 'product-list', 'products-wrapper',
                                 'product-grid', 'hover-card'}):
            continue
        query = parse_qsl(urlsplit(target).query, keep_blank_values=True)
        tracking_only = query and all(key.lower().startswith('utm_') or key.lower() in {'gclid', 'fbclid'}
                                      for key, _ in query)
        without_query = urlsplit(target)._replace(query='').geturl()
        if tracking_only and canonical(without_query) in canonical_menu_urls:
            continue
        if target not in candidates:
            candidates.append(target)
    for index, target in enumerate(candidates):
        if index >= 24:
            audit.append({'url': target, 'status': 'deferred', 'reason': 'homepage_candidate_limit'})
            continue
        verified = _homepage_category_identity(state, target)
        response = None
        if not verified and client is not None:
            try:
                response = client.get(target, timeout=12, attempts=1)
                verified = (catalog_url(response.url, base)
                            and _homepage_category_identity(state_from_html(response.text), response.url))
            except (CrawlError, ValueError, KeyError, TypeError) as error:
                audit.append({'url': target, 'status': 'unverified', 'reason': str(error)})
                continue
        if not verified:
            audit.append({'url': target, 'status': 'excluded', 'reason': 'no_category_identity'})
            continue
        parsed = urlsplit(target)
        key = unquote(parsed.path.rstrip('/').rsplit('/', 1)[-1])
        if parsed.query:
            key += '?' + parsed.query
        key = key.replace(',', ' ')
        if key in result and result[key] != target:
            key = unquote(parsed.path.strip('/')).replace('/', ' ').replace(',', ' ')
            if parsed.query:
                key += '?' + parsed.query.replace(',', ' ')
        if key in result and result[key] != target:
            audit.append({'url': target, 'status': 'deferred', 'reason': 'existing_category_key'})
            continue
        result[key] = target
        audit.append({'url': target, 'status': 'included', 'key': key})
        if response is not None and evidence_dir is not None:
            directory = Path(evidence_dir)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f'home-category-{index:02}.html').write_text(response.text, encoding='utf-8')
    if evidence_dir is not None:
        save_json(Path(evidence_dir) / 'pinia_homepage_audit.json', {'candidates': audit})


def navigation(html, base, client=None, evidence_dir=None):
    from .custom_navigation import catalog_url
    state = state_from_html(html)
    soup = parse_soup(html)
    header = soup.find('header') or soup
    anchors = {urljoin(base, a['href']) for a in header.select('a[href]')}
    items = state.get('categories', {}).get('menuItems', [])
    by_id = {item['id']: item for item in items}
    result = {}
    for item in items:
        target = urljoin(base, item.get('url', ''))
        if not item.get('url') or target not in anchors or not catalog_url(target, base):
            continue
        chain, seen, current = [], set(), item
        while current and current['id'] not in seen:
            seen.add(current['id'])
            if current.get('url'):
                chain.insert(0, current['name'].replace(',', ' '))
            current = by_id.get(current.get('parent'))
        if len(chain) > 3:
            chain = chain[:2] + [' '.join(chain[2:])]
        key = ','.join(' '.join(chain[:index + 1]) for index in range(len(chain)))
        result.setdefault(key, target)
    for item in state.get('ui', {}).get('topNavLinks', []):
        target = urljoin(base, item.get('url', ''))
        if target in anchors and target not in result.values() and catalog_url(target, base):
            result.setdefault(item['name'].replace(',', ' '), target)
    _homepage_categories(soup, state, base, result, client, evidence_dir)
    if not result:
        raise AdapterRequired('Pinia 页面缺少已声明菜单关系')
    return result


def _listing_page(html, url):
    state = state_from_html(html)
    categories = state.get('categories', {}).get('categories', {})
    active = {str(item['categoryId']) for item in categories.values()
              if canonical(urljoin(url, item.get('url', ''))) == canonical(url.split('?', 1)[0])}
    soup = parse_soup(html)
    observed = list(dict.fromkeys(urljoin(url, a['href']) for a in soup.select('.products-wrapper a.hover-card[href]')))
    if not active or not observed:
        raise AdapterRequired('分类缺少可确认身份的前台商品网格')
    totals = set()
    for batch in state.get('http', {}).get('cache', {}).values():
        if not isinstance(batch, dict):
            continue
        request = batch.get('cnstrc_request', {})
        if request and (request.get('browse_filter_name') != 'group_id'
                        or str(request.get('browse_filter_value')) not in active):
            continue
        products = batch.get('content', {}).get('product', {})
        values = products.get('value', [])
        urls = list(dict.fromkeys(urljoin(url, item['url']) for item in values if item.get('url')))
        if urls == observed and products.get('total_item') is not None:
            totals.add(int(products['total_item']))
    if len(totals) != 1:
        raise AdapterRequired('Pinia 分类数据与前台卡片顺序或总数不一致')
    next_link = soup.select_one('a[rel="next"][href]')
    return observed, totals.pop(), urljoin(url, next_link['href']) if next_link else ''


def collect_listing(client, url):
    pages, urls, seen = [], [], set()
    current = url
    while current:
        if current in seen:
            raise CrawlError('Pinia 分类分页循环')
        seen.add(current)
        response = client.get(current, timeout=45)
        found, total, following = _listing_page(response.text, response.url)
        pages.append({'url': current, 'html': response.text})
        urls.extend(item for item in found if item not in urls)
        if len(urls) < total and not following:
            raise AdapterRequired('Pinia 网格尚有下一页，需要读取其公开翻页请求')
        current = following
    return {'format': 'pinia_storefront', 'url': url, 'complete': True,
            'urls': urls, 'pages': pages, 'products': []}


def validate_listing(listing, links):
    current, urls = listing['url'], []
    for page in listing['pages']:
        if page['url'] != current:
            raise CrawlError('Pinia 分类分页来源不连续')
        found, total, current = _listing_page(page['html'], page['url'])
        urls.extend(item for item in found if item not in urls)
    if current or len(urls) != total or urls != links or urls != listing['urls']:
        raise CrawlError('Pinia 分类没有完整保留前台商品范围')


def _market(html):
    match = re.search(r'"l10n"\s*:\s*(\{[^{}]+\})', html)
    value = json.loads(match[1]) if match else {}
    if value.get('country') != 'US' or value.get('currency') != 'USD':
        raise CrawlError('Pinia 当前市场没有确认 US/USD')
    return 'US', 'USD'


def _description(value):
    if isinstance(value, dict):
        if value.get('skus') or value.get('props', {}).get('tab', 'Product Description') != 'Product Description':
            return ''
        if value.get('name') == 'html':
            return str(value.get('value', ''))
        return ''.join(_description(item) for item in value.values())
    if isinstance(value, list):
        return ''.join(_description(item) for item in value)
    return ''


def normalize(html, url):
    _market(html)
    state = state_from_html(html)
    store = state['products']
    path = urlsplit(url).path
    identifier = store.get('byUrl', {}).get(path)
    products = store.get('products', {})
    parent = products.get(str(identifier))
    if not parent or canonical(urljoin(url, parent['url'])) != canonical(url):
        raise AdapterRequired('Pinia 商品页与请求身份不匹配')
    leaves, attributes, seen = [], [], set()

    def visit(product):
        pid = str(product['productId'])
        if pid in seen:
            return
        seen.add(pid)
        if product.get('customOptions'):
            raise AdapterRequired('商品含自定义定价选项，需要专项解析')
        for attribute in product.get('configurableAttributes', []):
            if attribute['code'] not in [item['code'] for item in attributes]:
                attributes.append(attribute)
        if product.get('childrenIds'):
            for child_id in product['childrenIds']:
                if str(child_id) not in products:
                    raise AdapterRequired('公开状态缺少所声明的子商品')
                visit(products[str(child_id)])
        elif product.get('type') == 'simple':
            leaves.append(product)
        else:
            raise AdapterRequired('商品结构没有可验证的叶子 SKU')

    visit(parent)
    if not leaves or len(attributes) > 3:
        raise AdapterRequired('商品缺少 SKU 或属性超过当前导出维度')
    soup = parse_soup(html)
    sources = [img.get('src', '') for img in soup.select('img[src]')]
    paths = [image['link'] for product in [parent] + leaves for image in product.get('gallery', [])
             if image.get('type') == 'image' and image.get('isInGallery')]
    prefixes = []
    for source in sources:
        for image_path in paths:
            if source.endswith(image_path):
                prefixes.append(source[:-len(image_path)])
    if not prefixes:
        raise AdapterRequired('商品画廊没有可确认的公开媒体地址')
    media_prefix = max(prefixes, key=lambda value: max(
        (int(width) for width in re.findall(r'[,/]w_(\d+)', value)), default=0))
    images, variants = {}, []
    for product in [parent] + leaves:
        pid = str(product['productId'])
        for image in product.get('gallery', []):
            if image.get('type') == 'image' and image.get('isInGallery'):
                target = media_prefix + image['link']
                images.setdefault(target, [])
                if product in leaves and pid not in images[target]:
                    images[target].append(pid)
    for product in leaves:
        regular = money(product['price'], 'json')
        special = product.get('special_price')
        sale = special is not None and float(special) < float(regular)
        variant = {'id': str(product['productId']), 'sku': product['sku'],
                   'price': money(special, 'json') if sale else regular,
                   'compare_at_price': regular if sale else None,
                   'source_variant_url': url}
        for index, attribute in enumerate(attributes, 1):
            value = product.get('attributes', {}).get(attribute['code'])
            if value in (None, ''):
                raise AdapterRequired('真实 SKU 缺少父项声明的规格值')
            variant[f'option{index}'] = str(value)
        if not attributes and len(leaves) > 1:
            variant['option1'] = product['name']
        variants.append(variant)
    options = [{'name': item['title']} for item in attributes]
    if not options and len(leaves) > 1:
        options = [{'name': 'Style'}]
    return {'id': str(parent['productId']), 'handle': path.strip('/').replace('/', '-'),
            'title': parent['name'], 'currency': 'USD',
            'vendor': parent.get('attributes', {}).get('manufacturer', ''),
            'body_html': clean_description(_description(parent.get('roguebuilder', {}).get('slots', {}).get('tabs', []))),
            'options': options, 'variants': variants,
            'images': [{'src': source, 'variant_ids': ids} for source, ids in images.items()]}


def fetch_product(client, url, evidence):
    response = client.get(url, timeout=45)
    product = normalize(response.text, url)
    evidence = Path(evidence)
    evidence.mkdir(exist_ok=True, parents=True)
    name = 'pinia-' + product['id'] + '.html'
    (evidence / name).write_text(response.text, encoding='utf-8')
    return {'url': url, 'endpoint': url, 'source': 'json', 'product': product,
            'source_mapping': {'kind': 'pinia_storefront', 'file': 'evidence/' + name}}


def _source(root, sample):
    path = (Path(root) / sample['source_mapping']['file']).resolve()
    if not path.is_relative_to((Path(root) / 'evidence').resolve()):
        raise CrawlError('Pinia 商品源文件不在 evidence 目录')
    return path.read_text(encoding='utf-8')


def verify_source(root, sample):
    if normalize(_source(root, sample), sample['url']) != sample['product']:
        raise CrawlError('Pinia 商品与原始父子 SKU 状态不一致')


def product_market(root, sample):
    return _market(_source(root, sample))
