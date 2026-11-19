import copy
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.common import AdapterRequired, CrawlError, read_json, save_json
from core.pipeline import run_job
from core.simple_nav import publish_simple
from core.validator import validate
from tests.test_core import BASE, fixture, product


HOME = '<header><a href="/collections/shop">Shop</a></header>'
CATEGORIES = {'Shop': BASE + '/collections/shop'}
DETECTION = {
    'mode': 'custom_site', 'custom_platform': 'bigcommerce',
    'market': {'confirmed': True, 'countries': ['US'], 'currencies': ['USD']},
}


class CachedClient:
    def __init__(self):
        self.cache = {}

    def get(self, url, **kwargs):
        response = SimpleNamespace(url=url, text=HOME, headers={'Content-Type': 'text/html'})
        self.cache[url] = response
        return response


def completed_navigation(root, base, html, platform, **kwargs):
    root = Path(root)
    save_json(root / 'navigation_invocation.json', {'invocations': 1})
    release = publish_simple(root, CATEGORIES, method='custom_bigcommerce')
    return CATEGORIES, {'method': 'simple_nav', 'roots': []}, [html], release


def recovered_artifacts(root, reason):
    root = Path(root)
    samples, detail, listings, _ = copy.deepcopy(fixture())
    save_json(root / 'detail_url.json', detail)
    save_json(root / 'listings.json', listings)
    save_json(root / 'samples.json', samples)
    for sample in samples:
        save_json(root / 'evidence' / f'product-{sample["product"]["id"]}.json', sample['product'])
    save_json(root / 'ai_repair.json', {'status': 'completed', 'invocations': 1, 'reason': reason})
    (root / 'crawl_products.py').write_text('print("offline repair fixture")\n', encoding='utf-8')


def pipeline_dependencies(stack, error):
    stack.enter_context(patch('core.pipeline.Client', CachedClient))
    stack.enter_context(patch('core.detector.resolve_storefront', side_effect=lambda client, base, response: response))
    stack.enter_context(patch('core.custom_site.identify_platform', return_value='bigcommerce'))
    stack.enter_context(patch('core.custom_site.run_navigation', side_effect=completed_navigation))
    stack.enter_context(patch('core.pipeline.detect', return_value=copy.deepcopy(DETECTION)))
    return stack.enter_context(patch('core.custom_site.run_custom_sample', side_effect=error))


