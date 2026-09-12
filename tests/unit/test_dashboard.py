"""The dashboard, checked against the registry that claims it exists.

`observability/metrics.py` says of every instrument which panels plot it. That
is a claim about a file in `infra/`, and an unchecked claim is how this
repository's oldest defect works: `audit_event.trace_id` promised to hold a
trace id for three milestones, `DeadLetter.dedupe_key` is declared and has never
been written, and the connection-pool metric was declared and never produced
until a test asked for it.

So the claim is checked, in both directions. Every panel a metric names must
exist in the provisioned JSON, and every metric a panel queries must exist in
the registry. Neither half alone is enough: the first lets a panel query a
metric nobody declared, and the second lets a metric name a panel that was
deleted.

Nothing here needs a running stack. Grafana provisions from these files, and a
dashboard that names the wrong series renders an empty panel and reports no
error at all — which is precisely why it has to fail here instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from threshold.observability import metrics as m

DASHBOARD = Path(__file__).resolve().parents[2] / "infra/local/grafana/dashboards/threshold.json"

#: PromQL identifiers that are not metric names. Anything left over after these
#: are removed has to be something the registry declares.
PROMQL_VOCABULARY = frozenset(
    {
        "by",
        "clamp_min",
        "histogram_quantile",
        "increase",
        "le",
        "max",
        "rate",
        "sum",
        "without",
    }
)

IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
#: The suffixes Prometheus adds to a histogram, which a query names directly.
HISTOGRAM_PARTS = ("_bucket", "_count", "_sum")


@pytest.fixture(scope="module")
def dashboard() -> dict[str, Any]:
    assert DASHBOARD.exists(), f"the provisioned dashboard is missing: {DASHBOARD}"
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    return list(dashboard.get("panels", []))


def _expressions(dashboard: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (panel["title"], target["expr"])
        for panel in _panels(dashboard)
        for target in panel.get("targets", [])
        if target.get("expr")
    ]


def _known_series() -> set[str]:
    """Every series name the registry accounts for, histogram parts included."""
    names: set[str] = set()
    for spec in m.REGISTRY:
        names.add(spec.prometheus_name)
        names.update(spec.prometheus_name + part for part in HISTOGRAM_PARTS)
    return names


def _metric_names_in(expression: str) -> set[str]:
    """Identifiers in a PromQL expression that must be metric names.

    The two places a label name appears are stripped first -- matchers, so
    `http_status_code=~"5.."` does not look like a metric, and aggregation
    groupings, so `by (disposition)` does not either. What remains, minus
    PromQL's own vocabulary, is the series the panel is asking for.
    """
    stripped = re.sub(r"\{[^}]*\}", " ", expression)
    stripped = re.sub(r"\b(?:by|without)\s*\([^)]*\)", " ", stripped)
    stripped = re.sub(r"\$__\w+", " ", stripped)
    return {token for token in IDENTIFIER.findall(stripped) if token not in PROMQL_VOCABULARY}


class TestTheDashboardIsWellFormed:
    def test_the_uid_is_pinned(self, dashboard: dict[str, Any]) -> None:
        """Without it Grafana mints a new one whenever its database is empty.

        A `docker compose down -v` would then change the dashboard's URL, so
        every link to it — including the one in the README — breaks silently.
        """
        assert dashboard.get("uid"), "a provisioned dashboard must pin its uid"

    def test_it_has_a_title(self, dashboard: dict[str, Any]) -> None:
        """The only field Grafana's provisioner actually requires."""
        assert dashboard.get("title", "").strip()

    def test_panel_ids_are_unique(self, dashboard: dict[str, Any]) -> None:
        ids = [panel["id"] for panel in _panels(dashboard)]
        assert len(ids) == len(set(ids))

    def test_every_panel_is_placed(self, dashboard: dict[str, Any]) -> None:
        """Grafana provisions a panel with no gridPos without complaint, and
        then renders every one of them as a tiny box in the top-left corner."""
        for panel in _panels(dashboard):
            grid = panel.get("gridPos", {})
            assert set(grid) == {"x", "y", "w", "h"}, panel["title"]
            assert 0 < grid["w"] <= 24, panel["title"]
            assert grid["h"] > 0, panel["title"]

    def test_every_panel_queries_something(self, dashboard: dict[str, Any]) -> None:
        for panel in _panels(dashboard):
            targets = panel.get("targets", [])
            assert targets, f"{panel['title']} has no query"
            assert all(t.get("expr") for t in targets), panel["title"]

    def test_every_target_binds_to_the_provisioned_datasource(
        self, dashboard: dict[str, Any]
    ) -> None:
        """The uid is a string contract with `provisioning/datasources`.

        A mismatch is not an error anywhere: the panel renders "Datasource not
        found" to whoever happens to open it.
        """
        for panel in _panels(dashboard):
            for target in panel.get("targets", []):
                assert target["datasource"]["uid"] == "prometheus", panel["title"]


