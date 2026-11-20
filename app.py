"""Local workbench and CLI. Python 3.10+, no web framework required."""
import argparse
import ipaddress
import json
import mimetypes
import secrets
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from core.common import CrawlError, read_json, save_json
import core.common as _common
AccessChallenge = getattr(_common, 'AccessChallenge', CrawlError)
Client = getattr(_common, 'Client', None)
validate_proxy = getattr(_common, 'validate_proxy', lambda value: value or None)
redact_proxy = getattr(_common, 'redact_proxy', lambda value: None)
redact_sensitive = getattr(_common, 'redact_sensitive', lambda value, proxy=None: str(value))
from core.pipeline import run_job
from core.failure_telemetry import submit_failure_async

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / 'runs'
JOBS = {}
LOCK = threading.RLock()
WORKERS = threading.Semaphore(4)
APP_VERSION = '1.0.7'
MAX_PARALLEL_JOBS = 4
MAX_BATCH_SITES = 20
TERMINAL_STATES = {'completed', 'failed', 'interrupted', 'needs_recheck', 'cancelled'}
STAGE_PROGRESS = {
    'queued': 2, 'detecting': 8, 'navigation': 18, 'navigation_check': 22,
    'navigation_ai': 25, 'navigation_fastpath': 30, 'navigation_targets': 35,
    'awaiting_confirmation': 35, 'listing': 48, 'ai': 55, 'ai_repair': 58,
    'products': 72, 'preview': 82, 'packaging': 92, 'completed': 100,
    'cancelled': 0,
}


def cleanup_runs(keep_recent=30, job_id=None):
    """Remove inactive run directories while never touching active jobs."""
    keep_recent = int(keep_recent)
    if not 0 <= keep_recent <= 1000:
        raise ValueError('保留任务数必须在 0~1000 之间')
    with LOCK:
        active = {key for key, job in JOBS.items()
                  if (job.get('status') not in TERMINAL_STATES
                      or (job.get('worker_done') is not None
                          and not job['worker_done'].is_set()))}
        if job_id is not None:
            if job_id in active:
                raise CrawlError('运行中任务不能清理')
            candidates = [RUNS / job_id] if (RUNS / job_id).is_dir() else []
        else:
            inactive = [path for path in RUNS.iterdir()
                        if path.is_dir() and path.name not in active]
            inactive.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            candidates = inactive[keep_recent:]
        removed = []
        for path in candidates:
            resolved = path.resolve()
            if path.name in active or resolved.parent != RUNS.resolve():
                continue
            shutil.rmtree(path)
            removed.append(path.name)
            JOBS.pop(path.name, None)
    return {'removed': removed, 'removed_count': len(removed),
            'kept_recent': keep_recent}


def _sites(value):
    """Normalise a single URL or a newline/comma separated batch."""
    if not isinstance(value, str):
        raise TypeError('站点地址必须是文本')
    result = []
    for item in value.replace('\r', '\n').replace(',', '\n').split('\n'):
        item = item.strip()
        if item and item not in result:
            result.append(item)
    if not result:
        raise ValueError('请输入站点 URL')
    if len(result) > MAX_BATCH_SITES:
        raise ValueError(f'单次最多提交 {MAX_BATCH_SITES} 个站点')
    if any(len(item) > 2000 for item in result):
        raise ValueError('站点 URL 过长')
    return result


def _progress(job):
    if job.get('status') == 'failed':
        return max(1, int(job.get('progress', 0)))
    return max(int(job.get('progress', 0)), STAGE_PROGRESS.get(job.get('status'), 0))


