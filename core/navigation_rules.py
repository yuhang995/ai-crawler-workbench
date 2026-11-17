"""Result-oriented category rules, including button-owned drawer menus.

No domain checks or stored category maps: ownership comes from the current DOM.
"""
import re
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode, unquote
from .html_parser import parse_soup as BeautifulSoup

GENERIC = {'shop now', 'view all', 'see all', 'shop all', 'learn more', 'discover', 'explore', 'shop', 'buy now', 'start shopping'}


def is_generic_action(value):
    value = ' '.join(str(value or '').split()).casefold()
    value = re.sub(r'^[\W_]+|[\W_›»>→]+$', '', value).strip()
    return value in GENERIC or bool(re.fullmatch(
        r'(?:shop|view|see)\s+all\s+(?:products?|items?|styles?|collections?)', value))


def normalize_url(url):
    p = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
              if not k.startswith(('utm_', '_ga')) and k not in {'_gl', 'gclid', 'fbclid'}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', urlencode(params), p.fragment))


def slug(value):
    return re.sub(r'[^\w]+', '-', unquote(value).casefold()).strip('-')


def home_key(url, label):
    p = urlsplit(url)
    tail = p.path.rstrip('/').split('/')[-1]
    generic_tails = {'all', 'search'} | {slug(value) for value in GENERIC}
    return slug(label) if p.query or slug(tail) in generic_tails else tail


def text(node):
    if node is None:
        return ''
    return ' '.join(node.get_text(' ', strip=True, types=None).split())


def anchor_label(anchor):
    title = anchor.select_one('.subMenu__navGrandTitle,[class$="__card-title"]')
    return text(title) or anchor.get('data-ab-label') or text(anchor)


def _homepage_block_label(anchor, main):
    """Resolve a generic CTA from its nearest explicit homepage block title.

    This intentionally reads only semantic headings in the CTA's nearest
    card/header/module boundary.  Product-card titles and headings owned by a
    different link are excluded, so a carousel's first product can never name
    the carousel category.
    """
    own = anchor_label(anchor)
    if own and not is_generic_action(own):
        return own
    boundary_re = re.compile(
        r'hero|promo|card|banner|collection|carousel|quick.?links|shop.?by|media.?content|section|module|header',
        re.I,
    )
    current = anchor.parent
    while current is not None and current is not main:
        marker = str(current.get('id', '')) + ' ' + ' '.join(current.get('class', []))
        if boundary_re.search(marker) or current.name in {'section', 'article'}:
            headings = []
            for heading in current.select('h1,h2,h3,h4,h5,h6,[role=heading]'):
                other_link = heading.find_parent('a', href=True)
                if other_link is not None and other_link is not anchor:
                    continue
                product_owned = False
                for owner in [heading, *list(heading.parents)]:
                    if owner is current:
                        break
                    owner_marker = (str(owner.get('id', '')) + ' '
                                    + ' '.join(owner.get('class', [])))
                    if re.search(r'product.?(?:card|tile|item)', owner_marker, re.I):
                        product_owned = True
                        break
                if product_owned:
                    continue
                value = text(heading)
                if (not value or is_generic_action(value) or len(value) >= 100
                        or value.isdigit() or re.match(r'^[$\d\s.,%+-]+$', value)
                        or re.search(r'\b\d+\s+(?:products?|items?|styles?|colors?)\b', value, re.I)):
                    continue
                if value not in headings:
                    headings.append(value)
            if len(headings) == 1:
                generic_targets = {
                    anchor.get('href', '').strip()
                    for anchor in current.select('a[href]')
                    if is_generic_action(anchor_label(anchor))
                    and re.search(r'/(?:collections|categories|category)/',
                                  urlsplit(anchor.get('href', '')).path, re.I)
                }
                if len(generic_targets) > 1:
                    return own
                return headings[0]
            # Do not walk out of the first real module and borrow a title from
            # a neighbouring homepage section.
            if current.name in {'section', 'article'} or re.search(
                    r'hero|promo|banner|collection|carousel|module', marker, re.I):
                break
        current = current.parent
    return own


