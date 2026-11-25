"""Small registry for deterministic non-Shopify storefront adapters.

Custom storefronts do not share Shopify's URL or product schema.  Keep their
mechanism-specific code behind this module instead of weakening the Shopify
fast path.  The first supported family is Salesforce Commerce Cloud
(Demandware); other families can register the same navigation/listing/product
contract later.
"""
from __future__ import annotations

import html as html_lib
import hashlib
import json
import re
import time
from importlib import import_module
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .common import AdapterRequired, CrawlError, Client, canonical, query, read_json, save_json
from .html_parser import parse_soup as BeautifulSoup


STOREFRONT_MODULES = {
    'sfcc_storefront': 'sfcc_storefront',
    'magento_storefront': 'magento_storefront',
    'bigcommerce_storefront': 'bigcommerce_storefront',
    'pinia_storefront': 'pinia_storefront',
    'mobify_storefront': 'mobify_storefront',
    'jsonld_storefront': 'jsonld_storefront',
    'woocommerce_storefront': 'woocommerce_storefront',
}
SUPPORTED_PLATFORMS = {'demandware', 'braun_mia', 'magento', 'bigcommerce', 'pinia', 'mobify', 'jsonld', 'woocommerce'}


def source_adapter(kind):
    module = STOREFRONT_MODULES.get(kind)
    return import_module('.' + module, __package__) if module else None


def identify_platform(html: str) -> str:
    lower = html.lower()
    if 'id="mobify-data"' in lower or "id='mobify-data'" in lower:
        from .mobify_storefront import supports
        if supports(html):
            return 'mobify'
    if 'miaheader' in lower and '__next_data__' in lower:
        from .braun_navigation import is_mia_storefront
        if is_mia_storefront(html):
            return 'braun_mia'
    if "on/demandware.store" in lower or "dwanonymous" in lower:
        return "demandware"
    if "text/x-magento-init" in lower or "magento_" in lower:
        return "magento"
    if 'stencil' in lower and ('cdn11.bigcommerce.com' in lower or 'cdn10.bigcommerce.com' in lower):
        return 'bigcommerce'
    if "pinia" in lower and ("__nuxt__" in lower or "_payload.json" in lower or 'window.__initial_state__' in lower):
        return "pinia"
    from .woocommerce_storefront import supports as supports_woocommerce
    if supports_woocommerce(html):
        return 'woocommerce'
    from .jsonld_storefront import supports_home
    if supports_home(html):
        return 'jsonld'
    return ""


_NON_CATALOG = re.compile(
    r"/(?:account|login|logout|register|wishlist|cart|checkout|search|stores?|"
    r"storelocator|service|faq|order-status|policies?|privacy|terms)(?:/|\.|$)",
    re.I,
)
_GENERIC = re.compile(r"^(?:view|shop)\s+all$", re.I)


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _catalog_url(value: str, base: str) -> str:
    target = urljoin(base, value)
    p, origin = urlsplit(target), urlsplit(base)
    if p.scheme not in ("http", "https") or p.hostname != origin.hostname:
        return ""
    if _NON_CATALOG.search(p.path) or "/on/demandware." in p.path.lower():
        return ""
    return target


def _key(labels: list[str]) -> str:
    labels = [re.sub(r"\s+", " ", label).strip(" ,") for label in labels if label.strip(" ,")]
    labels = [label.replace(",", " ") for label in labels]
    return ",".join(" ".join(labels[: index + 1]) for index in range(len(labels)))


def demandware_navigation(html: str, base: str, client=None) -> dict[str, str]:
    """Read SFCC's explicit level-1/2/3 menu ownership."""
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, str] = {}
    roots = soup.select("header li.b-menu_bar-item")
    for root in roots:
        root_link = root.find("a", class_="b-menu_bar-link", recursive=False)
        if root_link is None:
            continue
        root_label = _text(root_link) or root_link.get("aria-label", "").strip()
        root_url = _catalog_url(root_link.get("href", ""), base)
        if not root_label:
            continue
        if root_url:
            result.setdefault(_key([root_label]), root_url)

        # Level ownership is encoded by the nearest nested menu item.  Avoid
        # duplicated top/bottom "View all" links; the owned parent already
        # carries the same destination.
        for level2 in root.select('a.b-menu_item-link[data-level="2"]'):
            if level2.find_parent("li", class_="b-menu_bar-item") is not root:
                continue
            label2, url2 = _text(level2), _catalog_url(level2.get("href", ""), base)
            if not label2 or _GENERIC.fullmatch(label2):
                continue
            labels2 = [root_label, label2]
            if url2:
                result.setdefault(_key(labels2), url2)
            owner = level2.find_parent("li", class_="b-menu_item")
            if owner is None:
                continue
            for level3 in owner.select('a.b-menu_item-link[data-level="3"]'):
                nested_owner = level3.find_parent("li", class_="b-menu_item")
                if nested_owner is None or nested_owner.find_parent("li", class_="b-menu_item") is not owner:
                    continue
                label3, url3 = _text(level3), _catalog_url(level3.get("href", ""), base)
                if not label3 or _GENERIC.fullmatch(label3) or not url3:
                    continue
                result.setdefault(_key(labels2 + [label3]), url3)
    if not result:
        from .sfcc_storefront import navigation
        return navigation(html, base, client=client)
    return result