class PipelineAIRepairTests(unittest.TestCase):
    def test_hydrogen_structural_failure_uses_controlled_repair_context(self):
        detection = {
            'mode': 'hydrogen_shopify', 'custom_platform': '',
            'market': {'confirmed': True, 'countries': ['US'], 'currencies': ['USD']},
        }
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            stack.enter_context(patch('core.pipeline.Client', CachedClient))
            stack.enter_context(patch(
                'core.detector.resolve_storefront', side_effect=lambda client, base, response: response))
            stack.enter_context(patch('core.custom_site.identify_platform', return_value=''))

            def simple_navigation(work, base, html):
                categories, evidence, documents, _ = completed_navigation(
                    work, base, html, '')
                return categories, evidence, documents

            stack.enter_context(patch('core.simple_nav.run_simple', side_effect=simple_navigation))
            stack.enter_context(patch('core.pipeline.detect', return_value=detection))
            structural = AdapterRequired('Hydrogen 分类 loader 结构需要适配')
            listing = stack.enter_context(patch(
                'core.pipeline.collect_listing', side_effect=structural))
            repair = stack.enter_context(patch(
                'core.ai_repair.repair_sample', side_effect=recovered_artifacts))

            report = run_job(BASE, root)

            listing.assert_called()
            repair.assert_called_once_with(root, str(structural))
            self.assertEqual(report['status'], 'completed')
            context = read_json(root / 'evidence/repair_failure_context.json')
            self.assertEqual(context['error_type'], 'AdapterRequired')
            self.assertEqual(context['attempted_category_urls'], [BASE + '/collections/shop'])

    def test_catalog_only_custom_site_stops_before_ai_repair(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            pipeline_dependencies(stack, AssertionError('native collector must be bypassed'))
            stack.enter_context(patch('core.custom_site.identify_platform', return_value=''))

            def simple_navigation(work, base, html):
                categories, evidence, documents, _ = completed_navigation(work, base, html, '')
                return categories, evidence, documents

            stack.enter_context(patch('core.simple_nav.run_simple', side_effect=simple_navigation))
            detection = dict(copy.deepcopy(DETECTION), custom_platform='')
            stack.enter_context(patch('core.pipeline.detect', return_value=detection))
            diagnosis = {
                'catalog_only': True,
                'listing_url': BASE + '/products/',
                'product_links': [BASE + f'/product/p{i}/' for i in range(3)],
                'checked_products': [],
                'reason': 'no commerce',
            }
            probe = stack.enter_context(patch(
                'core.custom_navigation.detect_catalog_only', return_value=diagnosis))
            repair = stack.enter_context(patch('core.ai_repair.repair_sample'))
            with self.assertRaisesRegex(CrawlError, '目录型品牌站'):
                run_job(BASE, root)
            probe.assert_called_once()
            repair.assert_not_called()
            self.assertEqual(read_json(root / 'catalog_only.json'), diagnosis)
            self.assertFalse((root / 'ai_repair.json').exists())

    def test_full_custom_repair_is_validated_without_restarting_shopify_collection(self):
        for platform, force_ai in [('', False), ('bigcommerce', True)]:
            with self.subTest(platform=platform, force_ai=force_ai), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                native = pipeline_dependencies(stack, AssertionError('native collector must be bypassed'))
                stack.enter_context(patch('core.custom_site.identify_platform', return_value=platform))
                def simple_navigation(work, base, html):
                    categories, evidence, documents, _ = completed_navigation(work, base, html, platform)
                    return categories, evidence, documents
                stack.enter_context(patch('core.simple_nav.run_simple', side_effect=simple_navigation))
                detection = dict(copy.deepcopy(DETECTION), custom_platform=platform or 'unknown')
                stack.enter_context(patch('core.pipeline.detect', return_value=detection))
                shopify = stack.enter_context(patch(
                    'core.shopify_fastpath.run_products', side_effect=AssertionError('repaired data must be retained')))

                def repair_all(work, reason):
                    scope = read_json(work / 'sample_scope.json')
                    self.assertEqual(scope['detail_scope'], 'all_products')
                    recovered_artifacts(work, reason)
                    samples = read_json(work / 'samples.json')
                    detail = read_json(work / 'detail_url.json')
                    listings = read_json(work / 'listings.json')
                    fourth = product(4)
                    url = BASE + '/products/p4'
                    samples.append({'url': url, 'source': 'json', 'product': fourth})
                    detail['Shop'].append(url)
                    listings['Shop']['feeds'][0]['products'].append(fourth)
                    page = listings['Shop']['pages'][0]
                    page['html'] = page['html'].replace('</main>', '<a href="/products/p4">P4</a></main>')
                    save_json(work / 'samples.json', samples)
                    save_json(work / 'detail_url.json', detail)
                    save_json(work / 'listings.json', listings)
                    save_json(work / 'evidence/product-4.json', fourth)
                    from core.ai_repair import _check_candidate
                    return _check_candidate(work, scope)

                repair = stack.enter_context(patch('core.ai_repair.repair_sample', side_effect=repair_all))
                report = run_job(BASE, root, full_products=True, force_ai=force_ai)
                native.assert_not_called()
                repair.assert_called_once()
                shopify.assert_not_called()
                self.assertEqual(report['status'], 'completed')
                self.assertEqual(report['validation']['products'], 4)
                self.assertEqual(read_json(root / 'sample_scope.json')['detail_scope'], 'all_products')
                self.assertEqual(len(read_json(root / 'samples.json')), 4)
                self.assertTrue((root / 'crawler.zip').is_file())

    def test_structural_failure_reaches_repair_after_navigation_ai_and_packages_valid_samples(self):
        errors = [TypeError('unexpected menu adapter response shape'),
                  AdapterRequired('商品网格结构需要适配'),
                  AdapterRequired('Demandware/SFCC 客户端渲染页面，需通过浏览器或 API 适配')]
        for error in errors:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                native = pipeline_dependencies(stack, error)
                repair = stack.enter_context(patch('core.ai_repair.repair_sample', side_effect=recovered_artifacts))
                events = []
                report = run_job(BASE, root, emit=events.append)
                native.assert_called_once()
                repair.assert_called_once_with(root, str(error))
                self.assertEqual(read_json(root / 'navigation_invocation.json'), {'invocations': 1})
                self.assertEqual(report['status'], 'completed')
                self.assertEqual(report['llm_invocations'], 2)
                self.assertTrue(report['validation']['passed'])
                self.assertEqual(len(report['samples']), 3)
                self.assertTrue(validate(root)['passed'])
                self.assertEqual(report['sample_category_attempts'][0]['status'], 'ai_repaired')
                self.assertIn('ai_repair', [event['stage'] for event in events])
                sources = read_json(root / 'evidence/repair-sources/index.json')
                self.assertEqual(len(sources), 1)
                self.assertEqual(sources[0]['url'], BASE + '/')
                self.assertEqual((root / sources[0]['file']).read_text(encoding='utf-8'), HOME)
                context = read_json(root / 'evidence/repair_failure_context.json')
                self.assertEqual(context['error_type'], type(error).__name__)
                self.assertEqual(context['attempted_category_urls'], [BASE + '/collections/shop'])
                self.assertEqual(context['cached_responses'][0]['bytes'], len(HOME.encode()))
                self.assertIn('failure_location', context)
                self.assertIn('traceback', context)
                with zipfile.ZipFile(root / 'crawler.zip') as archive:
                    self.assertTrue({'url.json', 'detail_url.json', 'samples.json', 'result.csv',
                                     'shopify_import.csv', 'ai_repair.json', 'navigation_invocation.json',
                                     'evidence/product-1.json', 'verify.py'}.issubset(archive.namelist()))

    def test_network_and_source_integrity_failures_never_enter_repair(self):
        errors = [CrawlError('HTTP Error 403 Forbidden'),
                  CrawlError('商品价格与源响应不一致'),
                  CrawlError('SKU identity mismatch'),
                  AdapterRequired('无法解析 HTTP 403 Forbidden 响应')]
        for error in errors:
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                native = pipeline_dependencies(stack, error)
                repair = stack.enter_context(patch('core.ai_repair.repair_sample'))
                with self.assertRaises(CrawlError) as raised:
                    run_job(BASE, root)
                self.assertIs(raised.exception, error)
                native.assert_called_once()
                repair.assert_not_called()
                failure = read_json(root / 'failure.json')
                self.assertEqual(failure['error'], str(error))
                self.assertEqual(failure['exception_type'], type(error).__name__)
                self.assertIn(type(error).__name__, failure['traceback'])
                self.assertFalse((root / 'crawler.zip').exists())
                self.assertFalse((root / 'ai_repair.json').exists())


if __name__ == '__main__':
    unittest.main()
