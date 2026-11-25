"""Only metric identity, labels and requested sampling configuration live here."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from numbers import Integral, Real
from types import MappingProxyType
from typing import Iterable, Mapping


def metric_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError('name must be a nonempty string')
    return name


def label_map(labels: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(labels, Mapping) or any(
        not isinstance(k, str) or not k or not isinstance(v, str) for k, v in labels.items()
    ):
        raise ValueError('labels must have nonempty string keys and string values')
    if '__name__' in labels:
        raise ValueError('__name__ is reserved; use the name field')
    return dict(labels)


def additional_map(labels: Mapping[str, str], source: Mapping[str, str]) -> dict[str, str]:
    local = label_map(labels)
    if local.keys() & source.keys():
        raise ValueError('additional_labels must not override source labels')
    return local


def label_names(labels: Iterable[str]) -> tuple[str, ...]:
    names = (labels,) if isinstance(labels, str) else tuple(labels)
    if any(not isinstance(k, str) or not k for k in names) or len(set(names)) != len(names):
        raise ValueError('label names must be unique nonempty strings')
    return names


def timestamp_window(start, end, step) -> tuple[int | float, int | float, int | float]:
    normalized = []
    for name, value in (('start', start), ('end', end), ('step', step)):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f'{name} must be finite seconds')
        normalized.append(int(value) if isinstance(value, Integral) else float(value))
    start, end, step = normalized
    if start > end or step <= 0:
        raise ValueError('start must not exceed end; step must be positive')
    return start, end, step


@dataclass(frozen=True, slots=True)
class MetricPointer:
    """Exact source identity; local labels and sampling configuration are excluded."""

    name: str
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        metric_name(self.name)
        object.__setattr__(self, 'labels', MappingProxyType(label_map(self.labels)))

    def __hash__(self):
        return hash((self.name, tuple(sorted(self.labels.items()))))


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricMetadata:
    """Description of a metric and its working range, containing no samples.

    labels are source labels. effective_labels includes __name__ and local labels.
    Copied read-only dictionaries make metadata safe to share between snapshots.
    All time fields are seconds and can include fractional values.
    """

    name: str
    start: int | float
    end: int | float
    step: int | float
    labels: Mapping[str, str] = field(default_factory=dict)
    additional_labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        metric_name(self.name)
        source = label_map(self.labels)
        local = additional_map(self.additional_labels, source)
        start, end, step = timestamp_window(self.start, self.end, self.step)
        object.__setattr__(self, 'labels', MappingProxyType(source))
        object.__setattr__(self, 'additional_labels', MappingProxyType(local))
        object.__setattr__(self, 'start', start)
        object.__setattr__(self, 'end', end)
        object.__setattr__(self, 'step', step)

    @property
    def key(self) -> MetricPointer:
        return MetricPointer(self.name, self.labels)

    @property
    def effective_labels(self) -> dict[str, str]:
        return {'__name__': self.name, **self.labels, **self.additional_labels}

    @property
    def timestamp_range(self) -> tuple[int | float, int | float]:
        return self.start, self.end

    def to_dict(self) -> dict:
        return {'name': self.name, 'labels': dict(self.labels),
                'additional_labels': dict(self.additional_labels),
                'start': self.start, 'end': self.end, 'step': self.step}


MetricReference = str | MetricPointer | MetricMetadata


def pointer(reference: MetricPointer | MetricMetadata) -> MetricPointer:
    if isinstance(reference, MetricMetadata):
        return reference.key
    if isinstance(reference, MetricPointer):
        return reference
    raise TypeError('Expected a MetricPointer or MetricMetadata')
