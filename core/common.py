"""Small shared IO and bounded, thread-local HTTP transport."""
import ipaddress
import html as html_lib
import hashlib
import json
import logging
import os
import random
import re
import socket
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote
from curl_cffi import requests


LOGGER = logging.getLogger(__name__)


class CrawlError(RuntimeError):
    pass


class AdapterRequired(CrawlError):
    """Structural incompatibility, eligible for exactly one AI session."""


class AccessChallenge(CrawlError):
    """The public storefront returned an interactive access challenge."""

    def __init__(self, message, url=None):
        super().__init__(message)
        self.url = url


def validate_proxy(value):
    """Validate a conventional HTTP/SOCKS proxy without resolving targets."""
    if value is None or str(value).strip() == '':
        return None
    value = str(value).strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError('代理地址格式无效') from error
    if (parsed.scheme.casefold() not in {'http', 'https', 'socks5'}
            or not parsed.hostname or port is None or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise ValueError('代理仅支持 http://、https:// 或 socks5://host:port')
    return value


def proxy_from_environment():
    for name in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                 'ALL_PROXY', 'all_proxy'):
        if os.environ.get(name):
            return validate_proxy(os.environ[name])
    return None


def redact_proxy(value):
    if not value:
        return None
    parsed = urlsplit(value)
    credentials = ''
    if parsed.username is not None:
        credentials = parsed.username + (':***' if parsed.password is not None else '') + '@'
    host = '[' + parsed.hostname + ']' if ':' in (parsed.hostname or '') else parsed.hostname
    return f'{parsed.scheme}://{credentials}{host}:{parsed.port}'


def redact_sensitive(value, proxy=None):
    text = str(value)
    if proxy:
        text = text.replace(proxy, redact_proxy(proxy))
        parsed = urlsplit(proxy)
        if parsed.password:
            text = text.replace(parsed.password, '***')
    return text


def playwright_proxy(value):
    if not value:
        return None
    parsed = urlsplit(value)
    result = {'server': f'{parsed.scheme}://{parsed.hostname}:{parsed.port}'}
    if parsed.username is not None:
        result['username'] = unquote(parsed.username)
    if parsed.password is not None:
        result['password'] = unquote(parsed.password)
    return result


_CHALLENGE_TEXT = re.compile(
    r'just a moment|verify (?:you are )?human|checking (?:your )?browser|'
    r'attention required!\s*\|\s*cloudflare|access denied|forbidden', re.I)


def _same_www_site(left, right):
    """Accept only the ordinary apex <-> www canonical hostname redirect."""
    if not left or not right:
        return False
    normalize = lambda value: value.casefold().removeprefix('www.')
    return normalize(left) == normalize(right)


def is_access_challenge_response(response):
    """Detect an interstitial without treating ordinary 403 pages as solved.

    Cloudflare documents ``cf-mitigated: challenge`` as the authoritative
    response signal.  The bounded body check covers older WAF interstitials
    which return either 403 or a misleading 200 HTML response.
    """
    headers = getattr(response, 'headers', {}) or {}
    if str(headers.get('cf-mitigated', '')).casefold() == 'challenge':
        return True
    body = getattr(response, 'text', '') or ''
    if not isinstance(body, str):
        body = ''
    content_type = str(headers.get('content-type', '')).casefold()
    is_html = 'text/html' in content_type or body.lstrip().lower().startswith(('<!doctype html', '<html'))
    status = getattr(response, 'status_code', 0)
    if status not in {200, 403} or not is_html:
        return False
    # Storefronts commonly ship dormant Cloudflare/Turnstile/Recaptcha code in
    # otherwise valid pages.  Script source is not evidence that the rendered
    # document is an interstitial.  Inspect the title, actual challenge form,
    # and bounded visible text instead.
    title_match = re.search(r'(?is)<title[^>]*>(.*?)</title>', body)
    title = re.sub(r'\s+', ' ', re.sub(r'(?s)<[^>]+>', ' ', title_match.group(1))).strip() if title_match else ''
    title_is_challenge = bool(_CHALLENGE_TEXT.fullmatch(title))
    if title_is_challenge:
        return True
    visible = re.sub(r'(?is)<!--.*?-->|<(?:script|style|svg|noscript)\b.*?</(?:script|style|svg|noscript)>', ' ', body)
    visible = re.sub(r'(?s)<[^>]+>', ' ', visible)
    visible = re.sub(r'\s+', ' ', visible).strip()
    # Real interstitials contain little visible storefront content.  This also
    # prevents a footer phrase such as "access denied" or a CAPTCHA script from
    # poisoning a complete page.
    short_page = len(visible) <= 20_000
    challenge_widget = bool(re.search(
        r'(?is)\b(?:id|class)=["\'][^"\']*(?:challenge-form|challenge-running|challenge-stage|cf-turnstile)[^"\']*["\']',
        body))
    return short_page and (challenge_widget or bool(_CHALLENGE_TEXT.search(visible)))


