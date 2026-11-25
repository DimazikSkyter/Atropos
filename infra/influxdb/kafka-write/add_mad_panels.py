"""Add the supplied MADDetector's offline results to the Kafka dashboard."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PATH=ROOT.parent/'dashboards/kafka-write-time.json'
DS={'type':'influxdb','uid':'influxdb-replay'}


def query(field,scenario='offline'):
    smoothing=('  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false, timeSrc: "_start")\n'
               if field=='score' else '')
    metric='kafka_write_mad_test' if scenario=='test_legacy' else 'kafka_write_mad'
    selector=('  |> filter(fn: (r) => r.mode == "test_legacy" and r.window == "20" and r.reference == "100")\n'
              if scenario=='test_legacy' else '  |> filter(fn: (r) => r.mode == "offline" and r.window == "10")\n')
    return ('import "date"\nfrom(bucket: v.defaultBucket)\n'
            '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
            f'  |> filter(fn: (r) => r._measurement == "{metric}" and r._field == "{field}")\n'+selector+smoothing+
            '  |> keep(columns: ["_time", "_value"])\n  |> group()\n  |> sort(columns: ["_time"])')


def panel(pid,title,y,fields,unit,maximum,scenario='offline'):
    targets=[{'refId':chr(65+i),'datasource':DS,'query':query(field,scenario)} for i,field in enumerate(fields)]
    defaults={'unit':unit,'min':0,'decimals':3 if unit=='none' else 1,
              'custom':{'drawStyle':'line','lineInterpolation':'linear','lineWidth':2,'fillOpacity':4,'showPoints':'never',
                        'spanNulls':False,'scaleDistribution':{'type':'linear'},'stacking':{'mode':'none','group':'A'},
                        'hideFrom':{'tooltip':False,'viz':False,'legend':False}}}
    if maximum is not None:defaults['max']=maximum
    labels={'score':('MADDetector · среднее за 1 минуту','#B877D9'),
            'local_mad_ms':('Локальный MAD · 10 точек','#FF9830'),
            'reference_mad_ms':('Скользящий референс · фактически 99 точек','#56D9E9'),
            'global_mad_ms':('Общий MAD · вся история','#56D9E9')}
    overrides=[]
    for target,field in zip(targets,fields):
        label,color=labels[field]
        if scenario=='test_legacy' and field=='local_mad_ms':label='Локальный MAD · 20 точек'
        props=[{'id':'displayName','value':label},{'id':'color','value':{'mode':'fixed','fixedColor':color}}]
        if field in ('global_mad_ms','reference_mad_ms'):props += [{'id':'custom.lineStyle','value':{'fill':'dash','dash':[6,4]}},{'id':'custom.fillOpacity','value':0}]
        overrides.append({'matcher':{'id':'byFrameRefID','options':target['refId']},'properties':props})
    return {'id':pid,'type':'timeseries','title':title,'datasource':DS,'interval':'1m' if fields==['score'] else '15s','maxDataPoints':10000,
            'gridPos':{'x':0,'y':y,'w':24,'h':7},'targets':targets,
            'description':'Исходный MADDetector без изменения формул. Окно 10 точек (2,5 минуты). Однократный офлайн-прогон по всем 1521 точкам; общий MAD вычислен по всей истории. Это среднее абсолютное отклонение от среднего, не медианное. Порог классификации не задавался.'+(' Оценка отображается как среднее за 1 минуту (4 отсчёта по 15 с) с помощью Flux aggregateWindow. Последнее окно неполное. Исходные оценки в БД не изменены.' if fields==['score'] else ''),
            'fieldConfig':{'defaults':defaults,'overrides':overrides},
            'options':{'legend':{'showLegend':True,'displayMode':'list','placement':'bottom','calcs':['max']},
                       'tooltip':{'mode':'multi','sort':'none'}}}


def build():
    dashboard=json.loads(PATH.read_text(encoding='utf-8'))
    original={p['id']:p for p in dashboard['panels']}
    raw=original[1];raw['gridPos'].update(y=0,h=14)
    note=original[2];note['gridPos'].update(y=28,h=8)
    marker='\n\n**Результат MADDetector.**'
    note['options']['content']=note['options']['content'].split(marker)[0]+marker+(
        ' Окно по умолчанию: 10 точек, 2,5 минуты. Обработан исходный ряд Kafka, не линия скользящего среднего. '
        'Это офлайн-прогон по всей истории: общий MAD = 22,708 мс; он включает и будущие для ранних точек значения. '
        'Локальный MAD — среднее абсолютное отклонение от локального среднего. '
        'Оценка показывает модуль относительного отличия локального MAD от общего после сигмоидальной нормировки; реагирует и на меньший, и на больший разброс. '
        'Первые 9 оценок равны нулю по логике исходного кода. Порог объявления аномалии в коде отсутствует и здесь не добавлялся. '
        'Максимум исходной оценки до сглаживания: 0,95349 в 02:15:15 16.09.2026 (Москва); локальный MAD в этот момент 107,585 мс. '
        'График оценки сглажен минутным средним средствами InfluxDB: 381 точка вместо 1521, по 4 отсчёта в полном окне. Последнее окно содержит один отсчёт. '
        'Входной ряд восстановлен по скриншоту, поэтому результат характеризует реконструкцию.')
    dashboard['panels']=[raw,panel(3,'MADDetector · оценка изменения разброса',14,['score'],'none',1),
                         panel(4,'MAD · локальное и общее отклонение',21,['local_mad_ms','global_mad_ms'],'ms',None),note]
    if (ROOT/'mad-test-report.json').exists():
        raw['gridPos']['h']=12
        score=dashboard['panels'][1];score['gridPos'].update(y=12,h=8)
        score['targets'].append({'refId':'B','datasource':DS,'query':query('score','test_legacy')})
        score['fieldConfig']['overrides'][0]['properties'][0]['value']='Вся история · окно 10 · среднее 1м'
        score['fieldConfig']['overrides'].append({'matcher':{'id':'byFrameRefID','options':'B'},'properties':[
            {'id':'displayName','value':'Как в тесте · 20/100 · среднее 1м'},
            {'id':'color','value':{'mode':'fixed','fixedColor':'#73BF69'}}]})
        score['description']='Сравнение на одних данных Kafka: фиолетовая линия — прежний офлайн-прогон (локальное окно 10, общий MAD всей истории); зелёная — последовательность вызовов из теста (локальное окно 20, скользящий референс 100, фактически 99 из-за границы среза). Обе линии отображаются минутными средними.'
        old=dashboard['panels'][2];old['gridPos'].update(x=0,y=20,w=12,h=8)
        old['title']='MAD · прежний офлайн-прогон'
        test=panel(5,'MAD · скользящий референс как в тесте',20,['local_mad_ms','reference_mad_ms'],'ms',None,'test_legacy')
        test['gridPos'].update(x=12,w=12,h=8)
        test['description']='Точная последовательность вызовов исходного теста: window=20; sub_range_metrics(point, point+100). В текущем Metrics правая граница среза исключается дважды: референс содержит 99 точек, последний отсчёт Kafka не обработан. Формулы и исходные классы не изменены.'
        note['options']['content']+='\n\n**Сравнение с тестом.** В исходном тесте window=20, а общий MAD пересчитывается в каждом скользящем фрагменте из 100 точек. Из-за границы среза фактически используются 99 точек и получается 1520 оценок вместо 1521. На графике это зелёная линия; максимум исходной оценки 0,54699 в 00:54:15. За последние 20 минут средний скор 0,0683 вместо 0,7262 в прежнем офлайн-прогоне: скользящий референс адаптируется к выросшему разбросу. Тестовая картинка использует синтетические гауссовские данные, поэтому совпадение формы с Kafka не ожидается. В репозитории исправления не выполнялись.'
        dashboard['panels']=[raw,score,old,test,note]
    dashboard['tags']=sorted(set(dashboard.get('tags',[]))|{'mad','anomaly-detection'})
    PATH.write_text(json.dumps(dashboard,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Added MAD score and local/global deviation panels to the existing Kafka dashboard')


if __name__=='__main__':build()
