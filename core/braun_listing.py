"""Read a Contentful storefront's public category grid in displayed order."""
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .common import CrawlError


def _market_url(value, page_url):
    page = urlsplit(page_url)
    target = urlsplit(urljoin(page_url, value))
    if (not isinstance(value, str) or not value.startswith(('/', 'https://'))
            or target.scheme != page.scheme or target.netloc != page.netloc):
        raise CrawlError('商品卡片链接不属于当前市场: ' + str(value))
    market = re.match(r'^/([a-z]{2}-[a-z]{2})(?:/|$)', page.path)
    if not market:
        raise CrawlError('分类地址缺少市场路径')
    prefix = '/' + market[1]
    if not target.path.startswith(prefix + '/'):
        if re.match(r'^/[a-z]{2}-[a-z]{2}(?:/|$)', target.path):
            raise CrawlError('商品卡片切换了市场')
        target = target._replace(path=prefix + target.path)
    return target.geturl()


def _product_fields(entry):
    return entry.get('fields', entry)


def _card_source(entries, page_url, field):
    result = {}
    for item in entries:
        sku, path = item.get('sku'), item.get(field)
        if not sku or not path:
            raise CrawlError('商品卡片缺少 SKU 或链接')
        url = _market_url(path, page_url)
        if result.setdefault(url, str(sku)) != str(sku):
            raise CrawlError('同一商品链接对应多个 SKU')
    return result


def _read_listing(html, page_url):
    document = BeautifulSoup(html, 'lxml')
    script = document.find('script', id='__NEXT_DATA__')
    try:
        page = json.loads(script.string)['props']['pageProps']['content']['page']
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CrawlError('分类页缺少公开商品数据') from exc
    main = document.find('main')
    if main is None:
        raise CrawlError('分类页缺少商品主体')
    for node in main.select('template, noscript, [hidden], [aria-hidden="true"]'):
        node.decompose()
    if page.get('isListingPage'):
        products = _card_source(page.get('algoliaInitialResults', []), page_url, 'slug')
        cards = main.select('[class*="_plpGrid__"] [class*="_productCard__"][data-action-detail]')
        # Category banner tiles (e.g. "Shop Now") share the card class.
        observed = [card['data-action-detail'] for card in cards
                    if card['data-action-detail'].startswith(('/', 'https://'))]
        counter = main.select_one('[class*="_totalProductsLabel__"] b')
        count = counter.get_text(strip=True) if counter else ''
        if not count.isdigit() or int(count) != len(products):
            raise CrawlError('分类商品总数与已加载商品不一致')
        kind = 'product_grid'
    elif page.get('categoryMegaPDPs'):
        # Series overview cards link to one buyable model per series. Other
        # variants in the search index and a separate bestseller shelf do not
        # occur in this product range.
        entries = [_product_fields(_product_fields(group).get('pdpLink') or {})
                   for group in page['categoryMegaPDPs']]
        products = _card_source(entries, page_url, 'internalLink')
        observed = [a['href'] for a in main.select('[class*="_cards__"] a.event_buy_now[href]')]
        kind = 'series_cards'
    else:
        # Editorial storefronts can have explicit, finite product shelves.
        # Only shelves with matching rendered product cards count.
        entries = []
        for section in page.get('pageContent', []):
            for block in _product_fields(section).get('sectionContent', []):
                fields = _product_fields(block)
                if fields.get('contentType') == 'shelf007':
                    entries.extend(_product_fields(item) for item in fields.get('products', []))
        if not entries:
            raise CrawlError('当前导购页没有可核对的商品网格或系列商品卡')
        products = _card_source(entries, page_url, 'internalLink')
        observed = [a['href'] for a in main.select('[class*="_productList__"] .productShelfCard a[href]')]
        kind = 'product_shelves'
    order = list(dict.fromkeys(_market_url(raw, page_url) for raw in observed))
    if set(order) != set(products):
        raise CrawlError('分类卡片与该页公开商品数据不一致')
    return kind, {url: products[url] for url in order}


def collect_listing(client, url):
    started = time.perf_counter()
    response = client.get(url, timeout=45)
    if _market_url(url, response.url).rstrip('/') != response.url.rstrip('/'):
        raise CrawlError('分类页重定向改变了商品范围')
    kind, cards = _read_listing(response.text, response.url)
    return {'format': 'braun', 'url': response.url, 'complete': True,
            'urls': list(cards), 'source_skus': cards, 'order_evidence': kind,
            'products': [], 'feeds': [], 'pages': [{'url': response.url, 'html': response.text}],
            'first3_seconds': round(time.perf_counter() - started, 4) if len(cards) >= 3 else None}


def validate_listing(listing, links):
    pages = listing.get('pages') or []
    if len(pages) != 1 or pages[0].get('url') != listing['url']:
        raise CrawlError('分类原始响应的范围不符')
    kind, cards = _read_listing(pages[0]['html'], pages[0]['url'])
    if (not listing.get('complete') or kind != listing.get('order_evidence')
            or listing.get('source_skus') != cards or list(cards) != listing.get('urls')
            or list(cards) != links):
        raise CrawlError('商品顺序或 SKU 与原始分类响应不一致')
