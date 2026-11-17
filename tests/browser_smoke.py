from pathlib import Path
from playwright.sync_api import sync_playwright
import json,time
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True)
 page=browser.new_page(viewport={'width':1280,'height':1100})
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto('http://127.0.0.1:8773')
 page.locator('input[name=site]').fill('https://blackstoneproducts.com')
 page.locator('input[name=manual]').check()
 with page.expect_response(lambda r: r.url.endswith('/api/jobs') and r.request.method=='POST') as response:
  page.get_by_role('button',name='开始采集').click()
 job_id=response.value.json()['id']
 card=page.locator('[data-job="'+job_id+'"]')
 card.get_by_role('button',name='确认范围并继续').wait_for(timeout=60000)
 assert not card.locator('.downloads').count()
 page.screenshot(path='docs/workbench-preview.png',full_page=True)
 card.get_by_role('button',name='确认范围并继续').click()
 card.get_by_role('link',name='下载独立运行包').wait_for(timeout=10000)
 with page.expect_download() as info:card.get_by_role('link',name='下载独立运行包').click()
 download=info.value;download.save_as('tests/live/browser-downloaded.zip')
 page.screenshot(path='docs/workbench.png',full_page=True)
 print(json.dumps({'page_errors':errors,'title':page.title(),'download_bytes':Path('tests/live/browser-downloaded.zip').stat().st_size}))
 assert not errors
 browser.close()

