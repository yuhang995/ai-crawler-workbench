"""Source-owned navigation trees. Labels stay arrays until JSON serialization."""
import re
import hashlib
from urllib.parse import urljoin, urlsplit

from .html_parser import parse_soup as BeautifulSoup

from .common import CrawlError
from .navigation_rules import normalize_url


def display(value):
    return ' '.join(str(value or '').split())


def serialized_label(value):
    # Commas belong to the legacy path format, never to an internal tree edge.
    return display(value.replace(',', ' ').replace('，', ' '))


def path_key(labels):
    labels = [serialized_label(label) for label in labels]
    return ','.join(' '.join(labels[:i + 1]) for i in range(len(labels)))


def marker(node):
    return ' '.join([node.get('id', ''), *node.get('class', [])])


def utility(node):
    return any(re.search(r'predictive|search-menu|search-drawer|cart-drawer', marker(p), re.I)
               or p.get('role') == 'search' or p.name in ('footer', 'template')
               for p in [node, *node.parents] if getattr(p, 'attrs', None) is not None)


def label(node):
    if node is None:
        return ''
    # Disclosure footer links repeat their own section's destination. Derive
    # their label from that section, never from generic navigation CTA text.
    if node.name == 'a' and 'main-nav__item--go' in node.get('class', []):
        for parent in node.parents:
            if parent.name == 'details':
                summary = parent.find('summary', recursive=False)
                owner = summary.find('a', href=True) if summary else None
                if owner is not None and owner.get('href') == node.get('href'):
                    return label(owner)
                break
    clone = BeautifulSoup(str(node), 'lxml')
    # Animated buttons paint a second copy of the same label. Only discard
    # the explicitly marked clone when its sibling supplies identical text.
    for duplicate in clone.select('.btn-duplicate'):
        value = display(duplicate.get_text(' ', strip=True))
        if value and any(sibling is not duplicate and 'btn-duplicate' not in sibling.get('class', [])
                         and display(sibling.get_text(' ', strip=True)) == value
                         for sibling in duplicate.parent.find_all(recursive=False)):
            duplicate.decompose()
    from .nav_extractor import is_label_noise
    container = clone.body.find(recursive=False) if clone.body else clone
    for el in list(container.find_all(True)):
        if el.attrs is not None and el.name not in ('a', 'button', 'summary') and is_label_noise(
                ' '.join(el.get('class', [])), el.get_text(' ', strip=True), el.get('role', '')):
            # A standalone category named "Sale"/"New" is not a badge.
            remainder = container.get_text(' ', strip=True).replace(el.get_text(' ', strip=True), '', 1).strip()
            if remainder or is_label_noise(' '.join(el.get('class', [])), '', el.get('role', '')):
                el.decompose()
    # aria-hidden controls accessibility, not visual display. Swatch labels
    # may be visibly painted while a shorter aria-label supplies spoken text.
    for el in clone.select('script,style,svg,.sr-only,.visually-hidden,[hidden],'
                           '[class*=description],[class*=__desc],[class*=badge],[role=meter]'):
        el.decompose()
    text = display(clone.get_text(' ', strip=True))
    # Use explicit short labels only when they are supported by visible text.
    for attr in ('data-label', 'segment-data-link-name'):
        preferred = display(node.get(attr))
        if preferred and text.casefold().startswith(preferred.casefold()):
            return preferred
    heading = clone.select_one('h1,h2,h3,h4,h5,h6,[role=heading],[class*=__title],[class*=link-text]')
    if heading is None and node.name == 'a' and node.find_parent(class_=re.compile(r'^nav-promo-card$')):
        titles = clone.select('span.font-bold')
        if len(titles) == 1:
            heading = titles[0]
    if heading:
        strong = heading.select('strong,b')
        if len(strong) == 1 and heading.find('br') is not None:
            return display(strong[0].get_text(' ', strip=True))
    if heading or text:
        return display(heading.get_text(' ', strip=True)) if heading else text
    # Image/card menus often use an absolutely positioned anchor whose only
    # accessible name is an sr-only span, while the same title is painted by a
    # sibling.  It is still an explicit label for that category anchor.  Keep
    # this fallback anchor-only so a generic "Toggle navigation" control never
    # becomes a taxonomy title.
    if node.name == 'a' and node.get('href'):
        accessible = display(node.get('aria-label') or node.get('title'))
        if not accessible:
            hidden = node.select_one('.sr-only,.visually-hidden')
            accessible = display(hidden.get_text(' ', strip=True)) if hidden else ''
        if accessible and len(accessible) < 100:
            return accessible
    return ''