def _native_navigation(root, base, html, platform, client):
    root = Path(root)
    if platform == 'braun_mia':
        from .braun_navigation import mia_navigation
        categories = mia_navigation(html, base, client=client, evidence_dir=root / 'evidence')
        scope = 'source_menu_and_merchandising_pages'
    elif platform == 'demandware':
        categories = demandware_navigation(html, base, client=client)
        scope = 'source_menu_level_attributes'
    elif platform in {'magento', 'bigcommerce', 'pinia', 'mobify', 'jsonld', 'woocommerce'}:
        adapter = source_adapter(platform + '_storefront')
        categories = adapter.navigation(html, base, client=client, evidence_dir=root / 'evidence')
        scope = 'source_navigation_and_homepage'
    else:
        raise AdapterRequired("当前自建站平台尚无确定性目录适配器: " + (platform or "unknown"))
    return categories, scope


def run_navigation(root, base: str, html: str, platform: str, client=None, selection=None):
    from .custom_navigation import audit_navigation, recover_navigation
    root = Path(root)
    platform = platform or 'unknown'
    try:
        categories, scope = _native_navigation(root, base, html, platform, client)
        documents = [p.read_text(encoding='utf-8') for p in (root / 'evidence').glob('home-menu-*.html')]
        # A modern SFCC React shell may contain platform markers but no menu
        # anchors until hydration. Acquire every accessible desktop drawer
        # once, save the exact DOM/screenshots, then reuse the normal SFCC
        # parser and audit instead of sending an empty shell to AI recovery.
        if platform == 'demandware' and not categories and not documents:
            from .sfcc_storefront import rendered_navigation, navigation as sfcc_navigation
            documents = rendered_navigation(base, root / 'evidence')
            for document in documents:
                categories.update(sfcc_navigation(document, base, client=client))
            scope = 'rendered_accessible_desktop_menu'
        audit = audit_navigation(categories, html, base, documents)
        save_json(root / 'navigation_native_audit.json', audit)
        if not audit['passed']:
            raise AdapterRequired('原生菜单覆盖不足，需要按完整源结构恢复')
    except AdapterRequired as error:
        if platform == 'woocommerce':
            from .simple_nav import run_simple
            categories, evidence, documents = run_simple(root, base, html)
            release = read_json(root / 'navigation_release.json')
            return categories, evidence, documents, release
        documents = [p.read_text(encoding='utf-8') for p in (root / 'evidence').glob('home-menu-*.html')]
        categories = recover_navigation(root, base, html, error, documents)
        scope = 'source_navigation_ai_recovery'
    except (ValueError, TypeError, AttributeError, KeyError) as error:
        documents = [p.read_text(encoding='utf-8') for p in (root / 'evidence').glob('home-menu-*.html')]
        categories = recover_navigation(root, base, html, error, documents)
        scope = 'source_navigation_ai_recovery'
    from .simple_nav import clean_reviewed_categories, publish_simple
    method = 'custom_' + platform
    sources = {'base_url': base, 'valid_urls': list(dict.fromkeys(categories.values()))}
    warnings = []
    if selection is not None:
        categories, warnings = clean_reviewed_categories(selection, sources, method)
    save_json(root / 'evidence/simple_nav_sources.json', sources)
    release = publish_simple(root, categories, warnings, method=method, scope=scope)
    evidence = {"method": "simple_nav", "source_method": "custom_" + platform, "roots": []}
    save_json(root / "evidence" / "navigation-observed.json", evidence)
    return categories, evidence, [html], release


def _product_urls(fragment: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(fragment, "lxml")
    result = []
    anchors = soup.select('a.b-product_tile-link[data-pid][href]')
    if not anchors:
        scope = soup.find('main') or soup
        anchors = scope.select(
            '.product-tile a.thumb-link[href],.product-tile a.name-link[href],'
            'li[class*="Tile"] > a[href],a[class*="ProductTile"][href]')
    for anchor in anchors:
        target = urljoin(page_url, anchor.get("href", ""))
        path = urlsplit(target).path
        if (urlsplit(target).hostname == urlsplit(page_url).hostname
                and re.search(r'/[^/]+/[A-Za-z0-9_-]+\.html$', path, re.I)
                and not re.search(r'/expert-advice/|/who-we-are/', path, re.I)
                and target not in result):
            result.append(target)
    return result


def _rendered_demandware_listing(url: str) -> tuple[str, str]:
    """Render a JS-only SFCC PLP and return its exact card DOM."""
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                          locale='en-US', timezone_id='America/New_York')
            page = context.new_page()
            page.route('**/*', lambda route: route.abort()
                       if route.request.resource_type in {'image', 'media', 'font'}
                       else route.continue_())
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except BrowserTimeout:
                if urlsplit(page.url).hostname != urlsplit(url).hostname:
                    raise
            try:
                page.wait_for_selector('main li[class*="Tile"] > a[href],main a[class*="ProductTile"][href]',
                                       timeout=12000)
            except BrowserTimeout:
                pass
            page.wait_for_timeout(350)
            return page.url, page.content()
        finally:
            browser.close()


