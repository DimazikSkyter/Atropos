import json
import os
from pathlib import Path
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from confluent_kafka import Consumer, TopicPartition
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

ROOT = Path('/faults')
ROOT.mkdir(exist_ok=True)
os.chmod(ROOT, 0o777)
ACTIVE = Gauge('lab_disk_stall_active', 'Requested disk stall, enforced with TTL')
BLOCKED = Gauge('lab_disk_blocked_calls', 'Currently intercepted blocked file calls')
TOTAL = Counter('lab_disk_gate_entries_total', 'Observed entries into actual file syscall gate')
PHASE = Gauge('lab_experiment_phase', '0 idle, 1 baseline, 2 fault, 3 recovery')
OFFERED = Gauge('lab_offered_requests_per_second', 'Configured constant aggregate HTTP arrival rate')
ISR = Gauge('lab_kafka_isr', 'In-sync replica count', ['partition'])
LEADER = Gauge('lab_kafka_leader', 'Broker ID leading partition', ['partition'])
LAG = Gauge('lab_kafka_group_lag', 'High watermark minus committed consumer group offset', ['partition'])
PROBE = Gauge('lab_kafka_probe_ok', 'Successful independent Kafka metadata/offset probe')
phase_until = 0
network_until = 0
network_lock = threading.Lock()
NETWORK = Gauge('lab_network_fault_active', 'Network latency injection on both consumer DB links')


def proxy_request(path, method, data=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request('http://toxiproxy:8474'+path, data=body, method=method,
                                 headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=3) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if method == 'DELETE' and exc.code == 404:
            return b''
        raise RuntimeError(f'Toxiproxy {exc.code}: {exc.read().decode()}') from exc


def clear_network():
    for proxy in ('consumer1-db','consumer2-db'):
        for direction in ('upstream','downstream'):
            proxy_request(f'/proxies/{proxy}/toxics/lab-{direction}', 'DELETE')


def network_watchdog():
    global network_until
    while True:
        try:
            with network_lock:
                if network_until and time.time() >= network_until:
                    clear_network()
                    network_until = 0
                NETWORK.set(bool(network_until))
        except Exception as exc:
            print('network recovery: '+str(exc),flush=True)
        time.sleep(.5)


def monitor():
    position = 0
    outstanding = 0
    while True:
        try:
            ACTIVE.set(float((ROOT/'kafka1-until').read_text()) > time.time())
        except (OSError, ValueError):
            ACTIVE.set(0)
        try:
            with (ROOT/'disk-events.log').open() as f:
                f.seek(position)
                for line in f:
                    if ' blocked ' in line:
                        outstanding += 1
                        TOTAL.inc()
                    elif ' released ' in line:
                        outstanding -= 1
                position = f.tell()
            BLOCKED.set(max(0,outstanding))
        except OSError:
            pass
        if phase_until and time.time() > phase_until:
            PHASE.set(0)
        time.sleep(.2)


def kafka_probe():
    c = Consumer({'bootstrap.servers':'kafka1:9092,kafka2:9092,kafka3:9092',
                  'group.id':'experiment-consumers','enable.auto.commit':False,
                  'socket.timeout.ms':3000})
    while True:
        try:
            metadata = c.list_topics('entities',timeout=3)
            topic = metadata.topics['entities']
            if topic.error:
                raise RuntimeError(topic.error)
            partitions = [TopicPartition('entities', p) for p in topic.partitions]
            committed = {p.partition:p.offset for p in c.committed(partitions,timeout=3)}
            for p, meta in topic.partitions.items():
                ISR.labels(str(p)).set(len(meta.isrs))
                LEADER.labels(str(p)).set(meta.leader)
                _, high = c.get_watermark_offsets(TopicPartition('entities',p),timeout=1)
                LAG.labels(str(p)).set(max(0, high-max(0,committed.get(p,0))))
            PROBE.set(1)
        except Exception as e:
            PROBE.set(0)
            print(str(e),flush=True)
        time.sleep(3)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_GET(self):
        if self.path == '/metrics':
            body = generate_latest()
            self.send_response(200)
            self.send_header('Content-Type',CONTENT_TYPE_LATEST)
        else:
            body = json.dumps({'time':time.time()}).encode()
            self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            self.control()
        except Exception as exc:
            print(str(exc),flush=True)
            self.send_error(502,str(exc))

    def control(self):
        global phase_until, network_until
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
        if self.path == '/disk':
            seconds = max(0,min(600,float(data.get('seconds',0))))
            temp = ROOT/'kafka1-until.tmp'
            temp.write_text(str(time.time()+seconds if seconds else 0))
            os.chmod(temp,0o644)
            temp.replace(ROOT/'kafka1-until')
        elif self.path == '/network':
            seconds = max(0,min(600,float(data.get('seconds',0))))
            with network_lock:
                # Set expiry before mutations: partial injection also has automatic cleanup.
                network_until = time.time()+seconds if seconds else time.time()
                clear_network()
                if seconds:
                    for proxy in ('consumer1-db','consumer2-db'):
                        for direction in ('upstream','downstream'):
                            proxy_request(f'/proxies/{proxy}/toxics','POST',{
                                'name':'lab-'+direction,'type':'latency','stream':direction,'toxicity':1.0,
                                'attributes':{'latency':int(data.get('latency_ms',100)),
                                              'jitter':int(data.get('jitter_ms',30))}})
                else:
                    network_until = 0
        elif self.path == '/phase':
            PHASE.set(int(data['phase']))
            OFFERED.set(float(data.get('rate',0)))
            phase_until = time.time()+min(7200,float(data.get('ttl',600)))
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')


threading.Thread(target=monitor,daemon=True).start()
threading.Thread(target=kafka_probe,daemon=True).start()
threading.Thread(target=network_watchdog,daemon=True).start()
ThreadingHTTPServer(('0.0.0.0',8099),Handler).serve_forever()