def generic(value):
    value = re.sub(r'^[\W_]+|[\W_›»>→]+$', '', value).strip()
    return bool(re.fullmatch(r'(?:shop|view|see)\s+all(?:\s+.*)?|shop now|explore|discover|learn more', value, re.I))


def category(anchor, base):
    from .nav_extractor import is_blacklisted
    if not anchor or not anchor.get('href') or utility(anchor):
        return None
    target = normalize_url(urljoin(base, anchor['href']))
    p, origin = urlsplit(target), urlsplit(base)
    if p.scheme not in ('http', 'https') or is_blacklisted(target):
        return None
    # Only the conventional www alias; never move between regional subdomains.
    target_host = p.hostname or ''
    origin_host = origin.hostname or ''
    if target_host.startswith('www.'):
        target_host = target_host[4:]
    if origin_host.startswith('www.'):
        origin_host = origin_host[4:]
    if target_host != origin_host:
        return None
    return p._replace(scheme=origin.scheme, netloc=origin.netloc).geturl()


def _group(anchor, boundary):
    current = anchor
    while current is not boundary and current.parent is not None:
        parent = current.parent
        if parent.name == 'details':
            summary = parent.find('summary', recursive=False)
            if summary is not None and current is not summary:
                value = label(summary)
                if value and not generic(value):
                    return value
        for sibling in reversed(list(current.previous_siblings)):
            if not getattr(sibling, 'name', None):
                continue
            if (sibling.name in ('h2', 'h3', 'h4', 'h5', 'h6', 'dt') or sibling.get('role') == 'heading'
                    or re.search(r'(?:heading|headline|block-title)', marker(sibling), re.I)
                    or 'navlink--child' in sibling.get('class', [])):
                value = label(sibling)
                if value and not generic(value):
                    return value
        if parent.name == 'li' and parent is not boundary:
            owner = parent.find(['a', 'button', 'summary'], recursive=False)
            if owner is not None and owner is not anchor:
                value = label(owner)
                if value and not generic(value):
                    return value
        current = parent
    return ''


def _path_labels(name, title, group, is_control, target, root_url):
    labels = [name]
    if group and group.casefold() != name.casefold():
        labels.append(group)
    if not is_control and not generic(title):
        if title.casefold() != labels[-1].casefold() or (len(labels) == 1 and target != root_url):
            labels.append(title)
    return [name] if target == root_url else labels