def _rendered_demandware_product(url: str) -> tuple[str, str]:
    """Render one React SFCC PDP without interacting with purchase controls."""
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                          locale='en-US', timezone_id='America/New_York')
            page = context.new_page()
            page.route('**/*', lambda route: route.abort()
                       if route.request.resource_type in {'media', 'font'}
                       else route.continue_())
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except BrowserTimeout:
                if urlsplit(page.url).hostname != urlsplit(url).hostname:
                    raise
            try:
                page.wait_for_selector('h1,[class*="ProductDetailsMainInfo"]', timeout=12000)
            except BrowserTimeout:
                pass
            page.wait_for_timeout(350)
            return page.url, page.content()
        finally:
            browser.close()


def _demandware_route(html: str, base: str) -> tuple[str, str]:
    """Return the exact SFCC site id and locale declared by this storefront.

    SFCC installations choose their own site id and may use ``default`` rather
    than ``en_US``.  The public document declares both in analytics/action or
    static asset URLs; never assume ``Sites-US-Site/en_US``.
    """
    routes = re.findall(
        r'/on/demandware\.store/(Sites-[A-Za-z0-9_.-]+-Site)/([A-Za-z_\-]+)/',
        html, re.I)
    if routes:
        return routes[0]
    static = re.search(
        r'/on/demandware\.static/(Sites-[A-Za-z0-9_.-]+-Site)/', html, re.I)
    locale = re.search(r'CQuotient\.locale\s*=\s*["\']([^"\']+)', html)
    if static:
        return static.group(1), locale.group(1) if locale else 'default'
    raise AdapterRequired('Demandware 页面未声明可验证的 siteId/locale 路由')


def demandware_listing(client, url: str) -> dict:
    from . import sfcc_storefront
    if sfcc_storefront.supports(url):
        return sfcc_storefront.collect_listing(client, url)
    started = time.perf_counter()
    response = client.get(url, timeout=45)
    if sfcc_storefront.supports(response.url, response.text):
        return sfcc_storefront.collect_listing(client, response.url)
    pages = [{"url": response.url, "html": response.text}]
    urls = _product_urls(response.text, response.url)
    match = re.search(r"cq_params\.categoryId\s*=\s*['\"](.*?)['\"]", response.text)
    if not match:
        if len(urls) < 3:
            if sfcc_storefront.is_client_rendered_shell(response.text):
                return sfcc_storefront.collect_react_listing(client, response.url, response)
            rendered_url, rendered_html = _rendered_demandware_listing(response.url)
            rendered_urls = _product_urls(rendered_html, rendered_url)
            if len(rendered_urls) < 3:
                raise CrawlError("Demandware 分类缺少 cgid 且渲染商品不足 3 个: " + url)
            response = type('RenderedResponse', (), {'url': rendered_url, 'text': rendered_html})()
            pages = [{"url": rendered_url, "html": rendered_html}]
            urls = rendered_urls
    else:
        cgid, start, size = match.group(1), len(urls), 24
        site_id, locale = _demandware_route(response.text, response.url)
        endpoint = urljoin(
            response.url,
            f"/on/demandware.store/{site_id}/{locale}/Search-UpdateGrid")
        for _ in range(100):
            page_url = query(endpoint, cgid=cgid, start=start, sz=size, ajax="true")
            page = client.get(page_url, timeout=45)
            found = _product_urls(page.text, response.url)
            if not found:
                break
            pages.append({"url": page.url, "html": page.text})
            before = len(urls)
            urls.extend(value for value in found if value not in urls)
            if len(found) < size or len(urls) == before:
                break
            start += size
        else:
            raise CrawlError("Demandware 分类分页超过安全上限")
    return {
        "url": response.url,
        "format": "custom",
        "complete": True,
        "catalog_urls": urls,
        "urls": urls,
        "products": [],
        "feeds": [],
        "pages": pages,
        "first3_seconds": round(time.perf_counter() - started, 4),
    }


def _variation_links(html: str, base: str) -> list[str]:
    pattern = r'(?:https?://[^"\s<]+)?/on/demandware\.store/[^"\s<]+/Product-Variation\?[^"\s<]+'
    result = []
    for value in re.findall(pattern, html, re.I):
        target = urljoin(base, html_lib.unescape(value.rstrip("'")))
        if urlsplit(target).hostname == urlsplit(base).hostname and target not in result:
            result.append(target)
    return result


def _selected_image_paths(raw: dict) -> list[str]:
    paths = []
    attrs = raw.get("product", {}).get("variationAttributes", [])
    for ai, attribute in enumerate(attrs):
        for vi, value in enumerate(attribute.get("values", [])):
            if not value.get("selected"):
                continue
            images = value.get("images") or {}
            for group, items in images.items():
                if not isinstance(items, list):
                    continue
                for ii, item in enumerate(items):
                    if isinstance(item, dict) and item.get("url"):
                        paths.append(f"/product/variationAttributes/{ai}/values/{vi}/images/{group}/{ii}/url")
            if paths:
                return paths[:12]
    return paths