def clean_soup(html):
    soup = BeautifulSoup(html, 'lxml')
    for node in soup.select('script,style,svg,picture,.sidebarMenu__tag,[data-crawl-excluded]'):
        node.decompose()
    return soup


def homepage_entries(soup, base, existing):
    from .nav_extractor import is_blacklisted
    main = soup.select_one('main,[role=main],#MainContent')
    if main is None:
        return []
    entries, seen = [], set(existing)
    for anchor in main.select('a[href]'):
        ancestors = list(anchor.parents)
        markers = ' '.join(str(n.get('id', '')) + ' ' + ' '.join(n.get('class', [])) for n in ancestors)
        if any(n.name in ('footer', 'nav', 'p', 'details') for n in ancestors):
            continue
        if re.search(r'faq|accordion|rich.?text|seo|article|recommend|recently|footer', markers, re.I):
            continue
        # Require a card/promo/button context, rather than every link in body text.
        card = re.search(r'hero|promo|card|banner|collection|quick.?links|shop.?by|media.?content', markers, re.I)
        label = anchor_label(anchor)
        if not card and not is_generic_action(label) and not anchor.select_one('img'):
            continue
        target = normalize_url(urljoin(base, anchor['href']))
        if urlsplit(target).hostname != urlsplit(base).hostname or is_blacklisted(target, label):
            continue
        if not re.search(r'/(collections|categories|category)/', urlsplit(target).path):
            continue
        if target in seen:
            continue
        label = _homepage_block_label(anchor, main)
        key = home_key(target, label)
        if not key or is_generic_action(key.replace('-', ' ')):
            # An unverifiable generic CTA is not a category fact.  Skip this
            # optional homepage supplement instead of invalidating an already
            # complete and independently proven primary menu.
            continue
        entries.append({'key': key, 'url': target, 'labels': [key], 'source': 'home'})
        seen.add(target)
    return entries


def drawer_entries(html, base):
    """Return None for other themes; never promote submenu leaves to root."""
    from .nav_extractor import is_blacklisted, _hierarchical_key
    soup = clean_soup(html)
    roots = soup.select('.sidebarMenu__nav')
    if not roots or not soup.select_one('.sidebarMenu__mainTrigger'):
        return None
    entries, keys, root_labels = [], {}, []

    def add(labels, href, source='menu'):
        url = normalize_url(urljoin(base, href))
        if urlsplit(url).hostname != urlsplit(base).hostname:
            return
        model = '/products/' in urlsplit(url).path
        if is_blacklisted(url, ' '.join(labels), allow_product=model):
            return
        if not urlsplit(url).path.startswith(('/collections/', '/products/')):
            return
        labels = [x for x in labels if x and not x.isdigit()]
        if len(labels) > 3:
            labels = labels[:2] + [labels[-1]]
        if not labels or (len(labels) > 1 and labels[-1].casefold() in GENERIC):
            raise ValueError('未解析通用菜单按钮标题: ' + url)
        key = _hierarchical_key(labels)
        if key in keys:
            if keys[key] != url:
                raise ValueError('菜单同一路径指向两个不同目标: ' + key)
            return
        keys[key] = url
        entries.append({'key': key, 'url': url, 'labels': labels,
                        'source': 'menu_model' if model else source})

    for root in roots:
        top = text(root.select_one('.sidebarMenu__mainTrigger'))
        if not top or top.isdigit():
            continue
        groups = root.select('.sidebarMenu__subNav')
        aggregator = next((a for g in groups for a in g.select('a[href]')
                           if '/collections/' in a['href'] and not is_blacklisted(urljoin(base, a['href']))), None)
        if aggregator is None:
            continue  # Learn/Rewards/support-only menus aren't product taxonomies.
        root_labels.append(top)
        add([top], aggregator['href'])
        for group in groups:
            owner = group.select_one('.sidebarMenu__triggerText')
            if owner is None:
                a = group.select_one('a[href]')
                if a:
                    add([top, anchor_label(a)], a['href'])
                continue
            branch = text(owner)
            panel = group.select_one('.sidebarMenu__subMenu')
            if panel is None:
                raise ValueError('菜单分支缺少面板: ' + branch)
            # Rendered snapshot pruning removes inactive A/B markup. This also
            # supports saved snapshots of an explicitly injected replacement.
            replacement = panel.select_one('[data-crawl-replacement],.ab-injected-bw151')
            active = replacement if replacement is not None else panel
            parent = active.select_one('.sidebarMenu__shopAll,[data-ab-section="shop-all-top"] a')
            if parent is None:
                parent = next((a for a in active.select('a[href]')
                               if anchor_label(a).casefold().startswith('shop all ') and '/collections/' in a['href']), None)
            if parent is None:
                raise ValueError('菜单分支缺少实际聚合入口: ' + branch)
            add([top, branch], parent['href'])
            for a in active.select('a[href]'):
                if normalize_url(urljoin(base, a['href'])) == normalize_url(urljoin(base, parent['href'])):
                    continue
                label = anchor_label(a)
                feature = a.find_parent(class_='featureCallout__feature')
                if feature:
                    if '/products/' in a['href']:
                        continue  # A featured promotional SKU is not a taxonomy node.
                    label = text(feature.select_one('.featureCallout__featureTitle'))
                if not label:
                    label = urlsplit(a['href']).path.rstrip('/').split('/')[-1].replace('-', ' ').title()
                add([top, branch, label], a['href'])
    home = homepage_entries(soup, base, [e['url'] for e in entries])
    for entry in home:
        if entry['key'] in keys and keys[entry['key']] != entry['url']:
            raise ValueError('首页平级键冲突: ' + entry['key'])
        entries.append(entry)
    return {'entries': entries, 'primary_roots': root_labels,
            'mode': 'drawer_rendered' if soup.select_one('[data-crawl-menu-snapshot]') else 'drawer_static'}


