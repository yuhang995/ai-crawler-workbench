"""Deterministic Shopify reader. Adapted from the user's two Shopify templates."""
import csv
import sys
import gc
import json
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from .html_parser import normalize_event_attributes
from urllib.parse import urlsplit, urljoin, unquote, parse_qs, parse_qsl, quote
from .html_parser import parse_soup as BeautifulSoup
from .common import CrawlError, AdapterRequired, query, save_json, read_json

# Shopify descriptions and embedded component payloads can legitimately exceed
# Python's conservative 128 KiB CSV default. Keep parsing bounded but practical.
try:
    csv.field_size_limit(min(sys.maxsize, 64 * 1024 * 1024))
except OverflowError:  # pragma: no cover
    csv.field_size_limit(2 ** 31 - 1)

FEED_PAGE_SIZE = 50
FULL_SWEEP_FEED_PAGE_SIZE = 250


class ListingCollectionTimeout(CrawlError):
    """One collection exceeded its bounded full-store listing budget."""


def _large_document_get(client, url, timeout=60):
    """Use the large-page budget while retaining simple test/client adapters."""
    try:
        # Exhaust the three configured fingerprints: Firefox -> Chrome -> Safari.
        return client.get(url, attempts=3, timeout=timeout)
    except TypeError as error:
        if 'unexpected keyword argument' not in str(error):
            raise
        return client.get(url)


def _rescue_listing_scopes(root_doc):
    """Move malformed main content out of header/footer/nav before pruning.

    Some storefront responses contain mismatched tags that make libxml attach
    the real ``main`` subtree to the navigation chrome.  Removing that chrome
    would otherwise delete the product grid with it.  Moving only the
    outermost main scope keeps the existing header/recommendation exclusions
    intact and avoids broad, page-wide product-link fallbacks.
    """
    scopes = root_doc.xpath(
        'descendant-or-self::main | '
        'descendant-or-self::*[@role="main" or @id="MainContent"]'
    )
    outermost = []
    for scope in scopes:
        if any(
            isinstance(ancestor.tag, str)
            and (ancestor.tag == 'main' or ancestor.get('role') == 'main'
                 or ancestor.get('id') == 'MainContent')
            for ancestor in scope.iterancestors()
        ):
            continue
        outermost.append(scope)
    for scope in outermost:
        chrome = [
            ancestor for ancestor in scope.iterancestors()
            if isinstance(ancestor.tag, str)
            and ancestor.tag in ('header', 'footer', 'nav')
        ]
        if not chrome:
            continue
        boundary = chrome[-1]
        parent = boundary.getparent()
        if parent is not None:
            parent.insert(parent.index(boundary) + 1, scope)


def handle(url):
    match = re.search(r'/products?/([^/?#]+)', urlsplit(url).path)
    if not match:
        return ''
    value = unquote(match[1]).strip()
    for suffix in ('.json', '.js'):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
    return value


def product_handle(product, url=''):
    """Return a stable import handle for Shopify and non-Shopify products."""
    existing = str(product.get('handle') or '').strip() if isinstance(product, dict) else ''
    if existing:
        return existing
    identity_url = str((product or {}).get('url') or url)
    candidate = handle(identity_url)
    if not candidate:
        candidate = unquote(urlsplit(identity_url).path.rstrip('/').rsplit('/', 1)[-1]).strip()
    if not candidate:
        candidate = str((product or {}).get('id') or '').strip()
    candidate = re.sub(r'-+', '-', re.sub(r'[^A-Za-z0-9_-]+', '-', candidate)).strip('-_')
    if not candidate:
        raise CrawlError('商品缺少可导出的 handle、URL slug 和 id')
    return candidate


def _membership_url(url):
    """Canonical product membership URL without non-identity query noise."""
    parsed = urlsplit(str(url or ''))
    path = re.sub(r'/+', '/', unquote(parsed.path)).rstrip('/') or '/'
    tracking = {'ref', 'source', 'campaign', 'fbclid', 'gclid', 'msclkid',
                '_gl', 'mc_cid', 'mc_eid'}
    identity_query = tuple(sorted(
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith('utm_') and key.casefold() not in tracking))
    return parsed.scheme.casefold(), parsed.netloc.casefold(), path, identity_query


def product_url(collection, slug):
    p = urlsplit(collection)
    if '/collections/' in p.path:
        prefix = p.path.split('/collections/')[0]
    else:
        locale = re.match(r'^(/(?:[a-z]{2}(?:-[a-z]{2})?))/pages/', p.path, re.I)
        prefix = locale.group(1) if locale else ''
    return p._replace(path=prefix + '/products/' + slug, query='', fragment='').geturl()


def react_router_payloads(html):
    """Decode Remix/React Router's devalue stream without executing scripts."""
    payloads = []
    pattern = re.compile(r'streamController\.enqueue\(("(?:\\.|[^"\\])*")\)')
    tables = []
    for match in pattern.finditer(html):
        try:
            frame = json.loads(match.group(1)).strip()
            later = re.fullmatch(r'([PE])(\d+):(.*)', frame, re.S)
            if later:
                if not tables:
                    continue
                flat, promises = tables[-1]
                value = json.loads(later[3])
                # Deferred records use the same reference table as the initial
                # loader, even when the resolved value belongs to another field.
                if isinstance(value, list) and value:
                    index = len(flat)
                    flat.extend(value)
                elif isinstance(value, int):
                    index = value
                else:
                    continue
                if later[1] == 'P':
                    promises[int(later[2])] = index
                continue
            flat = json.loads(frame)
        except (ValueError, TypeError):
            continue
        if isinstance(flat, list) and flat:
            tables.append((flat, {}))
    for flat, promises in tables:
        resolved = {}

        def restore(index):
            if not isinstance(index, int):
                return index
            if index < 0:
                return None
            if index >= len(flat):
                raise ValueError('devalue reference outside payload')
            if index in resolved:
                return resolved[index]
            value = flat[index]
            if isinstance(value, dict):
                target = {}
                resolved[index] = target
                for raw_key, raw_value in value.items():
                    if raw_key.startswith('_') and raw_key[1:].isdigit():
                        key_index = int(raw_key[1:])
                        if key_index >= len(flat) or not isinstance(flat[key_index], str):
                            raise ValueError('devalue object key is invalid')
                        key = flat[key_index]
                    else:
                        key = raw_key
                    target[str(key)] = restore(raw_value)
                return target
            if isinstance(value, list):
                if len(value) == 2 and value[0] == 'P' and isinstance(value[1], int):
                    resolved[index] = None
                    if value[1] in promises:
                        resolved[index] = restore(promises[value[1]])
                    return resolved[index]
                target = []
                resolved[index] = target
                target.extend(restore(item) for item in value)
                return target
            return value

        try:
            payload = restore(0)
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _hydrogen_route(html, kind):
    param = '(?:handle|collectionHandle)' if kind == 'collections' else '(?:handle|productHandle)'
    kind_pattern = 'products?' if kind == 'products' else re.escape(kind)
    # React Router IDs use either dotted Remix form
    # ``routes/collections.$handle`` or directory form
    # ``routes/collections/$handle/index``.
    pattern = re.compile(
        rf'(?:^|[/.]){kind_pattern}(?:[./])\${param}(?:$|[./])')
    for payload in react_router_payloads(html):
        loader = payload.get('loaderData', {})
        if not isinstance(loader, dict):
            continue
        containers = [loader]
        if isinstance(loader.get('routes'), dict):
            containers.append(loader['routes'])
        for routes in containers:
            for name, route in routes.items():
                if pattern.search(str(name)) and isinstance(route, dict):
                    return route
    return {}


def hydrogen_market(html):
    """Read the selected Oxygen buyer locale from the server response."""
    for payload in react_router_payloads(html):
        loader = payload.get('loaderData', {})
        if not isinstance(loader, dict):
            continue
        country = ''
        currency = ''
        # Hydrogen projects split global loader facts differently.  The stock
        # template uses ``root`` while other public storefronts put consent and
        # analytics market facts in the sibling ``root-layout`` route.
        for name in ('root', 'root-layout'):
            root = loader.get(name, {})
            if not isinstance(root, dict):
                continue
            locale = root.get('selectedLocale', {})
            locale = locale if isinstance(locale, dict) else {}
            oxygen = root.get('oxygen', {})
            buyer = oxygen.get('buyer', {}) if isinstance(oxygen, dict) else {}
            buyer = buyer if isinstance(buyer, dict) else {}
            consent = root.get('consent')
            consent = consent if isinstance(consent, dict) else {}
            shop = root.get('shop')
            shop = shop if isinstance(shop, dict) else {}
            analytics = root.get('shopAnalyticsPromise')
            analytics = analytics if isinstance(analytics, dict) else {}
            country = country or str(locale.get('country') or buyer.get('country') or
                                     consent.get('country') or '').upper()
            currency = currency or str(locale.get('currency') or shop.get('currency') or
                                       analytics.get('currency') or '').upper()
        if country or currency:
            return {'country': country, 'currency': currency}
    return {'country': '', 'currency': ''}


def _gid_number(value, kind):
    match = re.fullmatch(rf'gid://shopify/{kind}/(\d+)', str(value or ''))
    if not match:
        raise CrawlError(f'Hydrogen {kind} GID 无效')
    return match.group(1)


def _normalize_hydrogen_product(source, expected_handle=''):
    """Normalize one decoded Storefront product without inventing variants."""
    if not isinstance(source, dict) or not source.get('handle'):
        return None
    if expected_handle and source.get('handle') != expected_handle:
        raise CrawlError('Hydrogen 商品响应 handle 与请求不一致')
    product_id = _gid_number(source.get('id'), 'Product')
    option_nodes = source.get('options') or []
    options = []
    for position, option in enumerate(option_nodes[:3], 1):
        if not isinstance(option, dict) or not option.get('name'):
            raise AdapterRequired('Hydrogen 商品选项结构不兼容')
        values = [str(item.get('name')) for item in option.get('optionValues', [])
                  if isinstance(item, dict) and item.get('name') is not None]
        options.append({'id': position, 'name': str(option['name']),
                        'position': position, 'values': values})
    images = []
    for media in (source.get('media') or {}).get('nodes', []):
        preview = media.get('previewImage') if isinstance(media, dict) else None
        if not isinstance(preview, dict) or not preview.get('url'):
            continue
        image_id = ''
        gid = preview.get('id')
        match = re.fullmatch(r'gid://shopify/(?:ImageSource|ProductImage)/(\d+)', str(gid or ''))
        if match:
            image_id = match.group(1)
        images.append({'id': image_id, 'src': image_url(str(preview['url'])),
                       'alt': preview.get('altText') or '', 'variant_ids': []})
    variants = []
    currencies = set()
    for node in (source.get('variants') or {}).get('nodes', []):
        if not isinstance(node, dict):
            raise AdapterRequired('Hydrogen 变体结构不兼容')
        selected = node.get('selectedOptions') or []
        selected_by_name = {str(item.get('name')): str(item.get('value'))
                            for item in selected if isinstance(item, dict)
                            and item.get('name') is not None and item.get('value') is not None}
        if len(selected_by_name) != len(options):
            raise AdapterRequired('Hydrogen 变体选项数量与商品定义不兼容')
        price = node.get('price') or {}
        compare = node.get('compareAtPrice') or {}
        currencies.add(str(price.get('currencyCode') or '').upper())
        if compare.get('amount') is not None:
            currencies.add(str(compare.get('currencyCode') or '').upper())
        variant = {'id': _gid_number(node.get('id'), 'ProductVariant'),
                   'product_id': product_id, 'title': str(node.get('title') or ''),
                   'price': str(price.get('amount') or ''),
                   'compare_at_price': (str(compare.get('amount'))
                                        if compare.get('amount') is not None else None),
                   'sku': str(node.get('sku') or ''),
                   'available': bool(node.get('availableForSale')),
                   'inventory_quantity': (node.get('quantityAvailable')
                                          if node.get('quantityAvailable') is not None else '')}
        for index, option in enumerate(options, 1):
            variant[f'option{index}'] = selected_by_name[option['name']]
        image = node.get('image')
        if isinstance(image, dict) and image.get('url'):
            source_image = image_url(str(image['url']))
            variant['featured_image'] = {'src': source_image}
            match = re.fullmatch(r'gid://shopify/(?:ImageSource|ProductImage)/(\d+)',
                                 str(image.get('id') or ''))
            if match:
                variant['image_id'] = match.group(1)
            # Some Hydrogen PDPs omit product.media and expose the complete
            # real gallery only through variant.image. Preserve those source
            # images and their exact variant bindings instead of failing the
            # parent-product image contract or inventing a generic gallery.
            existing = next((item for item in images
                             if item.get('src') == source_image), None)
            if existing is None:
                existing = {'id': match.group(1) if match else '',
                            'src': source_image, 'alt': image.get('altText') or '',
                            'variant_ids': []}
                images.append(existing)
            if variant['id'] not in existing['variant_ids']:
                existing['variant_ids'].append(variant['id'])
        variants.append(variant)
    if currencies and currencies != {'USD'}:
        # A proven foreign currency is a market conflict, not a parser drift.
        raise CrawlError('Hydrogen 商品明确不是 USD')
    if not variants or len(variants) >= 250:
        raise AdapterRequired('Hydrogen 商品变体为空或可能截断，转交站点适配器')
    if currencies != {'USD'}:
        raise AdapterRequired('Hydrogen 商品变体缺少可验证 USD 币种')
    return {'id': product_id, 'title': str(source.get('title') or ''),
            'handle': str(source['handle']), 'vendor': str(source.get('vendor') or ''),
            'product_type': str(source.get('productType') or ''),
            'body_html': str(source.get('descriptionHtml') or source.get('description') or ''),
            'tags': [str(value) for value in source.get('tags', [])],
            'options': options, 'variants': variants, 'images': images, 'currency': 'USD'}


def _canonical_hydrogen_product_shape(source):
    """Canonicalize equivalent Storefront connection shapes without guessing."""
    if not isinstance(source, dict):
        return source
    result = dict(source)
    options = []
    for option in result.get('options') or []:
        if not isinstance(option, dict):
            options.append(option)
            continue
        normalized = dict(option)
        if 'optionValues' not in normalized and isinstance(normalized.get('values'), list):
            normalized['optionValues'] = [
                {'name': str(value)} for value in normalized['values']]
        options.append(normalized)
    result['options'] = options
    variants = result.get('variants') or {}
    if isinstance(variants, dict) and not isinstance(variants.get('nodes'), list):
        edges = variants.get('edges')
        if isinstance(edges, list):
            result['variants'] = {'nodes': [edge['node'] for edge in edges
                                             if isinstance(edge, dict)
                                             and isinstance(edge.get('node'), dict)]}
    if not isinstance(result.get('media'), dict):
        images = result.get('images') or {}
        edges = images.get('edges') if isinstance(images, dict) else None
        if isinstance(edges, list):
            result['media'] = {'nodes': [
                {'previewImage': edge['node']} for edge in edges
                if isinstance(edge, dict) and isinstance(edge.get('node'), dict)]}
    return result