def _pointer_value(data, path: str):
    for token in path.strip("/").split("/"):
        data = data[int(token)] if isinstance(data, list) else data[token]
    return data


def demandware_product(client, url: str, evidence: Path) -> dict:
    from . import sfcc_storefront
    def variation_json(target):
        try:
            return client.json(target)
        except CrawlError as error:
            if re.search(r'(?:接口|响应).*(?:未返回|不是).*JSON|JSON.*(?:不可用|无效)',
                         str(error), re.I):
                raise AdapterRequired('Demandware/SFCC variation 接口未返回 JSON') from error
            if (re.search(r'HTTP (?:Error )?5\d\d\b', str(error), re.I)
                    and not re.search(r'HTTP (?:Error )?(?:403|429)\b', str(error), re.I)):
                raise AdapterRequired('Demandware/SFCC variation 接口服务端结构异常') from error
            raise
    if sfcc_storefront.supports(url):
        return sfcc_storefront.fetch_product(client, url, evidence)
    page = client.get(url, timeout=45)
    if sfcc_storefront.supports(page.url, page.text):
        return sfcc_storefront.fetch_product(client, page.url, evidence)
    links = _variation_links(page.text, page.url)
    if not links:
        if sfcc_storefront.is_client_rendered_shell(page.text):
            return sfcc_storefront.fetch_react_product(client, page.url, evidence, page)
        rendered_url, rendered_html = _rendered_demandware_product(page.url)
        separator = '&' if '?' in rendered_url else '?'
        customer_url = rendered_url + separator + 'customer=true'
        customer = variation_json(customer_url)
        manifest = {'format': 'react_customer_json', 'url': rendered_url,
                    'customer_url': customer_url, 'customer': customer,
                    'rendered_html': rendered_html, 'raw_html': page.text}
        product = sfcc_storefront._normalize(manifest)
        evidence = Path(evidence)
        evidence.mkdir(parents=True, exist_ok=True)
        name = 'sfcc-react-' + re.sub(r'[^A-Za-z0-9_.-]', '-', product['id']) + '.json'
        save_json(evidence / name, manifest)
        return {'url': rendered_url, 'source': 'json', 'endpoint': customer_url,
                'product': product,
                'source_mapping': {'kind': 'sfcc_storefront',
                                   'file': 'evidence/' + name}}
    base_endpoint = links[0]
    base_raw = variation_json(base_endpoint)
    base_product = base_raw.get("product", {})
    if not base_product.get("id") or not base_product.get("productName"):
        raise CrawlError("Demandware 商品基础响应缺少身份")

    # Follow only selectable values for unselected dimensions.  Every accepted
    # leaf is an actual SFCC variant response, never a Cartesian product.
    queue, visited, leaves = [base_raw], {base_endpoint}, []
    while queue:
        raw = queue.pop(0)
        product = raw.get("product", {})
        if product.get("isAllAttributesSelected") or product.get("productType") in {"variant", "standard"}:
            leaves.append((raw, ""))
            continue
        selected = product.get("selectedAttributes") or {}
        pending = next((attribute for attribute in product.get("variationAttributes", [])
                        if attribute.get("id") not in selected), None)
        if pending is None:
            raise CrawlError("Demandware 变体响应无法继续选完属性")
        # ``selectable`` is an availability/UI flag in SFCC.  A false value
        # can still be a real sold-out SKU, so follow every server-declared
        # variation URL and let the leaf response prove product identity.
        next_values = [value for value in pending.get("values", []) if value.get("url")]
        if not next_values:
            raise CrawlError("Demandware 属性没有可选真实变体")
        for value in next_values:
            endpoint = html_lib.unescape(value["url"])
            if endpoint in visited:
                continue
            visited.add(endpoint)
            queue.append(variation_json(endpoint))
        if len(visited) >= 250:
            raise CrawlError("Demandware 变体达到 250 上限，需要独立完整性核验")

    unique = {}
    for raw, _ in leaves:
        product = raw.get("product", {})
        if product.get("id"):
            unique.setdefault(str(product["id"]), raw)
    if not unique:
        raise CrawlError("Demandware 未获取任何真实变体")

    evidence.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(base_product["id"]))
    base_file = evidence / f"demandware-{safe_id}-base.json"
    save_json(base_file, base_raw)
    variant_sources = []
    variants = []
    option_order = [attribute.get("id") for attribute in base_product.get("variationAttributes", [])]
    option_order = [value for value in option_order if value in {"color", "width", "size"}][:3]
    for index, raw in enumerate(unique.values()):
        product = raw["product"]
        variant_file = evidence / f"demandware-{safe_id}-variant-{index:03d}.json"
        save_json(variant_file, raw)
        variant_sources.append({"file": "evidence/" + variant_file.name, "path": "/product"})
        selected = product.get("selectedAttributes") or {}
        price = product.get("price", {})
        variant = {
            "id": str(product["id"]),
            "sku": str(product.get("productNumber") or product["id"]),
            "price": price.get("sales", {}).get("value"),
            "compare_at_price": (price.get("list") or {}).get("value"),
            "available": bool(product.get("available", product.get("availability", {}).get("isInStock"))),
            "inventory_quantity": product.get("availability", {}).get("quantityAvailableToSell", ""),
        }
        for oi, name in enumerate(option_order, 1):
            display = str((selected.get(name) or {}).get("displayValue", ""))
            if name == "size" and display.lstrip().startswith("{"):
                try:
                    size_data = json.loads(display)
                    display = next((str(value).split(";", 1)[0] for value in size_data.values()
                                    if str(value).strip()), display)
                except (ValueError, TypeError):
                    pass
            variant[f"option{oi}"] = display
        variants.append(variant)

    image_paths = _selected_image_paths(base_raw)
    if not image_paths:
        raise CrawlError("Demandware 商品源响应缺少可映射图片")
    image_urls = list(dict.fromkeys(str(_pointer_value(base_raw, path)) for path in image_paths))
    product = {
        "id": str(base_product["id"]),
        "handle": re.sub(r"\.html$", "", urlsplit(page.url).path.rstrip("/").rsplit("/", 1)[-1]),
        "title": base_product["productName"],
        "body_html": base_product.get("longDescription") or base_product.get("shortDescription") or "",
        "vendor": base_product.get("brand", ""),
        "currency": base_product.get("price", {}).get("sales", {}).get("currency", ""),
        "options": [{"name": name.title()} for name in option_order],
        "variants": variants,
        "images": [{"src": image, "variant_ids": list(unique)} for image in image_urls],
    }
    variant_fields = {"id": "/id", "price": "/price/sales/value",
                      "compare_at_price": "/price/list/value"}
    for oi, name in enumerate(option_order, 1):
        variant_fields[f"option{oi}"] = f"/selectedAttributes/{name}/displayValue"
    mapping = {
        "file": "evidence/" + base_file.name,
        "product_fields": {"id": "/product/id", "title": "/product/productName"},
        "variant_sources": variant_sources,
        "variant_fields": variant_fields,
        "variant_transforms": ({f"option{option_order.index('size') + 1}": "sfcc_size_eu"}
                               if "size" in option_order else {}),
        "image_paths": image_paths,
    }
    return {"url": page.url, "source": "json", "endpoint": base_endpoint,
            "product": product, "source_mapping": mapping}


