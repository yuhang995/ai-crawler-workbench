"""Public Magento/Hyva menu, product-grid and configurable-product sources."""
from __future__ import annotations

import html as html_lib
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from .common import CrawlError, read_json, save_json
from .custom_navigation import _comparison_url
from .html_parser import parse_soup


def _text(node):
    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''


def _local(value, base):
    if not value or value.startswith(('#', 'javascript:', 'mailto:')):
        return ''
    target = urljoin(base, html_lib.unescape(value))
    if urlsplit(target).hostname != urlsplit(base).hostname:
        return ''
    return target


def _key(labels):
    clean = [' '.join(label.replace(',', ' ').split()) for label in labels if label.strip()]
    return ','.join(' '.join(clean[:i + 1]) for i in range(len(clean)))


def _catalog_url(value, base):
    target = _local(value, base)
    if not target or re.search(r'gift.?card|/(?:customer(?:-service)?|checkout|account|stores?|store-locator|blog|careers|faq|warranty|company-info|community|challenge[^/]*|i-am-511|511-uniforms|trackorder)(?:/|\.|$)', target, re.I):
        return ''
    return target


def _load_menu_fragments(soup, base, client, evidence_dir):
    """Follow menu-fragment GETs explicitly declared by the loaded page."""
    if client is None:
        return
    for script in list(soup.find_all('script')):
        source = script.string or script.get_text()
        target = re.search(r"getElementById\(['\"]([^'\"]+)['\"]\)\.innerHTML\s*=", source)
        endpoint = re.search(r"fetch\(\s*BASE_URL\s*\+\s*['\"]([^'\"]+)['\"]", source)
        if not target or not endpoint:
            continue
        container = soup.find(id=target[1])
        if container is None or container.name != 'nav':
            continue
        declared_base = re.search(r"\bBASE_URL\s*=\s*['\"]([^'\"]+)['\"]", str(soup))
        origin = declared_base[1] if declared_base else base
        origin = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m[1], 16)), origin)
        url = _local(origin.rstrip('/') + '/' + endpoint[1].lstrip('/'), base)
        if not url:
            continue
        response = client.get(url, timeout=25)
        fragment = parse_soup(response.text)
        if not fragment.select('a[href]'):
            raise CrawlError('Magento 动态菜单响应没有链接: ' + url)
        container.clear()
        for node in list((fragment.body or fragment).contents):
            container.append(node)
        if evidence_dir is not None:
            directory = Path(evidence_dir)
            directory.mkdir(parents=True, exist_ok=True)
            suffix = hashlib.sha256(url.encode()).hexdigest()[:12]
            (directory / f'home-menu-native-{suffix}.html').write_text(str(soup), encoding='utf-8')
            save_json(directory / f'native-menu-{suffix}.json', {'url': url, 'html': response.text})


