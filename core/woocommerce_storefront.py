"""Deterministic public WooCommerce Store API storefront adapter."""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .common import CrawlError, canonical, query, read_json, save_json
from .html_parser import parse_soup

KIND = 'woocommerce_storefront'
PAGE_SIZE = 100


def supports(html):
    lower = html.casefold()
    return ('woocommerce' in lower and ('/wp-json/' in lower or 'wc-block' in lower)
            or '/wp-content/plugins/woocommerce/' in lower)


def _key(labels):
    cleaned = [' '.join(str(label).replace(',', ' ').split()) for label in labels if str(label).strip()]
    return ','.join(' '.join(cleaned[:i + 1]) for i in range(len(cleaned)))


def navigation(html, base, client=None, evidence_dir=None):
    soup = parse_soup(html, 'lxml')
    header = soup.find('header')
    if header is None:
        raise CrawlError('WooCommerce 首页缺少可见导航')
    result = {}
    for anchor in header.select('a[href*="/product-category/"]'):
        target = urljoin(base, anchor.get('href'))
        if urlsplit(target).hostname != urlsplit(base).hostname:
            continue
        label = ' '.join(anchor.get_text(' ', strip=True).split())
        if not label:
            continue
        owners = []
        for item in reversed(anchor.find_parents('li')[:3]):
            owner = item.find('a', recursive=False)
            owner_label = ' '.join(owner.get_text(' ', strip=True).split()) if owner else ''
            if owner_label and owner_label not in owners:
                owners.append(owner_label)
        if not owners or owners[-1] != label:
            owners.append(label)
        if len(owners) > 3:
            owners = owners[:2] + [owners[-1]]
        result.setdefault(_key(owners), target)
    if not result:
        raise CrawlError('WooCommerce 导航没有商品分类')
    if evidence_dir:
        save_json(Path(evidence_dir) / 'woocommerce-navigation.json', result)
    return result


def _category_id(html):
    body = parse_soup(html, 'lxml').body
    classes = body.get('class', []) if body else []
    for value in classes:
        match = re.fullmatch(r'term-(\d+)', str(value))
        if match:
            return match.group(1)
    match = re.search(r'(?i)product_cat[^\n]{0,200}?["\'](?:term_id|id)["\']\s*[:=]\s*["\']?(\d+)', html)
    if match:
        return match.group(1)
    raise CrawlError('WooCommerce 分类页缺少真实 term ID')


def collect_listing(client, url):
    page = client.get(url, timeout=45)
    category = _category_id(page.text)
    endpoint = urljoin(page.url, '/wp-json/wc/store/v1/products')
    rows, sources, seen = [], [], set()
    page_number = 1
    while True:
        source_url = query(endpoint, category=category, per_page=PAGE_SIZE, page=page_number)
        data = client.json(source_url)
        if not isinstance(data, list):
            raise CrawlError('WooCommerce Store API 列表不是数组')
        ids = [str(item.get('id')) for item in data if isinstance(item, dict)]
        if data and not (set(ids) - seen):
            raise CrawlError('WooCommerce Store API 分页重复')
        seen.update(ids)
        sources.append({'url': source_url, 'response': data})
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            break
        page_number += 1
    urls = [item.get('permalink') for item in rows
            if isinstance(item, dict) and isinstance(item.get('permalink'), str)]
    if not urls:
        raise CrawlError('WooCommerce 分类页没有可验证商品')
    return {'format': 'woocommerce-store-api-json', 'source_format': 'woocommerce_store_api',
            'url': url, 'category_id': category, 'urls': urls,
            'source_responses': sources, 'pages': [], 'complete': True}


def _amount(prices, name='price'):
    raw = prices.get(name)
    minor = prices.get('currency_minor_unit')
    if raw in (None, '') or not isinstance(minor, int):
        return None
    return str(Decimal(str(raw)) / (Decimal(10) ** minor))


def _normalize(raw):
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise CrawlError('WooCommerce 商品响应必须唯一')
    item = raw[0]
    prices = item.get('prices') or {}
    currency = prices.get('currency_code')
    price = _amount(prices)
    if currency != 'USD' or price is None:
        raise CrawlError('WooCommerce 商品缺少 USD 价格')
    images = [{'src': image['src'], 'variant_ids': [str(item['id'])]}
              for image in item.get('images', [])
              if isinstance(image, dict) and image.get('src')]
    if not images:
        raise CrawlError('WooCommerce 商品缺少图片')
    variant = {'id': str(item['id']), 'sku': str(item.get('sku') or item['id']),
               'price': price, 'compare_at_price': None,
               'inventory_quantity': '', 'available': bool(item.get('is_in_stock')),
               'featured_image': {'src': images[0]['src']}}
    regular = _amount(prices, 'regular_price')
    if regular is not None and Decimal(regular) > Decimal(price):
        variant['compare_at_price'] = regular
    return {'id': str(item['id']), 'handle': str(item.get('slug') or item['id']),
            'title': str(item.get('name') or ''), 'currency': currency,
            'body_html': str(item.get('description') or item.get('short_description') or ''),
            'vendor': '', 'options': [], 'variants': [variant], 'images': images}


def fetch_product(client, url, evidence):
    slug = urlsplit(url).path.rstrip('/').split('/')[-1]
    endpoint = query(urljoin(url, '/wp-json/wc/store/v1/products'), slug=slug)
    raw = client.json(endpoint)
    product = _normalize(raw)
    fingerprint = hashlib.sha256(endpoint.encode()).hexdigest()[:16]
    source_name = f'woocommerce-product-{fingerprint}.json'
    market_url = urljoin(url, '/wp-json/wc/store/v1/cart')
    market = client.json(market_url)
    market_name = 'woocommerce-market-' + hashlib.sha256(market_url.encode()).hexdigest()[:16] + '.json'
    evidence = Path(evidence); evidence.mkdir(parents=True, exist_ok=True)
    save_json(evidence / source_name, raw)
    save_json(evidence / market_name, market)
    return {'url': url, 'source': 'json', 'endpoint': endpoint, 'product': product,
            'source_mapping': {'kind': KIND, 'file': 'evidence/' + source_name,
                               'market_file': 'evidence/' + market_name}}


def _source(root, sample):
    path = (Path(root) / sample['source_mapping']['file']).resolve()
    if not path.is_relative_to((Path(root) / 'evidence').resolve()):
        raise CrawlError('WooCommerce 商品源越界')
    return read_json(path)


def verify_source(root, sample):
    if _normalize(_source(root, sample)) != sample.get('product'):
        raise CrawlError('WooCommerce 规范化商品与原始 Store API 不一致')


def product_market(root, sample):
    mapping = sample['source_mapping']
    path = (Path(root) / mapping['market_file']).resolve()
    if not path.is_relative_to((Path(root) / 'evidence').resolve()):
        raise CrawlError('WooCommerce 市场源越界')
    market = read_json(path)
    country = (market.get('shipping_address') or {}).get('country')
    currency = (market.get('totals') or {}).get('currency_code')
    if country != 'US' or currency != 'USD':
        raise CrawlError('WooCommerce 购物车未证明 US/USD')
    return country, currency


def validate_listing(listing, links):
    if [canonical(value) for value in listing.get('urls', [])] != [canonical(value) for value in links]:
        raise CrawlError('WooCommerce 列表顺序与详情 URL 不一致')