def visible_html_text(body):
    """Return rendered-ish text without dormant scripts or markup noise."""
    body = body if isinstance(body, str) else ''
    visible = re.sub(
        r'(?is)<!--.*?-->|<(?:script|style|svg|noscript|template)\b.*?</(?:script|style|svg|noscript|template)>',
        ' ', body)
    visible = re.sub(r'(?s)<[^>]+>', ' ', visible)
    return re.sub(r'\s+', ' ', html_lib.unescape(visible)).strip()


def manual_confirmation_ready(response, minimum_visible=200):
    """Require a substantive rendered page in addition to no challenge."""
    if is_access_challenge_response(response):
        return False
    visible = visible_html_text(getattr(response, 'text', '') or '')
    if len(visible) < minimum_visible:
        return False
    # Navigation placeholders can be long while the actual document is still
    # replacing itself.  Reject the common single-state loading messages.
    normalized = visible.casefold().strip(' .…')
    if re.fullmatch(r'(?:loading|loading page|please wait|redirecting|initializing)(?:[ .…]*)', normalized):
        return False
    return True


def retry_after_seconds(value, now=None, maximum=45.0):
    """Parse Retry-After delta-seconds or an HTTP date, bounded for workers."""
    if value is None or str(value).strip() == '':
        return None
    raw = str(value).strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            seconds = (target - current).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, min(float(maximum), seconds))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def split_category_key(value):
    """Split compact category paths without destroying punctuation in labels.

    Category path separators are emitted without whitespace (``Shop,Shop
    Sauces``), while a visible comma in a label retains its following space
    (``Collagen, Whey & Supplements``).  Keeping that distinction lets display
    text round-trip exactly while preserving the existing JSON key format.
    """
    normalized = str(value).replace('，', ',')
    return [part.strip() for part in re.split(r',(?=\S)', normalized) if part.strip()]


