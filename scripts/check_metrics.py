"""Every metric the registry declares, found in Prometheus by the name it claims.

Prints the number of declared metrics that are missing, which `make smoke`
compares against 0.

Polls, because push is not scrape. The services export on their own fifteen
second timer, so a metric recorded by the work the smoke test just did has not
necessarily left the process yet. Asking once would be measuring the export
interval rather than the property, and the first version of this check did
exactly that and failed.

The property itself is worth checking here and not only in the integration
suite: `MetricSpec.prometheus_name` is a guess about a translation Prometheus
performs in Go, and a wrong guess produces an empty Grafana panel and no error
anywhere at all.
"""

from __future__ import annotations

import sys
import time

import httpx

from threshold.observability.metrics import REGISTRY

PROMETHEUS = "http://localhost:9090"
BUDGET_SECONDS = 40


def present() -> set[str]:
    response = httpx.get(f"{PROMETHEUS}/api/v1/label/__name__/values", timeout=10)
    response.raise_for_status()
    return set(response.json()["data"])


def missing() -> list[str]:
    names = present()
    absent = []
    for spec in REGISTRY:
        # A histogram arrives as _bucket/_count/_sum rather than under its own
        # name; _count is the part that exists whatever the buckets are.
        if spec.prometheus_name in names or f"{spec.prometheus_name}_count" in names:
            continue
        absent.append(f"{spec.name} -> {spec.prometheus_name}")
    return absent


def main() -> int:
    absent: list[str] = []
    deadline = time.monotonic() + BUDGET_SECONDS
    while True:
        try:
            absent = missing()
        except httpx.HTTPError as exc:
            print(f"prometheus unreachable: {exc}", file=sys.stderr)
            print(len(REGISTRY))
            return 1
        if not absent or time.monotonic() > deadline:
            break
        time.sleep(2)

    print(len(absent))
    if absent:
        print("declared but absent from Prometheus:", absent, file=sys.stderr)
    return 1 if absent else 0


if __name__ == "__main__":
    raise SystemExit(main())
