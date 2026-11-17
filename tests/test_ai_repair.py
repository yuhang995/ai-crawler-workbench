import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.ai_repair import (PROJECT, _adopt, _checker, _copy_inputs, _prompt,
                            _stop_process, _unchanged, is_repairable, repair_sample)
from core.common import AdapterRequired, CrawlError, read_json, save_json
from core.navigation_release import digest
from core.validator import validate
from test_core import BASE, fixture


def prepare_root(root):
    save_json(root / 'url.json', {'Shop': BASE + '/collections/shop'})
    save_json(root / 'config.json', {'base_url': BASE, 'navigation_version': 'version-1'})
    save_json(root / 'navigation_release.json', {
        'method': 'simple_nav', 'status': 'approved',
        'navigation_version': 'version-1', 'inputs': {},
    })
    save_json(root / 'detection.json', {'market': {
        'confirmed': True, 'countries': ['US'], 'currencies': ['USD'],
    }})
    save_json(root / 'sample_scope.json', {
        'selected_categories': {'Shop': BASE + '/collections/shop'},
        'detail_scope': 'first_3_products', 'listing_scope': 'selected',
    })
    save_json(root / 'evidence/navigation-observed.json', {})
    (root / 'evidence/home.html').write_text('<nav>Shop</nav>', encoding='utf-8')


def write_candidate(work, bad_price=False):
    samples, detail, listings, _ = copy.deepcopy(fixture())
    save_json(work / 'detail_url.json', detail)
    save_json(work / 'listings.json', listings)
    for sample in samples:
        save_json(work / 'evidence' / f'product-{sample["product"]["id"]}.json', sample['product'])
    if bad_price:
        samples[0]['product']['variants'][0]['price'] = '9999.00'
    save_json(work / 'samples.json', samples)
    for name in ('get_detail_url.py', 'crawl_products.py'):
        (work / name).write_text('print("fixture script")\n', encoding='utf-8')