def _collectors(platform):
    if platform == 'braun_mia':
        from .braun_listing import collect_listing
        from .braun_product import braun_product
        return collect_listing, braun_product
    if platform == 'demandware':
        return demandware_listing, demandware_product
    if platform in {'magento', 'bigcommerce', 'pinia', 'mobify', 'jsonld', 'woocommerce'}:
        adapter = source_adapter(platform + '_storefront')
        return adapter.collect_listing, adapter.fetch_product
    raise CrawlError('没有已配置的自建站商品适配器: ' + str(platform))


def _save_market_evidence(root, sample):
    mapping = sample['source_mapping']
    if mapping.get('kind') == 'braun_bigcommerce' or mapping.get('kind') in STOREFRONT_MODULES:
        proof = {'file': mapping['file'], 'kind': mapping['kind']}
    else:
        proof = {'file': mapping['file'], 'locale_path': '/locale',
                 'currency_path': '/product/price/sales/currency'}
    save_json(root / 'market_evidence.json', proof)


def ordered_product_urls(detail, listings):
    """Keep card order while folding only independently observed parent IDs."""
    identities = {}
    for key, links in detail.items():
        listing = listings.get(key, {})
        sources = listing.get('identity_sources', {})
        if sources and listing.get('format') not in STOREFRONT_MODULES:
            raise CrawlError('该列表格式不支持父商品身份合并')
        for url, sample in sources.items():
            parent = str(sample.get('product', {}).get('id') or '')
            if (url not in links or sample.get('url') != url or not parent
                    or sample.get('source_mapping', {}).get('kind') not in STOREFRONT_MODULES):
                raise CrawlError('商品卡片的父商品身份缺少源证据')
            if url in identities and identities[url] != parent:
                raise CrawlError('同一商品卡片的父商品身份不一致')
            identities[url] = parent
    urls, seen_urls, seen_parents = [], set(), set()
    for links in detail.values():
        for url in links:
            parent = identities.get(url)
            if url in seen_urls or (parent and parent in seen_parents):
                continue
            seen_urls.add(url)
            if parent:
                seen_parents.add(parent)
            urls.append(url)
    return urls


def _preserve_product_source(root, sample):
    # Several color cards share a parent ID. Adapters may name their source
    # file by that ID, so snapshot it before the next card can overwrite it.
    mapping = sample['source_mapping']
    suffix = hashlib.sha256(sample['url'].encode('utf-8')).hexdigest()[:16]
    references = [mapping] + mapping.get('variant_sources', [])
    copied = {}
    for reference in references:
        relative = reference.get('file')
        if not relative:
            continue
        if relative not in copied:
            source = (root / relative).resolve()
            if not source.is_relative_to((root / 'evidence').resolve()):
                raise CrawlError('商品身份源文件不在 evidence 目录')
            target = source.with_name(source.stem + '-' + suffix + source.suffix)
            source.replace(target)
            copied[relative] = target.relative_to(root.resolve()).as_posix()
        reference['file'] = copied[relative]