def _bem_menu(soup, base):
    result, ambiguous = {}, set()
    structural_urls = set()
    for nav in soup.select('nav.main.navigation, nav.navigation'):
        if 'mobile' in nav.get('class', []):
            continue
        for root in nav.select(':scope > ul.main__list > li, :scope > ul.nav__list > li'):
            trigger = root.find('a', recursive=False)
            title = _text(trigger)
            if not trigger or not title:
                continue
            if root.select_one('a[data-url][href]'):
                structural_urls.add(_catalog_url(trigger.get('href'), base))
            anchors = [trigger] + root.select('a.main__inner-link[href], a.nav__inner-link[href]')
            for anchor in anchors:
                url = _catalog_url(anchor.get('href'), base)
                if not url:
                    continue
                if ('nav__inner-link--parent' in anchor.get('class', [])
                        and anchor.parent.select_one('a[data-url][href]')):
                    structural_urls.add(url)
                labels = [title]
                group = anchor.find_parent(class_='nav__parent-container')
                if group is not None:
                    ancestors = []
                    for owner in anchor.parents:
                        if owner is root:
                            break
                        if 'nav__parent-container' not in owner.get('class', []):
                            continue
                        heading = owner.find(class_=re.compile(r'^(?:nav__sub-title|nav__inner-link--parent)$'), recursive=False)
                        if heading is not anchor and _text(heading):
                            ancestors.append(_text(heading))
                    labels.extend(reversed(ancestors))
                else:
                    ancestors = []
                    owner = anchor.find_parent('li')
                    while owner is not None and owner is not root:
                        heading = owner.find('a', recursive=False)
                        if heading is not anchor and _text(heading):
                            ancestors.append(_text(heading))
                        owner = owner.find_parent('li')
                    labels.extend(reversed(ancestors))
                if anchor is not trigger:
                    text = _text(anchor)
                    if not text:
                        continue
                    generic = re.fullmatch(r'(?:shop|view|see|browse)\s+(?:all|now)(?:\s+.*)?', text, re.I)
                    if generic:
                        if _key(labels) in result and result[_key(labels)] != url:
                            text = re.sub(r'^(?:shop|view|see|browse)\s+', '', text, flags=re.I)
                        else:
                            text = labels[-1]
                    if text != labels[-1]:
                        labels.append(text)
                if len(labels) > 3:
                    labels = labels[:2] + [' '.join(labels[2:])]
                key = _key(labels)
                result.setdefault(key, url)
                path = urlsplit(url).path
                if (('main__link' in anchor.get('class', []) or 'main__inner-link' in anchor.get('class', []))
                        and (not path.endswith('.html') or len(path.strip('/').split('/')) < 2)):
                    ambiguous.add(url)
                if ('nav__inner-link' in anchor.get('class', []) and not anchor.has_attr('data-url')
                        and 'nav__inner-link--parent' not in anchor.get('class', [])):
                    ambiguous.add(url)
    return result, ambiguous - structural_urls


