"""Metadata selections: local-label-aware filters, groups and deferred bindings."""
from __future__ import annotations

import re
from typing import Callable, Iterable, Mapping

from .composition import MetricComposition, parameter_names
from .metadata import MetricMetadata, MetricPointer, MetricReference, label_names, pointer


class AmbiguousMetricError(ValueError):
    """A metric name or selection cannot identify exactly one source metric."""


class MetricCollection:
    """Immutable metadata entries in an independent selection; no sample values."""

    def __init__(self, metrics: Iterable[MetricMetadata] = ()):
        self._metrics: dict[MetricPointer, MetricMetadata] = {}
        for metric in metrics:
            if not isinstance(metric, MetricMetadata):
                raise TypeError('Collections accept MetricMetadata only')
            if metric.key in self._metrics:
                raise ValueError(f'Duplicate metric identity: {metric.key}')
            self._metrics[metric.key] = metric

    def __len__(self) -> int:
        return len(self._metrics)

    def __iter__(self):
        return iter(self._metrics.values())

    @property
    def metrics(self) -> tuple[MetricMetadata, ...]:
        return tuple(self)

    @property
    def keys(self) -> tuple[MetricPointer, ...]:
        return tuple(self._metrics)

    @property
    def names(self) -> list[str]:
        return sorted({metric.name for metric in self})

    @property
    def label_names(self) -> list[str]:
        return sorted({key for metric in self for key in metric.effective_labels})

    def label_values(self, label: str) -> list[str]:
        return sorted({metric.effective_labels[label] for metric in self if label in metric.effective_labels})

    @property
    def common_labels(self) -> dict[str, str]:
        items = iter(self)
        first = next(items, None)
        common = {} if first is None else first.effective_labels
        for metric in items:
            effective = metric.effective_labels
            common = {k: v for k, v in common.items() if k in effective and effective[k] == v}
        return common

    def get(self, reference: MetricReference) -> MetricMetadata:
        if isinstance(reference, str):
            matches = [metric for metric in self if metric.name == reference]
            if not matches:
                raise KeyError(reference)
            if len(matches) != 1:
                raise AmbiguousMetricError(f'{reference!r} has multiple source label sets; specify an exact pointer')
            return matches[0]
        return self._metrics[pointer(reference)]

    def one(self) -> MetricMetadata:
        if len(self) != 1:
            raise AmbiguousMetricError(f'Expected exactly one metric; found {len(self)}')
        return next(iter(self))

    def filter(self, *, prefix: str = '', name_regex: str | None = None,
               names: Iterable[str] | None = None, exclude_names: Iterable[str] = (),
               labels: Mapping[str, str] | None = None,
               label_regex: Mapping[str, str] | None = None) -> MetricCollection:
        allowed = None if names is None else set([names] if isinstance(names, str) else names)
        excluded = set([exclude_names] if isinstance(exclude_names, str) else exclude_names)
        pattern = re.compile(name_regex) if name_regex is not None else None
        regexes = {k: re.compile(v) for k, v in (label_regex or {}).items()}
        def matches(metric):
            effective = metric.effective_labels
            return (metric.name.startswith(prefix) and metric.name not in excluded
                    and (allowed is None or metric.name in allowed)
                    and (pattern is None or pattern.fullmatch(metric.name) is not None)
                    and all(k in effective and effective[k] == v for k, v in (labels or {}).items())
                    and all(k in effective and p.fullmatch(effective[k]) is not None for k, p in regexes.items()))
        return MetricCollection(metric for metric in self if matches(metric))

    def group_by(self, labels: Iterable[str], *, missing: str = 'drop') -> dict[tuple, MetricCollection]:
        names = label_names(labels)
        if missing not in ('drop', 'group', 'raise'):
            raise ValueError("missing must be 'drop', 'group' or 'raise'")
        groups = {}
        for metric in self:
            effective = metric.effective_labels
            if any(k not in effective for k in names):
                if missing == 'raise':
                    raise ValueError(f'Missing grouping label for {metric.key}')
                if missing == 'drop':
                    continue
            key = tuple((k, effective.get(k)) for k in names)
            groups.setdefault(key, []).append(metric)
        return {key: MetricCollection(metrics) for key, metrics in groups.items()}

    def union(self, *others: MetricCollection, conflict: str = 'raise') -> MetricCollection:
        if conflict not in ('raise', 'left', 'right'):
            raise ValueError("conflict must be 'raise', 'left' or 'right'")
        metrics = dict(self._metrics)
        for other in others:
            for key, metric in other._metrics.items():
                if key in metrics:
                    if metrics[key] != metric and conflict == 'raise':
                        raise ValueError(f'Conflicting metadata for {key}')
                    if conflict != 'right':
                        continue
                metrics[key] = metric
        return MetricCollection(metrics.values())

    def intersection(self, other: MetricCollection) -> MetricCollection:
        return MetricCollection(metric for key, metric in self._metrics.items() if key in other._metrics)

    def difference(self, other: MetricCollection) -> MetricCollection:
        return MetricCollection(metric for key, metric in self._metrics.items() if key not in other._metrics)

    def compose(self, name: str, function: Callable, *,
                bindings: Mapping[str, MetricReference] | None = None) -> MetricComposition:
        """Bind lambda parameters to exact metrics; never execute the lambda.

        Without bindings, parameter names are interpreted as unique metric names.
        Explicit bindings can use metadata or pointers obtained through filters.
        """
        parameters = parameter_names(function)
        if bindings is None:
            bindings = {parameter: parameter for parameter in parameters}
        if not isinstance(bindings, Mapping) or set(bindings) != set(parameters):
            raise ValueError('Bindings must match all function parameter names exactly')
        resolved = {parameter: self.get(bindings[parameter]).key for parameter in parameters}
        return MetricComposition(name, function, resolved)

    def resolve(self, composition: MetricComposition) -> dict[str, MetricMetadata]:
        """Return current metadata for each bound parameter, without fetching values."""
        if not isinstance(composition, MetricComposition):
            raise TypeError('Expected a MetricComposition')
        return {parameter: self.get(key) for parameter, key in composition.bindings.items()}
