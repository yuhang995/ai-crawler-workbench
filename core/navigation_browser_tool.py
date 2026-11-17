"""Run: python browser_tool.py actions.json

JSON: {"url":"https://store/", "actions":[
 {"action":"hover", "selector":"header button", "index":0},
 {"action":"snapshot", "name":"home-menu-supplement-001"},
 {"action":"inspect", "selector":"header"}]}
Actions: hover/click(selector,index), wait(ms), scroll(y), evaluate(script),
snapshot(name), inspect(selector). One browser context per batch. Snapshots
are unmodified page.content() plus PNG under the task's evidence directory.
"""
import json
import re
import sys
import time
import hashlib
try:
    from .browser_screenshot import screenshot
except ImportError:
    from browser_screenshot import screenshot
from pathlib import Path
from playwright.sync_api import sync_playwright


def stable_page_content(page, attempts=6):
    """Retry page.content while a storefront is finishing a client-side redirect."""
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


def run(config, root):
    root=Path(root).resolve(); evidence=root/'evidence';evidence.mkdir(exist_ok=True)
    output=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(locale='en-US',viewport=config.get('viewport',{'width':1440,'height':1000}))
        context.add_cookies([{'name':'localization','value':'US','url':config['url']},{'name':'cart_currency','value':'USD','url':config['url']}])
        page=context.new_page();page.set_default_timeout(7000)
        def snapshot(name):
            if not re.fullmatch(r'home-menu-[A-Za-z0-9_-]+',name):
                raise ValueError('snapshot name must start home-menu- and contain only letters, numbers, _ or -')
            (evidence/(name+'.html')).write_text(stable_page_content(page),encoding='utf-8')
            files={'html':str(evidence/(name+'.html'))}
            try:
                files['capture']=screenshot(page,evidence/(name+'.png'))
                files['png']=str(evidence/(name+'.png'))
                state={'url':page.url,'viewport':page.viewport_size,'scroll_y':page.evaluate('scrollY'),'captured_at':time.time(),
                       'html_sha256':hashlib.sha256((evidence/(name+'.html')).read_bytes()).hexdigest(),
                       'png_sha256':hashlib.sha256((evidence/(name+'.png')).read_bytes()).hexdigest()}
                (evidence/(name+'.state.json')).write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
            except Exception as error:
                files['screenshot_error']=str(error)
            return files
        try:
            page.goto(config['url'],wait_until='domcontentloaded',timeout=30000)
            page.wait_for_timeout(1000)
            for action in config.get('actions',[]):
                kind=action['action'];value=None
                if kind in ('hover','click'):
                    element=page.locator(action['selector']).nth(action.get('index',0))
                    getattr(element,kind)();page.wait_for_timeout(500)
                elif kind=='wait':page.wait_for_timeout(min(int(action.get('ms',500)),10000))
                elif kind=='scroll':page.evaluate('(y)=>window.scrollTo(0,y)',action.get('y',0));page.wait_for_timeout(500)
                elif kind=='evaluate':value=page.evaluate(action['script'])
                elif kind=='snapshot':value=snapshot(action['name'])
                elif kind=='inspect':
                    value=page.locator(action.get('selector','header')).evaluate_all("els=>els.map(el=>({text:el.innerText,links:[...el.querySelectorAll('a[href]')].filter(a=>a.getClientRects().length).map(a=>({text:a.innerText,url:a.href})),controls:[...el.querySelectorAll('button,[aria-haspopup]')].filter(a=>a.getClientRects().length).map(a=>({text:a.innerText,html:a.outerHTML.slice(0,1200)}))}))")
                else:raise ValueError('Unknown action: '+kind)
                output.append({'action':kind,'result':value})
            return {'ok':True,'url':page.url,'results':output}
        except Exception as error:
            try:files=snapshot('home-menu-supplement-error')
            except Exception:files={}
            return {'ok':False,'error':str(error),'results':output,**files}
        finally:browser.close()


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    result=run(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig')),Path(__file__).parent)
    print(json.dumps(result,ensure_ascii=False))
    sys.exit(0 if result['ok'] else 1)