def update_status(fetch=False):
    """Return a non-destructive Git update status for the private distribution."""
    result = {'version': APP_VERSION, 'repository': 'yuhang995/ai-crawler-workbench',
              'update_available': False, 'managed_by_git': (ROOT / '.git').exists()}
    if not result['managed_by_git']:
        result['message'] = '当前为压缩包安装，请使用 Git 克隆版启用一键更新'
        return result
    try:
        if fetch:
            subprocess.run(['git', 'fetch', '--quiet', 'origin', 'main'], cwd=ROOT,
                           check=True, timeout=45, capture_output=True, text=True)
        local = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        remote = subprocess.check_output(['git', 'rev-parse', 'origin/main'], cwd=ROOT, text=True).strip()
        result.update(local_commit=local[:12], remote_commit=remote[:12],
                      update_available=local != remote)
    except (OSError, subprocess.SubprocessError) as error:
        result['message'] = redact_sensitive(error)
    return result


def _normalise_host(host):
    """Return a comparable localhost/IP value, or None for names/public syntax."""
    if not isinstance(host, str) or not host:
        return None
    if host.casefold() == 'localhost':
        return 'localhost'
    try:
        return ipaddress.ip_address(host).compressed.casefold()
    except ValueError:
        return None


def _parse_authority(value):
    """Parse a Host-style authority, including bracketed IPv6, without DNS."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(char in value for char in '\r\n'):
        return None
    try:
        parsed = urlsplit('//' + value)
        port = parsed.port
    except ValueError:
        return None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or port is None):
        return None
    host = _normalise_host(parsed.hostname)
    return (host, port) if host is not None else None


def _is_private_authority(value, port):
    authority = _parse_authority(value)
    if not authority or authority[1] != port:
        return False
    host = authority[0]
    if host == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ((address.is_private or address.is_loopback)
            and not address.is_unspecified and not address.is_multicast)


def _is_same_private_origin(host_header, origin, port):
    """Require browser writes to come from the exact private authority in Host."""
    host_authority = _parse_authority(host_header)
    if not host_authority or not _is_private_authority(host_header, port):
        return False
    if not origin:
        return True
    if not isinstance(origin, str) or any(char in origin for char in '\r\n'):
        return False
    try:
        parsed = urlsplit(origin.strip())
        origin_port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme.casefold() != 'http' or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or origin_port is None):
        return False
    origin_host = _normalise_host(parsed.hostname)
    if origin_host is None:
        return False
    origin_authority = (origin_host, origin_port)
    return (origin_authority == host_authority
            and _is_private_authority(parsed.netloc, port))


def _clean_submitter(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise TypeError('提交人必须是文本')
    value = ' '.join(value.split())
    if len(value) > 80:
        raise ValueError('提交人最多 80 个字符')
    return value


def detect_lan_ip():
    """Best-effort detection of the private IPv4 address on the default route."""
    candidates = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect chooses an interface without sending application data.
        probe.connect(('192.0.2.1', 9))
        candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        candidates.extend(item[4][0] for item in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM))
    except OSError:
        pass
    private = []
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (address.version == 4 and address.is_private and not address.is_loopback
                and not address.is_unspecified and not address.is_multicast):
            value = address.compressed
            if value not in private:
                private.append(value)
    if private:
        # VPN/virtual adapters occasionally expose an address ending in .0 or
        # .255. Prefer a conventional host address when another private
        # interface exists, while retaining the route-selected order.
        usable = [value for value in private if value.rsplit('.', 1)[-1] not in {'0', '255'}]
        return (usable or private)[0]
    return '127.0.0.1'


def artifact_base(run, site):
    """Use the already-audited canonical storefront when resuming artifacts."""
    run = Path(run)
    for name, key in (('navigation_review_scope.json', 'base_url'), ('config.json', 'base_url')):
        path = run / name
        if path.is_file():
            value = read_json(path).get(key)
            if isinstance(value, str) and value.startswith(('https://', 'http://')):
                return value
    return site if '://' in site else 'https://' + site


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def execute(job):
    with WORKERS:
        try:
            cancel_event = job.get('cancel_event')
            if cancel_event is not None and cancel_event.is_set():
                return
            def emit(event):
                with LOCK:
                    if job.get('status') == 'cancelled' and event['stage'] != 'cancelled':
                        return
                    job['events'].append(event)
                    job['status'] = event['stage']
                    job['progress'] = max(job.get('progress', 0),
                                          int(event.get('progress', STAGE_PROGRESS.get(event['stage'], 0))))
                    if 'categories' in event:
                        job['categories'] = event['categories']
                    if 'samples' in event:
                        job['samples'] = event['samples']
                    save_json(RUNS / job['id'] / 'progress.json', snapshot(job))
            run_job(job['site'], RUNS / job['id'], manual=job['manual'], confirm=job['confirm'], emit=emit,
                    category=job.get('category'), all_categories=job['all_categories'], force_ai=job['force_ai'], reuse_navigation=job.get('reuse_navigation',False), full_products=job.get('full_products',False), visual_check=job.get('visual_check',False), uploaded_categories=job.get('uploaded_categories'), proxy=job.get('proxy'), use_proxy_pool=job.get('use_proxy_pool',False), cancel_event=cancel_event)
        except Exception:
            pass  # failure details are already persisted and emitted by the pipeline.
        finally:
            failure_path = RUNS / job['id'] / 'failure.json'
            if failure_path.is_file() and job.get('status') == 'failed':
                submit_failure_async(ROOT, RUNS / job['id'], job, APP_VERSION)
            if job.get('worker_done') is not None:
                job['worker_done'].set()


def resume_after_access_confirmation(job):
    """Atomically archive the access stop and enqueue the same job again."""
    with LOCK:
        if job.get('status') != 'access_confirmed':
            return False
        failed = RUNS / job['id'] / 'failure.json'
        if failed.exists():
            failed.replace(RUNS / job['id'] / 'previous-access-failure.json')
        job['status'] = 'queued'
        job['events'].append({
            'stage': 'queued',
            'message': '访问确认已保存，自动复用会话继续任务',
            'elapsed': 0,
        })
        save_json(RUNS / job['id'] / 'progress.json', snapshot(job))
    threading.Thread(target=execute, args=(job,), daemon=True).start()
    return True


def snapshot(job):
    result={k: v for k, v in job.items() if k not in {'confirm', 'cancel_event', 'worker_done', 'uploaded_categories', 'proxy'}}
    result['proxy_display'] = redact_proxy(job.get('proxy'))
    if job.get('use_proxy_pool'):
        result['proxy_display'] = '代理池（密码已隐藏）'
    result['progress'] = _progress(job)
    if job.get('uploaded_categories') is not None:
        result['uploaded_category_count'] = len(job['uploaded_categories'])
    if job.get('status')=='navigation_ai':
        result['menu_snapshots']=len(list((RUNS/job['id']/'evidence').glob('home-menu-*.html')))
        scope=RUNS/job['id']/'navigation_review_scope.json'
        if scope.is_file():
            review=read_json(scope)
            result['confirmed_categories']=len(review.get('confirmed_candidate',{}))
            result['uncertain_categories']=len(review.get('uncertain_candidate',{}))
            result['navigation_issues']=len(review.get('issues',[]))
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def respond(self, data, status=200, content_type='application/json; charset=utf-8'):
        body = json.dumps(data, ensure_ascii=False).encode() if not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ('/', '/style.css', '/light.css', '/app.js'):
            file = ROOT / 'web' / ('index.html' if path == '/' else path[1:])
            return self.respond(file.read_bytes(), content_type=mimetypes.guess_type(file)[0] + '; charset=utf-8')
        if path == '/api/jobs':
            with LOCK:
                return self.respond([snapshot(j) for j in reversed(list(JOBS.values()))])
        if path == '/api/system':
            with LOCK:
                active = sum(j.get('status') not in TERMINAL_STATES and
                             j.get('status') not in {'needs_access_confirm', 'access_confirmed'}
                             for j in JOBS.values())
            return self.respond({'version': APP_VERSION, 'parallel_workers': MAX_PARALLEL_JOBS,
                                 'active_jobs': active, **update_status(False)})
        pieces = path.strip('/').split('/')
        if len(pieces)==4 and pieces[:2]==['api','jobs'] and pieces[3]=='preview':
            job=JOBS.get(pieces[2])
            if not job:return self.respond({'error':'任务不存在'},404)
            file=RUNS/job['id']/'evidence/home-desktop.png'
            if not file.is_file():return self.respond({'error':'截图暂不可用，请核对目录表格'},404)
            return self.respond(file.read_bytes(),content_type='image/png')
        if len(pieces) == 4 and pieces[:2] == ['api', 'download']:
            job_id, name = pieces[2:]
            allowed = {'crawler.zip', 'url.json', 'detail_url.json', 'result.csv', 'result_clean.csv',
                       'shopify_collection_assignments.csv',
                       'shopify_import.csv', 'validation.json', 'report.json', 'failure.json', 'ai.log', 'navigation_ai.log',
                       'warnings.json', 'navigation_report.json', 'navigation_release.json', 'visual_audit.json', 'visual_ai.log', 'visual_context.json', 'review.md', 'navigation_diagnostic.json', 'navigation_review_scope.json', 'navigation_decision.json', 'ai_repair.json', 'ai_repair.log', 'navigation_repair.json', 'navigation_native_audit.json'}
            if job_id not in JOBS or name not in allowed:
                return self.respond({'error': '文件不存在'}, 404)
            if name not in {'warnings.json','visual_audit.json','visual_ai.log','visual_context.json','failure.json','ai.log','navigation_ai.log','navigation_diagnostic.json','navigation_review_scope.json','navigation_decision.json', 'ai_repair.json', 'ai_repair.log', 'navigation_repair.json', 'navigation_native_audit.json'}:
                proof=RUNS / job_id / 'navigation_report.json'
                audit=json.loads(proof.read_text(encoding='utf-8')) if proof.is_file() else {}
                from core.simple_nav import EDITABLE_NAV_METHODS
                simple_directory = audit.get('method') in EDITABLE_NAV_METHODS and name in {'url.json','navigation_report.json','navigation_release.json'}
                if (JOBS[job_id]['status']!='completed' and not simple_directory) or audit.get('audit_version')!=4 or not audit.get('passed'):
                    return self.respond({'error':'产物未通过新版目录核对，禁止作为成功结果下载'},409)
                from core.navigation_release import require_release
                try:
                    require_release(RUNS / job_id)
                except CrawlError as error:
                    return self.respond({'error': str(error)}, 409)
            file = RUNS / job_id / name
            if not file.is_file():
                return self.respond({'error': '文件尚未生成'}, 404)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', 'attachment; filename="' + name + '"')
            self.send_header('Content-Length', str(file.stat().st_size))
            self.end_headers()
            with file.open('rb') as f:
                import shutil
                shutil.copyfileobj(f, self.wfile)
            return
        self.respond({'error': '不存在'}, 404)

    def do_POST(self):
        # Browser writes may use loopback or a literal private LAN IP, but remain same-origin.
        host = self.headers.get('Host', '')
        if not _is_private_authority(host, self.server.server_port):
            return self.respond({'error': '非法 Host'}, 403)
        origin = self.headers.get('Origin')
        if not _is_same_private_origin(host, origin, self.server.server_port):
            return self.respond({'error': '非法 Origin'}, 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond({'error': 'JSON required'}, 415)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1_000_000:
                raise ValueError('请求大小无效')
            body = json.loads(self.rfile.read(size))
            path = urlsplit(self.path).path
            if path == '/api/jobs':
                sites = _sites(body.get('site', ''))
                submitter = _clean_submitter(body.get('submitter'))
                if body.get('full_products') and body.get('manual'):
                    raise ValueError('全自动全量模式不能同时启用手动确认')
                uploaded_categories = body.get('uploaded_categories')
                proxy = validate_proxy(body.get('proxy'))
                use_proxy_pool = bool(body.get('use_proxy_pool', False)) and not proxy
                if uploaded_categories is not None:
                    if body.get('manual'):
                        raise ValueError('手动放分类文件后无需再勾选手动确认目录范围')
                    if not isinstance(uploaded_categories, dict):
                        raise ValueError('分类文件必须是 JSON 对象')
                    # Do the complete canonical validation after resolving the
                    # live storefront.  Bound it here before queuing work.
                    if not uploaded_categories or len(uploaded_categories) > 5000:
                        raise ValueError('分类文件必须包含 1~5000 个目录')
                if uploaded_categories is not None and len(sites) != 1:
                    raise ValueError('手动分类文件一次只能用于一个站点')
                with LOCK:
                    active = sum(j['status'] not in ('completed', 'failed', 'interrupted', 'needs_recheck', 'needs_access_confirm', 'access_confirmed') for j in JOBS.values())
                    if active + len(sites) > 24:
                        return self.respond({'error': '待处理任务过多'}, 429)
                    jobs = []
                    batch_id = secrets.token_hex(6) if len(sites) > 1 else None
                    for site in sites:
                        job = {'id': secrets.token_hex(8), 'batch_id': batch_id, 'site': site,
                               'manual': bool(body.get('manual', False)),
                               'full_products': bool(body.get('full_products', False)),
                               'visual_check': bool(body.get('visual_check', False)),
                               'all_categories': bool(body.get('all_categories', False)),
                               'force_ai': bool(body.get('force_ai', False)),
                               'category': body.get('category') or None, 'submitter': submitter or None,
                               'uploaded_categories': uploaded_categories, 'proxy': proxy,
                               'use_proxy_pool': use_proxy_pool,
                               'status': 'queued', 'progress': 2, 'events': [],
                               'confirm': threading.Event(), 'cancel_event': threading.Event(),
                               'worker_done': threading.Event()}
                        JOBS[job['id']] = job
                        jobs.append(job)
                        (RUNS / job['id']).mkdir(parents=True, exist_ok=True)
                        save_json(RUNS / job['id'] / 'progress.json', snapshot(job))
                for job in jobs:
                    threading.Thread(target=execute, args=(job,), daemon=True).start()
                if len(jobs) == 1:
                    return self.respond(snapshot(jobs[0]), 201)
                return self.respond({'batch_id': batch_id, 'count': len(jobs),
                                     'jobs': [snapshot(job) for job in jobs]}, 201)
            if path == '/api/update/check':
                return self.respond(update_status(True))
            if path == '/api/update/apply':
                status = update_status(True)
                if not status.get('managed_by_git'):
                    return self.respond({'error': status.get('message', '当前安装不支持一键更新')}, 409)
                dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()
                if dirty:
                    return self.respond({'error': '检测到本地代码修改，为防止覆盖已取消更新'}, 409)
                if not status.get('update_available'):
                    return self.respond({**status, 'updated': False, 'message': '已经是最新版本'})
                completed = subprocess.run(['git', 'pull', '--ff-only', 'origin', 'main'], cwd=ROOT,
                                           timeout=120, capture_output=True, text=True)
                if completed.returncode:
                    return self.respond({'error': redact_sensitive(completed.stderr or completed.stdout)}, 409)
                return self.respond({**update_status(False), 'updated': True,
                                     'restart_required': True,
                                     'message': '更新完成，请重启工作台以启用后端更新'})
            if path == '/api/jobs/cleanup':
                result = cleanup_runs(body.get('keep_recent', 30), body.get('job_id'))
                return self.respond(result)
            pieces = path.strip('/').split('/')
            if len(pieces)==4 and pieces[:2]==['api','jobs'] and pieces[3]=='cancel':
                with LOCK:
                    job = JOBS.get(pieces[2])
                    if not job:
                        return self.respond({'error':'任务不存在'},404)
                    if job.get('status') in TERMINAL_STATES:
                        return self.respond({'error':'任务已终止'},409)
                    job['cancel_event'].set()
                    job['confirm'].set()
                    job['status'] = 'cancelled'
                    job['events'].append({'stage':'cancelled','message':'用户已取消任务','elapsed':0})
                    save_json(RUNS/job['id']/'progress.json', snapshot(job))
                return self.respond(snapshot(job),202)
            if len(pieces)==4 and pieces[:2]==['api','jobs'] and pieces[3]=='open-access-confirm':
                with LOCK:
                    job=JOBS.get(pieces[2])
                    if not job or job['status']!='needs_access_confirm':
                        return self.respond({'error':'任务当前不等待访问确认'},409)
                    job['status']='access_confirming'
                    job['events'].append({'stage':'access_confirming','message':'已打开本地浏览器，请完成人机验证','elapsed':0})
                    save_json(RUNS/job['id']/'progress.json',snapshot(job))
                def confirm_access():
                    try:
                        failure=read_json(RUNS/job['id']/'failure.json')
                        result=Client(proxy=job.get('proxy'), proxy_pool=job.get('use_proxy_pool',False),
                                      cancel_event=job.get('cancel_event')).manual_browser_confirm(failure.get('url') or job['site'])
                        with LOCK:
                            job['status']='access_confirmed'
                            engine=result.get('engine','visible_browser') if isinstance(result,dict) else 'visible_browser'
                            job['access_confirm_engine']=engine
                            job['events'].append({'stage':'access_confirmed','message':'访问确认已保存，正在自动继续任务','engine':engine,'elapsed':0})
                            save_json(RUNS/job['id']/'progress.json',snapshot(job))
                        resume_after_access_confirmation(job)
                    except Exception as error:
                        with LOCK:
                            job['status']='needs_access_confirm'
                            job['events'].append({'stage':'needs_access_confirm','message':redact_sensitive(error,job.get('proxy')),'elapsed':0})
                            save_json(RUNS/job['id']/'progress.json',snapshot(job))
                threading.Thread(target=confirm_access,daemon=True).start()
                return self.respond({'ok':True},202)
            if len(pieces)==4 and pieces[:2]==['api','jobs'] and pieces[3]=='continue-access':
                with LOCK:
                    job=JOBS.get(pieces[2])
                if not job or not resume_after_access_confirmation(job):
                    return self.respond({'error':'任务未处于可继续的访问确认状态'},409)
                return self.respond(snapshot(job),202)
            if len(pieces)==4 and pieces[:2]==['api','jobs'] and pieces[3]=='retry-artifacts':
                with LOCK:
                    job=JOBS.get(pieces[2])
                    if not job or job['status'] not in ('failed','completed'):return self.respond({'error':'仅可恢复失败任务或重建已完成产物'},409)
                    from core.simple_nav import EDITABLE_NAV_METHODS
                    if read_json(RUNS/job['id']/'navigation_report.json').get('method') not in EDITABLE_NAV_METHODS:
                        from core.navigation_ai import load_navigation
                        from core.validator import validate_navigation
                        directory,proof,documents=load_navigation(RUNS/job['id'])
                        base=artifact_base(RUNS/job['id'],job['site'])
                        validate_navigation(directory,documents,base,evidence=proof)
                    job['reuse_navigation']=True;job['status']='queued'
                    failed=RUNS/job['id']/'failure.json'
                    if failed.exists():failed.replace(RUNS/job['id']/'previous-failure.json')
                threading.Thread(target=execute,args=(job,),daemon=True).start()
                return self.respond(snapshot(job),202)
            if len(pieces) == 4 and pieces[:2] == ['api', 'jobs'] and pieces[3] == 'confirm':
                with LOCK:
                    job = JOBS.get(pieces[2])
                    if not job or job['status'] != 'awaiting_confirmation':
                        return self.respond({'error': '任务当前不等待确认'}, 409)
                    if 'categories' in body:
                        report_path=RUNS/job['id']/'navigation_report.json'
                        method=read_json(report_path).get('method') if report_path.is_file() else None
                        from core.simple_nav import EDITABLE_NAV_METHODS, clean_reviewed_categories
                        if method in EDITABLE_NAV_METHODS:
                            sources=read_json(RUNS/job['id']/'evidence/simple_nav_sources.json')
                            directory,_=clean_reviewed_categories(body['categories'],sources,method)
                        else:
                            from core.practical_navigation import validate_edits
                            directory=validate_edits(body['categories'],artifact_base(RUNS/job['id'],job['site']))
                        save_json(RUNS/job['id']/'confirmed_categories.json',directory)
                        job['categories']=directory
                    job['status']='navigation_targets'
                    job['confirm'].set()
                return self.respond({'ok': True})
            return self.respond({'error': '不存在'}, 404)
        except CrawlError as e:
            return self.respond({'error': str(e)}, 409)
        except (ValueError, TypeError) as e:
            return self.respond({'error': str(e)}, 400)


def serve(port, host='0.0.0.0'):
    RUNS.mkdir(exist_ok=True)
    for p in RUNS.glob('*/progress.json'):
        try:
            job = read_json(p)
            if job['status'] == 'access_confirming':
                job['status'] = 'needs_access_confirm'
                job['events'].append({'message': '进程已重启，请重新打开浏览器确认', 'stage': 'needs_access_confirm', 'elapsed': 0})
            elif job['status'] not in ('completed', 'failed', 'needs_recheck', 'needs_access_confirm', 'access_confirmed'):
                job['status'] = 'interrupted'
                job['events'].append({'message': '进程已重启；旧任务保留，请新建任务', 'stage': 'interrupted', 'elapsed': 0})
            audit_file=RUNS / job['id'] / 'navigation_report.json'
            audit=json.loads(audit_file.read_text(encoding='utf-8')) if audit_file.is_file() else {}
            if job['status']=='completed' and audit.get('audit_version')!=4:
                job['status']='needs_recheck'
            if job['status'] == 'completed':
                from core.navigation_release import require_release
                try:
                    require_release(RUNS / job['id'])
                except CrawlError:
                    job['status'] = 'needs_recheck'
            job['confirm'] = threading.Event()
            job['cancel_event'] = threading.Event()
            job['worker_done'] = threading.Event(); job['worker_done'].set()
            JOBS[job['id']] = job
        except (ValueError, KeyError):
            continue
    server = LocalServer((host, port), Handler)
    lan_ip = detect_lan_ip()
    print(f'ai爬虫 3 本机地址: http://127.0.0.1:{port}', flush=True)
    print(f'ai爬虫 3 局域网地址: http://{lan_ip}:{port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='ai爬虫 3 — 本地确定性电商采集工作台')
    parser.add_argument('--site', help='非空时直接执行单站端到端任务')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--category', help='选择真实导航分类键或 URL，默认第一分类')
    parser.add_argument('--category-file', type=Path, help='手动放入现成的 url.json，跳过自动扣目录')
    parser.add_argument('--proxy', help='可选 HTTP/HTTPS/SOCKS5 代理地址')
    parser.add_argument('--proxy-pool', action='store_true', help='按顺序使用 working_proxies.txt 代理池')
    parser.add_argument('--all-categories', action='store_true')
    parser.add_argument('--full', action='store_true', help='全自动：审核全部目录、采集全部分类与商品并校验，无确认步骤')
    parser.add_argument('--visual-check', action='store_true', help='截图与视觉审核，实用模式疑点记入警告')
    parser.add_argument('--force-ai', action='store_true', help='跨商品颜色合并/加密接口等非标处理')
    parser.add_argument('--port', type=int, default=8773)
    parser.add_argument('--host', default='0.0.0.0', help='工作台监听地址（默认开放局域网）')
    args = parser.parse_args()
    if args.site:
        output = args.out or RUNS / time.strftime('%Y%m%d-%H%M%S')
        uploaded = read_json(args.category_file) if args.category_file else None
        run_job(args.site, output, category=args.category, all_categories=args.all_categories,
                force_ai=args.force_ai, full_products=args.full, visual_check=args.visual_check,
                uploaded_categories=uploaded, proxy=args.proxy,
                use_proxy_pool=args.proxy_pool,
                emit=lambda e: print(json.dumps(e, ensure_ascii=False), flush=True))
    else:
        serve(args.port, args.host)
