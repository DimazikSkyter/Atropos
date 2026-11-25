import json
import math
from datetime import UTC, datetime

import pandas as pd
import plotly.express as px
import streamlit as st

from atropos_ui.clients import ApiError
from atropos_ui.time_picker import time_window as time_window


def query_frame(data: dict) -> pd.DataFrame:
    """Preserve all label sets; never collapse distinct series to a metric name."""
    try:
        kind, result = data["resultType"], data["result"]
        if kind in {"scalar", "string"}:
            result = [{"metric": {}, "value": result}]
        elif kind not in {"matrix", "vector"}:
            raise ValueError("unsupported resultType")
        rows = []
        for item in result:
            if "histogram" in item or "histograms" in item:
                raise ApiError("Native histograms пока доступны только в JSON; используйте histogram_* в PromQL.")
            labels = item["metric"]
            if not isinstance(labels, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
                raise ValueError("invalid labels")
            label = json.dumps(labels, sort_keys=True, ensure_ascii=False)
            samples = item["values"] if "values" in item else [item["value"]]
            for timestamp, value in samples:
                number = float(value) if kind != "string" else None
                rows.append({"time": pd.to_datetime(float(timestamp), unit="s", utc=True),
                             "series": label, "value": number if number is not None and math.isfinite(number) else None,
                             "raw_value": str(value)})
        return pd.DataFrame(rows, columns=["time", "series", "value", "raw_value"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ApiError("Некорректный формат результата запроса.") from exc


def render_result(data: dict):
    with st.expander("Ответ API (JSON)"):
        st.json(data)
    frame = query_frame(data)
    if frame.empty:
        st.info("Запрос выполнен, данных за выбранный период нет.")
        return
    if data["resultType"] == "matrix":
        st.plotly_chart(px.line(frame, x="time", y="value", color="series",
                                labels={"time": "Время (UTC)", "value": "Значение", "series": "Ряд"}),
                        use_container_width=True)
    st.dataframe(frame, hide_index=True, use_container_width=True)
    if frame["value"].isna().any():
        st.caption("NaN/Inf и строковые значения сохранены в raw_value; на графике это пропуски.")


def timestamps(start: str, end: str):
    def parse(value):
        parsed = datetime.fromisoformat(value)
        return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).timestamp()
    try:
        return parse(start), parse(end)
    except ValueError as exc:
        raise ValueError("Введите даты в формате ISO 8601, например 2026-09-26T10:00:00.") from exc
