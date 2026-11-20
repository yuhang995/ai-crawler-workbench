"""Validate artifacts and raw response content, never selector proofs or host hashes."""
import csv
import hashlib
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from .html_parser import normalize_event_attributes
from urllib.parse import urljoin, urlsplit, parse_qs
from lxml import html as lh
from .common import (CrawlError, read_json, canonical, save_json, query,
                     split_category_key)
from .nav_extractor import is_blacklisted, extract_navigation
from .navigation_rules import normalize_url
from .shopify_fastpath import (listing_dom, money, handle, variant_images,
                               product_gallery, embedded_product_handles,
                               explicit_colorway_entries, single_product_colour, variant_external_url,
                               hydrogen_group_products, merged_options, product_categories,
                               primary_collection)


def _allow_large_csv_fields():
    """Raise the stdlib CSV parser limit for evidence-backed rich descriptions."""
    limit = min(sys.maxsize, 64 * 1024 * 1024)
    try:
        csv.field_size_limit(limit)
    except OverflowError:  # pragma: no cover - relevant only on narrow C longs
        csv.field_size_limit(2 ** 31 - 1)


_allow_large_csv_fields()


def _page_html(page, root=None):
    html = page.get('html', '')
    if html or not page.get('html_file') or root is None:
        return html
    target = (Path(root) / page['html_file']).resolve()
    evidence = (Path(root) / 'evidence').resolve()
    if not _is_relative_to(target, evidence) or not target.is_file():
        raise CrawlError('分类 HTML 证据文件缺失或越界')
    return target.read_text(encoding='utf-8')


def source_order(listing, root=None):
    """Independently rebuild a non-DOM listing order from saved source evidence."""
    products, order, seen_ids, handles = [], [], set(), {}
    for feed in listing.get('feeds', []):
        batch = feed.get('products') if isinstance(feed, dict) else None
        if not isinstance(batch, list):
            raise CrawlError('分类 feed 证据无效')
        for product in batch:
            slug, product_id = product.get('handle'), str(product.get('id', ''))
            if not slug or not product_id or product_id in seen_ids:
                raise CrawlError('分类 feed 商品身份重复或缺失')
            if slug in handles and handles[slug] != product_id:
                raise CrawlError('分类 feed handle 身份冲突')
            handles[slug] = product_id;seen_ids.add(product_id)
            products.append(product);order.append(slug)
    page_size = listing.get('feed_page_size')
    if not products or not isinstance(page_size, int) or page_size <= 0:
        raise CrawlError('分类 feed 缺少完整性信息')
    if listing['feeds'] and len(listing['feeds'][-1].get('products', [])) >= page_size:
        raise CrawlError('分类 feed 最后一页未证明结束')
    evidence = listing.get('order_evidence', 'html_dom')
    pages = listing.get('pages', [])
    if not pages:
        raise CrawlError('分类列表缺少页面证据')
    if evidence == 'products_feed_fallback':
        scoped = any(key != 'page' for key in parse_qs(urlsplit(listing['url']).query))
        warnings = {warning.get('code') for warning in listing.get('warnings', [])}
        if scoped or 'listing_order_feed_fallback' not in warnings:
            raise CrawlError('筛选目录或无警告记录不得使用 Feed 顺序兜底')
        expected = order
    elif evidence == 'embedded_handles':
        observed = embedded_product_handles(_page_html(pages[0], root), products, pages[0]['url'])
        if len(observed) < 3:
            raise CrawlError('内嵌商品顺序证据不足')
        present = set(observed)
        expected = observed + [slug for slug in order if slug not in present]
    else:
        raise CrawlError('未知的非 DOM 商品顺序证据')
    if len(pages) != 1 or pages[0].get('handles') != expected:
        raise CrawlError('商品顺序证据与保存列表不一致')
    return expected


