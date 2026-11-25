"""Build the Kafka write-time replay with Python's standard library."""
import argparse
import bisect
import csv
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
UID="kafka-write-time-replay"
TITLE="Время записи в кафку"
MEASUREMENT="kafka_write_time"
DS={"type":"influxdb","uid":"influxdb-replay"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date",default="2026-09-15")
    args=parser.parse_args()
    start=datetime.fromisoformat(args.date+"T20:00:00+03:00")
    end=start+timedelta(minutes=380)
    knots=list(csv.DictReader((ROOT/"reference-knots.csv").open(encoding="utf-8")))
    seconds=[int(r["elapsed_seconds"]) for r in knots]
    values=[float(r["value_ms"]) for r in knots]
    samples=[]
    with (ROOT/"metrics.csv").open("w",encoding="utf-8",newline="") as table,(ROOT/"metrics.lp").open("w",encoding="utf-8",newline="\n") as lp:
        writer=csv.writer(table);writer.writerow(["timestamp","kafka_write_time_ms"])
        for offset in range(0,380*60+1,15):
            hi=bisect.bisect_left(seconds,offset)
            if hi==0: value=values[0]
            elif hi==len(seconds): value=values[-1]
            else:
                lo=hi-1
                value=values[lo]+(values[hi]-values[lo])*(offset-seconds[lo])/(seconds[hi]-seconds[lo])
            value=round(value,3);t=start+timedelta(seconds=offset)
            samples.append((t,value))
            writer.writerow([t.isoformat(),f"{value:.3f}"])
            lp.write(f"{MEASUREMENT} value={value:.3f} {int(t.timestamp())}\n")
    metadata={"measurement":MEASUREMENT,"field":"value","unit":"ms","unit_confirmed_by_user":True,
              "start":start.isoformat(),"end":end.isoformat(),"timezone":"Europe/Moscow","interval_seconds":15,
              "points":len(samples),"min_ms":min(v for _,v in samples),"max_ms":max(v for _,v in samples),
              "source":"reference.png","calibration":"calibration.json",
              "reconstruction":"Approximate raster digitization, preserving visible excursions and increasing late variance. No added random noise.",
              "dashboard_uid":UID,"dashboard_title":TITLE}
    (ROOT/"metadata.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    query=('import "date"\nfrom(bucket: v.defaultBucket)\n'
           '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
           f'  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "value")\n'
           '  |> keep(columns: ["_time", "_value"])\n  |> group()\n  |> sort(columns: ["_time"])')
    average_query=(query.replace('range(start: v.timeRangeStart,',
                                'range(start: date.sub(d: 15m, from: v.timeRangeStart),')
                   + '\n  |> movingAverage(n: 60)\n'
                     '  |> filter(fn: (r) => r._time >= v.timeRangeStart)')
    note=("**Приблизительное восстановление по скриншоту.** Единица измерения — миллисекунды, подтверждено пользователем. "
          "Дата выбрана для воспроизведения: 15.09.2026 20:00 — 16.09.2026 02:20, Москва. "
          "Исходные числовые данные и частота измерений неизвестны; оцифрованная линия приведена к шагу 15 секунд. "
          "Видимые выбросы сохранены без добавления случайного шума. "
          "Голубая линия — скользящее среднее за 15 минут (60 отсчётов); в начале истории требуется прогрев окна.")
    dashboard={"id":None,"uid":UID,"title":TITLE,"schemaVersion":41,"version":1,"editable":True,
               "timezone":"Europe/Moscow","refresh":"","graphTooltip":1,"fiscalYearStartMonth":0,
               "tags":["atropos","influxdb","replay","kafka"],"description":note,"annotations":{"list":[]},
               "time":{"from":start.astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
                       "to":end.astimezone(timezone.utc).isoformat().replace("+00:00","Z")},
               "timepicker":{"hidden":False},"templating":{"list":[]},
               "links":[{"type":"link","title":"Все графики InfluxDB","url":"/d/influxdb-replay","targetBlank":False}],
               "panels":[{"id":1,"type":"timeseries","title":TITLE,"datasource":DS,"description":note,
                          "gridPos":{"x":0,"y":0,"w":24,"h":20},"interval":"15s","maxDataPoints":10000,
                          "targets":[{"refId":"A","datasource":DS,"query":query},
                                     {"refId":"B","datasource":DS,"query":average_query}],
                          "fieldConfig":{"defaults":{"unit":"ms","min":220,"max":750,"decimals":1,"displayName":TITLE,
                                                    "color":{"mode":"fixed","fixedColor":"#B3A01B"},
                                                    "custom":{"drawStyle":"line","lineInterpolation":"linear","lineWidth":1,
                                                              "fillOpacity":18,"showPoints":"never","spanNulls":False,
                                                              "scaleDistribution":{"type":"linear"},"stacking":{"mode":"none","group":"A"},
                                                              "hideFrom":{"tooltip":False,"viz":False,"legend":False}}},
                                         "overrides":[{"matcher":{"id":"byFrameRefID","options":"B"},
                                                       "properties":[{"id":"displayName","value":"Среднее за 15 минут"},
                                                                     {"id":"color","value":{"mode":"fixed","fixedColor":"#56D9E9"}},
                                                                     {"id":"custom.lineWidth","value":3},
                                                                     {"id":"custom.fillOpacity","value":0}]}]},
                          "options":{"legend":{"showLegend":True,"calcs":["mean"],"displayMode":"list","placement":"bottom"},
                                     "tooltip":{"mode":"multi","sort":"none"}}},
                         {"id":2,"type":"text","title":"Источник и диапазон времени","gridPos":{"x":0,"y":20,"w":24,"h":4},
                          "options":{"mode":"markdown","content":note}}]}
    (ROOT.parent/"dashboards"/"kafka-write-time.json").write_text(json.dumps(dashboard,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    mad_builder=ROOT/'add_mad_panels.py'
    if mad_builder.exists() and (ROOT/'mad-report.json').exists():
        import runpy
        runpy.run_path(str(mad_builder),run_name='__main__')
    print(json.dumps(metadata,ensure_ascii=True,indent=2))


if __name__=="__main__":
    main()
