from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from functools import cached_property
from lxml import html as lxml_html
from .html_parser import normalize_event_attributes
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote


def is_blacklisted(url, label='', allow_product=False):
    path = unquote(urlsplit(url).path).lower()
    levels = label.split(',')
    leaf = levels[-1].strip()
    if len(levels) > 1 and leaf.startswith(levels[-2] + ' '):
        leaf = leaf[len(levels[-2]) + 1:]
    if path.rstrip('/') in ('', '/products', '/product', '/collections'):
        return True
    return bool(re.search(
        r'(?:^|[/_-])(?:account|login|register|cart|checkout|search|blogs?|support|about|contact|faq|shipping|returns|privacy|terms|customer_authentication)(?:$|[/_-])'
        r'|(?:^|[/_-])gift[-_ ]?(?:card|guide)s?(?:$|[/_-])'
        r'|/(?:apps|policies)(?:/|$)|(?:^|[/_-])(?:rewards?|loyalty|refer-a-friend)(?:$|[/_-])', path)
        or re.search(r'/pages(?:/|$)', path)
        or re.search(r'/products?/', path)
        or re.search(r'\bgift[ -]?(?:card|guide)s?\b', label, re.I)
        or re.fullmatch(r'email us|submit a ticket|contact us|(?:customer\s+)?support', leaf, re.I)
        or re.search(r'/(?:cdn-cgi|submit-a-ticket|store-locator|warranty)(?:/|$)', path))


_SPACE_RE = re.compile(r"\s+")
_NAV_HINT_RE = re.compile(r"(?:^|[-_ ])(?:nav|menu|mega-menu|navigation)(?:$|[-_ ])", re.I)
_GIFT_RE = re.compile(r"(?:^|[-_/ ])gift[-_ ]?(?:card|guide)s?(?:$|[-_/ ])", re.I)
_SERIES_PATH_PREFIXES = ("/collections/", "/category/", "/categories/")
_NON_SERIES_PATH_PREFIXES = {
    "/",
    "/products",
    "/product",
    "/pages",
    "/support",
    "/about-us",
    "/contact-us",
    "/search",
    "/cart",
    "/account",
    "/login",
    "/register",
    "/privacy-policy",
    "/terms",
}
_NON_SERIES_PATH_MARKERS = (
    "/products/",
    "/product/",
    "/pages/",
    "/blogs/",
    "/blog/",
    "/media/",
    "/assets/",
    "/search/",
    "/cart/",
    "/account/",
)
_NON_SERIES_SLUG_MARKERS = (
    "support",
    "about",
    "contact",
    "parts",
    "use-and-care",
    "care-guide",
    "editor",
    "editors-picks",
    "press",
    "news",
    "blog",
    "recipe",
    "recipes",
    "login", "register", "account", "faq", "shipping", "returns", "privacy", "terms", "checkout",
)
_SKIP_TAGS = {"script", "style", "template", "svg", "noscript"}
_GENERIC_HOME_LABELS = {
    "shop now",
    "view all",
    "see all",
    "learn more",
    "discover",
    "explore",
    "start shopping", "shop all", "shop", "buy now",
}
_GENERIC_MENU_LABELS = {"view all", "shop all", "see all", "all"}
_GENERIC_MENU_RE = re.compile(r"^(?:(?:view|shop|see)\s+all(?:\s+.*)?|all|shop now|explore|discover|learn more)$", re.I)
_TITLE_CLASS_RE = re.compile(
    r"^(?=.*(?:category|menu|submenu|mega))(?=.*(?:title|heading|label))",
    re.I,
)


def clean_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


def _is_generic_menu_label(value: str) -> bool:
    return bool(_GENERIC_MENU_RE.fullmatch(re.sub(r'^[\W_]+|[\W_›»>→]+$', '', clean_text(value)).strip()))


def is_label_noise(classes, value, role=''):
    """Only isolated child annotations; never strip words inside a category name."""
    return bool(re.search(r'(?:^|[-_\s])(?:badge|tag|pill|rebate|label-new)(?:$|[-_\s])', classes, re.I)
                or role == 'meter' or re.fullmatch(r'New|Sale|Free Engraving|\d+(?:\.\d+)?', clean_text(value), re.I))