def _transient_product_error(error):
    message = str(error).casefold()
    return ('请求失败' in str(error) and any(marker in message for marker in (
        'ssl_connect', 'connection closed', 'connection reset',
        'could not resolve host', 'name resolution', 'dns',
        'curl: (6)', 'curl: (28)', 'curl: (35)', 'curl: (56)',
        'timed out', 'timeout')))


def _distinct_samples(client, root, product, urls, cache, limit, skipped=None,
                      failed=None, preserve_sources=True):
    samples, parents = [], set()
    for url in dict.fromkeys(urls):
        if url not in cache:
            checkpoint_dir = root / 'evidence' / 'custom-product-checkpoints'
            checkpoint = checkpoint_dir / (hashlib.sha256(url.encode('utf-8')).hexdigest() + '.json')
            if checkpoint.is_file():
                saved = read_json(checkpoint)
                if (saved.get('url') == url and saved.get('product', {}).get('id')):
                    cache[url] = saved
                    sample = saved
                else:
                    checkpoint.unlink()
                    sample = None
            else:
                sample = None
            for transport_attempt in range(5):
                if sample is not None:
                    break
                try:
                    sample = product(client, url, root / 'evidence')
                    break
                except AdapterRequired as error:
                    if skipped is None:
                        raise
                    skipped.append({'url': url, 'reason': str(error)})
                    break
                except CrawlError as error:
                    transient = _transient_product_error(error)
                    if not transient or transport_attempt == 4:
                        if transient and failed is not None:
                            failed.append({'url': url, 'error': str(error),
                                           'exception_type': type(error).__name__})
                            sample = None
                            break
                        raise
                    time.sleep(min(8, 1 + 2 * transport_attempt))
            if sample is None:
                continue
            if sample.get('url') != url or not sample.get('product', {}).get('id'):
                raise CrawlError('商品卡片响应缺少对应的真实父商品身份')
            if preserve_sources:
                _preserve_product_source(root, sample)
            cache[url] = sample
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            save_json(checkpoint, sample)
        sample = cache[url]
        from .shopify_fastpath import normalize_product_images
        normalize_product_images(sample['product'], url)
        parent = str(sample['product']['id'])
        if parent in parents:
            continue
        parents.add(parent)
        samples.append(sample)
        if limit and len(samples) >= limit:
            break
    return samples


def _attach_identity_sources(detail, listings, cache):
    sources = {url: cache[url] for links in detail.values() for url in links if url in cache}
    by_parent = {}
    for url, sample in sources.items():
        by_parent.setdefault(str(sample['product']['id']), []).append(url)
    duplicate_urls = {url for urls in by_parent.values() if len(urls) > 1 for url in urls}
    for key, links in detail.items():
        identities = {url: cache[url] for url in links if url in duplicate_urls}
        if identities:
            listings[key]['identity_sources'] = identities
        else:
            listings[key].pop('identity_sources', None)


def _native_identity(platform, url):
    if platform in {'magento', 'bigcommerce', 'pinia', 'mobify', 'jsonld', 'woocommerce'}:
        return True
    if platform == 'demandware':
        from .sfcc_storefront import supports
        return supports(url)
    return False


