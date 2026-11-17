"""Bounded public collection inventory. An inventory is not a menu hierarchy."""
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin,urlsplit,unquote
from .common import save_json,CrawlError,canonical
from .nav_extractor import is_blacklisted


def collection_handle(url,base):
    p=urlsplit(url)
    if p.scheme not in ('http','https') or p.hostname!=urlsplit(base).hostname:return None
    match=re.fullmatch(r'(?:/[a-z]{2}(?:-[A-Za-z]{2})?)?/collections/([^/]+)(?:/[^/]+)?/?',p.path)
    return unquote(match[1]) if match and not is_blacklisted(url) else None


def parse_source(source,base):
    """Reparse raw responses both at ingestion and offline artifact verification."""
    entries={};children=[]
    if urlsplit(source['url']).hostname!=urlsplit(base).hostname:raise CrawlError('集合底账跨域')
    text=source['body']
    if source['kind']=='json':
        raw=json.loads(text)
        if not isinstance(raw,dict) or not isinstance(raw.get('collections'),list):raise ValueError('not collections JSON')
        for item in raw['collections']:
            handle=item.get('handle');title=item.get('title')
            if not isinstance(handle,str) or not re.fullmatch(r'[\w-]+',handle) or not isinstance(title,str) or not title.strip():continue
            url=urljoin(base,'/collections/'+handle)
            if collection_handle(url,base) and not is_blacklisted(url,title):
                entries[handle]={'title':title.strip(),'url':url,'source':source['url']}
    else:
        tree=ET.fromstring(text)
        if tree.tag.rsplit('}',1)[-1]=='sitemapindex':
            children=[n.text for n in tree.iter() if n.tag.rsplit('}',1)[-1]=='loc' and n.text
                      and re.fullmatch(r'/sitemap_collections_[^/]+\.xml',urlsplit(n.text).path)
                      and urlsplit(n.text).hostname==urlsplit(base).hostname]
        elif tree.tag.rsplit('}',1)[-1]=='urlset':
            for node in tree:
                loc=next((n.text for n in node if n.tag.rsplit('}',1)[-1]=='loc'),None)
                handle=collection_handle(loc or '',base)
                if handle and urlsplit(loc).path.startswith('/collections/'):
                    entries[handle]={'title':handle.replace('-',' ').title(),'url':loc,'source':source['url'],'title_source':'handle'}
        else:raise ValueError('not a sitemap')
    return entries,children


def fetch_inventory(client,base,out):
    started=time.monotonic();sources=[];warnings=[];entries={}
    def fetch(item):
        url,kind=item
        try:
            response=client.get(url,attempts=1,timeout=5)
            source={'url':response.url,'kind':kind,'body':response.text}
            data,children=parse_source(source,base)
            return source,data,children,None
        except Exception as error:return None,{},[],{'code':'inventory_source_unavailable','url':url,'message':str(error)}
    initial=[(urljoin(base,'/collections.json?limit=250'),'json'),(urljoin(base,'/sitemap.xml'),'xml'),(urljoin(base,'/sitemap_collections_1.xml'),'xml')]
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(fetch,initial))
    children=[]
    for source,data,links,error in results:
        if error:warnings.append(error)
        else:sources.append(source);entries.update(data);children.extend(links)
    # Actual sitemap locs commonly carry from/to query parameters.
    pending=[(u,'xml') for u in dict.fromkeys(children) if u not in {s['url'] for s in sources}]
    if len(pending)>8:warnings.append({'code':'inventory_partial','message':'集合 sitemap 超过 8 分片，保留已获取范围'})
    if pending:
        with ThreadPoolExecutor(max_workers=4) as pool:extra=list(pool.map(fetch,pending[:8]))
        for source,data,_,error in extra:
            if error:warnings.append(error)
            else:sources.append(source);entries.update(data)
    # JSON titles are preferable to names derived from sitemap handles.
    for source in sources:
        if source['kind']=='json':entries.update(parse_source(source,base)[0])
    if any(s['kind']=='json' and len(json.loads(s['body'])['collections'])==250 for s in sources) and not pending:
        warnings.append({'code':'inventory_partial','message':'集合 JSON 达 250 条且无可用 sitemap；未宣称全站完整'})
    result={'version':1,'base_url':base,'collections':entries,'sources':sources,'warnings':warnings,
            'seconds':round(time.monotonic()-started,3),'hierarchy_authority':False}
    save_json(Path(out)/'shopify_collections.json',result)
    return result


def source_inventory(inventory,base):
    result={}
    for source in inventory.get('sources',[]):result.update(parse_source(source,base)[0])
    return result
