"""Navigation recovery from public anchors without platform-specific URL paths."""
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote

from bs4 import Comment

from .common import AdapterRequired, CrawlError, save_json
from .html_parser import parse_soup


def _utility_region(node):
    marker = ' '.join(node.get('class', [])) + ' ' + ' '.join(
        node.get(key, '') for key in ('id', 'aria-label', 'aria-labelledby'))
    return re.search(r'footer|breadcrumb|copyright|account|customer|navUser|utility|skip links|'
                     r'(?:store|country|currency|language)[-_ ]?(?:switcher|selector)', marker, re.I)


def catalog_url(url, base):
    target, origin = urlsplit(url), urlsplit(base)
    path = unquote(target.path).lower()
    blocked_path = re.search(
        r'/(?:products?|items|pages|blogs?|account|customer|login|logout|cart|checkout|search|catalogsearch|wishlist|support|privacy|terms)(?:/|\.|$)',
        path)
    if path.rstrip('/') == '/products':
        blocked_path = None
    return (target.scheme in {'https', 'http'} and target.netloc == origin.netloc
            and not target.username and path not in {'', '/'}
            and path.rstrip('/') != unquote(origin.path).lower().rstrip('/')
            and not blocked_path
            and not re.search(r'/(?:about(?:-us)?|contact(?:-us)?|find-a-retailer|where-to-buy|'
                              r'careers|press|news|faq)(?:/|\.|$)', path)
            and not re.search(r'e?gifts?[-_]?cards?|warranty|store[-_]?(?:locator|finder)', path)
            and not target.fragment)


def _product_links(html, page_url, base):
    soup = parse_soup(html)
    origin = urlsplit(base).netloc
    result = []
    for anchor in soup.select('main a[href], [role="main"] a[href], #main-content a[href]'):
        target = urljoin(page_url, anchor['href'])
        parsed = urlsplit(target)
        if (parsed.scheme not in {'http', 'https'} or parsed.netloc != origin
                or not re.match(r'^/(?:product|products)/[^/?#]+/?$', parsed.path, re.I)):
            continue
        target = parsed._replace(query='', fragment='').geturl()
        if target not in result:
            result.append(target)
    return result


def _commerce_evidence(html):
    soup = parse_soup(html)
    prices = []
    for node in soup.select('[itemprop="price"][content], meta[property="product:price:amount"][content], [data-product-price], [data-price]'):
        value = node.get('content') or node.get('data-product-price') or node.get('data-price')
        if value not in (None, '') and re.search(r'\d', str(value)):
            prices.append(str(value))
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text(strip=True))
        except (TypeError, ValueError):
            continue
        stack = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                kind = current.get('@type', '')
                kinds = kind if isinstance(kind, list) else [kind]
                if 'Product' in kinds:
                    offers = current.get('offers', {})
                    offers = offers if isinstance(offers, list) else [offers]
                    for offer in offers:
                        if isinstance(offer, dict):
                            price = offer.get('price') or offer.get('lowPrice') or offer.get('highPrice')
                            if price not in (None, ''):
                                prices.append(str(price))
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
    price_text = ' '.join(node.get_text(' ', strip=True) for node in soup.select(
        '.price, .product-price, [class*="product-price"], [class*="product_price"]'))
    if re.search(r'(?:[$€£]\s*\d|\d[\d,.]*\s*(?:USD|EUR|GBP))', price_text, re.I):
        prices.append(price_text[:200])
    purchase = bool(soup.select_one(
        'form[action*="cart"], button[name*="add-to-cart"], .single_add_to_cart_button, '
        '[data-add-to-cart], [data-variant-id], select[name*="attribute"]'))
    return {'prices': prices, 'purchase_controls': purchase}


def _gatsby_product_links(client, response, base):
    """Read the public Gatsby page-data backing a JS-only product grid."""
    html = response.text
    if not re.search(r'<meta[^>]+name=["\']generator["\'][^>]+Gatsby|___gatsby|gatsby-chunk-mapping',
                     html, re.I):
        return [], None
    parsed = urlsplit(response.url)
    page_path = parsed.path.strip('/')
    endpoint_path = '/page-data/' + (page_path + '/' if page_path else '') + 'page-data.json'
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, '', ''))
    try:
        source = client.get(endpoint, timeout=20, attempts=1)
        payload = source.json()
    except (CrawlError, ValueError, TypeError):
        return [], None

    links = []
    locale_prefix = parsed.path[:parsed.path.lower().find('/our-teas/') + 1] if '/our-teas/' in parsed.path.lower() else '/'

    def walk(value):
        if isinstance(value, dict):
            slug = value.get('slug')
            product_id = value.get('productId')
            if isinstance(slug, str) and product_id not in (None, ''):
                clean = slug.strip('/')
                target = urljoin(response.url, locale_prefix + clean + '/')
                if (urlsplit(target).netloc == urlsplit(base).netloc
                        and target.rstrip('/') != response.url.rstrip('/')
                        and target not in links):
                    links.append(target)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return links, {'url': source.url, 'json': payload}


