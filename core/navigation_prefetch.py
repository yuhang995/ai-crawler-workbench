"""One bounded Python browser pass; the structure model never drives a browser."""
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from .common import save_json

STAMP = r'''() => {
 const visible=e=>{const r=e.getBoundingClientRect();if(!r.width||!r.height)return false;for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden')return false}return true};
 for(const e of document.querySelectorAll('body *')){let shown=true;for(let p=e;p&&shown;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden')shown=false}e.setAttribute('data-crawl-visible',shown?'1':'0');}
 return [...document.querySelectorAll('header a[href],nav a[href],[role=menu] a[href],[role=navigation] a[href],[class*=menu] a[href],[data-testid*=nav] a[href]')].filter(e=>!e.closest('footer,main')&&visible(e)).map(e=>({text:(e.innerText||e.getAttribute('aria-label')||'').trim(),href:e.getAttribute('href'),url:e.href}));
}'''
CONTROLS = r'''() => {
 const items=[...document.querySelectorAll('header a,header button,header summary,header [data-testid*=nav-trigger],header [aria-haspopup],nav a,nav button,[role=navigation] a,[role=navigation] button')];
 let out=[];
 for(const e of items){const r=e.getBoundingClientRect(),t=(e.innerText||e.getAttribute('aria-label')||'').trim();
 if(e.closest('menu-dropdown details')||e.closest('details:not([open])')&&!e.closest('summary'))continue;
 if(!r.width||!r.height||r.top<0||r.top>450||e.closest('main,footer')||!t||t.length>70)continue;
 let shown=true;for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden')shown=false}if(!shown)continue;
 const id='control-'+out.length;e.setAttribute('data-crawl-control',id);
 out.push({id,label:t,tag:e.tagName.toLowerCase(),controls:e.getAttribute('aria-controls'),href:e.getAttribute('href'),testid:e.getAttribute('data-testid')||''});
 }return out;
}'''


def prefetch_navigation(base, evidence, timeout=100):
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    from .navigation_guard import wait_for_storefront
    from .visual_capture import frame
    out=Path(evidence);out.mkdir(exist_ok=True,parents=True)
    started=time.monotonic();manifest={'version':1,'base_url':base,'frames':[],'errors':[],'complete':False}
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:
                ctx=browser.new_context(viewport={'width':1440,'height':1000},locale='en-US')
                ctx.add_cookies([{'name':'localization','value':'US','url':base},{'name':'cart_currency','value':'USD','url':base}])
                page=ctx.new_page()
                try:page.goto(base,wait_until='domcontentloaded',timeout=25000)
                except BrowserTimeout:
                    if urlsplit(page.url).hostname!=urlsplit(base).hostname or not page.locator('header:visible,nav:visible').count():raise
                wait_for_storefront(page,attempts=2)
                if urlsplit(page.url).hostname!=urlsplit(base).hostname:raise ValueError('导航预取离开源域名')
                page.wait_for_timeout(800)
                controls=page.evaluate(CONTROLS)
                manifest['controls']=controls
                def capture(name,owner=None):
                    links=page.evaluate(STAMP)
                    item=frame(page,out,name)
                    item.update(owner=owner,visible_links=links)
                    manifest['frames'].append(item)
                capture('home-desktop')
                seen=set()
                for control in controls:
                    label=' '.join(control['label'].split())
                    if label.isdigit() or label.casefold() in seen or re.search(r'account|sign in|search|cart|bag|currency|country|rewards|gift card|contact|support|subscribe|quiz|skip.to.content',label+' '+control['testid'],re.I):continue
                    if time.monotonic()-started>timeout:
                        manifest['errors'].append('navigation prefetch budget exceeded');break
                    seen.add(label.casefold())
                    item=page.locator('[data-crawl-control="'+control['id']+'"]')
                    try:
                        page.keyboard.press('Escape');page.mouse.move(1435,990)
                        item.hover(timeout=2000);page.wait_for_timeout(250)
                        disclosure=item.locator('xpath=ancestor::menu-dropdown[1]/details')
                        if disclosure.count()==1:
                            if disclosure.get_attribute('open') is None:
                                disclosure.locator(':scope > summary').click(timeout=2000,no_wait_after=True);page.wait_for_timeout(300)
                        elif (control['tag'] in ('button','summary') or control['controls'] or 'nav-trigger' in control['testid']) and item.get_attribute('aria-expanded')!='true':
                            item.click(timeout=2000,no_wait_after=True);page.wait_for_timeout(300)
                        capture('home-menu-prefetch-'+str(len(manifest['frames'])).zfill(2),label)
                    except Exception as error:
                        manifest['errors'].append({'label':label,'error':str(error)})
                manifest['complete']=bool(manifest['frames']) and not manifest['errors']
            finally:browser.close()
    except Exception as error:manifest['errors'].append(str(error))
    manifest['seconds']=round(time.monotonic()-started,3)
    save_json(out/'nav-prefetch.json',manifest)
    return manifest
