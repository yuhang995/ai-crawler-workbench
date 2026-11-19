"""Site routing adapted from Power-Spider shopify_detector.py."""
from __future__ import annotations

import re
import json
from urllib.parse import urljoin, urlsplit
from .common import CrawlError, query


def observe_market(html):
    """Adapted from ai爬虫 2/market_policy.py: require observed country+currency."""
    countries = set(re.findall(r'Shopify\.country\s*=\s*[\"\x27]([A-Z]{2})[\"\x27]', html))
    currencies = set()
    for text in re.findall(r'Shopify\.currency\s*=\s*(\{[^;]+?\})', html):
        try:
            currencies.add(json.loads(text)['active'])
        except (ValueError, KeyError):
            pass
    from .html_parser import parse_soup as BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    for node in soup.select('[data-current-country],select[name=country_code] option[selected]'):
        countries.add(node.get('data-current-country') or node.get('value'))
    for node in soup.select('[data-current-currency],meta[property="og:price:currency"],meta[itemprop=priceCurrency]'):
        currencies.add(node.get('data-current-currency') or node.get('content'))
    # Hydrogen v2 storefronts may expose the same React Router/devalue loader
    # through either the legacy React Router global or Remix's current global.
    # The parser supports both; do not skip its verified locale merely because
    # the JavaScript namespace changed.
    if any(marker in html for marker in (
            '__reactRouterContext.streamController.enqueue',
            '__remixContext.streamController.enqueue')):
        from .shopify_fastpath import hydrogen_market
        selected = hydrogen_market(html)
        if selected['country']:
            countries.add(selected['country'])
        if selected['currency']:
            currencies.add(selected['currency'])
    countries.discard(None)
    currencies.discard(None)
    return {'countries': sorted(countries), 'currencies': sorted(currencies),
            'confirmed': countries == {'US'} and currencies == {'USD'}}