class RepairTests(unittest.TestCase):
    def test_live_page_inspector_is_injected_and_protected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as sandbox:
            root, work = Path(directory), Path(sandbox)
            prepare_root(root)
            _, protected = _copy_inputs(root, work)
            inspector = work / 'inspect_live_page.py'
            self.assertTrue(inspector.is_file())
            source = inspector.read_text(encoding='utf-8')
            compile(source, str(inspector), 'exec')
            self.assertIn('playwright.sync_api', source)
            self.assertIn("sniffed-", source)
            self.assertIn('inspect_live_page.py', protected)
            inspector.write_text(source + '\n# changed\n', encoding='utf-8')
            with self.assertRaisesRegex(CrawlError, '输入发生变化'):
                _unchanged(work, protected)

    def fake_cli(self, root, create):
        def launch(command, **kwargs):
            self.assertEqual(read_json(root / 'ai_repair.json')['status'], 'running')
            work = Path(kwargs['cwd'])
            self.assertNotEqual(work, root)
            self.assertTrue((work / 'core/validator.py').is_file())
            self.assertTrue((work / 'check_sample.py').is_file())
            self.assertIn('--ignore-user-config', command)
            self.assertIn('--ignore-rules', command)
            self.assertIn('--ephemeral', command)
            self.assertIn('--approve-for-me', command)
            self.assertIn('code_mode', command)
            self.assertIn('code_mode_host', command)
            self.assertNotIn('approval_policy=never', command)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
            self.assertIn('sandbox_workspace_write.network_access=true', command)
            self.assertIn('mcp_servers={}', command)
            self.assertIn('apps', command)
            self.assertIn('browser_use', command)
            self.assertNotIn('shell_tool', command)
            self.assertNotIn('CODEX_THREAD_ID', kwargs['env'])
            self.assertEqual(kwargs['env'].get('AI_REPAIR_TEST_KEEP'), 'yes')
            process = MagicMock(returncode=0)
            def communicate(prompt, timeout):
                self.assertIn('check_sample.py', prompt)
                self.assertIn('最多做3轮', prompt)
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 300)
                create(work)
            process.communicate.side_effect = communicate
            return process
        return launch

    def test_independent_navigation_invocation_and_trusted_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_root(root)
            save_json(root / 'navigation_invocation.json', {'invocations': 1})
            save_json(root / 'ai_invocation.json', {'invocations': 1})
            protected = (root / 'url.json').read_bytes()
            with patch('core.ai_repair.shutil.which', return_value='codex.exe'), \
                    patch.dict(os.environ, {'CODEX_THREAD_ID': 'parent', 'AI_REPAIR_TEST_KEEP': 'yes'}), \
                    patch('core.ai_repair.subprocess.Popen', side_effect=self.fake_cli(root, write_candidate)) as cli:
                result = repair_sample(root, '结构不兼容')
            self.assertTrue(result['passed'])
            self.assertEqual(result['products'], 3)
            self.assertTrue(validate(root)['passed'])
            self.assertEqual(cli.call_count, 1)
            self.assertEqual(read_json(root / 'ai_repair.json')['status'], 'completed')
            self.assertEqual(read_json(root / 'ai_repair.json')['invocations'], 1)
            self.assertEqual(read_json(root / 'navigation_invocation.json'), {'invocations': 1})
            self.assertEqual((root / 'url.json').read_bytes(), protected)
            self.assertFalse((root / 'core').exists())
            self.assertIn('crawl_products.py', (root / '3.ShopifyCrawl_sort.py').read_text())
            self.assertEqual(read_json(root / 'detail_release.json')['sha256'], digest(root / 'detail_url.json'))
            with patch('core.ai_repair.subprocess.Popen') as second:
                with self.assertRaisesRegex(CrawlError, '已启动过一次'):
                    repair_sample(root, 'try again')
                second.assert_not_called()

    def test_invalid_candidate_keeps_existing_artifacts_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_root(root)
            save_json(root / 'samples.json', {'previous': 'unchanged'})
            previous = (root / 'samples.json').read_bytes()
            def candidate(work):
                write_candidate(work, bad_price=True)
                save_json(work / 'validation.json', {'passed': True})
                save_json(work / 'ai_repair_check.json', {'status': 'OK'})
            with patch('core.ai_repair.shutil.which', return_value='codex.exe'), \
                    patch.dict(os.environ, {'AI_REPAIR_TEST_KEEP': 'yes'}), \
                    patch('core.ai_repair.subprocess.Popen', side_effect=self.fake_cli(root, candidate)):
                with self.assertRaisesRegex(CrawlError, '源响应不同'):
                    repair_sample(root, '解析失败')
            self.assertEqual((root / 'samples.json').read_bytes(), previous)
            self.assertFalse((root / 'detail_url.json').exists())
            self.assertFalse((root / 'evidence/product-1.json').exists())
            self.assertEqual(read_json(root / 'ai_repair.json')['status'], 'failed')
            self.assertTrue((root / 'ai_repair_candidate/samples.json').is_file())
            self.assertTrue((root / 'ai_repair_candidate/evidence/product-1.json').is_file())

    def test_core_and_original_evidence_cannot_be_changed(self):
        for relative in ('core/validator.py', 'evidence/home.html', 'config.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prepare_root(root)
                def candidate(work):
                    write_candidate(work)
                    (work / relative).write_text('def validate(root): return {"passed": True}', encoding='utf-8')
                with patch('core.ai_repair.shutil.which', return_value='codex.exe'), \
                        patch.dict(os.environ, {'AI_REPAIR_TEST_KEEP': 'yes'}), \
                        patch('core.ai_repair.subprocess.Popen', side_effect=self.fake_cli(root, candidate)):
                    with self.assertRaisesRegex(CrawlError, '已确认输入发生变化'):
                        repair_sample(root, '结构不兼容')
                self.assertFalse((root / 'samples.json').exists())
                self.assertEqual(read_json(root / 'ai_repair.json')['status'], 'failed')

    def test_checker_runs_trusted_export_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_root(root)
            write_candidate(root)
            checker = root / 'check_sample.py'
            checker.write_text(_checker(read_json(root / 'sample_scope.json')), encoding='utf-8')
            result = subprocess.run([sys.executable, str(checker)], cwd=root,
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'OK')
            self.assertTrue(validate(root)['passed'])
            self.assertTrue((root / 'shopify_import.csv').is_file())

    def test_adopt_creates_trusted_wrapper_and_preserves_cli_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'published'
            work = Path(directory) / 'candidate'
            root.mkdir()
            work.mkdir()
            prepare_root(root)
            write_candidate(work)
            shutil.copytree(PROJECT / 'core', root / 'core',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            (work / 'crawl_products.py').write_text(
                'import json, sys\nprint(json.dumps([__name__, sys.argv[1:]]))\n', encoding='utf-8')
            (work / '3.ShopifyCrawl_sort.py').write_text('raise RuntimeError("untrusted wrapper")\n')
            _adopt(work, root, {})
            result = subprocess.run(
                [sys.executable, str(root / '3.ShopifyCrawl_sort.py'), '--all', '--category', 'one two'],
                capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout.splitlines()[0]), ['__main__', ['--all', '--category', 'one two']])
            self.assertTrue(validate(root)['passed'])
            self.assertEqual(read_json(root / 'sample_scope.json')['detail_scope'], 'all_products')
            self.assertTrue((root / 'shopify_import.csv').is_file())
            self.assertIn('尚未验证', (root / 'README.txt').read_text(encoding='utf-8'))

    def test_wrapper_failure_replaces_stale_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'published'
            work = Path(directory) / 'candidate'
            root.mkdir()
            work.mkdir()
            prepare_root(root)
            write_candidate(work)
            shutil.copytree(PROJECT / 'core', root / 'core',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            (work / 'crawl_products.py').write_text('raise RuntimeError("source unavailable")\n')
            _adopt(work, root, {})
            save_json(root / 'validation.json', {'passed': True})
            result = subprocess.run([sys.executable, str(root / '3.ShopifyCrawl_sort.py')],
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(read_json(root / 'validation.json')['passed'])

    def test_cli_failure_is_recorded_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_root(root)
            process = MagicMock(returncode=1)
            with patch('core.ai_repair.shutil.which', return_value='codex.exe'), \
                    patch('core.ai_repair.subprocess.Popen', return_value=process) as cli:
                with self.assertRaisesRegex(CrawlError, 'CLI 失败'):
                    repair_sample(root, '没有可验证样本')
                with self.assertRaisesRegex(CrawlError, '已启动过一次'):
                    repair_sample(root, '再次尝试')
            self.assertEqual(cli.call_count, 1)
            state = read_json(root / 'ai_repair.json')
            self.assertEqual(state['status'], 'failed')
            self.assertIn('seconds', state)
            self.assertIn('cli_seconds', state)
            self.assertTrue((root / 'ai_repair.log').is_file())

    def test_timeout_stops_process_and_preserves_partial_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_root(root)
            process = MagicMock(pid=12345)
            process.communicate.side_effect = subprocess.TimeoutExpired('codex', 1)
            with patch('core.ai_repair.shutil.which', return_value='codex.exe'), \
                    patch('core.ai_repair.subprocess.Popen', return_value=process), \
                    patch('core.ai_repair.subprocess.run'):
                with self.assertRaisesRegex(CrawlError, '超过 1 秒'):
                    repair_sample(root, '结构缺少', timeout=1)
            process.wait.assert_called_once()
            self.assertEqual(read_json(root / 'ai_repair.json')['status'], 'failed')
            self.assertTrue((root / 'ai_repair_candidate/check_sample.py').is_file())
            self.assertFalse((root / 'detail_url.json').exists())

    def test_queue_wait_is_bounded_and_does_not_launch_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_root(root)
            with patch('core.ai_repair.shutil.which', return_value='codex.exe'), \
                    patch('core.ai_repair.AI_SEMAPHORE') as semaphore, \
                    patch('core.ai_repair.subprocess.Popen') as launch:
                semaphore.acquire.return_value = False
                with self.assertRaisesRegex(CrawlError, '等待商品修复会话'):
                    repair_sample(root, '结构错误', timeout=30)
                self.assertLessEqual(semaphore.acquire.call_args.kwargs['timeout'], 30)
                semaphore.release.assert_not_called()
                launch.assert_not_called()
            self.assertEqual(read_json(root / 'ai_repair.json')['cli_invocations'], 0)

    def test_process_cleanup_waits_are_bounded(self):
        process = MagicMock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired('codex', 5), None]
        with patch('core.ai_repair.subprocess.run', side_effect=subprocess.TimeoutExpired('taskkill', 10)):
            _stop_process(process)
        self.assertTrue(process.kill.called)
        self.assertEqual([call.kwargs['timeout'] for call in process.wait.call_args_list], [5, 5])

    def test_policy_rejection_is_reported_instead_of_missing_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_root(root)
            def launch(command, **kwargs):
                kwargs['stdout'].write('exec_command rejected: blocked by policy\n')
                return MagicMock(returncode=0)
            with patch('core.ai_repair.shutil.which', return_value='codex.exe'), \
                    patch('core.ai_repair.subprocess.Popen', side_effect=launch):
                with self.assertRaisesRegex(CrawlError, '审批策略拒绝'):
                    repair_sample(root, '结构错误')
            self.assertIn('审批策略拒绝', read_json(root / 'ai_repair.json')['error'])

    def test_candidate_cannot_expand_selected_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_root(root)
            save_json(root / 'url.json', {'Shop': BASE + '/collections/shop',
                                          'Other': BASE + '/collections/other'})
            def candidate(work):
                write_candidate(work)
                detail = read_json(work / 'detail_url.json')
                save_json(work / 'detail_url.json', {'Other': detail['Shop']})
                save_json(work / 'sample_scope.json', {
                    'selected_categories': {'Other': BASE + '/collections/other'}})
            with patch('core.ai_repair.shutil.which', return_value='codex.exe'), \
                    patch.dict(os.environ, {'AI_REPAIR_TEST_KEEP': 'yes'}), \
                    patch('core.ai_repair.subprocess.Popen', side_effect=self.fake_cli(root, candidate)):
                with self.assertRaisesRegex(CrawlError, '超出本次已选择范围'):
                    repair_sample(root, '结构错误')
            self.assertFalse((root / 'detail_url.json').exists())

    def test_repairable_classification_checks_embedded_and_chained_causes(self):
        for error in (AdapterRequired('unknown theme'), AttributeError('list has no get'),
                      TypeError('unexpected list'), KeyError('variants'), IndexError('empty list'),
                      CrawlError('分类中没有可验证样本'), CrawlError('尚未适配此结构')):
            with self.subTest(error=str(error)):
                self.assertTrue(is_repairable(error))
        for message in ('候选没有可验证样本: HTTP 403', '没有样本: 429',
                        '结构解析失败: connection timeout', '源响应不是 US/USD',
                        '价格与源响应不匹配', '商品身份与原响应不一致',
                        'Cloudflare 拦截', '分类页面超时', '失败原因未知'):
            with self.subTest(message=message):
                self.assertFalse(is_repairable(CrawlError(message)))
        outer = AdapterRequired('没有可验证样本')
        outer.__cause__ = CrawlError('HTTP 403')
        self.assertFalse(is_repairable(outer))

    def test_prompt_points_to_bundled_adapter_and_actual_source_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_json(root / 'samples.json', [
                {'url': BASE + '/item', 'source_mapping': {
                    'kind': 'bigcommerce_storefront', 'file': 'evidence/observed-product.html'}},
                {'source_mapping': None},
            ])
            prompt = _prompt('samples.json 的 price 类型错误', root)
            self.assertIn('core.bigcommerce_storefront', prompt)
            self.assertIn('evidence/observed-product.html', prompt)
            self.assertIn('parse_product(', prompt)
            self.assertIn('允许并优先 import', prompt)
            self.assertIn('--offline', prompt)
            self.assertIn('--all 抓全部链接', prompt)
            self.assertIn('脚本正常结束时应自行', prompt)
            self.assertIn('inspect_live_page.py', prompt)
            self.assertIn('禁止用正则盲搜', prompt)
            self.assertIn('240秒前', prompt)
            self.assertIn('探索严格限制为两步', prompt)
            self.assertIn('第90秒前', prompt)
            self.assertIn('product_fields', prompt)
            self.assertIn('RFC 6901', prompt)
            self.assertIn('严禁读取 core/ 下无关适配器', prompt)
            self.assertNotIn('自包含', prompt)


if __name__ == '__main__':
    unittest.main()