def _collapse_adjacent_labels(labels: list[str]) -> list[str]:
    result: list[str] = []
    for label in labels:
        if not result or result[-1].casefold() != label.casefold():
            result.append(label)
    return result


@dataclass
class DOMNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["DOMNode"] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    parent: "DOMNode | None" = field(default=None, repr=False)

    @cached_property
    def text(self) -> str:
        own = clean_text(" ".join(self.text_parts))
        child_text = clean_text(" ".join(child.text for child in self.children
            if child.tag in ('a', 'button', 'summary') or not is_label_noise(
                child.attrs.get('class', ''), child.text, child.attrs.get('role', ''))
            or (not own and len(self.children) == 1 and not is_label_noise(
                child.attrs.get('class', ''), '', child.attrs.get('role', '')))))
        return clean_text(f"{own} {child_text}")




@dataclass
class NavigationAnalysis:
    base_url: str
    roots: list[DOMNode]
    data: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _walk(node: DOMNode):
    yield node
    for child in node.children:
        yield from _walk(child)


def _is_nav_root(node: DOMNode) -> bool:
    if node.tag == "nav" or node.attrs.get("role", "").lower() == "navigation":
        return True
    marker = f"{node.attrs.get('id', '')} {node.attrs.get('class', '')}"
    # A menu column/panel belongs to a primary trigger; the word "menu" in a
    # component class does not make it an independent navigation root.
    if re.search(r"(?:^|[-_ ])(?:column|item|link|card|panel|submenu|dropdown|flyout)(?:$|[-_ ])", marker, re.I):
        return False
    return bool(_NAV_HINT_RE.search(marker))


def _navigation_roots(document: DOMNode) -> list[DOMNode]:
    def excluded(node: DOMNode) -> bool:
        for current in (node, *_ancestors(node)):
            if current.tag in {"footer", "main-search", "output", "dialog"}:
                return True
            marker = f"{current.attrs.get('id', '')} {current.attrs.get('class', '')} {current.attrs.get('aria-label', '')}"
            if re.search(
                r"(?:^|[-_ ])(?:mobile|drawer|proxy|footer|secondary|utility|search|predictive)(?:$|[-_ ])",
                marker, re.I,
            ):
                return True
            if current.attrs.get("role", "").lower() in {"search", "dialog"}:
                return True
        return False

    semantic = [node for node in _walk(document)
                if node is not document
                and (node.tag == "nav" or node.attrs.get("role", "").lower() == "navigation")
                and not excluded(node)]
    roots = semantic or [node for node in _walk(document)
                         if node is not document and _is_nav_root(node) and not excluded(node)]
    selected: list[DOMNode] = []
    selected_ids: set[int] = set()
    for node in roots:
        if any(id(parent) in selected_ids for parent in _ancestors(node)):
            continue
        selected.append(node)
        selected_ids.add(id(node))
    if selected:
        return selected
    headers = [node for node in _walk(document) if node.tag == "header"]
    return headers[:1]


def _ancestors(node: DOMNode):
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


def _link_text(node: DOMNode) -> str:
    if node.tag == 'a':
        headings = [child for child in _walk(node) if child.tag in {'h1','h2','h3','h4','h5','h6'}]
        if len(headings) == 1 and headings[0].text:
            return clean_text(headings[0].text)
    return clean_text(node.text or node.attrs.get("aria-label", "") or node.attrs.get("title", ""))


def _anchor_nodes(root: DOMNode) -> list[DOMNode]:
    anchors = []
    for node in _walk(root):
        if node.tag != "a" or not node.attrs.get("href", "").strip():
            continue
        excluded = False
        current = node.parent
        while current is not None and current is not root:
            marker = f"{current.attrs.get('id', '')} {current.attrs.get('class', '')}"
            if (current.tag in {"main-search", "output", "dialog"}
                    or current.attrs.get("role", "").lower() in {"search", "dialog"}
                    or re.search(r"(?:^|[-_ ])(?:search|predictive)(?:$|[-_ ])", marker, re.I)):
                excluded = True
                break
            current = current.parent
        if not excluded:
            anchors.append(node)
    return anchors