def _expand_adjacent_variant_product(source):
    """Convert Hydrogen's one-option selected/adjacent shape to a connection.

    This representation contains real Shopify variant objects, but omits the
    conventional ``variants.nodes`` field.  It is complete only for a single
    option whose encoded existence range and option-value references exactly
    match the gathered variant IDs; multi-option products remain fail-closed.
    """
    if not isinstance(source, dict) or (source.get('variants') or {}).get('nodes'):
        return source
    options = source.get('options') or []
    if len(options) != 1 or not isinstance(options[0], dict):
        return source
    variants = []
    candidates = [source.get('selectedOrFirstAvailableVariant')]
    candidates.extend(source.get('adjacentVariants') or [])
    candidates.extend(value.get('firstSelectableVariant')
                      for value in options[0].get('optionValues', [])
                      if isinstance(value, dict))
    seen = set()
    for variant in candidates:
        if not isinstance(variant, dict) or not variant.get('id'):
            continue
        identity = str(variant['id'])
        if identity not in seen:
            seen.add(identity)
            variants.append(variant)
    existence = re.fullmatch(r'v1_(\d+)(?:-(\d+))?', str(source.get('encodedVariantExistence') or ''))
    expected = ((int(existence.group(2)) if existence.group(2) is not None
                 else int(existence.group(1))) - int(existence.group(1)) + 1
                if existence else 0)
    referenced = {str(value.get('firstSelectableVariant', {}).get('id') or '')
                  for value in options[0].get('optionValues', []) if isinstance(value, dict)}
    if not expected or len(variants) != expected or seen != referenced:
        return source
    expanded = dict(source)
    expanded['variants'] = {'nodes': variants}
    media = []
    for variant in variants:
        image = variant.get('image')
        if isinstance(image, dict) and image.get('url'):
            media.append({'previewImage': image})
    expanded['media'] = {'nodes': media}
    selected_product = (source.get('selectedOrFirstAvailableVariant') or {}).get('product') or {}
    if not expanded.get('productType') and selected_product.get('productType'):
        expanded['productType'] = selected_product['productType']
    return expanded


def _combined_hydrogen_product(route, expected_handle):
    """A combined PDP has a display parent and separately priced child products."""
    parent = route['rootProduct']
    parent_gid = parent.get('id')
    parent_id = _gid_number(parent_gid, 'Product')
    structured = route.get('jsonLd') or []
    groups = [item for item in structured if isinstance(item, dict)
              and item.get('@type') == 'ProductGroup'
              and str(item.get('productGroupID')) == parent_id]
    if parent_gid != route.get('rootProductId') or len(groups) != 1:
        raise CrawlError('Hydrogen 组合商品缺少可核对的父商品身份')
    group = groups[0]
    canonical_url = group.get('url') or ''
    origin = urlsplit(canonical_url)
    public_handle = handle(canonical_url)
    if (origin.scheme not in {'https', 'http'} or not origin.hostname or origin.username is not None
            or not public_handle or (expected_handle and public_handle != expected_handle)):
        raise CrawlError('Hydrogen 组合商品公开地址与请求不一致')
    offers = {}
    for item in structured:
        if (not isinstance(item, dict) or item.get('@type') != 'Product'
                or (item.get('isVariantOf') or {}).get('@id') != group.get('@id')):
            continue
        sku = item.get('sku')
        if not sku or sku in offers:
            raise CrawlError('Hydrogen 组合商品 SKU 证据缺失或重复')
        offers[sku] = item
    connection = (parent.get('childProducts') or {}).get('references') or {}
    children = connection.get('nodes') or []
    if not children or (connection.get('pageInfo') or {}).get('hasNextPage'):
        raise CrawlError('Hydrogen 组合商品子款列表为空或未取完')
    options, media, variants = {}, [], []
    metadata = {}
    for child in children:
        if ((child.get('parent') or {}).get('reference') or {}).get('id') != parent_gid:
            raise CrawlError('Hydrogen 子款引用了其他父商品')
        child_id = _gid_number(child.get('id'), 'Product')
        child_options = child.get('options') or []
        names = [option.get('name') for option in child_options]
        if not names or len(names) > 3 or not all(names) or len(set(names)) != len(names):
            raise CrawlError('Hydrogen 子款选项无效')
        if options and names != list(options):
            raise CrawlError('Hydrogen 子款选项与同组其他子款不一致')
        for option in child_options:
            values = options.setdefault(option['name'], {})
            for val in option.get('optionValues', []):
                values[val['name']] = val
        child_variants = child.get('variants') or {}
        if not child_variants.get('nodes') or (child_variants.get('pageInfo') or {}).get('hasNextPage'):
            raise CrawlError('Hydrogen 子款变体列表为空或未取完')
        for variant in child_variants['nodes']:
            sku = variant.get('sku')
            structured_item = offers.get(sku) or {}
            offer = structured_item.get('offers') or {}
            if (not offer or sku in metadata or not isinstance(variant.get('availableForSale'), bool)
                    or offer.get('priceCurrency') != 'USD'
                    or money(offer.get('price'), 'json') != money((variant.get('price') or {}).get('amount'), 'json')):
                raise CrawlError('Hydrogen 子款 SKU、价格或库存证据不完整')
            variant_url = offer.get('url') or ''
            locator = urlsplit(variant_url)
            if (locator.scheme != origin.scheme or locator.netloc != origin.netloc or locator.username is not None
                    or handle(variant_url) not in {public_handle, parent.get('handle'), child.get('handle')}):
                raise CrawlError('Hydrogen 子款变体链接与商品身份不符')
            selected = {opt['name'].casefold(): opt['value'] for opt in variant['selectedOptions']}
            if any(selected.get(attr) != structured_item[attr] for attr in ('color', 'size') if structured_item.get(attr)):
                raise CrawlError('Hydrogen 子款颜色或尺码与商品页不一致')
            metadata[sku] = {'source_child_product_id': child_id, 'source_variant_url': variant_url}
            variants.append(variant)
        media.extend((child.get('media') or {}).get('nodes', []))
    if len({v.get('id') for v in variants}) != len(variants) or set(metadata) != set(offers):
        raise CrawlError('Hydrogen 子款变体与商品页声明的完整 SKU 不一致')
    parent_media = (parent.get('media') or {}).get('nodes', [])
    gallery = [item for item in parent_media + media if item.get('mediaContentType') == 'IMAGE']
    source = dict(parent, handle=public_handle, media={'nodes': gallery},
                  variants={'nodes': variants}, options=[
                      {'name': name, 'optionValues': list(values.values())} for name, values in options.items()])
    normalized = _normalize_hydrogen_product(source, public_handle)
    normalized['images'] = list({im['src']: im for im in normalized['images']}.values())
    for variant in normalized['variants']:
        variant.update(metadata[variant['sku']])
    return normalized


def hydrogen_product_from_html(html, expected_handle=''):
    """Normalize one server-rendered Hydrogen product, preserving real variants."""
    route = _hydrogen_route(html, 'products')
    page_data = route.get('productPageData') if isinstance(route, dict) else None
    if isinstance(page_data, dict):
        data = page_data.get('data')
        query_data = data.get('productQuery') if isinstance(data, dict) else None
        embedded = query_data.get('product') if isinstance(query_data, dict) else None
        if isinstance(embedded, dict):
            route = dict(route, product=embedded)
    if isinstance(route.get('rootProduct'), dict) and route['rootProduct'].get('childProducts'):
        return _combined_hydrogen_product(route, expected_handle)
    mapped = route.get('mappedProduct') if isinstance(route, dict) else None
    if isinstance(mapped, dict) and isinstance(mapped.get('product'), dict):
        # Newer Oxygen storefronts expose a presentation-ready mapped product:
        # identity/options live under ``product`` while complete real variants,
        # gallery, display title and vendor are siblings.  Convert only those
        # observed fields back to the canonical Storefront shape.
        source = dict(mapped['product'])
        variants = mapped.get('variants')
        if not isinstance(variants, list) or not variants:
            raise CrawlError('Hydrogen mappedProduct 变体为空')
        option_names = []
        for variant in variants:
            if not isinstance(variant, dict):
                raise CrawlError('Hydrogen mappedProduct 变体结构无效')
            for selected in variant.get('selectedOptions') or []:
                name = selected.get('name') if isinstance(selected, dict) else None
                if name and name not in option_names:
                    option_names.append(name)
        declared = {str(option.get('name')): option for option in source.get('options') or []
                    if isinstance(option, dict) and option.get('name')}
        if not option_names or any(name not in declared for name in option_names):
            raise CrawlError('Hydrogen mappedProduct 选项与变体不一致')
        source['options'] = [declared[name] for name in option_names]
        source['variants'] = {'nodes': variants}
        media = []
        for item in (mapped.get('media') or {}).get('images', []):
            image = item.get('image') if isinstance(item, dict) else None
            if isinstance(image, dict) and image.get('url'):
                media.append({'previewImage': image})
        source['media'] = {'nodes': media}
        title = mapped.get('title') or {}
        vendor = mapped.get('vendor') or {}
        source['title'] = (title.get('full') if isinstance(title, dict) else title) or ''
        source['vendor'] = (vendor.get('raw') if isinstance(vendor, dict) else vendor) or ''
        source['descriptionHtml'] = mapped.get('descriptionHtml') or source.get('description') or ''
        return _normalize_hydrogen_product(source, expected_handle)
    source = route.get('product') if isinstance(route, dict) else None
    if (not isinstance(source, dict) and isinstance(route, dict)
            and route.get('handle') and route.get('id')):
        source = route
    if not isinstance(source, dict) or not source.get('handle'):
        if route.get('bundle') and route.get('productItems'):
            raise CrawlError('Hydrogen 配置型套装缺少独立可验证变体')
        return None
    source = _canonical_hydrogen_product_shape(source)
    return _normalize_hydrogen_product(_expand_adjacent_variant_product(source), expected_handle)


def hydrogen_group_products(html):
    """Return complete group members already embedded in the current PDP."""
    route = _hydrogen_route(html, 'products')
    product = route.get('product') if isinstance(route, dict) else None
    grouping = product.get('initialGrouping') if isinstance(product, dict) else None
    if not isinstance(grouping, dict):
        return {}
    result = {}
    for source in grouping.get('allProducts', []):
        if not isinstance(source, dict) or not source.get('handle'):
            continue
        try:
            normalized = _normalize_hydrogen_product(source, source['handle'])
        except CrawlError:
            continue
        result.setdefault(normalized['handle'], normalized)
    return result


def hydrogen_group_entries(html, seed_handle):
    """Read an explicit Hydrogen combined-listing group from the PDP payload."""
    route = _hydrogen_route(html, 'products')
    product = route.get('product') if isinstance(route, dict) else None
    grouping = product.get('initialGrouping') if isinstance(product, dict) else None
    if not isinstance(grouping, dict):
        return []
    declared = grouping.get('products') or []
    subgroup_title = ''
    if not declared:
        for subgroup in grouping.get('subgroups', []):
            members = subgroup.get('products', []) if isinstance(subgroup, dict) else []
            if any(isinstance(item, dict) and item.get('handle') == seed_handle
                   for item in members):
                declared = members
                subgroup_title = str(subgroup.get('title') or '').strip()
                break
    full = {item.get('handle'): item for item in grouping.get('allProducts', [])
            if isinstance(item, dict) and item.get('handle')}
    if (not 1 < len(declared) <= 100
            or not any(isinstance(item, dict) and item.get('handle') == seed_handle
                       for item in declared)):
        return []
    title = ' '.join(value for value in (
        str(grouping.get('title') or '').strip(), subgroup_title) if value)
    handle_title = re.sub(r"[’']", '', title.casefold())
    group_handle = re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', handle_title)).strip('-')
    entries, seen = [], set()
    for reference in declared:
        handle_value = str(reference.get('handle') or '') if isinstance(reference, dict) else ''
        source = full.get(handle_value)
        if not handle_value or handle_value in seen:
            return []
        # Group configuration can retain unpublished handles.  The same PDP's
        # ``allProducts`` payload is the authoritative active member set; a
        # declared handle absent there is not emitted or guessed.
        if not isinstance(source, dict):
            continue
        product_id = _gid_number(reference.get('id'), 'Product')
        if product_id != _gid_number(source.get('id'), 'Product'):
            return []
        colour_option = next((option for option in (source or {}).get('options', [])
                              if isinstance(option, dict)
                              and str(option.get('name', '')).casefold() in {'color', 'colour'}), None)
        values = colour_option.get('optionValues', []) if colour_option else []
        if values and (len(values) != 1 or not isinstance(values[0], dict)
                       or not values[0].get('name')):
            return []
        colour = str(values[0]['name']) if values else ''
        if not colour_option:
            # Split Shopify Products can expose their colour as a structured
            # product tag while their sole variant is the Shopify placeholder.
            # The enclosing group has already established membership by GID.
            tags = {tag.split('::', 1)[1].strip() for tag in source.get('tags', [])
                    if isinstance(tag, str) and tag.lower().startswith(('color::', 'colour::'))}
            if len(tags) == 1:
                colour = next(iter(tags))
        image = (source or {}).get('featuredImage') or {}
        entries.append({'handle': handle_value,
                        'color': colour,
                        'product_id': product_id,
                        'product_image': image_url(str(image.get('url') or '')),
                        'group_title': title, 'group_handle': group_handle})
        seen.add(handle_value)
    return entries if len(entries) > 1 and seed_handle in seen else []


def _cursor_route(response, page_url):
    """Decode the route-selected Single Fetch response for this exact page."""
    page = urlsplit(page_url)
    endpoint = urlsplit(response.get('url', ''))
    params = parse_qs(endpoint.query)
    routes = params.pop('_routes', [])
    if (page.scheme != endpoint.scheme or page.netloc != endpoint.netloc
            or endpoint.path != page.path.rstrip('/') + '.data'
            or params != parse_qs(page.query) or len(routes) != 1):
        raise CrawlError('Hydrogen 翻页响应来源与分页链接不一致')
    frames = ''.join('streamController.enqueue(' + json.dumps(line) + ')'
                     for line in response['body'].splitlines())
    for payload in react_router_payloads(frames):
        for name in routes[0].split(','):
            if re.search(r'(?:^|[/.])collections\.\$(?:handle|collectionHandle)(?:$|[./])', name):
                route = payload.get(name, {}).get('data')
                if isinstance(route, dict):
                    return route
    raise CrawlError('Hydrogen 翻页响应缺少分类路由数据')