class TestTheRegistryAndTheDashboardAgree:
    def test_every_declared_panel_exists(self, dashboard: dict[str, Any]) -> None:
        """The registry's side of the claim."""
        titles = {panel["title"] for panel in _panels(dashboard)}
        missing = {
            spec.name: [d for d in spec.dashboards if d not in titles]
            for spec in m.REGISTRY
            if any(d not in titles for d in spec.dashboards)
        }
        assert not missing, (
            f"these metrics name a dashboard panel that does not exist: {missing}. "
            f"The dashboard has: {sorted(titles)}"
        )

    def test_every_panel_queries_a_declared_metric(self, dashboard: dict[str, Any]) -> None:
        """The dashboard's side of it.

        Catches the panel that outlived the metric it plots, and the typo that
        turns a working query into a permanently empty graph.
        """
        known = _known_series()
        offenders = {
            title: sorted(_metric_names_in(expr) - known)
            for title, expr in _expressions(dashboard)
            if _metric_names_in(expr) - known
        }
        assert not offenders, (
            f"these panels query series the registry does not declare: {offenders}"
        )

    def test_no_panel_is_orphaned(self, dashboard: dict[str, Any]) -> None:
        """A panel nobody's registry entry claims is a panel nobody maintains."""
        claimed = {name for spec in m.REGISTRY for name in spec.dashboards}
        titles = {panel["title"] for panel in _panels(dashboard)}
        assert titles - claimed == set(), (
            f"these panels are not claimed by any metric in REGISTRY: {sorted(titles - claimed)}"
        )

    def test_the_check_is_not_vacuous(self, dashboard: dict[str, Any]) -> None:
        """Both halves above pass trivially against an empty dashboard."""
        assert len(_panels(dashboard)) >= 10
        assert len(_expressions(dashboard)) >= 10
        assert _known_series(), "the registry declares no series at all"

    def test_every_label_a_query_groups_by_is_declared(self, dashboard: dict[str, Any]) -> None:
        """`sum by (owner_role)` on a metric that has no owner_role silently
        collapses every series into one, which looks like a working panel.

        `le` is excluded: Prometheus synthesises it on histogram buckets, so it
        is never an attribute the application declares.
        """
        declared = {m.prometheus_label(label) for spec in m.REGISTRY for label in spec.labels} | {
            "le"
        }
        grouped = {
            label.strip()
            for _, expr in _expressions(dashboard)
            for group in re.findall(r"by\s*\(([^)]*)\)", expr)
            for label in group.split(",")
        }
        assert grouped <= declared, f"grouped by undeclared labels: {sorted(grouped - declared)}"

    def test_every_label_a_query_filters_on_is_declared(self, dashboard: dict[str, Any]) -> None:
        declared = {m.prometheus_label(label) for spec in m.REGISTRY for label in spec.labels}
        filtered = {
            match
            for _, expr in _expressions(dashboard)
            for matcher in re.findall(r"\{([^}]*)\}", expr)
            for match in re.findall(r"(\w+)\s*(?:=~|!~|=|!=)", matcher)
        }
        assert filtered <= declared, f"filtered on undeclared labels: {sorted(filtered - declared)}"