def _series_url(
    href: str,
    base_url: str,
    anchor: DOMNode | None = None,
    navigation_context: bool = False,
) -> str | None:
    absolute = urljoin(base_url, href.strip())
    parts = urlsplit(absolute)
    base = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    target_host = parts.hostname or ""
    base_host = base.hostname or ""
    if target_host.startswith("www."):
        target_host = target_host[4:]
    if base_host.startswith("www."):
        base_host = base_host[4:]
    if base.netloc and target_host != base_host:
        return None
    path = parts.path or "/"
    if is_blacklisted(absolute):
        return None
    normalized_path = path.rstrip("/").lower() or "/"
    path_segments = [segment for segment in normalized_path.split("/") if segment]
    if any(
        segment in _NON_SERIES_SLUG_MARKERS
        or any(segment.startswith(marker + "-") for marker in _NON_SERIES_SLUG_MARKERS)
        for segment in path_segments
    ):
        return None
    if not normalized_path.startswith(_SERIES_PATH_PREFIXES):
        # Custom-built stores use site-specific paths.  Do not infer a
        # product series from a particular slug shape; accept arbitrary
        # same-site paths only when the link belongs to a navigation menu and
        # is not clearly a product/content/system route.
        if (
            not navigation_context
            or normalized_path in _NON_SERIES_PATH_PREFIXES
            or normalized_path.startswith(_NON_SERIES_PATH_MARKERS)
            or re.search(r"\.[a-z0-9]{2,5}$", normalized_path)
        ):
            return None
    if _GIFT_RE.search(path):
        return None
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/") or "/", parts.query, parts.fragment))






def _menu_anchor_in_item(node: DOMNode) -> DOMNode | None:
    """Return the menu label node owned by one ``li`` without nested items.

    The label may be an anchor, button, summary, heading, or a text-only
    span.  A menu title does not need to be clickable to define hierarchy.
    """
    for child in node.children:
        if child.tag == "a" and child.attrs.get("href", "").strip():
            return child
        if child.tag == "li":
            continue
        if child.tag in {"button", "summary", "h1", "h2", "h3", "h4", "h5", "h6"}:
            if _link_text(child):
                return child
        marker = child.attrs.get("class", "")
        if _TITLE_CLASS_RE.search(marker) and _link_text(child):
            return child
        found = _menu_anchor_in_item(child)
        if found is not None:
            return found
    return None


def _category_labels(anchor: DOMNode, root: DOMNode) -> list[str]:
    details_labels: list[str] = []
    for details in reversed([node for node in _ancestors(anchor) if node.tag == "details"]):
        summary = next((child for child in details.children if child.tag == "summary"), None)
        label = _link_text(summary) if summary is not None else ""
        if label and label not in details_labels:
            details_labels.append(label)
    item_ancestors = [node for node in _ancestors(anchor) if node.tag == "li"]
    if item_ancestors:
        labels: list[str] = []
        for item in reversed(item_ancestors):
            item_anchor = _menu_anchor_in_item(item)
            label = _link_text(item_anchor) if item_anchor is not None else ""
            if label and label not in labels:
                labels.append(label)
        if labels:
            labels = labels[-3:]
            anchor_label = _link_text(anchor)
            category_label = _context_category_label(anchor)
            if category_label and anchor_label in labels:
                labels = [label for label in labels if label != anchor_label]
            if category_label and category_label not in labels:
                labels.append(category_label)
            for label in details_labels:
                if label not in labels:
                    labels.append(label)
            if (
                anchor_label
                and not _is_generic_menu_label(anchor_label)
                and anchor_label not in labels
            ):
                labels.append(anchor_label)
            return labels[-3:]

    labels: list[str] = list(details_labels)
    current: DOMNode | None = anchor
    chain: list[DOMNode] = []
    while current is not None and current is not root.parent:
        chain.append(current)
        if current is root:
            break
        current = current.parent
    for node in reversed(chain):
        if node.tag != "a":
            continue
        label = _link_text(node)
        if label and not _is_generic_menu_label(label) and label not in labels:
            labels.append(label)
    category_label = _context_category_label(anchor)
    if category_label and category_label not in labels:
        labels.append(category_label)
    return labels[-3:]


