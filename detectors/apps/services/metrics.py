"""In-process metadata registry; API transport and sample processing are external."""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable, Mapping

from .collection import AmbiguousMetricError, MetricCollection
from .composition import MetricComposition
from .metadata import (
    MetricMetadata, MetricPointer, MetricReference, additional_map, label_names,
    metric_name, pointer, timestamp_window,
)


LabelTargets = MetricReference | MetricCollection | Iterable[MetricReference]


class MetricService:
    """Manage names, labels, local labels, working ranges, steps and bindings.

    Receives ready API name lists or metadata supplied by the caller. It has no
    HTTP client, value buffers, query execution or lambda execution. Successful
    replacement changes the current catalog atomically. Local labels survive a
    metric's temporary absence. Stored compositions resolve against current
    metadata and fail explicitly when a bound metric is absent.
    """

    def __init__(self, metrics: Iterable[MetricMetadata] = ()):
        self._current: dict[MetricPointer, MetricMetadata] = {}
        self._additional: dict[MetricPointer, dict[str, str]] = {}
        self._compositions: dict[str, MetricComposition] = {}
        self.replace(metrics)

    def __len__(self) -> int:
        return len(self._current)

    def snapshot(self) -> MetricCollection:
        return MetricCollection(self._current.values())

    @property
    def metrics(self) -> tuple[MetricMetadata, ...]:
        return tuple(self._current.values())

    @property
    def names(self) -> list[str]:
        return self.snapshot().names

    def get(self, reference: MetricReference) -> MetricMetadata:
        return self.snapshot().get(reference)

    def load_names(self, names: Iterable[str], *, start: int | float,
                   end: int | float, step: int | float) -> MetricCollection:
        """Apply an API-provided name list and caller-provided sampling config.

        Known source label sets and local labels are retained for each listed
        name, including temporarily absent ones. New names start with empty labels.
        No endpoint is contacted and no samples are parsed.
        """
        if isinstance(names, (str, bytes, Mapping)):
            raise TypeError('names must be an iterable of metric names')
        start, end, step = timestamp_window(start, end, step)
        names = [metric_name(name) for name in names]
        if len(set(names)) != len(names):
            raise ValueError('API metric names must be unique')
        known_by_name = {}
        for key in self._additional:
            known_by_name.setdefault(key.name, []).append(key)
        metrics = []
        for name in names:
            known = known_by_name.get(name, [])
            for key in known or [MetricPointer(name)]:
                metrics.append(MetricMetadata(name=name, labels=key.labels, start=start,
                                              end=end, step=step))
        return self.replace(metrics)

    def load(self, payload, *, start: int | float, end: int | float,
             step: int | float) -> MetricCollection:
        """Accept a ready name list or a successful API envelope with that list."""
        if isinstance(payload, Mapping):
            if payload.get('status') != 'success' or not isinstance(payload.get('data'), list):
                raise ValueError('Expected a successful API response containing a name list')
            payload = payload['data']
        return self.load_names(payload, start=start, end=end, step=step)

    def replace(self, metrics: Iterable[MetricMetadata]) -> MetricCollection:
        incoming = MetricCollection(metrics)
        current, additional = {}, dict(self._additional)
        for metric in incoming:
            local = {**additional.get(metric.key, {}), **metric.additional_labels}
            metric = replace(metric, additional_labels=local)
            current[metric.key] = metric
            additional[metric.key] = dict(metric.additional_labels)
        self._current, self._additional = current, additional
        return self.snapshot()

    def add(self, metric: MetricMetadata) -> MetricMetadata:
        if not isinstance(metric, MetricMetadata):
            raise TypeError('Expected MetricMetadata')
        if metric.key in self._current:
            raise KeyError(f'Metric already exists: {metric.key}')
        local = {**self._additional.get(metric.key, {}), **metric.additional_labels}
        metric = replace(metric, additional_labels=local)
        self._current[metric.key] = metric
        self._additional[metric.key] = dict(metric.additional_labels)
        return metric

    def update(self, reference: MetricReference, **changes) -> MetricMetadata:
        """Change source metadata, range or step; migrate stored pointers on rekeying.

        Additional labels are edited through the dedicated label methods.
        """
        if changes.keys() - {'name', 'labels', 'start', 'end', 'step'}:
            raise ValueError('Only name, labels, start, end and step can be updated here')
        previous = self.get(reference)
        updated = replace(previous, **changes)
        old_key, new_key = previous.key, updated.key
        if new_key != old_key and new_key in self._current:
            raise KeyError(f'Metric already exists: {new_key}')
        compositions = {
            name: replace(composition, bindings={
                parameter: new_key if key == old_key else key
                for parameter, key in composition.bindings.items()
            }) for name, composition in self._compositions.items()
        } if new_key != old_key else self._compositions
        current = dict(self._current)
        del current[old_key]
        current[new_key] = updated
        additional = dict(self._additional)
        additional.pop(old_key, None)
        additional[new_key] = dict(updated.additional_labels)
        self._current, self._additional, self._compositions = current, additional, compositions
        return updated

    def remove(self, reference: MetricReference) -> MetricMetadata:
        metric = self.get(reference)
        del self._current[metric.key]
        self._additional.pop(metric.key, None)
        return metric

    def clear(self) -> None:
        self._current.clear()
        self._additional.clear()
        self._compositions.clear()

    def filter(self, **filters) -> MetricCollection:
        return self.snapshot().filter(**filters)

    def group_by(self, labels: Iterable[str], *, missing: str = 'drop') -> dict[tuple, MetricCollection]:
        return self.snapshot().group_by(labels, missing=missing)

    def _targets(self, targets: LabelTargets) -> tuple[MetricPointer, ...]:
        if isinstance(targets, (str, MetricPointer, MetricMetadata)):
            targets = [targets]
        elif isinstance(targets, MetricCollection):
            targets = targets.keys
        keys = []
        for target in targets:
            if isinstance(target, str):
                matches = [key for key in self._additional if key.name == target]
                if not matches:
                    raise KeyError(target)
                if len(matches) != 1:
                    raise AmbiguousMetricError(f'{target!r} requires an exact pointer')
                key = matches[0]
            else:
                key = pointer(target)
            if key not in self._additional:
                raise KeyError(key)
            keys.append(key)
        return tuple(dict.fromkeys(keys))

    def set_window(self, targets: LabelTargets, *, start: int | float,
                   end: int | float, step: int | float) -> None:
        start, end, step = timestamp_window(start, end, step)
        updates = {key: replace(self._current[key], start=start, end=end, step=step)
                   for key in self._targets(targets)}
        self._current.update(updates)

    def _edit_labels(self, targets: LabelTargets, labels, action: str) -> None:
        keys = self._targets(targets)
        names = label_names(labels) if action == 'remove' else None
        supplied = None if action == 'remove' else additional_map(labels, {})
        edits, current = {}, {}
        for key in keys:
            old = self._additional[key]
            if action == 'remove':
                if any(k not in old for k in names):
                    raise KeyError('Cannot remove an absent additional label')
                new = {k: v for k, v in old.items() if k not in names}
            else:
                supplied = additional_map(supplied, key.labels)
                if action == 'add' and supplied.keys() & old.keys():
                    raise KeyError('Additional label already exists; use update or set')
                if action == 'update' and any(k not in old for k in supplied):
                    raise KeyError('Cannot update an absent additional label; use add or set')
                new = supplied if action == 'set' else {**old, **supplied}
            edits[key] = dict(new)
            if key in self._current:
                current[key] = replace(self._current[key], additional_labels=new)
        self._additional.update(edits)
        self._current.update(current)

    def add_additional_labels(self, targets: LabelTargets, labels: Mapping[str, str]) -> None:
        self._edit_labels(targets, labels, 'add')

    def update_additional_labels(self, targets: LabelTargets, labels: Mapping[str, str]) -> None:
        self._edit_labels(targets, labels, 'update')

    def set_additional_labels(self, targets: LabelTargets, labels: Mapping[str, str]) -> None:
        self._edit_labels(targets, labels, 'set')

    def remove_additional_labels(self, targets: LabelTargets, labels: Iterable[str]) -> None:
        self._edit_labels(targets, labels, 'remove')

    @property
    def compositions(self) -> tuple[MetricComposition, ...]:
        return tuple(self._compositions.values())

    def compose(self, name: str, function: Callable, *,
                bindings: Mapping[str, MetricReference] | None = None,
                overwrite: bool = False) -> MetricComposition:
        if name in self._compositions and not overwrite:
            raise KeyError(f'Composition already exists: {name}')
        composition = self.snapshot().compose(name, function, bindings=bindings)
        self._compositions[composition.name] = composition
        return composition

    def get_composition(self, name: str) -> MetricComposition:
        return self._compositions[name]

    def remove_composition(self, name: str) -> MetricComposition:
        return self._compositions.pop(name)

    def resolve(self, composition: str | MetricComposition) -> dict[str, MetricMetadata]:
        if isinstance(composition, str):
            composition = self.get_composition(composition)
        return self.snapshot().resolve(composition)