def detect_catalog_only(client, base, categories, evidence_dir=None, max_products=3):
    """Detect product catalogs that have no public commerce facts."""
    ordered = list(dict.fromkeys(categories.values()))
    ordered.sort(key=lambda value: urlsplit(value).path.rstrip('/').lower() != '/products')
    checked_listings = []
    family_pages = []
    for listing_url in ordered[:8]:
        response = client.get(listing_url, timeout=20, attempts=1)
        links = _product_links(response.text, response.url, base)
        gatsby_source = None
        if len(links) < 3:
            links, gatsby_source = _gatsby_product_links(client, response, base)
        checked_listings.append({'url': response.url, 'product_links': links,
                                 'source': 'gatsby_page_data' if gatsby_source else 'html'})
        if len(links) < 3:
            soup = parse_soup(response.text)
            sku_links = []
            for anchor in soup.select('a[href*="where-to-buy" i]'):
                target = urljoin(response.url, anchor.get('href', ''))
                parsed = urlsplit(target)
                values = dict(parse_qsl(parsed.query, keep_blank_values=True))
                sku = str(values.get('ps-sku', '')).strip()
                if parsed.hostname == urlsplit(base).hostname and sku and sku not in sku_links:
                    sku_links.append(sku)
            evidence = _commerce_evidence(response.text)
            if sku_links and not (evidence['prices'] or evidence['purchase_controls']):
                family_pages.append({'url': response.url, 'ps_skus': sku_links, **evidence})
                if evidence_dir is not None:
                    directory = Path(evidence_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / f'catalog-only-family-{len(family_pages) - 1}.html').write_text(
                        response.text, encoding='utf-8')
            continue
        products = []
        has_commerce = False
        for index, product_url in enumerate(links[:max_products]):
            product = client.get(product_url, timeout=20, attempts=1)
            evidence = _commerce_evidence(product.text)
            has_commerce = has_commerce or bool(evidence['prices'] or evidence['purchase_controls'])
            products.append({'url': product.url, **evidence})
            if evidence_dir is not None:
                directory = Path(evidence_dir)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f'catalog-only-product-{index}.html').write_text(product.text, encoding='utf-8')
        if evidence_dir is not None and gatsby_source:
            save_json(Path(evidence_dir) / 'catalog-listing-page-data.json', gatsby_source)
        return {
            'catalog_only': not has_commerce,
            'listing_url': response.url,
            'product_links': links,
            'checked_products': products,
            'listing_source': 'gatsby_page_data' if gatsby_source else 'html',
            'reason': ('public_product_pages_without_price_or_purchase_variants'
                       if not has_commerce else 'commerce_evidence_present'),
        }
    if len(family_pages) >= 3:
        return {'catalog_only': True, 'listing_url': family_pages[0]['url'],
                'listing_urls': [item['url'] for item in family_pages],
                'product_links': [item['url'] for item in family_pages],
                'checked_products': family_pages, 'listing_source': 'product_family_pages',
                'reason': 'where_to_buy_product_families_without_public_prices'}
    return {'catalog_only': False, 'checked_listings': checked_listings,
            'reason': 'no_three_product_catalog_listing'}


def _regions(soup, base):
    # The product nav can be a sibling of a utility-only header. Prefer the
    # semantic nav itself, including panels it explicitly controls.
    candidates = [node for node in soup.select(
        'nav,[role="navigation"],[role="menubar"],'
        'header ul[class*="nav"][class*="list"],header ul#primaryNav,'
        '[id="header"] ul,[class~="header"] ul,[data-testid*="header" i] ul')
                  if node.find_parent('footer') is None
                  and not _utility_region(node)]
    def count(node):
        return sum(catalog_url(urljoin(base, a['href']), base) for a in node.select('a[href]'))
    candidates = [node for node in candidates if count(node)]
    if candidates:
        mobile = re.compile(r'mobile|drawer|hamburger|hide-for-(?:xlarge|large)|show-for-(?:small|medium-only)|d-lg-none', re.I)
        desktop = [node for node in candidates if not any(mobile.search(
            ' '.join(parent.get('class', [])) + ' ' + parent.get('id', ''))
            for parent in [node, *node.parents] if getattr(parent, 'attrs', None))]
        # A drawer-only storefront still has a real primary navigation.
        candidates = desktop or candidates
        selected = [node for node in candidates if not any(parent in candidates for parent in node.parents)]
        panels = []
        for node in selected:
            for trigger in node.select('[aria-controls],[data-controls],[data-collapsible]'):
                for attr in ('aria-controls', 'data-controls', 'data-collapsible'):
                    for target in trigger.get(attr, '').split():
                        panel = soup.find(id=target.lstrip('#'))
                        if (panel is not None and panel not in selected and panel not in panels
                                and not _utility_region(panel) and not _utility_region(trigger)
                                and not any(parent in selected for parent in panel.parents)):
                            panels.append(panel)
        return selected + panels
    # HTML5 allows content sections to use semantic <header> elements.  A
    # single editorial/campaign link is not evidence of a storefront menu;
    # requiring at least two catalog targets prevents homepage section titles
    # from becoming the entire category JSON when the real React header is a
    # div/custom component.
    return [node for node in soup.find_all('header')
            if sum(catalog_url(urljoin(base, anchor['href']), base)
                   for anchor in node.select('a[href]')) >= 2]