def navigation(html, base, client=None, evidence_dir=None):
    """Preserve the actual ownership encoded by Magento's desktop menu panels."""
    soup = parse_soup(html)
    _load_menu_fragments(soup, base, client, evidence_dir)
    result = {}

    def add(labels, anchor):
        if anchor is None:
            return
        target = _catalog_url(anchor.get('href', ''), base)
        if not target:
            return
        result.setdefault(_key(labels), target)

    for root in soup.select('#primaryNav > li'):
        trigger = root.find('a', recursive=False)
        label = _text(trigger)
        if not label or label.lower() in {'stay connected', 'support'}:
            continue
        add([label], trigger)
        for link in root.select('.side-menu a[href]'):
            title = _text(link)
            if title and not re.search(r'challenge|i am 5\.11|stories|careers', title, re.I):
                add([label, title], link)
        for group in root.select('.dropdown-section'):
            heading = group.select_one('a.section-heading')
            title = _text(heading)
            if not title or title.startswith('Featured'):
                continue
            if re.fullmatch(r'(?:shop|view) all', title, re.I) and heading.get('href') == trigger.get('href'):
                continue
            add([label, title], heading)
            for link in group.select('ul.sub-menu a[href]'):
                child = _text(link)
                if child and child != title and not link.find_parent(class_='panel-back'):
                    add([label, title, child], link)

    for root in soup.select('.c-mega-nav__list > .c-mega-nav__item'):
        trigger = root.select_one('.c-mega-nav__link')
        label = _text(trigger)
        if not label or label.lower() in {'explore', 'customize'}:
            continue
        all_link = root.select_one('a.c-mega-panel__featured-link--all')
        add([label], all_link)
        for link in root.select('.c-mega-panel__featured-list a[href]'):
            child = _text(link.select_one('.c-mega-panel__label')) or _text(link)
            if not re.match(r'(?:shop|view) all', child, re.I):
                add([label, child], link)
        for group in root.select('.c-mega-panel__column--accordion'):
            title = _text(group.select_one('.c-mega-panel__heading'))
            for link in group.select('a.c-mega-panel__link[href]'):
                # Hyva also places individual featured bottles in its size menu.
                path = urlsplit(link['href']).path
                if not path.startswith('/shop/') and (
                        title.upper() == 'POPULAR SIZES' or re.search(r'\b\d+-oz\b', path)):
                    continue
                child = _text(link.select_one('.c-mega-panel__label')) or _text(link)
                if child:
                    add([label, title, child], link)

    bem, ambiguous = _bem_menu(soup, base)
    if client is not None and ambiguous:
        known_paths = {urlsplit(url).path.rstrip('/') for url in bem.values() if url not in ambiguous}
        def is_listing(url):
            try:
                response = client.get(url, timeout=20)
                page = parse_soup(response.text)
                if page.select_one('#product_addtocart_form'):
                    return False
                if _grid(response.text, response.url, soup=page)[0]:
                    return True
                main = page.find('main')
                if main is None:
                    return False
                linked_paths = {urlsplit(_local(anchor['href'], base)).path.rstrip('/')
                                for anchor in main.select('a[href]')
                                if not anchor.find_parent(class_=re.compile(r'(?:^|[-_])seo(?:$|[-_])', re.I))}
                parent_keys = [key for key, value in bem.items() if value == url]
                child_paths = {urlsplit(value).path.rstrip('/') for key, value in bem.items()
                               if value not in ambiguous and any(key.startswith(parent + ',') for parent in parent_keys)}
                return bool(linked_paths & child_paths) or len(linked_paths & known_paths) >= 2
            except CrawlError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            confirmed = dict(zip(sorted(ambiguous), pool.map(is_listing, sorted(ambiguous))))
        bem = {key: url for key, url in bem.items() if url not in confirmed or confirmed[url]}
    result.update(bem)

    main = soup.find('main')
    if main is not None and client is not None:
        known = {_comparison_url(value) for value in result.values()}
        candidates = []
        product_container = re.compile(
            r'^(?:product-item|product-tile|product__container|product__link|products-grid|related|upsell|crosssell|recommendations)$'
            r'|(?:^|-)product-carousel(?:-|$)')
        for link in main.select('a[href]'):
            target = _catalog_url(link['href'], base)
            if (not target or _comparison_url(target) in known or target in candidates
                    or urlsplit(target).path == '/'
                    or link.find_parent(class_=product_container)
                    or link.find_parent(attrs={'data-product-id': True})):
                continue
            candidates.append(target)

        def confirmed_category(target):
            try:
                response = client.get(target, timeout=20)
                if not _local(response.url, base) or urlsplit(response.url).path == '/':
                    return None
                page = parse_soup(response.text)
                # A PDP may also contain related-product grids. Its own page
                # identity excludes those grids from category confirmation.
                if page.select_one('body.catalog-product-view, #product_addtocart_form'):
                    return None
                links, _ = _grid(response.text, response.url, soup=page)
                if links:
                    return target
            except CrawlError:
                return None
            return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            for target in pool.map(confirmed_category, candidates):
                if target is None:
                    continue
                parts = urlsplit(target)
                slug = parts.path.rstrip('/').rsplit('/', 1)[-1].removesuffix('.html')
                if parts.query:
                    slug += '?' + urlencode(parse_qsl(parts.query, keep_blank_values=True))
                if slug in result and result[slug] != target:
                    slug = parts.path.strip('/').removesuffix('.html').replace('/', '-') + ('?' + parts.query if parts.query else '')
                result.setdefault(slug, target)
    return result


