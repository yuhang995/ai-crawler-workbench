"""Read the selected Braun US model from its public BigCommerce/Contentful PDP."""
import json
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .common import CrawlError
from .shopify_fastpath import money


def _us_model_path(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != 'us.braun.com':
        raise CrawlError('Braun 商品链接不属于美国站')
    return parts.path.removeprefix('/en-us/').strip('/')


def _description(node):
    if node.get('nodeType') == 'text':
        text = escape(node.get('value', ''))
        for mark in node.get('marks', []):
            tag = {'bold': 'strong', 'italic': 'em'}.get(mark.get('type'))
            if tag:
                text = f'<{tag}>{text}</{tag}>'
        return text
    content = ''.join(_description(child) for child in node.get('content', []))
    tag = {'paragraph': 'p', 'unordered-list': 'ul', 'ordered-list': 'ol',
           'list-item': 'li'}.get(node.get('nodeType'))
    return f'<{tag}>{content}</{tag}>' if tag and content else content


def parse_braun_product(html, url):
    doc = BeautifulSoup(html, 'lxml')
    return _product(doc, url)


def _product(doc, url):
    canonical = doc.select_one('link[rel~="canonical"][href]')
    if (doc.html is None or str(doc.html.get('lang', '')).lower() != 'en-us' or canonical is None
            or not urlsplit(canonical['href']).path.startswith('/en-us/')
            or _us_model_path(canonical['href']) != _us_model_path(url)):
        raise CrawlError('Braun 商品页未确认美国市场及当前型号')
    state = doc.find('script', id='__NEXT_DATA__')
    try:
        content = json.loads(state.string)['props']['pageProps']['content']
        page = content['page']
        item = page['bigCProductDetails']
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise CrawlError('Braun 商品缺少可复验的 BigCommerce 数据') from error
    if content.get('type') != 'product' or not item.get('id') or not page.get('sku'):
        raise CrawlError('Braun 页面缺少当前型号或 SKU')
    models = []
    for script in doc.select('script[type="application/ld+json"]'):
        try:
            schema = json.loads(script.string or '')
        except ValueError:
            continue
        if isinstance(schema, dict) and schema.get('@type') == 'Product':
            models.append(schema)
    if len(models) != 1:
        raise CrawlError('Braun 商品页缺少唯一 Product 声明')
    model = models[0]
    sku = str(page['sku'])
    offer = model.get('offers') or {}
    path = _us_model_path(url)
    if (str(model.get('sku')) != sku or not page.get('productName')
            or page['productName'] != model.get('name') or not page.get('internalLink')
            or _us_model_path(urljoin(url, page['internalLink'])) != path
            or offer.get('@type') != 'Offer' or _us_model_path(offer.get('url', '')) != path):
        raise CrawlError('Braun 当前型号、SKU 或报价地址不一致')
    if (offer.get('priceCurrency') != 'USD' or
            money(offer.get('price'), 'json') != money(item.get('calculatedPrice'), 'json')):
        raise CrawlError('Braun USD 报价与 BigCommerce 实际售价不一致')
    regular = money(item.get('price'), 'json')
    price = money(item['calculatedPrice'], 'json')
    availability = str(offer.get('availability', '')).rsplit('/', 1)[-1]
    if availability not in {'InStock', 'OutOfStock', 'SoldOut'}:
        raise CrawlError('Braun 商品库存状态未知')
    available = availability == 'InStock'
    if available and (item.get('availability') == 'disabled' or item.get('isVisible') is False):
        raise CrawlError('Braun 前端与 BigCommerce 库存状态不一致')
    # Subscription frequency is a purchase modifier with no separate SKU.
    # Other product configurations need their own identity and price evidence.
    modifiers = (item.get('modifiers') or {}).get('data')
    if not isinstance(modifiers, list) or any(
        str(mod.get('product_id')) != str(item['id']) or mod.get('required')
        or str(mod.get('display_name', '')).strip().lower() != 'ships every'
        or any((value.get('adjusters') or {}).get('price') for value in mod.get('option_values', []))
        for mod in modifiers
    ):
        raise CrawlError('Braun 商品含未核验的购买选项')
    images = [model.get('image')]
    for media in page.get('productImageVideo', []):
        fields = media.get('fields', {})
        if fields.get('contentType') == 'image' and fields.get('desktop'):
            images.append(fields['desktop'])
    gallery = list(dict.fromkeys(urljoin(url, image) for image in images if isinstance(image, str) and image))
    if not gallery or any(urlsplit(image).scheme != 'https' for image in gallery):
        raise CrawlError('Braun 当前商品缺少可核验的图片')
    variant = {'id': sku, 'sku': sku, 'price': price,
               'compare_at_price': regular if regular != price else None,
               'available': available, 'inventory_quantity': item.get('inventoryLevel', ''),
               'featured_image': {'src': gallery[0]}}
    return {'id': str(item['id']), 'handle': path.rsplit('/', 1)[-1],
            'title': page['productName'], 'currency': 'USD',
            'body_html': _description(page.get('productDescription') or {})
                         or escape(str(model.get('description') or '')),
            'vendor': (model.get('brand') or {}).get('name') or '',
            'options': [], 'variants': [variant], 'images': [{'src': image} for image in gallery]}


def braun_product(client, url, evidence):
    page = client.get(url)
    if _us_model_path(page.url) != _us_model_path(url):
        raise CrawlError('Braun 商品页重定向到了不同型号')
    product = parse_braun_product(page.text, url)
    directory = Path(evidence)
    directory.mkdir(parents=True, exist_ok=True)
    filename = 'braun-product-' + str(int(product['id'])) + '.html'
    (directory / filename).write_text(page.text, encoding='utf-8')
    return {'url': url, 'source': 'json', 'endpoint': page.url, 'product': product,
            'source_mapping': {'kind': 'braun_bigcommerce', 'file': 'evidence/' + filename}}


def _evidence_html(root, sample):
    evidence = (Path(root) / 'evidence').resolve()
    path = (Path(root) / sample['source_mapping']['file']).resolve()
    if not path.is_relative_to(evidence):
        raise CrawlError('Braun 商品页证据路径无效')
    return path.read_text(encoding='utf-8')


def braun_product_market(root, sample):
    doc = BeautifulSoup(_evidence_html(root, sample), 'lxml')
    product = _product(doc, sample['url'])
    return doc.html['lang'].rsplit('-', 1)[-1].upper(), product['currency']


def verify_braun_product(root, sample):
    if (sample.get('source') != 'json' or _us_model_path(sample['url']) != _us_model_path(sample['endpoint'])
            or parse_braun_product(_evidence_html(root, sample), sample['url']) != sample['product']):
        raise CrawlError('Braun 商品与原始美国站 PDP 不一致')
