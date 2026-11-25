"""Refresh exports for an existing completed run using its ORIGINAL time interval."""
import argparse
import datetime as dt
import json
from pathlib import Path
import urllib.parse
from metrics import QUERIES
from run import VM, fetch, save

def export(run):
    manifest=json.loads((run/'manifest.json').read_text(encoding='utf-8'))
    if manifest['status'] not in ('completed','export_failed'): raise ValueError('Load run must have completed')
    exported={}
    for key,expr in QUERIES.items():
        params={'query':expr,'start':manifest['start'],'end':manifest['end'],'step':2,'nocache':1,'latency_offset':'1ms'}
        data=fetch(VM+'/api/v1/query_range?'+urllib.parse.urlencode(params))
        if data.get('status')!='success' or not data.get('data',{}).get('result'): raise ValueError('Missing data for '+key)
        exported[key]=data
    # Validate every query before replacing previous exports.
    for key,data in exported.items(): save(run/(key+'.json'),data)
    save(run/'queries.json',QUERIES)
    save(run/'export-info.json',{'exported_at':dt.datetime.now(dt.timezone.utc).isoformat(),'vm':VM,
                               'start':manifest['start'],'end':manifest['end'],'step':2,'nocache':1,'latency_offset':'1ms'})
    print('Exported original interval:',run)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('run',type=Path); export(p.parse_args().run)
