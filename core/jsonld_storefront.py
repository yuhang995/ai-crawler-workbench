"""Deterministic storefront adapter for paged HTML grids + Product JSON-LD.

This is mechanism based: category pages expose stable product-detail anchors,
and each PDP publishes a complete schema.org Product/Offer record.  No domain
names, private APIs, JS reverse engineering, or guessed variants are used.
"""
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .common import AdapterRequired, CrawlError, read_json, save_json
from .html_parser import parse_soup


DETAIL_PATH = re.compile(r'(?:^/goods-detail/[^/?#]+/?$|^/[^/?#]+-p\d+\.prod/?$)', re.I)
LIST_PATH = re.compile(
    r'(?:^/goods-list/[^/?#]+/?$|^/[^/?#]+-(?:[sz]\d+\.store|d\d+\.dept|[acb]\d+\.lp)/?$)', re.I)


def _personalization_listing(path):
    return bool(re.search(r'-(?:[sz]\d+\.store|d\d+\.dept|[acb]\d+\.lp)/?$', path, re.I))


def supports_home(html):
    soup = parse_soup(html)
    lists = {urlsplit(a.get('href', '')).path for a in soup.select('a[href]')
             if LIST_PATH.match(urlsplit(a.get('href', '')).path)}
    # Homepages often server-render the full menu but hydrate product shelves
    # later.  Repeated goods-list routes are enough to select the adapter; its
    # listing/PDP checks still fail closed unless real goods-detail cards and a
    # unique Product JSON-LD record are observed.
    return len(lists) >= 3


def navigation(html, base, client=None, evidence_dir=None):
    # The generic custom-navigation recovery owns hierarchy construction.  An
    # empty native result intentionally routes through its source-bound AI map,
    # while product collection remains fully deterministic in this adapter.
    return {}


def _page_url(url, page):
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key.casefold() != 'page']
    if page > 1:
        query.append(('page', str(page)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode(query), ''))


def _product_links(html, page_url):
    soup = parse_soup(html)
    result = []
    scope = soup.find('main') or soup
    for anchor in scope.select('a[href]'):
        target = urljoin(page_url, anchor['href'])
        if (urlsplit(target).hostname == urlsplit(page_url).hostname
                and DETAIL_PATH.match(urlsplit(target).path)):
            target = urlsplit(target)._replace(query='', fragment='').geturl()
            if target not in result:
                result.append(target)
    return result


def _compact_page(url, links):
    anchors = ''.join('<a href="' + link.replace('&', '&amp;').replace('"', '&quot;')
                      + '"></a>' for link in links)
    return {'url': url, 'html': '<main>' + anchors + '</main>'}


def collect_listing(client, url, max_pages=100):
    if not LIST_PATH.match(urlsplit(url).path):
        raise AdapterRequired('JSON-LD storefront 分类 URL 不是可验证商品列表')
    urls, pages = [], []
    page_limit = 1 if _personalization_listing(urlsplit(url).path) else max_pages
    for page in range(1, page_limit + 1):
        target = _page_url(url, page)
        response = client.get(target)
        found = _product_links(response.text, response.url)
        if not found:
            if page == 1:
                raise AdapterRequired('JSON-LD storefront 分类页没有商品卡片')
            break
        new = [item for item in found if item not in urls]
        if not new:
            raise CrawlError('JSON-LD storefront 分类分页重复，无法证明已到终点')
        urls.extend(new)
        pages.append(_compact_page(response.url, found))
    else:
        if page_limit == 1:
            return {'url': url, 'format': 'jsonld_storefront', 'source_format': 'jsonld_storefront',
                    'complete': True, 'urls': urls, 'catalog_urls': urls,
                    'pages': pages, 'feeds': []}
        raise CrawlError('JSON-LD storefront 分类超过有界分页上限')
    return {'url': url, 'format': 'jsonld_storefront', 'source_format': 'jsonld_storefront',
            'complete': True, 'urls': urls, 'catalog_urls': urls,
            'pages': pages, 'feeds': []}


