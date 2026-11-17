"""Independent navigation evidence and a fail-closed publication gate.

The browser observes visible controls/links; it never asks an LLM to invent scope.
"""
import json
import re
import time
from urllib.parse import urljoin, urlsplit
from .html_parser import parse_soup as BeautifulSoup
from .common import CrawlError
from .navigation_rules import normalize_url, text, homepage_entries, clean_soup

VERSION = 4

def stable_page_content(page, attempts=6):
    """Read rendered HTML across short client-side redirects without accepting a partial page."""
    last_error = None
    for attempt in range(attempts):
        try:
            return page.content()
        except Exception as error:
            last_error = error
            if 'navigat' not in str(error).casefold() or attempt + 1 == attempts:
                raise
            try:
                page.wait_for_load_state('domcontentloaded', timeout=2500)
            except Exception:
                pass
            page.wait_for_timeout(350)
    raise last_error

def is_access_challenge(html):
    """Distinguish an interstitial from an empty/unsupported storefront menu."""
    soup=BeautifulSoup(html,'lxml')
    title=soup.title.get_text(' ',strip=True).casefold() if soup.title else ''
    return title in {'just a moment...', 'just a moment', '请稍候', '请稍候…', 'attention required! | cloudflare'} or bool(soup.select_one('#challenge-running,#challenge-stage,form#challenge-form'))

def is_market_unavailable(html):
    text_value = ' '.join(BeautifulSoup(html, 'lxml').get_text(' ', strip=True).split()).casefold()
    return any(message in text_value for message in (
        "isn’t available in your region", "isn't available in your region",
        'not available in your region', 'not available in your country'))

def wait_for_storefront(page, attempts=6):
    for attempt in range(attempts+1):
        html = stable_page_content(page)
        if is_market_unavailable(html):
            raise CrawlError('market_unavailable: 当前浏览器出口地区无法访问目标市场页面')
        if not is_access_challenge(html):return
        if attempt<attempts:page.wait_for_timeout(2000)
    raise CrawlError('网站访问验证未通过（Cloudflare/人机验证），浏览器尚未取得真实首页；无法核对菜单，未生成目录或运行包')


def labels_from_key(key):
    levels=key.split(',')
    labels=[levels[0]]
    for parent,child in zip(levels,levels[1:]):
        if not child.startswith(parent+' '):raise CrawlError('目录父级前缀不一致: '+key)
        labels.append(child[len(parent)+1:])
    return labels

def static_evidence(html, base):
    """Only simple semantic nested lists can be certified without rendering."""
    soup=clean_soup(html)
    navs=soup.select('nav,[role=navigation]')
    if len(navs)!=1 or soup.select_one('template,[aria-controls],[aria-haspopup],header button'):
        return None
    nav=navs[0]
    roots=[]
    for item in nav.find_all('li'):
        if item.find_parent('li'):continue
        anchor=item.find('a',recursive=False)
        if anchor is None:return None
        label=text(anchor)
        links=[]
        for a in item.select('a[href]'):
            path=[]
            for li in reversed(list(a.find_parents('li'))):
                own=li.find('a',recursive=False)
                if own is None:return None
                path.append(text(own))
            if not path or path[-1]!=text(a):return None
            links.append({'labels':path,'url':normalize_url(urljoin(base,a['href']))})
        roots.append({'label':label,'url':normalize_url(urljoin(base,anchor['href'])),'links':links})
    return {'version':VERSION,'method':'semantic_static','roots':roots,'complete':True} if roots else None


_GENERIC_MENU_TEXT = {'view all', 'shop all', 'see all', 'all'}
_PANEL_MARKER = re.compile(r'(?:mega[-_ ]?menu|submenu|subnav|dropdown|flyout|drawer|menu[-_ ]?panel)', re.I)


def _static_label(node):
    if node is None:
        return ''
    preferred = node.select_one(
        'h1,h2,h3,h4,h5,h6,[role=heading],[class*="link-label" i],'
        '[class*="item-label" i],[class*="card-title" i],[class*="menu-item-text" i]'
    )
    value = text(preferred or node) or node.get('aria-label', '') or node.get('title', '')
    value = re.sub(r'\s+', ' ', value).strip()
    return value if value and ',' not in value and len(value) < 80 else ''


def _static_category_url(anchor, base):
    """Normalize a category link already observed in primary navigation DOM."""
    from .nav_extractor import is_blacklisted
    if anchor is None or not anchor.get('href'):
        return None
    target = normalize_url(urljoin(base, anchor['href'].strip()))
    if urlsplit(target).hostname != urlsplit(base).hostname or is_blacklisted(target):
        return None
    return target


def _semantic_scopes(soup):
    def excluded(node):
        for current in [node, *list(node.parents)]:
            if getattr(current, 'attrs', None) is None:
                continue
            if current.name in {'footer','main-search','output','dialog'}:
                return True
            marker = (str(current.get('id', '')) + ' ' + ' '.join(current.get('class', []))
                      + ' ' + str(current.get('aria-label', '')))
            if re.search(r'(?:^|[-_ ])(?:mobile|drawer|proxy|footer|secondary|utility|search|predictive)(?:$|[-_ ])', marker, re.I):
                return True
            if current.get('role', '').lower() in {'search','dialog'}:
                return True
        return False
    scopes = [node for node in soup.select('header nav,header [role=navigation]')
              if not excluded(node)]
    if not scopes:
        scopes = [node for node in soup.select('nav,[role=navigation]')
                  if not excluded(node)]
    # Nested navigation nodes represent the same primary tree.
    return [node for node in scopes if not any(parent in scopes for parent in node.parents)]


def _primary_control_labels(soup):
    labels, urls = set(), set()
    for scope in _semantic_scopes(soup):
        for control in scope.select('a[href],button,summary'):
            if control.find_parent('footer') is not None:
                continue
            nested = False
            for parent in control.parents:
                if parent is scope:
                    break
                marker = ' '.join(parent.get('class', [])) + ' ' + parent.get('id', '')
                if parent.get('role') == 'menu' or _PANEL_MARKER.search(marker):
                    nested = True
                    break
                if parent.name == 'li' and parent.find_parent('li') is not None:
                    nested = True
                    break
            if nested:
                continue
            label = _static_label(control)
            if label:
                labels.add(label.casefold())
            target = _static_category_url(control, 'https://static.invalid/')
            if target:
                urls.add(urlsplit(target).path)
    return labels, urls


def _column_heading(anchor, boundary):
    """Closest visible semantic heading which groups ``anchor`` in a panel."""
    current = anchor
    while current is not None and current is not boundary:
        parent = current.parent
        if parent is None:
            break
        preceding = []
        for sibling in parent.children:
            if not getattr(sibling, 'name', None):
                continue
            if sibling is current or (hasattr(sibling, '__contains__') and current in sibling.descendants):
                break
            preceding.append(sibling)
        for sibling in reversed(preceding):
            marker = ' '.join(sibling.get('class', []))
            if (sibling.name in {'h1','h2','h3','h4','h5','h6'} or
                    sibling.get('role') == 'heading' or
                    re.search(r'(?:^|[-_ ])(?:heading|title)(?:$|[-_ ])', marker, re.I)):
                value = _static_label(sibling)
                if value and value.casefold() not in _GENERIC_MENU_TEXT:
                    return value
        current = parent
    return ''