def _state_group_tree(soup, html, base):
    """Explicit state-controlled panels outside nav; never evaluate page JS."""
    primary = [n for n in soup.select('header nav')
               if re.search(r'primary|main', n.get('aria-label', ''), re.I)
               and n.find_parent('header').select('[data-nav_group]')]
    if not primary:
        return None
    if len(primary) != 1:
        raise CrawlError('外置状态菜单缺少唯一主导航')
    nav = primary[0]
    header = nav.find_parent('header')
    def desktop(node):
        return not any(re.search(r'mobile|drawer', marker(p) + ' ' + p.get('x-ref', ''), re.I)
                       for p in [node, *node.parents] if getattr(p, 'attrs', None) is not None and p is not header)
    groups = [g for g in header.select('[data-nav_group]') if desktop(g)]
    nodes = soup.find_all(True)
    indices = {id(n): i for i, n in enumerate(nodes)}
    roots, eligible, covered, used, issues = [], set(), set(), set(), []
    for control in nav.select('a[href]'):
        root_url = category(control, base)
        if not root_url:
            continue
        name = label(control)
        handlers = ' '.join(str(v) for k, v in control.attrs.items() if k.startswith('data-crawl-event-'))
        ids = {m[1] for m in re.findall(r'\bsetActiveGroup\(\s*([\'"])([^\'"]+)\1\s*\)', handlers)}
        panels = [g for g in groups if g['data-nav_group'] in ids]
        if len(panels) > 1:
            raise CrawlError('外置状态菜单分组重复，无法确认桌面归属')
        if ids and len(panels) != 1:
            raise CrawlError('外置状态菜单缺少主入口对应面板')
        used.update(id(p) for p in panels)
        root = {'label': serialized_label(name), 'display_label': name, 'url': root_url,
                'source_node': indices[id(control)], 'owner_node': indices[id(control.parent)],
                'panel_nodes': [indices[id(p)] for p in panels], 'links': []}
        entries = {}
        anchors = [control] + [a for p in panels for a in p.select('a[href]')]
        for anchor in anchors:
            target = category(anchor, base)
            if not target:
                continue
            source = indices[id(anchor)]; eligible.add(source)
            title = label(anchor)
            title_source = anchor
            labels = [name]
            section = anchor.find_parent(attrs={'data-nav_megamenu': True}) if anchor is not control else None
            if section is not None:
                owner = section.find(['a', 'button'], recursive=False)
                if owner is None:
                    raise CrawlError('状态子菜单缺少直接主控件')
                section_title = label(owner)
                section_id = section['data-nav_megamenu']
                expression = r'\s*activeMegamenu\s*==\s*([\'"])' + re.escape(section_id) + r'\1\s*'
                panel = next((p for p in section.select('[x-show]') if re.fullmatch(expression, p['x-show'])), None)
                if anchor is not owner and (panel is None or not any(p is panel for p in anchor.parents)):
                    raise CrawlError('分类不属于状态控件声明的子面板')
                if anchor is not owner and (not title or generic(title)) and target != category(owner, base):
                    aliases = {}
                    for other in section.select('a[href]'):
                        if other is not anchor and category(other, base) == target:
                            value = label(other)
                            if value and not generic(value):
                                aliases.setdefault(value, other)
                    if len(aliases) == 1:
                        title, title_source = next(iter(aliases.items()))
                labels.append(section_title)
                if anchor is not owner and not generic(title) and title != section_title:
                    labels.append(title)
            elif anchor is not control and not generic(title) and title != name:
                labels.append(title)
            if target == root_url:
                labels = [name]
            if not title or not all(labels):
                issues.append('状态菜单分类缺少真实标题')
                continue
            key = path_key(labels)
            if key in entries and entries[key]['url'] != target:
                raise CrawlError('状态菜单同一路径对应不同分类: ' + key)
            entry = entries.setdefault(key, {'labels': [serialized_label(v) for v in labels],
                'display_labels': labels, 'url': target, 'model': False, 'source_nodes': [], 'label_sources': {}})
            entry['source_nodes'].append(source)
            entry['label_sources'][str(source)] = indices[id(title_source)]
            covered.add(source)
        root['links'] = list(entries.values())
        roots.append(root)
    if any(id(g) not in used and any(category(a, base) for a in g.select('a[href]')) for g in groups):
        raise CrawlError('状态面板存在没有主导航归属的分类')
    eligible.update(indices[id(a)] for a in nav.select('a[href]') if category(a, base))
    return {'version': 4, 'method': 'owned_menu_tree', 'layout': 'state_group_panels',
            'complete': not issues and eligible == covered, 'nav_node': indices[id(nav)],
            'source_html_sha256': hashlib.sha256(html.encode('utf-8')).hexdigest(),
            'drawer_trigger_node': None, 'roots': roots, 'issues': issues,
            'coverage': {'eligible': sorted(eligible), 'covered': sorted(covered)}}


