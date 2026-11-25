"""Consume grocery-cart purchase events from Kafka and persist them to PostgreSQL."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import uuid
from datetime import datetime
from decimal import Decimal
from time import perf_counter
from typing import Any

import psycopg
from confluent_kafka import Consumer, KafkaError, KafkaException, Message
from prometheus_client import Counter, Histogram, start_http_server
from psycopg.types.json import Jsonb


LOG = logging.getLogger("purchase-consumer")
STOP = threading.Event()
MONEY = Decimal("0.01")

PERSISTED = Counter(
    "atropos_purchases_persisted_total",
    "Purchase events persisted to PostgreSQL.",
)
CONSUMER_ERRORS = Counter(
    "atropos_consumer_errors_total",
    "Purchase events that failed validation or persistence.",
    ["kind"],
)
PERSIST_SECONDS = Histogram(
    "atropos_purchase_persist_seconds",
    "Time spent persisting a purchase transaction.",
)


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def as_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{field} must be a UUID") from error


def as_money(value: Any, field: str) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(MONEY)
    except Exception as error:
        raise ValueError(f"{field} must be a monetary number") from error
    if amount < 0:
        raise ValueError(f"{field} must not be negative")
    return amount


def validate_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    if event.get("schema_version") != 1:
        raise ValueError("unsupported schema_version")
    if event.get("event_type") != "purchase.completed":
        raise ValueError("unsupported event_type")

    event_id = as_uuid(event.get("event_id"), "event_id")
    try:
        occurred_at = datetime.fromisoformat(str(event["occurred_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("occurred_at must be an ISO-8601 timestamp") from error
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must include a timezone")

    purchase = event.get("purchase")
    if not isinstance(purchase, dict):
        raise ValueError("purchase must be a JSON object")

    purchase_id = as_uuid(purchase.get("purchase_id"), "purchase.purchase_id")
    customer_id = as_uuid(purchase.get("customer_id"), "purchase.customer_id")
    currency = str(purchase.get("currency", "")).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("purchase.currency must be a three-letter code")

    raw_items = purchase.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 10:
        raise ValueError("purchase.items must contain between 1 and 10 products")

    items: list[dict[str, Any]] = []
    calculated_total = Decimal("0")
    for line_number, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"purchase.items[{line_number - 1}] must be an object")
        product_name = str(raw_item.get("name", "")).strip()
        if not product_name:
            raise ValueError(f"purchase.items[{line_number - 1}].name must not be empty")
        try:
            quantity = int(raw_item["quantity"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"purchase.items[{line_number - 1}].quantity must be an integer") from error
        if quantity <= 0:
            raise ValueError(f"purchase.items[{line_number - 1}].quantity must be positive")

        unit_price = as_money(raw_item.get("unit_price"), "item.unit_price")
        line_total = as_money(raw_item.get("line_total"), "item.line_total")
        if line_total != (unit_price * quantity).quantize(MONEY):
            raise ValueError(f"purchase.items[{line_number - 1}].line_total is inconsistent")
        calculated_total += line_total
        items.append(
            {
                "line_number": line_number,
                "product_id": as_uuid(raw_item.get("product_id"), "item.product_id"),
                "product_name": product_name,
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": line_total,
            }
        )

    total_amount = as_money(purchase.get("total_amount"), "purchase.total_amount")
    if total_amount != calculated_total.quantize(MONEY):
        raise ValueError("purchase.total_amount does not equal the sum of item totals")

    return {
        "event_id": event_id,
        "purchase_id": purchase_id,
        "customer_id": customer_id,
        "purchased_at": occurred_at,
        "currency": currency,
        "total_amount": total_amount,
        "items": items,
    }


def persist_event(connection: psycopg.Connection[Any], event: dict[str, Any]) -> None:
    purchase = validate_event(event)
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO purchases (
                    purchase_id, event_id, customer_id, purchased_at,
                    currency, total_amount, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (purchase_id) DO NOTHING
                """,
                (
                    purchase["purchase_id"],
                    purchase["event_id"],
                    purchase["customer_id"],
                    purchase["purchased_at"],
                    purchase["currency"],
                    purchase["total_amount"],
                    Jsonb(event),
                ),
            )
            if cursor.rowcount == 0:
                LOG.info("Purchase %s is already stored; treating it as processed", purchase["purchase_id"])
                return

            cursor.executemany(
                """
                INSERT INTO purchase_items (
                    purchase_id, line_number, product_id, product_name,
                    quantity, unit_price, line_total
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        purchase["purchase_id"],
                        item["line_number"],
                        item["product_id"],
                        item["product_name"],
                        item["quantity"],
                        item["unit_price"],
                        item["line_total"],
                    )
                    for item in purchase["items"]
                ],
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap-servers",
        default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
    )
    parser.add_argument("--topic", default=os.getenv("KAFKA_TOPIC", "cart-purchases"))
    parser.add_argument(
        "--group-id",
        default=os.getenv("KAFKA_CONSUMER_GROUP_ID", "purchases-postgres-writer"),
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql://atropos:atropos@localhost:5432/atropos",
        ),
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=env_int("CONSUMER_METRICS_PORT", 8002),
        help="Prometheus metrics port; 0 disables the endpoint.",
    )
    return parser.parse_args()


def request_stop(_signum: int, _frame: Any) -> None:
    STOP.set()


def process_message(
    consumer: Consumer,
    connection: psycopg.Connection[Any],
    message: Message,
) -> None:
    try:
        event = json.loads(message.value().decode("utf-8"))
        started = perf_counter()
        persist_event(connection, event)
        PERSIST_SECONDS.observe(perf_counter() - started)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        CONSUMER_ERRORS.labels(kind="invalid_event").inc()
        LOG.error(
            "Discarding invalid event at %s[%s] offset=%s: %s",
            message.topic(),
            message.partition(),
            message.offset(),
            error,
        )
    except psycopg.Error:
        CONSUMER_ERRORS.labels(kind="database").inc()
        LOG.exception("Database write failed; Kafka offset was not committed")
        raise
    else:
        PERSISTED.inc()
    consumer.commit(message=message, asynchronous=False)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    if args.metrics_port:
        start_http_server(args.metrics_port)
        LOG.info("Metrics endpoint: http://localhost:%s/metrics", args.metrics_port)

    consumer = Consumer(
        {
            "bootstrap.servers": args.bootstrap_servers,
            "group.id": args.group_id,
            "client.id": "atropos-purchase-consumer",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )

    try:
        with psycopg.connect(args.database_url, autocommit=True) as connection:
            consumer.subscribe([args.topic])
            LOG.info(
                "Consuming Kafka topic %s via %s and writing to PostgreSQL",
                args.topic,
                args.bootstrap_servers,
            )
            while not STOP.is_set():
                message = consumer.poll(1.0)
                if message is None:
                    continue
                if message.error():
                    if message.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    raise KafkaException(message.error())
                process_message(consumer, connection, message)
    except (psycopg.Error, KafkaException):
        LOG.exception("Consumer stopped because of an infrastructure error")
        return 1
    finally:
        consumer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