def _context_category_label(anchor: DOMNode) -> str:
    """Find a category label when it is not represented by nested anchors.

    Themes commonly expose this relationship through data/ARIA attributes,
    a heading-like class, or a preceding heading sibling in a mega-menu.
    Return an empty string when the relationship cannot be established.
    """
    anchor_text = _link_text(anchor).casefold()
    in_menu_column = any(
        re.search(
            r"(?:mega[-_ ]?menu.*(?:column|group|section)|megamenu__column|"
            r"(?:nav|menu|submenu|dropdown|flyout)[-_ ]*(?:column|group|section)|"
            r"extra-column|product-extras)",
            ancestor.attrs.get("class", "").lower(),
        )
        for ancestor in _ancestors(anchor)
    )
    if (
        not _is_generic_menu_label(anchor_text)
        and "category__button" not in anchor.attrs.get("class", "").lower()
        and not in_menu_column
    ):
        return ""

    for node in (anchor, *_ancestors(anchor)):
        attrs = ("data-category", "data-category-title", "data-menu-title", "data-title")
        if node is anchor and (
            _is_generic_menu_label(anchor_text)
            or "category__button" in anchor.attrs.get("class", "").lower()
        ):
            attrs += ("aria-label", "title")
        for attr in attrs:
            value = clean_text(node.attrs.get(attr, ""))
            if value and not _is_generic_menu_label(value):
                return value

        marker = node.attrs.get("class", "")
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or _TITLE_CLASS_RE.search(marker):
            value = clean_text(node.text)
            if value and not _is_generic_menu_label(value):
                return value

        # Look only at preceding siblings in the nearest menu container.
        # Hidden mega menus frequently put a plain ``h3`` before the links;
        # visibility is irrelevant because the source DOM still proves the
        # ownership.  Walk backwards so the closest column heading wins.
        preceding = []
        for sibling in node.parent.children if node.parent else []:
            if sibling is node:
                break
            preceding.append(sibling)
        for sibling in reversed(preceding):
            sibling_marker = sibling.attrs.get("class", "")
            heading_like = (
                sibling.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
                or sibling.attrs.get("role", "").lower() == "heading"
                or bool(_TITLE_CLASS_RE.search(sibling_marker))
            )
            if heading_like:
                value = clean_text(sibling.text)
                if value and not _is_generic_menu_label(value):
                    return value

        if re.search(
            r"(?:mega[-_ ]?menu.*(?:column|group|section)|megamenu__column|"
            r"(?:nav|menu|submenu|dropdown|flyout)[-_ ]*(?:column|group|section)|"
            r"extra-column|product-extras)",
            node.attrs.get("class", "").lower(),
        ):
            for sibling in node.children:
                if sibling is anchor:
                    break
                if (
                    sibling.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
                    or sibling.attrs.get("role", "").lower() == "heading"
                    or _TITLE_CLASS_RE.search(sibling.attrs.get("class", ""))
                ):
                    value = clean_text(sibling.text)
                    if value and not _is_generic_menu_label(value):
                        return value
    return ""


def _hierarchical_key(labels: list[str]) -> str:
    if not labels:
        return ""
    levels = [labels[0]]
    for label in labels[1:]:
        levels.append(f"{levels[-1]} {label}")
    return ",".join(levels)


def _is_inside(node: DOMNode, ancestor: DOMNode) -> bool:
    current = node.parent
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _path_label(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1] if path else "Shop"
    segment = re.sub(r"[-_]+", " ", segment)
    return clean_text(segment.title()) or "Shop"


def _home_module_label(anchor: DOMNode, url: str) -> str:
    label = _link_text(anchor)
    if not label or label.casefold() in _GENERIC_HOME_LABELS:
        return _path_label(url)
    return label


def _analysis_from_rule_tree(tree: dict, base_url: str) -> NavigationAnalysis:
    """Normalize specialized theme output into the same evidence metadata."""
    entries, data = [], {}
    for raw in tree.get("entries", []):
        entry = dict(raw)
        labels = list(entry.get("labels") or [])
        # A nested generic action owns its already-resolved parent; a primary
        # nav item literally named "Shop All" remains a valid root.
        if len(labels) > 1 and _is_generic_menu_label(labels[-1]):
            labels = labels[:-1]
        labels = _collapse_adjacent_labels(labels)
        if not labels:
            continue
        key = _hierarchical_key(labels)
        url = entry["url"]
        if key in data:
            if data[key] != url:
                raise ValueError("菜单同一路径指向两个不同目标: " + key)
            continue
        entry.update({"key": key, "labels": labels, "url": url})
        entries.append(entry)
        data[key] = url
    metadata = dict(tree)
    metadata["entries"] = entries
    metadata["primary_roots"] = list(dict.fromkeys(
        entry["labels"][0] for entry in entries if entry.get("source") != "home"
    ))
    return NavigationAnalysis(base_url=base_url, roots=[], data=data, metadata=metadata)


