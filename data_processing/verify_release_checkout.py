"""Replay the published inputs in a checkout containing only versioned/reviewable files."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def signature(data):
    info=data['information']
    return dict(prices=sorted((c['city'],r['date'],r['price'],r['method']) for c in data['cities'] for r in c['monthly']),
                cih=sorted((r['city'],r['kind'],r['period'],r['price']) for r in info['cih']['prices']),
                income=info['income']['income'],wages=info['income']['minimum_wages'],
                lpr=[(r['as_of'],r['one_year'],r['five_year'],r['unit']) for r in info['macro']['lpr']['history']],
                realestate=info['macro']['realestate']['history'],
                market=sorted((kind,c['city'],r['month'],r.get('price'),r.get('price_source'),r.get('provisional',False),r.get('fetched_at'))
                              for kind,segment in data['housing_segments'].items()
                              for c in segment['cities'] for r in c['monthly']))


def verify(checkout):
    sys.stdout.reconfigure(encoding='utf-8')
    paths=subprocess.check_output(['git','ls-files','-co','--exclude-standard','-z'],cwd=ROOT).decode('utf-8').split('\0')
    for name in set(paths):
        if not name or name.startswith('data_processing/frontend/realestate/') or name.endswith(('.tmp','.log')):continue
        source=ROOT/name
        if not source.is_file():continue
        destination=checkout/name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
    assert not (checkout/'output/income').exists(), 'Local ignored inputs leaked into clean checkout'
    before=json.loads((checkout/'data_processing/frontend/intersection_housing_data.json').read_text(encoding='utf-8'))
    log=ROOT/'output/release-clean-checkout.log'
    env=dict(os.environ,PYTHONIOENCODING='utf-8')
    # The pipeline entry already runs the complete isolated offline updater.
    commands=[['release_inputs.py'],['pipeline.py','--skip-crawl'],
              ['verify_frontend_data.py','--dashboard-only'],['build_pages.py']]
    with log.open('w',encoding='utf-8') as stream:
        for command in commands:
            result=subprocess.run([sys.executable,str(checkout/'data_processing'/command[0]),*command[1:]],cwd=checkout,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=2400)
            if result.returncode:raise RuntimeError('Clean checkout command failed: '+str(command)+'; log: '+str(log))
    after=json.loads((checkout/'data_processing/frontend/intersection_housing_data.json').read_text(encoding='utf-8'))
    assert signature(before)==signature(after), 'Offline replay changed published prices or source histories'
    assert not (checkout/'dist/pages/realestate').exists(), 'Unpublished map leaked into build'
    assert not list((checkout/'output/update_work').glob('run-*')), 'Completed update worker was not removed'
    assert not (checkout/'output/warehouse/clean/project').exists(), 'Project history still creates loose copies'
    from snapshot_archive import SnapshotArchive
    with SnapshotArchive(checkout,readonly=True) as archive:
        hashes=[r[0] for r in archive.db.execute('SELECT digest FROM objects')]
        assert hashes, 'Offline updater did not preserve project history'
        for sha in hashes:archive.verify(sha)
    report=dict(state='passed',checkout=str(checkout),commands=commands,
                cities=len(after['cities']),price_rows=len(signature(after)['prices']),
                cih_rows=len(signature(after)['cih']),income_years=len(signature(after)['income']),wage_releases=len(signature(after)['wages']),
                lpr_months=len(signature(after)['lpr']),
                realestate_reports=len(signature(after)['realestate']),
                typed_market_quotes=sum(r[3] is not None for r in signature(after)['market']),
                price_and_source_histories_unchanged=True,map_included=False,
                isolated_offline_update_verified=True,worker_workspaces_removed=True,
                packed_history_objects=len(hashes),loose_project_history_created=False)
    (ROOT/'output/release-clean-checkout-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


def main(keep_checkout=False):
    from update_workspace import UpdateWorkspace
    with UpdateWorkspace(ROOT).single_writer():
        parent=ROOT/'output/release-checkouts';parent.mkdir(parents=True,exist_ok=True)
        checkout=Path(tempfile.mkdtemp(prefix='verified-',dir=parent))
        try:
            verify(checkout)
        except Exception as exc:
            (ROOT/'output/release-clean-checkout-report.json').write_text(json.dumps(
                dict(state='failed',checkout=str(checkout),error=str(exc)),ensure_ascii=False,indent=2),encoding='utf-8')
            raise
        finally:
            if not keep_checkout:
                target=checkout.resolve()
                if target.parent!=parent.resolve() or target==parent.resolve():
                    raise ValueError('Invalid release checkout cleanup target')
                shutil.rmtree(target)
                report=ROOT/'output/release-clean-checkout-report.json'
                if report.exists():
                    saved=json.loads(report.read_text(encoding='utf-8'))
                    if saved.get('checkout')==str(checkout):
                        saved['checkout_removed']=True
                        report.write_text(json.dumps(saved,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep-checkout',action='store_true',help='Keep the temporary checkout for explicit debugging')
    main(parser.parse_args().keep_checkout)