def _render_hydrogen_cursor_pages(url, first_html):
    """A React Router link may drop its cursor on a full-document navigation."""
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
    initial = _hydrogen_collection_page(first_html, url)
    target = initial['next_url']
    pages = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(locale='en-US')
                page = context.new_page()
                page.route('**/*', lambda req: req.abort() if req.request.resource_type
                           in ('image', 'media', 'font') else req.continue_())
                page.goto(url, wait_until='domcontentloaded', timeout=60000)
                page.wait_for_function('() => !!window.__reactRouterDataRouter', timeout=20000)
                while target:
                    if target in pages:
                        raise CrawlError('Hydrogen 分类分页游标未推进')
                    requested = urlsplit(target)
                    def route_response(res):
                        p = urlsplit(res.url)
                        return (p.netloc == requested.netloc and p.path == requested.path.rstrip('/') + '.data'
                                and parse_qs(p.query).get('cursor') == parse_qs(requested.query).get('cursor'))

                    with page.expect_response(route_response, timeout=30000) as pending:
                        clicked = page.evaluate("""url => {
                            const a = [...document.querySelectorAll('main a[href]')].find(a => a.href === url);
                            if (a) a.click();
                            return !!a;
                        }""", target)
                        if not clicked:
                            raise CrawlError('Hydrogen 页面未显示对应下一页链接')
                    response = pending.value
                    if response.status != 200:
                        raise CrawlError(f'Hydrogen 分类翻页响应 HTTP {response.status}')
                    source = {'url': response.url, 'body': response.body().decode('utf-8')}
                    route = _cursor_route(source, target)
                    info = route['collection']['products']['pageInfo']
                    page.wait_for_function("""([target, next]) => {
                        if (location.href !== target) return false;
                        const links = [...document.querySelectorAll('main a[href]')]
                            .map(a => new URL(a.href)).filter(u => u.searchParams.get('direction') === 'next');
                        return next ? links.some(u => u.searchParams.get('cursor') === next) : !links.length;
                    }""", arg=[target, info['endCursor'] if info['hasNextPage'] else None], timeout=20000)
                    html = page.locator('main').evaluate_all(
                        "els => els.map(el => el.outerHTML).join('')")
                    parsed = _hydrogen_collection_page(html, target, source)
                    pages[target] = {'html': html, 'router_response': source}
                    target = parsed['next_url']
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise CrawlError('Hydrogen 前端翻页未完成: ' + str(exc)) from exc
    return pages


def _hydrogen_collection_page(html, url, router_response=None):
    route = _cursor_route(router_response, url) if router_response else _hydrogen_route(html, 'collections')
    if not route and not router_response:
        requested_path = urlsplit(url).path.rstrip('/') or '/'
        for payload in react_router_payloads(html):
            loader = payload.get('loaderData', {})
            if not isinstance(loader, dict):
                continue
            for candidate in loader.values():
                data = candidate.get('productsListingPageData') if isinstance(candidate, dict) else None
                if not isinstance(data, dict):
                    continue
                page_url = str(data.get('pageUrl') or '')
                if (urlsplit(page_url).path.rstrip('/') or '/') == requested_path:
                    route = data
                    break
            if route:
                break
    # Modern Hydrogen storefronts can expose a presentation-ready PLP payload
    # on arbitrary public routes (/ties, /whats-new, /c/*).  Accept it only
    # when the page URL, numeric Shopify identities, USD market and public card
    # URLs all agree with the requested server-rendered document.
    if (isinstance(route, dict) and isinstance(route.get('products'), list)
            and isinstance(route.get('pagination'), dict) and route.get('pageUrl')):
        market = hydrogen_market(html)
        if market.get('currency') != 'USD' or market.get('country') != 'US':
            raise CrawlError('Hydrogen 顶级分类市场不是 US/USD')
        requested = urlsplit(url)
        page_target = urlsplit(str(route['pageUrl']))
        if ((page_target.path.rstrip('/') or '/') != (requested.path.rstrip('/') or '/')
                or page_target.netloc not in ('', requested.netloc)):
            raise CrawlError('Hydrogen 顶级分类页面身份不一致')
        handles, products = [], []
        for card in route['products']:
            if not isinstance(card, dict):
                continue
            slug = str(card.get('handle') or '')
            product_id = str(card.get('product_id') or card.get('id') or '')
            variant_id = str(card.get('variant_id') or '')
            target = urljoin(url, str(card.get('url') or ''))
            target_parts = urlsplit(target)
            try:
                Decimal(str(card.get('price')))
            except (InvalidOperation, TypeError, ValueError):
                raise CrawlError('Hydrogen 顶级分类商品价格无效')
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', slug)
                    or not product_id.isdigit() or not variant_id.isdigit()
                    or target_parts.scheme != requested.scheme
                    or target_parts.netloc != requested.netloc
                    or handle(target) != slug):
                raise CrawlError('Hydrogen 顶级分类商品身份或链接无效')
            if slug in handles or any(item['id'] == product_id for item in products):
                raise CrawlError('Hydrogen 顶级分类出现重复父商品')
            handles.append(slug)
            products.append({'id': product_id, 'handle': slug, 'url': target,
                             'variants': [], '_hydrogen_partial': True})
        pagination = route['pagination']
        try:
            current_page = int(pagination['currentPage'])
            total_results = int(pagination['totalResults'])
        except (KeyError, TypeError, ValueError) as error:
            raise CrawlError('Hydrogen 顶级分类分页底账无效') from error
        if not products or current_page < 1 or total_results < len(products):
            raise CrawlError('Hydrogen 顶级分类分页数量矛盾')
        page_size = len(products)
        raw_next = pagination.get('nextPage')
        if raw_next in (None, '', False):
            has_next_page = False
            total_pages = current_page
        else:
            try:
                next_page = int(raw_next)
            except (TypeError, ValueError) as error:
                raise CrawlError('Hydrogen 顶级分类下一页标记无效') from error
            if next_page != current_page + 1:
                raise CrawlError('Hydrogen 顶级分类分页未连续推进')
            has_next_page = True
            total_pages = next_page
        return {'handles': handles, 'products': products,
                'current_page': current_page, 'total_pages': total_pages,
                'total_results': total_results, 'page_size': page_size,
                'has_next_page': has_next_page,
                'end_cursor': '', 'pagination_kind': 'page'}
    collection = route.get('collection') if isinstance(route, dict) else None
    # Some Hydrogen route loaders return the collection object itself instead
    # of wrapping it as ``{"collection": ...}``.
    if (not isinstance(collection, dict) and isinstance(route, dict)
            and isinstance(route.get('products'), dict) and route.get('handle')):
        collection = route
    if not isinstance(collection, dict):
        return None
    # Locale-aware Hydrogen storefronts may pre-group colour/size cards in a
    # title-keyed ``products`` mapping while retaining distinct Shopify parent
    # products in ``rawProducts``. The latter is authoritative for sampling.
    raw_products = collection.get('rawProducts')
    grouped_products = collection.get('products')
    route_page_info = route.get('pageInfo') if isinstance(route, dict) else None
    if (isinstance(grouped_products, dict) and isinstance(raw_products, list)
            and isinstance(route_page_info, dict)):
        products, handles = [], []
        for source in raw_products:
            if not isinstance(source, dict):
                continue
            slug = str(source.get('handle') or '')
            product_id = _gid_number(source.get('id'), 'Product')
            variants = source.get('variants')
            if isinstance(variants, dict):
                variants = variants.get('nodes')
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', slug)
                    or not product_id or not isinstance(variants, list) or not variants):
                raise CrawlError('Hydrogen 分组分类的父商品身份或变体无效')
            for variant in variants:
                price = variant.get('price') if isinstance(variant, dict) else None
                if not isinstance(price, dict) or price.get('currencyCode') != 'USD':
                    raise CrawlError('Hydrogen 分组分类商品价格不是 USD')
            if slug in handles:
                raise CrawlError('Hydrogen 分组分类出现重复父商品')
            handles.append(slug)
            products.append({'id': product_id, 'handle': slug,
                             'url': product_url(url, slug), 'variants': [],
                             '_hydrogen_partial': True})
        if not products:
            raise CrawlError('Hydrogen 分组分类商品为空')
        more = route_page_info.get('hasNextPage')
        cursor = str(route_page_info.get('endCursor') or '')
        if not isinstance(more, bool) or (more and not cursor):
            raise CrawlError('Hydrogen 分组分类分页状态无效')
        return {'handles': handles, 'products': products,
                'current_page': 1, 'total_pages': 1,
                'total_results': len(products), 'page_size': len(products),
                'has_next_page': more, 'end_cursor': cursor,
                'pagination_kind': 'cursor'}
    # Some Oxygen storefronts expose a Constructor-backed result list at the
    # route top level instead of a Storefront ``collection.products``
    # connection.  Accept it only with an exact collection handle, numeric
    # Shopify product/variation identities, USD prices, and a complete numeric
    # pagination ledger from the same server-rendered loader response.
    route_products = route.get('products') if isinstance(route, dict) else None
    route_pagination = route.get('pagination') if isinstance(route, dict) else None
    route_cursor = route.get('paginationInfo') if isinstance(route, dict) else None
    if isinstance(route_products, list) and isinstance(route_cursor, dict):
        expected = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
        if collection.get('handle') != expected:
            raise CrawlError('Hydrogen 游标分类商品与请求 handle 不一致')
        handles, products = [], []
        for card in route_products:
            source = card.get('product') if isinstance(card, dict) else None
            if not isinstance(source, dict):
                continue
            slug = str(source.get('handle') or '')
            product_id = _gid_number(source.get('id'), 'Product')
            price_range = source.get('priceRange') or {}
            prices = [price_range.get('minVariantPrice'), price_range.get('maxVariantPrice')]
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', slug)
                    or not product_id
                    or any(not isinstance(price, dict) or price.get('currencyCode') != 'USD'
                           for price in prices)):
                raise CrawlError('Hydrogen 游标分类商品身份、价格或币种无效')
            if slug in handles or any(str(item['id']) == product_id for item in products):
                raise CrawlError('Hydrogen 游标分类商品重复')
            handles.append(slug)
            products.append({'id': product_id, 'handle': slug,
                             'url': product_url(url, slug), 'variants': [],
                             '_hydrogen_partial': True})
        more = route_cursor.get('hasNextPage')
        cursor = str(route_cursor.get('endCursor') or '')
        if (not products or not isinstance(more, bool) or (more and not cursor)):
            raise CrawlError('Hydrogen 游标分类分页状态无效')
        return {'handles': handles, 'products': products,
                'current_page': 1, 'total_pages': 1,
                'total_results': len(products), 'page_size': len(products),
                'has_next_page': more, 'end_cursor': cursor,
                'pagination_kind': 'cursor'}
    if isinstance(route_products, list) and isinstance(route_pagination, dict):
        expected = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
        if collection.get('handle') != expected:
            raise CrawlError('Hydrogen 顶层分类商品与请求 handle 不一致')
        handles, products = [], []
        for item in route_products:
            if not isinstance(item, dict) or item.get('type') not in (None, 'product'):
                continue
            slug = str(item.get('handle') or '')
            product_id = str(item.get('id') or '')
            variation_id = str(item.get('variationId') or '')
            price = item.get('price') or {}
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', slug)
                    or not product_id.isdigit() or not variation_id.isdigit()
                    or price.get('currencyCode') != 'USD'):
                raise CrawlError('Hydrogen 顶层分类商品身份、价格或币种无效')
            if slug in handles or any(str(product['id']) == product_id for product in products):
                raise CrawlError('Hydrogen 顶层分类商品重复')
            handles.append(slug)
            products.append({'id': product_id, 'handle': slug, 'variants': [],
                             '_hydrogen_partial': True})
        try:
            current_page = int(route_pagination['currentPage'])
            total_pages = int(route_pagination['totalPages'])
            total_results = int(route_pagination['totalResults'])
            per_page = int(route_pagination['resultsPerPage'])
        except (KeyError, TypeError, ValueError) as error:
            raise CrawlError('Hydrogen 顶层分类分页底账无效') from error
        if (current_page < 1 or total_pages < current_page or total_results < len(products)
                or per_page < len(products) or not products):
            raise CrawlError('Hydrogen 顶层分类分页数量矛盾')
        return {'handles': handles, 'products': products,
                'current_page': current_page, 'total_pages': total_pages,
                'total_results': total_results, 'page_size': per_page,
                'has_next_page': current_page < total_pages,
                'end_cursor': '', 'pagination_kind': 'page'}
    # Searchspring-backed Hydrogen stores expose ``results`` plus numeric
    # pagination.  Native Storefront API Hydrogen routes expose a Shopify
    # connection at ``collection.products.nodes`` with cursor pagination.
    connection = collection.get('products')
    if isinstance(connection, dict) and isinstance(connection.get('nodes'), list):
        nodes = connection['nodes']
        if any(isinstance(item, dict) and 'productItem' in item for item in nodes):
            # A storefront can render one card per colour even when all cards
            # link to one public PDP. The node GID belongs to the colour. The
            # nested productItem supplies the display parent's GID and route.
            from lxml import html as lh
            tree = lh.fromstring(normalize_event_attributes(html))
            mains = tree.xpath('//main | //*[@role="main"]')
            if not mains or collection.get('handle') != urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]:
                raise CrawlError('Hydrogen 分类商品卡片缺少对应页面主体')
            main = max(mains, key=lambda node: len(node.xpath('.//a[contains(@href,"/products/")]')))
            for el in main.xpath('.//template | .//noscript | .//nav | .//header | .//footer | '
                                 './/*[@hidden or @aria-hidden="true"]'):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
            grids = main.xpath('.//*[@data-product-grid or contains(@class, "products-grid") '
                               'or contains(@class, "product-grid") or @id="product-grid"]')
            scope = grids[0] if grids else main

            def route_key(href):
                parts = urlsplit(href)
                return parts._replace(path=parts.path.rstrip('/')).geturl()

            base = product_url(url, '')
            by_target = {}
            for item in nodes:
                parent = item.get('productItem') if isinstance(item, dict) else None
                if not isinstance(parent, dict) or not parent.get('targetRoute'):
                    raise CrawlError('Hydrogen 颜色卡片缺少父商品链接')
                target = urljoin(base, parent['targetRoute'])
                slug = handle(target)
                if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', slug)
                        or route_key(urlsplit(target)._replace(query='', fragment='').geturl())
                        != product_url(url, slug)):
                    raise CrawlError('Hydrogen 颜色卡片父商品链接非法')
                pid = _gid_number(parent.get('id'), 'Product')
                key = route_key(target)
                if key in by_target and by_target[key] != (pid, slug):
                    raise CrawlError('Hydrogen 商品卡片链接对应多个父商品')
                by_target[key] = (pid, slug)
            visible = {}
            hrefs = scope.xpath('.//a[@href]/@href')
            # Load more appends cards; a previously displayed colour may be
            # repeated on the new page. Use the most recent grid occurrence.
            for href in reversed(hrefs) if router_response else hrefs:
                target = urljoin(url, href)
                key = route_key(target)
                if key in by_target:
                    visible.setdefault(key, target)
            if router_response:
                visible = dict(reversed(visible.items()))
            if list(visible) != list(by_target):
                raise CrawlError('Hydrogen 父商品卡片与前端网格顺序或链接不符')
            products = []
            for key, target in visible.items():
                pid, slug = by_target[key]
                products.append({'id': pid, 'handle': slug, 'url': target,
                                 'variants': [], '_hydrogen_partial': True})
            page_info = connection.get('pageInfo') or {}
            more = page_info.get('hasNextPage')
            cursor = page_info.get('endCursor')
            if not isinstance(more, bool):
                raise CrawlError('Hydrogen 颜色卡片缺少分页状态')
            next_url = None
            if more:
                location = urlsplit(url)._replace(query='', fragment='')
                for href in main.xpath('.//a[@href]/@href'):
                    target = urlsplit(urljoin(url, href))
                    query_values = parse_qs(target.query)
                    if (cursor and target._replace(query='', fragment='').geturl().rstrip('/')
                            == location.geturl().rstrip('/') and query_values.get('cursor') == [cursor]
                            and query_values.get('direction') == ['next']):
                        next_url = target.geturl()
                        break
                if not next_url:
                    raise CrawlError('Hydrogen 颜色卡片游标与前端下一页链接不符')
            return {'handles': [p['handle'] for p in products], 'products': products,
                    'current_page': 1, 'total_pages': 1, 'total_results': len(products),
                    'page_size': len(nodes), 'has_next_page': more, 'end_cursor': cursor,
                    'next_url': next_url, 'pagination_kind': 'parent_cards'}
        handles, products = [], []
        for item in nodes:
            if not isinstance(item, dict):
                continue
            if 'type' in item and 'data' in item and item.get('type') != 'product':
                continue
            source = (item.get('data') if item.get('type') == 'product'
                      and isinstance(item.get('data'), dict) else item)
            slug = str(source.get('handle') or '')
            product_id = _gid_number(source.get('id'), 'Product')
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', slug)
                    or not product_id):
                raise CrawlError('Hydrogen 分类商品身份无效')
            if slug in handles:
                raise CrawlError('Hydrogen 分类页出现重复商品')
            handles.append(slug)
            products.append({'id': product_id, 'handle': slug, 'variants': [],
                             '_hydrogen_partial': True})
        page_info = connection.get('pageInfo') or {}
        return {'handles': handles, 'products': products, 'current_page': 1,
                'total_pages': 1, 'total_results': len(handles),
                'page_size': max(len(handles), 1),
                'has_next_page': bool(page_info.get('hasNextPage')),
                'end_cursor': str(page_info.get('endCursor') or ''),
                'pagination_kind': 'cursor'}
    if not isinstance(collection.get('results'), list):
        return None
    handles, products = [], []
    for item in collection['results']:
        if not isinstance(item, dict):
            continue
        slug = str(item.get('handle') or '')
        href = str(item.get('url') or '')
        if not slug and href:
            slug = handle(urljoin(url, href))
        uid = str(item.get('uid') or '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', slug) or not uid.isdigit():
            raise CrawlError('Hydrogen 分类商品身份无效')
        if slug in handles:
            raise CrawlError('Hydrogen 分类页出现重复商品')
        handles.append(slug)
        products.append({'id': uid, 'handle': slug, 'variants': [],
                         '_hydrogen_partial': True})
    pagination = collection.get('pagination') or {}
    total_pages = int(pagination.get('totalPages') or 1)
    current_page = int(pagination.get('currentPage') or 1)
    total_results = int(pagination.get('totalResults') or len(handles))
    per_page = int(pagination.get('perPage') or max(len(handles), 1))
    return {'handles': handles, 'products': products, 'current_page': current_page,
            'total_pages': total_pages, 'total_results': total_results,
            'page_size': per_page, 'has_next_page': current_page < total_pages,
            'end_cursor': '', 'pagination_kind': 'page'}