def _grid(html, base, *, soup=None):
    soup = soup if soup is not None else parse_soup(html)
    urls = []
    for grid in soup.select('.products-grid,main .banner__products,main .product-slider'):
        if grid.find_parent(class_=re.compile(r'upsell|related|recommend', re.I)):
            continue
        for link in grid.select('a.product-item-link[href], a.product__link[href], .product__link a.product__image-link[href]'):
            target = _local(link['href'], base)
            if target and target not in urls:
                urls.append(target)
    next_link = soup.select_one('.pages a.next[href], .pager a.next[href], a[rel~="next"][href], main a.next[href]')
    next_link = next_link or soup.select_one('link[rel~="next"][href]')
    # The parser has already decoded HTML entities in href; resolve once.
    following = urljoin(base, next_link['href']) if next_link else ''
    if (urlsplit(following).scheme not in {'http', 'https'}
            or urlsplit(following).netloc != urlsplit(base).netloc):
        following = ''
    following = urlsplit(following)._replace(fragment='').geturl()
    return urls, following


def collect_listing(client, url):
    started = time.perf_counter()
    pages, urls, visited = [], [], set()
    next_url = url
    while next_url:
        if next_url in visited:
            raise CrawlError('Magento 商品分页循环')
        visited.add(next_url)
        try:
            response = client.get(next_url, timeout=45)
        except CrawlError as error:
            if not pages or type(error) is not CrawlError:
                raise
            raise CrawlError(f'Magento 下一页请求失败: {next_url}；{error}') from error
        found, following = _grid(response.text, response.url)
        if not found:
            raise CrawlError('Magento 分类页没有可验证的商品网格: ' + response.url)
        fresh = [item for item in found if item not in urls]
        if following and not fresh:
            raise CrawlError('Magento 分页未返回新增商品')
        urls.extend(fresh)
        pages.append({'url': response.url, 'html': response.text})
        next_url = following
    return {'url': pages[0]['url'], 'format': 'magento_storefront', 'complete': True,
            'urls': urls, 'catalog_urls': urls, 'products': [], 'feeds': [],
            'pages': pages, 'first3_seconds': round(time.perf_counter() - started, 4)}


def validate_listing(listing, links):
    observed = []
    pages = listing.get('pages', [])
    if not pages or not listing.get('complete'):
        raise CrawlError('Magento 商品列表缺少完整分页证据')
    for i, page in enumerate(pages):
        found, following = _grid(page['html'], page['url'])
        expected = pages[i + 1]['url'] if i + 1 < len(pages) else ''
        if following != expected:
            raise CrawlError('Magento 商品分页证据不连续')
        observed.extend(item for item in found if item not in observed)
    if observed != links or listing.get('urls') != links:
        raise CrawlError('Magento 商品顺序与原始网格不一致')


def _objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _objects(item)