def _associated_panels(control, soup, include_owner=True):
    panels = []
    for panel_id in re.split(r'\s+', control.get('aria-controls', '').strip()):
        if panel_id:
            panel = soup.find(id=panel_id)
            if panel is not None:
                panels.append(panel)
    details = control.find_parent('details')
    if details is not None:
        panels.append(details)
    if include_owner:
        owner = control.find_parent('li')
        if owner is None:
            parent = control.parent
            if parent is not None and parent.name not in {'nav','header','body','html'}:
                owner = parent
        if owner is not None and owner.select_one('a[href]'):
            panels.append(owner)
    for attr, raw in control.attrs.items():
        if not (attr.startswith('data-') and re.search(r'menu|nav|drawer|target|trigger|handle', attr, re.I)):
            continue
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            value = str(value).strip()
            if not value or value.casefold() in {'true','false'}:
                continue
            panel = soup.find(id=value)
            if panel is not None:
                panels.append(panel)
            panels.extend(soup.find_all(attrs={'data-menu-handle': value}))
    result, seen = [], set()
    for panel in panels:
        if id(panel) not in seen:
            seen.add(id(panel));result.append(panel)
    return result


def _controlled_dom_roots(html, base, explicit_only=False):
    """Read aria/details/mega-menu associations without applying visibility."""
    soup = BeautifulSoup(html, 'lxml')
    roots = []
    semantic_scopes = _semantic_scopes(soup)
    for scope in semantic_scopes:
        for control in scope.select('a[href],button,summary'):
            # Only first-tier controls own roots.  Descendants in panels are
            # consumed with their owning control below.
            details = control.find_parent('details')
            if details is not None and control.name != 'summary':
                continue
            if any(_PANEL_MARKER.search(' '.join(parent.get('class', [])) + ' ' + parent.get('id', ''))
                   or parent.get('role') == 'menu'
                   for parent in control.parents if parent is not scope):
                continue
            item = control.find_parent('li')
            if item is not None and item.find_parent('li') is not None:
                continue
            label = _static_label(control)
            if not label or label.isdigit() or re.search(
                    r'search|account|cart|bag|wishlist|country|currency|region|language|cookie|log.?in|sign.?in|translation missing|open close',
                    label, re.I):
                continue
            own_url = _static_category_url(control, base)
            panels = _associated_panels(control, soup, include_owner=not explicit_only)
            if explicit_only:
                panels = [panel for panel in panels
                          if not any(panel is semantic or any(parent is semantic for parent in panel.parents)
                                     for semantic in semantic_scopes)]
            if explicit_only and not panels:
                continue
            panel_anchors = []
            for panel in panels:
                panel_anchors.extend(panel.select('a[href]'))
            if not own_url and not panel_anchors:
                continue
            links, seen = [], set()
            if own_url:
                links.append({'labels': [label], 'url': own_url, 'order': [0], 'model': False})
                seen.add(((label,), own_url))
            order = 1
            for anchor in panel_anchors:
                if anchor is control or control in anchor.parents:
                    continue
                target = _static_category_url(anchor, base)
                if not target:
                    continue
                child = _static_label(anchor)
                group = _column_heading(anchor, next((p for p in panels if anchor in p.descendants), None))
                # A nested category owner is a stronger group than a decorative
                # column heading.
                for parent in anchor.parents:
                    if parent in panels:
                        break
                    owner = parent.find(['button','summary'], recursive=False)
                    if owner is None:
                        owner = parent.select_one(':scope > a[aria-controls],:scope > a[aria-haspopup],:scope > a[aria-expanded]')
                    if owner is not None and owner is not anchor:
                        owner_label = _static_label(owner)
                        if owner_label and owner_label.casefold() not in _GENERIC_MENU_TEXT:
                            group = owner_label
                            break
                if child.casefold() in _GENERIC_MENU_TEXT:
                    if group:
                        labels = [label, group]
                    else:
                        labels = [label]
                else:
                    labels = [label] + ([group] if group and group.casefold() != label.casefold() else []) + [child]
                    if len(labels) > 3:
                        labels = labels[:2] + [labels[-1]]
                if len(labels) == 2 and labels[-1].casefold() == label.casefold():
                    labels = [label]
                identity = (tuple(labels), target)
                if identity in seen:
                    continue
                links.append({'labels': labels, 'url': target, 'order': [order], 'model': False})
                seen.add(identity);order += 1
            if links:
                top_item = control.find_parent('li')
                direct_primary = bool(
                    control.parent is scope
                    or (top_item is not None and top_item.find_parent('li') is None)
                    or (control.name == 'summary' and control.find_parent('details') is not None
                        and control.find_parent('details').find_parent('li') is None)
                )
                roots.append({'label': label, 'url': own_url, 'links': links,
                              'source': 'dom_tree_static', 'direct_primary': direct_primary})
    return roots


def _analysis_dom_roots(analysis, html, base):
    """Convert deterministic extractor output to independent evidence roots."""
    metadata = getattr(analysis, 'metadata', {}) or {}
    entries = metadata.get('entries') or []
    primary_roots = metadata.get('primary_roots') or []
    normalized = []
    if entries and primary_roots:
        for index, entry in enumerate(entries):
            if entry.get('source') == 'home':
                continue
            labels = list(entry.get('labels') or [])
            if not labels:
                try:
                    labels = labels_from_key(entry['key'])
                except Exception:
                    continue
            if labels[-1].casefold() in _GENERIC_MENU_TEXT:
                labels = labels[:-1]
                if not labels:
                    continue
            normalized.append((labels, entry['url'], index, entry.get('source') == 'menu_model'))
    else:
        soup = clean_soup(html)
        try:
            home_pairs = {(entry['key'], entry['url']) for entry in homepage_entries(soup, base, [])}
        except Exception:
            home_pairs = set()
        top_labels, _ = _primary_control_labels(soup)
        for index, (key, url) in enumerate(getattr(analysis, 'data', {}).items()):
            try:
                labels = labels_from_key(key)
            except Exception:
                continue
            if labels[-1].casefold() in _GENERIC_MENU_TEXT:
                labels = labels[:-1]
                if not labels:
                    continue
            if (key, url) in home_pairs and labels[0].casefold() not in top_labels:
                continue
            if top_labels and labels[0].casefold() not in top_labels:
                continue
            normalized.append((labels, url, index, False))
        primary_roots = list(dict.fromkeys(labels[0] for labels, _, _, _ in normalized))

    grouped = {root.casefold(): {'label': root, 'url': None, 'links': [], 'source': 'dom_tree_static'}
               for root in primary_roots if root}
    for labels, url, index, model in normalized:
        if not labels:
            continue
        root = grouped.setdefault(labels[0].casefold(),
                                  {'label': labels[0], 'url': None, 'links': [], 'source': 'dom_tree_static'})
        item = {'labels': labels[:3], 'url': normalize_url(url), 'order': [index], 'model': model}
        if not any(existing['labels'] == item['labels'] and existing['url'] == item['url'] for existing in root['links']):
            root['links'].append(item)
        if len(labels) == 1:
            root['url'] = item['url']
    return [root for root in grouped.values() if root['links']]