def hydrogen_collection_listing(client, url, first_html, full=True):
    """Collect a complete Searchspring/Hydrogen collection in visible order."""
    pages, feeds, products = [], [], []
    by_id, by_handle, seen_cursors = {}, {}, set()
    html = first_html
    page_number = 1
    total_pages = 1
    total_results = None
    page_size = 1
    cursor = ''
    next_url = None
    rendered = None
    pagination_kind = 'page'
    while True:
        current_url = (url if page_number == 1 else
                       next_url if pagination_kind == 'parent_cards' else
                       query(url, cursor=cursor, direction='next')
                       if pagination_kind == 'cursor' else
                       query(url, page=page_number))
        router_response = None
        if page_number > 1 and rendered is None:
            # Hydrogen route documents can exceed 2 MB.  Twenty seconds is a
            # useful ordinary request bound but too short for a valid later
            # page on a busy Oxygen edge; use one explicit large-page budget.
            response = _large_document_get(client, current_url)
            html = response.text
            if (pagination_kind == 'parent_cards'
                    and parse_qs(urlsplit(getattr(response, 'url', current_url)).query)
                    != parse_qs(urlsplit(current_url).query)):
                rendered = _render_hydrogen_cursor_pages(url, first_html)
        if rendered is not None:
            if current_url not in rendered:
                raise CrawlError('Hydrogen 前端翻页没有对应的原始响应')
            html = rendered[current_url]['html']
            router_response = rendered[current_url]['router_response']
        parsed = _hydrogen_collection_page(html, current_url, router_response)
        if parsed is None:
            if page_number == 1:
                return None
            raise CrawlError('Hydrogen 分类后续页缺少同源列表数据')
        total_pages = parsed['total_pages']
        kind = parsed.get('pagination_kind', 'page')
        if page_number > 1 and kind != pagination_kind:
            raise CrawlError('Hydrogen 分类分页格式发生变化')
        pagination_kind = kind
        if pagination_kind == 'page' and parsed['current_page'] != page_number:
            raise CrawlError('Hydrogen 分类分页序号不一致')
        total_results = parsed['total_results'] if total_results is None else total_results
        if pagination_kind == 'page' and parsed['total_results'] != total_results:
            raise CrawlError('Hydrogen 分类分页总数发生变化')
        page_size = parsed['page_size']
        for product in parsed['products']:
            identity, slug = str(product['id']), product['handle']
            if identity in by_id:
                if pagination_kind == 'parent_cards' and by_id[identity] == slug:
                    continue
                raise CrawlError('Hydrogen 分类分页重复商品')
            if slug in by_handle and pagination_kind == 'parent_cards':
                raise CrawlError('Hydrogen 分类页父商品链接与身份不符')
            by_id[identity] = slug
            by_handle[slug] = identity
            products.append(product)
        pages.append({'url': current_url, 'handles': parsed['handles'], 'html': html})
        if router_response:
            pages[-1]['router_response'] = router_response
        feeds.append({'products': parsed['products'], 'source': 'hydrogen_embedded_collection'})
        has_next_page = bool(parsed.get('has_next_page'))
        cursor = parsed.get('end_cursor', '')
        next_url = parsed.get('next_url')
        if not full or not has_next_page:
            break
        if pagination_kind in ('cursor', 'parent_cards'):
            if not cursor or cursor in seen_cursors:
                raise CrawlError('Hydrogen 分类分页缺少游标或游标未推进')
            seen_cursors.add(cursor)
        page_number += 1
    if full and pagination_kind == 'page' and len(products) != total_results:
        raise CrawlError('Hydrogen 分类商品数与分页底账不一致')
    if not products:
        raise CrawlError('Hydrogen 分类商品为空')
    return {'format': 'hydrogen', 'url': url, 'products': products,
            'urls': [p.get('url') or product_url(url, p['handle']) for p in products], 'warnings': [],
            'order_evidence': 'hydrogen_embedded_collection', 'feed_page_size': page_size,
            'membership_sources': [], 'card_sources': [], 'pages': pages, 'feeds': feeds,
            'complete': bool(full and not has_next_page),
            'first3_seconds': None, 'first3_cpu_seconds': 0.0}


def listing_dom(html, url):
    from lxml import html as lh
    root_doc = lh.fromstring(normalize_event_attributes(html or '<html/>'))
    _rescue_listing_scopes(root_doc)
    fallback_pagination = root_doc.xpath('//noscript//a[@href]')
    def marker(el):
        return ' '.join((el.get('id', ''), el.get('class', ''), str(el.tag)))
    excluded = re.compile(r'recommend|recently[-_ ]?view|related[-_ ]?product|upsell|cross[-_ ]?sell|predictive', re.I)
    for el in list(root_doc.iter()):
        if not isinstance(el.tag, str) or el is root_doc:
            continue
        if (el.tag in ('script','style','template','noscript','header','footer','nav','aside')
            or el.get('hidden') is not None or el.get('aria-hidden') == 'true'
            or re.search(r'display\s*:\s*none', el.get('style',''), re.I)
            or excluded.search(marker(el))):
            if el.getparent() is not None:
                el.getparent().remove(el)
    fragment = unquote(urlsplit(url).fragment)
    matches = root_doc.xpath('descendant-or-self::*[@id=$id]', id=fragment) if fragment else []
    root = matches[0] if matches else None
    if fragment and root is None:
        raise CrawlError('分类锚点不存在，不能扩大到整个分类: ' + url)
    if root is None:
        for id_value in ('product-grid','CollectionGrid','ProductGridContainer'):
            nodes=root_doc.xpath('descendant-or-self::*[@id=$id]', id=id_value)
            if nodes:
                root=nodes[0];break
    if root is None:
        for el in root_doc.iter():
            if not isinstance(el.tag,str):continue
            classes=set(el.get('class','').split())
            if (el.get('data-product-grid') is not None or any(re.search(r'(?:^|[_-])(?:collection-cards-grid|productsgridwrapper)(?:$|[_-])',c,re.I) for c in classes) or classes & {'product-grid','product-list-grid','collection-products','collection__products','product-list','collection-grid'}) and el.xpath('.//a[contains(@href,"/products/") or contains(@href,"/product/")]'):
                root=el;break
    if root is None:
        nodes=root_doc.xpath(
            'descendant-or-self::main | '
            'descendant-or-self::*[@role="main" or @id="MainContent" or @id="main" or @id="content" '
            'or contains(concat(" ", normalize-space(@class), " "), " main-content ")]')
        root=nodes[0] if nodes else None
    if root is None:
        # Some otherwise standard themes render the PLP directly below body
        # without a semantic main wrapper.  Header/nav/footer/aside and known
        # recommendation blocks were already removed above, so the remaining
        # body is a bounded fallback rather than an arbitrary whole-page scan.
        bodies = root_doc.xpath('descendant-or-self::body')
        candidate = bodies[0] if bodies else root_doc
        if candidate.xpath('.//a[contains(@href,"/products/") or contains(@href,"/product/")]'):
            root = candidate
        else:
            # Return an empty visible grid.  collect_listing can then use the
            # complete unfiltered Shopify feed with an explicit warning.
            root = candidate
    ordered, seen=[],set()
    for href in root.xpath('.//a[contains(@href,"/products/") or contains(@href,"/product/")]/@href'):
        target=urljoin(url,href)
        if urlsplit(target).hostname != urlsplit(url).hostname:continue
        slug=handle(target)
        if slug and slug not in seen:
            ordered.append(slug);seen.add(slug)
    next_url=None
    links=root_doc.xpath('//a[@href]') + fallback_pagination
    for a in links:
        label = ' '.join(a.itertext()).strip().casefold()
        if 'next' in a.get('rel','').split() or a.get('title','').lower() == 'next' or a.get('aria-label','').lower() in ('next','next page') or set(a.get('class','').split()) & {'pagination__next','next'} or label == 'load more':
            next_url=urljoin(url,a.get('href'));break
    if not next_url:
        from urllib.parse import parse_qs
        p=urlsplit(url);page=int(parse_qs(p.query).get('page',['1'])[0])
        for a in links:
            target=urljoin(url,a.get('href'));q=urlsplit(target)
            if q.path==p.path and parse_qs(q.query).get('page')==[str(page+1)]:
                next_url=target;break
    dynamic=bool(root_doc.xpath('//*[@data-next-url or @data-load-more or (self::button and @name="load_more") or (contains(concat(" ",normalize-space(@class)," ")," load-more ") and not(@disabled))]'))
    grids = root_doc.xpath('//results-list//*[@ref="grid" and @data-last-page]')
    if grids:
        grid = grids[0]
        cards = grid.xpath('.//*[@data-page]')
        if cards:
            current_page = max(int(c.get('data-page')) for c in cards)
            last_page = int(grid.get('data-last-page'))
            next_url = query(url, page=current_page + 1) if current_page < last_page else None
            dynamic = False
    elif root_doc.xpath('//button[@ref="loadMoreButton" or @aria-label="Load more products"]'):
        dynamic = True
    return ordered,next_url,dynamic


def ordered_products(html, products, url):
    slugs, _, _ = listing_dom(html, url)
    by_handle = {p['handle']: p for p in products}
    result, seen = [], set()
    for slug in slugs:
        p = by_handle.get(slug)
        if p and str(p['id']) not in seen:
            result.append(p)
            seen.add(str(p['id']))
    return result


def embedded_product_handles(html, products, url):
    """Order feed handles by their first exact appearance in the page body.

    Header/footer and recommendation widgets are excluded. Scripts inside the
    product-page main region remain searchable because many Shopify themes put
    their client-rendered grid data there instead of emitting product anchors.
    """
    soup = BeautifulSoup(html or '', 'lxml')
    main = soup.select_one('main,[role=main],#MainContent') or soup.body
    if main is None:
        return []
    for node in main.select('header,footer,nav,[class*="recommend" i],[id*="recommend" i],'
                            '[class*="recently" i],[id*="recently" i],'
                            '[class*="related" i],[id*="related" i],'
                            '[class*="upsell" i],[id*="upsell" i]'):
        node.decompose()
    source = str(main)
    found = []
    for index, product in enumerate(products):
        slug = product.get('handle') if isinstance(product, dict) else None
        if not isinstance(slug, str) or not slug:
            continue
        escaped = re.escape(slug)
        patterns = (
            r'/products/' + escaped + r'(?=[/?#"\x27<\\]|$)',
            r'\\/products\\/' + escaped + r'(?=[/?#"\x27<\\]|$)',
            r'["\x27]handle["\x27]\s*:\s*["\x27]' + escaped + r'["\x27]',
        )
        positions = [match.start() for pattern in patterns
                     if (match := re.search(pattern, source, re.I))]
        if positions:
            found.append((min(positions), index, slug))
    found.sort()
    return [slug for _, _, slug in found] if len(found) >= 3 else []


class RenderedPages(dict):
    def __init__(self):
        super().__init__()
        self.warnings=[]


def verify_listing_location(actual_url,requested_url):
    actual,requested=urlsplit(actual_url),urlsplit(requested_url)
    same_path = actual.path.rstrip('/') == requested.path.rstrip('/')
    # Shopify commonly canonicalizes an obsolete collection handle (and tag
    # child paths) to another real collection URL.  That remains inside the
    # requested catalog and must not turn an otherwise valid full-store run
    # into a hard failure.  Cross-domain and non-collection redirects remain
    # blocked, while filter/sort preservation is still checked below.
    collection_redirect = (
        actual.hostname == requested.hostname
        and re.fullmatch(r'/collections/[^/]+(?:/[^/]+)?/?', actual.path)
        and re.fullmatch(r'/collections/[^/]+(?:/[^/]+)?/?', requested.path)
    )
    if actual.hostname!=requested.hostname or (not same_path and not collection_redirect):
        raise CrawlError('商品列表跳离请求分类')
    before,after=parse_qs(requested.query),parse_qs(actual.query)
    for key,values in before.items():
        if (key.startswith('filter.') or key=='sort_by') and sorted(after.get(key,[]))!=sorted(values):
            raise CrawlError('商品列表丢失筛选或排序参数: '+key)


