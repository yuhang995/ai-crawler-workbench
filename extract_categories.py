"""Extract and audit category JSON without starting product collection."""
import argparse
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

_bundled_skill = Path(__file__).resolve().parent / 'skills/category-json'
if (_bundled_skill / 'SKILL.md').is_file():
    os.environ.setdefault('CATEGORY_JSON_SKILL', str(_bundled_skill))
from core.common import Client, save_json, read_json, CrawlError
from core.detector import resolve_storefront
from core.navigation_ai import run_navigation
from core.navigation_release import publish


def main():
    parser = argparse.ArgumentParser(description='提取并审核目录 JSON；不启动商品详情采集')
    parser.add_argument('--site', required=True)
    parser.add_argument('--out', required=True, help='必须为新的输出目录，避免旧结果混入')
    parser.add_argument('--visual-check', action='store_true', help='配对截图与 AI 视觉审核；实用模式疑点记入警告')
    parser.add_argument('--navigation-mode',choices=('simple','practical','ai'),default='simple')
    parser.add_argument('--category-file', type=Path, help='校验并发布现成的 url.json，不自动扣目录')
    args = parser.parse_args()
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / 'evidence').mkdir()
    started = time.perf_counter()
    try:
        client = Client()
        site = args.site if '://' in args.site else 'https://' + args.site
        p = urlsplit(site)
        base = p._replace(path='/', query='', fragment='').geturl()
        response = resolve_storefront(client, base, client.get(base))
        (root / 'evidence/home.html').write_text(response.text, encoding='utf-8')
        if args.category_file:
            from core.simple_nav import validate_uploaded_categories, publish_simple
            categories = validate_uploaded_categories(read_json(args.category_file), response.url)
            save_json(root/'uploaded_url.json', categories)
            proof,documents={'method':'simple_nav','source':'uploaded_category_file','roots':[]},[response.text]
            release=publish_simple(root,categories,method='uploaded_category_file',
                                   scope='uploaded_category_file_structure_and_same_origin')
        elif args.navigation_mode=='simple':
            from core.custom_site import identify_platform, SUPPORTED_PLATFORMS, run_navigation as run_custom_navigation
            platform = identify_platform(response.text)
            if platform in SUPPORTED_PLATFORMS:
                categories,proof,documents,_ = run_custom_navigation(
                    root, response.url, response.text, platform, client=client)
            else:
                from core.simple_nav import run_simple
                categories,proof,documents=run_simple(root,response.url,response.text)
        elif args.navigation_mode=='practical':
            from core.detector import collection_inventory
            from core.practical_navigation import build_practical
            inventory=collection_inventory(client,response.url,response.text,root/'evidence')
            categories,proof,documents=build_practical(root,response.url,response.text,inventory)
        else:categories, proof, documents = run_navigation(root, response.url, emit=lambda e: print(e, flush=True))
        if args.visual_check and args.navigation_mode!='simple' and not args.category_file:
            from core.visual_audit import run_visual_audit
            try:run_visual_audit(root, response.url, categories, proof)
            except CrawlError as error:
                if args.navigation_mode!='practical':raise
                from core.practical_navigation import warn
                warn(root,'visual_audit_warning',str(error))
        if args.category_file:
            pass
        elif args.navigation_mode=='simple':
            from core.common import read_json
            release=read_json(root/'navigation_release.json')
            if not categories:raise CrawlError('AI 未生成可用分类目录')
        else:
            release = publish(root, categories, proof, documents, response.url, client, visual_check=args.visual_check)
        save_json(root / 'report.json', {'status': 'completed', 'scope': 'navigation_only', 'categories': len(categories), 'navigation_version': release['navigation_version'], 'seconds': round(time.perf_counter()-started, 3), 'target_validation_seconds': release['target_validation_seconds']})
        print('目录审核通过：', root / 'url.json')
        return 0
    except Exception as error:
        save_json(root / 'failure.json', {'status': 'blocked', 'scope': 'navigation_only', 'error': str(error), 'seconds': round(time.perf_counter()-started, 3)})
        print('目录审核未通过：', error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
