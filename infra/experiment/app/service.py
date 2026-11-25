"""Two API producers, two consumer/read services; real Kafka and PostgreSQL I/O."""
import asyncio
import hashlib
import json
import os
import threading
import time
from contextlib import asynccontextmanager

import httpx
from confluent_kafka import Consumer, KafkaException, Producer
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

ROLE = os.environ.get("ROLE", "api")
NODE = os.environ.get("NODE", "unknown")
BROKERS = "kafka1:9092,kafka2:9092,kafka3:9092"
BUCKETS = (.001, .0025, .005, .01, .025, .05, .1, .2, .4, .8, 1.6, 3.2, 6.4, 12.8, 30)
LATENCY = Histogram("lab_duration_seconds", "Actual elapsed durations", ["stage", "result"], buckets=BUCKETS)
SQUARES = Counter("lab_duration_squared_seconds", "Sum of squared duration observations", ["stage", "result"])
EVENTS = Counter("lab_events", "Entity lifecycle counters", ["stage"])
LAG = Gauge("lab_partition_lag", "High watermark minus locally DB-processed offset", ["partition"])
ASSIGNED = Gauge("lab_assigned_partitions", "Assigned partition count")
HEALTH = Gauge("lab_worker_alive", "Consumer processing thread is alive")
INFLIGHT = Gauge("lab_requests_inflight", "Active application HTTP requests, excluding idle keep-alive connections")
stop = threading.Event()
pool = None
producer = None
client = None
worker = None


def observe(stage, started, result="ok"):
    duration = time.perf_counter() - started
    LATENCY.labels(stage, result).observe(duration)
    SQUARES.labels(stage, result).inc(duration * duration)


def work():
    # Deterministic application CPU cost, equal for every request; never adjusted during faults.
    hashlib.pbkdf2_hmac("sha256", b"entity-validation", b"lab-fixed-salt", int(os.environ.get("CPU_ITERATIONS", "12000")))


def read_entity(entity_id):
    work()
    started = time.perf_counter()
    result = "ok"
    try:
        with pool.connection(timeout=5) as conn:
            row = conn.execute("SELECT id, payload FROM entities WHERE id = %s", (entity_id,)).fetchone()
        return None if row is None else {"id": row[0], "payload": row[1]}
    except Exception:
        result = "error"
        raise
    finally:
        observe("db_read", started, result)


def consume():
    consumer = Consumer({"bootstrap.servers": BROKERS, "group.id": "experiment-consumers",
                         "enable.auto.commit": False, "enable.auto.offset.store": False,
                         "auto.offset.reset": "earliest", "max.poll.interval.ms": 300000,
                         "partition.assignment.strategy": "range"})
    processed = {}

    def assigned(c, partitions):
        LAG.clear()
        processed.clear()
        ASSIGNED.set(len(partitions))

    def revoked(c, partitions):
        ASSIGNED.set(0)
        LAG.clear()
        processed.clear()

    consumer.subscribe(["entities"], on_assign=assigned, on_revoke=revoked)
    last_commit = last_lag = time.monotonic()
    try:
        while not stop.is_set():
            HEALTH.set(1)
            msg = consumer.poll(.1)
            if msg is not None and not msg.error():
                event = json.loads(msg.value())
                while not stop.is_set():
                    started = time.perf_counter()
                    try:
                        work()
                        db_started = time.perf_counter()
                        try:
                            with pool.connection(timeout=5) as conn:
                                row = conn.execute(
                                    "INSERT INTO entities(id,payload,created_at) VALUES (%s,%s,to_timestamp(%s)) "
                                    "ON CONFLICT (id) DO NOTHING RETURNING id",
                                    (event["id"], event["payload"], event["created_at"])).fetchone()
                            observe("db_write", db_started)
                        except Exception:
                            observe("db_write", db_started, "error")
                            raise
                        consumer.store_offsets(message=msg)
                        processed[msg.partition()] = msg.offset() + 1
                        EVENTS.labels("persisted" if row else "duplicate").inc()
                        if row:
                            elapsed = max(0, time.time() - event["created_at"])
                            LATENCY.labels("end_to_end_write", "ok").observe(elapsed)
                            SQUARES.labels("end_to_end_write", "ok").inc(elapsed * elapsed)
                        observe("consume", started)
                        break
                    except Exception as exc:
                        EVENTS.labels("db_retry").inc()
                        observe("consume", started, "error")
                        print(json.dumps({"node": NODE, "error": str(exc), "event": event["id"]}), flush=True)
                        stop.wait(.25)
            elif msg is not None and msg.error():
                EVENTS.labels("kafka_consume_error").inc()
            now = time.monotonic()
            if now - last_commit > 1 and processed:
                consumer.commit(asynchronous=True)
                last_commit = now
            if now - last_lag > 2:
                for tp in consumer.assignment():
                    try:
                        _, high = consumer.get_watermark_offsets(tp, timeout=.3, cached=False)
                        position = processed.get(tp.partition)
                        if position is None:
                            position = consumer.committed([tp], timeout=.3)[0].offset
                        LAG.labels(str(tp.partition)).set(max(0, high - max(0, position)))
                    except KafkaException:
                        EVENTS.labels("lag_probe_error").inc()
                last_lag = now
    finally:
        HEALTH.set(0)
        consumer.close()


