"""Generate grocery-cart purchase events and publish them to Kafka."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from confluent_kafka import KafkaError, Producer
from prometheus_client import Counter, start_http_server


LOG = logging.getLogger("purchase-producer")
STOP = threading.Event()

PRODUCED = Counter(
    "atropos_purchases_produced_total",
    "Purchase events delivered to Kafka.",
)
PRODUCER_ERRORS = Counter(
    "atropos_producer_errors_total",
    "Purchase events Kafka failed to deliver.",
)

PRODUCTS: tuple[tuple[str, str, Decimal], ...] = (
    ("0d6f5554-f2f4-4da3-89b7-22bce983cdd5", "Молоко", Decimal("109.90")),
    ("be6b2ab3-3973-4a6a-9ccc-40fa1c48b7ef", "Хлеб", Decimal("64.50")),
    ("e6375a57-2fb5-49c8-b56b-a56e591e475f", "Яйца, 10 шт.", Decimal("139.90")),
    ("c2334e89-8063-4d0a-8d46-727124277091", "Сыр", Decimal("349.00")),
    ("f0da673a-48aa-44fd-9b18-a62f7763ee64", "Яблоки", Decimal("159.90")),
    ("967656bd-4cf8-47a1-ac31-eaa2fdd14e52", "Бананы", Decimal("129.90")),
    ("8cf199d6-943d-4b6e-8edf-902cb934e42f", "Картофель", Decimal("79.90")),
    ("a838a471-7174-446d-a33a-415ce9a5aacf", "Куриное филе", Decimal("489.00")),
    ("27aac860-e172-4dbf-bca0-f28f12b694bf", "Макароны", Decimal("119.90")),
    ("635e58b3-509e-4fe9-ad3a-4934efbf7113", "Рис", Decimal("149.90")),
    ("3bcc9854-b8f9-46ee-9c6c-28dfb2cbf69e", "Йогурт", Decimal("84.90")),
    ("18354ab7-3836-4b40-b581-a5377aa96563", "Кофе", Decimal("549.00")),
    ("7018cc4a-8a5a-479c-97a9-e0de0bf23d30", "Чай", Decimal("219.90")),
    ("577d07ef-24be-4074-847d-4504510058cd", "Сахар", Decimal("94.90")),
    ("59283be4-ad1f-449b-9fd4-a8146266b525", "Шоколад", Decimal("129.90")),
)


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def generate_event() -> dict[str, Any]:
    product_count = random.randint(1, 10)
    selected = random.sample(PRODUCTS, product_count)
    items: list[dict[str, Any]] = []
    total = Decimal("0")

    for product_id, name, unit_price in selected:
        quantity = random.randint(1, 5)
        line_total = unit_price * quantity
        total += line_total
        items.append(
            {
                "product_id": product_id,
                "name": name,
                "quantity": quantity,
                "unit_price": money(unit_price),
                "line_total": money(line_total),
            }
        )

    purchase_id = str(uuid.uuid4())
    return {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "event_type": "purchase.completed",
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purchase": {
            "purchase_id": purchase_id,
            "customer_id": str(uuid.uuid4()),
            "currency": "RUB",
            "items": items,
            "total_amount": money(total),
        },
    }


def delivery_report(error: KafkaError | None, message: Any) -> None:
    if error is not None:
        PRODUCER_ERRORS.inc()
        LOG.error("Kafka delivery failed: %s", error)
        return
    PRODUCED.inc()
    LOG.info(
        "Delivered purchase key=%s to %s[%s] offset=%s",
        message.key().decode("utf-8"),
        message.topic(),
        message.partition(),
        message.offset(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap-servers",
        default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
    )
    parser.add_argument("--topic", default=os.getenv("KAFKA_TOPIC", "cart-purchases"))
    parser.add_argument(
        "--interval",
        type=float,
        default=env_float("PRODUCER_INTERVAL_SECONDS", 1.0),
        help="Seconds between messages.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=env_int("PRODUCER_MESSAGE_COUNT", 0),
        help="Number of messages; 0 means run until interrupted.",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=env_int("PRODUCER_METRICS_PORT", 8001),
        help="Prometheus metrics port; 0 disables the endpoint.",
    )
    return parser.parse_args()


def request_stop(_signum: int, _frame: Any) -> None:
    STOP.set()


def main() -> int:
    args = parse_args()
    if args.interval < 0 or args.count < 0:
        raise SystemExit("--interval and --count must be non-negative")

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    if args.metrics_port:
        start_http_server(args.metrics_port)
        LOG.info("Metrics endpoint: http://localhost:%s/metrics", args.metrics_port)

    producer = Producer(
        {
            "bootstrap.servers": args.bootstrap_servers,
            "client.id": "atropos-purchase-producer",
            "acks": "all",
            "enable.idempotence": True,
        }
    )

    sent = 0
    LOG.info("Producing to Kafka topic %s via %s", args.topic, args.bootstrap_servers)
    while not STOP.is_set() and (args.count == 0 or sent < args.count):
        event = generate_event()
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        key = event["purchase"]["purchase_id"].encode("utf-8")

        while not STOP.is_set():
            try:
                producer.produce(
                    args.topic,
                    key=key,
                    value=payload,
                    on_delivery=delivery_report,
                )
                break
            except BufferError:
                producer.poll(0.5)

        producer.poll(0)
        sent += 1
        STOP.wait(args.interval)

    undelivered = producer.flush(10)
    if undelivered:
        LOG.error("%s message(s) were not delivered before shutdown", undelivered)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
