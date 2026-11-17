"""Pack/Hydrogen desktop menus render the active column from a hydrated tree."""
import re
from urllib.parse import urljoin, urlsplit

from .common import CrawlError
from .html_parser import parse_soup
from .menu_tree import path_key
from .navigation_rules import home_key
from .shopify_fastpath import react_router_payloads


def has_pack_menu_data(html):
    return all(mark in html for mark in ('streamController.enqueue', 'siteSettings', 'navItems'))


def pack_navigation(html, base):
    """Read the published header menu only when its root bar matches the DOM."""
    from .simple_nav import _homepage_desktop_hidden, legal_url

    if not has_pack_menu_data(html):
        return None
    for payload in react_router_payloads(html):
        value = payload
        for name in ('loaderData', 'root', 'siteSettings', 'data', 'siteSettings',
                     'settings', 'header', 'menu', 'navItems'):
            value = value.get(name) if isinstance(value, dict) else None
        if isinstance(value, list) and value:
            items = value
            break
    else:
        return None

    def target(link):
        raw = link.get('url')
        return urljoin(base, raw) if isinstance(raw, str) and raw.strip() else ''

    def collection(url):
        locale = re.match(r'^/([a-z]{2}-[a-z]{2})(?:/|$)', urlsplit(url).path, re.I)
        return (legal_url(url, base)
                and (not locale or urlsplit(base).path.startswith('/' + locale[1])))

    # The same settings object also contains footer/mobile/slider links. Bind
    # the chosen tree to a real desktop bar before trusting nested metadata.
    soup = parse_soup(html)
    declared = [(item['navItem'].get('text'), target(item['navItem'])) for item in items
                if isinstance(item, dict) and isinstance(item.get('navItem'), dict)]
    if len(declared) != len(items) or not all(label for label, _ in declared):
        return None
    for nav in soup.select('header nav'):
        controls = nav.select(':scope > ul > li > [aria-label]')
        observed = [(el['aria-label'], urljoin(base, el['href']) if el.get('href') else '')
                    for el in controls]
        if observed == declared:
            break
    else:
        return None

    categories = {}

    def record(parents, link):
        label = (link or {}).get('text')
        url = target(link or {})
        if not label or not url or not collection(url):
            return
        labels = [*parents, label]
        # A merchant may display the parent prefix in the child label itself
        # (Shop By > Shop By Color). Store that prefix exactly once.
        parts = [labels[0]]
        for parent, child in zip(labels, labels[1:]):
            parts.append(child.removeprefix(parent + ' '))
        key = path_key(parts)
        if key in categories and categories[key] != url:
            raise CrawlError('同一菜单名称指向不同分类: ' + key)
        categories[key] = url

    for item in items:
        root = item['navItem']
        record([], root)
        for group in item.get('links') or []:
            link = group.get('link') or {}
            if not link.get('text'):
                continue
            record([root['text']], link)
            for leaf in group.get('sublinks') or []:
                record([root['text'], link['text']], leaf.get('link'))
        # A featured collection may be unique (e.g. the Shop By promotion).
        # Identical Shop All CTAs already belong to their root/group.
        promo = (item.get('featuredImage') or {}).get('link') or {}
        if target(promo) != target(root):
            record([root['text']], promo)

    if not categories:
        return None
    menu_urls = set(categories.values())
    for anchor in soup.select('main a[href]'):
        url = urljoin(base, anchor['href'])
        if (url not in menu_urls and collection(url)
                and not _homepage_desktop_hidden(anchor)):
            key = home_key(url, anchor.get_text(' ', strip=True))
            if key and key not in categories:
                categories[key] = url
    return categories
