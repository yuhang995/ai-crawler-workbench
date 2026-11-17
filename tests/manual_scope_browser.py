"""Browser smoke of the actual workbench editor using an isolated waiting job."""
import sys
import tempfile
import threading
import shutil
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from core.common import read_json
from playwright.sync_api import sync_playwright


def main():
    with tempfile.TemporaryDirectory() as directory,patch.object(app,'RUNS',Path(directory)),patch.object(app,'JOBS',{}):
        root=Path(directory)/'preview';(root/'evidence').mkdir(parents=True)
        source=Path('runs/7d3db9b2e4e8e0e4/evidence/home-visual-desktop.png')
        if source.exists():shutil.copyfile(source,root/'evidence/home-desktop.png')
        job={'id':'preview','site':'https://example.com/','status':'awaiting_confirmation','manual':True,
             'confirm':threading.Event(),'events':[],'categories':{'Shop':'https://example.com/collections/shop','Extra':'https://example.com/collections/extra'}}
        app.JOBS['preview']=job
        server=app.LocalServer(('127.0.0.1',0),app.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':1100})
                    page.goto(f'http://127.0.0.1:{server.server_port}')
                    editor=page.locator('[data-editor=preview]');editor.wait_for()
                    editor.locator('[data-row="0"] [data-field=key]').fill('Renamed Shop')
                    page.wait_for_timeout(1500)
                    assert editor.locator('[data-row="0"] [data-field=key]').input_value()=='Renamed Shop'
                    editor.locator('[data-row="1"] [data-field=checked]').uncheck()
                    page.locator('[data-add-scope]').click()
                    editor.locator('[data-row="2"] [data-field=key]').fill('New Collection')
                    editor.locator('[data-row="2"] [data-field=url]').fill('https://example.com/collections/new')
                    Path('exports').mkdir(exist_ok=True)
                    page.screenshot(path='exports/practical-manual-ui.png',full_page=True)
                    page.locator('[data-confirm]').click()
                    page.wait_for_timeout(500)
                    assert job['confirm'].is_set()
                    assert read_json(root/'confirmed_categories.json')=={'Renamed Shop':'https://example.com/collections/shop','New Collection':'https://example.com/collections/new'}
                    print('PASS: rename survives refresh; unchecked row removed; added URL submitted; confirmation wakes job')
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':main()
