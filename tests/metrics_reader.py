"""The one meter provider the suite can read, and the only way to read it.

Two facts about the OpenTelemetry metrics SDK decide the shape of this file, and
both were measured:

A meter provider can be **set once per process**. The second call is refused
with a warning, and every later measurement keeps going to the first provider.
`api/main.py` calls `configure_metrics` at import time, so the moment a test
module imports the app, the process would otherwise hold a provider with no
reader and nothing could be asserted about any instrument for the rest of the
run. `tests/conftest.py` imports this module before pytest collects anything,
which is early enough.

The instruments in `observability/metrics.py` are **module-level and pin
themselves to the first provider they resolve against**, so a test cannot be
handed a private provider the way the tracing tests are. It has to read the same
instruments production records into, or it is testing copies.

This lives outside `conftest.py` because pytest loads the rootdir conftest under
its own module name, so a test doing `from tests.conftest import ...` imports a
*second* copy — which builds a second reader, has it refused, and then reads an
empty exposition while every assertion passes. That is not hypothetical; it
happened while this file was being written, and `install_test_provider` now
raises rather than allowing it.

The reader is CUMULATIVE, like production. `get_metrics_data()` returns running
totals and does not drain, so a test that wants to know what one call recorded
must diff two readings — which is what `Recording` is for. An absolute assertion
would pass or fail depending on which tests ran before it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from threshold.observability.metrics import install_test_provider

METRICS = InMemoryMetricReader()
install_test_provider(METRICS)

#: A data point, flattened to the two things a test cares about.
Point = tuple[frozenset[tuple[str, Any]], float]


def _read() -> dict[str, list[Point]]:
    data = METRICS.get_metrics_data()
    if data is None:
        return {}
    out: dict[str, list[Point]] = {}
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                points = out.setdefault(metric.name, [])
                for point in metric.data.data_points:
                    # Sums and gauges carry `value`; histograms carry `count`.
                    value = getattr(point, "value", None)
                    if value is None:
                        value = getattr(point, "count", 0)
                    points.append((frozenset(dict(point.attributes).items()), float(value)))
    return out


@dataclass(frozen=True, slots=True)
class Recording:
    """What has changed since this object was made.

    Every accessor re-reads, so a test can assert partway through and again
    later without having to close anything.
    """

    before: dict[str, list[Point]]

    def _moved(self) -> dict[str, set[Point]]:
        return {
            name: set(points) - set(self.before.get(name, [])) for name, points in _read().items()
        }

    def names(self) -> set[str]:
        """Metric names that gained a point, or whose value moved."""
        return {name for name, moved in self._moved().items() if moved}

    def labels(self, name: str) -> list[dict[str, Any]]:
        """Attribute sets that appeared or changed for one metric."""
        return [dict(attributes) for attributes, _ in self._moved().get(name, set())]

    def _at(self, snapshot: dict[str, list[Point]], name: str, wanted: frozenset[Any]) -> float:
        return sum(value for keys, value in snapshot.get(name, []) if keys == wanted)

    def increase(self, name: str, **attributes: Any) -> float:
        """How much a COUNTER moved. A delta, and it has to be.

        The reader is cumulative, so a counter's data point holds every
        recording the process has ever made. Reading the point directly gives a
        number that depends on which tests ran first: in isolation it is the one
        increment the test caused, and in a full run it is that plus everything
        before it. That is exactly how a green test turns red when the suite is
        reordered, and it happened here before this was a subtraction.
        """
        wanted = frozenset(attributes.items())
        return self._at(_read(), name, wanted) - self._at(self.before, name, wanted)

    def value(self, name: str, **attributes: Any) -> float:
        """What a GAUGE currently reads. A level, not a delta.

        Subtracting two readings of a gauge would be meaningless: queue depth is
        the number of jobs now, not the number since the last look.
        """
        return self._at(_read(), name, frozenset(attributes.items()))

    def bounds(self, name: str) -> tuple[float, ...]:
        """The bucket boundaries the SDK actually resolved for a histogram."""
        data = METRICS.get_metrics_data()
        if data is not None:
            for resource_metric in data.resource_metrics:
                for scope_metric in resource_metric.scope_metrics:
                    for metric in scope_metric.metrics:
                        if metric.name != name:
                            continue
                        point = next(iter(metric.data.data_points))
                        return tuple(point.explicit_bounds)
        raise AssertionError(f"{name} has never been recorded, so it has no buckets")

    def every_point(self) -> Iterator[tuple[str, Mapping[str, Any]]]:
        """Every (metric, attributes) pair the reader currently holds.

        Not a delta. An undeclared label is just as wrong on a series that has
        not moved since the last reading, so the checks that police the label
        schema look at the whole exposition.
        """
        for name, points in _read().items():
            for attributes, _ in points:
                yield name, dict(attributes)