def goto_listing(page,url,warnings):
    from playwright.sync_api import TimeoutError as BrowserTimeout, Error as BrowserError
    for attempt in range(3):
        try:
            page.goto(url,wait_until='commit',timeout=12000)
            break
        except BrowserTimeout:
            requested,actual=urlsplit(url),urlsplit(page.url)
            if (requested.hostname!=actual.hostname or requested.path.rstrip('/')!=actual.path.rstrip('/') or requested.query!=actual.query
                    or not page.locator('main a[href*="/products/"]:visible').count()):raise
            # A loaded same-collection grid is subsequently checked against feed
            # identities, pagination, filters and market. Timeout alone is not success.
            warnings.append({'code':'listing_load_timeout','message':'列表页加载事件超时，但同路径真实商品网格已渲染；继续核对源响应','url':url})
            return
        except BrowserError as error:
            transient = any(marker in str(error) for marker in (
                'ERR_CONNECTION_CLOSED', 'ERR_CONNECTION_RESET',
                'ERR_NETWORK_CHANGED', 'ERR_TIMED_OUT'))
            if not transient or attempt == 2:
                raise
            page.wait_for_timeout(500 * (attempt + 1))
    try:
        page.locator('main a[href*="/products/"]').first.wait_for(timeout=12000)
    except BrowserTimeout:
        requested,actual=urlsplit(url),urlsplit(page.url)
        if (requested.hostname!=actual.hostname or requested.path.rstrip('/')!=actual.path.rstrip('/') or requested.query!=actual.query
                or not page.locator('main a[href*="/products/"]:visible').count()):raise
        warnings.append({'code':'listing_load_timeout','message':'列表页商品元素等待超时，但同路径真实商品网格已渲染；继续核对源响应','url':url})


def route_listing_assets(page, url):
    """Block heavy assets and known tracking hosts without blocking store data."""
    origin = urlsplit(url).hostname or ''
    trackers = ('google-analytics.com','googletagmanager.com','doubleclick.net',
                'facebook.net','facebook.com','tiktok.com','pinterest.com',
                'hotjar.com','adroll.com','criteo.com','snapchat.com')
    def route_request(route, request):
        hostname = urlsplit(request.url).hostname or ''
        if request.resource_type in {'image','media','font'}:
            return route.abort()
        if hostname != origin and any(hostname == name or hostname.endswith('.' + name) for name in trackers):
            return route.abort()
        return route.continue_()
    page.route('**/*', route_request)


def rendered_listing_pages(url, full=True):
    """Read real client-rendered grids and observed pagination in one session."""
    from playwright.sync_api import sync_playwright
    pages = RenderedPages()
    deadline = time.monotonic() + 120
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(locale='en-US', viewport={'width':1440,'height':1000})
            context.add_cookies([{'name':'localization','value':'US','url':url}, {'name':'cart_currency','value':'USD','url':url}])
            page = context.new_page()
            route_listing_assets(page,url)
            current = url
            previous_links = None
            while current:
                if current in pages or time.monotonic() > deadline:
                    raise CrawlError('动态商品分页循环或超过120秒')
                if previous_links is None:
                    goto_listing(page,current,pages.warnings)
                else:
                    # Follow the site's own client-side pagination handler.
                    target = page.locator('a[href]').filter(has_text=re.compile(r'^\s*Next\s*$', re.I))
                    exact = page.locator('a[title="Next"]')
                    control = exact.first if exact.count() else target.first
                    if not control.count():
                        goto_listing(page,current,pages.warnings)
                    else:
                        for selector in ('#onetrust-reject-all-handler', '#onetrust-close-btn-container button', '#onetrust-accept-btn-handler'):
                            dismiss = page.locator(selector).first
                            if dismiss.count() and dismiss.is_visible():
                                dismiss.click(timeout=3000)
                                page.wait_for_timeout(300)
                                break
                        control.click(timeout=10000, no_wait_after=True)
                        page.wait_for_function("old => JSON.stringify([...document.querySelectorAll('main a[href*=\"/products/\"]')].map(a=>a.getAttribute('href'))) !== old", arg=previous_links, timeout=20000)
                page.wait_for_timeout(700)
                verify_listing_location(page.url,current)
                html = page.content()
                slugs, nxt, dynamic = listing_dom(html, current)
                if not slugs or (dynamic and full):
                    raise AdapterRequired('渲染后仍缺少可验证商品或存在未知加载更多')
                pages[current] = html
                previous_links = page.evaluate("JSON.stringify([...document.querySelectorAll('main a[href*=\"/products/\"]')].map(a=>a.getAttribute('href')))")
                if nxt and (urlsplit(nxt).netloc != urlsplit(url).netloc or urlsplit(nxt).path != urlsplit(url).path):
                    raise CrawlError('动态分页离开当前分类')
                current = nxt if full else None
        finally:
            browser.close()
    return pages


def landing_collection_links(html, url):
    """Explicit child collection links in the landing's main content only."""
    from .nav_extractor import is_blacklisted
    soup = BeautifulSoup(html, 'lxml')
    main = soup.select_one('main,[role=main],#MainContent')
    if main is None:
        return []
    for node in main.select('header,footer,nav,template,script,[hidden],[aria-hidden=true]'):
        node.decompose()
    result = []
    for a in main.select('a[href]'):
        target = urljoin(url, a['href']); p = urlsplit(target)
        if (p.hostname == urlsplit(url).hostname and target.rstrip('/') != url.rstrip('/')
                and re.fullmatch(r'/collections/[^/]+/?', p.path)
                and not p.query and not p.fragment and not is_blacklisted(target)):
            result.append(target)
    return list(dict.fromkeys(result))


def landing_membership(client, html, url):
    children = landing_collection_links(html, url)
    if not children or len(children) > 32:
        raise AdapterRequired('集合落地页没有有界、明确的子分类来源')
    sources, products = [], {}
    for child in children:
        p = urlsplit(child)
        endpoint = p._replace(path=p.path.rstrip('/') + '/products.json').geturl()
        feeds, seen = [], set()
        for page in range(1, 101):
            feed = client.json(query(endpoint, page=page, limit=FEED_PAGE_SIZE))
            batch = feed.get('products') if isinstance(feed, dict) else None
            if not isinstance(batch, list):
                raise CrawlError('落地页子集合 feed 无效')
            ids = {str(product['id']) for product in batch}
            if batch and not ids - seen:
                raise CrawlError('落地页子集合分页重复')
            seen.update(ids); feeds.append(feed)
            for product in batch:
                previous = products.get(product['handle'])
                if previous and str(previous['id']) != str(product['id']):
                    raise CrawlError('落地页子集合商品身份冲突')
                products[product['handle']] = product
            if len(batch) < FEED_PAGE_SIZE:
                break
        else:
            raise CrawlError('落地页子集合分页超出上限')
        sources.append({'url': child, 'endpoint': endpoint, 'page_size': FEED_PAGE_SIZE, 'feeds': feeds})
    slugs, nxt, dynamic = listing_dom(html, url)
    if nxt or dynamic or set(slugs) - set(products):
        raise AdapterRequired('落地页商品未被显式子集合完整覆盖，或存在未完成的动态分页')
    return list(products.values()), sources


def curated_card_handles(html, url):
    """Only explicit product-card components, never arbitrary body links."""
    soup = BeautifulSoup(html, 'lxml')
    main = soup.select_one('main,[role=main],#MainContent')
    if main is None:
        return set()
    for node in main.select('header,footer,nav,template,script,[hidden],[aria-hidden=true]'):
        node.decompose()
    handles = set()
    for node in main.select('[data-handle],[data-product-handle]'):
        if not node.name.endswith('product-card'):
            continue
        slug = node.get('data-product-handle') or node.get('data-handle')
        links = {handle(urljoin(url, a['href'])) for a in node.select('a[href]')
                 if urlsplit(urljoin(url, a['href'])).hostname == urlsplit(url).hostname}
        if slug in links:
            handles.add(slug)
    return handles


def observed_landing_card_handles(html, url):
    """Finite product-grid handles on a verified catalog landing."""
    try:
        slugs, nxt, dynamic = listing_dom(html, url)
    except AdapterRequired:
        return set()
    if len(slugs) < 3 or len(slugs) > 100 or nxt or dynamic:
        return set()
    return set(slugs)


def resolve_landing_membership(client, html, url, allow_observed_grid=False):
    try:
        products, sources = landing_membership(client, html, url)
        return products, sources, []
    except AdapterRequired:
        slugs, nxt, dynamic = listing_dom(html, url)
        declared = (observed_landing_card_handles(html, url) if allow_observed_grid
                    else curated_card_handles(html, url))
        if (not slugs or len(slugs) > 100 or nxt or dynamic
                or set(slugs) != declared):
            raise AdapterRequired('落地页既无完整子集合来源，也无完整、有限的显式商品卡片')
        products, cards = [], []
        for slug in slugs:
            target = product_url(url, slug)
            raw = client.json(target + '.json')
            product = raw.get('product', raw) if isinstance(raw, dict) else {}
            if product.get('handle') != slug or not product.get('id') or not product.get('variants'):
                raise CrawlError('落地页商品卡片与实际商品身份不符')
            products.append(product)
            cards.append({'url': target, 'endpoint': target + '.json', 'response': raw})
        return products, [], cards