def _configuration(soup, product_id):
    for node in soup.select('[data-product-id][data-config]'):
        if str(node.get('data-product-id')) != product_id:
            continue
        try:
            config = json.loads(node['data-config'])
        except (ValueError, TypeError):
            continue
        if isinstance(config, dict) and str(config.get('productId')) == product_id:
            return config
    for script in soup.select('script[type="text/x-magento-init"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for obj in _objects(data):
            config = obj.get('jsonConfig')
            if isinstance(config, dict) and str(config.get('productId')) == product_id:
                return config
    pattern = r'initConfigurableOptions\(\s*[\'\"]' + re.escape(product_id) + r'[\'\"]\s*,\s*'
    for script in soup.find_all('script'):
        source = script.string or script.get_text()
        match = re.search(pattern, source)
        if match:
            try:
                config = json.JSONDecoder().raw_decode(source[match.end():])[0]
            except ValueError:
                continue
            if isinstance(config, dict):
                return config
    return None


def _description(soup):
    node = soup.select_one('.product-description-full, #description-content .description, .product.attribute.description .value, [itemprop="description"]')
    if node is None:
        node = soup.select_one('.product.attribute.overview .value')
    if node is None:
        return ''
    for link in node.select('a'):
        link.unwrap()
    for unwanted in node.select('script,style,iframe,form'):
        unwanted.decompose()
    return node.decode_contents().strip()


def _gallery(items, base):
    return list(dict.fromkeys(urljoin(base, item.get('full') or item.get('img') or item.get('url'))
                             for item in items if isinstance(item, dict)
                             and item.get('type', 'image') == 'image'
                             and (item.get('full') or item.get('img') or item.get('url'))))


def _variant_gallery(config, child_id, base, cache):
    images = config.get('images')
    if isinstance(images, dict):
        gallery = _gallery(images.get(child_id, []), base)
        if gallery:
            return gallery
    for attribute_id, attribute in config['attributes'].items():
        if str(attribute.get('code') or attribute.get('label', '')).lower() not in {'color', 'colour'}:
            continue
        color_id = str(config['index'][child_id][attribute_id])
        color = config.get('color_variants', {}).get(color_id, {})
        gallery = _gallery(color.get('gallery', []), base)
        if gallery:
            return gallery
    source = config.get('magictoolbox', {}).get('galleryData', {}).get(child_id)
    if isinstance(source, str):
        if source not in cache:
            gallery = parse_soup(source)
            cache[source] = list(dict.fromkeys(urljoin(base, node.get('data-image') or node['href'])
                for node in gallery.select('a.MagicZoom[href], a[data-image]')
                if node.get('data-image') or node.get('href')))
        return cache[source]
    return []


def _page_gallery(soup, url):
    gallery_node = soup.select_one('[id="gallery"][x-data]')
    gallery = []
    if gallery_node is not None:
        function = gallery_node['x-data'].split('(', 1)[0].strip()
        for script in soup.find_all('script'):
            source = script.string or script.get_text()
            if not re.search(r'function\s+' + re.escape(function) + r'\s*\(', source):
                continue
            match = re.search(r'\bimages\s*:\s*(\[)', source)
            if match:
                try:
                    gallery = _gallery(json.JSONDecoder().raw_decode(source[match.start(1):])[0], url)
                except ValueError:
                    pass
    if not gallery:
        gallery = list(dict.fromkeys(urljoin(url, a['href']) for a in soup.select('.gallery__grid a.MagicZoom[href]')))
    if not gallery:
        # Hyva can server-render the current product gallery as ordinary images
        # without x-data image JSON or MagicZoom anchors.
        gallery = list(dict.fromkeys(
            urljoin(url, node.get('src'))
            for node in soup.select('img[data-index][src*="/media/catalog/product/"]')
            if node.get('src') and not node.get('src').startswith('data:')))
    if not gallery:
        social = soup.select_one('meta[property="og:image"][content]')
        if social is not None:
            gallery = [urljoin(url, social.get('content'))]
    return gallery


def _simple_product(soup, product_id, url):
    if ('page-product-configurable' in (soup.body.get('class', []) if soup.body else [])
            or soup.select_one('[name^="super_attribute["], [name^="options["]')):
        raise CrawlError('Magento 可选商品缺少真实子商品映射: ' + url)
    sku = soup.select_one('[itemprop="sku"][content]')
    price = soup.select_one('[itemprop="price"][content]')
    # Hyva/newer Magento themes often replace schema.org itemprop nodes with
    # the add-to-cart form SKU and Magento's own price-box attributes.  Both
    # values are server-rendered, product-scoped source evidence.
    sku_value = (sku.get('content') if sku else '')
    if not sku_value:
        sku_node = soup.select_one('#product_addtocart_form[data-product-sku], [data-product-sku]')
        sku_value = sku_node.get('data-product-sku', '') if sku_node else ''
    price_value = (price.get('content') if price else '')
    if not price_value:
        price_node = soup.select_one(
            '#product_addtocart_form [data-price-type="finalPrice"][data-price-amount], '
            '[data-price-type="finalPrice"][data-price-amount]')
        price_value = price_node.get('data-price-amount', '') if price_node else ''
    if not price_value:
        price_meta = soup.select_one('meta[property="product:price:amount"][content]')
        price_value = price_meta.get('content', '') if price_meta else ''
    availability = soup.select_one('[itemprop="availability"]')
    if not sku_value or not price_value:
        raise CrawlError('Magento 单品缺少 SKU 或价格源证据: ' + url)
    gallery = _page_gallery(soup, url)
    if not gallery:
        raise CrawlError('Magento 单品缺少当前商品图集: ' + url)
    amount = price_value
    try:
        valid_amount = float(amount) >= 0
    except (TypeError, ValueError):
        valid_amount = False
    if not valid_amount:
        raise CrawlError('Magento 单品源价格无效')
    variant = {'id': product_id, 'sku': sku_value, 'price': str(amount),
               'compare_at_price': None, 'inventory_quantity': '',
               'featured_image': {'src': gallery[0]}, 'source_variant_url': url}
    if availability:
        status = availability.get('content') or availability.get('href', '')
        if status.endswith(('/InStock', '/OutOfStock')):
            variant['available'] = status.endswith('/InStock')
    return {'id': product_id, 'handle': urlsplit(url).path.strip('/').removesuffix('.html'),
            'title': _text(soup.select_one('h1')), 'currency': 'USD', 'body_html': _description(soup),
            'vendor': '', 'options': [], 'variants': [variant],
            'images': [{'src': image, 'variant_ids': [product_id]} for image in gallery]}


def _market(soup, config=None):
    currencies = [node.get('content', '') for node in soup.select('[itemprop="priceCurrency"],meta[property="product:price:currency"]')]
    if 'USD' not in currencies:
        raise CrawlError('Magento 商品缺少 USD 源价格证据')
    body_classes = soup.body.get('class', []) if soup.body else []
    source = str(soup)
    if 'region-us' not in body_classes and not re.search(r'(?:/en_US/|[\"\']locale[\"\']\s*:\s*[\"\']en[_-]US|window\.locale\s*=\s*[\"\']en-US)', source):
        raise CrawlError('Magento 商品缺少美国市场证据')
    return {'country': 'US', 'currency': 'USD'}


def _normalize(manifest):
    soup = parse_soup(manifest['html'])
    product_input = soup.select_one('#product_addtocart_form input[name="product"], input[name="product"][value]')
    if product_input is None:
        raise CrawlError('Magento 商品缺少真实 product ID')
    product_id = str(product_input['value'])
    config = _configuration(soup, product_id)
    _market(soup, config)
    if config is None:
        return _simple_product(soup, product_id, manifest['url'])
    if not isinstance(config.get('index'), dict) or not config['index']:
        raise CrawlError('Magento 可配置商品缺少真实子商品映射: ' + manifest['url'])
    attributes = config.get('attributes', {})
    order = list(attributes)
    if not order:
        raise CrawlError('Magento 商品缺少属性定义')
    labels = {key: {str(option['id']): html_lib.unescape(str(option['label']))
                    for option in attribute.get('options', [])}
              for key, attribute in attributes.items()}
    canonical_link = soup.select_one('link[rel="canonical"][href]')
    product_url = _local(canonical_link['href'], manifest['url']) if canonical_link else manifest['url']
    variants, images, gallery_cache, missing_images = [], {}, {}, []
    page_gallery = _page_gallery(soup, manifest['url'])
    stock = config.get('salable')
    stock_known = isinstance(stock, dict) and bool(stock)
    salable_ids = set()
    if stock_known:
        for values in stock.values():
            for ids in values.values():
                salable_ids.update(map(str, ids))
    for child_id, values in config['index'].items():
        price = config.get('optionPrices', {}).get(child_id, {})
        amount = price.get('finalPrice', {}).get('amount')
        if amount is None or float(amount) < 0:
            raise CrawlError('Magento 子商品缺少精确价格: ' + child_id)
        gallery = _variant_gallery(config, child_id, manifest['url'], gallery_cache)
        if len(config['index']) == 1:
            gallery = list(dict.fromkeys(gallery + page_gallery))
        if not gallery:
            missing_images.append(child_id)
        sku = config.get('sku', {}).get(child_id, '')
        variant = {'id': child_id, 'sku': str(sku), 'price': str(amount),
                   'compare_at_price': price.get('oldPrice', {}).get('amount'),
                   'inventory_quantity': '',
                   'source_variant_url': product_url or manifest['url']}
        if gallery:
            variant['featured_image'] = {'src': gallery[0]}
        if stock_known:
            variant['available'] = child_id in salable_ids
        inventory = config.get('in_stock_products', {}).get(child_id, {}).get('qty')
        if inventory is not None:
            variant['inventory_quantity'] = inventory
            variant['available'] = float(inventory) > 0
        options = []
        for index, key in enumerate(order, 1):
            value_id = str(values.get(key, ''))
            if value_id not in labels[key]:
                raise CrawlError('Magento 子商品属性不属于真实选项')
            options.append(labels[key][value_id])
        for index, value in enumerate(options[:2] + [' / '.join(options[2:])] if len(options) > 3 else options, 1):
            variant['option' + str(index)] = value
        if len(options) > 3:
            variant['source_options'] = {str(attributes[key].get('label') or attributes[key].get('code')): value
                                         for key, value in zip(order, options)}
        variants.append(variant)
        for image in gallery:
            images.setdefault(image, []).append(child_id)
    heading = soup.select_one('h1.page-title, h1')
    option_names = [attributes[key].get('label') or attributes[key].get('code') for key in order]
    if len(option_names) > 3:
        option_names = option_names[:2] + [' / '.join(option_names[2:])]
    for image in page_gallery:
        images.setdefault(image, [])
    if not images:
        raise CrawlError('Magento 商品缺少真实父图集: ' + manifest['url'])
    product = {'id': product_id, 'handle': urlsplit(manifest['url']).path.strip('/').removesuffix('.html'),
            'title': _text(heading), 'currency': 'USD', 'body_html': _description(soup),
            'vendor': '', 'options': [{'name': name} for name in option_names],
            'variants': variants, 'images': [{'src': src, 'variant_ids': ids} for src, ids in images.items()]}
    if missing_images:
        product['missing_variant_images'] = missing_images
        product['warnings'] = [{'code': 'missing_variant_images', 'variant_ids': missing_images,
                                'message': '源页面未提供这些变体的图片，保留真实规格及价格并将变体图留空'}]
    return product


def fetch_product(client, url, evidence):
    response = client.get(url, timeout=45)
    manifest = {'url': response.url, 'html': response.text}
    name = 'magento-' + hashlib.sha256(url.encode()).hexdigest()[:16] + '.json'
    save_json(Path(evidence) / name, manifest)
    product = _normalize(manifest)
    return {'url': url, 'source': 'json', 'endpoint': response.url, 'product': product,
            'source_mapping': {'kind': 'magento_storefront', 'file': 'evidence/' + name}}


def _manifest(root, sample):
    root = Path(root).resolve()
    path = (root / sample['source_mapping']['file']).resolve()
    if not path.is_relative_to(root / 'evidence'):
        raise CrawlError('Magento 源响应不在 evidence 内')
    manifest = read_json(path)
    original, observed = urlsplit(sample['url']), urlsplit(manifest['url'])
    if (original.hostname, original.path) != (observed.hostname, observed.path):
        raise CrawlError('Magento 商品证据与请求身份不一致')
    return manifest


def verify_source(root, sample):
    if _normalize(_manifest(root, sample)) != sample['product']:
        raise CrawlError('Magento 规范化商品与真实子商品映射不一致')


def product_market(root, sample):
    market = _market(parse_soup(_manifest(root, sample)['html']))
    return market['country'], market['currency']
