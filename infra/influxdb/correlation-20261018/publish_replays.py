"""Idempotent additive import and exact read-back verification of screenshot replays."""
import argparse
import base64
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import urlencode

os.environ.pop('SSLKEYLOGFILE', None)
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA = ROOT/'replay' if (ROOT/'replay').is_dir() else ROOT
INFLUX = os.getenv('INFLUX_URL', 'http://127.0.0.1:8086').rstrip('/')
GRAFANA = os.getenv('GRAFANA_URL', 'http://127.0.0.1:3001').rstrip('/')
ORG = os.getenv('INFLUXDB_ORG', 'atropos')
BUCKET = os.getenv('INFLUXDB_BUCKET', 'metrics-replay')
TOKEN = os.getenv('INFLUXDB_TOKEN', 'atropos-local-replay-token')
UID = 'correlation-change-20261018'
MEASUREMENT = 'correlation_screenshot_replay'


def request(url, data=None, headers=None):
    with urlopen(Request(url, data=data, headers=headers or {}), timeout=60) as response:
        return response.read().decode()


def query():
    flux = f'''from(bucket: {json.dumps(BUCKET)})
      |> range(start: 2026-10-17T21:00:00Z, stop: 2026-10-19T21:00:00Z)
      |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "value")'''
    raw = request(INFLUX+'/api/v2/query?'+urlencode({'org': ORG}), flux.encode(),
        {'Authorization': 'Token '+TOKEN, 'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
    header, values = None, {}
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith('#'):
            continue
        if '_time' in fields and '_value' in fields:
            header = fields
        elif header:
            row = dict(zip(header, fields))
            t = int(datetime.fromisoformat(row['_time'].replace('Z','+00:00')).timestamp())
            key = row['scenario'], row['trace'], t
            if key in values:
                raise ValueError('duplicate target database key')
            values[key] = float(row['_value'])
    return values


def dashboard(meta):
    colors = {'blue':'#5794F2','green':'#73BF69','yellow':'#F2CC0C',
              'orange':'#FF9830','red':'#F2495C','purple':'#B877D9'}
    panels = []
    for pid, (case, scenario) in enumerate(meta['scenarios'].items(), 1):
        stop = (datetime.fromisoformat(scenario['end'])+timedelta(seconds=scenario['step_seconds'])).isoformat()
        targets = []
        for index, trace in enumerate(scenario['traces']):
            flux = f'''from(bucket: {json.dumps(BUCKET)})
              |> range(start: time(v: "{scenario['start']}"), stop: time(v: "{stop}"))
              |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and
                 r._field == "value" and r.scenario == "{case}" and r.trace == "{trace}")
              |> keep(columns: ["_time", "_value"])
              |> rename(columns: {{_value: "{trace}"}})'''
            targets.append({'refId': chr(65+index), 'datasource': {'type':'influxdb','uid':'influxdb-replay'},
                            'query':flux})
        defaults = {'unit': 'suffix:ms' if case == 'latency' else 'ops', 'min':0,
            'custom':{'drawStyle':'line','lineInterpolation':'linear','lineWidth':1,'fillOpacity':0,
                      'showPoints':'never','spanNulls':False,'axisLabel':scenario['unit']}}
        overrides = [{'matcher':{'id':'byName','options':name},
                      'properties':[{'id':'color','value':{'mode':'fixed','fixedColor':colors[name]}}]}
                     for name in scenario['traces']]
        detail_url = '/d/'+UID+'?from='+str(int(datetime.fromisoformat(scenario['start']).timestamp()*1000))+'&to='+str(int(datetime.fromisoformat(stop).timestamp()*1000))+'&timezone=Europe%2FMoscow&viewPanel=panel-'+str(pid)
        panels.append({'id':pid,'type':'timeseries','title': 'Задержка — 18.10.2026' if case=='latency' else 'Read operations — 18–19.10.2026',
            'description':'Приближённая оцифровка приложенного графика. Все сохранённые точки без усреднения. '+
                          'Шаг '+str(scenario['step_seconds'])+' с. Перекрытия интерполированы.',
            'datasource':{'type':'influxdb','uid':'influxdb-replay'},
            'gridPos':{'h':13,'w':24,'x':0,'y':(pid-1)*13},'maxDataPoints':20000,
            'targets':targets,'links':[{'title':'Полный масштаб этого графика','url':detail_url,'targetBlank':False}],
            'fieldConfig':{'defaults':defaults,'overrides':overrides},
            'options':{'legend':{'displayMode':'list','placement':'bottom'},'tooltip':{'mode':'multi'}}})
    return {'uid':UID,'title':'Корреляция — примеры 18.10.2026','schemaVersion':41,
        'version':1,'editable':True,'timezone':'Europe/Moscow','refresh':'',
        'tags':['atropos','replay','correlation'], 'panels':panels,
        'description':'Восстановлены три видимые линии задержки и четыре линии read operations. '+
                      'Скрытые линии легенды недоступны; данные не являются исходной телеметрией.',
        'time':{'from':'2026-10-18T13:52:45Z','to':'2026-10-19T08:12:00Z'},
        'annotations':{'list':[]}, 'links':[]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    metadata = json.loads((DATA/'metadata.json').read_text(encoding='utf-8'))
    assert hashlib.sha256((DATA/'metrics.csv').read_bytes()).hexdigest() == metadata['csv_sha256']
    with (DATA/'metrics.csv').open(encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    expected = {(r['scenario'],r['trace'],int(r['unix_seconds'])):float(r['value']) for r in rows}
    assert len(expected) == len(rows) == metadata['points']
    actual = query()
    # Never overwrite unrelated/existing values, including a changed prior replay.
    assert all(k in expected and expected[k] == v for k,v in actual.items()), 'Target data conflict; refusing overwrite'
    dash = dashboard(metadata)
    (DATA/'dashboard.json').write_text(json.dumps(dash, ensure_ascii=False, indent=2)+'\n',encoding='utf-8')
    if args.write:
        if actual != expected:
            request(INFLUX+'/api/v2/write?'+urlencode({'org':ORG,'bucket':BUCKET,'precision':'s'}),
                    (DATA/'metrics.lp').read_bytes(),
                    {'Authorization':'Token '+TOKEN,'Content-Type':'text/plain; charset=utf-8'})
        actual = query()
        assert actual == expected, 'InfluxDB values differ from the digitization'
        auth = {'Authorization':'Basic '+base64.b64encode((os.getenv('GRAFANA_ADMIN_USER','admin')+':'+
                        os.getenv('GRAFANA_ADMIN_PASSWORD','admin')).encode()).decode()}
        folders = json.loads(request(GRAFANA+'/api/search?type=dash-folder',headers=auth))
        folder = next((f['uid'] for f in folders if f['title']=='Atropos'), None)
        if folder is None:
            folder = json.loads(request(GRAFANA+'/api/folders',json.dumps({'title':'Atropos'}).encode(),
                                {**auth,'Content-Type':'application/json'}))['uid']
        request(GRAFANA+'/api/dashboards/db',json.dumps({'dashboard':dash,'folderUid':folder,'overwrite':True}).encode(),
                {**auth,'Content-Type':'application/json'})
        live = json.loads(request(GRAFANA+'/api/dashboards/uid/'+UID,headers=auth))['dashboard']
        assert len(live['panels']) == 2 and live['timezone'] == 'Europe/Moscow'
        # Verify Grafana's datasource sees exactly the database values used by tests.
        seen = {}
        for panel, (case, info) in zip(live['panels'],metadata['scenarios'].items()):
            payload = {'queries':[{**t,'intervalMs':info['step_seconds']*1000,'maxDataPoints':20000}
                                  for t in panel['targets']],
                       'from':str(int(datetime.fromisoformat(info['start']).timestamp()*1000)),
                       'to':str(int(datetime.fromisoformat(info['end']).timestamp()*1000)+info['step_seconds']*1000)}
            result = json.loads(request(GRAFANA+'/api/ds/query',json.dumps(payload).encode(),
                               {**auth,'Content-Type':'application/json'}))['results']
            for ref, trace in zip('ABCD', info['traces']):
                assert not result[ref].get('error'), result[ref].get('error')
                for frame in result[ref].get('frames',[]):
                    fields, data = frame['schema']['fields'],frame['data']['values']
                    ti = next(i for i,f in enumerate(fields) if f['type']=='time')
                    vi = next(i for i,f in enumerate(fields) if f['type']=='number')
                    seen.update(((case,trace,int(t/1000)),v) for t,v in zip(data[ti],data[vi]))
        assert seen == expected, 'Grafana datasource did not return all exact replay points'
    report = {'passed': actual == expected,'points':len(actual), 'expected_points':len(expected),
              'measurement':MEASUREMENT,'exact_values_verified':actual==expected,
              'grafana_exact_values_verified':bool(args.write),
              'dashboard_url':GRAFANA+'/d/'+UID,'scenarios':metadata['scenarios']}
    (DATA/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='scenarios'},indent=2))


if __name__ == '__main__':
    main()