def run_custom_sample(client, root, platform, candidates, all_categories=False, full_products=False):
    root = Path(root)
    collect, product = _collectors(platform)
    attempts = []
    selected, listings, detail, by_url, cache = {}, {}, {}, {}, {}
    samples = []
    native = False
    parent_urls = {url for key, url in candidates
                   if any(other.startswith(key + ',') for other, _ in candidates)}
    for key, url in candidates:
        try:
            if url not in by_url:
                by_url[url] = collect(client, url)
            candidate = by_url[url]
            if candidate.get('source_format') == 'react_customer_json':
                sources = candidate.get('source_responses', [])
                files = []
                for source_index, source in enumerate(sources):
                    fingerprint = hashlib.sha256(source['url'].encode('utf-8')).hexdigest()[:16]
                    name = f'sfcc-listing-{fingerprint}-{source_index:03d}.json'
                    save_json(root / 'evidence' / name, source['response'])
                    files.append({'file': 'evidence/' + name, 'url': source['url'],
                                  'products_path': '/products', 'paging_path': '/paging'})
                candidate['source_mapping']['files'] = files
            if candidate.get('source_format') == 'woocommerce_store_api':
                files = []
                for source_index, source in enumerate(candidate.get('source_responses', [])):
                    fingerprint = hashlib.sha256(source['url'].encode('utf-8')).hexdigest()[:16]
                    name = f'woocommerce-listing-{fingerprint}-{source_index:03d}.json'
                    save_json(root / 'evidence' / name, source['response'])
                    files.append('evidence/' + name)
                candidate['pages'] = files
            native = _native_identity(platform, url)
            card_count = len(candidate['urls'])
            candidate_samples = []
            if native and not all_categories and (full_products or card_count >= 3):
                candidate_samples = _distinct_samples(
                    client, root, product, candidate['urls'], cache,
                    None if full_products else 3)
            count = len(candidate_samples) if native and not all_categories else card_count
            take = all_categories or (count > 0 if native and full_products else count >= 3)
            attempts.append({"category": key, "url": url, "distinct_products": count,
                             "card_urls": card_count,
                             "status": "selected" if take else "insufficient"})
            if take:
                selected[key], listings[key], detail[key] = url, candidate, candidate['urls']
                if not all_categories:
                    samples = candidate_samples
                    break
        except CrawlError as error:
            content_landing = (all_categories and url in parent_urls
                               and '分类页没有可验证的商品网格' in str(error))
            if content_landing:
                attempts.append({"category": key, "url": url, "distinct_products": 0,
                                 "card_urls": 0, "status": "content_landing_skipped",
                                 "error": str(error)})
                warnings_path = root / 'warnings.json'
                warnings = read_json(warnings_path) if warnings_path.is_file() else []
                warning = {'code': 'content_landing_skipped',
                           'message': '父级导购页没有商品网格；已保留下级真实分类继续全量采集',
                           'url': url, 'reason': str(error)}
                if warning not in warnings:
                    warnings.append(warning)
                    save_json(warnings_path, warnings)
                continue
            attempts.append({"category": key, "url": url, "distinct_products": 0,
                             "status": "unverifiable_listing", "error": str(error)})
            save_json(root / 'sample_attempts.json', attempts)
            save_json(root / 'candidate_listings.json', by_url)
            # SFCC structural drift belongs to the bounded site_adapter repair
            # loop. Transport/access failures remain CrawlError and fail closed.
            if isinstance(error, AdapterRequired) and platform == 'demandware':
                raise
            if all_categories:
                raise
    urls = list(dict.fromkeys(u for links in detail.values() for u in links))
    # Full runs may spend minutes paginating many category pages. Persist that
    # deterministic work before detail collection so a later transport error
    # has a resumable/auditable checkpoint instead of an empty run directory.
    if all_categories:
        save_json(root / "detail_url.json", detail)
        save_json(root / "listings.json", listings)
        save_json(root / 'sample_attempts.json', attempts)
    if native and all_categories:
        skipped_products = []
        failed_products = []
        samples = _distinct_samples(client, root, product, urls, cache,
                                    None if full_products else 3,
                                    skipped_products if full_products else None,
                                    failed_products if full_products else None)
        if skipped_products:
            skipped_urls = {item['url'] for item in skipped_products}
            for key in list(detail):
                detail[key] = [url for url in detail[key] if url not in skipped_urls]
                listing = listings[key]
                original_listing_urls = set(listing.get('urls', []))
                listing['urls'] = [url for url in listing.get('urls', []) if url not in skipped_urls]
                listing['catalog_urls'] = [url for url in listing.get('catalog_urls', [])
                                           if url not in skipped_urls]
                listing['excluded_urls'] = sorted(
                    set(listing.get('excluded_urls', [])) & skipped_urls
                    | (original_listing_urls & skipped_urls))
        if failed_products:
            save_json(root / 'failed_products.json', failed_products)
            failure_ratio = len(failed_products) / max(1, len(urls))
            warnings_path = root / 'warnings.json'
            warnings = read_json(warnings_path) if warnings_path.is_file() else []
            warnings.append({'code': 'transient_products_skipped',
                             'message': '个别商品临时网络失败；已保留成功商品并继续全量任务',
                             'failed': len(failed_products),
                             'planned': len(urls), 'ratio': round(failure_ratio, 4)})
            save_json(warnings_path, warnings)
            if len(samples) < 3 or failure_ratio > .20:
                raise CrawlError('全量商品临时网络失败比例超过 20%')
    elif not native:
        failed_products = []
        samples = _distinct_samples(client, root, product, urls, cache,
                                    None if full_products else 3,
                                    failed=failed_products if full_products else None,
                                    preserve_sources=False)
        if failed_products:
            save_json(root / 'failed_products.json', failed_products)
            failure_ratio = len(failed_products) / max(1, len(urls))
            warnings_path = root / 'warnings.json'
            warnings = read_json(warnings_path) if warnings_path.is_file() else []
            warnings.append({'code': 'transient_products_skipped',
                             'message': '个别商品临时网络失败；已保留成功商品并继续全量任务',
                             'failed': len(failed_products), 'planned': len(urls),
                             'ratio': round(failure_ratio, 4)})
            save_json(warnings_path, warnings)
            if len(samples) < 3 or failure_ratio > .20:
                raise CrawlError('全量商品临时网络失败比例超过 20%')
    if not samples or (not full_products and len(samples) < 3):
        reason = next((a['error'] for a in reversed(attempts) if a.get('error')), '')
        raise CrawlError(f'{platform} 候选分类没有可验证的商品样本。{reason}')
    if native:
        _attach_identity_sources(detail, listings, cache)
    save_json(root / "detail_url.json", detail)
    save_json(root / "listings.json", listings)
    save_json(root / "samples.json", samples)
    save_json(root / 'sample_attempts.json', attempts)
    warnings_path = root / 'warnings.json'
    warnings = read_json(warnings_path) if warnings_path.is_file() else []
    for item in locals().get('skipped_products', []):
        warnings.append({'code': 'unavailable_product_pruned', **item})
    for sample in samples:
        for warning in sample['product'].get('warnings', []):
            entry = {'product_url': sample['url'], **warning} if isinstance(warning, dict) else {
                'product_url': sample['url'], 'message': str(warning)}
            if entry not in warnings:
                warnings.append(entry)
    save_json(warnings_path, warnings)
    _save_market_evidence(root, samples[0])
    return selected, detail, listings, samples, attempts