def detect(client, base, html, categories):
    from .custom_site import identify_platform
    native_platform = identify_platform(html)
    hydrogen_marker = ('oxygen-v2' in html
                       or '__reactRouterContext.streamController.enqueue' in html
                       or '__remixContext.streamController.enqueue' in html)
    headless_shopify = bool(re.search(
        r'''(?:isHeadless\s*:\s*true|["']isHeadless["']\s*:\s*true)''', html, re.I))
    shopify_origin = ''
    origin_match = re.search(
        r'''(?:TripleName\s*:\s*["']|https?://)([a-z0-9][a-z0-9-]*\.myshopify\.com)''',
        html, re.I)
    if origin_match:
        shopify_origin = 'https://' + origin_match.group(1).lower()
    if hydrogen_marker and not shopify_origin:
        # Some Oxygen storefronts publish a separate canonical Shopify data
        # domain in their server-rendered root loader. Decode that declaration
        # instead of guessing from the branded hostname.
        from .shopify_fastpath import react_router_payloads
        for payload in react_router_payloads(html):
            root = (payload.get('loaderData') or {}).get('root')
            configs = root.get('envConfigs') if isinstance(root, dict) else None
            declared = configs.get('shopPrimaryDomainUrl') if isinstance(configs, dict) else None
            parsed_declared = urlsplit(str(declared or ''))
            if (parsed_declared.scheme == 'https' and parsed_declared.hostname
                    and parsed_declared.username is None
                    and not parsed_declared.path.rstrip('/')
                    and not parsed_declared.query and not parsed_declared.fragment):
                try:
                    cart = client.json(parsed_declared.geturl().rstrip('/') + '/cart.js')
                    if isinstance(cart, dict) and cart.get('currency') == 'USD':
                        shopify_origin = parsed_declared.geturl().rstrip('/')
                        client.shopify_origin = shopify_origin
                except (CrawlError, ValueError):
                    pass
                break
    candidates = ([] if hydrogen_marker or native_platform else
                  list(dict.fromkeys(u for u in categories.values()
                                     if '/collections/' in urlsplit(u).path))[:3])
    def probe(url):
        p = urlsplit(url)
        path = p.path.split('/collections/')[0] + '/collections/' + p.path.split('/collections/')[1].split('/')[0]
        branded = p._replace(path=path + '/products.json', query='', fragment='')
        endpoints = []
        # A branded Shopify storefront can advertise its myshopify origin even
        # when that host is protected by a bot challenge.  Prefer the public
        # host for ordinary themes; a proven headless shell has no branded
        # Shopify JSON route, so its embedded origin remains the first choice.
        if not headless_shopify:
            endpoints.append(branded)
        if shopify_origin:
            origin = urlsplit(shopify_origin)
            endpoints.append(branded._replace(scheme=origin.scheme, netloc=origin.netloc))
        if headless_shopify or not endpoints:
            endpoints.append(branded)
        for endpoint_parts in dict.fromkeys(endpoints):
            endpoint = query(endpoint_parts.geturl(), page=1, limit=1)
            try:
                data = client.json(endpoint)
                if isinstance(data, dict) and isinstance(data.get('products'), list):
                    if shopify_origin and endpoint_parts.netloc == urlsplit(shopify_origin).netloc:
                        # Downstream listing/detail code changes origin only
                        # after this exact public endpoint has been observed.
                        client.shopify_origin = shopify_origin
                    return {'collection': url, 'endpoint': endpoint, 'products': data['products']}
            except CrawlError:
                pass
        return None
    # One successful collection feed is sufficient. Probe in menu order so
    # four workbench jobs create at most four detection requests, rather than
    # a 4x3 burst that can make healthy Shopify endpoints reset connections.
    valid = []
    for candidate in candidates:
        observed = probe(candidate)
        if observed is not None:
            valid.append(observed)
            break
    provider = next((k for k, marks in THIRD_PARTY_MARKERS.items() if any(m in html.lower() for m in marks)), '')
    graphql = static_graphql_candidates(html, base)
    storefront = static_storefront_config(html)
    if storefront['endpoint'] and storefront['endpoint'] not in graphql:
        graphql.append(urljoin(base, storefront['endpoint']))
    shopify = (not native_platform and
               (bool(valid) or bool(storefront['endpoint']) or any(m.lower() in html.lower() for m in SHOPIFY_MARKERS)))
    # A search widget alone does not prove the collection grid is search-app driven.
    hydrogen = shopify and (hydrogen_marker or headless_shopify)
    market = observe_market(html)
    if shopify_origin and headless_shopify and not market['confirmed']:
        try:
            cart = client.get(shopify_origin + '/cart.js').json()
            # Client always requests the explicit US localization cookie.  A
            # Shopify cart response in USD proves that market preference was
            # accepted even when the branded Gatsby shell emits no Shopify
            # localization globals.
            if isinstance(cart, dict) and cart.get('currency') == 'USD':
                market = {'countries': ['US'], 'currencies': ['USD'],
                          'confirmed': True,
                          'source': 'shopify_cart_after_us_localization'}
        except (CrawlError, ValueError):
            pass
    mode = ('standard_shopify' if valid else 'hydrogen_shopify' if hydrogen
            else 'search_app_shopify' if shopify and provider
            else 'graphql_shopify' if shopify and graphql else 'custom_site')
    return {'mode': mode, 'shopify': shopify, 'provider': provider,
            'custom_platform': '' if shopify else native_platform,
            'graphql_candidates': graphql, 'market': market, 'probes': valid,
            'shopify_origin': shopify_origin, 'headless_shopify': headless_shopify}

SHOPIFY_MARKERS = (
    "cdn.shopify.com",
    "shopify-section",
    "shopify-features",
    "myshopify.com",
    "/cdn/shop/",
    "Shopify.theme",
)

THIRD_PARTY_MARKERS = {
    "algolia": ("algolia", "algoliasearch"),
    "searchspring": ("searchspring",),
    "klevu": ("klevu",),
    "constructor": ("constructor.io", "constructorio"),
}


def collection_inventory(client,base,html,out):
    from .shopify_inventory import fetch_inventory
    from .common import save_json
    from pathlib import Path
    if any(marker.lower() in html.lower() for marker in SHOPIFY_MARKERS):
        return fetch_inventory(client,base,out)
    value={'version':1,'base_url':base,'collections':{},'sources':[],'warnings':[{'code':'inventory_not_shopify','message':'首页未发现 Shopify 标识，仅保留实际菜单来源'}]}
    save_json(Path(out)/'shopify_collections.json',value)
    return value