def _anchors(region):
    # This menu schema labels every navigation entry with data-menu. Sibling
    # image cards are merchandising, and may link directly to individual SKUs.
    if region.select_one('ul.main__list,ul.nav__list') and region.select_one('a[data-menu][href]'):
        return region.select('a[data-menu][href]')
    return [anchor for anchor in region.select('a[href]')
            if anchor.get('data-link-type') not in {'product', 'content', 'editorial'}
            and not any(parent.has_attr('data-product-id') or parent.has_attr('data-product-sku')
                        for parent in [anchor, *anchor.parents] if getattr(parent, 'attrs', None))]


def _comparison_url(url):
    """Ignore attribution when comparing, keeping actual filter parameters."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith(('utm_', 'icid'))
             and key.lower() not in {'gclid', 'fbclid'}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), urlencode(sorted(query)), ''))


def navigation_source(html, base, extra_documents=()):
    regions = []
    for document in [html, *extra_documents]:
        soup = parse_soup(document)
        for node in soup.select('script,style,svg,noscript,img,footer'):
            node.decompose()
        for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
            node.extract()
        selected = _regions(soup, base)
        if not selected:
            # Some storefronts render their desktop mega menu in a custom
            # element (or directly below body) instead of header/nav.  The
            # document has already had footer/scripts/editorial media removed;
            # only use this broad fallback when it contains a real catalogue
            # set, so a lone campaign link cannot become the whole taxonomy.
            catalogue_count = sum(
                catalog_url(urljoin(base, anchor.get('href', '')), base)
                for anchor in soup.select('a[href]'))
            if document != html or catalogue_count >= 3:
                selected = [soup]
        for region in selected:
            allowed_anchors = {id(anchor) for anchor in _anchors(region)}
            for anchor in region.select('a[href]'):
                if id(anchor) not in allowed_anchors:
                    anchor.decompose()
            for node in [region, *region.find_all(True)]:
                node.attrs = {key: value for key, value in node.attrs.items()
                              if key in {'href', 'id', 'aria-label', 'aria-controls', 'data-controls',
                                         'data-collapsible', 'class', 'hidden', 'role'}}
            regions.append(region)
    urls = list(dict.fromkeys(urljoin(base, a['href']) for region in regions for a in region.select('a[href]')
                             if catalog_url(urljoin(base, a['href']), base)))
    fragments = list(dict.fromkeys(re.sub(r'\s+', ' ', str(region)) for region in regions))
    navigation = '\n'.join(fragments)
    return {'base_url': base, 'url_policy': 'custom', 'valid_urls': urls, 'bytes': len(navigation.encode('utf-8')),
            'source_navigation_urls': urls, 'navigation_html': navigation,
            'self_heal_html': navigation, 'navigation_outline': '',
            'text': 'BASE ' + base + '\nNAVIGATION HTML\n' + navigation,
            'navigation_coverage': {'source_urls': len(urls), 'ratio': 1.0 if urls else 0.0,
                                    'needs_self_heal': False, 'source_roots': [], 'outline_roots': []}}


def audit_navigation(categories, html, base, extra_documents=()):
    source = navigation_source(html, base, extra_documents)
    originals = {_comparison_url(url): url for url in source['source_navigation_urls']}
    expected = set(originals)
    observed = expected.intersection(_comparison_url(url) for url in categories.values())
    ratio = len(observed) / len(expected) if expected else 0.0
    return {'passed': bool(categories) and (not expected or ratio >= .6),
            'source_urls': len(expected), 'matched_urls': len(observed),
            'ratio': round(ratio, 4), 'missing_urls': sorted(originals[url] for url in expected - observed)}


def recover_navigation(root, base, html, reason, extra_documents=()):
    from .simple_nav import _thin_flat_navigation, extract_url_json_with_ai, rendered_html
    root = Path(root)
    source = navigation_source(html, base, extra_documents)
    if not extra_documents and _thin_flat_navigation(source, html):
        try:
            documents = rendered_html(base, root / 'evidence')
            expanded = navigation_source(html, base, documents)
            if set(expanded['valid_urls']) - set(source['valid_urls']):
                source = expanded
        except Exception as error:
            save_json(root / 'browser_warning.json', {'error': str(error)})
    save_json(root / 'navigation_repair.json', {'status': 'running', 'reason': str(reason),
                                               'source_urls': len(source['valid_urls'])})
    try:
        if not source['valid_urls']:
            raise AdapterRequired('菜单没有可供恢复的真实链接，需要动态菜单源')
        categories = extract_url_json_with_ai(root, base, source)
        save_json(root / 'navigation_repair.json', {'status': 'completed', 'reason': str(reason),
                                                   'categories': len(categories)})
        return categories
    except Exception as error:
        save_json(root / 'navigation_repair.json', {'status': 'failed', 'reason': str(reason),
                                                   'error': str(error)})
        raise