def capture_drawers(page, timeout_ms=45000):
    """Capture every branch in one browser session; replace owners, never union A/Bs."""
    import time
    from .html_parser import parse_soup as BeautifulSoup
    deadline = time.monotonic() + timeout_ms / 1000
    page.wait_for_timeout(2000)
    snapshots = {}
    clean_js = r'''el => {
      const clone=el.cloneNode(true), src=[el,...el.querySelectorAll('*')], dst=[clone,...clone.querySelectorAll('*')];
      for(let i=src.length-1;i>0;i--){const n=src[i],s=getComputedStyle(n);
        if(s.display==='none'||s.visibility==='hidden'||['SCRIPT','STYLE','SVG'].includes(n.tagName))dst[i].remove();
      }
      return clone.outerHTML;
    }'''
    roots = page.locator('.sidebarMenu__nav')
    for ri in range(roots.count()):
        root = roots.nth(ri)
        # Don't open purely informational menus.
        if root.locator('a[href*="/collections/"]').count() == 0:
            continue
        trigger = root.locator('.sidebarMenu__mainTrigger').first
        trigger.click(timeout=3000)
        page.wait_for_timeout(250)
        groups = root.locator('.sidebarMenu__subNav')
        for gi in range(groups.count()):
            if time.monotonic() > deadline:
                raise RuntimeError('菜单展开超时，未交付不完整目录')
            group = groups.nth(gi)
            button = group.locator('.sidebarMenu__trigger').first
            if not button.count():
                continue
            button.click(timeout=3000)
            page.wait_for_timeout(500)
            panel = group.locator('.sidebarMenu__subMenu').first
            if not panel.is_visible():
                raise RuntimeError('菜单分支未实际展开')
            snapshots[group.get_attribute('id')] = panel.evaluate(clean_js)
        close = root.locator('.header__closeNav').first
        if close.count() and close.is_visible():
            close.click(timeout=3000)
    # One current DOM + one authoritative snapshot per owner. Hidden original
    # panels never get merged back after their live replacement has been read.
    soup = BeautifulSoup(page.content(), 'lxml')
    for owner, fragment in snapshots.items():
        group = soup.find(id=owner)
        if group is None:
            raise RuntimeError('菜单所有者在渲染中消失')
        old = group.select_one('.sidebarMenu__subMenu')
        replacement = BeautifulSoup(fragment, 'lxml').select_one('.sidebarMenu__subMenu')
        old.replace_with(replacement)
    soup.html['data-crawl-menu-snapshot'] = 'current-session'
    return str(soup)