def collect_listing(client, url, out=None, full=True, prefer_feed_order=False,
                    max_seconds=None):
    start = time.perf_counter()
    deadline = start + max_seconds if max_seconds else None

    def check_deadline():
        if deadline is not None and time.perf_counter() >= deadline:
            raise ListingCollectionTimeout(
                f'单分类列表采集超过 {max_seconds:g} 秒: {url}')

    def listing_json(target):
        check_deadline()
        if deadline is None:
            return client.json(target)
        remaining = max(1.0, deadline - time.perf_counter())
        try:
            result = client.json(target, timeout=min(20.0, remaining), attempts=3)
        except TypeError as error:
            # Preserve lightweight fixture/custom client compatibility.
            if 'unexpected keyword argument' not in str(error):
                raise
            result = client.json(target)
        check_deadline()
        return result
    parts = urlsplit(url)
    if '/products/' in parts.path and '/collections/' not in parts.path:
        return {'format': 'direct_product', 'url': url, 'urls': [url], 'products': [], 'pages': [], 'feeds': [], 'complete': True}
    # Preserve the storefront's natural browser order in one session/thread:
    # home page (already fetched by the pipeline) -> collection HTML -> JSON.
    # Some Shopify storefronts issue a bot/session cookie from the collection
    # response and reject a parallel products.json request that races ahead in
    # a different thread-local Client session.
    is_collection_path = '/collections/' in parts.path
    collection_tail = (parts.path.split('/collections/', 1)[1].strip('/').split('/')
                       if is_collection_path else [])
    tag_scoped = len(collection_tail) > 1
    # Analytics/attribution parameters do not alter collection membership.
    # Treat only semantic query keys as scoped; otherwise a harmless campaign
    # token can force Playwright and defeat the verified Feed fallback.
    query_keys = parse_qs(parts.query)
    tracking_key = re.compile(
        r'^(?:utm_.+|gclid|dclid|fbclid|msclkid|ttclid|twclid|ref|source|'
        r'campaign|campaignid|affiliate|affid|irclickid|abralink)$', re.I)
    query_scoped = any(key != 'page' and not tracking_key.fullmatch(key)
                       for key in query_keys)
    feed_only_allowed = bool(prefer_feed_order and not tag_scoped and not query_scoped)
    early_warnings = []
    try:
        response = _large_document_get(
            client, parts._replace(fragment='').geturl(),
            timeout=min(30, max_seconds) if max_seconds else 60)
    except CrawlError as error:
        if not feed_only_allowed:
            raise
        response = type('FeedOnlyCollectionResponse', (), {'text': '', 'url': url})()
        early_warnings.append({'code': 'listing_html_feed_only_fallback',
            'message': '全量无筛选分类的 HTML 暂时不可用；继续使用 Shopify products.json 权威成员源',
            'url': url, 'reason': str(error)})
    # Hydrogen storefronts may expose real PLP routes such as /ties, /shirts
    # or /c/polos-sweaters instead of Shopify's conventional /collections/*.
    # They have no products.json endpoint derivable from the public route, so
    # inspect the rendered/embedded Hydrogen payload before collection logic.
    if not is_collection_path:
        hydrogen = hydrogen_collection_listing(client, url, response.text, full=full)
        if hydrogen is None and re.fullmatch(r'/pages/[^/]+/?', parts.path, re.I):
            products, membership_sources, card_sources = resolve_landing_membership(
                client, response.text, url, allow_observed_grid=True)
            by_handle = {product['handle']: product for product in products}
            slugs = listing_dom(response.text, url)[0]
            ordered = [by_handle[slug] for slug in slugs if slug in by_handle]
            if len(ordered) < 3:
                raise AdapterRequired('Shopify 商品落地页不足三个可验证商品')
            result = {'format': 'shopify_landing_grid', 'url': url,
                      'products': ordered, 'urls': [product_url(url, p['handle']) for p in ordered],
                      'warnings': early_warnings, 'order_evidence': 'html_dom',
                      'feed_page_size': 0, 'membership_sources': membership_sources,
                      'card_sources': card_sources, 'card_source_kind': 'observed_grid',
                      'pages': [{'url': url, 'handles': slugs,
                                 'excluded_not_in_collection': [], 'html': response.text}],
                      'feeds': [{'products': []}], 'complete': full,
                      'first3_seconds': round(time.perf_counter() - start, 4),
                      'first3_cpu_seconds': 0.0}
            if out:
                save_json(Path(out) / 'listing.json', result)
            return result
        if hydrogen is None:
            raise AdapterRequired('Shopify Hydrogen 顶级分类路由缺少可验证商品数据')
        if early_warnings:
            hydrogen.setdefault('warnings', []).extend(early_warnings)
        if out:
            save_json(Path(out) / 'listing.json', hydrogen)
        return hydrogen
    from .brumate_listing import is_nosto_grid, collect_listing as collect_nosto_grid
    if is_nosto_grid(response.text):
        listing = collect_nosto_grid(client, url, first_html=response.text, full=full)
        if out:
            save_json(Path(out) / 'listing.json', listing)
        return listing
    path = parts.path.split('/collections/')[0] + '/collections/' + parts.path.split('/collections/')[1].split('/')[0]
    endpoint_parts = parts._replace(path=path + '/products.json', query='', fragment='')
    shopify_origin = getattr(client, 'shopify_origin', '')
    if not isinstance(shopify_origin, str):
        shopify_origin = ''
    if shopify_origin:
        origin = urlsplit(shopify_origin)
        endpoint_parts = endpoint_parts._replace(scheme=origin.scheme, netloc=origin.netloc)
    endpoint = endpoint_parts.geturl()
    try:
        feed_page_size = (FULL_SWEEP_FEED_PAGE_SIZE
                          if prefer_feed_order and not tag_scoped and not query_scoped
                          else FEED_PAGE_SIZE)
        feed = listing_json(query(endpoint, page=1, limit=feed_page_size))
    except CrawlError:
        hydrogen = hydrogen_collection_listing(client, url, response.text, full=full)
        if hydrogen is None:
            raise
        start_first3 = time.perf_counter()
        if len(hydrogen['products']) >= 3:
            hydrogen['first3_seconds'] = round(time.perf_counter() - start, 4)
            hydrogen['first3_cpu_seconds'] = round(time.perf_counter() - start_first3, 4)
        if out:
            save_json(Path(out) / 'listing.json', hydrogen)
        return hydrogen
    if not isinstance(feed, dict) or not isinstance(feed.get('products'), list):
        raise CrawlError('分类 JSON 不可用')
    products = list(feed['products'])
    membership_sources, card_sources = [], []
    if not products and listing_dom(response.text, url)[0]:
        products, membership_sources, card_sources = resolve_landing_membership(client, response.text, url)
    rendered = RenderedPages() if early_warnings else {}
    if early_warnings:
        rendered.warnings.extend(early_warnings)
    query_values=parse_qs(parts.query)
    filters={k:v for k,v in query_values.items() if k.startswith('filter.')}
    # Any non-pagination query can alter the visible subset/order.  A public
    # collection feed does not prove those custom parameters, so it can never
    # be the sole order/range evidence for such URLs.
    scoped=tag_scoped or query_scoped
    static_handles=[]
    feed_fallback=False
    initial_dynamic = False
    try:
        initial_slugs, _, initial_dynamic = listing_dom(response.text, url)
    except AdapterRequired:
        if not products:
            raise
        initial_slugs=[]
    if products and not initial_slugs:
        static_handles=embedded_product_handles(response.text,products,url)
    if prefer_feed_order and products and not scoped and not initial_slugs and not static_handles:
        if not isinstance(rendered, RenderedPages):
            rendered = RenderedPages()
        rendered.warnings.append({'code': 'listing_order_feed_fallback',
            'message': '全量采集使用已验证 products.json 成员与顺序，避免逐分类重复渲染前端网格',
            'url': url, 'reason': 'full_store_feed_order'})
        feed_fallback = True
    elif products and not scoped and not initial_slugs and not static_handles:
        # An unfiltered Shopify collection feed is authoritative membership.
        # If the corresponding HTML is a client-rendered shell with no
        # observable product links, launching Playwright cannot strengthen the
        # source identity and used to turn a healthy feed into a 300-second AI
        # repair.  Preserve the feed's published order explicitly instead.
        if not isinstance(rendered, RenderedPages):
            rendered = RenderedPages()
        rendered.warnings.append({'code': 'spa_feed_order_fallback',
            'message': 'SPA 分类 HTML 未包含商品链接；使用已验证 products.json 成员与顺序',
            'url': url, 'reason': 'static_spa_grid_empty'})
        feed_fallback = True
    elif scoped or (products and not initial_slugs and not static_handles):
        from playwright.sync_api import TimeoutError as BrowserTimeout, Error as BrowserError
        try:
            rendered = rendered_listing_pages(url, full)
        except BrowserTimeout as error:
            # products.json is authoritative membership/order for an
            # unfiltered collection in both sample and full-store runs.
            if len(products) < 3 or (scoped and not prefer_feed_order):
                raise
            rendered=RenderedPages()
            rendered.warnings.append({'code':'listing_order_feed_fallback',
                'message':'Playwright 商品网格超时；使用已验证 Shopify Feed 顺序继续',
                'url':url,'reason':str(error)})
            feed_fallback=True
        except (BrowserError,AdapterRequired) as error:
            if scoped or len(products)<3:
                raise
            rendered=RenderedPages()
            rendered.warnings.append({'code':'listing_order_feed_fallback',
                'message':'前端商品网格未在12秒内可用；使用已验证 products.json 顺序',
                'url':url,'reason':str(error)})
            feed_fallback=True
    initial_html = rendered.get(url, response.text)
    try:
        rendered_slugs = listing_dom(initial_html, url)[0]
    except AdapterRequired:
        rendered_slugs = []
    if (products and not scoped and not feed_fallback and not static_handles
            and not rendered_slugs):
        # A renderer can finish successfully while a heavy web component still
        # exposes no product anchors.  For an unfiltered collection the feed is
        # authoritative membership and a safe explicit order fallback; do not
        # misclassify this as a site-wide adapter failure.
        if not isinstance(rendered, RenderedPages):
            replacement = RenderedPages()
            replacement.update(rendered)
            rendered = replacement
        rendered.warnings.append({'code': 'listing_order_feed_fallback',
            'message': '渲染完成但前端商品网格没有可验证链接；使用 products.json 顺序',
            'url': url, 'reason': 'rendered_grid_empty'})
        feed_fallback = True
    if products and initial_dynamic and not scoped and not feed_fallback:
        # The paginated products feed is complete membership evidence.  For an
        # unfiltered collection, a theme's opaque infinite-scroll button must
        # not turn a healthy feed into an adapter failure; retain an explicit
        # warning that order fell back to the feed.
        if not isinstance(rendered, RenderedPages):
            replacement = RenderedPages()
            replacement.update(rendered)
            rendered = replacement
        rendered.warnings.append({'code': 'listing_order_feed_fallback',
            'message': '前端使用未知动态加载；使用完整 products.json 成员与顺序',
            'url': url, 'reason': 'dynamic_load_more'})
        feed_fallback = True
    first3_start = time.perf_counter()
    if feed_fallback:
        first3 = products[:3]
    elif static_handles and not scoped:
        first3_by_handle = {product['handle']: product for product in products}
        first3 = [first3_by_handle[slug] for slug in static_handles
                  if slug in first3_by_handle][:3]
    else:
        first3 = ordered_products(initial_html, products, url)[:3]
    first3_cpu_seconds = time.perf_counter() - first3_start
    first3_seconds = time.perf_counter() - start
    feeds = [feed]
    if membership_sources:
        feeds.extend(f for source in membership_sources for f in source['feeds'])
    # Feed pages build the membership map; HTML controls order and inclusion.
    seen_feed = {str(p['id']) for p in products}
    page = 1
    while not membership_sources and len(feeds[-1]['products']) == feed_page_size:
        check_deadline()
        page += 1
        data = listing_json(query(endpoint, page=page, limit=feed_page_size))
        batch = data.get('products')
        if not isinstance(batch, list):
            raise CrawlError('分类 feed 分页无效')
        if batch and not ({str(p['id']) for p in batch} - seen_feed):
            raise CrawlError('分类 feed 重复页')
        seen_feed.update(str(p['id']) for p in batch)
        products.extend(batch)
        feeds.append(data)
    order_evidence='html_dom'
    if static_handles and not scoped:
        order_evidence='embedded_handles'
        # The initial evidence pass only knows the first feed page.  Rebuild
        # it after feed pagination so publication and the independent
        # validator use the identical complete product universe.  Otherwise
        # handles embedded later in the same HTML can be appended in feed
        # order here but rediscovered in DOM/script order during validation.
        static_handles = embedded_product_handles(response.text, products, url)
        present=set(static_handles)
        static_handles.extend(p['handle'] for p in products if p.get('handle') not in present)
    if feed_fallback:
        order_evidence='products_feed_fallback'
        static_handles=[p['handle'] for p in products]
    by_handle = {p['handle']: p for p in products}
    html, current = initial_html, url
    ordered, seen, pages, visited = [], set(), [], set()
    while True:
        if current in visited:
            raise CrawlError('前端分页循环')
        visited.add(current)
        if current==url and static_handles and (not scoped or feed_fallback):
            slugs=static_handles
            selected=[by_handle[slug] for slug in slugs if slug in by_handle]
            nxt=None;dynamic=False
        else:
            slugs, nxt, dynamic = listing_dom(html, current)
            selected = ordered_products(html, products, current)
        missing = [s for s in slugs if s not in by_handle]
        fresh = [p for p in selected if str(p['id']) not in seen]
        if pages and not fresh and nxt:
            raise CrawlError('前端分页没有新增商品但仍有下一页')
        for p in fresh:
            ordered.append(p)
            seen.add(str(p['id']))
        pages.append({'url': current, 'handles': slugs, 'excluded_not_in_collection': missing, 'html': html})
        if not full or not nxt:
            if full and dynamic:
                raise AdapterRequired('发现动态加载更多，不能把首屏当全量；需非标适配')
            break
        if urlsplit(nxt).netloc != parts.netloc or urlsplit(nxt).path.rstrip('/') != parts.path.rstrip('/'):
            raise CrawlError('分页离开当前分类')
        if any(sorted(parse_qs(urlsplit(nxt).query).get(k,[]))!=sorted(v) for k,v in filters.items()):
            raise CrawlError('分页丢失分类筛选参数，停止以免扩大商品范围')
        current = nxt
        html = rendered[current] if current in rendered else client.get(current).text
    if not ordered and products and not any(p['handles'] for p in pages):
        raise AdapterRequired('当前分类没有可验证的前端商品: ' + url)
    result = {'url': url, 'products': ordered, 'urls': [product_url(url, p['handle']) for p in ordered],
              'warnings':getattr(rendered,'warnings',[]),
              'order_evidence':order_evidence,
              'feed_page_size': feed_page_size,
              'membership_sources': membership_sources,
              'card_sources': card_sources,
              'pages': pages, 'feeds': feeds, 'complete': full,
              'first3_seconds': round(first3_seconds, 4) if len(first3) == 3 else None,
              'first3_cpu_seconds': round(first3_cpu_seconds, 4)}
    if out:
        save_json(Path(out) / 'listing.json', result)
    return result


def money(value, source):
    if value is None or value == '' or isinstance(value, bool):
        raise CrawlError('缺失或非法价格')
    try:
        amount = Decimal(str(value))
    except InvalidOperation as e:
        raise CrawlError('非法价格') from e
    # .js contract is integer cents; JSON feeds are dollar amounts, even integer 49.
    if source == 'js':
        if amount != amount.to_integral_value():
            raise CrawlError('.js 美分价格不是整数')
        amount /= 100
    elif source not in ('json', 'collection'):
        raise CrawlError('未知价格来源，禁止猜单位')
    if not amount.is_finite() or amount < 0:
        raise CrawlError('非法价格')
    return format(amount.quantize(Decimal('.01')), '.2f')


def clean_description(value):
    soup = BeautifulSoup(value or '', 'html.parser')
    for node in soup.select('script,style,iframe'):
        node.decompose()
    for a in soup.select('a'):
        a.unwrap()
    return str(soup)


def image_url(value, base=''):
    """Return a safe absolute public image URL, or an empty string.

    Shopify/theme payloads may use protocol-relative paths, root-relative
    paths, or leave spaces unescaped.  A malformed optional gallery member is
    not product identity evidence and must not invalidate an entire full run.
    """
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or value.casefold().startswith(('data:', 'javascript:', 'blob:')):
        return ''
    target = urljoin(base, 'https:' + value if value.startswith('//') else value)
    parsed = urlsplit(target)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username:
        return ''
    path = quote(unquote(parsed.path), safe='/%:@!$&\'()*+,;=-._~')
    query_value = quote(unquote(parsed.query), safe='=&%/:;+,-._~')
    return parsed._replace(path=path, query=query_value, fragment='').geturl()


def display_image_url(value, base=''):
    target = image_url(value, base)
    path = unquote(urlsplit(target).path).lower()
    if path.endswith('.svg') or re.search(
            r'(?:^|[/_.-])(?:logo|favicon|icon|sprite|badge|banner|placeholder)(?:[/_.-]|$)', path):
        return ''
    return target


def normalize_product_images(product, base):
    """Normalize gallery/variant images and report only discarded bad values."""
    discarded = []
    normalized = []
    for item in product.get('images', []):
        raw = item.get('src', '') if isinstance(item, dict) else item
        target = display_image_url(raw, base)
        if not target:
            if raw:
                discarded.append(str(raw))
            continue
        if isinstance(item, dict):
            item = dict(item); item['src'] = target
        else:
            item = target
        normalized.append(item)
    product['images'] = normalized
    for variant in product.get('variants', []):
        featured = variant.get('featured_image') or variant.get('image')
        raw = (featured.get('src') or featured.get('url')) if isinstance(featured, dict) else featured
        target = display_image_url(raw, base)
        if raw and not target:
            discarded.append(str(raw))
        variant['featured_image'] = {'src': target} if target else None
        if not target:
            matched = variant_images(product, variant)
            if matched:
                variant['featured_image'] = {'src': matched[0]}
    return discarded


def source_product_without_images(sample):
    """True only for an honestly empty official gallery, never a sanitized bad URL."""
    return (not product_gallery(sample.get('product', {}))
            and not any(warning.get('code') == 'invalid_product_images_skipped'
                        for warning in sample.get('warnings', [])))


def variant_images(product, variant):
    matched, gallery = [], []
    for img in product.get('images', []):
        src = display_image_url(img.get('src', '') if isinstance(img, dict) else img)
        if src:
            gallery.append(src)
            if isinstance(img, dict) and (str(variant['id']) in map(str, img.get('variant_ids', [])) or (variant.get('image_id') is not None and str(variant['image_id']) == str(img.get('id')))):
                matched.append(src)
    featured = variant.get('featured_image')
    if isinstance(featured, dict) and featured.get('src'):
        matched.insert(0, display_image_url(featured['src']))
    return list(dict.fromkeys(filter(None, matched)))


def product_gallery(product):
    images = [image_url(i.get('src', '') if isinstance(i, dict) else i) for i in product.get('images', [])]
    return list(dict.fromkeys(u for u in images if u))


def fetch_product(client, url, fallback=None):
    errors = []
    partial = bool((fallback or {}).get('_hydrogen_partial'))
    # Hydrogen cards link directly to the server-rendered PDP with its selected options.
    shopify_origin = getattr(client, 'shopify_origin', '')
    if not isinstance(shopify_origin, str):
        shopify_origin = ''
    ajax = (('.json', 'json'), ('.js', 'js')) if (not partial or shopify_origin) else ()
    parsed = urlsplit(url)
    for suffix, source in ajax:
        endpoint_path = parsed.path.rstrip('/') + suffix
        if shopify_origin and re.search(r'/product/[^/]+$', parsed.path):
            endpoint_path = '/products/' + handle(url) + suffix
        endpoint_parts = parsed._replace(path=endpoint_path, fragment='')
        if shopify_origin:
            origin = urlsplit(shopify_origin)
            endpoint_parts = endpoint_parts._replace(scheme=origin.scheme, netloc=origin.netloc)
        endpoint = endpoint_parts.geturl()
        try:
            data = client.json(endpoint)
            product = data.get('product', data)
            if product.get('handle') != handle(url) or not product.get('variants'):
                raise CrawlError('商品身份或变体数据无效')
            if len(product['variants']) >= 250:
                raise CrawlError('达到接口 250 变体上限，需非标完整性核验')
            discarded = normalize_product_images(product, url)
            return {'url': url, 'source': source, 'endpoint': endpoint, 'product': product,
                    'warnings': ([{'code': 'invalid_product_images_skipped',
                                  'url': url, 'count': len(discarded)}]
                                 if discarded else [])}
        except (CrawlError, AttributeError) as e:
            errors.append(str(e))
    try:
        html = _large_document_get(client, url).text
        product = hydrogen_product_from_html(html, handle(url))
        if product is None:
            raise CrawlError('商品页缺少受支持的 Hydrogen 商品数据')
        if partial and fallback.get('id') and str(product['id']) != str(fallback['id']):
            raise CrawlError('Hydrogen 商品页身份与分类卡片不一致')
        discarded = normalize_product_images(product, url)
        return {'url': url, 'source': 'collection', 'endpoint': url,
                'product': product, 'source_html': html,
                'source_format': 'hydrogen_devalue',
                'warnings': ([{'code': 'invalid_product_images_skipped',
                              'url': url, 'count': len(discarded)}]
                             if discarded else [])}
    except CrawlError as error:
        errors.append(str(error))
    if fallback and not partial and fallback.get('handle') == handle(url) and 0 < len(fallback.get('variants', [])) < 250:
        discarded = normalize_product_images(fallback, url)
        return {'url': url, 'source': 'collection', 'endpoint': 'listing.json',
                'product': fallback,
                'warnings': ([{'code': 'invalid_product_images_skipped',
                              'url': url, 'count': len(discarded)}]
                             if discarded else [])}
    raise CrawlError(f'商品详情 {url}: ' + '; '.join(errors))


