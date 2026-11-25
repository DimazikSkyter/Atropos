"""Metric results and read-only VictoriaMetrics sample inspection.

The VictoriaMetrics client uses the Python 3.12 standard library. DataframeResult
imports pandas lazily. All public timestamps are Unix seconds.
"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
import json
import math
from numbers import Integral, Real
import re
import time
from typing import TYPE_CHECKING, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import warnings

if TYPE_CHECKING:
    import pandas as pd


ALL_SERIES = '{__name__!=""}'


class Metric(ABC):
    name: str


class MetricResult(Metric):
    """A metric with list values and integer Unix timestamps in seconds.

    The constructor retains the attached Metrics.py call convention:
    MetricResult(json, name, tags, values=None, timestamps=None).
    JSON input is a Prometheus matrix row: metric labels and [timestamp, value]
    pairs. Containers are copied so original and anomalous series stay independent.
    """

    values: list[float | int]
    timestamps: list[int]
    tags: dict[str, str]

    def __init__(self, json=None, name: str = '', tags: Mapping[str, str] | None = None,
                 values: Iterable[float | int] | None = None,
                 timestamps: Iterable[int] | None = None):
        if json is not None:
            labels = json['metric']
            name = name or labels.get('__name__', '')
            tags = {k: v for k, v in labels.items() if k != '__name__'}
            pairs = list(json['values'])
            values = [float(pair[1]) for pair in pairs]
            raw_stamps = [float(pair[0]) for pair in pairs]
            if any(not math.isfinite(t) or not t.is_integer() for t in raw_stamps):
                raise ValueError('timestamps must be integer Unix seconds')
            timestamps = [int(t) for t in raw_stamps]
        if not isinstance(name, str) or not name:
            raise ValueError('name must be a nonempty string')
        self.name = name
        self.tags = {} if tags is None else dict(tags)
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.tags.items()):
            raise ValueError('tags must contain string keys and values')
        self.values = [] if values is None else list(values)
        self.timestamps = [] if timestamps is None else list(timestamps)
        if len(self.values) != len(self.timestamps):
            raise ValueError('values and timestamps must have equal length')
        if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v)
               for v in self.values):
            raise ValueError('values must be finite numbers')
        self.values = [int(v) if isinstance(v, Integral) else float(v) for v in self.values]
        if any(isinstance(t, bool) or not isinstance(t, Integral) for t in self.timestamps):
            raise ValueError('timestamps must be integer Unix seconds')
        self.timestamps = [int(t) for t in self.timestamps]
        if any(a >= b for a, b in zip(self.timestamps, self.timestamps[1:])):
            raise ValueError('timestamps must be strictly increasing')

    def __str__(self):
        suffix = '...' if len(self.values) > 3 else ''
        return (f'Metric(name={self.name}, tags={self.tags}, '
                f'values={self.values[:3]}{suffix}, timestamps={self.timestamps[:3]}{suffix})')

    def __repr__(self):
        return (f'MetricResult(name={self.name!r}, tags={self.tags!r}, '
                f'values={self.values!r}, timestamps={self.timestamps!r})')


class DataframeResult(Metric):
    """Window features: one DataFrame row and one start timestamp per window.

    window is the effective window length in points. Column format is chosen by
    the producer; the ACF/PACF analyzer uses a (function, lag) MultiIndex.
    Pandas is imported only when creating this result, keeping the VM client
    usable without scientific dependencies.
    """

    values: pd.DataFrame
    timestamps: list[int]
    tags: dict[str, str]
    window: int

    def __init__(self, name: str, tags: Mapping[str, str] | None,
                 values: pd.DataFrame, timestamps: Iterable[int], window: int):
        import pandas as pd

        if not isinstance(values, pd.DataFrame):
            raise TypeError('values must be a pandas DataFrame')
        if isinstance(window, bool) or not isinstance(window, Integral) or window < 1:
            raise ValueError('window must be a positive integer number of points')
        stamps = list(timestamps)
        # Share metadata and timestamp validation with the original series contract.
        metadata = MetricResult(name=name, tags=tags, values=[0] * len(stamps),
                                timestamps=stamps)
        if len(values) != len(stamps):
            raise ValueError('DataFrame rows and timestamps must have equal length')
        self.name, self.tags = metadata.name, metadata.tags
        self.timestamps = metadata.timestamps
        self.window = int(window)
        self.values = values.copy(deep=True)
        self.values.index = pd.Index(self.timestamps, name='timestamp')

    def __repr__(self):
        return (f'DataframeResult(name={self.name!r}, window={self.window}, '
                f'rows={len(self.values)}, columns={len(self.values.columns)})')


class VictoriaMetricsError(RuntimeError):
    """Transport, API or response-format error; never converted into empty data."""


@dataclass(frozen=True, slots=True)
class MetricSeries:
    name: str
    tags: tuple[tuple[str, str], ...]
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    @classmethod
    def from_labels(cls, labels: Mapping[str, str]):
        if not isinstance(labels, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
        ) or not labels.get('__name__'):
            raise VictoriaMetricsError('Expected string labels including __name__')
        return cls(labels['__name__'], tuple(sorted(
            (k, v) for k, v in labels.items() if k != '__name__')))

    @property
    def labels(self) -> dict[str, str]:
        return {'__name__': self.name, **dict(self.tags)}

    def selector(self) -> str:
        """An exact series selector, ready for a future PromQL builder."""
        return '{' + ','.join(f'{k}={json.dumps(v, ensure_ascii=False)}'
                              for k, v in self.labels.items()) + '}'


@dataclass(frozen=True, slots=True)
class MetricCatalog:
    series: tuple[MetricSeries, ...]
    start: float
    end: float

    @property
    def names(self) -> list[str]:
        return sorted({s.name for s in self.series})

    @property
    def label_names(self) -> list[str]:
        return sorted({k for s in self.series for k in s.labels})

    def label_values(self, label: str) -> list[str]:
        return sorted({s.labels[label] for s in self.series if label in s.labels})

    def filter(self, *, prefix: str = '', name_regex: str | None = None,
               names: Iterable[str] | None = None, exclude_names: Iterable[str] = (),
               labels: Mapping[str, str] | None = None,
               label_regex: Mapping[str, str] | None = None):
        """Local AND filters. Regex uses fullmatch; missing labels never match.

        Returns a new catalog without changing the original. name_regex='.*cpu.*'
        selects names containing cpu. labels may include __name__.
        """
        allowed = None if names is None else set(names)
        excluded = set(exclude_names)
        pattern = re.compile(name_regex) if name_regex is not None else None
        regexes = {k: re.compile(v) for k, v in (label_regex or {}).items()}
        selected = []
        for series in self.series:
            tags = series.labels
            if not series.name.startswith(prefix) or series.name in excluded:
                continue
            if allowed is not None and series.name not in allowed:
                continue
            if pattern is not None and pattern.fullmatch(series.name) is None:
                continue
            if any(tags.get(k) != v for k, v in (labels or {}).items()):
                continue
            if any(k not in tags or p.fullmatch(tags[k]) is None for k, p in regexes.items()):
                continue
            selected.append(series)
        return MetricCatalog(tuple(selected), self.start, self.end)


@dataclass(frozen=True, slots=True)
class SeriesActivity:
    series: MetricSeries
    finite_samples: int = 0
    nonzero_samples: int = 0
    invalid_samples: int = 0
    last_sample_at: float | None = None

    @property
    def has_data(self) -> bool:
        return self.finite_samples > 0

    @property
    def has_nonzero(self) -> bool:
        return self.nonzero_samples > 0

    @property
    def status(self) -> str:
        if self.has_nonzero:
            return 'nonzero'
        if self.has_data:
            return 'zero_only'
        if self.invalid_samples:
            return 'invalid_only'
        return 'no_data'


class VictoriaMetricsClient:
    def __init__(self, base_url: str = 'http://localhost:8428', *, timeout: float = 30,
                 headers: Mapping[str, str] | None = None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError('base_url must be an HTTP(S) URL without query or fragment')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.headers = dict(headers or {})

    @staticmethod
    def _window(lookback_seconds: float, end: float | None):
        end = time.time() if end is None else end
        if not math.isfinite(lookback_seconds) or lookback_seconds <= 0 or not math.isfinite(end):
            raise ValueError('lookback must be positive and timestamps finite')
        return end - lookback_seconds, end

    @staticmethod
    def _selectors(selectors):
        if isinstance(selectors, str):
            selectors = [selectors]
        else:
            selectors = list(selectors)
        if not selectors or any(not isinstance(s, str) or not s.strip() for s in selectors):
            raise ValueError('At least one nonempty series selector is required')
        return [('match[]', s) for s in selectors]

    def _open(self, path, params):
        # POST form parameters are supported by these read endpoints and avoid URL limits.
        request = Request(self.base_url + path, data=urlencode(params).encode(),
                          headers={**self.headers, 'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            return urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            exc.close()
            raise VictoriaMetricsError(f'{path}: HTTP {exc.code}') from exc
        except (URLError, OSError) as exc:
            raise VictoriaMetricsError(f'{path}: connection failed ({type(exc).__name__})') from exc

    def _json(self, path, params):
        try:
            with self._open(path, params) as response:
                payload = json.load(response)
        except (ValueError, OSError) as exc:
            raise VictoriaMetricsError(f'{path}: unreadable JSON response') from exc
        if not isinstance(payload, dict) or payload.get('status') != 'success' or 'data' not in payload:
            raise VictoriaMetricsError(f'{path}: unsuccessful or malformed API response')
        for warning in payload.get('warnings', []):
            warnings.warn(str(warning), RuntimeWarning, stacklevel=2)
        return payload['data']

    def _metadata(self, path, selectors, lookback_seconds, end):
        start, end = self._window(lookback_seconds, end)
        params = self._selectors(selectors) + [('start', start), ('end', end)]
        return self._json(path, params), start, end

    def list_metric_names(self, selectors=ALL_SERIES, *, lookback_seconds=3600, end=None) -> list[str]:
        return self.list_label_values('__name__', selectors, lookback_seconds=lookback_seconds, end=end)

    def list_label_names(self, selectors=ALL_SERIES, *, lookback_seconds=3600, end=None) -> list[str]:
        data, _, _ = self._metadata('/api/v1/labels', selectors, lookback_seconds, end)
        return self._strings(data)

    def list_label_values(self, label: str, selectors=ALL_SERIES, *, lookback_seconds=3600,
                          end=None) -> list[str]:
        if not isinstance(label, str) or not label:
            raise ValueError('label must be nonempty')
        data, _, _ = self._metadata(f'/api/v1/label/{quote(label, safe="")}/values',
                                    selectors, lookback_seconds, end)
        return self._strings(data)

    @staticmethod
    def _strings(data):
        if not isinstance(data, list) or any(not isinstance(v, str) for v in data):
            raise VictoriaMetricsError('Expected a list of strings')
        return sorted(set(data))

    def catalog(self, selectors=ALL_SERIES, *, lookback_seconds=3600, end=None) -> MetricCatalog:
        data, start, end = self._metadata('/api/v1/series', selectors, lookback_seconds, end)
        if not isinstance(data, list):
            raise VictoriaMetricsError('Expected a list of series')
        series = {MetricSeries.from_labels(row) for row in data}
        return MetricCatalog(tuple(sorted(series, key=lambda s: (s.name, s.tags))), start, end)

    def list_series(self, selectors=ALL_SERIES, *, lookback_seconds=3600, end=None) -> list[MetricSeries]:
        return list(self.catalog(selectors, lookback_seconds=lookback_seconds, end=end).series)

    def _export(self, params):
        try:
            with self._open('/api/v1/export', params) as response:
                for line in response:
                    if line.strip():
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError('Expected JSON object')
                        yield row
        except (ValueError, OSError) as exc:
            raise VictoriaMetricsError('/api/v1/export: unreadable JSON-line response') from exc

    def recent_activity(self, selectors=ALL_SERIES, *, lookback_seconds=300, end=None,
                        zero_tolerance=0.0) -> list[SeriesActivity]:
        """Inspect actual raw samples in [end-lookback, end], without query resampling.

        Catalog-only series are returned as no_data. NaN/Inf/null are invalid, not
        zero. Negative finite values are nonzero too. No absolute-value aggregation
        that could hide cancelling values or a brief inter-scrape spike is used.
        """
        if not math.isfinite(zero_tolerance) or zero_tolerance < 0:
            raise ValueError('zero_tolerance must be finite and nonnegative')
        start, end = self._window(lookback_seconds, end)
        matchers = self._selectors(selectors)
        selectors = [v for _, v in matchers]
        catalog = self.catalog(selectors, lookback_seconds=lookback_seconds, end=end)
        # Entries hold finite, nonzero, invalid counts and latest timestamp.
        counts = {s: [0, 0, 0, None] for s in catalog.series}
        params = matchers + [('start', start), ('end', end), ('max_rows_per_line', 1000)]
        for row in self._export(params):
            series = MetricSeries.from_labels(row.get('metric'))
            values, timestamps = row.get('values'), row.get('timestamps')
            if not isinstance(values, list) or not isinstance(timestamps, list) or len(values) != len(timestamps):
                raise VictoriaMetricsError('Export values/timestamps must be equal-length lists')
            stats = counts.setdefault(series, [0, 0, 0, None])
            for raw, stamp in zip(values, timestamps):
                try:
                    stamp = float(stamp) / 1000  # Export timestamps are milliseconds.
                except (TypeError, ValueError) as exc:
                    raise VictoriaMetricsError('Invalid export timestamp') from exc
                if not math.isfinite(stamp):
                    raise VictoriaMetricsError('Nonfinite export timestamp')
                if not start <= stamp <= end:
                    continue
                stats[3] = stamp if stats[3] is None else max(stats[3], stamp)
                if raw is None:
                    value = math.nan
                else:
                    try:
                        value = float(raw)
                    except (TypeError, ValueError) as exc:
                        raise VictoriaMetricsError('Invalid export sample encoding') from exc
                if not math.isfinite(value):
                    stats[2] += 1
                else:
                    stats[0] += 1
                    stats[1] += int(abs(value) > zero_tolerance)
        return [SeriesActivity(series, *counts[series])
                for series in sorted(counts, key=lambda s: (s.name, s.tags))]

    def has_recent_nonzero(self, selectors=ALL_SERIES, **kwargs) -> bool:
        return any(s.has_nonzero for s in self.recent_activity(selectors, **kwargs))

    def active_catalog(self, selectors=ALL_SERIES, *, lookback_seconds=300, end=None,
                       zero_tolerance=0.0) -> MetricCatalog:
        start, end = self._window(lookback_seconds, end)
        activity = self.recent_activity(selectors, lookback_seconds=lookback_seconds,
                                        end=end, zero_tolerance=zero_tolerance)
        return MetricCatalog(tuple(s.series for s in activity if s.has_nonzero), start, end)
