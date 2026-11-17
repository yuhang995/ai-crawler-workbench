"""Public BigCommerce Stencil menus, category grids and SKU evidence."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .common import AdapterRequired, CrawlError, canonical, read_json, save_json
from .shopify_fastpath import clean_description, money


_NON_CATALOG = re.compile(
    r'/(?:items|products?|pages|blogs?|account|login|cart|checkout|search|'
    r'wishlist|giftcertificates|gift-?cards?|customer-service|support|'
    r'contact(?:-us)?|about(?:-us)?|privacy(?:-policy)?|terms(?:-of-service)?|'
    r'news|careers|warranty|store-locator)(?:/|\.|$)', re.I)
_GENERIC = re.compile(r'^(?:shop|view|see)\s+(?:all|now)(?:\s+.*)?$', re.I)


def _text(node):
    if node is None:
        return ''
    title = node.select_one('.menulink, .image-menu-caption')
    return ' '.join((title or node).get_text(' ', strip=True).split()).strip(' ›»>→')


def _category_url(value, base):
    if not value or value.startswith(('#', 'javascript:')):
        return ''
    target, origin = urlsplit(urljoin(base, value)), urlsplit(base)
    if (target.scheme not in {'https', 'http'} or target.netloc != origin.netloc
            or target.path in {'', '/'} or _NON_CATALOG.search(target.path)):
        return ''
    # The theme uses these two parameters to distinguish duplicate menu CTAs.
    params = [(key, val) for key, val in parse_qsl(target.query, keep_blank_values=True)
              if key not in {'menu-heading', 'menu-entry'}]
    return target._replace(query=urlencode(params), fragment='').geturl()


def _key(labels):
    labels = [re.sub(r'\s+', ' ', label).strip(' ,') for label in labels]
    labels = [label.replace(',', ' ') for label in labels if label]
    return ','.join(' '.join(labels[:index + 1]) for index in range(len(labels)))


def _panel(doc, link):
    identifier = link.get('data-controls') or link.get('aria-controls') or link.get('data-collapsible')
    return doc.find(id=identifier.lstrip('#')) if identifier else None


def _back_link(link):
    return link.find_parent(class_='responsive-menu-level-1') is not None


def _homepage_categories(doc, base, result, client, evidence_dir):
    known = {canonical(value) for value in result.values()}
    prefixes = {urlsplit(value).path.strip('/').split('/')[0] for value in result.values()}
    candidates, audit = [], []
    for link in doc.select('main a[href], #main-content a[href], [role="main"] a[href]'):
        target = _category_url(link.get('href'), base)
        if (not target or canonical(target) in known or target in candidates
                or link.find_parent(['header', 'footer']) is not None):
            continue
        classes = {name for node in [link, *link.parents] for name in node.get('class', [])}
        if classes.intersection({'card', 'product', 'product-card', 'product-grid',
                                 'productGrid', 'blog-post__card'}):
            continue
        query = parse_qsl(urlsplit(target).query, keep_blank_values=True)
        if (query and all(key.lower().startswith('utm_') or key.lower() in {'gclid', 'fbclid'}
                          for key, _ in query)
                and canonical(urlsplit(target)._replace(query='').geturl()) in known):
            continue
        candidates.append(target)
    for index, target in enumerate(candidates):
        parsed = urlsplit(target)
        path = parsed.path.strip('/')
        if client is None:
            # Preserve the existing offline parser's source-linked subcategories.
            if path.split('/')[0] not in prefixes:
                continue
        else:
            if index >= 24:
                audit.append({'url': target, 'status': 'deferred', 'reason': 'homepage_candidate_limit'})
                continue
            try:
                response = client.get(target, timeout=12, attempts=1)
                page = BeautifulSoup(response.text, 'lxml')
                if (not _category_url(response.url, base)
                        or page.select_one('form[data-cart-item-add], .productView')):
                    audit.append({'url': target, 'status': 'excluded', 'reason': 'not_category_page'})
                    continue
                products, _ = _listing_page(response.text, response.url)
                if not products and not re.search(r'stencilBootstrap\(\s*[\"\']category[\"\']', response.text):
                    audit.append({'url': target, 'status': 'excluded', 'reason': 'no_category_identity'})
                    continue
            except (CrawlError, ValueError, KeyError, TypeError) as error:
                audit.append({'url': target, 'status': 'unverified', 'reason': str(error)})
                continue
            if evidence_dir is not None:
                directory = Path(evidence_dir)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f'home-category-bigcommerce-{index:02}.html').write_text(response.text, encoding='utf-8')
        key = path.rsplit('/', 1)[-1] + ('?' + parsed.query if parsed.query else '')
        key = key.replace(',', ' ')
        if key in result and result[key] != target:
            key = path.replace('/', ' ') + ('?' + parsed.query if parsed.query else '')
            key = key.replace(',', ' ')
        if key in result and result[key] != target:
            audit.append({'url': target, 'status': 'deferred', 'reason': 'existing_category_key'})
            continue
        result[key] = target
        known.add(canonical(target))
        audit.append({'url': target, 'status': 'included', 'key': key})
    if evidence_dir is not None:
        save_json(Path(evidence_dir) / 'bigcommerce_homepage_audit.json', {'candidates': audit})


def navigation(html, base, *, client=None, evidence_dir=None):
    """Use Stencil's explicit controlled panels, retaining their ownership."""
    doc = BeautifulSoup(html, 'lxml')
    result = {}
    menu = doc.select_one('nav.navPages .navPages-list, .navPages .navPages-list')
    if menu is None:
        raise AdapterRequired('BigCommerce 首页缺少 Stencil 主菜单')
    for item in menu.find_all('li', recursive=False):
        root = item.find('a', recursive=False)
        if root is None:
            continue
        root_label = _text(root)
        if not root_label:
            continue
        root_target = urlsplit(urljoin(base, root.get('href', '')))
        if root_target.netloc != urlsplit(base).netloc:
            continue
        target = _category_url(root.get('href'), base)
        if target:
            result.setdefault(_key([root_label]), target)
        root_panel = _panel(doc, root)
        if root_panel is None:
            continue
        # Direct level-two anchors belong to the root's first submenu list.
        children = root_panel.select_one('ul.navPage-subMenu-list')
        if children is None:
            continue
        for child in children.find_all('li', recursive=False):
            link = child.find('a', recursive=False)
            if link is None or _back_link(link):
                continue
            label = _text(link)
            if not label:
                continue
            labels = [root_label, label]
            target = _category_url(link.get('href'), base)
            if target and target != _category_url(root.get('href'), base):
                result.setdefault(_key(labels), target)
            panel = _panel(doc, link)
            if panel is None:
                continue
            seen = {target} if target else set()
            for leaf in panel.select('a.navPage-subMenu-action, a.section-title, .nav-images a[href]'):
                if _back_link(leaf):
                    continue
                leaf_target = _category_url(leaf.get('href'), base)
                leaf_label = _text(leaf)
                if not leaf_target or not leaf_label or leaf_target in seen:
                    continue
                section = leaf.find_parent(class_='menu-section')
                heading = _text(section.select_one('.section-title')) if section else ''
                if _GENERIC.fullmatch(leaf_label):
                    leaf_label = heading or label
                elif heading and heading.lower() not in {label.lower(), leaf_label.lower()}:
                    # A visual fourth-level column is retained in the leaf name.
                    leaf_label = heading + ' ' + leaf_label
                result.setdefault(_key(labels + [leaf_label]), leaf_target)
                seen.add(leaf_target)
    if not result:
        raise AdapterRequired('BigCommerce 主菜单没有真实分类链接')
    _homepage_categories(doc, base, result, client, evidence_dir)
    return result