def extract_navigation(html: str, base_url: str) -> NavigationAnalysis:
    from .menu_tree import build_tree, tree_data
    tree = build_tree(html, base_url) if base_url else None
    if tree is not None:
        from .navigation_rules import homepage_entries, clean_soup
        data = tree_data(tree)
        warnings = list(tree['issues'])
        try:
            for entry in homepage_entries(clean_soup(html), base_url, data.values()):
                if entry['key'] in data and data[entry['key']] != entry['url']:
                    raise ValueError('首页分类键冲突: ' + entry['key'])
                data[entry['key']] = entry['url']
        except ValueError as error:
            warnings.append(str(error))
        return NavigationAnalysis(base_url=base_url, roots=[], data=data, warnings=warnings,
                                  metadata={'owned_tree': tree})
    from .navigation_rules import linked_panel_entries
    if 'data-meganav-handle' in html:
        tree = linked_panel_entries(html, base_url)
        if tree is not None:
            return _analysis_from_rule_tree(tree, base_url)
    from .navigation_rules import image_column_entries
    if 'mega-menu__column' in html:
        tree = image_column_entries(html, base_url)
        if tree is not None:
            return _analysis_from_rule_tree(tree, base_url)
    from .navigation_rules import classic_megamenu_entries
    if 'site-nav__dropdown-link--top-level' in html:
        tree = classic_megamenu_entries(html, base_url)
        if tree is not None:
            return _analysis_from_rule_tree(tree, base_url)
    from .navigation_rules import column_menu_entries
    if 'submenu-test-link-list-heading' in html:
        columns = column_menu_entries(html, base_url)
        if columns is not None:
            return _analysis_from_rule_tree(columns, base_url)
    from .navigation_rules import drawer_entries, normalize_url, homepage_entries, clean_soup
    if 'sidebarMenu__mainTrigger' in html:
        drawer = drawer_entries(html, base_url)
        if drawer is not None:
            return _analysis_from_rule_tree(drawer, base_url)
    # lxml handles void elements and malformed real-world menus; scripts are not labels.
    dom = lxml_html.fromstring(normalize_event_attributes(html or '<html></html>'))
    def convert(el, parent=None):
        node = DOMNode(el.tag.lower(), dict(el.attrib), parent=parent)
        if el.text:
            node.text_parts.append(el.text)
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if child.tag.lower() not in _SKIP_TAGS:
                node.children.append(convert(child, node))
            if child.tail:
                node.text_parts.append(child.tail)
        return node
    class Document:
        root = DOMNode('document')
        root.children = [convert(dom, root)]
    parser = Document()
    inferred = base_url.strip()
    if not inferred:
        for node in _walk(parser.root):
            if node.tag == "base" and node.attrs.get("href"):
                inferred = node.attrs["href"]
                break
    if not inferred:
        for node in _walk(parser.root):
            if node.tag == "link" and node.attrs.get("rel", "").lower() == "canonical":
                inferred = node.attrs.get("href", "")
                if inferred:
                    break
    if not inferred:
        raise ValueError("无法从 HTML 推断网站基础 URL，请填写网站基础 URL")
    parts = urlsplit(inferred)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("请输入包含 http:// 或 https:// 的网站基础 URL")

    roots = _navigation_roots(parser.root)
    data: dict[str, str] = {}
    entries: list[dict] = []
    entry_positions: dict[str, int] = {}
    primary_roots: list[str] = []
    warnings: list[str] = []
    navigation_urls: set[str] = set()
    explicit_navigation_urls: set[str] = set()
    explicit_keys: set[str] = set()
    ambiguous_entries: list[tuple[str, str, str]] = []
    entry_mobile: dict[str, bool] = {}
    for root in roots:
        for anchor in _anchor_nodes(root):
            url = _series_url(
                anchor.attrs["href"], inferred, anchor, navigation_context=True
            )
            if not url:
                continue
            labels = _category_labels(anchor, root)
            resolved_labels = [x for x in labels if not _is_generic_menu_label(x)]
            generic_unresolved = _is_generic_menu_label(_link_text(anchor)) and not resolved_labels
            if generic_unresolved:
                warnings.append(
                    f"无法确认菜单层级：{url}（链接文字为通用标签，请人工确认父级）"
                )
            labels = resolved_labels
            effective_labels = _collapse_adjacent_labels(labels or [_path_label(url)])
            key = _hierarchical_key(effective_labels)
            if not key or is_blacklisted(url, key):
                continue
            normalized_url = normalize_url(url)
            mobile_copy = any(re.search(
                r"(?:^|[-_ ])(?:mobile|drawer)(?:$|[-_ ])",
                f"{node.attrs.get('id', '')} {node.attrs.get('class', '')}", re.I)
                for node in (anchor, *_ancestors(anchor))
            )
            if key in data and data[key] == normalized_url and mobile_copy and not entry_mobile.get(key, False):
                continue
            if key in data and data[key] != normalized_url:
                represented_deeper = any(
                    existing_url == normalized_url and len(existing_key.split(',')) > len(effective_labels)
                    for existing_key, existing_url in data.items()
                )
                if mobile_copy or represented_deeper:
                    continue
                if not entry_mobile.get(key, False):
                    raise ValueError("菜单同一路径指向两个不同目标: " + key)
            data[key] = normalized_url
            entry = {'key': key, 'url': normalized_url, 'labels': effective_labels,
                     'source': 'menu'}
            if key in entry_positions:
                entries[entry_positions[key]] = entry
            else:
                entry_positions[key] = len(entries)
                entries.append(entry)
            entry_mobile[key] = mobile_copy
            if effective_labels[0] not in primary_roots:
                primary_roots.append(effective_labels[0])
            navigation_urls.add(url)
            if not _is_generic_menu_label(_link_text(anchor)):
                explicit_navigation_urls.add(normalize_url(url))
                explicit_keys.add(key)
            elif generic_unresolved:
                ambiguous_entries.append((key, normalized_url, url))

    # A parentless generic action is never promoted to a path-derived root.
    # If the URL already has an explicit menu owner, drop only the ambiguous
    # duplicate; otherwise retain its warning so the browser/AI can resolve it.
    covered_ambiguous = {(key, target, raw) for key, target, raw in ambiguous_entries
                         if target in explicit_navigation_urls}
    remove_keys = {key for key, target, _ in covered_ambiguous
                   if key not in explicit_keys and data.get(key) == target}
    if remove_keys:
        entries = [entry for entry in entries if entry['key'] not in remove_keys]
        for key in remove_keys:
            data.pop(key, None)
    covered_raw = {raw for _, _, raw in covered_ambiguous}
    warnings = [warning for warning in warnings
                if not any(raw in warning for raw in covered_raw)]

    # Preserve a visible/non-clickable column heading when a three-level leaf
    # proves it.  Use the root aggregator as the structural group's target,
    # matching ``from_evidence`` and keeping depth-first order deterministic.
    existing_keys = set(data)
    expanded_entries = []
    for entry in entries:
        labels = entry.get('labels', [])
        if entry.get('source') != 'home' and len(labels) == 3:
            group_labels = labels[:2]
            group_key = _hierarchical_key(group_labels)
            if group_key not in existing_keys:
                root_key = _hierarchical_key(labels[:1])
                group_url = data.get(root_key)
                if group_url:
                    expanded_entries.append({'key': group_key, 'url': group_url,
                                             'labels': group_labels, 'source': 'menu_group'})
                    data[group_key] = group_url
                    existing_keys.add(group_key)
        expanded_entries.append(entry)
    entries = expanded_entries
    data = {entry['key']: entry['url'] for entry in entries}
    primary_roots = list(dict.fromkeys(
        entry['labels'][0] for entry in entries if entry.get('source') != 'home'
    ))

    # Only homepage category cards, with the user's flat slug naming contract.
    home = homepage_entries(clean_soup(html), base_url, data.values())
    for item in home:
        if item['key'] in data and data[item['key']] != item['url']:
            raise ValueError('首页分类键冲突: ' + item['key'])
        data[item['key']] = item['url']
        entries.append(dict(item))
    if not roots:
        warnings.append("未找到 nav/navigation/menu 节点")
    if not data:
        warnings.append("导航树中未找到可识别的集合/分类链接")
    return NavigationAnalysis(
        base_url=urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
        roots=roots,
        data=data,
        warnings=warnings,
        metadata={'entries': entries, 'primary_roots': primary_roots,
                  'mode': 'generic_static_dom'},
    )