def dom_tree_static_evidence(html: str, base_url: str, analysis=None):
    """Build authoritative v4 evidence from source DOM, including hidden menus.

    Visibility attributes and CSS do not invalidate semantic ownership.  The
    function returns ``None`` only when the source is empty/ambiguous or an
    expansion control is an actual JS-populated shell.
    """
    from .nav_extractor import extract_navigation, needs_dynamic, NavigationAnalysis
    try:
        analysis = analysis or extract_navigation(html, base_url)
    except Exception:
        return None
    owned = (getattr(analysis, 'metadata', {}) or {}).get('owned_tree')
    if owned is not None:
        return owned if owned['complete'] and not analysis.warnings else None
    roots = _analysis_dom_roots(analysis, html, base_url)
    controlled = _controlled_dom_roots(html, base_url, explicit_only=True)
    by_label = {root['label'].casefold(): root for root in roots}
    added_entries = []
    for extra in controlled:
        current = by_label.get(extra['label'].casefold())
        if current is None:
            if roots and (not extra.get('direct_primary') or re.search(
                    r'navigation|translation missing|open close|language|country|currency', extra['label'], re.I)):
                continue
            roots.append(extra);by_label[extra['label'].casefold()] = extra
            added_entries.extend((extra, entry) for entry in extra['links'])
            continue
        existing = {(tuple(entry['labels']), entry['url']) for entry in current['links']}
        existing_urls = {entry['url'] for entry in current['links']}
        for entry in extra['links']:
            identity = (tuple(entry['labels']), entry['url'])
            if identity not in existing and entry['url'] not in existing_urls:
                current['links'].append(entry);existing.add(identity);existing_urls.add(entry['url'])
                added_entries.append((current, entry))
        if current.get('url') is None and extra.get('url'):
            current['url'] = extra['url']
    if not roots:
        return None
    candidate = {}
    from .nav_extractor import _hierarchical_key
    for root in roots:
        for entry in root['links']:
            candidate[_hierarchical_key(entry['labels'])] = entry['url']
    warnings = [warning for warning in (getattr(analysis, 'warnings', []) or [])
                if not re.search(r'未找到\s*(?:nav|navigation|menu)|未找到可识别的.*(?:集合|分类)链接', warning, re.I)]
    synthetic = NavigationAnalysis(base_url=base_url, roots=[], data=candidate,
                                   warnings=warnings)
    if needs_dynamic(html, synthetic):
        return None
    proof = {'version': VERSION, 'method': 'dom_tree_static', 'complete': True, 'roots': roots}
    try:
        roundtrip = from_evidence(proof, html, base_url).data
    except Exception:
        return None
    # Explicitly bound outside panels may add only their observed paths (and
    # the structural two-level prefix of an observed three-level leaf).
    expected = dict(getattr(analysis, 'data', {}))
    from .nav_extractor import _hierarchical_key
    for root, entry in added_entries:
        additions = [(entry['labels'], entry['url'])]
        if len(entry['labels']) == 3:
            group_labels = entry['labels'][:2]
            group_url = next((item['url'] for item in root['links']
                              if item['labels'] == group_labels), root.get('url'))
            if group_url:
                additions.insert(0, (group_labels, group_url))
        for labels, url in additions:
            key = _hierarchical_key(labels)
            if key in expected and expected[key] != url:
                return None
            expected[key] = url
    if roundtrip != expected:
        return None
    return proof

def linked_static_evidence(html, base):
    """Certify desktop menus whose triggers and panels are explicitly linked in source HTML."""
    from .nav_extractor import is_blacklisted
    soup = BeautifulSoup(html, 'lxml')
    base_host = urlsplit(base).hostname

    def markers(node):
        values = []
        for parent in [node, *list(node.parents)]:
            if getattr(parent, 'attrs', None) is None:
                continue
            values.append(str(parent.get('id', '')))
            values.extend(parent.get('class', []))
            if parent.name == 'header':
                break
        return ' '.join(values)

    def desktop(node):
        value = markers(node)
        nav = node.find_parent('nav')
        primary = nav and re.search(r'primary\s+navigation', nav.get('aria-label', ''), re.I)
        return bool(primary or re.search(r'desktop', value, re.I)) and not bool(re.search(r'mobile|drawer', value, re.I))

    def category_url(anchor):
        target = normalize_url(urljoin(base, anchor.get('href', '')))
        return target if (urlsplit(target).hostname == base_host and
                          re.search(r'/(?:collections|categories|category)/', urlsplit(target).path, re.I) and
                          not is_blacklisted(target)) else None

    def label(anchor):
        preferred = anchor.select_one('[data-text],[class*="link-label" i],[class*="item-label" i],[class*="title" i]')
        value = (preferred.get('data-text') if preferred else '') or text(preferred or anchor)
        value = re.sub(r'\s+', ' ', value).strip()
        return value if value and ',' not in value and len(value) < 80 else ''

    controls = []
    for anchor in soup.select('header a[href][aria-controls],nav a[href][aria-controls]'):
        if anchor.find_parent(lambda node: getattr(node, 'attrs', None) is not None and
                              (node.get('role') == 'region' or re.search(r'mega.*panel|products?.*panel',
                               ' '.join(node.get('class', [])), re.I))):
            continue
        target = category_url(anchor)
        panel_id = anchor.get('aria-controls', '').strip()
        panels = soup.find_all(id=panel_id) if panel_id else []
        if target and desktop(anchor):
            if len(panels) != 1:
                return None
            controls.append((anchor, target, panels[0]))
    if not controls:
        return None

    controlled_panels = {id(panel): panel for _, _, panel in controls}
    roots = []
    root_nodes = []
    for anchor in soup.select('header nav a[href],header [class*="desktop" i] a[href]'):
        target = category_url(anchor)
        if not target or not desktop(anchor):
            continue
        if any(id(parent) in controlled_panels for parent in anchor.parents):
            continue
        name = label(anchor)
        if name:
            root_nodes.append((anchor, name, target))

    seen_roots = set()
    for anchor, root_label, root_url in root_nodes:
        root_key = (root_label.casefold(), root_url)
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        panel = next((panel for control, target, panel in controls if control is anchor and target == root_url), None)
        links = [{'labels': [root_label], 'url': root_url, 'model': False}]
        if panel is not None:
            nested = {}
            for child in panel.select('a[href][aria-controls]'):
                nested_id = child.get('aria-controls', '').strip()
                matches = soup.find_all(id=nested_id) if nested_id else []
                if len(matches) == 1:
                    nested[id(matches[0])] = (child, matches[0])
            added = {(tuple(links[0]['labels']), root_url)}
            for child in panel.select('a[href]'):
                child_url = category_url(child)
                if not child_url or child_url == root_url or any(id(parent) in nested for parent in child.parents):
                    continue
                child_label = label(child)
                if not child_label:
                    return None
                child_path = [root_label, child_label]
                item = (tuple(child_path), child_url)
                if item not in added:
                    links.append({'labels': child_path, 'url': child_url, 'model': False});added.add(item)
                nested_id = child.get('aria-controls', '').strip()
                nested_panel = soup.find(id=nested_id) if nested_id else None
                if nested_panel is not None:
                    for leaf in nested_panel.select('a[href]'):
                        leaf_url = category_url(leaf)
                        if not leaf_url or leaf_url in {root_url, child_url}:
                            continue
                        leaf_label = label(leaf)
                        if not leaf_label:
                            return None
                        leaf_path = [root_label, child_label, leaf_label]
                        leaf_item = (tuple(leaf_path), leaf_url)
                        if leaf_item not in added:
                            links.append({'labels': leaf_path, 'url': leaf_url, 'model': False});added.add(leaf_item)
        roots.append({'label': root_label, 'url': root_url, 'links': links,
                      'source': 'linked_static_dom', 'panel_id': panel.get('id') if panel else None})
    controlled_root_ids = {id(control) for control, _, _ in controls}
    if not roots or not controlled_root_ids.issubset({id(anchor) for anchor, _, _ in root_nodes}):
        return None
    return {'version': VERSION, 'method': 'linked_static_dom', 'roots': roots, 'complete': True}