def build_tree(html, base):
    """Return None for unsupported/ambiguous primary-menu structures.

    Hidden populated panels are usable only through their owning primary item.
    A separate mobile tree is never unioned into a selected desktop tree.
    """
    soup = BeautifulSoup(html, 'lxml')
    state_tree = _state_group_tree(soup, html, base)
    if state_tree is not None:
        return state_tree
    navs = []
    fallback_navs = []
    for nav in soup.select('header nav,header [role=navigation],nav'):
        if utility(nav) or nav.find_parent('nav'):
            continue
        ancestors = []
        for p in [nav, *nav.parents]:
            if p.name in ('header', 'body', 'html', '[document]'):
                break
            ancestors.append(p)
        ancestry = ' '.join(marker(p) for p in ancestors)
        if re.search(r'mobile|drawer|footer', ancestry, re.I):
            continue
        # Only an explicitly identified primary tree enters this path. Existing
        # theme readers continue handling unstructured and shared-drawer menus.
        explicit = bool(re.search(r'desktop|navigation|main menu|primary', ancestry + ' ' + nav.get('aria-label', ''), re.I))
        candidates = []
        # Disclosure themes put each primary item directly under nav instead
        # of a ul/li. ARIA targets belong to the component, not its anchor.
        components = nav.find_all('hover-disclosure', recursive=False)
        if components:
            items = [n for n in nav.children if getattr(n, 'name', None)
                     and n.find(['a', 'button', 'summary'], recursive=False)]
            candidates.append((nav, items))
        lists = [n for n in nav.select('ul,ol') if not any(
            p.name in ('ul', 'ol') for p in n.parents if p is not nav
            and any(a is nav for a in p.parents))]
        lists = [n for n in lists if any(category(a, base) for a in n.select('a[href]'))
                 or n.select_one('button[aria-controls],a[aria-controls]')]
        if not candidates and len(lists) == 1 and lists[0].find('li', recursive=False):
            candidates.append((nav, lists[0].find_all('li', recursive=False)))
        if explicit:
            navs.extend(candidates)
        if nav.find_parent('header') and candidates:
            def visible_link(a):
                return not any(p.has_attr('hidden') or p.get('aria-hidden') == 'true'
                               or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', p.get('style', ''), re.I)
                               or (p.name not in ('header', 'body', 'html', '[document]')
                                   and re.search(r'mobile|drawer|footer', marker(p), re.I))
                               for p in [a, *a.parents] if getattr(p, 'attrs', None) is not None)
            count = sum(visible_link(a) and bool(category(a, base)) for a in nav.select('a[href]'))
            if count:
                fallback_navs.append((count, candidates[0]))
    if len(navs) != 1 and fallback_navs:
        largest = max(count for count, _ in fallback_navs)
        # Ties remain ambiguous rather than silently selecting DOM order.
        navs = [candidate for count, candidate in fallback_navs if count == largest]
    drawer_trigger = None
    if not navs:
        # A shared desktop drawer is an authoritative menu only when linked
        # explicitly from the header, not merely because it exists in HTML.
        triggers = soup.select('header button[data-drawer-name="menu"]')
        drawers = soup.select('[data-drawer-names="menu"]')
        if len(triggers) == 1 and len(drawers) == 1:
            drawer_trigger, drawer = triggers[0], drawers[0]
            panel_ids = {i for c in drawer.select('[aria-controls]') for i in c['aria-controls'].split()}
            items = []
            for ul in drawer.select('ul,ol'):
                if any(p.name in ('ul', 'ol') or p.get('id') in panel_ids for p in ul.parents if p is not drawer):
                    continue
                children = ul.find_all('li', recursive=False)
                # CSS order is explicit evidence of display order.
                def display_order(n):
                    match = re.search(r'(?:^|;)\s*order\s*:\s*(-?\d+)', n.get('style', ''))
                    return int(match[1]) if match else 0
                items.extend(sorted(children, key=display_order))
            # Standalone promotional category cards are real visible entries.
            for a in drawer.select('a[href]'):
                if category(a, base) and not any(p.name == 'li' or p.get('id') in panel_ids for p in a.parents):
                    items.append(a)
            if items:
                navs.append((drawer, items))
    if len(navs) != 1:
        return None
    nav, items = navs[0]
    all_nodes = soup.find_all(True)
    indices = {id(n): i for i, n in enumerate(all_nodes)}
    roots, issues, covered = [], [], set()
    eligible = set()
    for item in items:
        control = item if item.name == 'a' else item.find(['a', 'button', 'summary'], recursive=False)
        if control is None:
            # A single direct wrapper may hold the primary control, while its
            # sibling holds the panel. Never search inside that panel for it.
            candidates = [c for child in item.find_all(['div', 'span', 'menu-dropdown'], recursive=False)
                          if child.get('role') != 'menu' and not child.has_attr('data-mega-menu')
                          for c in child.find_all(['a', 'button'], recursive=False)]
            if len(candidates) == 1:
                control = candidates[0]
        disclosure = item.find('details', recursive=False)
        if disclosure is None:
            wrapper=item.find('menu-dropdown',recursive=False)
            if wrapper is not None:disclosure=wrapper.find('details',recursive=False)
        if control is None and disclosure is not None:
            summary = disclosure.find('summary', recursive=False)
            if summary is not None:
                control = summary.find('a', href=True)
                if control is None:
                    control = summary
        if control is None:
            return None
        name = label(control)
        if not name:
            issues.append('主入口缺少显示名称')
            continue
        panels = []
        panel_ids = list(dict.fromkeys((control.get('aria-controls', '') + ' ' + item.get('aria-controls', '')).split()))
        for panel_id in panel_ids:
            matches = soup.find_all(id=panel_id)
            if len(matches) != 1 or not matches[0].select('a[href]'):
                issues.append('关联面板缺失或为空: ' + name)
            else:
                panels.append(matches[0])
        explicit_expand = disclosure is not None or control.has_attr('aria-expanded') or control.has_attr('data-nav-menu-toggle') or item.has_attr('aria-expanded')
        if not panels:
            panels = [item]
            if explicit_expand and not [a for a in item.select('a[href]') if a is not control]:
                issues.append('子菜单尚未加载: ' + name)
        anchors = [control] if control.name == 'a' else []
        for panel in panels:
            anchors.extend(panel.select('a[href]'))
        anchors = list({id(a): a for a in anchors}.values())
        root_url = category(control, base)
        if root_url is None:
            def owns_root(a):
                boundary = next((p for p in panels if p is a or any(p is parent for parent in a.parents)), item)
                group = _group(a, boundary)
                return not group or group.casefold() == name.casefold()
            def explicit_root_cta(a):
                return re.sub(r'[^\w]+',' ',label(a).casefold()).strip() == 'shop all '+re.sub(r'[^\w]+',' ',name.casefold()).strip()
            all_urls = {category(a, base) for a in anchors if category(a, base) and
                        (explicit_root_cta(a) or (generic(label(a)) and owns_root(a)))}
            if len(all_urls) == 1:
                root_url = next(iter(all_urls))
        root = {'label': serialized_label(name), 'display_label': name,
                'url': root_url, 'links': [], 'source_node': indices[id(control)],
                'owner_node': indices[id(item)], 'panel_nodes': [indices[id(p)] for p in panels]}
        seen = {}
        for anchor in anchors:
            target = category(anchor, base)
            if not target:
                continue
            source = indices[id(anchor)]; eligible.add(source)
            title = label(anchor)
            label_source = anchor
            if not title:
                # An image-only alias may borrow a name from an observed link
                # to the exact same URL. Never use descriptive image alt text
                # or a guessed slug as a category title.
                for pool in (anchors, nav.select('a[href]')):
                    alternatives = {}
                    for other in pool:
                        if category(other, base) == target:
                            value = label(other)
                            if value and not generic(value):
                                alternatives.setdefault(value.casefold(), (value, other))
                    if len(alternatives) == 1:
                        title, label_source = next(iter(alternatives.values()))
                        break
                    if alternatives:
                        break
            if not title:
                issues.append('分类缺少独立标题: ' + target)
                continue
            boundary = next((p for p in panels if p is anchor or any(p is a for a in anchor.parents)), item)
            group = _group(anchor, boundary) if anchor is not control else ''
            # Every exact-URL alias of the root (Shop All buttons, image
            # promos, announcement copy) is another control for that same
            # taxonomy node, never a child category with a promotional name.
            labels = _path_labels(name, title, group, anchor is control, target, root['url'])
            safe = [serialized_label(v) for v in labels]
            key = path_key(labels)
            if key in seen and seen[key]['url'] != target:
                issues.append('同一路径对应不同 URL: ' + key)
                continue
            if key not in seen:
                entry = {'labels': safe, 'display_labels': labels, 'url': target,
                         'order': [len(root['links'])], 'model': False, 'source_nodes': [], 'label_sources': {}}
                seen[key] = entry; root['links'].append(entry)
            seen[key]['source_nodes'].append(source); covered.add(source)
            seen[key]['label_sources'][str(source)] = indices[id(label_source)]
        if root['links']:
            roots.append(root)
    # Every category anchor in the chosen primary tree must have an owner.
    eligible.update(indices[id(a)] for a in nav.select('a[href]') if category(a, base))
    for trigger in nav.select('[aria-controls]'):
        for target_id in trigger['aria-controls'].split():
            matches = soup.find_all(id=target_id)
            if len(matches) != 1 or not matches[0].select('a[href]'):
                issues.append('嵌套菜单面板缺失、重复或为空: ' + target_id)
    if eligible - covered:
        issues.append('主导航存在未归属的分类链接')
    if not roots:
        return None
    return {'version': 4, 'method': 'owned_menu_tree', 'complete': not issues, 'nav_node': indices[id(nav)],
            'source_html_sha256': hashlib.sha256(html.encode('utf-8')).hexdigest(),
            'drawer_trigger_node': indices[id(drawer_trigger)] if drawer_trigger is not None else None,
            'roots': roots, 'issues': issues, 'coverage': {'eligible': sorted(eligible), 'covered': sorted(covered)}}