def static_graphql_candidates(html: str, base_url: str) -> list[str]:
    patterns = (
        r"""["']([^"']*graphql(?:\.json)?[^"']*)["']""",
        r"""(?:endpoint|url)\s*:\s*["']([^"']*graphql[^"']*)["']""",
    )
    result: list[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, html, flags=re.I):
            candidate = urljoin(base_url, match.replace("\\/", "/"))
            if (
                urlsplit(candidate).netloc == urlsplit(base_url).netloc
                and "," not in candidate
                and "graphql" in urlsplit(candidate).path.lower()
                and candidate not in result
            ):
                result.append(candidate)
    return result

def static_storefront_config(html: str) -> dict[str, str]:
    endpoint_match = re.search(
        r"""shopifyStorefrontGraphQlEndpoint\s*:\s*["']([^"']+)""",
        html,
        flags=re.I,
    )
    token_match = re.search(
        r"""(?:shopifyStorefrontCheckoutToken|shopifyToken)\s*:\s*["']([^"']+)""",
        html,
        flags=re.I,
    )
    return {
        "endpoint": endpoint_match.group(1) if endpoint_match else "",
        "token": token_match.group(1) if token_match else "",
    }


def resolve_storefront(client, requested_url, response):
    """Resolve the US market for Braun's global entry point after geo-routing."""
    origin = urlsplit(requested_url)
    if origin.hostname in {'braun.com', 'www.braun.com'} and origin.path.rstrip('/') == '':
        from .braun_navigation import is_mia_storefront
        from .html_parser import parse_soup
        target = 'https://us.braun.com/en-us'
        if response.url.rstrip('/') != target:
            response = client.get(target)
        doc = parse_soup(response.text, 'lxml')
        canonical = doc.select_one('link[rel~="canonical"][href]')
        if (response.url.rstrip('/') != target or canonical is None
                or urljoin(response.url, canonical['href']).rstrip('/') != target
                or not doc.html or str(doc.html.get('lang', '')).lower() != 'en-us'
                or not is_mia_storefront(response.text)):
            raise CrawlError('Braun 美国首页的地区、正式 URL 或店铺身份未得到验证')
        return response
    response = localized_hydrogen_us_storefront(client, response)
    return canonical_storefront(client, response)


def localized_hydrogen_us_storefront(client, response):
    """Recover a verified US locale after Hydrogen geo-routing.

    Some Oxygen storefronts redirect the bare origin according to the request
    IP even when the crawler has already supplied US market cookies.  Prefer
    their ordinary ``/en-us`` locale only when the returned document proves
    US/USD and shares a Shopify CDN store fingerprint with the geo-routed
    document.  Failure is deliberately non-fatal here: the normal market gate
    still rejects the unconfirmed response later in the pipeline.
    """
    html = response.text
    if (observe_market(html)['confirmed'] or not (
            'oxygen-v2' in html
            or '__reactRouterContext.streamController.enqueue' in html
            or '__remixContext.streamController.enqueue' in html)):
        return response
    current = urlsplit(response.url)
    target = f'{current.scheme}://{current.netloc}/en-us'
    if response.url.rstrip('/') == target:
        return response
    try:
        candidate = client.get(target)
    except Exception:
        return response
    found = urlsplit(candidate.url)
    if (found.scheme != current.scheme or found.netloc != current.netloc
            or not found.path.lower().rstrip('/').startswith('/en-us')
            or not observe_market(candidate.text)['confirmed']):
        return response
    fingerprint = re.compile(r'cdn\.shopify\.com/s/files/1/(\d+)', re.I)
    source_ids = set(fingerprint.findall(html))
    candidate_ids = set(fingerprint.findall(candidate.text))
    if not source_ids or not candidate_ids or source_ids.isdisjoint(candidate_ids):
        return response
    return candidate


def canonical_storefront(client, response):
    """Follow a site's declared canonical origin only for the same US Shopify store."""
    from .html_parser import parse_soup as BeautifulSoup
    html=response.text
    link=BeautifulSoup(html,'lxml').select_one('link[rel=canonical][href]')
    if link is None:return response
    target=urljoin(response.url,link['href']);old=urlsplit(response.url);new=urlsplit(target)
    if new.hostname==old.hostname or new.path.rstrip('/') or new.query or new.fragment:return response
    identity=lambda h:re.search(r'Shopify\.shop\s*=\s*["\']([^"\']+)',h)
    source=identity(html)
    if source is None:return response
    candidate=client.get(target)
    found=identity(candidate.text)
    if found and found[1]==source[1] and observe_market(candidate.text)['confirmed']:
        return candidate
    raise CrawlError('正式域名与原站的店铺身份或 US/USD 市场不一致')