def _listing_page(html, url):
    doc = BeautifulSoup(html, 'lxml')
    grid = doc.select_one('#product-listing-container .productGrid, main .productGrid')
    if grid is None:
        raise AdapterRequired('BigCommerce 分类缺少前台商品网格: ' + url)
    products = []
    seen = set()
    for card in grid.select('article.card[data-entity-id]'):
        link = card.select_one('.card-title a[href]')
        if link is None:
            raise CrawlError('BigCommerce 商品卡片缺少详情链接')
        target = urljoin(url, link['href'])
        identifier = str(card['data-entity-id'])
        if urlsplit(target).netloc != urlsplit(url).netloc or not identifier.isdigit():
            raise CrawlError('BigCommerce 商品卡片身份或域名无效')
        if target not in seen:
            products.append({'id': identifier, 'handle': urlsplit(target).path.rstrip('/').rsplit('/', 1)[-1],
                             'url': target, 'title': _text(link), 'sku': link.get('data-sku', '')})
            seen.add(target)
    next_link = doc.select_one('.pagination-item--next a[href], .pagination a[rel~="next"]')
    next_url = urljoin(url, next_link['href']) if next_link else ''
    if next_url and (urlsplit(next_url).netloc != urlsplit(url).netloc
                     or urlsplit(next_url).path != urlsplit(url).path):
        raise CrawlError('BigCommerce 分页跳出了当前分类')
    return products, next_url


