"""A callable and its metric pointers, without an execution engine."""
from __future__ import annotations

from dataclasses import dataclass
from inspect import Parameter, signature
from types import MappingProxyType
from typing import Callable, Mapping

from .metadata import MetricPointer, metric_name


def parameter_names(function: Callable) -> tuple[str, ...]:
    if not callable(function):
        raise TypeError('Composition function must be callable')
    try:
        parameters = signature(function).parameters.values()
    except (TypeError, ValueError) as exc:
        raise ValueError('Composition function must have an inspectable signature') from exc
    names = []
    for parameter in parameters:
        if parameter.kind not in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY):
            raise ValueError('Composition requires named parameters; positional-only and variadic parameters are unsupported')
        names.append(parameter.name)
    if not names:
        raise ValueError('Composition must have at least one metric parameter')
    return tuple(names)


@dataclass(frozen=True, slots=True)
class MetricComposition:
    """Deferred lambda/function and parameter -> exact metric pointer bindings.

    Every named parameter is bound explicitly, including those with defaults.
    Storing or resolving this description never invokes the function.
    """

    name: str
    function: Callable
    bindings: Mapping[str, MetricPointer]

    def __post_init__(self):
        metric_name(self.name)
        names = parameter_names(self.function)
        if not isinstance(self.bindings, Mapping) or set(self.bindings) != set(names):
            raise ValueError('Bindings must match all function parameter names exactly')
        if any(not isinstance(value, MetricPointer) for value in self.bindings.values()):
            raise TypeError('Composition bindings must be MetricPointer instances')
        object.__setattr__(self, 'bindings', MappingProxyType({name: self.bindings[name] for name in names}))

    @property
    def parameters(self) -> tuple[str, ...]:
        return tuple(self.bindings)