def tree_data(tree):
    result = {}
    for root in tree['roots']:
        # Root entries precede children even when the Shop All link is last.
        for entry in sorted(root['links'], key=lambda e: len(e['display_labels']) != 1):
            key = path_key(entry['display_labels'])
            if key in result and result[key] != entry['url']:
                raise CrawlError('目录路径冲突: ' + key)
            result[key] = entry['url']
    return result


def verify_sources(tree, html, base):
    """Verify recorded ownership against raw DOM, not only the output mapping."""
    nodes = BeautifulSoup(html, 'lxml').find_all(True)
    if tree.get('source_html_sha256') and tree['source_html_sha256'] != hashlib.sha256(html.encode('utf-8')).hexdigest():
        raise CrawlError('菜单树来源快照不匹配')
    rebuilt = build_tree(html, base)
    def ownership(value):
        return [(r['label'], r['source_node'], r['owner_node'], r['panel_nodes'], r['url']) for r in value['roots']]
    if (not rebuilt or not rebuilt['complete'] or rebuilt['nav_node'] != tree.get('nav_node')
            or ownership(rebuilt) != ownership(tree)):
        raise CrawlError('主导航选择或根节点归属与源 DOM 不符')
    if rebuilt.get('layout') == 'state_group_panels':
        if tree != rebuilt:
            raise CrawlError('外置状态菜单的归属、标题或覆盖与原始 DOM 不符')
        return
    if tree.get('coverage', {}).get('eligible') != tree.get('coverage', {}).get('covered'):
        raise CrawlError('目录树覆盖不完整')
    try:
        indices = {id(node): i for i, node in enumerate(nodes)}
        eligible = {indices[id(a)] for a in nodes[tree['nav_node']].select('a[href]') if category(a, base)}
        covered = set()
        for root in tree['roots']:
            control = nodes[root['source_node']]
            if serialized_label(label(control)) != root['label']:
                raise CrawlError('一级目录与主入口名称不符')
            panels = [nodes[i] for i in root['panel_nodes']]
            eligible.update(indices[id(a)] for p in panels for a in p.select('a[href]') if category(a, base))
            for entry in root['links']:
                if not entry['source_nodes']:
                    raise CrawlError('目录条目没有源节点')
                for i in entry['source_nodes']:
                    anchor = nodes[i]
                    if category(anchor, base) != entry['url']:
                        raise CrawlError('目录 URL 与源链接不符')
                    if anchor is not control and not any(any(p is a for a in anchor.parents) for p in panels):
                        raise CrawlError('分类链接不属于声明的主菜单面板')
                    label_node = nodes[entry.get('label_sources', {}).get(str(i), i)]
                    if label_node is not anchor:
                        if label(anchor) or category(label_node, base) != entry['url']:
                            raise CrawlError('无文字卡片标题没有同 URL 源依据')
                        if not any(p is nodes[tree['nav_node']] for p in label_node.parents):
                            raise CrawlError('借用的分类标题不在主导航中')
                    title = label(label_node)
                    boundary = next((p for p in panels if p is anchor or any(p is a for a in anchor.parents)), None)
                    group = _group(anchor, boundary) if anchor is not control else ''
                    labels = _path_labels(label(control), title, group, anchor is control, entry['url'], root['url'])
                    if entry['labels'] != [serialized_label(v) for v in labels]:
                        raise CrawlError('目录层级或标题与源节点不符')
                    covered.add(i)
        if covered != eligible or sorted(covered) != tree['coverage']['covered']:
            raise CrawlError('源菜单中存在遗漏或重复归属的分类链接')
    except (IndexError, KeyError, TypeError) as e:
        raise CrawlError('菜单树来源记录无效') from e