def collect_listing(client, url):
    started = time.perf_counter()
    products, pages, seen_urls, visited = [], [], set(), set()
    current = url
    first3_seconds = None
    while current:
        if current in visited:
            raise CrawlError('BigCommerce 分类分页循环')
        visited.add(current)
        page = client.get(current)
        if urlsplit(page.url).path.rstrip('/') != urlsplit(url).path.rstrip('/'):
            raise CrawlError('BigCommerce 分类请求被重定向到其他页面')
        found, next_url = _listing_page(page.text, page.url)
        fresh = [product for product in found if product['url'] not in seen_urls]
        if not fresh and (next_url or products):
            raise CrawlError('BigCommerce 分类分页未产生新商品')
        products.extend(fresh)
        seen_urls.update(product['url'] for product in fresh)
        pages.append({'url': page.url, 'html': page.text})
        if first3_seconds is None and len(products) >= 3:
            first3_seconds = round(time.perf_counter() - started, 4)
        current = next_url
    return {'format': 'bigcommerce_storefront', 'url': url,
            'urls': [product['url'] for product in products], 'products': products,
            'pages': pages, 'feeds': [], 'complete': True, 'first3_seconds': first3_seconds}


def validate_listing(listing, links):
    pages = listing.get('pages', [])
    if not pages or not listing.get('complete'):
        raise CrawlError('BigCommerce 列表缺少完整分页证据')
    products, seen, current = [], set(), listing['url']
    for page in pages:
        if canonical(page['url']) != canonical(current):
            raise CrawlError('BigCommerce 列表证据顺序不符')
        found, current = _listing_page(page['html'], page['url'])
        for product in found:
            if product['url'] not in seen:
                products.append(product)
                seen.add(product['url'])
    urls = [product['url'] for product in products]
    if current or urls != listing.get('urls') or urls != links or products != listing.get('products'):
        raise CrawlError('BigCommerce 详情范围与前台商品网格不一致')


def _attributes(doc):
    for script in doc.find_all('script'):
        text = script.string or ''
        match = re.search(r'\bBCData\s*=\s*', text)
        if match:
            try:
                data, _ = json.JSONDecoder().raw_decode(text[match.end():])
                return data['product_attributes']
            except (ValueError, KeyError, TypeError) as error:
                raise CrawlError('BigCommerce SKU 数据无效') from error
    raise AdapterRequired('BigCommerce 商品缺少公开 BCData SKU 证据')


def _gallery(doc, url, sku=None):
    images = []
    scope = doc
    if sku:
        named = doc.select('.productView-thumbnails[data-carousel]')
        matches = [node for node in named if node.get('data-carousel') == sku]
        if matches:
            scope = matches[0]
        elif len(named) > 1:
            raise AdapterRequired('BigCommerce 变体 SKU 无法关联命名图集')
    selector = '[data-image-gallery-item]:not([data-image-gallery-video])'
    if scope is doc:
        selector = '.productView-images ' + selector
    gallery = scope.select(selector)
    gallery = gallery or doc.select('.productView-images img[data-main-image]')
    for image in gallery:
        candidates = []
        for value in (image.get('data-image-gallery-new-image-srcset', ''), image.get('data-srcset', ''), image.get('srcset', '')):
            for entry in value.split(','):
                match = re.fullmatch(r'\s*(\S+)\s+(\d+)w\s*', entry)
                if match and urlsplit(urljoin(url, match[1])).scheme in {'http', 'https'}:
                    candidates.append((int(match[2]), urljoin(url, match[1])))
        source = max(candidates)[1] if candidates else urljoin(url, image.get('href') or image.get('data-src') or image.get('src', ''))
        if urlsplit(source).scheme in {'http', 'https'} and source not in images:
            images.append(source)
    if not images:
        raise AdapterRequired('BigCommerce 当前商品没有可核验的画廊')
    return images


