import json
from pathlib import Path
from metrics import QUERIES

PANELS=[('mean','Обработка сообщения: среднее','s'),('hit_ratio','Cache hit ratio','percentunit'),
        ('p95','Обработка: p95','s'),('stddev','Обработка: стандартное отклонение','s'),
        ('produced','Входящий поток по партициям','ops'),('consumed','Обработано по партициям','ops'),
        ('outcome_mean','Стоимость hit и miss','s'),('db','Запрос в PostgreSQL при miss','s'),
        ('end_to_end','От приёма в API до результата consumer','s'),('lag','Kafka lag (committed offsets)','short'),
        ('cpu','CPU JVM (доля доступного CPU)','percentunit'),('api','HTTP: только Kafka ACK','s'),
        ('probe','Доступность проверки Kafka lag','short'),('assignments','Партиций на consumer','short')]

def build():
    panels=[]
    for i,(key,title,unit) in enumerate(PANELS):
        legend='{{node}}' if key in ('cpu','api','assignments') else 'partition {{partition}}'
        if key=='outcome_mean': legend+=' / {{outcome}}'
        panels.append({'id':i+1,'type':'timeseries','title':title,'gridPos':{'x':(i%2)*12,'y':(i//2)*8,'w':12,'h':8},
            'datasource':{'type':'prometheus','uid':'victoria-metrics'},
            'targets':[{'refId':'A','expr':QUERIES[key],'legendFormat':legend}],
            'fieldConfig':{'defaults':{'unit':unit,'min':0,'custom':{'drawStyle':'line','lineWidth':2,'spanNulls':False}},'overrides':[]},
            'options':{'tooltip':{'mode':'multi'},'legend':{'displayMode':'table','placement':'bottom','calcs':['mean','max']}}})
    return {'uid':'experiment2-cache','title':'Experiment 2 — client sharding and cache locality','schemaVersion':39,'version':1,
            'tags':['experiment2'],'timezone':'browser','refresh':'5s','time':{'from':'now-15m','to':'now'},'panels':panels}

if __name__=='__main__':
    (Path(__file__).parent/'dashboard.json').write_text(json.dumps(build(),ensure_ascii=False,indent=2),encoding='utf-8')
