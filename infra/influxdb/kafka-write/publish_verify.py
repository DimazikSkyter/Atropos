"""Import the Kafka replay and verify raw data and preservation of existing history."""
import argparse
import base64
import csv
import hashlib
import io
import json
import math
import os
import statistics
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

os.environ.pop("SSLKEYLOGFILE",None)
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parent
METRIC="kafka_write_time"
INFLUX=os.getenv("INFLUX_URL","http://127.0.0.1:8086").rstrip("/")
GRAFANA=os.getenv("GRAFANA_URL","http://127.0.0.1:3001").rstrip("/")
ORG=os.getenv("INFLUXDB_ORG","atropos")
BUCKET=os.getenv("INFLUXDB_BUCKET","metrics-replay")
TOKEN=os.getenv("INFLUXDB_TOKEN","atropos-local-replay-token")
AUTH={"Authorization":"Basic "+base64.b64encode((os.getenv("GRAFANA_ADMIN_USER","admin")+":"+os.getenv("GRAFANA_ADMIN_PASSWORD","admin")).encode()).decode()}


def request(url,data=None,headers=None):
    with urlopen(Request(url,data=data,headers=headers or {}),timeout=60) as response:
        return response.read().decode()


def database():
    flux=f'from(bucket: {json.dumps(BUCKET)}) |> range(start: 0)'
    raw=request(INFLUX+"/api/v2/query?"+urlencode({"org":ORG}),flux.encode(),
                {"Authorization":"Token "+TOKEN,"Content-Type":"application/vnd.flux","Accept":"application/csv"})
    points,header={},None
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith("#"): continue
        if "_time" in fields: header=fields;continue
        if header is None: raise ValueError("Missing Influx CSV header")
        row=dict(zip(header,fields))
        if not row.get("_time"):continue
        tags=tuple(sorted((k,v) for k,v in row.items() if k not in
                          {"","result","table","_start","_stop","_time","_value","_field","_measurement"} and v))
        key=(row["_measurement"],row["_field"],row["_time"],tags)
        assert key not in points,"Duplicate returned identity"
        points[key]=row["_value"]
    return points


def summary(points):
    return {metric:{"field_values":len(part),"sha256":hashlib.sha256(json.dumps(sorted(part),separators=(",",":")).encode()).hexdigest()}
            for metric in sorted({k[0] for k in points})
            for part in [[(k,v) for k,v in points.items() if k[0]==metric]]}