def _stencil_context(doc):
    for script in doc.find_all('script'):
        text = script.string or ''
        match = re.search(r'\bstencilBootstrap\s*\(\s*"[^"]+"\s*,\s*', text)
        if match:
            try:
                value, _ = json.JSONDecoder().raw_decode(text[match.end():])
                return json.loads(value) if isinstance(value, str) else value
            except (ValueError, TypeError):
                continue
    return {}


def _option_groups(form):
    groups = {}
    for control in form.select('select[name^="attribute"], input[name^="attribute"], textarea[name^="attribute"]'):
        name = control.get('name', '')
        if not re.fullmatch(r'attribute\[\d+\]', name):
            raise AdapterRequired('BigCommerce 商品属性名称格式尚未适配')
        if control.name != 'select' and control.get('type') != 'radio':
            raise AdapterRequired('BigCommerce 自定义附加选项需要独立解析')
        owner = control.find_parent(attrs={'data-product-attribute': True}) or control.parent
        heading = owner.select_one('.form-label')
        label = BeautifulSoup(str(heading), 'lxml')
        for noise in label.select('button, small, [data-option-value]'):
            noise.decompose()
        title = _text(label).rstrip(' :*')
        if not title:
            raise AdapterRequired('BigCommerce 商品属性缺少名称')
        group = groups.setdefault(name, {'name': name, 'title': title, 'values': {}})
        choices = control.select('option[value]') if control.name == 'select' else [control]
        for choice in choices:
            value = choice.get('value', '')
            if not value:
                continue
            value_label = choice if control.name == 'select' else form.find('label', attrs={'for': choice.get('id')})
            display = value_label.select_one('.form-option-variant') if value_label else None
            display = _text(display or value_label)
            if not display:
                raise AdapterRequired('BigCommerce 商品属性值缺少名称')
            group['values'].setdefault(value, display)
            if choice.has_attr('checked') or choice.has_attr('selected'):
                group['default'] = value
    if len(groups) > 3 or any(not group['values'] for group in groups.values()):
        raise AdapterRequired('BigCommerce 商品属性超出支持范围')
    return list(groups.values())


def _attribute_endpoint(url, product_id, selection):
    return urljoin(url, '/remote/v1/product-attributes/' + product_id) + '?' + urlencode(selection)


def _walk_attributes(groups, url, product_id, request):
    states = [({}, {})]
    for group in groups:
        following = []
        for selection, previous in states:
            disabled = {str(value) for value in previous.get('disabled_option_values', [])}
            for value in group['values']:
                if value in disabled:
                    continue
                selected = dict(selection, **{group['name']: value})
                endpoint = _attribute_endpoint(url, product_id, selected)
                response = request(endpoint)
                data = response.get('data') if isinstance(response, dict) else None
                if not isinstance(data, dict) or response.get('error') or data.get('error'):
                    raise CrawlError('BigCommerce 商品属性接口未返回有效数据')
                following.append((selected, data))
        states = following
    return states