def check(categories, evidence, html, base):
    if evidence.get('method')=='shopify_practical':
        from .practical_navigation import validate_practical
        return validate_practical(categories,evidence,[html],base)
    """Compare candidate paths to separately captured source ownership."""
    from .nav_extractor import is_blacklisted
    if not evidence or not evidence.get('complete') or evidence.get('version')!=VERSION:
        raise CrawlError('目录缺少完整的独立核对证据')
    if evidence.get('visual_pruning'):
        pruning=evidence['visual_pruning']
        original=pruning['original_proof']
        if original.get('visual_pruning'):
            raise CrawlError('不能递归伪造剪枝证明')
        check(pruning['original_categories'],original,html,base)
        names=set(pruning['auto_pruned_roots'])
        expected={k:v for k,v in pruning['original_categories'].items() if k.split(',')[0] not in names}
        if not expected or categories!=expected:
            raise CrawlError('剪枝后的目录与原始分支不一致')
    if evidence.get('method')=='ai_skill':
        review=evidence.get('review',{})
        if not review.get('complete') or review.get('unresolved') or review.get('skill')!='category-json' or not review.get('branches'):
            raise CrawlError('AI 目录审查未完成')
    for root in evidence['roots']:
        coverage=root.get('submenu_coverage')
        if coverage and any(e['leafId'] not in coverage['observed_ids'] for e in coverage['expected']):
            raise CrawlError('独立菜单证据存在未展开的子链接')
    roots={r['label'].casefold():r for r in evidence['roots']}
    if evidence.get('method') == 'owned_menu_tree':
        from .menu_tree import verify_sources, tree_data
        verify_sources(evidence, html, base)
        home = homepage_entries(clean_soup(html), base, tree_data(evidence).values())
    else:
        menu_urls = [entry['url'] for root in evidence.get('roots', [])
                     for entry in root.get('links', []) if entry.get('url')]
        home=[] if evidence.get('method')=='ai_skill' else homepage_entries(
            clean_soup(html), base, menu_urls)
    home_pairs={(e['key'],e['url']) for e in home}
    represented=set()
    for key,url in categories.items():
        labels=labels_from_key(key)
        if len(labels)>3 or any(not x or ',' in x or x.isdigit() for x in labels):
            raise CrawlError('目录标签或层级不合法: '+key)
        root=roots.get(labels[0].casefold())
        if root is None:
            if len(labels)==1 and (key,url) in home_pairs:continue
            raise CrawlError('目录一级未出现在已核对的主导航: '+labels[0])
        matches=[e for e in root['links'] if e['url']==url]
        if not matches:raise CrawlError('链接未出现在所属展开菜单: '+key)
        if len(labels)>1:
            exact=[e for e in matches if [x.casefold() for x in e['labels']]==[x.casefold() for x in labels]]
            # A non-clickable grouping may reuse the actual parent aggregator.
            group=any([x.casefold() for x in e['labels'][:len(labels)]]==[x.casefold() for x in labels] for e in root['links'])
            aggregate=any(e['labels'][:len(labels)]==labels and re.match(r'^(shop|view|see) all',e['labels'][-1],re.I) for e in matches)
            if not exact and not (group and (url==root['url'] or aggregate)):
                raise CrawlError('分组标题或层级与独立菜单证据不符: '+key)
        represented.add((tuple(x.casefold() for x in labels),url))
    for root in evidence['roots']:
        for entry in root['links']:
            if is_blacklisted(entry['url'],allow_product=entry.get('model',False)):continue
            if (tuple(x.casefold() for x in entry['labels']),entry['url']) not in represented:
                raise CrawlError('目录遗漏已展开菜单链接: '+entry['url'])
    return {'passed':True,'version':VERSION,'method':evidence['method'],'roots':[r['label'] for r in evidence['roots']]}

# Shared DOM rules: semantic controls, visibility, and nearby heading ownership.
SUBMENU_JS = r"""e => {
 const parent=e.parentElement;
 const panels=[...parent.children].filter(n=>n!==e&&!n.contains(e)&&n.querySelector('a[href]')&&(n.matches('ul,menu,[role=menu]')||/menu|dropdown|flyout/i.test(n.className||'')));
 for(const id of (e.getAttribute('aria-controls')||'').split(/\s+/)){const n=document.getElementById(id);if(n&&!panels.includes(n))panels.push(n)}
 return panels.flatMap(n=>[...n.querySelectorAll('a[href]')]).map(a=>{let order=[];for(let n=a;n&&n.parentElement;n=n.parentElement)order.unshift([...n.parentElement.children].indexOf(n));if(!a.dataset.navLeafId)a.dataset.navLeafId='leaf-'+(window.__navLeafCounter=(window.__navLeafCounter||0)+1);return {url:a.href,order,leafId:a.dataset.navLeafId}});
}"""