def column_menu_entries(html, base):
    """Read primary desktop owners and their explicit column-heading groups."""
    from .nav_extractor import is_blacklisted, _hierarchical_key
    soup = clean_soup(html)
    menu = soup.select_one('menu[aria-label="Main Navigation"]')
    if menu is None or not menu.select_one('.submenu-test-link-list-heading'):
        return None
    entries, data, roots = [], {}, []
    def add(labels, href):
        url = normalize_url(urljoin(base, href.strip()))
        if is_blacklisted(url) or urlsplit(url).hostname != urlsplit(base).hostname:
            return
        key = _hierarchical_key(labels)
        if key in data:
            if data[key] != url:
                raise ValueError('菜单同一路径指向两个不同目标: ' + key)
            return
        data[key] = url
        entries.append({'key':key,'url':url,'labels':labels,'source':'menu'})
    for owner in menu.find_all('li', recursive=False):
        anchor = owner.find('a', recursive=False)
        if anchor is None:
            continue
        top = text(anchor)
        if is_blacklisted(urljoin(base, anchor.get('href',''))):
            continue
        roots.append(top)
        add([top], anchor['href'])
        for heading in owner.select('.submenu-test-link-list-heading'):
            group = text(heading)
            href = heading.get('href','')
            add([top,group], href if href and href != '#' else anchor['href'])
            children = heading.parent.find('menu', recursive=False)
            if children is None:
                continue
            for child in children.select('a[href]'):
                label = text(child)
                add([top,group,label],child['href'])
    entries.extend(homepage_entries(soup,base,data.values()))
    return {'entries':entries,'primary_roots':roots,'mode':'primary_column_menu'}


def classic_megamenu_entries(html, base):
    from .nav_extractor import is_blacklisted, _hierarchical_key
    soup = clean_soup(html)
    owners = soup.select('li.site-nav__expanded-item')
    if not owners or not soup.select_one('.site-nav__dropdown-link--top-level'):
        return None
    entries, data, roots = [], {}, []
    def add(labels, href):
        url=normalize_url(urljoin(base,href.strip()))
        if is_blacklisted(url) or urlsplit(url).hostname != urlsplit(base).hostname:
            return
        key=_hierarchical_key(labels)
        if key in data:
            if data[key] != url:
                raise ValueError('菜单同一路径指向两个不同目标: ' + key)
            return
        data[key]=url
        entries.append({'key':key,'url':url,'labels':labels,'source':'menu'})
    for owner in owners:
        a=owner.find('a',recursive=False)
        if a is None or is_blacklisted(urljoin(base,a.get('href',''))):continue
        top=text(a);roots.append(top);add([top],a['href'])
        for heading in owner.select('.site-nav__dropdown-link--top-level'):
            group=text(heading)
            column=heading.find_parent(class_='grid__item')
            if column is None:continue
            links=[x for x in column.select('a[href]') if x is not heading and not is_blacklisted(urljoin(base,x['href']))]
            if not links:continue
            add([top,group],heading.get('href') or a['href'])
            for child in links:add([top,group,text(child)],child['href'])
    entries.extend(homepage_entries(soup,base,data.values()))
    return {'entries':entries,'primary_roots':roots,'mode':'classic_megamenu'}