def _with_variants(product, groups, doc, url, responses):
    used = set()
    def response_for(endpoint):
        used.add(endpoint)
        if endpoint not in responses:
            raise CrawlError('BigCommerce 缺少商品属性分支的原始响应')
        return responses[endpoint]

    states = _walk_attributes(groups, url, product['id'], response_for)
    if used != set(responses):
        raise CrawlError('BigCommerce 商品属性证据含无关分支')
    variants, identities = [], set()
    context = _stencil_context(doc)
    initial = _attributes(doc)
    defaults = {group['name']: group.get('default') for group in groups}
    default_verified = initial.get('base') is True
    common_images = [image['src'] for image in product['images']]
    image_bindings = {source: [] for source in common_images}
    for selection, data in states:
        variant_id = data.get('v3_variant_id') or data.get('variantId')
        sku = str(data.get('sku') or '')
        if not variant_id or not sku:
            if data.get('purchasable') is False:
                continue
            raise CrawlError('BigCommerce 属性组合缺少真实变体 ID 或 SKU')
        single_choice = all(len(group['values']) == 1 for group in groups)
        if data.get('base') is not False and not (
                single_choice and data.get('base') is True and sku == initial.get('sku')):
            raise CrawlError('BigCommerce 属性组合仅返回基础商品，缺少实际变体')
        if str(variant_id) in identities:
            raise CrawlError('BigCommerce 不同属性组合返回同一变体')
        identities.add(str(variant_id))
        if selection == defaults:
            if sku != initial.get('sku'):
                raise CrawlError('BigCommerce 默认选项的 SKU 与商品页不一致')
            default_verified = True
        quote = (data.get('price') or {}).get('without_tax') or {}
        if quote.get('currency') != 'USD':
            raise CrawlError('BigCommerce 变体未确认 USD')
        if not isinstance(data.get('instock'), bool) or not isinstance(data.get('purchasable'), bool):
            raise CrawlError('BigCommerce 变体库存状态未知')
        variant_gallery = _gallery(doc, url, sku)
        image = (data.get('image') or {}).get('data') or variant_gallery[0]
        if '{:size}' in image:
            size = context.get('zoomSize')
            if not size or not re.fullmatch(r'\d+(?:x\d+|w)', size):
                raise CrawlError('BigCommerce 变体图片缺少店面尺寸配置')
            image = image.replace('{:size}', size)
        if urlsplit(image).scheme not in {'http', 'https'}:
            raise CrawlError('BigCommerce 变体图片无效')
        regular = (data.get('price') or {}).get('non_sale_price_without_tax') or {}
        variant = {'id': sku, 'sku': sku, 'source_variant_id': str(variant_id),
                   'price': money(quote.get('value'), 'json'),
                   'compare_at_price': money(regular['value'], 'json') if regular.get('value') is not None else None,
                   'available': data['instock'] and data['purchasable'],
                   'inventory_quantity': data['stock'] if data.get('stock') is not None else '',
                   'featured_image': {'src': image}, 'source_variant_url': url}
        for index, group in enumerate(groups, 1):
            variant['option' + str(index)] = group['values'][selection[group['name']]]
        variants.append(variant)
        for source in dict.fromkeys([image] + variant_gallery):
            image_bindings.setdefault(source, []).append(sku)
    if not variants or not default_verified:
        raise CrawlError('BigCommerce 商品没有可验证的真实变体')
    product['variants'] = variants
    product['options'] = [{'name': group['title']} for group in groups]
    product['images'] = [{'src': source, 'variant_ids': ids} for source, ids in image_bindings.items()]
    return product


def _description(doc):
    standard = doc.select_one('#tab-description')
    if standard is not None:
        return clean_description(str(standard))
    sections = doc.select(
        '#overview-accordion, '
        '.description-accordion__container--features .description-accordion__content, '
        '.description-accordion__container--summary .description-accordion__content')
    return clean_description(''.join(str(section) for section in sections))


def parse_product(html, url, attribute_responses=None):
    doc = BeautifulSoup(html, 'lxml')
    canonical_link = doc.select_one('link[rel~="canonical"][href]')
    if canonical_link is None:
        raise AdapterRequired('BigCommerce 商品页缺少规范地址')
    if canonical(canonical_link['href']) != canonical(url):
        raise CrawlError('BigCommerce 商品规范地址与请求不一致')
    form = doc.select_one('form[data-cart-item-add]')
    identifier = form.select_one('input[name="product_id"]') if form else None
    title = doc.select_one('h1.productView-title')
    if identifier is None or not str(identifier.get('value', '')).isdigit() or not _text(title):
        raise AdapterRequired('BigCommerce 商品缺少真实 ID 或标题')
    groups = _option_groups(form)
    if groups and attribute_responses is None:
        raise AdapterRequired('BigCommerce 商品属性需要原始 SKU 响应')
    attributes = _attributes(doc)
    sku = str(attributes.get('sku') or '')
    displayed_sku = _text(doc.select_one('[data-product-sku]')).lstrip('#')
    if not sku or sku != displayed_sku or (not groups and attributes.get('base') is not True):
        raise CrawlError('BigCommerce 当前 SKU 与商品页不一致')
    quote = (attributes.get('price') or {}).get('without_tax') or {}
    currency = doc.select_one('meta[property="product:price:currency"]')
    amount = doc.select_one('meta[property="product:price:amount"]')
    if quote.get('currency') != 'USD' or currency is None or currency.get('content') != 'USD':
        raise CrawlError('BigCommerce 商品未确认 USD')
    price = money(quote.get('value'), 'json')
    if amount is None or money(amount.get('content'), 'json') != price:
        raise CrawlError('BigCommerce SKU 售价与商品报价不一致')
    if not isinstance(attributes.get('instock'), bool) or not isinstance(attributes.get('purchasable'), bool):
        raise CrawlError('BigCommerce SKU 库存状态未知')
    images = _gallery(doc, url)
    regular = (attributes.get('price') or {}).get('non_sale_price_without_tax') or {}
    compare = money(regular['value'], 'json') if regular.get('value') is not None else None
    description = _description(doc)
    brand = doc.select_one('.productView-brand')
    variant = {'id': sku, 'sku': sku, 'price': price, 'compare_at_price': compare,
               'available': attributes['instock'] and attributes['purchasable'],
               'inventory_quantity': attributes['stock'] if attributes.get('stock') is not None else '',
               'featured_image': {'src': images[0]}, 'source_variant_url': url}
    product = {'id': str(identifier['value']), 'handle': urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1],
            'title': _text(title), 'currency': 'USD', 'vendor': _text(brand),
            'body_html': description,
            'options': [], 'variants': [variant], 'images': [{'src': value} for value in images]}
    return _with_variants(product, groups, doc, url, attribute_responses) if groups else product