CONTROL_SCOPE_JS = r"""e => {
 const anchor=e.matches('a[href]')?e:e.querySelector(':scope > a[href]');
 const href=anchor?.getAttribute('href')||'';
 const parent=e.parentElement;
 const container=(anchor&&anchor!==e)?e:(e.closest('li,[aria-haspopup=true],details')||(!parent.matches('nav,header,body')&&parent.querySelector(':scope > ul,:scope > [role=menu],:scope > [class*=submenu],:scope > [class*=dropdown]')?parent:e));
 const panels=[container];
 for(const id of (e.getAttribute('aria-controls')||'').split(/\s+/)){const p=document.getElementById(id);if(p)panels.push(p)}
 return {href, descendants:[...new Set(panels.flatMap(p=>[...p.querySelectorAll('a[href]')]).filter(a=>a!==anchor&&!e.contains(a)).map(a=>a.href))]};
}"""

def excluded_control(scope, base):
    from .nav_extractor import is_blacklisted
    href=scope.get('href','')
    if not href or href=='#':return False
    if not is_blacklisted(urljoin(base,href)):return False
    return not any(not is_blacklisted(urljoin(base,u),allow_product=True) for u in scope.get('descendants',[]))

BROWSER_JS = r"""() => {
 const visible=e=>!!(e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && getComputedStyle(e).display!=='none' && e.getBoundingClientRect().right>0 && e.getBoundingClientRect().left<innerWidth && !e.closest('[inert],footer') && ![...function*(n){for(;n;n=n.parentElement)yield n}(e.parentElement)].some(n=>{const s=getComputedStyle(n),r=n.getBoundingClientRect();return Number(s.opacity)===0||(/hidden|clip/.test(s.overflow+s.overflowY)&&(r.height<1||r.width<1))}));
 const label=e=>{const h=e.querySelector('h1,h2,h3,h4,h5,h6,[class*=title i],[class*=heading i],[class*=menu-item-text],[class*=link__text]');const n=(h||e).cloneNode(true);n.querySelectorAll('svg,script,style,[aria-hidden=true],[class*=badge],[class*=pill],[class$=__tag],[class$="-tag"]').forEach(x=>x.remove());return ((n.textContent||'').replace(/\s+/g,' ').trim()||(e.getAttribute('aria-label')||'').trim())};
 const els=[...document.querySelectorAll('header a,header button,header summary,nav a,nav button,nav summary,[role=navigation] a,[role=navigation] button')].filter(visible).filter(e=>{const li=e.closest('li');return !(e.tagName==='BUTTON' && li && [...li.children].some(c=>c.tagName==='A'&&visible(c)))});
 return els.map((e,i)=>{e.setAttribute('data-nav-audit-id','control-'+i);return {id:'control-'+i,label:label(e),href:e.getAttribute('href')||'',utility:[e.getAttribute('aria-label'),e.getAttribute('title'),e.getAttribute('alt'),e.getAttribute('href'),e.getAttribute('aria-controls')].filter(Boolean).join(' '),bindings:[...e.attributes].filter(a=>a.name==='aria-controls'||a.name!=='data-nav-audit-id'&&/^data-/.test(a.name)&&/nav|menu|drawer|target|trigger/.test(a.name)).map(a=>a.value).filter(v=>v&&v.length<80&&!['true','false'].includes(v)),expand:!!e.parentElement?.matches('[aria-haspopup=true]')||!!(e.closest('li')&&e.closest('li').querySelector('ul,menu,[role=menu],[class*=mega],[class*=dropdown]'))||e.hasAttribute('aria-expanded')||e.hasAttribute('aria-haspopup')||e.tagName==='SUMMARY'||[...e.attributes].some(a=>a.name!=='data-nav-audit-id' && /menu|nav|trigger/.test(a.name)),top:e.getBoundingClientRect().top}});
}"""
LINKS_JS = r"""() => {
 const visible=e=>!!(e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && getComputedStyle(e).display!=='none' && e.getBoundingClientRect().right>0 && e.getBoundingClientRect().left<innerWidth && !e.closest('[inert],footer') && ![...function*(n){for(;n;n=n.parentElement)yield n}(e.parentElement)].some(n=>{const s=getComputedStyle(n),r=n.getBoundingClientRect();return Number(s.opacity)===0||(/hidden|clip/.test(s.overflow+s.overflowY)&&(r.height<1||r.width<1))}));
 const text=e=>{const n=e.cloneNode(true);n.querySelectorAll('span').forEach(x=>{if(/^(new!?|sale!?|hot!?)$/i.test(x.textContent.trim()))x.remove()});n.querySelectorAll('svg,script,style,[aria-hidden=true],[class*=badge],[class$=__tag]').forEach(x=>x.remove());return ((n.textContent||'').replace(/\s+/g,' ').trim()||(e.getAttribute('aria-label')||'').trim())};
 return [...document.querySelectorAll('header a[href],nav a[href],[role=navigation] a[href],[role=menu] a[href]')].filter(visible).map(a=>{
  const own=a.querySelector('h1,h2,h3,h4,h5,h6,[class*=title i],[class*=heading i],[class*=menu-item-text],[class*=link__text]');const label=text(own||a);let groups=[];let owner='';let bindings=[];
  for(let n=a.parentElement;n && !['NAV','HEADER','BODY'].includes(n.tagName);n=n.parentElement){
   bindings.push(...[...n.attributes].filter(x=>x.name==='id'||x.name!=='data-nav-audit-id'&&/^data-/.test(x.name)&&/nav|menu|drawer|target|trigger/.test(x.name)).map(x=>x.value).filter(v=>v&&v.length<80&&!['true','false'].includes(v)));
   if(!owner){for(const sibling of n.children){if(sibling===a||sibling.contains(a))continue;const cs=sibling.matches('[data-nav-audit-id]')?[sibling]:[...sibling.querySelectorAll('[data-nav-audit-id]')];if(cs.length===1){owner=cs[0].getAttribute('data-nav-audit-id');break}}}
   let h=[...n.children].find(c=>c!==a && !c.contains(a) && (/^H[1-6]$/.test(c.tagName)||c.matches('[role=heading]')||/(^|[-_ ])(heading|title|h[1-6])($|[-_ ])/.test(c.className||'')));
   if(!h && (n.tagName==='LI'||n.matches('[aria-haspopup=true]')||n.querySelector(':scope > button')))h=[...n.children].find(c=>c!==a&&!c.contains(a)&&c.matches('a,button,summary'));
   if(h){const t=text(h);if(t && t!=='#' && !/close|open.*menu|back/i.test(t) && t.length<70)groups.unshift({label:t,control:h.getAttribute('data-nav-audit-id')||'',tag:h.tagName,sectionHeader:/(^|[-_ ])header($|[-_ ])/.test(n.className||'')})}
  }
  let order=[];for(let n=a;n&&n.parentElement;n=n.parentElement)order.unshift([...n.parentElement.children].indexOf(n));
  if(!a.dataset.navLeafId)a.dataset.navLeafId='leaf-'+(window.__navLeafCounter=(window.__navLeafCounter||0)+1);
  return {label,url:a.href,leafId:a.dataset.navLeafId,owner,bindings,control:a.getAttribute('data-nav-audit-id')||'',ambiguous:!own&&!!a.querySelector('p,[class*=description i],[class*=subtitle i]'),order,groups:groups.map(g=>g.label),groupControls:groups.map(g=>g.control),groupTags:groups.map(g=>g.tag),groupHeaders:groups.map(g=>g.sectionHeader),promo:!!a.closest('[class*=promo],[class*=featureCallout],[class*=recommend]')};
 });
}"""

