import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.common import Client


URL = 'https://store.example/'
PROXY = 'http://alice:secret@proxy.example:8080'


class Cookie:
    def __init__(self):
        self.name = 'clearance'
        self.value = 'passed'
        self.domain = '.store.example'
        self.path = '/'
        self.raw = {'name': self.name, 'value': {'value': self.value},
                    'domain': self.domain, 'path': '/', 'httpOnly': True,
                    'secure': True, 'sameSite': 'None', 'expiry': 1893456000}


class FakePage:
    def __init__(self, url=URL, html=None):
        self.url = url
        self.html = html or ('<html><title>Store</title><nav>Shop</nav><main>'
                             + ('Visible catalog products and categories. ' * 8)
                             + '</main></html>')
        self.emulation = MagicMock()
        self.quit = MagicMock()
        self.get = MagicMock()

    def run_js(self, script):
        self.ua_script = script
        return 'Firefox Real UA'

    def get_cookies(self, all_info=False):
        self.cookies_all_info = all_info
        return [Cookie()]


class RuyiPageManualConfirmationTests(unittest.TestCase):
    def test_ruyipage_is_preferred_and_persists_cookie_and_real_ua(self):
        page = FakePage()
        module = types.ModuleType('ruyipage')
        module.launch = MagicMock(return_value=page)
        with tempfile.TemporaryDirectory() as directory:
            firefox = Path(directory) / 'firefox.exe'
            firefox.write_bytes(b'fake')
            state = Path(directory) / 'state'
            profiles = Path(directory) / 'profiles'
            client = Client(proxy=PROXY, state_dir=state)
            with patch.dict(sys.modules, {'ruyipage': module}), patch('core.common.public_url', side_effect=lambda value:value), patch.dict(
                    'os.environ', {'RUYIPAGE_FIREFOX_PATH': str(firefox),
                                   'RUYIPAGE_USER_DIR': str(profiles)}, clear=False):
                result = client.manual_browser_confirm(URL, timeout=3)

            self.assertEqual(result['engine'], 'ruyipage_firefox')
            kwargs = module.launch.call_args.kwargs
            self.assertFalse(kwargs['headless'])
            self.assertEqual(kwargs['browser_path'], str(firefox))
            self.assertEqual(kwargs['proxy'], PROXY)
            self.assertTrue(Path(kwargs['user_dir']).is_dir())
            self.assertTrue(str(Path(kwargs['user_dir']).resolve()).startswith(
                str(profiles.resolve())))
            page.emulation.set_locale.assert_called_once_with('en-US')
            page.emulation.set_timezone.assert_called_once_with('America/New_York')
            page.get.assert_called_once_with(URL, wait='none', timeout=20)
            self.assertTrue(page.cookies_all_info)
            self.assertEqual(page.ua_script, 'return navigator.userAgent')
            page.quit.assert_called_once()

            restored = Client(proxy=PROXY, state_dir=state)
            restored._load_browser_state('store.example')
            self.assertEqual(restored.browser_user_agents['store.example'], 'Firefox Real UA')
            self.assertEqual(restored.browser_cookies[0]['name'], 'clearance')

    def test_ruyipage_launch_failure_falls_back_to_playwright(self):
        module = types.ModuleType('ruyipage')
        module.launch = MagicMock(side_effect=RuntimeError('Firefox unavailable'))
        client = Client()
        expected = {'confirmed': True, 'engine': 'playwright_chromium'}
        with patch.dict(sys.modules, {'ruyipage': module}), patch('core.common.public_url', side_effect=lambda value:value), patch.object(
                client, '_manual_playwright_confirm', return_value=expected) as fallback:
            self.assertEqual(client.manual_browser_confirm(URL, timeout=9), expected)
        fallback.assert_called_once_with(URL, 9)

    def test_www_redirect_and_dormant_challenge_script_confirm_normally(self):
        page = FakePage(
            url='https://www.store.example/',
            html='<html><title>Store</title><script src="/challenge-platform/a.js"></script><nav>Shop</nav><main>'
                 + ('Visible catalog products and categories. ' * 8) + '</main></html>')
        page.get_cookies = MagicMock(return_value=[])
        module = types.ModuleType('ruyipage')
        module.launch = MagicMock(return_value=page)
        with tempfile.TemporaryDirectory() as directory:
            client = Client(state_dir=Path(directory) / 'state')
            with patch.dict(sys.modules, {'ruyipage': module}), patch(
                    'core.common.public_url', side_effect=lambda value: value):
                result = client.manual_browser_confirm(URL, timeout=3)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['engine'], 'ruyipage_firefox')


if __name__ == '__main__':
    unittest.main()