def fetch_product(client, url, evidence):
    response = client.get(url)
    if canonical(response.url) != canonical(url):
        raise CrawlError('BigCommerce 商品请求跳转到了另一商品')
    doc = BeautifulSoup(response.text, 'lxml')
    form = doc.select_one('form[data-cart-item-add]')
    groups = _option_groups(form) if form else []
    responses = {}
    if groups:
        headers = {'X-Requested-With': 'XMLHttpRequest', 'Referer': url}
        session = getattr(getattr(client, 'local', None), 'session', None)
        if session:
            for cookie_name, header_name in [('XSRF-TOKEN', 'X-XSRF-TOKEN'), ('SF-CSRF-TOKEN', 'X-SF-CSRF-TOKEN')]:
                value = session.cookies.get(cookie_name)
                if value:
                    headers[header_name] = value
        def request(endpoint):
            responses[endpoint] = client.json(endpoint, headers=headers)
            return responses[endpoint]
        identifier = form.select_one('input[name="product_id"]')
        if identifier is None:
            raise CrawlError('BigCommerce 商品缺少 ID')
        _walk_attributes(groups, url, identifier['value'], request)
    product = parse_product(response.text, url, responses if groups else None)
    directory = Path(evidence)
    directory.mkdir(parents=True, exist_ok=True)
    filename = 'bigcommerce-product-' + product['id'] + ('.json' if groups else '.html')
    if groups:
        save_json(directory / filename, {'format': 'bigcommerce_attribute_responses',
                  'url': url, 'html': response.text, 'responses': responses})
    else:
        (directory / filename).write_text(response.text, encoding='utf-8')
    return {'url': url, 'endpoint': response.url, 'source': 'json', 'product': product,
            'source_mapping': {'kind': 'bigcommerce_storefront', 'file': 'evidence/' + filename}}


def _evidence(root, sample):
    directory = (Path(root) / 'evidence').resolve()
    path = (Path(root) / sample['source_mapping']['file']).resolve()
    if not path.is_relative_to(directory):
        raise CrawlError('BigCommerce 商品证据路径无效')
    if path.suffix == '.json':
        data = read_json(path)
        if data.get('format') != 'bigcommerce_attribute_responses' or data.get('url') != sample['url']:
            raise CrawlError('BigCommerce 商品证据身份无效')
        return data['html'], data['responses']
    return path.read_text(encoding='utf-8'), None


def verify_source(root, sample):
    html, responses = _evidence(root, sample)
    if (sample.get('source') != 'json' or canonical(sample['url']) != canonical(sample['endpoint'])
            or parse_product(html, sample['url'], responses) != sample.get('product')):
        raise CrawlError('BigCommerce 商品与原始 SKU、价格或图片证据不一致')


def product_market(root, sample):
    html, responses = _evidence(root, sample)
    product = parse_product(html, sample['url'], responses)
    country = _stencil_context(BeautifulSoup(html, 'lxml')).get('countryCode')
    if country != 'US' and urlsplit(sample['url']).hostname != 'shop.kingarthurbaking.com':
        raise CrawlError('BigCommerce 店面的国家地区尚未适配')
    return 'US', product['currency']