def image_column_entries(html, base):
    from .nav_extractor import is_blacklisted, _hierarchical_key
    soup=clean_soup(html)
    owners=soup.select('li.header__linklist-item')
    if not owners or not soup.select_one('.mega-menu__column'):return None
    data={};entries=[];roots=[]
    def add(labels,href):
        url=normalize_url(urljoin(base,href.strip()))
        if is_blacklisted(url) or urlsplit(url).hostname != urlsplit(base).hostname:return
        key=_hierarchical_key(labels)
        if key in data:
            if data[key] != url:raise ValueError('菜单同一路径指向两个不同目标: ' + key)
            return
        data[key]=url;entries.append({'key':key,'url':url,'labels':labels,'source':'menu'})
    for owner in owners:
        a=owner.find('a',recursive=False)
        if a is None or is_blacklisted(urljoin(base,a.get('href',''))):continue
        top=text(a);roots.append(top);add([top],a['href'])
        for column in owner.select('.mega-menu__column'):
            heading=column.find(['h2','h3','h4'],recursive=False)
            labels=[top,text(heading)] if heading else [top]
            if heading:add(labels,a['href'])
            for child in column.select('a[href]'):add(labels+[text(child)],child['href'])
        for card in owner.select('a.mega-menu__image-push[href]'):
            heading=card.find(['h2','h3','h4'])
            if heading:add([top,text(heading)],card['href'])
    entries.extend(homepage_entries(soup,base,data.values()))
    return {'entries':entries,'primary_roots':roots,'mode':'image_column_menu'}


def linked_panel_entries(html, base):
    from .nav_extractor import is_blacklisted, _hierarchical_key
    soup=clean_soup(html)
    triggers=soup.select('.header__links [data-meganav-handle]')
    if not triggers:return None
    entries=[];data={};roots=[]
    def add(labels,href):
        url=normalize_url(urljoin(base,href))
        if is_blacklisted(url) or urlsplit(url).hostname != urlsplit(base).hostname:return
        key=_hierarchical_key(labels)
        if key in data:
            if data[key] != url:raise ValueError('菜单同一路径指向两个不同目标: ' + key)
            return
        data[key]=url;entries.append({'key':key,'url':url,'labels':labels,'source':'menu'})
    for trigger in triggers:
        panel=soup.find(attrs={'data-menu-handle':trigger['data-meganav-handle']})
        if panel is None:raise ValueError('主导航触发器缺少所属面板')
        tree=panel.select_one('.meganav__list-parent')
        if tree is None:continue
        links=[a for a in tree.select('a[href]') if not is_blacklisted(urljoin(base,a['href']))]
        if not links:continue
        top=text(trigger);roots.append(top)
        aggregate=next((a for a in links if text(a).casefold().startswith(('shop all','view all'))),None)
        href=trigger.get('href','')
        if href in ('','#'):
            if aggregate is None:raise ValueError('主导航缺少实际聚合入口: '+top)
            href=aggregate['href']
        add([top],href)
        for item in tree.find_all('li',recursive=False):
            heading=item.find(['h2','h3'],recursive=False)
            group=text(heading)
            children=item.find('ul',recursive=False)
            if children:
                labels=[top,group] if group and group!='#' else [top]
                if len(labels)==2:add(labels,href)
                for a in children.select('a[href]'):add(labels+[text(a)],a['href'])
            else:
                a=item.select_one('a[href]')
                if a:add([top,text(a)],a['href'])
    entries.extend(homepage_entries(soup,base,data.values()))
    return {'entries':entries,'primary_roots':roots,'mode':'linked_panel_menu'}


def verify_primary_ownership(categories, html):
    """Independent DOM ownership checks; does not call the extraction routine."""
    from .common import CrawlError
    soup=clean_soup(html)
    triggers=soup.select('.header__links [data-meganav-handle]')
    if not triggers:return
    primary={text(t) for t in triggers}
    main=soup.select_one('main,[role=main],#MainContent')
    home_urls={normalize_url(a['href']) for a in main.select('a[href]')} if main else set()
    from urllib.parse import urlsplit
    for key,url in categories.items():
        root=key.split(',')[0]
        if root not in primary:
            path=urlsplit(url).path
            if ',' in key or key != path.rstrip('/').split('/')[-1] or not any(urlsplit(u).path==path for u in home_urls):
                raise CrawlError('目录一级不属于主导航: '+root)
