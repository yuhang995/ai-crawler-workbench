"""Compact navigation trees plus an independent, source-backed link ledger."""
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin,urlsplit
from .common import CrawlError,save_json,read_json,canonical
from .html_parser import parse_soup as BeautifulSoup
from .navigation_rules import clean_soup,normalize_url
from .menu_tree import label,generic
from .nav_extractor import is_blacklisted


def identity(text):
    return ' '.join(re.sub(r'[^\w]+',' ',str(text).casefold()).split())


def build_digest(root,base):
    root=Path(root);directory=root/'evidence'
    prefetch=read_json(directory/'nav-prefetch.json') if (directory/'nav-prefetch.json').exists() else {}
    frame_map={f['html']:f for f in prefetch.get('frames',[])}
    paths=[directory/name for name in frame_map] or sorted(directory.glob('home*.html'))
    urls=[];url_index={};classes=[];class_index={};trees=[];ledger=[];hashes={};all_controls=set()
    def target(a):
        href=a.get('href','');url=normalize_url(urljoin(base,href))
        if (not href or urlsplit(url).hostname!=urlsplit(base).hostname or
            not re.fullmatch(r'(?:/[a-z]{2}(?:-[A-Za-z]{2})?)?/collections/[^/]+(?:/[^/]+)?/?',urlsplit(url).path) or is_blacklisted(url)):
            return None
        if url not in url_index:url_index[url]=len(urls);urls.append(url)
        return url_index[url]
    def own_text(node):
        direct=' '.join(str(t).strip() for t in node.find_all(string=True,recursive=False) if str(t).strip())
        if direct:return ' '.join(direct.split())[:100]
        if node.name in ('a','button','summary','h1','h2','h3','h4','h5','p','span','label'):
            value=label(node)
            return value[:100] if len(value)<=100 else ''
        return ''
    for path in paths:
        if not path.exists():continue
        html=path.read_text(encoding='utf-8');source='evidence/'+path.name
        hashes[source]=hashlib.sha256(path.read_bytes()).hexdigest()
        soup=clean_soup(html)
        for node in soup.select('script,style,svg,template,noscript'):node.decompose()
        frame=frame_map.get(path.name,{});owner=frame.get('owner')
        visible_pairs=Counter((identity(n['text']),canonical(n['url'])) for n in frame.get('visible_links',[]))
        original_pairs=Counter((identity(n['text']),canonical(n['url'])) for n in prefetch.get('frames',[{}])[0].get('visible_links',[])) if prefetch.get('frames') else Counter()
        containers=list(soup.select('header,nav,[role=navigation]'))
        containers=[n for n in containers if not n.find_parent(['header','nav']) and not n.find_parent(['main','footer'])]
        for control in soup.select('[aria-controls]'):
            if not control.find_parent(['header','nav']) and not control.get('data-crawl-control'):continue
            if control.get('data-crawl-visible')=='0':continue
            panel=soup.find(id=control.get('aria-controls'))
            if panel is not None and not any(p is panel or p in panel.parents for p in containers):containers.append(panel)
        # Portalled menus may be outside header with no ARIA relationship.
        if owner:
            for n in soup.select('[role=menu],[class*=megamenu],[class*=mega-menu],[data-testid*=nav]'):
                if n.get('data-crawl-visible')=='1' and not any(p is n or p in n.parents for p in containers):containers.append(n)
        text_pool=set()
        for container in containers:
            for n in container.find_all(['a','button','summary','h1','h2','h3','h4','h5','p','span','label']):
                if n.get('data-crawl-visible')=='0':continue
                t=own_text(n)
                if t and len(t)<80:text_pool.add(t)
        controls={c['label'].strip() for c in prefetch.get('controls',[]) if c.get('label')}
        controls.update(own_text(c) for c in soup.select('header button,header summary,nav > a,nav > ul > li > a'))
        controls.discard('');all_controls.update(controls)
        def tree(node,ancestors=()):
            if not getattr(node,'name',None) or node.name in ('footer','script','style','svg','template'):return None
            # A hidden mobile copy is not evidence for a desktop branch.
            if node.get('data-crawl-visible')=='0' and not node.select_one('[data-crawl-visible="1"]'):return None
            text=own_text(node);cls=' '.join(c for c in node.get('class',[]) if re.search(r'nav|menu|panel|column|group|desktop|mobile|drawer',c,re.I))[:100]
            heading=text if node.name in ('button','summary','h1','h2','h3','h4','h5','p','span','label') else ''
            context=tuple(dict.fromkeys((*ancestors,heading))) if heading else ancestors
            uid=target(node) if node.name=='a' else None
            children=[] if uid is not None else [v for child in node.children if (v:=tree(child,context)) is not None]
            if uid is not None:
                leaf=label(node);pair=(identity(leaf),canonical(urls[uid]))
                root_hints=[]
                if owner and visible_pairs[pair]>original_pairs[pair]:root_hints.append(owner)
                for parent in node.parents:
                    if parent.name in ('li','details'):
                        trigger=parent.find(['a','button','summary'],recursive=False)
                        if trigger and trigger is not node:
                            t=label(trigger)
                            if t and len(t)<80:root_hints.append(t)
                    if parent.name in ('header','nav'):break
                if not root_hints:root_hints=[leaf]
                nearby=[]
                for parent in list(node.parents)[:4]:
                    nearby.extend(label(h) for h in parent.find_all(['h2','h3','h4','p','label'],recursive=False))
                alternatives=list(dict.fromkeys([leaf,*nearby]))
                ledger.append({'url_id':uid,'leaf':leaf,'alternatives':alternatives,'owners':list(dict.fromkeys(root_hints)),
                               'context':list(dict.fromkeys([*context,*nearby,*text_pool])),'source':source,'kind':'menu'})
            if uid is None and not children and not heading and not node.get('aria-controls'):return None
            if uid is None and not text and not re.search(r'nav|menu|column|group|panel',cls,re.I) and node.name not in ('header','nav','ul','li'):
                return children[0] if len(children)==1 else ['group','',-1,-1,children] if children else None
            if cls not in class_index:class_index[cls]=len(classes);classes.append(cls[:180])
            value=[node.name,text,class_index[cls],uid if uid is not None else -1,children]
            if node.get('aria-controls'):value.append(node['aria-controls'])
            return value
        forest=[v for node in containers if (v:=tree(node)) is not None]
        encoded=json.dumps(forest,ensure_ascii=False,separators=(',',':'))
        if forest and not any(t['signature']==encoded for t in trees):trees.append({'owner':owner,'tree':forest,'signature':encoded})
        for a in soup.select('main a[href],[role=main] a[href]'):
            if a.get('data-crawl-visible')=='0' or a.find_parent(['header','nav','footer']):continue
            uid=target(a)
            if uid is None:continue
            name=label(a);near=[]
            for parent in list(a.parents)[:3]:near.extend(label(n) for n in parent.find_all(['h1','h2','h3','h4'],recursive=False))
            ledger.append({'url_id':uid,'leaf':name,'alternatives':[name,*near,urlsplit(urls[uid]).path.rstrip('/').split('/')[-1]],'owners':[],
                           'context':near,'source':source,'kind':'home'})
    menu_ids={x['url_id'] for x in ledger if x['kind']=='menu'}
    home=list(dict.fromkeys((x['leaf'],x['url_id']) for x in ledger if x['kind']=='home' and x['url_id'] not in menu_ids))
    origin=urlsplit(base);prefix=origin.scheme+'://'+origin.netloc
    digest={'version':1,'base':prefix,'urls':[u[len(prefix):] if u.startswith(prefix) else u for u in urls],
            'node_format':['tag','text','class_id','url_id','children','optional_aria_controls'],
            'classes':classes,'menus':[{k:v for k,v in t.items() if k!='signature'} for t in trees],'home':home}
    compact=json.dumps(digest,ensure_ascii=False,separators=(',',':'))
    (directory/'nav_digest.json').write_text(compact,encoding='utf-8')
    save_json(directory/'nav-ledger.json',{'version':1,'base':base,'urls':urls,'records':ledger,'controls':sorted(all_controls),'source_hashes':hashes,
              'digest_sha256':hashlib.sha256(compact.encode()).hexdigest(),'digest_bytes':len(compact.encode()),'complete':prefetch.get('complete',True)})
    return digest