@asynccontextmanager
async def lifespan(app):
    global pool, producer, client, worker
    stop.clear()
    if ROLE == "consumer":
        pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=2, max_size=24,
                              kwargs={"connect_timeout": 5, "options": "-c statement_timeout=10000"}, open=True)
        pool.wait(timeout=60)
        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
    else:
        producer = Producer({"bootstrap.servers": BROKERS, "acks": "all", "enable.idempotence": True,
                             "linger.ms": 5, "delivery.timeout.ms": 10000, "request.timeout.ms": 5000})
        def poll():
            while not stop.is_set():
                producer.poll(.02)
        worker = threading.Thread(target=poll, daemon=True)
        worker.start()
        client = httpx.AsyncClient(timeout=8, limits=httpx.Limits(max_connections=128, max_keepalive_connections=64))
    yield
    stop.set()
    worker.join(timeout=12)
    if producer:
        producer.flush(5)
    if client:
        await client.aclose()
    if pool:
        pool.close()


app = FastAPI(lifespan=lifespan)


class Entity(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    payload: str = Field(max_length=16384)


class RequestMetrics:
    """Pure ASGI: no per-request background task/stream machinery on disconnect."""
    def __init__(self, app):
        self.app = app
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] in ('/metrics', '/health'):
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        stage = ('api_' if ROLE == 'api' else 'callback_') + scope['method'].lower()
        if self.active >= 256:
            await send({'type':'http.response.start','status':503,
                        'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':b'{"detail":"request concurrency limit"}'})
            observe(stage,started,'error')
            return
        status = 500
        self.active += 1
        INFLIGHT.inc()

        async def measured_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            self.active -= 1
            INFLIGHT.dec()
            observe(stage, started, 'ok' if status < 400 else 'error')


app.add_middleware(RequestMetrics)


@app.get("/health")
def health():
    if not worker or not worker.is_alive():
        raise HTTPException(503, "worker unavailable")
    return {"node": NODE, "role": ROLE}


@app.get("/metrics")
def prometheus():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/entities", status_code=202)
async def create(entity: Entity):
    if ROLE != "api":
        raise HTTPException(404)
    EVENTS.labels("create_received").inc()
    await asyncio.to_thread(work)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    started = time.perf_counter()
    event = {"id": entity.id, "payload": entity.payload, "created_at": time.time()}

    def delivered(error, msg):
        def finish():
            if not future.done():
                if error:
                    future.set_exception(RuntimeError(str(error)))
                else:
                    future.set_result(None)
        loop.call_soon_threadsafe(finish)

    try:
        # Explicit stable partitioning avoids key skew between six partitions.
        partition = int.from_bytes(hashlib.sha256(entity.id.encode()).digest()[:4], "big") % 6
        producer.produce("entities", partition=partition, key=entity.id, value=json.dumps(event), on_delivery=delivered)
        await asyncio.wait_for(future, timeout=12)
        observe("kafka_produce", started)
        EVENTS.labels("accepted").inc()
        return {"id": entity.id, "status": "accepted"}
    except Exception as exc:
        observe("kafka_produce", started, "error")
        raise HTTPException(503, str(exc)) from exc


@app.get("/entities/{entity_id}")
async def get_entity(entity_id: str):
    try:
        if ROLE == "consumer":
            entity = await asyncio.to_thread(read_entity, entity_id)
            if entity is None:
                raise HTTPException(404, "not persisted yet")
            return entity
        await asyncio.to_thread(work)
        callback = "consumer1" if hashlib.sha256(entity_id.encode()).digest()[0] % 2 == 0 else "consumer2"
        started = time.perf_counter()
        try:
            response = await client.get(f"http://{callback}:8000/entities/{entity_id}")
            observe("consumer_callback", started, "ok" if response.status_code == 200 else "error")
        except Exception:
            observe("consumer_callback", started, "error")
            raise
        return Response(response.content, status_code=response.status_code, media_type="application/json")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