def _is_relative_to(path, parent):
    """Path.is_relative_to compatibility for the project's Python 3.8 runtime."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def pointer(data, path):
    """RFC 6901 field references are data locations, not UI selectors."""
    if path == '':
        return data
    if not isinstance(path, str) or not path.startswith('/'):
        raise CrawlError('源字段必须是 JSON Pointer')
    try:
        for token in path[1:].split('/'):
            token = token.replace('~1', '/').replace('~0', '~')
            data = data[int(token)] if isinstance(data, list) else data[token]
        return data
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise CrawlError('源字段不存在: ' + path) from e


def verify_source_mapping(root, sample):
    """Check nonstandard normalized variants against unmodified JSON or embedded JSON."""
    mapping = sample['source_mapping']
    file = (root / mapping['file']).resolve()
    if not _is_relative_to(file, (root / 'evidence').resolve()):
        raise CrawlError('源响应必须位于 evidence 目录')
    from .custom_site import source_adapter
    adapter = source_adapter(mapping.get('kind'))
    if adapter:
        adapter.verify_source(root, sample)
        return
    if mapping.get('kind') == 'braun_bigcommerce':
        from .braun_product import verify_braun_product
        verify_braun_product(root, sample)
        return
    if mapping.get('kind') == 'hydrogen_devalue':
        from .shopify_fastpath import hydrogen_product_from_html
        observed = hydrogen_product_from_html(
            file.read_text(encoding='utf-8'), mapping.get('handle', ''))
        if observed != sample.get('product'):
            raise CrawlError('Hydrogen 规范化商品与原始页面数据流不一致')
        return
    if mapping.get('script_id'):
        doc = lh.fromstring(normalize_event_attributes(file.read_text(encoding='utf-8')))
        nodes = doc.xpath('//script[@id=$id]', id=mapping['script_id'])
        if len(nodes) != 1:
            raise CrawlError('嵌入 JSON 数据块不存在')
        import json
        raw = json.loads(nodes[0].text)
    else:
        raw = read_json(file)
    p = sample['product']
    for name in ('id', 'title'):
        if str(pointer(raw, mapping['product_fields'][name])) != str(p[name]):
            raise CrawlError('规范化商品与原始源响应不一致: ' + name)
    if mapping.get('variant_sources'):
        variants = []
        for source in mapping['variant_sources']:
            variant_file = (root / source['file']).resolve()
            if not _is_relative_to(variant_file, (root / 'evidence').resolve()):
                raise CrawlError('变体源响应必须位于 evidence 目录')
            variants.append(pointer(read_json(variant_file), source.get('path', '')))
    else:
        variants = pointer(raw, mapping['variants_path'])
    if not isinstance(variants, list) or len(variants) != len(p['variants']):
        raise CrawlError('规范化变体数量与源响应不同')
    fields = mapping['variant_fields']
    transforms = mapping.get('variant_transforms', {})
    if not {'id', 'price'}.issubset(fields):
        raise CrawlError('必须映射源变体 ID 和价格')
    for raw_variant, variant in zip(variants, p['variants']):
        for name in ('id', 'price', 'compare_at_price', 'option1', 'option2', 'option3'):
            value = variant.get(name)
            if value not in (None, ''):
                observed = pointer(raw_variant, fields[name]) if name in fields else None
                if transforms.get(name) == 'sfcc_size_eu':
                    try:
                        import json
                        sizes = json.loads(str(observed))
                        observed = next((str(item).split(';', 1)[0] for item in sizes.values()
                                         if str(item).strip()), observed)
                    except (ValueError, TypeError, AttributeError):
                        pass
                elif transforms.get(name) == 'woocommerce_minor_units':
                    try:
                        from decimal import Decimal
                        minor = int(pointer(raw_variant, '/prices/currency_minor_unit'))
                        observed = Decimal(str(observed)) / (Decimal(10) ** minor)
                    except (ValueError, TypeError, KeyError, IndexError):
                        pass
                if transforms.get(name) == 'woocommerce_minor_units':
                    try:
                        mismatch = Decimal(str(observed)) != Decimal(str(value))
                    except (ValueError, TypeError):
                        mismatch = True
                else:
                    mismatch = str(observed) != str(value)
                if name not in fields or mismatch:
                    raise CrawlError('规范化变体字段与源响应不一致: ' + name)
    paths = mapping.get('image_paths', [])
    if not paths:
        raise CrawlError('必须映射原始商品图集')
    raw_images = [pointer(raw, path) for path in paths]
    raw_images = {urljoin(sample['url'], str(u)) for u in raw_images}
    if not set(product_gallery(p)).issubset(raw_images):
        raise CrawlError('商品图集包含未映射的源图片')
    for variant in p['variants']:
        if not set(variant_images(p, variant)).issubset(raw_images):
            raise CrawlError('规范化图片未出现在源图集')


def custom_front_order(listing):
    """Result-level check for non-Shopify paths using actual HTML anchors."""
    catalog = listing.get('catalog_urls', [])
    if not catalog:
        raise CrawlError('非标分类缺少商品 URL 集合')
    allowed = {canonical(u) for u in catalog}
    order = []
    for page in listing['pages']:
        doc = lh.fromstring(normalize_event_attributes(page['html']))
        for el in doc.xpath('//header|//footer|//nav|//template|//*[@hidden]|//*[@aria-hidden="true"]'):
            if el.getparent() is not None:
                el.getparent().remove(el)
        for a in doc.xpath('//a[@href]'):
            url = canonical(urljoin(page['url'], a.get('href')))
            if url in allowed and url not in order:
                order.append(url)
    return order


def validate_navigation(categories, documents, base, evidence=None):
    if not isinstance(categories, dict) or not categories:
        raise CrawlError('url.json 为空')
    if evidence and evidence.get('method')=='shopify_practical':
        from .practical_navigation import validate_practical
        return validate_practical(categories,evidence,documents,base)
    observed = set()
    for html in documents:
        doc = lh.fromstring(normalize_event_attributes(html or '<html/>'))
        for href in doc.xpath('//a/@href'):
            observed.add(canonical(normalize_url(urljoin(base, href))))
            if evidence and evidence.get('method') == 'owned_menu_tree':
                target, origin = urlsplit(urljoin(base, href)), urlsplit(base)
                if (target.hostname or '').removeprefix('www.') == (origin.hostname or '').removeprefix('www.'):
                    observed.add(canonical(normalize_url(target._replace(scheme=origin.scheme, netloc=origin.netloc).geturl())))
    if evidence is not None:
        from .navigation_guard import check
        source = documents[-1]
        if evidence.get('source_html_sha256'):
            import hashlib
            matching = [h for h in documents if hashlib.sha256(h.encode('utf-8')).hexdigest() == evidence['source_html_sha256']]
            if not matching:
                raise CrawlError('缺少菜单树对应的原始快照')
            source = matching[0]
        check(categories,evidence,source,base)
        model_urls={e['url'] for r in evidence['roots'] for e in r['links'] if e.get('model')}
        for key,url in categories.items():
            if is_blacklisted(url, key, allow_product=url in model_urls) or canonical(url) not in observed or urlsplit(url).hostname!=urlsplit(base).hostname:
                raise CrawlError('独立核对目录包含无效或未观察到的链接: '+key)
        return
    # Validate the authoritative current menu, including hierarchy and home keys.
    # No generated selectors or host hashes: reconstruct labels from saved DOM.
    snapshots = [h for h in documents if 'data-crawl-menu-snapshot=' in h]
    authority = snapshots[-1] if snapshots else documents[-1]
    from .navigation_rules import verify_primary_ownership
    verify_primary_ownership(categories, authority)
    expected = extract_navigation(authority, base)
    model_keys = {e['key'] for e in expected.metadata.get('entries', []) if e['source'] == 'menu_model'}
    for key, url in categories.items():
        if not key.strip() or is_blacklisted(url, key, allow_product=key in model_keys):
            raise CrawlError('目录包含黑名单链接: ' + str(url))
        levels = split_category_key(key)
        if len(levels) > 3 or any(x.isdigit() for x in levels) or key.startswith('home,'):
            raise CrawlError('目录层级或首页平级键无效: ' + key)
        if any(not levels[i].startswith(levels[i-1] + ' ') for i in range(1, len(levels))):
            raise CrawlError('目录未使用父级前缀: ' + key)
        if canonical(url) not in observed:
            raise CrawlError('目录 URL 未出现在首页/菜单响应: ' + url)
        if urlsplit(url).hostname != urlsplit(base).hostname:
            raise CrawlError('目录跨市场域名')
        if expected.data.get(key) != url:
            raise CrawlError('目录键名/归属与当前菜单或首页区块不符: ' + key)
    if categories != expected.data:
        raise CrawlError('目录遗漏当前菜单或首页分类')


def validate_samples(detail, samples, listings, rows, full=False, root=None):
    from .custom_site import ordered_product_urls
    expected_urls = ordered_product_urls(detail, listings)
    if full and root is not None and (Path(root) / 'failed_products.json').is_file():
        failures = read_json(Path(root) / 'failed_products.json')
        no_image_failures = [item for item in failures if isinstance(item, dict)
                             and item.get('code') == 'source_product_without_images']
        if no_image_failures:
            warnings_path = Path(root) / 'warnings.json'
            warnings = read_json(warnings_path) if warnings_path.is_file() else []
            matching = next((warning for warning in warnings
                             if warning.get('code') == 'products_without_images_skipped'), None)
            if (not matching or matching.get('failed') != len(no_image_failures)
                    or set(matching.get('urls', []))
                    != {item.get('url') for item in no_image_failures}):
                raise CrawlError('无图商品跳过记录与审计警告不一致')
        failed_urls = {item.get('url') for item in failures if isinstance(item, dict)}
        expected_urls = [url for url in expected_urls if url not in failed_urls]
    if not full:
        expected_urls = expected_urls[:3]
    if (not expected_urls or (not full and len(expected_urls) != 3)
            or [s['url'] for s in samples] != expected_urls):
        raise CrawlError('必须是分类顺序下前 3 个不同商品')
    ids = [str(s['product']['id']) for s in samples]
    if len(set(ids)) != len(expected_urls):
        raise CrawlError('样本不是三个不同商品')
    if any(not isinstance(links, list) for links in detail.values()):
        raise CrawlError('detail_url.json 分类值必须是数组')
    for key, links in detail.items():
        listing = listings[key]
        if not listing.get('complete'):
            raise CrawlError('分类列表未完整采集')
        from .custom_site import source_adapter
        adapter = source_adapter(listing.get('format'))
        if adapter:
            adapter.validate_listing(listing, links)
            continue
        if listing.get('format') == 'direct_product':
            if links != [listing['url']]:
                raise CrawlError('型号菜单链接与商品不一致')
            continue
        if listing.get('format') == 'brumate_nosto_grid':
            from .brumate_listing import validate_listing
            validate_listing(listing, links, samples=samples)
            continue
        if listing.get('format') == 'braun':
            from .braun_listing import validate_listing
            validate_listing(listing, links)
            card_skus = listing['source_skus']
            for sample in samples:
                if sample['url'] in card_skus and (
                        [str(v['sku']) for v in sample['product']['variants']]
                        != [str(card_skus[sample['url']])]):
                    raise CrawlError('Braun 列表 SKU 与商品详情不一致')
            continue
        if listing.get('format') == 'custom':
            if [canonical(u) for u in links] != custom_front_order(listing):
                raise CrawlError('非标商品顺序与页面不一致')
            continue
        if listing.get('format') == 'woocommerce-store-api-json':
            observed = []
            page_size = 100
            page_rows = []
            for relative in listing.get('pages', []):
                file = (root / relative).resolve()
                if not _is_relative_to(file, (root / 'evidence').resolve()):
                    raise CrawlError('WooCommerce 列表源响应必须位于 evidence')
                rows_from_page = read_json(file)
                if not isinstance(rows_from_page, list):
                    raise CrawlError('WooCommerce 列表源响应不是数组')
                page_rows.append(rows_from_page)
                observed.extend(str(row.get('permalink', '')) for row in rows_from_page
                                if isinstance(row, dict) and row.get('permalink'))
            if not page_rows or len(page_rows[-1]) >= page_size:
                raise CrawlError('WooCommerce 列表分页证据不完整')
            if [canonical(u) for u in links] != [canonical(u) for u in observed]:
                raise CrawlError('WooCommerce 商品列表与 Store API 顺序或范围不一致: ' + key)
            continue
        if listing.get('format') == 'hydrogen':
            from .shopify_fastpath import _hydrogen_collection_page
            observed, parent_urls = [], {}
            parent_ids, parent_handles = {}, {}
            next_url, seen_urls = listing.get('url'), set()
            parent_cards = None
            for page in listing.get('pages', []):
                parsed = _hydrogen_collection_page(_page_html(page, root), page.get('url', ''),
                                                   page.get('router_response'))
                if parsed is None or parsed['handles'] != page.get('handles'):
                    raise CrawlError('Hydrogen 分类页证据无法复建')
                current_kind = parsed['pagination_kind'] == 'parent_cards'
                if parent_cards is not None and current_kind != parent_cards:
                    raise CrawlError('Hydrogen 分类分页格式发生变化')
                parent_cards = current_kind
                if not parent_cards:
                    observed.extend(parsed['handles'])
                    continue
                if page['url'] != next_url or next_url in seen_urls:
                    raise CrawlError('Hydrogen 分类分页与前端下一页链接不符')
                seen_urls.add(next_url)
                next_url = parsed['next_url']
                for card in parsed['products']:
                    pid, slug = card['id'], card['handle']
                    if (parent_ids.setdefault(slug, pid) != pid or
                            parent_handles.setdefault(pid, slug) != slug):
                        raise CrawlError('Hydrogen 分类父商品链接与身份冲突')
                    parent_urls.setdefault(pid, card['url'])
            if parent_cards:
                sample_ids = {sample['url']: str(sample['product']['id']) for sample in samples}
                if (next_url or list(parent_urls.values()) != links
                        or any(sample_ids.get(url, pid) != pid for pid, url in parent_urls.items())):
                    raise CrawlError('Hydrogen 父商品与原始页面顺序、身份或分页范围不符: ' + key)
            elif [handle(url) for url in links] != observed:
                raise CrawlError('Hydrogen 商品列表与原始页面顺序/范围不符: ' + key)
            continue
        feed_by_handle = {p['handle']: str(p['id']) for feed in listing['feeds'] for p in feed['products']}
        if listing.get('card_sources'):
            from .shopify_fastpath import curated_card_handles
            first = listing['pages'][0]
            declared = curated_card_handles(_page_html(first, root), first['url'])
            cards = listing['card_sources']
            observed_cards = {}
            for source in cards:
                product = source['response'].get('product', source['response'])
                if (urlsplit(source['url']).hostname != urlsplit(first['url']).hostname
                        or handle(source['url']) != product['handle']
                        or source['endpoint'] != source['url'] + '.json'):
                    raise CrawlError('落地页卡片身份或接口来源不符')
                observed_cards[product['handle']] = str(product['id'])
            if set(observed_cards) != declared or any(feed['products'] for feed in listing['feeds']):
                raise CrawlError('落地页卡片来源不完整或混入伪造 feed')
            feed_by_handle.update(observed_cards)
        if listing.get('membership_sources'):
            from .shopify_fastpath import landing_collection_links
            first = listing['pages'][0]
            children = set(landing_collection_links(_page_html(first, root), first['url']))
            sources = listing['membership_sources']
            if {s['url'] for s in sources} != children:
                raise CrawlError('落地页子集合来源与真实链接不一致')
            if listing['feeds'][0].get('products') or listing['feeds'][1:] != [f for s in sources for f in s['feeds']]:
                raise CrawlError('落地页 feed 没有可复验的子集合来源')
            if any(not s['feeds'] or len(s['feeds'][-1]['products']) >= s['page_size'] for s in sources):
                raise CrawlError('落地页子集合分页不完整')
        if listing.get('order_evidence', 'html_dom') != 'html_dom':
            front = source_order(listing, root)
        else:
            front, seen = [], set()
            for page in listing['pages']:
                slugs, _, _ = listing_dom(_page_html(page, root), page['url'])
                for slug in slugs:
                    if slug not in feed_by_handle:
                        continue  # The contract is HTML ∩ collection feed, preserving HTML order.
                    pid = feed_by_handle[slug]
                    if pid not in seen:
                        front.append(slug)
                        seen.add(pid)
        if [handle(u) for u in links] != front:
            raise CrawlError('商品列表与前端顺序/范围不符: ' + key)
    expected = {}
    product_checks = {}
    for sample in samples:
        p, source = sample['product'], sample['source']
        if (not sample.get('source_mapping') and handle(sample['url']) != p['handle']) or not p.get('title'):
            raise CrawlError('样本身份/标题无效')
        variants = p.get('variants', [])
        if not variants or (len(variants) >= 250 and
                            not source_adapter((sample.get('source_mapping') or {}).get('kind'))):
            raise CrawlError('样本变体为空或达到截断上限')
        imgs = product_gallery(p)
        if not imgs or any(urlsplit(u).scheme not in ('http', 'https')
                           or not urlsplit(u).netloc for u in imgs):
            raise CrawlError('图片 URL 无效')
        categories = (','.join(product_categories(sample, detail, listings))
                      if source_adapter(sample.get('source_mapping', {}).get('kind')) else None)
        product_checks[str(p['id'])] = {
            'images': imgs, 'images_csv': ','.join(imgs), 'categories': categories,
        }
        for v in variants:
            key = (str(p['id']), str(v['id']))
            if key in expected:
                raise CrawlError('源响应重复变体 ID')
            expected[key] = (p, v, source, sample)
    actual = Counter((r['Product ID'], r['Variant ID']) for r in rows)
    if actual != Counter({k: 1 for k in expected}):
        raise CrawlError('变体 ID/数量不等于源响应，存在缺失、重复或伪造组合')
    for row in rows:
        p, v, source, sample = expected[(row['Product ID'], row['Variant ID'])]
        product_check = product_checks[str(p['id'])]
        if source_adapter(sample.get('source_mapping', {}).get('kind')):
            categories = product_check['categories']
            if row['Categories'] != categories or row['Tags'] != categories:
                raise CrawlError('父商品分类归属与列表身份依据不一致')
        if row['Name'] != p['title'] or row['Currency'] != 'USD':
            raise CrawlError('标题或币种不匹配')
        price = money(v['price'], source)
        compare = money(v['compare_at_price'], source) if v.get('compare_at_price') not in ('', None) else ''
        discounted = compare and Decimal(compare) > Decimal(price)
        if row['Regular price'] != (compare if discounted else price) or row['Sale price'] != (price if discounted else ''):
            raise CrawlError('价格与源响应不匹配')
        if row['External URL'] != variant_external_url(sample, v):
            raise CrawlError('变体链接无效')
        if row['SKU'] != str(v['id']) or row['Parent'] != str(p['id']):
            raise CrawlError('父子标识无效')
        if row['Images'] != product_check['images_csv']:
            raise CrawlError('商品图集缺失或顺序不符')
        if row.get('Variant Image', '') != next(iter(variant_images(p, v)), ''):
            raise CrawlError('变体主图缺少源绑定依据')
        for i in range(1, 4):
            expected_option = v.get(f'option{i}') or ''
            # Shopify combined-listing responses sometimes retain an unused
            # Default Title slot without defining that option. It is not a
            # customer-selectable dimension; keep raw evidence unchanged.
            if i > len(p.get('options', [])) and expected_option == 'Default Title':
                expected_option = ''
            if row[f'Attribute {i} value(s)'] != expected_option:
                raise CrawlError('属性组合不是源响应中的真实变体')
        if re_anchor(row['Description']):
            raise CrawlError('描述含 a 标签')
    return {'products': len(samples), 'variants': len(rows)}


def re_anchor(value):
    import re
    return bool(re.search(r'<a(?:\s|>)', value, re.I))


def _validate_shopify_import(root, imported, samples):
    detail = read_json(root / 'detail_url.json') if (root / 'detail_url.json').is_file() else {}
    listings = read_json(root / 'listings.json') if (root / 'listings.json').is_file() else {}
    manifest_path = root / 'shopify_merge_groups.json'
    if not manifest_path.is_file():
        rows_by_handle = {}
        for row in imported:
            rows_by_handle.setdefault(row['Handle'], []).append(row)
        for sample in samples:
            product = sample['product']
            group = rows_by_handle.get(product['handle'], [])
            categories = product_categories(sample, detail, listings) if detail else []
            expected_type = ','.join(categories)
            expected_collection = primary_collection(categories)
            if expected_collection and (not group
                    or group[0].get('Type') != expected_type
                    or group[0].get('Collection') != expected_collection):
                raise CrawlError('Shopify 父商品分类或主集合归属缺失')
            images = [row for row in group if row['Image Src']]
            if ([row['Image Src'] for row in images] != product_gallery(product)
                    or [row['Image Position'] for row in images]
                    != [str(index + 1) for index in range(len(images))]):
                raise CrawlError('Shopify 导入图集缺失或顺序不符')
            variants = [row for row in group if row['Variant SKU']]
            if [row['Variant SKU'] for row in variants] != [str(v['id']) for v in product['variants']]:
                raise CrawlError('Shopify 导入变体被图片行改变')
            for row, variant in zip(variants, product['variants']):
                if row['Variant Image'] != next(iter(variant_images(product, variant)), ''):
                    raise CrawlError('Shopify 变体图片绑定错误')
        return {}

    manifest = read_json(manifest_path)
    groups = manifest.get('groups') if isinstance(manifest, dict) else None
    if not isinstance(groups, list) or not groups:
        raise CrawlError('Shopify 合并清单为空或格式无效')
    expected_handles, variant_total = [], 0
    for group in groups:
        group_handle = group.get('handle')
        members = group.get('members')
        if (not isinstance(group_handle, str) or not group_handle
                or not isinstance(members, list) or not members):
            raise CrawlError('Shopify 合并分组缺少 handle 或成员')
        expected_handles.append(group_handle)
        declared = [(str(member.get('handle', '')), str(member.get('product_id', '')),
                     str(member.get('color', ''))) for member in members]
        observed_members = None
        embedded = {}
        if group.get('source') == 'pdp_explicit_colorways':
            evidence = (root / str(group.get('evidence', ''))).resolve()
            if not _is_relative_to(evidence, (root / 'evidence').resolve()) or not evidence.is_file():
                raise CrawlError('颜色合并证据不在 evidence 目录')
            html = evidence.read_text(encoding='utf-8')
            observed_members = explicit_colorway_entries(html, str(group.get('seed_handle', '')))
            embedded = hydrogen_group_products(html)

        products = []
        for member in members:
            source_file = (root / str(member.get('source_file', ''))).resolve()
            if not _is_relative_to(source_file, (root / 'evidence').resolve()) or not source_file.is_file():
                raise CrawlError('颜色成员源响应不在 evidence 目录')
            product = read_json(source_file)
            product = product.get('product', product)
            if (str(product.get('id')) != str(member.get('product_id'))
                    or product.get('handle') != member.get('handle')):
                raise CrawlError('颜色成员商品身份与源响应不一致')
            variant_ids = [str(variant.get('id')) for variant in product.get('variants', [])]
            if not variant_ids or variant_ids != [str(value) for value in member.get('variant_ids', [])]:
                raise CrawlError('颜色成员变体与合并清单不一致')
            original = embedded.get(product['handle'])
            if original and variant_ids != [str(v['id']) for v in original['variants']]:
                raise CrawlError('Shopify 色款变体集合与商品页不一致')
            products.append((product, str(member.get('color', '')),
                             str(member.get('variant_image', ''))))
        if observed_members is not None:
            by_handle = {product['handle']: product for product, _, _ in products}
            observed = [(item['handle'], item['product_id'],
                         item['color'] or single_product_colour(by_handle.get(item['handle'], {})))
                        for item in observed_members]
            if observed != declared:
                raise CrawlError('Shopify 合并成员与商品页显式颜色清单不一致')
            for observed_member, (_, _, image) in zip(observed_members, products):
                if observed_member.get('product_image') and observed_member['product_image'] != image:
                    raise CrawlError('Shopify 色款图片与商品页声明不一致')

        rows = [row for row in imported if row['Handle'] == group_handle]
        if not rows or rows[0]['Title'] != group.get('title'):
            raise CrawlError('Shopify 合并商品标题或首行缺失')
        expected_type = ','.join(group.get('categories', []))
        expected_collection = primary_collection(group.get('categories', []))
        if expected_collection and (rows[0].get('Type') != expected_type
                or rows[0].get('Collection') != expected_collection):
            raise CrawlError('Shopify 合并商品分类或主集合归属缺失')
        expected_images = list(dict.fromkeys(
            image for product, _, _ in products for image in product_gallery(product)))
        image_rows = [row for row in rows if row['Image Src']]
        if ([row['Image Src'] for row in image_rows] != expected_images
                or [row['Image Position'] for row in image_rows]
                != [str(index + 1) for index in range(len(expected_images))]):
            raise CrawlError('Shopify 合并商品图集缺失或顺序不符')
        variant_rows = [row for row in rows if row['Variant SKU']]
        expected_variants = [(product, colour, colour_image, variant)
                             for product, colour, colour_image in products
                             for variant in product['variants']]
        if [row['Variant SKU'] for row in variant_rows] != [str(item[3]['id']) for item in expected_variants]:
            raise CrawlError('Shopify 合并商品变体丢失或顺序不符')
        variant_total += len(expected_variants)
        embedded_variants = {(slug, str(v['id'])): v for slug, member in embedded.items()
                             for v in member['variants']}
        for row, (product, colour, colour_image, variant) in zip(variant_rows, expected_variants):
            if product['handle'] in embedded:
                observed_variant = embedded_variants.get((product['handle'], str(variant['id'])))
                if observed_variant is None or observed_variant.get('sku') != variant.get('sku'):
                    raise CrawlError('Shopify 色款 SKU 与商品页不一致')
                price = money(observed_variant['price'], 'collection')
                original = observed_variant.get('compare_at_price')
                compare = money(original, 'collection') if original not in (None, '') else ''
                sale = bool(compare and Decimal(compare) > Decimal(price))
                if row['Variant Price'] != price or row['Variant Compare At Price'] != (compare if sale else ''):
                    raise CrawlError('Shopify 色款价格与商品页不一致')
            if row['Variant Image'] != (colour_image or next(iter(variant_images(product, variant)), '')):
                raise CrawlError('Shopify 合并变体图片绑定错误')
            options = [(option.get('name', '') if isinstance(option, dict) else str(option),
                        str(variant.get(f'option{index}') or ''))
                       for index, option in enumerate(product.get('options', [])[:3], 1)]
            options = merged_options(options, colour)
            actual = [(row[f'Option{index} Name'], row[f'Option{index} Value'])
                      for index in range(1, 4)]
            expected = options[:3] + [('', '')] * (3 - len(options[:3]))
            if actual != expected:
                raise CrawlError('Shopify 合并变体选项与源响应不一致')
    if set(row['Handle'] for row in imported) != set(expected_handles):
        raise CrawlError('Shopify 导入包含合并清单之外的商品 handle')
    if manifest.get('variants') != variant_total:
        raise CrawlError('Shopify 合并清单变体统计不一致')
    return {'shopify_products': len(groups), 'shopify_variants': variant_total,
            'merged_colorway_groups': sum(group.get('source') == 'pdp_explicit_colorways'
                                          for group in groups)}


def validate(root):
    start = time.perf_counter()
    root = Path(root)
    from .navigation_release import require_release
    release = require_release(root)
    product_run = root / 'product_run.json'
    if product_run.is_file() and read_json(product_run).get('status') in ('running', 'failed'):
        raise CrawlError('商品任务尚未完成或已失败，不能复用旧验证结果')
    config = read_json(root / 'config.json')
    if config.get('navigation_version') != release['navigation_version']:
        raise CrawlError('采集配置未绑定当前目录版本')
    categories, detail, samples = (read_json(root / n) for n in ('url.json', 'detail_url.json', 'samples.json'))
    if any(key not in categories for key in detail):
        raise CrawlError('商品分类不属于已审核目录')
    from .navigation_release import digest
    binding = read_json(root / 'detail_release.json')
    if binding.get('navigation_version') != release['navigation_version'] or binding.get('sha256') != digest(root / 'detail_url.json'):
        raise CrawlError('商品链接文件与目录版本错配')
    documents = [p.read_text(encoding='utf-8') for p in sorted((root / 'evidence').glob('home*.html'))]
    verified_home = root / 'evidence' / 'home-verified.html'
    if verified_home.is_file():
        documents.append(verified_home.read_text(encoding='utf-8'))
    proof_path = root / 'evidence' / 'navigation-observed.json'
    if not proof_path.is_file():
        raise CrawlError('缺少目录独立核对证据，不能发布成功结果')
    source_categories = (read_json(root / 'navigation_source_categories.json')
                         if 'navigation_source_categories.json' in release['inputs'] else categories)
    if release.get('method') != 'simple_nav':
        validate_navigation(source_categories, documents, config['base_url'], evidence=read_json(proof_path))
    market = read_json(root / 'detection.json')['market']
    if not market['confirmed']:
        proof_path = root / 'market_evidence.json'
        if not proof_path.is_file():
            raise CrawlError('未验证 US/USD 市场')
        proof = read_json(proof_path)
        file = (root / proof['file']).resolve()
        if not _is_relative_to(file, (root / 'evidence').resolve()):
            raise CrawlError('市场源响应必须位于 evidence')
        from .custom_site import source_adapter
        adapter = source_adapter(proof.get('kind'))
        if adapter:
            if not samples or any(proof.get(k) != samples[0].get('source_mapping', {}).get(k)
                                  for k in ('file', 'kind')):
                raise CrawlError('市场证据与当前样本来源不一致')
            country, currency = adapter.product_market(root, samples[0])
        elif proof.get('kind') == 'braun_bigcommerce':
            from .braun_product import braun_product_market
            if not samples or samples[0].get('source_mapping') != proof:
                raise CrawlError('Braun 市场证据与样本不一致')
            country, currency = braun_product_market(root, samples[0])
        else:
            raw = read_json(file)
            country = (pointer(raw, proof['country_path']) if proof.get('country_path')
                       else str(pointer(raw, proof['locale_path'])).replace('-', '_').rsplit('_', 1)[-1])
            currency = pointer(raw, proof['currency_path'])
        if country != 'US' or currency != 'USD':
            raise CrawlError('市场源响应不是 US/USD')
    with (root / 'result.csv').open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    listings = read_json(root / 'listings.json')
    for key, listing in listings.items():
        for url, source in listing.get('identity_sources', {}).items():
            if url not in detail.get(key, []) or source.get('url') != url:
                raise CrawlError('商品身份依据不属于当前分类列表')
            verify_source_mapping(root, source)
    # Feed identity provides an independent check against samples.json identity.
    for sample in samples:
        if sample.get('source_mapping'):
            verify_source_mapping(root, sample)
            continue
        identities = {(p['handle'], str(p['id'])) for item in listings.values() for feed in item.get('feeds', []) for p in feed['products']}
        for item in listings.values():
            for source in item.get('card_sources', []):
                product = source['response'].get('product', source['response'])
                identities.add((product['handle'], str(product['id'])))
        if (sample['product']['handle'], str(sample['product']['id'])) not in identities:
            raise CrawlError('样本商品不属于分类源响应')
        raw = read_json(root / 'evidence' / ('product-' + str(sample['product']['id']) + '.json'))
        if raw.get('product', raw) != sample['product']:
            raise CrawlError('样本与保存的商品源响应不同')
    scope_path = root / 'sample_scope.json'
    if scope_path.is_file() and read_json(scope_path).get('detail_scope') == 'pending':
        raise CrawlError('只有列表完成，商品采集尚未执行')
    full = scope_path.is_file() and read_json(scope_path).get('detail_scope') == 'all_products'
    result = validate_samples(detail, samples, listings, rows, full=full, root=root)
    # result_clean.csv is required to be byte-for-byte the trusted normalized
    # export. Comparing streaming digests avoids parsing and retaining a second
    # 500 MB copy for stores with very large variant galleries.
    def file_digest(path):
        digest = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.digest()
    if file_digest(root / 'result_clean.csv') != file_digest(root / 'result.csv'):
        raise CrawlError('清洗结果与已验证的唯一变体行不一致')
    with (root / 'shopify_import.csv').open(encoding='utf-8-sig', newline='') as f:
        imported = list(csv.DictReader(f))
    import_metrics = _validate_shopify_import(root, imported, samples)
    result.update({'passed': True, 'categories': len(categories), 'sampled_categories': len(detail),
                   'seconds': round(time.perf_counter() - start, 4)})
    result.update(import_metrics)
    save_json(root / 'validation.json', result)
    return result