# Read an explicitly associated subtree even when CSS keeps it hidden or a
# hover handler fails to toggle aria-expanded.  This is evidence from the
# current DOM, not an inferred selector or synthetic category.
BOUND_LINKS_JS = r"""e => {
 const text=n=>{const c=n.cloneNode(true);c.querySelectorAll('span').forEach(x=>{if(/^(new!?|sale!?|hot!?)$/i.test((x.textContent||'').trim()))x.remove()});c.querySelectorAll('svg,script,style,[aria-hidden=true],[class*=badge],[class$=__tag]').forEach(x=>x.remove());return ((c.textContent||'').replace(/\s+/g,' ').trim()||(n.getAttribute('aria-label')||'').trim())};
 const panels=[];const add=n=>{if(n&&!panels.includes(n))panels.push(n)};
 for(const id of (e.getAttribute('aria-controls')||'').split(/\s+/)){if(id)add(document.getElementById(id))}
 const owner=e.closest('li,details,[aria-haspopup=true]');if(owner)add(owner);
 const parent=e.parentElement;if(parent&&!parent.matches('nav,header,body')&&(parent.querySelector(':scope > ul,:scope > menu,:scope > [role=menu],:scope > [class*=mega],:scope > [class*=submenu],:scope > [class*=dropdown],:scope > [class*=flyout]')))add(parent);
 return [...new Set(panels.flatMap(p=>[...p.querySelectorAll('a[href]')]))].filter(a=>a!==e&&!e.contains(a)).map(a=>{
  const own=a.querySelector('h1,h2,h3,h4,h5,h6,[class*=title i],[class*=heading i],[class*=menu-item-text],[class*=link__text]');
  const label=text(own||a);let groups=[];let order=[];
  for(let n=a.parentElement;n&&!panels.includes(n)&&!['NAV','HEADER','BODY'].includes(n.tagName);n=n.parentElement){
   const children=[...n.children],branch=children.find(c=>c===a||c.contains(a));const before=children.slice(0,Math.max(0,children.indexOf(branch)));
   const h=[...before].reverse().find(c=>/^H[1-6]$/.test(c.tagName)||c.matches('[role=heading]')||/(^|[-_ ])(heading|title)($|[-_ ])/.test(c.className||''));
   if(h){const t=text(h);if(t&&!/^(view|shop|see) all$/i.test(t)&&t.length<70)groups.unshift(t)}
   const owner=[...children].find(c=>c!==branch&&!c.contains(a)&&c.matches('a,button,summary'));
   if(owner){const t=text(owner);if(t&&!/^(view|shop|see) all$/i.test(t)&&t.length<70&&!groups.includes(t))groups.unshift(t)}
  }
  const boundary=panels.find(p=>p.contains(a));
  if(boundary){const children=[...boundary.children],branch=children.find(c=>c===a||c.contains(a)),before=children.slice(0,Math.max(0,children.indexOf(branch)));const h=[...before].reverse().find(c=>/^H[1-6]$/.test(c.tagName)||c.matches('[role=heading]')||/(^|[-_ ])(heading|title)($|[-_ ])/.test(c.className||''));if(h){const t=text(h),root=text(e);if(t&&t.toLowerCase()!==root.toLowerCase()&&!/^(view|shop|see) all$/i.test(t)&&!groups.includes(t))groups.unshift(t)}}
  for(let n=a;n&&n.parentElement;n=n.parentElement)order.unshift([...n.parentElement.children].indexOf(n));
  if(!a.dataset.navLeafId)a.dataset.navLeafId='leaf-'+(window.__navLeafCounter=(window.__navLeafCounter||0)+1);
  const bindings=[...new Set([e.getAttribute('aria-controls')||'',...panels.flatMap(p=>[p.id,...[...p.attributes].filter(x=>/^data-/.test(x.name)&&/nav|menu|drawer|target|trigger|handle/.test(x.name)).map(x=>x.value)])].filter(Boolean))];
  return {label,url:a.href,leafId:a.dataset.navLeafId,owner:e.getAttribute('data-nav-audit-id')||'',bindings,control:a.getAttribute('data-nav-audit-id')||'',ambiguous:!own&&!!a.querySelector('p,[class*=description i],[class*=subtitle i]'),order,groups,groupControls:groups.map(()=>''),groupTags:groups.map(()=>''),groupHeaders:groups.map(()=>false),promo:!!a.closest('[class*=promo],[class*=featureCallout],[class*=recommend]')};
 });
}"""

def capture_visual_snapshots(base, out):
    from .visual_capture import capture_visual_snapshots as capture
    return capture(base, out)