def canonical(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', p.query, p.fragment))


def query(url, **params):
    p = urlsplit(url)
    # Filters may repeat the same name (e.g. blue OR navy). Updating page/limit
    # must not collapse these into a single value.
    q = [(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k not in params]
    q.extend((k,str(v)) for k,v in params.items())
    return urlunsplit(p._replace(query=urlencode(q), fragment=''))


def public_url(url):
    p = urlsplit(url)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password:
        raise CrawlError('请输入公开 http/https 站点 URL')
    if p.port not in (None, 80, 443):
        raise CrawlError('仅接受公开网站的 80/443 端口')
    for addr in socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(addr[4][0]).is_global:
            raise CrawlError('不允许访问本机或私网地址')
    return url


class Client:
    _domain_locks = {}
    _domain_locks_guard = threading.Lock()

    def __init__(self, timeout=20, proxy=None, use_env_proxy=False, state_dir=None):
        self.timeout = timeout
        self.proxy = validate_proxy(proxy) if proxy else (proxy_from_environment() if use_env_proxy else None)
        self.state_dir = Path(state_dir) if state_dir else Path(__file__).resolve().parents[1] / 'browser_state'
        self.local = threading.local()
        self.cache = {}
        self.lock = threading.Lock()
        self.min_interval = 0.15
        self.rate_lock = threading.Lock()
        self.next_request_at = 0.0
        self.parallel_pacing = False
        self.retry_not_before = 0.0
        self.browser_attempted = set()
        self.browser_cookies = []
        self.browser_user_agents = {}
        self.recovery_attempted_domains = set()

    @classmethod
    def domain_lock(cls, hostname):
        with cls._domain_locks_guard:
            return cls._domain_locks.setdefault(hostname.casefold(), threading.Lock())

    def _new_session(self, hostname=None):
        options = {'impersonate': 'firefox'}
        if self.proxy:
            options['proxy'] = self.proxy
        session = requests.Session(**options)
        session.headers.update({'Accept-Language': 'en-US,en;q=0.9'})
        session.cookies.update({'localization': 'US', 'cart_currency': 'USD'})
        if hostname and hostname in self.browser_user_agents:
            session.headers.update({'User-Agent': self.browser_user_agents[hostname]})
        return session

    def _session(self, hostname):
        self._load_browser_state(hostname)
        sessions = getattr(self.local, 'sessions', None)
        if sessions is None:
            sessions = self.local.sessions = {}
            legacy = getattr(self.local, 'session', None)
            if legacy is not None:
                sessions[hostname] = legacy
        session = sessions.get(hostname)
        if session is None:
            session = sessions[hostname] = self._new_session(hostname)
            # Preserve the historical attribute for callers/tests which
            # intentionally inspect or replace the current thread's session.
            self.local.session = session
        return session

    def _state_path(self, hostname):
        proxy_authority = ''
        if self.proxy:
            parsed = urlsplit(self.proxy)
            proxy_authority = f'{parsed.scheme}://{parsed.hostname}:{parsed.port}'
        identity = hashlib.sha256(proxy_authority.encode()).hexdigest()[:12]
        safe_host = re.sub(r'[^a-z0-9.-]+', '_', hostname.casefold())
        return self.state_dir / f'{safe_host}-{identity}.json'

    def _load_browser_state(self, hostname):
        if hostname in self.browser_user_agents:
            return
        path = self._state_path(hostname)
        if not path.is_file():
            return
        try:
            state = read_json(path)
            cookies = state.get('cookies', [])
            user_agent = state.get('user_agent')
            if not isinstance(cookies, list) or not isinstance(user_agent, str):
                return
            valid = [cookie for cookie in cookies if isinstance(cookie, dict)
                     and isinstance(cookie.get('name'), str)
                     and isinstance(cookie.get('value'), str)
                     and isinstance(cookie.get('domain'), str)]
            with self.lock:
                self.browser_cookies.extend(cookie for cookie in valid
                    if cookie not in self.browser_cookies)
                self.browser_user_agents[hostname] = user_agent
        except (OSError, ValueError, TypeError):
            return

    def _save_browser_state(self, hostname, cookies, user_agent):
        path = self._state_path(hostname)
        path.parent.mkdir(parents=True, exist_ok=True)
        save_json(path, {'version': 1, 'hostname': hostname,
                         'cookies': cookies, 'user_agent': user_agent})

    def _store_browser_state(self, hostname, cookies, user_agent):
        selected = [c for c in cookies if hostname == c.get('domain', '').lstrip('.')
                    or hostname.endswith('.' + c.get('domain', '').lstrip('.'))]
        with self.lock:
            self.browser_cookies = selected
            self.browser_user_agents[hostname] = user_agent
        self._save_browser_state(hostname, selected, user_agent)

    def pace(self):
        while True:
            with self.rate_lock:
                now = time.monotonic()
                next_at = getattr(self.local, 'next_request_at', 0.0) if self.parallel_pacing else self.next_request_at
                delay = max(next_at, self.retry_not_before) - now
                if delay <= 0:
                    if self.parallel_pacing:
                        # Parallel validation historically has deterministic
                        # worker pacing; the shared 429 cooldown still applies.
                        self.local.next_request_at = now + self.min_interval
                    else:
                        interval = self.min_interval + random.uniform(0, min(0.12, self.min_interval * 0.25))
                        self.next_request_at = now + interval
                    return
            time.sleep(delay)

    def backoff(self, seconds):
        # A 429 from any worker pauses new requests from all site workers.
        with self.rate_lock:
            self.retry_not_before = max(self.retry_not_before, time.monotonic() + seconds)

    def _apply_browser_state(self, session, hostname):
        with self.lock:
            cookies = list(self.browser_cookies)
            user_agent = self.browser_user_agents.get(hostname)
        if user_agent:
            session.headers.update({'User-Agent': user_agent})
        for cookie in cookies:
            domain = cookie.get('domain', '').lstrip('.')
            if hostname == domain or hostname.endswith('.' + domain):
                session.cookies.set(cookie['name'], cookie['value'],
                                    domain=cookie.get('domain') or hostname,
                                    path=cookie.get('path', '/'))

    def warm_browser_session(self, url):
        """Warm an ordinary public browser session; never solve a challenge."""
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
        parsed = urlsplit(public_url(url))
        hostname = parsed.hostname.casefold()
        with self.domain_lock(hostname):
            with self.lock:
                if hostname in self.recovery_attempted_domains:
                    raise CrawlError('该域名的一次浏览器会话恢复已经失败')
                self.recovery_attempted_domains.add(hostname)
            with sync_playwright() as pw:
                browser = None
                try:
                    try:
                        browser = pw.chromium.launch(channel='chrome', headless=True)
                    except Exception:
                        browser = pw.chromium.launch(headless=True)
                    context_options = {'locale': 'en-US', 'timezone_id': 'America/New_York'}
                    if self.proxy:
                        context_options['proxy'] = playwright_proxy(self.proxy)
                    context = browser.new_context(**context_options)
                    context.add_cookies([{'name': 'localization', 'value': 'US', 'url': url},
                                         {'name': 'cart_currency', 'value': 'USD', 'url': url}])
                    page = context.new_page()
                    try:
                        response = page.goto(url, wait_until='commit', timeout=12000)
                        page.wait_for_timeout(1500)
                    except BrowserTimeout as error:
                        raise CrawlError('浏览器打开公开页面超时') from error
                    if urlsplit(page.url).hostname != parsed.hostname:
                        raise CrawlError('浏览器恢复跳转到不同站点')
                    html = page.content()
                    marker = type('BrowserResponse', (), {
                        'headers': response.headers if response else {'content-type': 'text/html'},
                        'status_code': response.status if response else 0,
                        'text': html})()
                    if is_access_challenge_response(marker):
                        raise AccessChallenge('站点要求人工完成人机验证；未自动绕过挑战', url)
                    if not response or response.status >= 400:
                        raise CrawlError('浏览器公开页面 HTTP ' + str(response.status if response else 'unknown'))
                    cookies = context.cookies()
                    user_agent = page.evaluate('navigator.userAgent')
                    self._store_browser_state(hostname, cookies, user_agent)
                finally:
                    if browser:
                        browser.close()

    def get(self, url, attempts=3, timeout=None, headers=None):
        cache_key = (url, tuple(sorted((key.lower(), str(value)) for key, value in headers.items()))) if headers else url
        with self.lock:
            if cache_key in self.cache:
                return self.cache[cache_key]
        public_url(url)
        target = url
        recovered = False
        for redirect in range(6):
            hostname = urlsplit(target).hostname.casefold()
            session = self._session(hostname)
            self._apply_browser_state(session, hostname)
            for attempt in range(attempts):
                try:
                    self.pace()
                    request_options = {'headers': headers} if headers else {}
                    if headers and urlsplit(target).netloc != urlsplit(url).netloc:
                        request_options['headers'] = {key: value for key, value in headers.items()
                                                      if key.lower() not in {'authorization', 'cookie', 'referer'}}
                    r = session.get(target, timeout=self.timeout if timeout is None else timeout,
                                    allow_redirects=False, **request_options)
                    challenged = is_access_challenge_response(r)
                    if challenged or r.status_code == 403:
                        if recovered:
                            if challenged:
                                raise AccessChallenge('站点持续返回人机验证挑战: ' + target, target)
                            r.raise_for_status()
                        self.warm_browser_session(target)
                        recovered = True
                        self._apply_browser_state(session, hostname)
                        self.pace()
                        r = session.get(target, timeout=self.timeout if timeout is None else timeout,
                                        allow_redirects=False, **request_options)
                        challenged = is_access_challenge_response(r)
                        if challenged:
                            raise AccessChallenge('站点持续返回人机验证挑战: ' + target, target)
                        if r.status_code == 403:
                            r.raise_for_status()
                    if r.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                        delay = retry_after_seconds(r.headers.get('Retry-After'))
                        if delay is None:
                            delay = min(45.0, float(2 ** (attempt + 1)))
                        if r.status_code == 429:
                            with self.rate_lock:
                                self.parallel_pacing = False
                                self.min_interval = min(2.0, max(0.3, self.min_interval * 2))
                            self.backoff(delay)
                        else:
                            time.sleep(delay)
                        continue
                    r.raise_for_status()
                    break
                except AccessChallenge:
                    raise
                except requests.RequestsError as e:
                    if attempt + 1 == attempts:
                        raise CrawlError(f'请求失败 {target}: {e}') from e
                    # Immediate retries tend to hit the same transient reset;
                    # a short bounded pause is enough without slowing success.
                    time.sleep(0.5 * (attempt + 1))
            if r.status_code in (301, 302, 303, 307, 308):
                from urllib.parse import urljoin
                target = public_url(urljoin(target, r.headers['Location']))
                continue
            break
        else:
            raise CrawlError('重定向次数过多')
        if len(r.content) > 25_000_000:
            raise CrawlError('单个响应超过 25 MB')
        if is_access_challenge_response(r):
            raise AccessChallenge('站点返回人机验证挑战，响应未缓存: ' + target, target)
        with self.lock:
            self.cache[cache_key] = r
        return r

    def json(self, url, headers=None):
        try:
            response = self.get(url, headers=headers) if headers else self.get(url)
            requested, actual = urlsplit(url), urlsplit(response.url)
            if requested.hostname != actual.hostname or unquote(requested.path).rstrip('/') != unquote(actual.path).rstrip('/'):
                raise CrawlError('JSON 接口重定向改变了来源范围: ' + url)
            return response.json()
        except CrawlError as error:
            # One browser recovery per public JSON URL, including its page/filter.
            # This never prompts for login/CAPTCHA or retries indefinitely.
            parsed = urlsplit(url)
            if 'HTTP Error 403' not in str(error) or not parsed.path.endswith(('.json', '.js')):
                raise
            with self.lock:
                if url in self.browser_attempted:
                    raise
                self.browser_attempted.add(url)
            try:
                return self.browser_json(url)
            except Exception as recovery:
                raise CrawlError('公开 JSON 访问阻塞: '+str(error)+'；一次浏览器恢复失败: '+str(recovery)) from recovery
        except ValueError as e:
            raise CrawlError('接口未返回 JSON: ' + url) from e

    def browser_json(self, url):
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
        parsed = urlsplit(url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context_options = {'locale': 'en-US'}
                if self.proxy:
                    context_options['proxy'] = playwright_proxy(self.proxy)
                context = browser.new_context(**context_options)
                context.add_cookies([{'name': 'localization', 'value': 'US', 'url': url},
                                    {'name': 'cart_currency', 'value': 'USD', 'url': url}])
                page = context.new_page()
                path = parsed.path
                if '/collections/' in path and path.endswith('/products.json'):
                    landing = parsed._replace(path=path[:-len('/products.json')], query='', fragment='').geturl()
                else:
                    landing = parsed._replace(path=path.removesuffix('.json').removesuffix('.js'), query='', fragment='').geturl()
                try:
                    page.goto(landing, wait_until='commit', timeout=10000)
                    page.locator('a[href*="/products/"]').first.wait_for(state='attached', timeout=3000)
                except BrowserTimeout:
                    pass
                if urlsplit(page.url).netloc != parsed.netloc:
                    raise CrawlError('浏览器分类页未进入同源站点: ' + landing)
                result = page.evaluate("""async u => {
                    const r = await fetch(u, {credentials:'same-origin', signal:AbortSignal.timeout(12000)});
                    return {status:r.status, url:r.url, body:await r.text()};
                }""", url)
                actual = urlsplit(result['url'])
                if actual.netloc != parsed.netloc or actual.path.rstrip('/') != parsed.path.rstrip('/') or actual.query != parsed.query:
                    raise CrawlError('浏览器 JSON 响应改变了来源范围: ' + url)
                if result['status'] != 200:
                    raise CrawlError('浏览器同源 fetch HTTP ' + str(result['status']) + ': ' + url)
                try:
                    data = json.loads(result['body'])
                except ValueError as error:
                    raise CrawlError('浏览器同源 fetch 未返回 JSON: ' + url) from error
                with self.lock:
                    self.browser_cookies = [c for c in context.cookies() if parsed.hostname == c['domain'].lstrip('.') or parsed.hostname.endswith('.' + c['domain'].lstrip('.'))]
                return data
            finally:
                browser.close()

    def manual_browser_confirm(self, url, timeout=300):
        """Prefer visible ruyiPage Firefox, then fall back to Playwright."""
        try:
            return self._manual_ruyipage_confirm(url, timeout)
        except AccessChallenge:
            raise
        except Exception as error:
            # A launched Firefox can still fail while reading BiDi page state,
            # cookies, or UA.  Keep the Chromium fallback, but never make that
            # transition invisible to operators diagnosing a confirmation.
            LOGGER.warning('ruyiPage 人工确认读取失败，回退 Playwright: %s',
                           redact_sensitive(error, self.proxy), exc_info=True)
            return self._manual_playwright_confirm(url, timeout)

    def _ruyipage_browser_path(self):
        configured = os.environ.get('RUYIPAGE_FIREFOX_PATH')
        candidates = [configured] if configured else []
        candidates.extend([
            r'C:\Program Files\Mozilla Firefox\firefox.exe',
            r'C:\Program Files (x86)\Mozilla Firefox\firefox.exe',
        ])
        return next((str(Path(item)) for item in candidates
                     if item and Path(item).is_file()), None)

    def _ruyipage_user_dir(self, hostname):
        base = Path(os.environ.get(
            'RUYIPAGE_USER_DIR', str(self.state_dir / 'ruyipage_profiles')))
        identity = self._state_path(hostname).stem
        path = base / identity
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    @staticmethod
    def _ruyipage_cookies(items):
        result = []
        for item in items:
            raw = getattr(item, 'raw', {}) or {}
            cookie = {
                'name': getattr(item, 'name', raw.get('name', '')),
                'value': getattr(item, 'value', raw.get('value', '')),
                'domain': getattr(item, 'domain', raw.get('domain')),
                'path': getattr(item, 'path', raw.get('path')) or '/',
            }
            if not cookie['name'] or not cookie['domain']:
                continue
            for source, target in (('httpOnly', 'httpOnly'), ('secure', 'secure'),
                                   ('sameSite', 'sameSite'), ('expiry', 'expires')):
                value = raw.get(source)
                if value is not None:
                    cookie[target] = value
            result.append(cookie)
        return result

    def _manual_ruyipage_confirm(self, url, timeout):
        from ruyipage import launch
        parsed = urlsplit(public_url(url))
        hostname = parsed.hostname.casefold()
        page = launch(
            headless=False,
            browser_path=self._ruyipage_browser_path(),
            user_dir=self._ruyipage_user_dir(hostname),
            proxy=self.proxy,
            close_on_exit=True,
            timeout_page_load=20,
        )
        try:
            page.emulation.set_locale('en-US')
            page.emulation.set_timezone('America/New_York')
            page.get(url, wait='none', timeout=20)
            deadline = time.monotonic() + timeout
            ready_after = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                current_url = page.url
                current_hostname = urlsplit(current_url).hostname
                if _same_www_site(current_hostname, parsed.hostname):
                    marker = type('BrowserResponse', (), {
                        'headers': {'content-type': 'text/html'},
                        'status_code': 200, 'text': page.html or ''})()
                    if time.monotonic() >= ready_after and manual_confirmation_ready(marker):
                        user_agent = page.run_js('return navigator.userAgent')
                        cookies = self._ruyipage_cookies(
                            page.get_cookies(all_info=True))
                        # Persist under both the requested and canonical www
                        # host so the next HTTP request can reuse the confirmed
                        # identity before and after its normal redirect.
                        self._store_browser_state(current_hostname.casefold(), cookies, user_agent)
                        if current_hostname.casefold() != hostname:
                            self._save_browser_state(hostname, cookies, user_agent)
                        return {'hostname': hostname, 'confirmed': True,
                                'engine': 'ruyipage_firefox'}
                time.sleep(1)
            raise AccessChallenge('人工访问确认超时，Firefox 仍处于验证页面', url)
        finally:
            page.quit()

    def _manual_playwright_confirm(self, url, timeout):
        from playwright.sync_api import sync_playwright
        parsed = urlsplit(public_url(url))
        hostname = parsed.hostname.casefold()
        with self.domain_lock(hostname), sync_playwright() as pw:
            browser = None
            try:
                try:
                    browser = pw.chromium.launch(channel='chrome', headless=False)
                except Exception:
                    browser = pw.chromium.launch(headless=False)
                options = {'locale': 'en-US', 'timezone_id': 'America/New_York'}
                if self.proxy:
                    options['proxy'] = playwright_proxy(self.proxy)
                context = browser.new_context(**options)
                page = context.new_page()
                page.goto(url, wait_until='commit', timeout=20000)
                deadline = time.monotonic() + timeout
                ready_after = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    current_hostname = urlsplit(page.url).hostname
                    if _same_www_site(current_hostname, parsed.hostname):
                        html = page.content()
                        marker = type('BrowserResponse', (), {
                            'headers': {'content-type': 'text/html'},
                            'status_code': 200, 'text': html})()
                        if time.monotonic() >= ready_after and manual_confirmation_ready(marker):
                            user_agent = page.evaluate('navigator.userAgent')
                            cookies = context.cookies()
                            self._store_browser_state(current_hostname.casefold(), cookies, user_agent)
                            if current_hostname.casefold() != hostname:
                                self._save_browser_state(hostname, cookies, user_agent)
                            return {'hostname': hostname, 'confirmed': True,
                                    'engine': 'playwright_chromium'}
                    page.wait_for_timeout(1000)
                raise AccessChallenge('人工访问确认超时，站点仍处于验证页面', url)
            finally:
                if browser:
                    browser.close()
