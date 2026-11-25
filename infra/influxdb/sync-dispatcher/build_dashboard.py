"""Provision aligned client response-time and dispatcher-delay panels."""
from pathlib import Path
from datetime import datetime,timezone
import json

ROOT=Path(__file__).resolve().parent
meta=json.loads((ROOT/'metadata.json').read_text(encoding='utf-8'))
DS={'type':'influxdb','uid':'influxdb-replay'}
CLIENT='sync_service_client_response_time';DISPATCHER='service_dispatcher_delay'
start=min(item['start'] for item in meta['ranges'].values());end=max(item['end'] for item in meta['ranges'].values())
def utc(t):return datetime.fromisoformat(t).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
def epoch_ms(t):return int(datetime.fromisoformat(t.replace('Z','+00:00')).timestamp()*1000)
def query(metric):
    return ('import "date"\nfrom(bucket: v.defaultBucket)\n'
            '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
            f'  |> filter(fn: (r) => r._measurement == "{metric}" and r._field == "value")\n'
            '  |> keep(columns: ["_time", "_value", "host"])\n'
            '  |> group(columns: ["host"])\n  |> sort(columns: ["_time"])')
def panel(pid,metric,unit,maximum,y,height,legend):
    return {'id':pid,'type':'timeseries','title':meta['measurement_labels'][metric],'datasource':DS,
            'description':'Реконструкция скриншота, сырые точки через 5 с, без агрегации. '+
                          ('Только основные линии 6–24 мс. За пределами 18:15–18:25 точки отсутствуют.' if metric==CLIENT
                           else 'Пять различимых цветных линий. До роста, с 18:15 до 18:19:15, фон 10–20 мс по запросу пользователя; далее оцифрованные значения.'),
            'gridPos':{'x':0,'y':y,'w':24,'h':height},'interval':'5s','maxDataPoints':10000,
            'targets':[{'refId':'A','datasource':DS,'query':query(metric)}],
            'fieldConfig':{'defaults':{'unit':unit,'min':0,'max':maximum,'decimals':2,
                                      'displayName':legend+' · ${__field.labels.host}',
                                      'custom':{'drawStyle':'line','lineInterpolation':'linear','lineWidth':1.5,'fillOpacity':0,
                                                'showPoints':'never','spanNulls':False,'axisPlacement':'left',
                                                'scaleDistribution':{'type':'linear'},'stacking':{'mode':'none','group':'A'},
                                                'hideFrom':{'tooltip':False,'viz':False,'legend':False}}},
                           'overrides':[{'matcher':{'id':'byRegexp','options':'.*'+host+'$'},
                                         'properties':[{'id':'color','value':{'mode':'fixed','fixedColor':color}}]}
                                        for host,color in meta['host_colors'].items()]},
            'options':{'legend':{'showLegend':True,'displayMode':'list','placement':'bottom','calcs':[]},
                       'tooltip':{'mode':'multi','sort':'none','hideZeros':False}}}
day=meta['date']
focus_start=utc(day+'T18:18:30+03:00');focus_end=utc(day+'T18:25:30+03:00')
content=('**05.10.2026, Москва.** Скачок уровня ВО клиента около **18:19:15** и начало роста диспетчерной задержки '
         'согласованы по времени. Верхний график: **миллисекунды**; нижний: **секунды**.\n\n'
         'С **18:15 до 18:19:15** нижнему графику задан фон **10–20 мс (0,010–0,020 с)**; шаг точек обоих графиков — 5 с.\n\n'
         'Верхний график содержит только основные линии 6–24 мс по указанию пользователя. '
         'Его исходник охватывает 18:15–18:25; значения после 18:25 не продолжены. '
         'Нижний исходник охватывает 18:10–18:45, включая повторные пики после 18:35. '
         'Хосты обезличены как host1–host8 в порядке видимой легенды; для нижнего графика восстановлены '
         'пять различимых цветных трасс. Числа приблизительно оцифрованы, без добавленного случайного шума. '
         'Ссылка «Основной эпизод» увеличивает совместный участок.')
dashboard={'id':None,'uid':'sync-client-dispatcher-replay','title':'ВО клиента / диспетчер сервиса — 05.10.2026',
           'schemaVersion':41,'version':1,'editable':True,'timezone':'Europe/Moscow','refresh':'','graphTooltip':1,
           'fiscalYearStartMonth':0,'tags':['atropos','influxdb','replay','client','dispatcher','latency'],
           'description':'Два восстановленных графика с согласованным началом скачка ВО и роста диспетчерной задержки.',
           'time':{'from':utc(start),'to':utc(end)},'timepicker':{'hidden':False},'templating':{'list':[]},
           'annotations':{'list':[]},
           'links':[{'type':'link','title':'Основной эпизод: 18:18:30–18:25:30',
                     'url':f'/d/sync-client-dispatcher-replay?from={epoch_ms(focus_start)}&to={epoch_ms(focus_end)}&timezone=Europe%2FMoscow','targetBlank':False},
                    {'type':'link','title':'Все данные: 18:10–18:45',
                     'url':f'/d/sync-client-dispatcher-replay?from={epoch_ms(start)}&to={epoch_ms(end)}&timezone=Europe%2FMoscow','targetBlank':False}],
           'panels':[panel(1,CLIENT,'ms',27.5,0,15,'ВО клиента → синхронный сервис'),
                     panel(2,DISPATCHER,'s',10,15,15,'Диспетчер сервиса клиента'),
                     {'id':3,'type':'text','title':'Время, единицы и исходники','gridPos':{'x':0,'y':30,'w':24,'h':6},
                      'options':{'mode':'markdown','content':content}}]}
(ROOT.parent/'dashboards'/'sync-client-dispatcher.json').write_text(json.dumps(dashboard,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('Created sync client / dispatcher dashboard')