def browser_evidence(base, timeout=15, out=None):
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    from .nav_extractor import is_blacklisted
    started=time.monotonic()
    captures=[];roots=[]
    def bounded():
        if time.monotonic()-started>timeout:raise CrawlError('目录展开核对超时，未发布目录')
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        try:
            page=browser.new_page(locale='en-US',viewport={'width':1440,'height':1000})
            page.context.add_cookies([{'name':'localization','value':'US','url':base},{'name':'cart_currency','value':'USD','url':base}])
            try:
                page.goto(base,wait_until='domcontentloaded',timeout=min(8000, max(2500, int(timeout*1000*.55))))
            except BrowserTimeout:
                # Late third-party scripts must not override actual menu evidence.
                if urlsplit(page.url).hostname!=urlsplit(base).hostname or not page.locator('header:visible,nav:visible,[role=navigation]:visible').count():
                    raise
                captures.append(stable_page_content(page))
            page.wait_for_timeout(500)
            try:
                wait_for_storefront(page, attempts=2)
            except CrawlError:
                captures.append(stable_page_content(page))
                raise
            for selector in ('#onetrust-reject-all-handler','#onetrust-accept-btn-handler'):
                el=page.locator(selector).first
                if el.count() and el.is_visible():el.click(timeout=2000)
            for closer in page.locator('button[aria-label*="close" i],[role=button][aria-label*="close" i],[class*=popup] [class*=close],[id*=popup] [id*=close],button[class*=close]').all():
                if closer.is_visible() and closer.evaluate('e=>{const r=e.getBoundingClientRect(),h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return !!h && (h===e||e.contains(h))}'):
                    closer.click(timeout=2000);page.wait_for_timeout(200)
            shopify='Shopify' in stable_page_content(page)
            controls=page.evaluate(BROWSER_JS)
            controls=[c for c in controls if c['label'] and not c['label'].isdigit() and not re.search(r'wish.?list|shopping.?bag|shopping.?cart|/cart|/account|search|sign.?in|log.?in|currency|country|language',c.get('utility',''),re.I) and len(c['label'])<65 and c['top']<350 and not re.search(r'search|account|cart|log.?in|sign.?in|currency|country|region|language|cookie',c['label'],re.I) and (c['expand'] or not is_blacklisted(urljoin(base,c['href'])))]
            controls=[c for c in controls if not excluded_control(page.locator('[data-nav-audit-id="'+c['id']+'"]').evaluate(CONTROL_SCOPE_JS),base)]
            if not controls or len(controls)>35:raise CrawlError('无法唯一识别可见主导航范围')
            baseline={(e['label'],e['url'],tuple(e['order'])) for e in page.evaluate(LINKS_JS)}
            all_bindings={b for c in controls for b in c.get('bindings',[])}
            for c in controls:
                bounded()
                control=page.locator('[data-nav-audit-id="'+c['id']+'"]')
                if control.count()!=1:raise CrawlError('主导航在核对过程中发生变化')
                action_error=None
                try:
                    if not control.is_visible():raise CrawlError('主导航触发器当前不可见')
                    page.wait_for_function('id => { const e=document.querySelector(`[data-nav-audit-id="${id}"]`); if(!e)return false; const r=e.getBoundingClientRect(),h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2); return h && (e.contains(h)||h===e||h===e.closest("li")); }',arg=c['id'],timeout=2500)
                    box=control.bounding_box()
                    page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
                    page.wait_for_timeout(300)
                    if c['expand'] and (control.get_attribute('aria-expanded')=='false' or control.evaluate('e=>["BUTTON","SUMMARY"].includes(e.tagName)') and control.get_attribute('aria-expanded')!='true'):
                        # Only explicit menu controls can be clicked, never normal links.
                        control.click(timeout=1500,no_wait_after=True);page.wait_for_timeout(450)
                except Exception as error:
                    action_error=error
                observed=page.evaluate(LINKS_JS)
                expected=control.evaluate(SUBMENU_JS)
                bound=control.evaluate(BOUND_LINKS_JS)
                for entry in bound:
                    entry['owner']=c['id'];entry['control']=''
                observed_ids={entry.get('leafId') for entry in observed}
                expected_ids={entry.get('leafId') for entry in expected}
                unresolved=expected_ids-observed_ids
                visible_expected=expected_ids & observed_ids
                has_new_visible=any((entry['label'],entry['url'],tuple(entry['order'])) not in baseline for entry in observed)
                fallback_needed=bool(action_error or (expected_ids and not visible_expected)
                                     or (c['expand'] and not expected_ids and not has_new_visible))
                if bound and fallback_needed:
                    observed.extend(entry for entry in bound if entry.get('leafId') not in observed_ids)
                    if not expected:
                        expected.extend({'url':entry['url'],'order':entry['order'],'leafId':entry['leafId']} for entry in bound)
                elif action_error:
                    raise action_error
                visited=set()
                for branch_index in range(30):
                    bounded()
                    branches=page.locator('header button,nav button,header summary,nav summary,[role=navigation] button,header a[aria-expanded=false],nav a[aria-expanded=false],nav [aria-haspopup=true] > a,header a,nav a,[role=navigation] a')
                    opened=False
                    for bi in range(branches.count()):
                        branch=branches.nth(bi)
                        if not branch.is_visible():continue
                        if excluded_control(branch.evaluate(CONTROL_SCOPE_JS),base):continue
                        submenu=branch.evaluate(SUBMENU_JS)
                        if not submenu and not branch.evaluate('e=>{const r=e.getBoundingClientRect();return r.right>0&&r.left<innerWidth&&!e.closest("[inert],[id*=cart],[class*=cart-drawer],[id*=search]")&&(e.parentElement?.matches("[aria-haspopup=true]")||e.tagName==="SUMMARY"||e.hasAttribute("aria-controls")||e.hasAttribute("aria-expanded")||[...e.attributes].some(a=>/^data-/.test(a.name)&&!/^data-nav-(audit|leaf)-id$/.test(a.name)&&/nav|menu|trigger/.test(a.name)))}'):continue
                        if branch.get_attribute('data-nav-audit-id'):continue
                        title=' '.join(branch.inner_text().split())
                        if title.casefold() in {x['label'].casefold() for x in controls}:continue
                        if not title or title in visited or re.search(r'close|search|cart|account|back',title,re.I):continue
                        if branch.get_attribute('aria-expanded')=='true':
                            expected.extend(submenu)
                            continue
                        expected.extend(submenu)
                        before={(e['label'],e['url']) for e in page.evaluate(LINKS_JS)}
                        branch_error=None
                        try:
                            if submenu and branch.evaluate('e=>e.tagName==="A"'):
                                control.hover(timeout=1500)
                                page.wait_for_timeout(150)
                                branch.hover(timeout=1500)
                            else:
                                branch.click(timeout=1500,no_wait_after=True)
                            page.wait_for_timeout(350)
                        except Exception as error:
                            branch_error=error
                        after=page.evaluate(LINKS_JS)
                        fresh=[e for e in after if (e['label'],e['url']) not in before]
                        if not fresh:
                            fresh=branch.evaluate(BOUND_LINKS_JS)
                            for entry in fresh:
                                entry['owner']=c['id'];entry['control']=''
                            if fresh and not submenu:
                                expected.extend({'url':entry['url'],'order':entry['order'],'leafId':entry['leafId']} for entry in fresh)
                        if not fresh:
                            if branch_error:raise branch_error
                            raise CrawlError('子菜单未展开出可核对内容: '+title)
                        for e in fresh:
                            if title not in e['groups']:
                                e['groups'].insert(0,title);e.setdefault('groupControls',[]).insert(0,'');e.setdefault('groupTags',[]).insert(0,'');e.setdefault('groupHeaders',[]).insert(0,False)
                        visited.add(title)
                        observed.extend(fresh);captures.append(stable_page_content(page));opened=True
                        break
                    if not opened:break
                else:raise CrawlError('子菜单展开超过30个分支，未发布不完整目录')
                observed_positions={e['leafId'] for e in observed}
                missing=[e['url'] for e in expected if not is_blacklisted(e['url'],allow_product=True) and e['leafId'] not in observed_positions]
                if missing and out:
                    from pathlib import Path
                    Path(out).mkdir(parents=True,exist_ok=True)
                    (Path(out)/'coverage.json').write_text(json.dumps({'root':c,'visited':list(visited),'expected':expected,'observed':observed},ensure_ascii=False),encoding='utf-8')
                if missing:raise CrawlError('子菜单覆盖不完整，仍有链接未实际展开核对: '+', '.join(dict.fromkeys(missing)))
                original_order={}
                for entry in expected:original_order.setdefault(entry['leafId'],entry['order'])
                observed=[dict(e,order=original_order.get(e['leafId'],e['order'])) for e in observed]
                scoped = any(set(e.get("bindings",[])) & set(c.get("bindings",[])) for e in observed)
                def belongs(e):
                    if e.get('control') in {x['id'] for x in controls}:return False
                    bound=set(e.get('bindings',[])) & all_bindings
                    if bound:return bool(bound & set(c.get('bindings',[])))
                    if scoped:return False
                    return e.get('owner')==c['id'] or (not e.get('owner') and (e['label'],e['url'],tuple(e['order'])) not in baseline)
                unique=[e for e in observed if belongs(e)]
                if c['expand'] and not unique:
                    # Last-chance semantic fallback: a failed CSS hover or an
                    # unchanged aria-expanded flag is not evidence of an empty
                    # menu when the trigger already owns category anchors.
                    fallback=control.evaluate(BOUND_LINKS_JS)
                    for entry in fallback:
                        entry['owner']=c['id'];entry['control']=''
                    fallback=[entry for entry in fallback if not is_blacklisted(entry['url'],allow_product=True)]
                    if fallback:
                        unique=fallback
                        observed.extend(entry for entry in fallback if entry.get('leafId') not in observed_positions)
                        observed_positions.update(entry.get('leafId') for entry in fallback if entry.get('leafId'))
                        if not expected:
                            expected.extend({'url':entry['url'],'order':entry['order'],'leafId':entry['leafId']} for entry in fallback)
                own_url=normalize_url(urljoin(base,c['href']))
                links=[]
                has_own_link=bool(c['href'] and c['href']!='#')
                if has_own_link and not is_blacklisted(own_url):links.append({'labels':[c['label']],'url':own_url})
                for e in unique:
                    u=normalize_url(e['url'])
                    if shopify and not re.search(r'/collections/',urlsplit(u).path):continue
                    if re.search(r'/(?:products?|pages?)/', urlsplit(u).path, re.I):continue
                    if is_blacklisted(u,allow_product=not e.get('promo',False)):continue
                    if e.get('ambiguous'):raise CrawlError('菜单卡片含说明文字且缺少可确认的独立标题: '+u)
                    gs=[g for i,g in enumerate(e['groups']) if g.casefold()!=c['label'].casefold() and (i>=len(e.get('groupControls',[])) or e['groupControls'][i]!=c['id'])]
                    if e['label'].casefold() in {'shop now','view all','see all','learn more'}:
                        if gs:e=dict(e,label=gs.pop())
                        elif u==own_url:e=dict(e,label=c['label'])
                        else:raise CrawlError('菜单通用按钮无法确认分类标题')
                    labels=[c['label']]+gs[:1]+[e['label']]
                    if not e['label'] and any(x['label'] and normalize_url(x['url'])==u for x in unique):continue
                    if len(labels)>3 or not e['label'] or ',' in e['label']:raise CrawlError('菜单标题含描述或无法确定层级: '+repr(e))
                    links.append({'labels':labels,'url':u,'order':e.get('order',[]),'model':'/products/' in urlsplit(u).path})
                if c['expand'] and not unique:
                    captures.append(stable_page_content(page))
                    if out is not None:
                        diagnostic_dir = __import__('pathlib').Path(out)
                        diagnostic_dir.mkdir(parents=True,exist_ok=True)
                        (diagnostic_dir/'failed-controls.json').write_text(json.dumps({'control':c,'observed':observed},ensure_ascii=False,indent=2),encoding='utf-8')
                    raise CrawlError('菜单触发器未展开出可验证内容: '+c['label'])
                if links:
                    if not has_own_link or is_blacklisted(own_url):
                        all_link=next((e for e in links if re.match(r'^(shop|view|see) all',e['labels'][-1],re.I)),None)
                        if all_link is None:
                            own_url=None  # Structural parent; keep its name in child paths only.
                        else:
                            own_url=all_link['url']
                            links.insert(0,{'labels':[c['label']],'url':own_url})
                    roots.append({'label':c['label'],'url':own_url,'links':links,'submenu_coverage':{'expected':[e for e in expected if not is_blacklisted(e['url'],allow_product=True)],'observed_ids':sorted(observed_positions)}})
                captures.append(stable_page_content(page))
                if out is not None:
                    from pathlib import Path
                    Path(out).mkdir(parents=True,exist_ok=True)
                    from .visual_capture import frame
                    capture = frame(page, Path(out), f'home-menu-{len(captures)-1:03d}')
                    captures[-1] = (Path(out)/capture['html']).read_text(encoding='utf-8')
                if c['expand'] and control.count() and (control.get_attribute('aria-expanded')=='true' or control.evaluate('e=>e.tagName==="SUMMARY" && e.parentElement.open')):
                    control.click(timeout=1500,no_wait_after=True)
                page.keyboard.press('Escape');page.mouse.move(1430,950);page.wait_for_timeout(150)
            if not roots:raise CrawlError('浏览器未确认任何商品菜单')
            return {'version':VERSION,'method':'browser_visible','complete':True,'roots':roots},captures,stable_page_content(page)
        finally:
            if out is not None:
                from pathlib import Path
                from .common import save_json
                target=Path(out);target.mkdir(parents=True,exist_ok=True)
                for i,fragment in enumerate(captures):(target / f'menu-observation-{i:03d}.html').write_text(fragment,encoding='utf-8')
                save_json(target / 'menu-observation-partial.json', {'roots':roots,'elapsed':round(time.monotonic()-started,3)})
            browser.close()

