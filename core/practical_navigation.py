"""Menu-owned categories; public inventory never supplies automatic taxonomy."""
import re
from pathlib import Path
from urllib.parse import urljoin,urlsplit
from .common import CrawlError,save_json,read_json,canonical
from .html_parser import parse_soup
from .navigation_rules import clean_soup,normalize_url
from .nav_extractor import extract_navigation,is_blacklisted
from .menu_tree import label,generic
from .shopify_inventory import collection_handle,source_inventory


def us_collection_url(url):
    return bool(re.match(r'^/(?:en(?:-us)?/)?collections/',urlsplit(url).path,re.I))


def warn(root,code,message,**details):
    path=Path(root)/'warnings.json'
    warnings=read_json(path) if path.exists() else []
    entry={'code':code,'message':message,**details}
    if entry not in warnings:warnings.append(entry)
    save_json(path,warnings)


def make_proof(categories,inventory,metadata,pruned=(),manual_sources=()):
    from .navigation_guard import labels_from_key
    roots={}
    for key,url in categories.items():
        labels=labels_from_key(key)
        branch=roots.setdefault(labels[0],{'label':labels[0],'url':categories.get(labels[0]),'links':[]})
        branch['links'].append({'labels':labels,'url':url,'source':metadata[key]['source']})
    return {'version':4,'method':'shopify_practical','complete':True,'roots':list(roots.values()),
            'entries':metadata,'inventory':inventory,'auto_pruned_roots':list(pruned),'manual_sources':list(manual_sources),
            'scope':'observed_navigation_or_explicit_manual_selection','menu_hierarchy_complete':False}


def build_practical(root,base,html,inventory):
    root=Path(root);soup=clean_soup(html);pruned=[]
    # A drawer is only discarded when an independent desktop nav exists.
    navs=soup.select('header nav,nav[aria-label],nav[role=navigation]')
    desktop=[n for n in navs if not any(re.search(r'mobile|header-drawer|menu-drawer',' '.join(p.get('class',[]))+' '+p.name+' '+p.get('id',''),re.I) for p in [n,*n.parents] if getattr(p,'name',None))]
    desktop.extend(a for a in soup.select('header a[href]') if collection_handle(urljoin(base,a['href']),base)
                   and not any(re.search(r'mobile|header-drawer|menu-drawer',' '.join(p.get('class',[]))+' '+p.name+' '+p.get('id',''),re.I)
                               for p in a.parents if getattr(p,'name',None)))
    if desktop:
        for node in list(soup.select('header-drawer,[id*=menu-drawer],[class*=mobile-menu],[class*=mobile-nav]')):
            if node.parent is not None:
                title=label(node.find(['summary','button'])) if node.find(['summary','button']) else 'Mobile Menu'
                pruned.append(title or 'Mobile Menu');node.decompose()
    cleaned=str(soup);(root/'evidence/home-practical.html').write_text(cleaned,encoding='utf-8')
    if pruned:warn(root,'mobile_roots_pruned','独立桌面导航存在，移除移动菜单副本',auto_pruned_roots=pruned)
    from .navigation_guard import dom_tree_static_evidence
    try:
        analysis=extract_navigation(cleaned,base)
        structural=dom_tree_static_evidence(cleaned,base,analysis)
    except Exception as error:
        warn(root,'menu_structure_unresolved',str(error))
        raise CrawlError('菜单归属尚未确认，禁止用集合底账平铺替代: '+str(error)) from error
    if not structural or not structural.get('complete') or analysis.warnings:
        warn(root,'menu_structure_unresolved','菜单结构不完整，需补证或人工核对')
        raise CrawlError('菜单归属尚未确认，禁止发布底账拼凑目录')
    candidate=dict(analysis.data)
    from .validator import validate_navigation
    validate_navigation(candidate,[cleaned],base,evidence=structural)
    menu_urls={e['url'] for r in structural['roots'] for e in r['links']}
    metadata={k:{'source':'observed_menu' if u in menu_urls else 'observed_home'} for k,u in candidate.items()}
    for item in inventory.get('warnings',[]):warn(root,item['code'],item['message'],url=item.get('url'))
    proof=make_proof(candidate,inventory,metadata,pruned)
    proof.update(navigation_contract='menu_owned_v1',scope='desktop_menu_and_observed_home',menu_hierarchy_complete=True,
                 structural_proof=structural,structural_categories=dict(candidate))
    save_json(root/'category_candidate.json',candidate)
    save_json(root/'evidence/navigation-observed.json',proof)
    save_json(root/'navigation_timing.json',{'mode':'practical_deterministic','cli_seconds':0,'ai_wait_seconds':0,'llm_invocations':0})
    return candidate,proof,[html,cleaned]