def detail_shopify_origin(detection):
    """Return the separate data origin only for a proven headless storefront.

    Ordinary Shopify themes sometimes retain an obsolete ``myshopify.com``
    declaration after a domain/store migration.  Their public collection and
    product JSON routes are authoritative, so redirecting PDP requests to that
    declaration can silently cross into an unrelated legacy store.
    """
    if not isinstance(detection, dict):
        return ''
    if not (detection.get('headless_shopify')
            or detection.get('mode') == 'hydrogen_shopify'):
        return ''
    origin = detection.get('shopify_origin', '')
    return origin.rstrip('/') if isinstance(origin, str) and origin.startswith('https://') else ''


FIELDS = ['Type', 'SKU', 'Name', 'Description', 'Sale price', 'Regular price', 'Categories', 'Tags', 'Images', 'Parent',
          'Attribute 1 name', 'Attribute 1 value(s)', 'Attribute 2 name', 'Attribute 2 value(s)',
          'Attribute 3 name', 'Attribute 3 value(s)', 'brand', 'Stock', 'In stock?', 'is_upload',
          'External URL', 'Product ID', 'Variant ID', 'Currency', 'Source SKU', 'Variant Image']


def category_leaf_label(key):
    """Return the visible leaf label from a cumulative navigation key."""
    parts = [part.strip() for part in str(key).split(',') if part.strip()]
    if not parts:
        return ''
    leaf = parts[-1]
    if len(parts) > 1:
        parent = parts[-2]
        if leaf.casefold().startswith(parent.casefold() + ' '):
            leaf = leaf[len(parent):].strip()
    return leaf or parts[-1]


def primary_collection(categories):
    """Choose a deterministic deepest collection for Shopify's single field."""
    values = [value for value in categories if category_leaf_label(value)]
    if not values:
        return ''
    key = max(enumerate(values), key=lambda item: (item[1].count(','), -item[0]))[1]
    return category_leaf_label(key)


def write_shopify_collection_assignments(out, groups):
    """Persist every verified collection membership, including secondary ones."""
    rows = []
    for handle_value, categories in groups:
        seen = set()
        for key in categories:
            label = category_leaf_label(key)
            identity = label.casefold()
            if not label or identity in seen:
                continue
            seen.add(identity)
            rows.append({'Handle': handle_value, 'Collection': label,
                         'Navigation Path': str(key)})
    write_csv(Path(out) / 'shopify_collection_assignments.csv', rows,
              ['Handle', 'Collection', 'Navigation Path'])
    return rows


def variant_external_url(sample, variant):
    url = sample['url']
    from .custom_site import STOREFRONT_MODULES
    if (sample.get('source_mapping') or {}).get('kind') in STOREFRONT_MODULES:
        target = variant.get('source_variant_url') or url
        actual, origin = urlsplit(target), urlsplit(url)
        if actual.scheme != origin.scheme or actual.netloc != origin.netloc or actual.username is not None:
            raise CrawlError('变体链接与商品页来源不一致')
        return target
    if (sample.get('source_mapping') or {}).get('kind') == 'braun_bigcommerce':
        return url
    target = variant.get('source_variant_url')
    if target and (sample.get('source_format') == 'hydrogen_devalue' or
                   (sample.get('source_mapping') or {}).get('kind') == 'hydrogen_devalue'):
        actual, origin = urlsplit(target), urlsplit(url)
        if actual.scheme != origin.scheme or actual.netloc != origin.netloc or not handle(target):
            raise CrawlError('变体链接与商品页来源不一致')
        return target
    return query(url, variant=variant['id'])


def product_rows(sample, categories):
    p, source, url = sample['product'], sample['source'], sample['url']
    if p.get('currency', 'USD') != 'USD':
        raise CrawlError('商品币种不是 USD')
    rows = []
    for v in p['variants']:
        price = money(v['price'], source)
        compare = money(v['compare_at_price'], source) if v.get('compare_at_price') not in ('', None) else ''
        sale = bool(compare and Decimal(compare) > Decimal(price))
        row = dict.fromkeys(FIELDS, '')
        row.update({'Type': 'variation', 'SKU': str(v['id']), 'Name': p['title'],
                    'Description': clean_description(p.get('body_html', p.get('description', ''))),
                    'Sale price': price if sale else '', 'Regular price': compare if sale else price,
                    'Categories': ','.join(categories), 'Tags': ','.join(categories),
                    'Images': ','.join(product_gallery(p)), 'Variant Image': next(iter(variant_images(p, v)), ''), 'Parent': str(p['id']), 'brand': p.get('vendor', ''),
                    'Stock': str(v.get('inventory_quantity', '')), 'In stock?': str(int(v['available'])) if 'available' in v else '',
                    'is_upload': '0', 'External URL': variant_external_url(sample, v),
                    'Product ID': str(p['id']), 'Variant ID': str(v['id']), 'Currency': 'USD', 'Source SKU': v.get('sku') or ''})
        for i, option in enumerate(p.get('options', [])[:3], 1):
            row[f'Attribute {i} name'] = option.get('name', '') if isinstance(option, dict) else option
            row[f'Attribute {i} value(s)'] = v.get(f'option{i}') or ''
        rows.append(row)
    return rows


def write_csv(path, rows, fields):
    with Path(path).open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def explicit_colorway_entries(html, seed_handle):
    """Read a PDP's explicit cross-product colour selector.

    Some Shopify storefronts model every colour as a separate Product while
    the PDP exposes one user-facing product through a structured colourway
    list.  Only accept a JSON array that contains the current product and
    supplies a handle, colour label and Shopify product GID for every member;
    title similarity is deliberately not evidence of a merge relationship.
    """
    decoder = json.JSONDecoder()
    markers = ('"available-colors":', '"availableColors":')
    for marker in markers:
        start = 0
        while True:
            position = html.find(marker, start)
            if position < 0:
                break
            start = position + len(marker)
            try:
                value, _ = decoder.raw_decode(html[start:].lstrip())
            except (ValueError, TypeError):
                continue
            if not isinstance(value, list) or not 1 < len(value) <= 100:
                continue
            entries, seen = [], set()
            for item in value:
                if not isinstance(item, dict):
                    entries = []
                    break
                handle_value = str(item.get('handle', '')).strip()
                colour = str(item.get('name') or item.get('color') or '').strip()
                gid = str(item.get('id', '')).strip()
                match = re.fullmatch(r'gid://shopify/Product/(\d+)', gid)
                if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', handle_value)
                        or not colour or not match or handle_value in seen):
                    entries = []
                    break
                seen.add(handle_value)
                swatch_image = image_url(str(item.get('productImage', '')).strip())
                if swatch_image and urlsplit(swatch_image).scheme not in ('http', 'https'):
                    swatch_image = ''
                entries.append({'handle': handle_value, 'color': colour,
                                'product_id': match.group(1),
                                'product_image': swatch_image})
            if entries and seed_handle in seen:
                return entries
    return hydrogen_group_entries(html, seed_handle)


def _colour_slug(value):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', value.casefold())).strip('-')


def _merged_identity(seed, entry):
    product = seed['product']
    if entry.get('group_title') and entry.get('group_handle'):
        return entry['group_handle'], entry['group_title']
    title = str(product.get('title', '')).strip()
    colour = entry['color']
    suffix = ' - ' + colour
    group_title = title[:-len(suffix)].strip() if title.casefold().endswith(suffix.casefold()) else title
    slug = product['handle']
    colour_slug = _colour_slug(colour)
    if colour_slug and slug.casefold().endswith('-' + colour_slug):
        slug = slug[:-(len(colour_slug) + 1)].rstrip('-')
    if not slug:
        slug = product['handle'] + '-merged'
    return slug, group_title or title


def single_product_colour(product):
    """Return the one source colour represented by a split Product."""
    for index, option in enumerate(product.get('options', [])[:3], 1):
        name = option.get('name', '') if isinstance(option, dict) else str(option)
        if name.casefold() not in {'color', 'colour'}:
            continue
        values = list(dict.fromkeys(str(variant.get(f'option{index}') or '')
                                    for variant in product.get('variants', [])))
        values = [value for value in values if value]
        return values[0] if len(values) == 1 else ''
    return ''


def merged_options(options, colour):
    options = [(name, value) for name, value in options if name or value]
    if colour and options == [('Title', 'Default Title')]:
        options = []
    if colour and not any(name.casefold() in {'color', 'colour'} for name, _ in options):
        if len(options) >= 3:
            raise CrawlError('拆分色款已有三个非颜色选项，无法安全合并')
        options.insert(0, ('Color', colour))
    return options


def _shopify_row(source_row, handle_value, first, group_title, colour,
                  colour_image='', categories=()):
    collection = primary_collection(categories)
    item = {'Handle': handle_value, 'Title': group_title if first else '',
            'Body (HTML)': source_row['Description'] if first else '',
            'Vendor': source_row['brand'] if first else '',
            # Preserve the user's established WP -> Shopify contract: the
            # complete verified category membership is also the product Type.
            'Type': source_row['Tags'] if first else '',
            'Collection': collection if first else '',
            'Tags': source_row['Tags'] if first else '',
            'Published': 'FALSE' if first else '',
            'Variant SKU': source_row['SKU'],
            'Variant Price': source_row['Sale price'] or source_row['Regular price'],
            'Variant Compare At Price': (source_row['Regular price']
                                         if source_row['Sale price'] else ''),
            'Image Src': '', 'Image Position': '',
            'Variant Image': colour_image or source_row['Variant Image'],
            'Status': 'draft' if first else ''}
    options = [(source_row[f'Attribute {index} name'],
                source_row[f'Attribute {index} value(s)']) for index in range(1, 4)]
    options = merged_options(options, colour)
    for index in range(1, 4):
        name, value = options[index - 1] if index <= len(options) else ('', '')
        item[f'Option{index} Name'] = name
        item[f'Option{index} Value'] = value
    return item


def merge_explicit_colorways(client, samples, detail, out,
                             import_name='shopify_import.csv',
                             manifest_name='shopify_merge_groups.json',
                             output_suffix=''):
    """Rewrite Shopify import rows so explicit PDP colourways share one handle.

    Source samples/result.csv remain untouched and independently verifiable.
    Extra colourway products are persisted verbatim under evidence/ and the
    merge manifest binds every imported variant back to its source product.
    """
    out = Path(out)
    base = next((urlsplit(sample['url'])._replace(path='/', query='', fragment='').geturl()
                 for sample in samples), '')
    groups, claimed = [], set()
    for seed in samples:
        if seed['product']['handle'] in claimed:
            continue
        html = client.get(seed['url']).text
        entries = explicit_colorway_entries(html, seed['product']['handle'])
        if not entries:
            continue
        group_handle, group_title = _merged_identity(
            seed, next(item for item in entries if item['handle'] == seed['product']['handle']))
        evidence_name = 'evidence/colorways-' + str(seed['product']['id']) + '.html'
        (out / evidence_name).write_text(html, encoding='utf-8')
        embedded = hydrogen_group_products(html)
        members = []
        for entry in entries:
            product_url_value = urljoin(base, '/products/' + entry['handle'])
            if entry['handle'] == seed['product']['handle']:
                member = seed
            elif entry['handle'] in embedded:
                member = {'url': product_url_value, 'source': 'collection',
                          'endpoint': seed['url'], 'product': embedded[entry['handle']]}
            else:
                member = fetch_product(client, product_url_value)
            product = member['product']
            if (product.get('handle') != entry['handle']
                    or str(product.get('id')) != entry['product_id']):
                raise CrawlError('颜色合并成员与商品页显式 Product ID 不一致')
            source_name = 'evidence/product-' + str(product['id']) + '.json'
            save_json(out / source_name, product)
            colour = entry['color'] or single_product_colour(product)
            if not colour:
                raise CrawlError('显式颜色成员没有唯一真实 Color 选项')
            colour_image = entry.get('product_image', '') or next(
                iter(product_gallery(product)), '')
            members.append({'sample': member, 'color': colour,
                            'color_image': colour_image,
                            'source_file': source_name})
            claimed.add(entry['handle'])
        categories = list(dict.fromkeys(
            key for key, urls in detail.items()
            if any(member['sample']['url'] in urls for member in members)))
        groups.append({'handle': group_handle, 'title': group_title,
                       'seed_handle': seed['product']['handle'],
                       'source': 'pdp_explicit_colorways', 'evidence': evidence_name,
                       'categories': categories, 'members': members})
    if not groups:
        return []

    # Keep samples with no explicit colourway group in the same import file.
    for seed in samples:
        if seed['product']['handle'] not in claimed:
            categories = [key for key, urls in detail.items() if seed['url'] in urls]
            groups.append({'handle': seed['product']['handle'], 'title': seed['product']['title'],
                           'seed_handle': seed['product']['handle'], 'source': 'single_product',
                           'evidence': '', 'categories': categories,
                           'members': [{'sample': seed, 'color': '',
                                       'color_image': '',
                                       'source_file': 'evidence/product-' + str(seed['product']['id']) + '.json'}]})

    imported, merged_wp, manifest_groups = [], [], []
    for group in groups:
        group_rows, gallery, first = [], [], True
        manifest_members = []
        for member in group['members']:
            sample, colour = member['sample'], member['color']
            for source_row in product_rows(sample, group['categories']):
                group_rows.append(_shopify_row(source_row, group['handle'], first,
                                               group['title'], colour,
                                               member.get('color_image', ''),
                                               group['categories']))
                first = False
            gallery.extend(product_gallery(sample['product']))
            manifest_members.append({
                'handle': sample['product']['handle'],
                'product_id': str(sample['product']['id']),
                'color': colour,
                'variant_image': member.get('color_image', ''),
                'source_file': member['source_file'],
                'variant_ids': [str(variant['id']) for variant in sample['product']['variants']],
            })
        gallery = list(dict.fromkeys(gallery))
        seed_product = next(member['sample']['product'] for member in group['members']
                            if member['sample']['product']['handle'] == group['seed_handle'])
        parent_id = str(seed_product['id'])
        for member in group['members']:
            for source_row in product_rows(member['sample'], group['categories']):
                row = dict(source_row)
                row['Name'] = group['title']
                row['Parent'] = parent_id
                row['Product ID'] = parent_id
                row['Images'] = ','.join(gallery)
                row['Variant Image'] = (member.get('color_image', '')
                                        or row['Variant Image'])
                attributes = [(row[f'Attribute {index} name'],
                               row[f'Attribute {index} value(s)']) for index in range(1, 4)]
                attributes = merged_options(attributes, member['color'])
                for index in range(1, 4):
                    name, value = attributes[index - 1] if index <= len(attributes) else ('', '')
                    row[f'Attribute {index} name'] = name
                    row[f'Attribute {index} value(s)'] = value
                merged_wp.append(row)
        for index, src in enumerate(gallery):
            if index < len(group_rows):
                target = group_rows[index]
            else:
                target = dict.fromkeys(group_rows[0], '')
                target['Handle'] = group['handle']
                group_rows.append(target)
            target['Image Src'] = src
            target['Image Position'] = str(index + 1)
        imported.extend(group_rows)
        manifest_groups.append({'handle': group['handle'], 'title': group['title'],
                                'seed_handle': group['seed_handle'], 'source': group['source'],
                                'evidence': group['evidence'], 'categories': group['categories'],
                                'members': manifest_members, 'images': gallery})
    write_csv(out / import_name, imported, list(imported[0]))
    write_shopify_collection_assignments(
        out, [(group['handle'], group['categories']) for group in groups])
    unique_wp = {}
    for row in merged_wp:
        identity = (row['Parent'], row['Variant ID'])
        if identity in unique_wp and unique_wp[identity] != row:
            raise CrawlError('WP 合并变体数据冲突')
        unique_wp.setdefault(identity, row)
    merged_wp = list(unique_wp.values())
    write_csv(out / f'result_merged{output_suffix}.csv', merged_wp, FIELDS)
    write_csv(out / f'result_clean_merged{output_suffix}.csv', merged_wp, FIELDS)
    manifest = {'version': 1, 'method': 'explicit_colorway_merge',
                'groups': manifest_groups,
                'source_products': sum(len(group['members']) for group in manifest_groups),
                'import_products': len(manifest_groups),
                'variants': sum(len(member['variant_ids']) for group in manifest_groups
                                for member in group['members']),
                'wordpress_rows': len(merged_wp)}
    save_json(out / manifest_name, manifest)
    write_wp_parent_products(out, manifest_name, output_suffix)
    return manifest_groups


