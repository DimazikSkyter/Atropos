import json
import re

from atropos_ui.service_api import Datasource as Datasource
from atropos_ui.service_api import MetricDefinition as MetricDefinition
from atropos_ui.service_api import validate_url as validate_url


def selector(metric: str, labels: dict[str, str] | None = None) -> str:
    """Escape actual catalog labels as PromQL string literals."""
    pairs = {"__name__": metric, **(labels or {})}
    for key in pairs:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
            raise ValueError("Имя label не поддерживается конструктором; используйте редактор.")
    return "{" + ",".join(f"{key}={json.dumps(value, ensure_ascii=False)}"
                          for key, value in sorted(pairs.items())) + "}"