def candidate_proof(root,entries):
    """Build audit metadata from model labels plus host-owned URLs and sources."""
    from .navigation_guard import labels_from_key
    root=Path(root);ledger=read_json(root/'evidence/nav-ledger.json');digest=root/'evidence/nav_digest.json'
    if hashlib.sha256(digest.read_bytes()).hexdigest()!=ledger['digest_sha256']:raise CrawlError('导航摘要已变化')
    content=read_json(digest)
    if [urljoin(content['base'],u) for u in content['urls']]!=ledger['urls']:raise CrawlError('URL 索引与摘要不一致')
    observed={}
    for name,sha in ledger['source_hashes'].items():
        if (root/name).resolve().parent!=(root/'evidence').resolve():raise CrawlError('摘要来源路径无效')
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=sha:raise CrawlError('摘要来源已变化')
        soup=BeautifulSoup((root/name).read_text(encoding='utf-8'),'lxml')
        observed[name]={canonical(normalize_url(urljoin(ledger['base'],a['href']))) for a in soup.select('a[href]')}
    scope=read_json(root/'navigation_review_scope.json');confirmed=scope.get('confirmed_candidate',{})
    if not ledger.get('complete'):raise CrawlError('程序尚未完成菜单预取，不能让 AI 猜测缺失分支')
    candidate=dict(confirmed);chosen={};records=ledger['records']
    for item in entries:
        labels=item['labels'];index=item['url_id']
        if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(ledger['urls']):raise CrawlError('AI 返回未观察到的 URL 索引')
        if not 1<=len(labels)<=3 or any(not isinstance(t,str) or not t.strip() or ',' in t or t.isdigit() for t in labels):raise CrawlError('AI 目录层级无效')
        labels=[t.strip() for t in labels];key=','.join(' '.join(labels[:i+1]) for i in range(len(labels)));url=ledger['urls'][index]
        if key in confirmed and confirmed[key]!=url:raise CrawlError('AI 修改程序确定项')
        if is_blacklisted(url,key):raise CrawlError('AI 目录包含黑名单')
        if key in candidate and candidate[key]!=url:raise CrawlError('AI 目录路径冲突')
        matches=[]
        for rec in records:
            if rec['url_id']!=index:continue
            if canonical(url) not in observed.get(rec['source'],set()):continue
            names={identity(t) for t in rec['alternatives'] if t}
            if identity(labels[-1]) not in names:continue
            if rec['kind']=='home':
                if len(labels)==1:matches.append(rec)
                continue
            if identity(labels[0]) not in {identity(t) for t in rec['owners']} and key not in confirmed:continue
            context={identity(t) for t in rec['context']}
            if any(identity(t) not in context for t in labels[1:-1]):continue
            matches.append(rec)
        if not matches and key not in confirmed:raise CrawlError('目录标题或归属没有摘要来源: '+key)
        candidate[key]=url;chosen[key]=matches
    if not candidate:raise CrawlError('AI 未给出有效目录')
    needed={ledger['urls'][r['url_id']] for r in records}
    if needed-set(candidate.values()):raise CrawlError('AI 遗漏已观察的导航链接: '+str(sorted(needed-set(candidate.values()))[:5]))
    roots={}
    for key,url in candidate.items():
        labels=labels_from_key(key);sources=chosen.get(key) or [r for r in records if ledger['urls'][r['url_id']]==url]
        if not sources:raise CrawlError('已确认目录来源缺失')
        files=sorted({r['source'] for r in sources});kind='home' if all(r['kind']=='home' for r in sources) else 'menu'
        branch=roots.setdefault(labels[0],{'label':labels[0],'url':candidate.get(labels[0]),'kind':kind,'links':[],'source_files':[]})
        branch['links'].append({'labels':labels,'url':url,'model':False,'source_files':files})
        branch['source_files']=sorted(set(branch['source_files'])|set(files))
    issues=[i['id'] for i in scope.get('issues',[])];files=sorted(ledger['source_hashes'])
    decision={'version':2,'complete':True,'confirmed_unchanged':True,'resolved_issue_ids':issues,'unresolved':[],
              'reviewed_roots':sorted(set(roots)|set(scope.get('uncertain_roots',[]))|{r for i in scope.get('issues',[]) for r in i.get('roots',[])}),
              'summary':'Python 对照原始导航摘要生成证明并核验来源；模型只提供结构标签与 URL 索引。'}
    proof={'version':4,'method':'ai_skill','complete':True,'roots':list(roots.values()),'digest_version':1,
           'review':{'complete':True,'unresolved':[],'skill':'category-json','scope_version':2,'resolved_issue_ids':issues,'confirmed_unchanged':True,
                     'branches':[{'label':r['label'],'kind':r['kind'],'source_files':r['source_files']} for r in roots.values()],
                     'corrections':[{'issue_id':i,'action':'replace','source_files':files,'reason':decision['summary']} for i in issues]}}
    return candidate,proof,decision
