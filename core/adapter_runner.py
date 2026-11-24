"""Host-owned runner for a small, task-local ``site_adapter.py``.

The generated adapter owns only site-specific discovery and parsing.  This
module owns scope selection, request auditing, normalization, artifacts,
exports and trusted validation so model-authored code does not duplicate the
delivery framework or invent source-mapping pointers.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .common import Client, CrawlError, public_url, read_json, save_json
from .navigation_release import digest, require_release
from .shopify_fastpath import export, product_handle


TRACKING_KEYS = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term',
                 'utm_content', 'fbclid', 'gclid', 'ref'}


def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _load_adapter(root):
    path = Path(root) / 'site_adapter.py'
    if not path.is_file() or path.is_symlink():
        raise CrawlError('商品修复缺少 site_adapter.py')
    source = path.read_text(encoding='utf-8-sig')
    tree = ast.parse(source, filename='site_adapter.py')
    functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    required = {'collect_product_urls', 'fetch_product'}
    if not required.issubset(functions):
        raise CrawlError('site_adapter.py 必须实现 collect_product_urls 和 fetch_product')
    # Reject the common fake-adapter shape: product records or a catalog are
    # embedded as literal constants. Selectors, endpoint paths and headers are
    # legitimate constants and remain allowed.
    suspicious = {'title', 'price', 'images', 'variants', 'currency'}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return)):
            continue
        value = node.value if hasattr(node, 'value') else None
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue
        records = literal if isinstance(literal, list) else [literal]
        for record in records:
            if isinstance(record, dict) and len(suspicious.intersection(record)) >= 3:
                raise CrawlError('site_adapter.py 疑似硬编码商品数据，拒绝执行')
    spec = importlib.util.spec_from_file_location(
        'task_site_adapter_' + hashlib.sha256(str(path).encode()).hexdigest()[:12], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in required:
        if not callable(getattr(module, name, None)):
            raise CrawlError('site_adapter.py 函数不可调用: ' + name)
    return module


class TrackedClient:
    """Small Client facade that persists evidence for every adapter request."""
    def __init__(self, root, client=None):
        self.root = Path(root)
        self.client = client or Client()
        self.calls = []
        (self.root / 'evidence' / 'adapter-http').mkdir(parents=True, exist_ok=True)

    def _record(self, url, value, kind):
        public_url(url)
        index = len(self.calls) + 1
        suffix = '.json' if kind == 'json' else '.html'
        relative = f'evidence/adapter-http/{index:04d}-{hashlib.sha256(url.encode()).hexdigest()[:12]}{suffix}'
        target = self.root / relative
        if kind == 'json':
            save_json(target, value)
        else:
            text = value if isinstance(value, str) else str(value or '')
            target.write_text(text, encoding='utf-8')
        self.calls.append({'url': url, 'file': relative, 'kind': kind})

    def get(self, url, **kwargs):
        response = self.client.get(url, **kwargs)
        self._record(getattr(response, 'url', url) or url, getattr(response, 'text', ''), 'html')
        return response

    def json(self, url, **kwargs):
        value = self.client.json(url, **kwargs)
        self._record(url, value, 'json')
        return value

    def browser_json(self, url, **kwargs):
        value = self.client.browser_json(url, **kwargs)
        self._record(url, value, 'json')
        return value

    def close(self):
        close = getattr(self.client, 'close', None)
        if callable(close):
            close()

    def __getattr__(self, name):
        if name.startswith('_') or name in {'session', 'sessions'}:
            raise AttributeError(name)
        return getattr(self.client, name)


def _request_slice(tracker, start, label):
    calls = tracker.calls[start:]
    if not calls:
        raise CrawlError(f'site_adapter {label} 未通过 Client 发起真实网络请求')
    return calls


def _valid_url(value, base):
    absolute = urljoin(base, str(value or '').strip())
    public_url(absolute)
    if urlsplit(absolute).hostname != urlsplit(base).hostname:
        raise CrawlError('site_adapter 返回跨域商品 URL: ' + absolute)
    return absolute


def _image_urls(value, base):
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        if isinstance(item, dict):
            item = item.get('src') or item.get('url')
        if not item:
            continue
        url = urljoin(base, str(item).strip())
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            raise CrawlError('site_adapter 返回无效图片 URL')
        if url not in result:
            result.append(url)
    return result


def _money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise CrawlError('site_adapter 返回无效价格') from error
    if not amount.is_finite() or amount <= 0:
        raise CrawlError('site_adapter 价格必须是正数')
    return format(amount, 'f')


def normalize_product(raw, product_url):
    if not isinstance(raw, dict):
        raise CrawlError('site_adapter.fetch_product 必须返回对象')
    product_url = _valid_url(raw.get('url') or product_url, product_url)
    product_id = str(raw.get('id') or '').strip()
    title = str(raw.get('title') or '').strip()
    currency = str(raw.get('currency') or 'USD').upper().strip()
    if not product_id or not title:
        raise CrawlError('site_adapter 商品缺少 id 或 title')
    if currency != 'USD':
        raise CrawlError('site_adapter 商品币种不是 USD')
    images = _image_urls(raw.get('images'), product_url)
    if not images:
        raise CrawlError('site_adapter 商品缺少有效图片')
    raw_variants = raw.get('variants')
    if not isinstance(raw_variants, list) or not raw_variants:
        raise CrawlError('site_adapter 商品 variants 必须是非空数组')
    option_names = []
    variants, seen = [], set()
    for item in raw_variants:
        if not isinstance(item, dict):
            raise CrawlError('site_adapter 变体必须是对象')
        variant_id = str(item.get('id') or item.get('sku') or '').strip()
        if not variant_id or variant_id in seen:
            raise CrawlError('site_adapter 变体 ID 缺失或重复')
        seen.add(variant_id)
        variant_currency = str(item.get('currency') or currency).upper().strip()
        if variant_currency != 'USD':
            raise CrawlError('site_adapter 变体币种不是 USD')
        variant = {'id': variant_id, 'price': _money(item.get('price')),
                   'currency': 'USD', 'sku': str(item.get('sku') or variant_id)}
        compare = item.get('compare_at_price')
        if compare not in (None, ''):
            variant['compare_at_price'] = _money(compare)
        if 'available' in item:
            variant['available'] = bool(item['available'])
        if 'inventory_quantity' in item:
            variant['inventory_quantity'] = item['inventory_quantity']
        options = item.get('options') if isinstance(item.get('options'), dict) else {}
        for name in options:
            if name not in option_names and len(option_names) < 3:
                option_names.append(str(name))
        for index in range(1, 4):
            direct = item.get(f'option{index}')
            if direct not in (None, ''):
                variant[f'option{index}'] = str(direct)
        variant_images = _image_urls(item.get('images') or item.get('image'), product_url)
        if variant_images:
            variant['featured_image'] = {'src': variant_images[0]}
        variants.append(variant)
    for variant, item in zip(variants, raw_variants):
        options = item.get('options') if isinstance(item.get('options'), dict) else {}
        for index, name in enumerate(option_names, 1):
            if name in options:
                variant[f'option{index}'] = str(options[name])
    options = raw.get('options')
    if not isinstance(options, list):
        options = [{'name': name} for name in option_names]
    product = {'id': product_id, 'handle': product_handle(raw, product_url),
               'title': title, 'url': product_url, 'currency': 'USD',
               'description': str(raw.get('description') or ''),
               'body_html': str(raw.get('description') or ''),
               'vendor': str(raw.get('vendor') or ''),
               'images': [{'src': url, 'variant_ids': []} for url in images],
               'options': options[:3], 'variants': variants}
    return product


def _candidate_categories(root, full_scope=False):
    categories = read_json(Path(root) / 'url.json')
    scope_path = Path(root) / 'sample_scope.json'
    scope = read_json(scope_path) if scope_path.is_file() else {}
    if full_scope:
        return list(categories.items())
    values = []
    for item in scope.get('sample_candidates', []):
        if isinstance(item, dict):
            values.append((item.get('category'), item.get('url')))
    values.extend((scope.get('selected_categories') or {}).items())
    values.extend(categories.items())
    result, seen = [], set()
    for key, url in values:
        if key in categories and categories[key] == url and url not in seen:
            result.append((key, url)); seen.add(url)
    return result


def collect_details(root, adapter, tracker, full_scope=False):
    detail, listings, small_fallback = {}, {}, None
    for key, category_url in _candidate_categories(root, full_scope):
        start = len(tracker.calls)
        raw_urls = adapter.collect_product_urls(tracker, category_url)
        calls = _request_slice(tracker, start, '分类采集')
        if not isinstance(raw_urls, list):
            raise CrawlError('collect_product_urls 必须返回 URL 数组')
        urls = []
        for value in raw_urls:
            url = _valid_url(value, category_url)
            if url not in urls:
                urls.append(url)
        if not urls:
            if full_scope:
                continue
            continue
        if not full_scope and len(urls) < 3:
            # Keep looking for a richer leaf.  If every real candidate is a
            # genuinely tiny catalogue, retain the first complete non-empty
            # one rather than fabricating or rejecting a third product.
            if small_fallback is None:
                small_fallback = (key, category_url, urls, calls)
            continue
        proof = {'category': key, 'category_url': category_url,
                 'product_urls': urls, 'requests': calls}
        relative = 'evidence/adapter-listing-' + hashlib.sha256(category_url.encode()).hexdigest()[:16] + '.json'
        save_json(Path(root) / relative, proof)
        detail[key] = urls
        listings[key] = {'format': 'site_adapter', 'url': category_url,
                         'complete': True, 'urls': urls, 'catalog_urls': urls,
                         'feeds': [], 'pages': [], 'evidence_file': relative}
        if not full_scope:
            break
    if not detail and small_fallback is not None and not full_scope:
        key, category_url, urls, calls = small_fallback
        proof = {'category': key, 'category_url': category_url,
                 'product_urls': urls, 'requests': calls,
                 'small_catalog_after_candidate_exhaustion': True}
        relative = 'evidence/adapter-listing-' + hashlib.sha256(category_url.encode()).hexdigest()[:16] + '.json'
        save_json(Path(root) / relative, proof)
        detail[key] = urls
        listings[key] = {'format': 'site_adapter', 'url': category_url,
                         'complete': True, 'urls': urls, 'catalog_urls': urls,
                         'feeds': [], 'pages': [], 'evidence_file': relative,
                         'small_catalog': True}
    if not detail:
        raise CrawlError('site_adapter 未在候选分类中找到商品')
    save_json(Path(root) / 'detail_url.json', detail)
    save_json(Path(root) / 'listings.json', listings)
    return detail, listings


def fetch_samples(root, adapter, tracker, detail, all_products=False):
    urls = list(dict.fromkeys(url for links in detail.values() for url in links))
    if not all_products:
        urls = urls[:3]
    if not urls:
        raise CrawlError('site_adapter 未动态取得真实父商品')
    samples = []
    for product_url in urls:
        start = len(tracker.calls)
        raw = adapter.fetch_product(tracker, product_url)
        calls = _request_slice(tracker, start, '商品采集')
        product = normalize_product(raw, product_url)
        relative = 'evidence/adapter-product-' + hashlib.sha256(product_url.encode()).hexdigest()[:16] + '.json'
        # The runner deliberately requests the US storefront context and only
        # accepts products whose live response resolves to USD.  Persist that
        # host-owned request context next to the audited response so an
        # unconfirmed homepage market does not force model-authored evidence.
        save_json(Path(root) / relative, {
            'url': product_url, 'product': product, 'requests': calls,
            'market': {'country': 'US', 'currency': product['currency']},
        })
        samples.append({'url': product_url, 'source': 'json',
                        'endpoint': calls[-1]['url'], 'product': product,
                        'source_mapping': {'kind': 'site_adapter', 'file': relative}})
    save_json(Path(root) / 'samples.json', samples)
    if samples:
        save_json(Path(root) / 'market_evidence.json', {
            'file': samples[0]['source_mapping']['file'],
            'country_path': '/market/country',
            'currency_path': '/market/currency',
        })
    return samples


def run_adapter(root, all_products=False, full_scope=False, links_only=False,
                reuse_detail=False, client=None):
    root = Path(root).resolve()
    adapter = _load_adapter(root)
    tracker = TrackedClient(root, client)
    try:
        if reuse_detail:
            detail = read_json(root / 'detail_url.json')
            listings = read_json(root / 'listings.json')
        else:
            detail, listings = collect_details(root, adapter, tracker, full_scope=full_scope)
        if links_only:
            return {'categories': len(detail), 'links': sum(map(len, detail.values()))}
        samples = fetch_samples(root, adapter, tracker, detail, all_products=all_products)
        release = require_release(root)
        scope_path = root / 'sample_scope.json'
        scope = read_json(scope_path) if scope_path.is_file() else {}
        scope.update(selected_categories={key: read_json(root / 'url.json')[key] for key in detail},
                     listing_scope='all' if full_scope else 'selected',
                     detail_scope='all_products' if all_products else 'first_3_products',
                     selection_status='site_adapter',
                     navigation_version=release['navigation_version'])
        save_json(scope_path, scope)
        save_json(root / 'detail_release.json', {
            'navigation_version': release['navigation_version'],
            'sha256': digest(root / 'detail_url.json')})
        export(samples, detail, root)
        from .validator import validate
        result = validate(root)
        save_json(root / 'adapter_validation.json', result)
        return result
    finally:
        tracker.close()


def verify_source(root, sample):
    mapping = sample.get('source_mapping') or {}
    file = (Path(root) / mapping.get('file', '')).resolve()
    evidence = (Path(root) / 'evidence').resolve()
    if not _inside(file, evidence) or not file.is_file():
        raise CrawlError('site_adapter 商品证据缺失或越界')
    proof = read_json(file)
    if proof.get('url') != sample.get('url') or proof.get('product') != sample.get('product'):
        raise CrawlError('site_adapter 商品与宿主保存证据不一致')
    if not isinstance(proof.get('requests'), list) or not proof['requests']:
        raise CrawlError('site_adapter 商品没有真实请求证据')


def validate_listing(root, listing, links):
    file = (Path(root) / listing.get('evidence_file', '')).resolve()
    evidence = (Path(root) / 'evidence').resolve()
    if not _inside(file, evidence) or not file.is_file():
        raise CrawlError('site_adapter 分类证据缺失或越界')
    proof = read_json(file)
    if (proof.get('category_url') != listing.get('url')
            or proof.get('product_urls') != links
            or not proof.get('requests')):
        raise CrawlError('site_adapter 分类列表与请求证据不一致')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--links-only', action='store_true')
    parser.add_argument('--full-scope', action='store_true')
    parser.add_argument('--reuse-detail', action='store_true')
    args = parser.parse_args(argv)
    print(json.dumps(run_adapter(
        Path(args.root), all_products=args.all, full_scope=args.full_scope,
        links_only=args.links_only, reuse_detail=args.reuse_detail),
        ensure_ascii=False))


if __name__ == '__main__':
    main()
