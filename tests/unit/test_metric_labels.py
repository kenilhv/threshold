"""The label schema, and the two directions it can be broken.

Cardinality is what kills a metrics backend, and it dies to one careless
attribute: a `document_id` on one counter turns a six-series metric into a
series per invoice, permanently, and nothing fails at the time. Review is a poor
control for it because the offending line looks perfectly reasonable where it is
written.

So there are two guards, and they face opposite ways.

*Outward*, at the call site: `configure_metrics` compiles `REGISTRY` into SDK
views with `attribute_keys=`, so an attribute nobody declared is removed by the
SDK before an exporter ever sees it. `TestTheAllowlistIsEnforced` proves that
happens rather than assuming it.

*Inward*, at the registry: someone can always add `document_id` to a
declaration. `FORBIDDEN_LABELS` is the guard for that, and
`test_the_check_can_fail` is the guard for the guard — a check that cannot fail
is the way this suite has fooled itself three times already.
"""

from __future__ import annotations

import re

import pytest
from opentelemetry.sdk.metrics.view import DropAggregation
from tests.metrics_reader import Recording

from threshold.domain.jobs import JobKind
from threshold.domain.matching import Disposition
from threshold.observability import metrics as m

#: What an unbounded label value looks like when one gets through. Checked
#: against real values rather than against key names, because the way this
#: actually happens is a well-named key holding a terrible value.
UNBOUNDED_VALUE = re.compile(
    r"""
    [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}  # a uuid
    | [0-9a-f]{32}                                                # a trace id
    | \d{6,}                                                      # a long number
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _forbidden(spec: m.MetricSpec) -> set[str]:
    return set(spec.labels) & m.FORBIDDEN_LABELS


class TestTheRegistryItself:
    def test_no_instrument_declares_a_forbidden_label(self) -> None:
        offenders = {spec.name: sorted(_forbidden(spec)) for spec in m.REGISTRY if _forbidden(spec)}
        assert not offenders, (
            f"these labels grow with the number of invoices or people the system has seen: "
            f"{offenders}. The trace and the audit row carry per-entity detail; a metric "
            f"carries what is true of all of them."
        )

    def test_the_check_can_fail(self) -> None:
        """The guard for the guard.

        A label test that passes because it checks nothing is worse than no
        label test, and this suite has shipped that exact mistake three times.
        """
        planted = m.MetricSpec(
            name="threshold.bad",
            prometheus_name="threshold_bad",
            labels=frozenset({"disposition", "document_id"}),
            dashboards=("nothing",),
            alerts=(),
            emitted_by="nowhere",
        )
        assert _forbidden(planted) == {"document_id"}

    def test_every_instrument_names_what_reads_it(self) -> None:
        """A metric nobody plots is this repository's oldest defect in a new hat.

        `trace_id` held the wrong value for three milestones and
        `DeadLetter.dedupe_key` has never been written; both were declared and
        never read. A dashboard panel or an alert rule has to be named — and
        `test_dashboard.py` then checks the named panel actually exists.
        """
        unread = [spec.name for spec in m.REGISTRY if not spec.consumers]
        assert not unread, f"no consumer declared for: {unread}"

    def test_a_consumer_name_is_not_blank(self) -> None:
        """An empty string would satisfy the check above and name nothing."""
        blank = [spec.name for spec in m.REGISTRY if any(not c.strip() for c in spec.consumers)]
        assert not blank, f"blank consumer name on: {blank}"

    def test_every_instrument_says_what_prometheus_calls_it(self) -> None:
        """The name a dashboard has to query, which is not the name here.

        Checked for shape only; `test_telemetry_stack.py` checks it against a
        running Prometheus, which is the only place the claim can be settled.
        """
        for spec in m.REGISTRY:
            assert spec.prometheus_name, spec.name
            assert "." not in spec.prometheus_name, (
                f"{spec.prometheus_name} still has a dot; Prometheus replaces them"
            )
            # Prometheus only ever ADDS to the underscored name -- a unit
            # suffix, a `_total` -- so the underscored form is always a prefix
            # of it. A typo like `threshhold_...` fails here rather than as a
            # blank panel nobody looks at.
            assert spec.prometheus_name.startswith(spec.name.replace(".", "_")), (
                f"{spec.prometheus_name} is not {spec.name} with its dots replaced"
            )

    def test_names_are_unique(self) -> None:
        names = [spec.name for spec in m.REGISTRY]
        assert len(names) == len(set(names))

    def test_names_are_what_the_sdk_accepts(self) -> None:
        """An invalid instrument name raises a bare `Exception` at creation.

        Not a ValueError, and not at import of this module — at the
        `create_counter` call, which in production is module import of
        `metrics.py`, which is API startup. Worth failing here instead.
        """
        for spec in m.REGISTRY:
            assert spec.name.isascii(), spec.name
            assert len(spec.name) <= 63, spec.name
            assert not spec.name[0].isdigit(), spec.name
            assert spec.name == spec.name.lower(), (
                f"{spec.name} is not lowercase; the SDK lowercases instrument names "
                "silently, so the registry would disagree with the exposition"
            )

    def test_a_dropped_metric_is_not_also_declared(self) -> None:
        """Declaring and dropping the same instrument is a contradiction."""
        assert not set(m.DROPPED) & set(m.BY_NAME)


class TestViewsAreGeneratedFromTheRegistry:
    def test_one_view_per_declaration(self) -> None:
        views = m._views()
        assert len(views) == len(m.REGISTRY) + len(m.DROPPED)

    def test_each_view_allows_exactly_what_was_declared(self) -> None:
        by_name = {view._instrument_name: view for view in m._views()}
        for spec in m.REGISTRY:
            assert by_name[spec.name]._attribute_keys == set(spec.labels)

    def test_the_unwanted_instruments_are_dropped_not_merely_untouched(self) -> None:
        by_name = {view._instrument_name: view for view in m._views()}
        for name in m.DROPPED:
            assert isinstance(by_name[name]._aggregation, DropAggregation)


class TestTheAllowlistIsEnforced:
    """The registry is enforcement, not documentation.

    These record deliberately wrong attributes through the real instruments and
    assert the SDK removed them, which is the only way to know the views are
    wired rather than merely written.
    """

    def test_an_undeclared_attribute_is_dropped(self, recording: Recording) -> None:
        m._match_decisions.add(
            1,
            {
                "disposition": Disposition.AUTO_APPROVE.value,
                "policy_version": "2026.1",
                "document_id": "8c7b1e1a-0000-4000-8000-000000000000",
            },
        )
        labels = recording.labels(m.MATCH_DECISIONS)
        assert labels, "nothing was recorded, so this test proves nothing"
        for attributes in labels:
            assert "document_id" not in attributes

    def test_the_declared_attributes_survive(self, recording: Recording) -> None:
        """The other half: a view that dropped everything would pass the test above."""
        m._match_decisions.add(
            1, {"disposition": Disposition.BLOCKED.value, "policy_version": "2026.1"}
        )
        assert {"disposition": "blocked", "policy_version": "2026.1"} in recording.labels(
            m.MATCH_DECISIONS
        )

    def test_no_exported_value_looks_unbounded(self, recording: Recording) -> None:
        """Catches a well-named key holding a terrible value.

        `http.target` is the route template, which is safe; if the
        instrumentation ever started sending the raw path instead, every invoice
        would become a series and the key name would not change.
        """
        offenders = [
            (name, key, value)
            for name, attributes in recording.every_point()
            for key, value in attributes.items()
            if isinstance(value, str) and UNBOUNDED_VALUE.search(value)
        ]
        assert not offenders, f"unbounded label values: {offenders}"


class TestJobKindIsClamped:
    """`Job.kind` is a string column, so it is a row, not an enum.

    The worker has an unknown-kind path precisely because the value may not be a
    kind this build knows. Passing it through would put a database row in a
    label, on the one path nobody exercises.
    """

    @pytest.mark.parametrize("kind", [k.value for k in JobKind])
    def test_a_known_kind_passes_through(self, kind: str) -> None:
        assert m._kind_label(kind) == kind

    @pytest.mark.parametrize(
        "kind", ["", "pay_invoice", "extract_document_v2", "'; drop table job--"]
    )
    def test_anything_else_collapses(self, kind: str) -> None:
        assert m._kind_label(kind) == "unknown"

    def test_the_clamp_is_applied_by_the_recorder(self, recording: Recording) -> None:
        """Not merely available: the helper has to use it."""
        m.record_job_execution(
            kind="a-kind-from-the-future", outcome=m.JobOutcome.NO_HANDLER, seconds=0.1
        )
        assert recording.labels(m.JOB_DURATION) == [{"kind": "unknown", "outcome": "no_handler"}]