def local_checks():
    meta=json.loads((ROOT/"metadata.json").read_text(encoding="utf-8"))
    samples=[(datetime.fromisoformat(r["timestamp"]),float(r["kafka_write_time_ms"]))
             for r in csv.DictReader((ROOT/"metrics.csv").open(encoding="utf-8"))]
    values=[v for _,v in samples]
    start=samples[0][0]
    middle=[v for t,v in samples if 2*3600<=(t-start).total_seconds()<4*3600]
    late=[v for t,v in samples if (t-start).total_seconds()>=5*3600]
    checks={"points_and_dates_match_metadata":len(samples)==meta["points"] and samples[0][0].isoformat()==meta["start"] and samples[-1][0].isoformat()==meta["end"],
            "uniform_15_second_step":all((b[0]-a[0]).total_seconds()==15 for a,b in zip(samples,samples[1:])),
            "finite_values_in_image_scale":all(math.isfinite(v) and 220<=v<=750 for v in values),
            "central_level_matches_image":470<statistics.fmean(middle)<490,
            "increased_late_variance_preserved":statistics.pstdev(late)>2*statistics.pstdev(middle),
            "visible_large_excursions_preserved":min(values)<280 and max(values)>690,
            "unit_ms_confirmed":meta["unit"]=="ms" and meta["unit_confirmed_by_user"]}
    stats={"points":len(samples),"mean_ms":statistics.fmean(values),"min_ms":min(values),"max_ms":max(values),
           "middle_sigma_ms":statistics.pstdev(middle),"late_sigma_ms":statistics.pstdev(late)}
    return samples,meta,checks,stats


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase",choices=["draft","before","write","after"])
    args=parser.parse_args()
    samples,meta,checks,stats=local_checks()
    assert all(checks.values()),checks
    if args.phase=="draft":
        result={"passed":True,"checks":checks,"statistics":stats}
        (ROOT/"draft-verification.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(result,indent=2));return
    if args.phase=="before":
        current=summary(database())
        assert METRIC not in current,"The metric already exists: inspect before updating"
        (ROOT/"before.json").write_text(json.dumps(current,indent=2)+"\n",encoding="utf-8")
        print(f"Snapshot: {len(current)} existing measurements");return
    before=json.loads((ROOT/"before.json").read_text(encoding="utf-8"))
    assert METRIC not in before,"New metric conflicts with existing history"
    if args.phase=="write":
        current=summary(database())
        assert METRIC not in current,"Metric already imported; verify it before importing again"
        request(INFLUX+"/api/v2/write?"+urlencode({"org":ORG,"bucket":BUCKET,"precision":"s"}),
                (ROOT/"metrics.lp").read_bytes(),{"Authorization":"Token "+TOKEN,"Content-Type":"text/plain; charset=utf-8"})
        print(f"Imported {len(samples)} Kafka write-time samples");return
    current=database();after=summary(current)
    expected={int(t.timestamp()):v for t,v in samples}
    actual={int(datetime.fromisoformat(k[2].replace("Z","+00:00")).timestamp()):float(v)
            for k,v in current.items() if k[0]==METRIC and k[1]=="value"}
    checks["all_existing_measurements_unchanged"]=all(after.get(name)==info for name,info in before.items())
    checks["new_database_points_match_exactly"]=actual==expected and after[METRIC]["field_values"]==len(expected)
    dashboard=json.loads(request(GRAFANA+"/api/dashboards/uid/"+meta["dashboard_uid"],headers=AUTH))["dashboard"]
    panel=next(p for p in dashboard["panels"] if p["id"]==1)
    checks["dashboard_title_matches_request"]=dashboard["title"]=="Время записи в кафку" and panel["title"]==dashboard["title"]
    checks["dashboard_uses_correct_dates_and_timezone"]=dashboard["timezone"]=="Europe/Moscow" and int(datetime.fromisoformat(dashboard["time"]["from"].replace("Z","+00:00")).timestamp())==min(expected) and int(datetime.fromisoformat(dashboard["time"]["to"].replace("Z","+00:00")).timestamp())==max(expected)
    payload={"from":str(min(expected)*1000),"to":str(max(expected)*1000),
             "queries":[{**panel["targets"][0],"intervalMs":15000,"maxDataPoints":10000}]}
    result=json.loads(request(GRAFANA+"/api/ds/query",json.dumps(payload).encode(),{**AUTH,"Content-Type":"application/json"}))["results"]["A"]
    assert not result.get("error"),result.get("error")
    rendered={}
    for frame in result["frames"]:
        fields=frame["schema"]["fields"]
        ti=next(i for i,f in enumerate(fields) if f["type"]=="time")
        vi=next(i for i,f in enumerate(fields) if f["type"]=="number")
        times,values=frame["data"]["values"][ti],frame["data"]["values"][vi]
        rendered.update((int(t/1000),v) for t,v in zip(times,values))
    checks["grafana_returns_all_raw_points"]=rendered==expected and len(result["frames"])==1
    checks["grafana_unit_ms"]=panel["fieldConfig"]["defaults"]["unit"]=="ms"
    overview=json.loads(request(GRAFANA+"/api/dashboards/uid/influxdb-replay",headers=AUTH))["dashboard"]
    checks["overview_contains_kafka_panel"]=any(p.get("type")=="timeseries" and p.get("title")==dashboard["title"] for p in overview["panels"])
    verification={"passed":all(checks.values()),"checks":checks,"statistics":stats,"before":before,"after":after,
                  "dashboard_url":GRAFANA+"/d/"+meta["dashboard_uid"],"metadata":meta}
    (ROOT/"verification.json").write_text(json.dumps(verification,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"passed":verification["passed"],"checks":checks,"statistics":stats},indent=2))
    raise SystemExit(0 if verification["passed"] else 1)


if __name__=="__main__":
    main()