def from_evidence(evidence,html,base):
    from .nav_extractor import _hierarchical_key,NavigationAnalysis
    if evidence.get('method') == 'owned_menu_tree':
        from .menu_tree import tree_data
        data = tree_data(evidence)
        for entry in homepage_entries(clean_soup(html), base, data.values()):
            if entry['key'] in data and data[entry['key']] != entry['url']:
                raise CrawlError('首页键冲突')
            data[entry['key']] = entry['url']
        return NavigationAnalysis(base_url=base, roots=[], data=data, metadata={'owned_tree': evidence})
    data={}
    for root in evidence['roots']:
        for entry in sorted(root['links'],key=lambda e:e.get('order',[])):
            if len(entry['labels'])==3:
                group_labels=entry['labels'][:2]
                group_key=_hierarchical_key(group_labels)
                group_target=next((e['url'] for e in root['links'] if e['labels']==group_labels),None)
                if group_target is None:
                    group_target=next((e['url'] for e in root['links'] if e['labels'][:2]==group_labels and re.match(r'^(shop|view|see) all',e['labels'][-1],re.I)),root['url'])
                if group_target:data.setdefault(group_key,group_target)
            key=_hierarchical_key(entry['labels'])
            if key in data and data[key]!=entry['url']:raise CrawlError('展开菜单同名路径冲突: '+key)
            data[key]=entry['url']
    for entry in homepage_entries(clean_soup(html),base,data.values()):
        if entry['key'] in data and data[entry['key']]!=entry['url']:raise CrawlError('首页键冲突')
        data[entry['key']]=entry['url']
    return NavigationAnalysis(base_url=base,roots=[],data=data,metadata={'mode':'verified_semantic','audit_version':VERSION})