def validate_practical(categories,proof,documents,base):
    from .navigation_guard import labels_from_key
    if not categories:raise CrawlError('目录为空')
    if proof.get('navigation_contract')=='menu_owned_v1':
        from .validator import validate_navigation
        original=proof['structural_categories']
        validate_navigation(original,documents,base,evidence=proof['structural_proof'])
        if not proof.get('manual_confirmation') and any(original.get(k)!=u for k,u in categories.items()):
            raise CrawlError('目录名称或归属与已核验菜单不一致，底账不能替代菜单证据')
        if any(e.get('source')=='collections_feed_fallback' for e in proof['entries'].values()):
            raise CrawlError('禁止将集合底账填入菜单目录')
    actual=source_inventory(proof.get('inventory',{}),base)
    observed={canonical(normalize_url(urljoin(base,a['href']))) for html in documents for a in parse_soup(html,'lxml').select('a[href]')}
    allowed={canonical(v['url']) for v in actual.values()}|observed
    for source in proof.get('manual_sources',[]):
        if source.get('status')==200 and source.get('body') and collection_handle(source['url'],base):allowed.add(canonical(source['url']))
    entries=proof.get('entries',{})
    if set(entries)!=set(categories):raise CrawlError('目录来源条目与候选不一致')
    for key,url in categories.items():
        labels=labels_from_key(key)
        if not 1<=len(labels)<=3 or any(not t.strip() or t.isdigit() for t in labels):raise CrawlError('目录名称无效')
        if not us_collection_url(url) or not collection_handle(url,base) or is_blacklisted(url,key) or canonical(url) not in allowed:raise CrawlError('目录 URL 无真实 US 来源: '+key)
    mapped={','.join(' '.join(e['labels'][:i+1]) for i in range(len(e['labels']))):e['url'] for r in proof['roots'] for e in r['links']}
    if mapped!=categories:raise CrawlError('目录分支与映射不一致')


def validate_edits(categories,base):
    from .navigation_guard import labels_from_key
    if not isinstance(categories,dict) or not categories or len(categories)>5000:raise ValueError('请至少保留一个分类，最多 5000 行')
    for key,url in categories.items():
        if not isinstance(key,str) or not isinstance(url,str) or len(key)>500 or len(url)>2000:raise ValueError('分类名称或 URL 无效')
        labels=labels_from_key(key)
        if not 1<=len(labels)<=3 or any(not t.strip() or t.isdigit() for t in labels) or not us_collection_url(url) or not collection_handle(url,base) or is_blacklisted(url,key):raise ValueError('仅接受本站 US 商品集合及 1~3 级前缀名称')
    return categories


def apply_edits(root,categories,proof,base,client,documents):
    root=Path(root);edited=validate_edits(read_json(root/'confirmed_categories.json'),base)
    metadata={k:{'source':'manual_confirmation'} for k in edited};sources=list(proof.get('manual_sources',[]))
    known={canonical(u) for u in categories.values()}|{canonical(v['url']) for v in source_inventory(proof.get('inventory',{}),base).values()}
    for url in dict.fromkeys(edited.values()):
        if canonical(url) in known:continue
        response=client.get(url,attempts=1,timeout=10)
        if not collection_handle(response.url,base):raise CrawlError('补充分类跳转到非本站商品集合')
        from .detector import observe_market
        market=observe_market(response.text)
        if not us_collection_url(response.url) or set(market['countries'])-{'US'} or set(market['currencies'])-{'USD'}:raise CrawlError('补充分类不是 US/USD')
        for key,value in list(edited.items()):
            if value==url:edited[key]=response.url
        sources.append({'url':response.url,'status':200,'body':response.text})
    updated=make_proof(edited,proof.get('inventory',{}),metadata,proof.get('auto_pruned_roots',[]),sources)
    if proof.get('navigation_contract'):
        updated.update({k:proof[k] for k in ('navigation_contract','structural_categories','structural_proof')})
        updated['manual_confirmation']=True
    validate_practical(edited,updated,documents,base)
    save_json(root/'manual_navigation_change.json',{'before':categories,'after':edited,'source':'web_confirmation'})
    return edited,updated


def capture_preview(base,root):
    from playwright.sync_api import sync_playwright
    from .visual_capture import frame
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:
                context=browser.new_context(viewport={'width':1440,'height':1000},locale='en-US')
                context.add_cookies([{'name':'localization','value':'US','url':base},{'name':'cart_currency','value':'USD','url':base}])
                page=context.new_page()
                try:page.goto(base,wait_until='domcontentloaded',timeout=10000)
                except Exception:
                    if not page.locator('header:visible,nav:visible').count():raise
                page.wait_for_timeout(500)
                result=frame(page,Path(root)/'evidence','home-desktop')
                if not result['stable']:warn(root,'preview_header_unstable','导航在截图时变化，请结合表格核对')
            finally:browser.close()
    except Exception as error:warn(root,'preview_unavailable',str(error))
