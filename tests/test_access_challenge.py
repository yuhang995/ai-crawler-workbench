import unittest
from unittest.mock import Mock
from core.navigation_guard import is_access_challenge, wait_for_storefront
from core.common import CrawlError

class AccessChallengeTests(unittest.TestCase):
    def test_cloudflare_script_on_store_is_not_interstitial(self):
        self.assertFalse(is_access_challenge('<title>Store</title><nav>Shop</nav><script src="/cdn-cgi/challenge-platform/script.js"></script>'))
    def test_challenge_clears_before_menu_read(self):
        page=Mock();page.content.side_effect=['<title>Just a moment...</title>','<title>Store</title><nav>Shop</nav>']
        wait_for_storefront(page,attempts=1)
        page.wait_for_timeout.assert_called_once_with(2000)
    def test_persistent_challenge_has_specific_failure(self):
        page=Mock();page.content.return_value='<form id="challenge-form"></form>'
        with self.assertRaisesRegex(CrawlError,'Cloudflare'):wait_for_storefront(page,attempts=1)