def write_wp_parent_products(out, manifest_name='shopify_merge_groups.json',
                             output_suffix=''):
    """Create WooCommerce variable parents and a parent+variation import file."""
    out = Path(out)
    manifest = read_json(out / manifest_name)
    parents = []
    for group in manifest.get('groups', []):
        products = []
        for member in group.get('members', []):
            product = read_json(out / member['source_file'])
            product = product.get('product', product)
            products.append((product, str(member.get('color', ''))))
        if not products:
            raise CrawlError('WP 父商品缺少来源成员')
        seed = next((product for product, _ in products
                     if product.get('handle') == group.get('seed_handle')), products[0][0])
        row = dict.fromkeys(FIELDS, '')
        row.update({'Type': 'variable', 'SKU': group['handle'], 'Name': group['title'],
                    'Description': clean_description(seed.get('body_html', seed.get('description', ''))),
                    'Categories': ','.join(group.get('categories', [])),
                    'Tags': ','.join(group.get('categories', [])),
                    'Images': ','.join(group.get('images', [])),
                    'brand': seed.get('vendor', ''), 'is_upload': '0',
                    'External URL': urljoin(
                        urlsplit(next(iter(products))[0].get('url', ''))._replace(path='/', query='', fragment='').geturl(),
                        '/products/' + group['seed_handle']) if seed.get('url') else '',
                    'Product ID': str(seed['id']), 'Currency': 'USD'})
        option_values = {}
        option_order = []
        for product, colour in products:
            options = [(option.get('name', '') if isinstance(option, dict) else str(option),
                        index) for index, option in enumerate(product.get('options', [])[:3], 1)]
            for variant in product.get('variants', []):
                dimensions = [(name, str(variant.get(f'option{index}') or '')) for name, index in options]
                for name, value in merged_options(dimensions, colour):
                    if not name:
                        continue
                    canonical_name = 'Color' if name.casefold() in {'color', 'colour'} else name
                    if canonical_name not in option_values:
                        option_order.append(canonical_name)
                        option_values[canonical_name] = []
                    if value and value not in option_values[canonical_name]:
                        option_values[canonical_name].append(value)
        if len(option_order) > 3:
            raise CrawlError('WP 父商品选项超过三个')
        for index, name in enumerate(option_order, 1):
            row[f'Attribute {index} name'] = name
            row[f'Attribute {index} value(s)'] = ', '.join(option_values[name])
        parents.append(row)
    write_csv(out / f'wp_parent_products{output_suffix}.csv', parents, FIELDS)
    with (out / f'result_clean_merged{output_suffix}.csv').open(
            encoding='utf-8-sig', newline='') as handle:
        variations = list(csv.DictReader(handle))
    combined = []
    by_parent = {}
    for row in variations:
        by_parent.setdefault(row['Parent'], []).append(row)
    for parent in parents:
        combined.append(parent)
        for variation in by_parent.get(parent['Product ID'], []):
            combined.append(dict(variation, Parent=parent['SKU'], Images=variation['Variant Image']))
    if len(combined) != len(parents) + len(variations):
        raise CrawlError('WP 父商品与变体归属不完整')
    write_csv(out / f'wp_import_merged{output_suffix}.csv', combined, FIELDS)
    return parents


def product_categories(sample, detail, listings=None):
    from .custom_site import STOREFRONT_MODULES
    urls = {_membership_url(sample['url'])}
    kind = sample.get('source_mapping', {}).get('kind')
    if kind in STOREFRONT_MODULES:
        for key, listing in (listings or {}).items():
            for url, source in listing.get('identity_sources', {}).items():
                if (listing.get('format') == kind
                        and source.get('url') == url and url in detail.get(key, [])
                        and source.get('source_mapping', {}).get('file')
                        and source.get('source_mapping', {}).get('kind') == kind
                        and str(source['product']['id']) == str(sample['product']['id'])):
                    urls.add(_membership_url(url))
    return [key for key, links in detail.items()
            if urls.intersection(_membership_url(url) for url in links)]


def export(samples, detail, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    listing_path = out / 'listings.json'
    listings = read_json(listing_path) if listing_path.is_file() else {}
    rows = []
    categories_by_product = {}
    for sample in samples:
        cats = product_categories(sample, detail, listings)
        if not cats:
            raise CrawlError('父商品缺少分类归属')
        categories_by_product[str(sample['product']['id'])] = cats
        rows.extend(product_rows(sample, cats))
    write_csv(out / 'result.csv', rows, FIELDS)
    unique = {}
    for row in rows:
        key = (row['Product ID'], row['Variant ID'])
        if key in unique and row != unique[key]:
            raise CrawlError('重复变体数据冲突')
        unique.setdefault(key, row)
    cleaned = list(unique.values())
    write_csv(out / 'result_clean.csv', cleaned, FIELDS)
    shopify = []
    handles = {str(s['product']['id']): product_handle(s['product'], s.get('url', ''))
               for s in samples}
    seen = set()
    for row in cleaned:
        first = row['Product ID'] not in seen
        seen.add(row['Product ID'])
        categories = categories_by_product.get(row['Product ID'], [])
        collection = primary_collection(categories)
        item = {'Handle': handles[row['Product ID']], 'Title': row['Name'] if first else '',
                'Body (HTML)': row['Description'] if first else '', 'Vendor': row['brand'] if first else '',
                'Type': row['Tags'] if first else '',
                'Collection': collection if first else '',
                'Tags': row['Tags'] if first else '', 'Published': 'FALSE' if first else '',
                'Variant SKU': row['SKU'], 'Variant Price': row['Sale price'] or row['Regular price'],
                'Variant Compare At Price': row['Regular price'] if row['Sale price'] else '',
                'Image Src': '', 'Image Position': '', 'Variant Image': row['Variant Image'],
                'Status': 'draft' if first else ''}
        for i in range(1, 4):
            item[f'Option{i} Name'] = row[f'Attribute {i} name']
            item[f'Option{i} Value'] = row[f'Attribute {i} value(s)']
        shopify.append(item)
    if not shopify:
        raise CrawlError('没有可导出的真实变体')
    for sample in samples:
        slug = product_handle(sample['product'], sample.get('url', ''))
        slots = [r for r in shopify if r['Handle'] == slug]
        for index, src in enumerate(product_gallery(sample['product'])):
            if index < len(slots):
                target = slots[index]
            else:
                target = dict.fromkeys(shopify[0], '')
                target['Handle'] = slug
                shopify.append(target)
            target['Image Src'] = src
            target['Image Position'] = str(index + 1)
    product_order = {product_handle(s['product'], s.get('url', '')): i
                     for i, s in enumerate(samples)}
    shopify.sort(key=lambda row: product_order[row['Handle']])
    write_csv(out / 'shopify_import.csv', shopify, list(shopify[0]))
    write_shopify_collection_assignments(
        out, [(product_handle(sample['product'], sample.get('url', '')),
               categories_by_product[str(sample['product']['id'])])
              for sample in samples])
    return rows


def run_links(root):
    from .common import Client
    from .navigation_release import require_release
    root = Path(root)
    release = require_release(root)
    categories = read_json(root / 'url.json')
    result, listings = {}, {}
    client = Client()
    # Stable category and frontend order, regardless of worker finish times.
    for key, url in categories.items():
        listings[key] = collect_listing(client, url)
        result[key] = listings[key]['urls']
        print(key, len(result[key]), flush=True)
    save_json(root / 'detail_url.json', result)
    save_json(root / 'listings.json', listings)
    save_json(root / 'sample_scope.json', {'selected_categories': categories,
               'navigation_categories': len(categories), 'listing_scope': 'all',
               'detail_scope': 'pending', 'navigation_version': release['navigation_version']})
    from .navigation_release import digest
    save_json(root / 'detail_release.json', {'navigation_version': release['navigation_version'],
                                           'sha256': digest(root / 'detail_url.json')})
    return result


def run_products(root, all_products=False):
    from .common import Client
    from .navigation_release import require_release, digest
    root = Path(root)
    release = require_release(root)
    binding = read_json(root / 'detail_release.json')
    if binding.get('navigation_version') != release['navigation_version'] or binding.get('sha256') != digest(root / 'detail_url.json'):
        raise CrawlError('商品链接与已审核目录版本不一致')
    detail = read_json(root / 'detail_url.json')
    urls = list(dict.fromkeys(u for links in detail.values() for u in links))
    if not all_products:
        urls = urls[:3]
    if not urls:
        raise CrawlError('已审核范围没有可采集商品')
    client = Client()
    detection_file = root / 'detection.json'
    if detection_file.is_file():
        detected_origin = detail_shopify_origin(read_json(detection_file))
        if detected_origin:
            client.shopify_origin = detected_origin
    checkpoint_file = root / 'product_checkpoint.json'
    checkpoint = read_json(checkpoint_file) if checkpoint_file.is_file() else {}
    version = release['navigation_version'] + ':' + binding['sha256']
    if checkpoint.get('version') != version:
        checkpoint = {'version': version, 'completed': {}}
    cache = root / 'evidence' / 'product-checkpoints'; cache.mkdir(parents=True, exist_ok=True)
    samples = []
    failed_products = []
    transient_failures = []
    no_image_failures = []
    if all_products and (root / 'failed_products.json').is_file():
        (root / 'failed_products.json').unlink()
    save_json(root / 'product_run.json', {'status': 'running', 'planned': len(urls), 'navigation_version': release['navigation_version']})
    try:
        for index, url in enumerate(urls):
            saved = checkpoint['completed'].get(url)
            file = cache / f'{index:06d}.json'
            if saved and file.is_file() and saved.get('sha256') == digest(file):
                sample = read_json(file)
                if sample.get('url') != url:
                    raise CrawlError('商品断点 URL 不匹配')
            else:
                try:
                    sample = fetch_product(client, url)
                except CrawlError as error:
                    message = str(error).casefold()
                    transient = any(marker in message for marker in (
                        'could not resolve host', 'name resolution', 'dns',
                        'curl: (6)', 'curl: (28)', 'curl: (35)', 'curl: (56)',
                        'ssl_connect', 'connection closed', 'connection reset',
                        'timed out', 'timeout'))
                    if not all_products or not transient:
                        raise
                    failed_products.append({'url': url, 'error': str(error),
                                            'exception_type': type(error).__name__,
                                            'code': 'transient_network_error'})
                    transient_failures.append(failed_products[-1])
                    save_json(root / 'failed_products.json', failed_products)
                    continue
                save_json(file, sample)
                checkpoint['completed'][url] = {'sha256': digest(file)}
                save_json(checkpoint_file, checkpoint)
            if all_products and not product_gallery(sample['product']):
                if not source_product_without_images(sample):
                    raise CrawlError('商品源数据包含非法图片 URL: ' + url)
                failure = {'url': url, 'error': '官方商品源数据 images 为空',
                           'exception_type': 'SourceProductWithoutImages',
                           'code': 'source_product_without_images'}
                failed_products.append(failure)
                no_image_failures.append(failure)
                save_json(root / 'failed_products.json', failed_products)
                continue
            save_json(root / 'evidence' / f'product-{sample["product"]["id"]}.json', sample['product'])
            samples.append(sample)
            print('product', index + 1, '/', len(urls), url, flush=True)
    except Exception as error:
        save_json(root / 'product_run.json', {'status': 'failed', 'error': str(error), 'completed': len(samples), 'planned': len(urls)})
        raise
    save_json(root / 'samples.json', samples)
    image_warnings = [warning for sample in samples for warning in sample.get('warnings', [])]
    if image_warnings:
        warnings_path = root / 'warnings.json'
        warnings = read_json(warnings_path) if warnings_path.is_file() else []
        warnings.extend(image_warnings)
        save_json(warnings_path, warnings)
    if failed_products:
        ratio = len(failed_products) / max(1, len(urls))
        warnings_path = root / 'warnings.json'
        warnings = read_json(warnings_path) if warnings_path.is_file() else []
        if transient_failures:
            warnings.append({'code': 'transient_products_skipped',
                             'message': '个别商品临时网络失败；已保留成功断点并继续导出',
                             'failed': len(transient_failures), 'planned': len(urls),
                             'ratio': round(len(transient_failures) / max(1, len(urls)), 4)})
        if no_image_failures:
            warnings.append({'code': 'products_without_images_skipped',
                             'message': '官方源数据本身没有图片的少量商品已跳过；其余商品继续导出',
                             'failed': len(no_image_failures), 'planned': len(urls),
                             'ratio': round(len(no_image_failures) / max(1, len(urls)), 4),
                             'urls': [item['url'] for item in no_image_failures]})
        save_json(warnings_path, warnings)
        if len(samples) < 3 or ratio > .20:
            save_json(root / 'product_run.json', {'status': 'failed',
                      'error': '临时网络失败比例超过 20%',
                      'completed': len(samples), 'planned': len(urls)})
            raise CrawlError('全量商品临时网络失败比例超过 20%')
    scope = read_json(root / 'sample_scope.json')
    scope['detail_scope'] = 'all_products' if all_products else 'first_3_products'
    save_json(root / 'sample_scope.json', scope)
    export(samples, detail, root)
    from .validator import validate
    save_json(root / 'product_run.json', {'status': 'validating', 'planned': len(urls)})
    try:
        result = validate(root)
    except Exception as error:
        save_json(root / 'product_run.json', {'status': 'failed', 'error': str(error), 'completed': len(samples)})
        raise
    save_json(root / 'product_run.json', {'status': 'completed', 'validation': result, 'navigation_version': release['navigation_version']})
    client.clear_cache()
    gc.collect()
    return result
