"""Read the store's Nosto-sorted Hydrogen grid after its product cards hydrate."""
import re
import time
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup

from .common import CrawlError


def is_nosto_grid(html):
    return ('__reactRouterContext' in html and 'nosto-products-loading' in html
            and 'data-comp="CollectionGrid"' in html)


GRID = '[data-comp="CollectionGrid"]'
CARD = 'li[data-comp="ProductItem"]'
COUNTER = re.compile(r'^([0-9,]+) Products?$', re.I)


def _scope(url):
    parts = urlsplit(url)
    filters = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
               if key != 'page']
    return parts.scheme, parts.netloc, parts.path.rstrip('/'), sorted(filters)


def _read_page(html, url):
    doc = BeautifulSoup(html, 'lxml')
    grid = doc.select_one(GRID)
    if grid is None:
        raise CrawlError('商品网格未加载')
    count = doc.find(string=COUNTER)
    if not count:
        raise CrawlError('商品网格缺少分类总数')
    total = int(COUNTER.fullmatch(str(count))[1].replace(',', ''))
    links = []
    for card in grid.select(CARD):
        if card.has_attr('hidden') or card.get('aria-hidden') == 'true':
            continue
        targets = {urljoin(url, a['href']) for a in card.select('a[href]') if
                   re.search(r'/products/[^/]+/?$', urlsplit(a['href']).path)}
        if len(targets) != 1:
            raise CrawlError('商品卡缺少唯一的实际详情链接')
        target = targets.pop()
        a, b = urlsplit(target), urlsplit(url)
        if a.scheme != b.scheme or a.netloc != b.netloc:
            raise CrawlError('商品卡片离开当前店铺')
        links.append(target)
    current = grid.select_one('nav button[aria-current="page"]')
    number = current.get_text(strip=True) if current else '1'
    forward = grid.select_one('nav button[aria-label="Next page"]')
    if not number.isdigit() or bool(current) != bool(forward):
        raise CrawlError('商品网格分页状态不明确')
    return int(number), bool(forward and not forward.has_attr('disabled')), total, links


def _replay(pages, url, complete):
    links, total, more = [], None, True
    for expected, page in enumerate(pages, start=1):
        if not more or _scope(page['url']) != _scope(url):
            raise CrawlError('分类分页改变了筛选范围或超出末页')
        number, more, count, batch = _read_page(page['html'], page['url'])
        if number != expected or (total is not None and count != total) or (not batch and count):
            raise CrawlError('商品网格页码、总数或商品不符')
        total = count
        links.extend(batch)
    if (total is None or len(set(links)) != len(links) or len(links) > total
            or (complete and (more or len(links) != total))):
        raise CrawlError('商品网格存在重复、遗漏或未采集的下一页')
    return links


_READY = """([number, oldFirst]) => {
    const grid = document.querySelector('[data-comp="CollectionGrid"]');
    if (!grid || grid.classList.contains('nosto-products-loading')) return false;
    const current = grid.querySelector('nav button[aria-current="page"]');
    if (Number(current?.textContent || 1) !== number) return false;
    const cards = [...grid.querySelectorAll('li[data-comp="ProductItem"]')]
        .filter(c => !c.hidden && c.getAttribute('aria-hidden') !== 'true');
    const links = cards.map(c => c.querySelector('a[href*="/products/"]')?.href);
    return links.length ? links.every(Boolean) && links[0] !== oldFirst :
        [...document.querySelectorAll('main p')].some(p => /^0 Products?$/i.test(p.textContent.trim()));
}"""


def collect_listing(client, url, first_html=None, full=True):
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    from .shopify_fastpath import hydrogen_market

    started = time.perf_counter()
    if first_html is None:
        first_html = client.get(url).text
    if not is_nosto_grid(first_html) or hydrogen_market(first_html) != {'country': 'US', 'currency': 'USD'}:
        raise CrawlError('分类页未确认美国 Nosto 商品网格')
    pages, identities = [], {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(locale='en-US', viewport={'width': 1440, 'height': 1000})
                page.route('**/*', lambda route: route.abort() if route.request.resource_type in
                           {'image', 'media', 'font'} else route.continue_())

                def card_response(response):
                    parts = urlsplit(response.url)
                    if (parts.netloc != urlsplit(url).netloc or parts.path != '/api/product'
                            or response.status != 200):
                        return
                    try:
                        raw = response.json().get('product') or {}
                    except (ValueError, PlaywrightError):
                        return
                    gid = re.fullmatch(r'gid://shopify/Product/(\d+)', str(raw.get('id', '')))
                    if gid and raw.get('handle'):
                        identities[raw['handle']] = gid[1]

                page.on('response', card_response)
                response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
                if not response or response.status >= 400:
                    raise CrawlError('分类前台页面无法访问')
                old_first, first3_seconds = '', None
                while True:
                    if _scope(page.url) != _scope(url):
                        raise CrawlError('分类页已重定向至不同地区或筛选范围')
                    number = len(pages) + 1
                    page.wait_for_function(_READY, arg=[number, old_first], timeout=45000)
                    html = page.locator('main').evaluate('m => m.outerHTML')
                    current, more, total, links = _read_page(html, page.url)
                    if current != number or (not links and more):
                        raise CrawlError('分类分页未推进')
                    pages.append({'url': page.url, 'html': html})
                    if number == 1 and len(links) >= 3:
                        first3_seconds = round(time.perf_counter() - started, 3)
                    if not more or not full:
                        break
                    if number >= total:
                        raise CrawlError('分类分页超出商品总数')
                    old_first = links[0]
                    page.locator(GRID).get_by_role('button', name='Next page', exact=True).click(timeout=10000)
            finally:
                browser.close()
    except PlaywrightError as error:
        raise CrawlError('前台商品卡片或分类翻页未就绪: ' + str(error)) from error
    complete = not more
    links = _replay(pages, url, complete)
    products = []
    for link in links:
        slug = urlsplit(link).path.rstrip('/').rsplit('/', 1)[-1]
        product = {'handle': slug, '_hydrogen_partial': True, 'variants': []}
        if slug in identities:
            product['id'] = identities[slug]
        products.append(product)
    return {'format': 'brumate_nosto_grid', 'url': url, 'urls': links, 'products': products,
            'pages': pages, 'feeds': [], 'complete': complete, 'first3_seconds': first3_seconds,
            'order_evidence': 'rendered_nosto_grid'}


def validate_listing(listing, links, samples=()):
    observed = _replay(listing['pages'], listing['url'], True)
    if not listing.get('complete') or observed != links or observed != listing['urls']:
        raise CrawlError('分类顺序与实际渲染的 Nosto 网格不符')
    products = listing.get('products', [])
    handles = [urlsplit(link).path.rstrip('/').rsplit('/', 1)[-1] for link in links]
    if [item['handle'] for item in products] != handles:
        raise CrawlError('分类商品与前台卡片身份不符')
    by_url = dict(zip(links, products))
    for sample in samples:
        item = by_url.get(sample['url'])
        if item and (sample['product']['handle'] != item['handle'] or
                     (item.get('id') and str(sample['product']['id']) != item['id'])):
            raise CrawlError('详情商品与类别卡片身份不符')
