"""Run a fixed site cohort through the actual workbench HTTP entry point."""
import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--selection',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--platform',default='http://127.0.0.1:8880')
    parser.add_argument('--retry-of',help='Prior summary retained separately when rerunning failed cases')
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--visual-check',action='store_true')
    args=parser.parse_args()
    output=Path(args.out);output.mkdir(parents=True,exist_ok=False)
    selection=json.loads(Path(args.selection).read_text(encoding='utf-8'))
    (output/'selection.json').write_text(json.dumps(selection,ensure_ascii=False,indent=2),encoding='utf-8')
    def request(path,payload=None):
        data=json.dumps(payload).encode() if payload is not None else None
        req=urllib.request.Request(args.platform+path,data=data,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=15) as response:return json.load(response)
    def run(site):
        start=time.monotonic()
        job=request('/api/jobs',{'site':site['site'],'visual_check':args.visual_check,'manual':False,'full_products':False,'submitter':'shopify-platform-acceptance'})
        (output/(site['name']+'.job.json')).write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8')
        previous=None
        while True:
            current=next(j for j in request('/api/jobs') if j['id']==job['id'])
            if current['status']!=previous:
                print(site['name'],job['id'],current['status'],flush=True);previous=current['status']
            if current['status'] in ('completed','failed','interrupted','needs_recheck'):break
            time.sleep(5)
        root=Path('runs')/job['id']
        def read(name):
            p=root/name
            return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
        report=read('report.json');failure=read('failure.json');audit=read('visual_audit.json');release=read('navigation_release.json')
        timing=read('navigation_timing.json');digest=read('evidence/nav-ledger.json')
        result={**site,'job_id':job['id'],'status':current['status'],'seconds':report.get('seconds',failure.get('seconds')),
                'wall_seconds':round(time.monotonic()-start,3),'categories':len(read('url.json')),
                'candidate_categories':len(read('visual_candidate.json') or read('program_candidate.json')),
                'visual_status':audit.get('status'),'navigation_status':release.get('status'),
                'navigation_ai_seconds':timing.get('cli_seconds',0),'visual_ai_seconds':audit.get('cli_seconds',0),
                'ai_queue_seconds':timing.get('ai_wait_seconds',0)+audit.get('ai_wait_seconds',0),
                'digest_bytes':digest.get('digest_bytes'),'slim_bytes':timing.get('slim_bytes'),
                'navigation_seconds':report.get('nav_seconds'),'navigation_mode':timing.get('mode'),
                'auto_pruned_roots':audit.get('auto_pruned_roots',[]),
                'warnings':read('warnings.json'),'inventory_seconds':read('evidence/shopify_collections.json').get('seconds'),
                'target_validation_seconds':release.get('target_validation_seconds'),
                'error':failure.get('error'),'report':str(root/'report.json'),'json':str(root/'url.json')}
        (output/(site['name']+'.result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result
    with ThreadPoolExecutor(max_workers=args.workers) as pool:results=list(pool.map(run,selection))
    passed=sum(r['status']=='completed' for r in results)
    summary={'scope':'platform: directory + default category listing + first 3 product details + package; not whole-store crawl','visual_check':args.visual_check,
             'first_attempt_only':not bool(args.retry_of),'retry_of':args.retry_of,'total':len(results),'passed':passed,'success_rate':passed/len(results),'results':results}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
