"""Bounded desktop screenshots paired with DOM, coordinates and provenance."""
import hashlib
import time
from pathlib import Path
from urllib.parse import urlsplit
from .common import save_json, read_json

VISIBLE_DOM = """() => {
 const all=[...document.querySelectorAll('*')];
 return [...document.querySelectorAll('header a,header button,header summary,nav a,nav button,main a,main h1,main h2,main h3,[role=main] a')].map(el=>{
 const r=el.getBoundingClientRect(); let shown=r.width>0&&r.height>0;
 for(let p=el;p&&shown;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')shown=false;}
 const ancestors=[];for(let p=el.parentElement;p&&ancestors.length<6;p=p.parentElement){const s=getComputedStyle(p);ancestors.push({tag:p.tagName.toLowerCase(),id:p.id,class:p.className?.toString().slice(0,180),display:s.display,visibility:s.visibility});}
 return {id:'node-'+all.indexOf(el),tag:el.tagName.toLowerCase(),text:(el.innerText||el.getAttribute('aria-label')||'').trim().slice(0,180),href:el.getAttribute('href'),url:el.href||null,rendered:shown,in_viewport:shown&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth,bbox:{x:r.x,y:r.y,width:r.width,height:r.height},in_header:!!el.closest('header')||(!!el.closest('nav,[role=navigation]')&&!el.closest('main,footer,[role=main]')),controls:el.getAttribute('aria-controls'),expanded:el.getAttribute('aria-expanded'),ancestors};
 });
}"""


def frame(page, out, name, full_page=False):
    for attempt in range(3):
        result=_frame_once(page,out,name,full_page)
        result['attempts']=attempt+1
        if result['stable']:
            return result
        if attempt<2:page.wait_for_timeout(350)
    return result


def _frame_once(page, out, name, full_page=False):
    from .navigation_guard import stable_page_content
    out = Path(out)
    html = stable_page_content(page)
    nodes = page.evaluate(VISIBLE_DOM)
    state = page.evaluate('() => ({url:location.href,scroll_y:scrollY,height:document.documentElement.scrollHeight,viewport:{width:innerWidth,height:innerHeight},currency:window.Shopify?.currency?.active||null,country:window.Shopify?.country||null})')
    (out/(name+'.html')).write_text(html, encoding='utf-8')
    from .browser_screenshot import screenshot
    capture=screenshot(page,out/(name+'.png'),full_page=full_page)
    after = page.evaluate(VISIBLE_DOM)
    # Rotating homepage cards are expected. Only the desktop navigation must
    # retain the same rendered state across the paired capture.
    signature = lambda ns: [(n['id'],n['text'],n['href'],n['bbox']) for n in ns if n.get('in_header') and n['in_viewport']]
    record = {'id':name,'html':name+'.html','png':name+'.png','dom':name+'.json','state':state,
              'captured_at':time.time(),'stable':signature(nodes)==signature(after),'nodes':nodes,'capture':capture}
    save_json(out/(name+'.json'),record)
    return {k:v for k,v in record.items() if k!='nodes'}


def capture_visual_snapshots(base, out, max_segments=16):
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    from .navigation_guard import wait_for_storefront
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    started=time.monotonic()
    manifest={'version':1,'base_url':base,'scope':'desktop_primary_and_homepage','frames':[],
              'coverage_complete':False,'market_preferences':{'locale':'en-US','country':'US','currency':'USD'}}
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:
                context=browser.new_context(viewport={'width':1440,'height':1000},locale='en-US')
                context.add_cookies([{'name':'localization','value':'US','url':base},{'name':'cart_currency','value':'USD','url':base}])
                page=context.new_page()
                try:
                    page.goto(base,wait_until='domcontentloaded',timeout=25000)
                except BrowserTimeout:
                    if urlsplit(page.url).hostname!=urlsplit(base).hostname or not page.locator('header:visible,nav:visible,[role=navigation]:visible').count():
                        raise
                # A visible header is sufficient for menu inspection, but not
                # for claiming the entire homepage has been captured.
                page.wait_for_function("document.readyState !== 'loading'",timeout=12000)
                wait_for_storefront(page,attempts=2)
                page.wait_for_timeout(800)
                if urlsplit(page.url).hostname!=urlsplit(base).hostname:
                    raise ValueError('截图页面重定向到不同域名')
                manifest['frames'].append(frame(page,out,'home-visual-desktop'))
                # Scroll incrementally to trigger lazy loading. Uncovered tails
                # are explicit, never silently declared complete.
                covered=0
                for index in range(max_segments):
                    page.evaluate('(y)=>scrollTo(0,y)',index*850)
                    page.wait_for_timeout(250)
                    item=frame(page,out,f'home-visual-section-{index:02d}')
                    manifest['frames'].append(item)
                    covered=max(covered,item['state']['scroll_y']+1000)
                    if covered>=item['state']['height']:
                        manifest['coverage_complete']=True;break
                page.evaluate('scrollTo(0,0)');page.wait_for_timeout(200)
                height=page.evaluate('document.documentElement.scrollHeight')
                manifest['document_height']=height;manifest['covered_height']=covered
                if height<=8000:
                    manifest['frames'].append(frame(page,out,'home-visual-full',full_page=True))
                else:
                    manifest['full_image_omitted']='height exceeds 8000px; use bounded segments'
                manifest['coverage_complete'] &= covered>=height
                manifest['stable']=all(f['stable'] for f in manifest['frames'])
            finally:
                browser.close()
    except Exception as error:
        manifest['error']=str(error)
        manifest['coverage_complete']=False
    manifest['seconds']=round(time.monotonic()-started,3)
    manifest['hashes']={f[name]:hashlib.sha256((out/f[name]).read_bytes()).hexdigest()
                        for f in manifest['frames'] for name in ('html','png','dom')}
    save_json(out/'visual-manifest.json',manifest)
    return manifest
