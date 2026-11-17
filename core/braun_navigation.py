"""Braun's Next.js header uses linked, typed CMS entries for its drawer."""
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .common import Client, CrawlError, save_json
from .html_parser import parse_soup
from .menu_tree import path_key
from .navigation_rules import home_key, is_generic_action


def _next_data(html):
    node = parse_soup(html).select_one('script#__NEXT_DATA__[type="application/json"]')
    try:
        data = json.loads(node.string) if node and node.string else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_mia_storefront(html):
    if '__NEXT_DATA__' not in html or 'miaHeader' not in html:
        return False
    props = _next_data(html).get('props', {}).get('pageProps', {})
    header = props.get('content', {}).get('headerMenu', {}).get('fields', {})
    return (str(props.get('settings', {}).get('siteName')).casefold() == 'braun'
            and header.get('contentType') == 'miaHeader' and bool(header.get('menuItems')))


def _text(node):
    if isinstance(node, str):
        return ' '.join(node.split())
    if not isinstance(node, dict):
        return ''
    if node.get('nodeType') == 'text':
        return node.get('value', '')
    return ''.join(_text(child) for child in node.get('content', [])).strip()


def _components(items):
    for item in items:
        component = item.get('fields', {})
        yield component
        yield from _components(component.get('sectionContent', []))


def _sells_products(html):
    page = _next_data(html).get('props', {}).get('pageProps', {}).get('content', {}).get('page', {})
    # Examine components placed on this page. The shared header, footer and
    # linked Contentful entries can themselves contain unrelated products.
    for component in _components(page.get('pageContent', [])):
        if component.get('contentType') in {'miaProductShelf', 'shelf007'}:
            if any(p.get('fields', {}).get('contentType') == 'product'
                   and p['fields'].get('sku') and p['fields'].get('internalLink')
                   for p in component.get('products', [])):
                return True
    return False


def _site_url(base, prefix, href):
    parsed = urlsplit(urljoin(base, href or ''))
    if (not href or parsed.scheme != urlsplit(base).scheme or parsed.netloc != urlsplit(base).netloc
            or parsed.username is not None or not parsed.path.strip('/')):
        return ''
    path = parsed.path
    if prefix and not (path == prefix or path.startswith(prefix + '/')):
        if re.match(r'^/[a-z]{2}-[a-z]{2}(?:/|$)', path, re.I):
            return ''
        path = prefix + path
    return parsed._replace(path=path).geturl()


def mia_navigation(html, base, *, client=None, evidence_dir=None):
    data = _next_data(html)
    content = data.get('props', {}).get('pageProps', {}).get('content', {})
    header = content.get('headerMenu', {}).get('fields', {})
    prefix = str(data.get('assetPrefix') or '').rstrip('/')
    if not re.fullmatch(r'/[a-z]{2}-[a-z]{2}', prefix, re.I):
        prefix = ''
    client = client or Client()
    categories, landings = {}, {}
    dest = Path(evidence_dir) if evidence_dir is not None else None

    def record(labels, target):
        kind = target.get('contentType')
        if kind not in {'miaCategoryPage', 'contentPage'}:
            return
        url = _site_url(base, prefix, target.get('internalLink'))
        if not url:
            return
        if kind == 'contentPage':
            if url not in landings:
                try:
                    response = client.get(url)
                    is_catalog = response.url.rstrip('/') == url.rstrip('/') and _sells_products(response.text)
                    landings[url] = {'has_product_shelf': is_catalog}
                    if is_catalog and dest is not None:
                        name = f'navigation-landing-{len(landings):02d}.html'
                        (dest / name).write_text(response.text, encoding='utf-8')
                        landings[url]['file'] = name
                except CrawlError as error:
                    landings[url] = {'has_product_shelf': False, 'error': str(error)}
            if not landings[url]['has_product_shelf']:
                return
        key = path_key([value for value in labels if value])
        if not key:
            return
        if key in categories and categories[key] != url:
            raise CrawlError('Braun 菜单同一分类名指向不同目标: ' + key)
        categories[key] = url

    if dest is not None:
        dest.mkdir(parents=True, exist_ok=True)
    for node in header.get('menuItems', []):
        root = node['fields']
        title = _text(root.get('menuText')) + _text(root.get('highlightedText'))
        if not title:
            continue
        record([title], root.get('page', {}).get('fields', {}))
        links = [item['fields'] for item in root.get('links', [])]
        if not any(link.get('internalLink', {}).get('fields', {}).get('contentType')
                   == 'miaCategoryPage' for link in links):
            continue
        group = _text(root.get('additionalMenuCardsTitle'))
        for link in links:
            label = _text(link.get('text'))
            if not label:
                continue
            labels = [title] if is_generic_action(label) else [title, group, label]
            record(labels, link.get('internalLink', {}).get('fields', {}))
        group = _text(root.get('menuCardText'))
        for item in root.get('menuCards', []):
            card = item['fields']
            label = _text(card.get('title'))
            if label:
                for cta in card.get('callToActions', []):
                    record([title, group, label],
                           cta['fields'].get('internalLink', {}).get('fields', {}))

    # Homepage-only collections are owned by the category shelf's displayed
    # cards. Do not scan the embedded site settings, newsletter or footer.
    for module in _components(content.get('page', {}).get('pageContent', [])):
        if module.get('contentType') != 'miaCategoryShelf':
            continue
        for group in module.get('categories', []):
            fields = group['fields']
            for card in fields.get('bigCards', []) + fields.get('smallCards', []):
                fields_card = card['fields']
                target = fields_card.get('internalLink', {}).get('fields', {})
                url = _site_url(base, prefix, target.get('internalLink'))
                if url and url not in categories.values():
                    record([home_key(url, _text(fields_card.get('title')))], target)
    if dest is not None:
        save_json(dest / 'navigation-landing-pages.json', landings)
    if not categories:
        raise CrawlError('Braun 首页主菜单没有可验证的商品分类')
    return categories