def validate_listing(listing, links):
    """Replay the compact, source-derived listing evidence.

    The adapter stores only the observed product anchors instead of retaining
    multi-megabyte rendered pages.  Validation still reconstructs the ordered
    URL list through the same parser and rejects reordered, missing, injected,
    or cross-category evidence.
    """
    pages = listing.get('pages')
    if (listing.get('format') != 'jsonld_storefront'
            or listing.get('source_format') != 'jsonld_storefront'
            or not listing.get('complete') or not isinstance(pages, list) or not pages):
        raise CrawlError('JSON-LD storefront 列表缺少完整分页证据')
    observed = []
    for index, page in enumerate(pages, 1):
        if not isinstance(page, dict) or not page.get('url') or not isinstance(page.get('html'), str):
            raise CrawlError('JSON-LD storefront 分页证据格式无效')
        expected = _page_url(listing['url'], index)
        actual_parts, expected_parts = urlsplit(page['url']), urlsplit(expected)
        if ((actual_parts.hostname, actual_parts.path, parse_qsl(actual_parts.query))
                != (expected_parts.hostname, expected_parts.path, parse_qsl(expected_parts.query))):
            raise CrawlError('JSON-LD storefront 分页证据顺序不符')
        found = _product_links(page['html'], page['url'])
        if not found:
            raise CrawlError('JSON-LD storefront 分页证据没有商品卡片')
        for url in found:
            if url not in observed:
                observed.append(url)
    excluded = set(listing.get('excluded_urls', []))
    if excluded and not excluded.issubset(observed):
        raise CrawlError('JSON-LD storefront 剔除商品不属于原始商品网格')
    observed = [url for url in observed if url not in excluded]
    if (observed != listing.get('urls') or observed != listing.get('catalog_urls')
            or observed != links):
        raise CrawlError('JSON-LD storefront 详情范围与商品网格不一致')


def _product_json(html):
    soup = parse_soup(html)
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        stack = list(value) if isinstance(value, list) else [value]
        while stack:
            item = stack.pop(0)
            if not isinstance(item, dict):
                continue
            kind = item.get('@type')
            kinds = kind if isinstance(kind, list) else [kind]
            if 'Product' in kinds:
                found.append(item)
            graph = item.get('@graph')
            if isinstance(graph, list):
                stack.extend(graph)
    if len(found) != 1:
        raise AdapterRequired('Product JSON-LD 数量不唯一')
    return found[0]


def normalize_product(raw, expected_url=''):
    offer = raw.get('offers')
    if isinstance(offer, list):
        if len(offer) != 1:
            raise AdapterRequired('Product JSON-LD 包含多个无法归属的 Offer')
        offer = offer[0]
    images = raw.get('image')
    images = images if isinstance(images, list) else [images]
    sku = str(raw.get('sku') or raw.get('mpn') or '').strip()
    if not sku and expected_url:
        match = re.search(r'-p(\d+)\.prod/?$', urlsplit(expected_url).path, re.I)
        sku = match.group(1) if match else ''
    title = str(raw.get('name') or '').strip()
    price = str((offer or {}).get('price') or '').strip()
    currency = str((offer or {}).get('priceCurrency') or '').strip()
    source_url = str(raw.get('url') or '').strip()
    if (not sku or not title or not re.fullmatch(r'\d+(?:\.\d+)?', price)
            or currency != 'USD' or not isinstance(offer, dict)):
        raise CrawlError('Product JSON-LD 缺少身份、USD 价格或 Offer')
    if expected_url and source_url and urlsplit(source_url).path.rstrip('/') != urlsplit(expected_url).path.rstrip('/'):
        raise CrawlError('Product JSON-LD 商品身份与请求 URL 不一致')
    color = str(raw.get('color') or '').strip()
    availability = str(offer.get('availability') or '')
    image_rows = [{'id': str(index + 1), 'src': str(image), 'variant_ids': [sku]}
                  for index, image in enumerate(images) if isinstance(image, str) and image]
    if not image_rows:
        raise CrawlError('Product JSON-LD 没有可映射商品图片')
    variant = {'id': sku, 'sku': sku, 'title': color or 'Default',
               'price': price, 'compare_at_price': None,
               'available': availability.endswith('/InStock'),
               'option1': color or 'Default', 'option2': None, 'option3': None,
               'image_id': image_rows[0]['id'], 'featured_image': {'src': image_rows[0]['src']}}
    brand = raw.get('brand') or {}
    vendor = brand.get('name') if isinstance(brand, dict) else brand
    return {'id': sku, 'handle': urlsplit(expected_url or source_url).path.rstrip('/').rsplit('/', 1)[-1],
            'title': title, 'vendor': str(vendor or ''),
            'body_html': str(raw.get('description') or ''), 'currency': currency,
            'options': [{'id': 1, 'name': 'Color', 'position': 1,
                         'values': [color or 'Default']}],
            'variants': [variant], 'images': image_rows}


