"""Two aligned raw-data panels for Cassandra response time and reconstructed TPS."""
from pathlib import Path
from copy import deepcopy
from datetime import datetime,timezone
import json

ROOT=Path(__file__).resolve().parent
metadata=json.loads((ROOT/'metadata.json').read_text(encoding='utf-8'))
DS={'type':'influxdb','uid':'influxdb-replay'}
COLORS={'green':'#73BF69','yellow':'#E0B400','gray':'#B6B9BD','red':'#F2495C','olive':'#B3A84B','teal':'#56A89A','orange':'#FF9830'}
def query(measurement,tag):
    return ('import "date"\nfrom(bucket: v.defaultBucket)\n'
            '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
            f'  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "value")\n'
            f'  |> keep(columns: ["_time", "_value", "{tag}"])\n'
            f'  |> group(columns: ["{tag}"])\n  |> sort(columns: ["_time"])')
def panel(pid,title,metric,tag,unit,maximum,y,height,colors):
    return {'id':pid,'type':'timeseries','title':title,'datasource':DS,
            'description':'Raw 15-second points, no aggregateWindow. Screenshot reconstruction is approximate.',
            'gridPos':{'x':0,'y':y,'w':24,'h':height},'interval':'15s','maxDataPoints':10000,
            'targets':[{'refId':'A','datasource':DS,'query':query(metric,tag)}],
            'fieldConfig':{'defaults':{'unit':unit,'min':0,'max':maximum,'decimals':2 if unit=='ms' else 1,
                                      'displayName':'${__field.labels.'+tag+'}',
                                      'custom':{'drawStyle':'line','lineInterpolation':'stepAfter','lineWidth':1,
                                                'fillOpacity':7 if unit=='ops' else 0,'showPoints':'never','spanNulls':False,
                                                'scaleDistribution':{'type':'linear'},'stacking':{'mode':'none','group':'A'},
                                                'hideFrom':{'tooltip':False,'viz':False,'legend':False}}},
                           'overrides':[{'matcher':{'id':'byName','options':name},'properties':[
                               {'id':'color','value':{'mode':'fixed','fixedColor':color}}]} for name,color in colors.items()]},
            'options':{'legend':{'showLegend':True,'displayMode':'list','placement':'bottom','calcs':[]},
                       'tooltip':{'mode':'multi','sort':'none','hideZeros':False}}}
node_colors={'cassandra-1':'#5794F2','cassandra-2':'#FF9830','cassandra-3':'#F2495C','cassandra-4':'#B877D9'}
rows=['| Событие (02.10.2026, Москва) | Время | TPS |','| --- | --- | --- |']
names={'first_node_failure':'Отказ первой ноды','node2_plateau':'Плато ноды 2','node3_plateau':'Плато обеих нод',
       'node3_terminal_drop':'Последний спад ноды 3','node2_terminal_drop':'Последний спад ноды 2'}
effects={'first_node_failure':'Без изменения уровня','node2_plateau':'Уровень ещё сохраняется',
         'node3_plateau':'Частичное падение, нагрузка остаётся','node3_terminal_drop':'Нагрузка остаётся положительной',
         'node2_terminal_drop':'Все TPS = 0'}
for event in metadata['events']:
    rows.append(f'| {names[event["name"]]} | {datetime.fromisoformat(event["time"]).strftime("%H:%M:%S")} | {effects[event["name"]]} |')
content='**Реконструкция скриншота; имена семи TPS-серий заданы по цветам, поскольку легенда отсутствует.**\n\n'
content+='ВО растёт без отдельного падения TPS; частичное падение совмещено с выходом обеих нод на плато. '
content+='После последнего спада ноды 2 TPS обнулены. Колебания восстановлены по изображению; дополнительное изменение шума не придумано.\n\n'
content+='\n'.join(rows)
dashboard={'id':None,'uid':'cassandra-tps-correlation','title':'Cassandra response time / TPS — correlated replay',
           'schemaVersion':41,'version':1,'editable':True,'timezone':'Europe/Moscow','refresh':'','graphTooltip':1,
           'fiscalYearStartMonth':0,'tags':['atropos','influxdb','replay','cassandra','tps','correlation'],
           'description':'TPS reconstructed from the supplied image and time-aligned to the existing Cassandra history confirmed by the user.',
           'annotations':{'list':[]},'links':[{'type':'link','title':'Исходный Cassandra-график','url':'/d/cassandra-response-time-replay','targetBlank':False}],
           'time':{field:datetime.fromisoformat(metadata[name]).astimezone(timezone.utc).isoformat().replace('+00:00','Z') for field,name in [('from','start'),('to','end')]},
           'timepicker':{'hidden':False},'templating':{'list':[]},
           'panels':[panel(1,'Cassandra · время отклика','cassandra_response_time','host','ms',40,0,12,node_colors),
                     panel(2,'TPS · реконструкция с привязкой к Cassandra','cassandra_tps','trace','ops',800,12,16,COLORS),
                     {'id':3,'type':'text','title':'Привязка событий','gridPos':{'x':0,'y':28,'w':24,'h':9},
                      'options':{'mode':'markdown','content':content}}]}
(ROOT.parent/'dashboards'/'cassandra-tps-correlation.json').write_text(json.dumps(dashboard,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('Created Cassandra/TPS dashboard')

# Keep the service scenario on the same dashboard when rebuilding it.
service_builder = ROOT.parent / 'service-impact' / 'build_dashboard.py'
if service_builder.exists():
    import runpy
    runpy.run_path(str(service_builder), run_name='__main__')