def run_demandware_sample(client, root, categories, sample_candidates):
    return run_custom_sample(client, root, 'demandware', sample_candidates)


def run_links(root):
    """Refresh custom storefront categories in their original order."""
    root = Path(root)
    from .navigation_release import require_release, digest
    release = require_release(root)
    categories = read_json(root / "url.json")
    client, detail, listings = Client(), {}, {}
    collect, _ = _collectors(read_json(root / 'detection.json')['custom_platform'])
    client.get(read_json(root / "config.json")["base_url"], timeout=45)
    for key, url in categories.items():
        listing = collect(client, url)
        if listing.get('source_format') == 'react_customer_json':
            files = []
            for source_index, source in enumerate(listing.get('source_responses', [])):
                fingerprint = hashlib.sha256(source['url'].encode('utf-8')).hexdigest()[:16]
                name = f'sfcc-listing-{fingerprint}-{source_index:03d}.json'
                save_json(root / 'evidence' / name, source['response'])
                files.append({'file': 'evidence/' + name, 'url': source['url'],
                              'products_path': '/products', 'paging_path': '/paging'})
            listing['source_mapping']['files'] = files
        if listing.get('source_format') == 'woocommerce_store_api':
            files = []
            for source_index, source in enumerate(listing.get('source_responses', [])):
                fingerprint = hashlib.sha256(source['url'].encode('utf-8')).hexdigest()[:16]
                name = f'woocommerce-listing-{fingerprint}-{source_index:03d}.json'
                save_json(root / 'evidence' / name, source['response'])
                files.append('evidence/' + name)
            listing['pages'] = files
        listings[key], detail[key] = listing, listing["urls"]
        print(key, len(listing["urls"]), flush=True)
    save_json(root / "listings.json", listings)
    save_json(root / "detail_url.json", detail)
    save_json(root / "detail_release.json", {
        "navigation_version": release["navigation_version"],
        "sha256": digest(root / "detail_url.json"),
    })
    save_json(root / "sample_scope.json", {
        "selected_categories": categories,
        "navigation_categories": len(categories),
        "listing_scope": "all",
        "detail_scope": "pending",
        "navigation_version": release["navigation_version"],
    })


def run_products(root, all_products=False):
    """Refresh custom storefront products from an existing detail_url.json."""
    root = Path(root)
    detail = read_json(root / "detail_url.json")
    listings = read_json(root / 'listings.json')
    urls = list(dict.fromkeys(url for values in detail.values() for url in values))
    client = Client()
    base_url = read_json(root / 'config.json')['base_url']
    client.get(base_url, timeout=45)
    platform = read_json(root / 'detection.json')['custom_platform']
    _, product = _collectors(platform)
    if platform == 'demandware':
        from .sfcc_storefront import seed_react_cards
        seed_react_cards(client, listings)
    cache = {}
    failed_products = []
    if all_products and (root / 'failed_products.json').is_file():
        (root / 'failed_products.json').unlink()
    native = _native_identity(platform, base_url)
    if native:
        samples = _distinct_samples(client, root, product, urls, cache,
                                    None if all_products else 3,
                                    failed=failed_products if all_products else None,
                                    preserve_sources=native)
    else:
        samples = _distinct_samples(client, root, product, urls, cache,
                                    None if all_products else 3,
                                    failed=failed_products if all_products else None)
    if not samples or (not all_products and len(samples) != 3):
        raise CrawlError('商品范围不足 3 个不同父商品')
    if failed_products:
        save_json(root / 'failed_products.json', failed_products)
        ratio = len(failed_products) / max(1, len(urls))
        warnings_path = root / 'warnings.json'
        warnings = read_json(warnings_path) if warnings_path.is_file() else []
        warnings.append({'code': 'transient_products_skipped', 'failed': len(failed_products),
                         'planned': len(urls), 'ratio': round(ratio, 4)})
        save_json(warnings_path, warnings)
        if len(samples) < 3 or ratio > .20:
            raise CrawlError('全量商品临时网络失败比例超过 20%')
    if native:
        _attach_identity_sources(detail, listings, cache)
        save_json(root / 'listings.json', listings)
    save_json(root / "samples.json", samples)
    from .shopify_fastpath import export
    export(samples, detail, root)
    _save_market_evidence(root, samples[0])
    scope = read_json(root / "sample_scope.json")
    scope["detail_scope"] = "all_products" if all_products else "first_3_products"
    save_json(root / "sample_scope.json", scope)
    return samples