def fetch_product(client, url, evidence_dir):
    response = client.get(url)
    raw = _product_json(response.text)
    product = normalize_product(raw, response.url)
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    market_file = evidence_dir / 'jsonld-market.html'
    home_file = evidence_dir / 'home.html'
    if not market_file.is_file() and home_file.is_file():
        home_soup = parse_soup(home_file.read_text(encoding='utf-8'))
        shipping = next((urljoin(url, anchor.get('href', ''))
                         for anchor in home_soup.select('a[href]')
                         if 'shippingrates' in anchor.get('href', '').casefold()), '')
        if shipping and urlsplit(shipping).hostname == urlsplit(url).hostname:
            market_response = client.get(shipping)
            market_soup = parse_soup(market_response.text)
            selected = market_soup.select_one('select option[selected]')
            if selected and 'united states' in selected.get_text(' ', strip=True).casefold():
                market_file.write_text(market_response.text, encoding='utf-8')
    name = 'jsonld-product-' + re.sub(r'[^A-Za-z0-9_.-]+', '-', product['id']) + '.json'
    save_json(evidence_dir / name, raw)
    relative = 'evidence/' + name
    return {'url': response.url, 'source': 'json', 'endpoint': response.url,
            'product': product,
            'source_mapping': {'kind': 'jsonld_storefront', 'file': relative}}


def verify_source(root, sample):
    raw = read_json(Path(root) / sample['source_mapping']['file'])
    observed = normalize_product(raw, sample['url'])
    if observed != sample.get('product'):
        raise CrawlError('JSON-LD 规范化商品与原始证据不一致')


def product_market(root, sample):
    raw = read_json(Path(root) / sample['source_mapping']['file'])
    offer = raw.get('offers')
    offer = offer[0] if isinstance(offer, list) and len(offer) == 1 else offer
    currency = (offer or {}).get('priceCurrency')
    home = Path(root) / 'evidence' / 'home.html'
    soup = parse_soup(home.read_text(encoding='utf-8')) if home.is_file() else None
    base_host = urlsplit(read_json(Path(root) / 'config.json')['base_url']).hostname
    us = bool(soup and any(
        str(link.get('hreflang', '')).casefold() == 'en-us'
        and urlsplit(urljoin(sample['url'], link.get('href', ''))).hostname == base_host
        for link in soup.select('link[rel="alternate"][hreflang][href]')))
    market = Path(root) / 'evidence' / 'jsonld-market.html'
    if not us and market.is_file():
        market_soup = parse_soup(market.read_text(encoding='utf-8'))
        selected = market_soup.select_one('select option[selected]')
        us = bool(selected and 'united states' in selected.get_text(' ', strip=True).casefold())
    return ('US' if us else '', currency)