def write_data_json(analysis: NavigationAnalysis, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(analysis.data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def analyze_html(path, base_url=''):
    return extract_navigation(Path(path).read_text(encoding='utf-8-sig'), base_url)


def _static_menu_shells(html: str, base_url: str = "") -> list[str]:
    """Return expansion controls whose category DOM is genuinely absent.

    ``hidden``/``aria-hidden``/CSS visibility are deliberately ignored.  A
    control is an empty shell only when neither its semantic owner nor an
    explicitly associated panel contains a same-site category anchor.
    """
    from .html_parser import parse_soup as BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    scopes = [node for node in soup.select("header nav,header [role=navigation],nav,[role=navigation]")
              if node.find_parent("footer") is None]
    controls = []
    for scope in scopes:
        for control in scope.select(
            "[aria-controls],[aria-haspopup],[aria-expanded],details > summary,"
            "button[class*='menu' i],button[class*='nav' i],"
            "[data-menu-trigger],[data-nav-trigger],[data-open-drawer],[data-meganav-handle]"
        ):
            # Product cards, modal quick-add controls and nested utility
            # widgets are not promises of a category submenu.
            marker = " ".join(
                str(parent.get("id", "")) + " " + " ".join(parent.get("class", []))
                for parent in [control, *list(control.parents)]
                if getattr(parent, "attrs", None) is not None and parent is not scope
            )
            if re.search(r"quick.?add|product.?card|product.?tile|add.?to.?cart|predictive.?search", marker, re.I):
                continue
            item = control.find_parent("li")
            if item is not None:
                parent_item = item.find_parent("li")
                # Nested controls remain relevant only inside a genuine menu
                # panel, not an unrelated list/widget in the header.
                if parent_item is not None:
                    panel_marker = " ".join(parent_item.get("class", [])) + " " + parent_item.get("id", "")
                    if not (_NAV_HINT_RE.search(panel_marker) or re.search(r"submenu|mega|dropdown|flyout|drawer", panel_marker, re.I)):
                        continue
            controls.append(control)
        for control in scope.find_all(True):
            if control in controls:
                continue
            if any(name.startswith("data-") and re.search(
                    r"menu|nav|drawer|target|trigger|handle", name, re.I)
                   for name in control.attrs):
                controls.append(control)
    for control in soup.select(
        "header [data-meganav-handle],header [data-menu-trigger],"
        "header [data-nav-trigger],header [data-open-drawer]"
    ):
        if control in controls:
            continue
        marker = " ".join(
            str(parent.get("id", "")) + " " + " ".join(parent.get("class", []))
            for parent in [control, *list(control.parents)]
            if getattr(parent, "attrs", None) is not None
        )
        if not re.search(r"quick.?add|product.?card|product.?tile|add.?to.?cart|predictive.?search", marker, re.I):
            controls.append(control)

    def category_anchor(node) -> bool:
        if node is None:
            return False
        for anchor in node.select("a[href]"):
            target = urljoin(base_url or "https://static.invalid/", anchor.get("href", ""))
            parts = urlsplit(target)
            if base_url and parts.hostname and parts.hostname != urlsplit(base_url).hostname:
                continue
            if re.search(r"/(?:collections|categories|category)/", parts.path, re.I) and not is_blacklisted(target):
                return True
        return False

    shells, seen = [], set()
    for control in controls:
        identity = id(control)
        if identity in seen:
            continue
        seen.add(identity)
        label = clean_text(control.get_text(" ", strip=True) or control.get("aria-label", ""))
        utility = " ".join((label, control.get("aria-controls", ""), control.get("id", ""),
                            " ".join(control.get("class", []))))
        if re.search(r"search|account|cart|bag|wishlist|country|currency|region|language|cookie|login|sign.?in|quick.?add|add.?to.?cart|about|support|contact|faq", utility, re.I):
            continue
        panels = []
        for panel_id in re.split(r"\s+", control.get("aria-controls", "").strip()):
            if panel_id:
                panel = soup.find(id=panel_id)
                if panel is not None:
                    panels.append(panel)
        details = control.find_parent("details")
        if details is not None:
            panels.append(details)
        owner = control.find_parent(["li", "menu"])
        if owner is None:
            parent = control.parent
            if parent is not None and parent.name not in {"nav", "header", "body", "html"}:
                owner = parent
        if owner is not None:
            panels.append(owner)
        # Attribute-bound panels used by shared drawers/mega menus.
        for attr, value in control.attrs.items():
            if not (attr.startswith("data-") and re.search(r"menu|nav|drawer|target|trigger|handle", attr, re.I)):
                continue
            values = value if isinstance(value, list) else [value]
            for marker in values:
                marker = clean_text(str(marker))
                if not marker or marker.casefold() in {"true", "false"}:
                    continue
                for panel in soup.find_all(attrs={"data-menu-handle": marker}):
                    panels.append(panel)
                panel = soup.find(id=marker)
                if panel is not None:
                    panels.append(panel)
        if any(category_anchor(panel) for panel in panels):
            continue
        # A category anchor with aria-haspopup but no descendants can be a
        # legitimate flat menu item; only explicit expansion state/target is
        # an empty-shell promise.
        own_href = control.get("href", "")
        own_target = urljoin(base_url or "https://static.invalid/", own_href)
        if own_href and is_blacklisted(own_target, label):
            continue
        explicit = bool(control.get("aria-controls") or control.has_attr("aria-expanded")
                        or control.name in {"button", "summary"}
                        or any(re.search(r"menu|nav|drawer|target|trigger", name, re.I)
                               for name in control.attrs if name.startswith("data-")))
        if explicit:
            shells.append(label or control.get("aria-controls", "") or control.name)
    return shells


def needs_dynamic(html: str, analysis: NavigationAnalysis) -> bool:
    """Whether source DOM is insufficient and one browser expansion is needed.

    Flat menus are valid.  Hidden-but-populated menus are valid.  Rendering is
    requested only for an empty result, an unresolved structural warning, or
    a real expansion control whose associated DOM has no category links.
    """
    if analysis is None or not getattr(analysis, "data", None):
        return True
    if getattr(analysis, "warnings", None):
        return True
    return bool(_static_menu_shells(html, getattr(analysis, "base_url", "")))


def dom_tree_static_evidence(html: str, base_url: str):
    """Compatibility entry point for the version-4 static evidence builder."""
    from .navigation_guard import dom_tree_static_evidence as build_evidence
    return build_evidence(html, base_url)


def expand_menus(url, timeout_ms=25000):
    """One isolated browser visit. Reuses old browser_read's click/hover+DOM idea."""
    from playwright.sync_api import sync_playwright
    import time
    fragments = []
    deadline = time.monotonic() + timeout_ms / 1000
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(locale='en-US', viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            if page.locator('.sidebarMenu__mainTrigger').count():
                from .navigation_rules import capture_drawers
                return [capture_drawers(page)]
            fragments.append(page.content())
            # Scope actions to menu controls, not cart/account forms or arbitrary links.
            controls = page.locator('nav button[aria-expanded],nav summary,header button[aria-controls*="menu" i],header summary,[role=navigation] button[aria-expanded]')
            for i in range(min(controls.count(), 40)):
                if time.monotonic() >= deadline:
                    raise RuntimeError('动态菜单展开达到时间上限，未宣称目录完整')
                control = controls.nth(i)
                if not control.is_visible():
                    continue
                try:
                    if control.get_attribute('aria-expanded') != 'true':
                        control.click(timeout=900)
                    control.hover(timeout=900)
                    page.wait_for_timeout(120)
                    fragments.append(page.content())
                except Exception:
                    continue
            return [fragment.replace('<html', '<html data-crawl-menu-snapshot="current-session"', 1) for fragment in fragments]
        finally:
            browser.close()
