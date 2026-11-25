"""Explicit offline adapter. It never pretends to execute arbitrary PromQL."""
import math
import re
import time
from dataclasses import asdict

from atropos_ui.clients import METRIC_SEARCH_LIMIT, metric_name_pattern, validate_window


class DemoClient:
    def __init__(self):
        self.published = {}

    def names(self, search):
        pattern = re.compile(metric_name_pattern(search))
        return [name for name in ["up", "process_cpu_seconds_total", "http_requests_total"]
                if pattern.fullmatch(name)][:METRIC_SEARCH_LIMIT + 1]

    def series(self, metric, start, end):
        return [{"__name__": metric, "job": "demo", "instance": instance}
                for instance in ["node-a:8001", "node-b:8001"]]

    def instant(self, query):
        return {"resultType": "vector", "result": [
            {"metric": {"__name__": "demo_value", "instance": "node-a"}, "value": [time.time(), "42"]}]}

    def range(self, query, start, end, step):
        validate_window(start, end, step)
        return {"resultType": "matrix", "result": [
            {"metric": {"__name__": "demo_value", "instance": node},
             "values": [[start + i * step, str(40 + offset + 5 * math.sin(i / 8))]
                        for i in range(int((end - start) / step) + 1)]}
            for node, offset in [("node-a", 0), ("node-b", 12)]]}

    def publish(self, definition, source):
        self.published[definition.name] = asdict(definition)
        return {"name": definition.name, "status": "demo", "message": "Stored in this demo session only"}

    def definitions(self):
        return list(self.published.values())

    def exposition(self):
        names = list(self.published) or ["atropos_demo_value"]
        return "\n".join(f"# HELP {name} Synthetic demo gauge\n# TYPE {name} gauge\n{name}{{instance=\"node-a\"}} 42"
                         for name in names) + "\n"
