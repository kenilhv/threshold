"""The telemetry stack, asked whether it actually has what was promised.

Three claims meet reality here, and none of them can be settled anywhere else:

**The translated names.** Every ``MetricSpec`` writes down what Prometheus will
call its series. That translation happens in Go, inside Prometheus, and it is
not guessable: dots become underscores, a counter gains ``_total``, seconds
append ``_seconds`` -- but a unit whose word already appears in the name appends
nothing, which is why ``db.client.connections.usage`` is
``db_client_connections_usage`` and not ``..._connections``. A wrong guess is
invisible: Grafana renders an empty panel and reports no error.

**The trace really crosses the process boundary.** ``audit_event.trace_id`` is
looked up in Tempo, and the trace it returns has to contain spans from both
services. That is the M5 promise end to end, through a real exporter and a real
backend, rather than through an in-memory span processor.

**The dashboard is actually provisioned.** A JSON file that parses is not a
dashboard Grafana loaded.

Requires: make up
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from threshold.observability import metrics as m

pytestmark = pytest.mark.integration

PROMETHEUS = "http://localhost:9090"
TEMPO = "http://localhost:3200"
GRAFANA = "http://localhost:3001"
API = "http://localhost:8000"

#: A selector for the series this application produces, so a query does not also
#: pick up Prometheus's own self-monitoring metrics.
_OURS = '{__name__=~"threshold_.*|http_server_.*|db_client_.*"}'


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=30) as c:
        for name, url in (("Prometheus", PROMETHEUS), ("Tempo", TEMPO), ("Grafana", GRAFANA)):
            try:
                c.get(url, follow_redirects=True)
            except httpx.HTTPError:
                pytest.skip(f"{name} is not running; `make up`")
        yield c


def _instant(client: httpx.Client, query: str) -> list[dict[str, Any]]:
    response = client.get(f"{PROMETHEUS}/api/v1/query", params={"query": query})
    response.raise_for_status()
    body = response.json()
    assert body["status"] == "success", body
    return list(body["data"]["result"])


def _series_names(client: httpx.Client) -> set[str]:
    response = client.get(f"{PROMETHEUS}/api/v1/label/__name__/values")
    response.raise_for_status()
    return set(response.json()["data"])


class TestPrometheusHasWhatTheRegistryPromised:
    def test_the_otlp_receiver_is_enabled(self, client: httpx.Client) -> None:
        """One missing flag and every metric silently goes nowhere.

        Without ``--web.enable-otlp-receiver`` the push path 404s, the exporter
        retries against a server that is otherwise perfectly healthy, and the
        only evidence is a log line inside the application container. A GET
        answering 405 rather than 404 proves the route exists.
        """
        response = client.get(f"{PROMETHEUS}/api/v1/otlp/v1/metrics")
        assert response.status_code == 405, (
            "the OTLP receiver is not enabled; Prometheus needs --web.enable-otlp-receiver"
        )

    def test_every_declared_series_name_exists(self, client: httpx.Client) -> None:
        """The claim each MetricSpec makes about its Prometheus name.

        Histograms are checked through ``_count``, which is the part that exists
        whatever the bucket layout is.
        """
        present = _series_names(client)
        missing = []
        for spec in m.REGISTRY:
            if spec.prometheus_name in present:
                continue
            if f"{spec.prometheus_name}_count" in present:
                continue
            missing.append(f"{spec.name} -> {spec.prometheus_name}")
        assert not missing, (
            f"declared Prometheus names that do not exist: {missing}. "
            "A wrong name here is an empty dashboard panel and no error anywhere."
        )

    def test_the_check_is_not_vacuous(self, client: httpx.Client) -> None:
        """A name Prometheus has never seen must genuinely be absent."""
        assert "threshold_not_a_real_metric" not in _series_names(client)
        assert len(_series_names(client)) > 10

    def test_the_services_are_distinguishable(self, client: httpx.Client) -> None:
        """`service.name` becomes `job`, and the worker spent three milestones
        calling itself the API."""
        jobs = {series["metric"].get("job") for series in _instant(client, "target_info")}
        assert {"threshold-api", "threshold-worker"} <= jobs, jobs

    def test_each_service_reports_under_a_stable_instance(self, client: httpx.Client) -> None:
        """`service.instance.id` becomes `instance`, and it has to be set.

        The SDK invents a random UUID when nothing sets it, so every restart
        would abandon every series and start new ones -- and two replicas with
        no instance id are silently merged into one series whose counter is the
        interleaving of both.
        """
        for series in _instant(client, "target_info"):
            instance = series["metric"].get("instance", "")
            assert instance, series["metric"]
            assert "-" not in instance, (
                f"instance {instance!r} looks like the SDK's random UUID rather than "
                "a stable identity; every restart would start a new time series"
            )

    def test_the_environment_is_a_label_and_not_only_on_target_info(
        self, client: httpx.Client
    ) -> None:
        """Resource attributes are not labels unless Prometheus is told to
        promote them, and `sum by (deployment_environment)` silently returns
        nothing when they are not."""
        result = _instant(client, "sum by (deployment_environment) (threshold_job_queue_depth)")
        assert result, "no queue depth reported at all"
        assert {s["metric"].get("deployment_environment") for s in result} == {"local"}

    def test_no_series_carries_a_forbidden_label(self, client: httpx.Client) -> None:
        """The registry's allowlist, checked where it finally matters.

        Everything up to here has been in-process. This is the database that
        would actually hold the cardinality explosion.
        """
        # Scoped to this application's series. An unscoped query also returns
        # the labels on Prometheus's own self-monitoring metrics, which carry a
        # `reason` among others -- a false positive that would make this test
        # fail for something nobody here wrote.
        response = client.get(
            f"{PROMETHEUS}/api/v1/labels",
            params={"match[]": _OURS},
        )
        response.raise_for_status()
        labels = set(response.json()["data"])
        assert labels, "no labels found at all, so this checks nothing"
        leaked = labels & {m.prometheus_label(name) for name in m.FORBIDDEN_LABELS}
        assert not leaked, f"unbounded labels reached Prometheus: {sorted(leaked)}"


class TestTheDashboardIsLoadedAndNotJustWritten:
    def test_grafana_provisioned_it(self, client: httpx.Client) -> None:
        response = client.get(f"{GRAFANA}/api/search", params={"type": "dash-db"})
        response.raise_for_status()
        found = {d["uid"]: d["title"] for d in response.json()}
        assert "threshold-overview" in found, found

    def test_both_datasources_resolve(self, client: httpx.Client) -> None:
        """The uids the dashboard JSON references are string contracts.

        A mismatch renders "Datasource not found" in every panel and fails
        nothing at provisioning time.
        """
        response = client.get(f"{GRAFANA}/api/datasources")
        response.raise_for_status()
        by_uid = {d["uid"]: d["type"] for d in response.json()}
        assert by_uid.get("prometheus") == "prometheus"
        assert by_uid.get("tempo") == "tempo"

    def test_every_panel_query_is_accepted_by_prometheus(self, client: httpx.Client) -> None:
        """Not that a panel has data -- that its query parses.

        A dashboard is the one artifact here with no compiler. A malformed
        expression is a red box seen by whoever opens the page, at the moment
        they most need the page.
        """
        dashboard = json.loads(
            (
                __import__("pathlib").Path(__file__).resolve().parents[2]
                / "infra/local/grafana/dashboards/threshold.json"
            ).read_text(encoding="utf-8")
        )
        rejected = []
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                # The dashboard variables are Grafana's, not Prometheus's.
                expression = target["expr"].replace("$__rate_interval", "1m")
                expression = expression.replace("$__range", "1h")
                response = client.get(f"{PROMETHEUS}/api/v1/query", params={"query": expression})
                if response.status_code != 200:
                    rejected.append((panel["title"], response.json().get("error")))
        assert not rejected, f"Prometheus rejected these panel queries: {rejected}"


def _services_in(trace: dict) -> set[str]:
    return {
        attribute["value"]["stringValue"]
        for batch in trace.get("batches", [])
        for attribute in batch["resource"]["attributes"]
        if attribute["key"] == "service.name"
    }


def _span_names_in(trace: dict) -> set[str]:
    return {
        span["name"]
        for batch in trace.get("batches", [])
        for scope in batch["scopeSpans"]
        for span in scope["spans"]
    }


def _trace_from_tempo(
    client: httpx.Client,
    trace_id: str,
    *,
    until: Callable[[dict], bool],
    describe: str,
    within: float = 20.0,
) -> dict:
    """Ask Tempo for a trace, allowing for the fact that it ingests asynchronously.

    This is not a sleep hiding a race. Tempo batches spans and flushes on an
    interval, so a trace id committed to Postgres at T is queryable at T plus
    some delta the exporter and the backend agree on between themselves. The
    property under test is that the id *resolves* -- that an audit row is a
    working handle on the whole story -- not that it resolves within one round
    trip.

    The old version asked once, for the newest id in the table, which is the id
    least likely to have been flushed. It passed whenever the suite happened to
    run a few seconds after the last write and failed otherwise, which is the
    worst behaviour a test can have: it taught people that a red run means
    nothing.

    The caller supplies the condition, and that is the part worth insisting on.
    Waiting for "any batch at all" and then asserting on the contents just moves
    the race: the API flushes before the worker does, so a trace that exists but
    has not finished arriving satisfies the wait and fails the assertion. That is
    exactly how this test failed -- "the trace does not cross the queue; it only
    touched {'threshold-api'}" -- and a naive retry would have left it failing
    the same way, less often, which is worse.

    The bound matters as much as the retry. A test that waits forever cannot
    fail, and the message distinguishes "Tempo never got it" from "Tempo was
    slow", so a genuine break still reads as one.
    """
    deadline = time.monotonic() + within
    trace: dict = {}
    last = "Tempo was never asked"
    while True:
        response = client.get(f"{TEMPO}/api/traces/{urllib.parse.quote(trace_id)}")
        if response.status_code == 200:
            trace = response.json()
            if until(trace):
                return trace
            last = f"has {sorted(_services_in(trace)) or 'no batches'}"
        else:
            last = f"Tempo answered {response.status_code}"
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"after {within:.0f}s Tempo still does not have {describe} for trace "
                f"{trace_id}, which an audit row claims to belong to: it {last}"
            )
        time.sleep(0.5)


class TestOneInvoiceOneTraceThroughARealBackend:
    def test_an_audit_rows_trace_id_resolves_in_tempo(self, client: httpx.Client) -> None:
        """The M5 promise, end to end, through a real exporter and a real store.

        `test_trace_continuity.py` proves the four audit rows share one id. This
        proves that id is worth something: it is the handle that turns a row in
        Postgres into the whole story, across both processes.
        """
        trace_id = _newest_trace_id()
        if trace_id is None:
            pytest.skip("no audit rows with a trace id yet; run `make smoke`")

        both = {"threshold-api", "threshold-worker"}
        trace = _trace_from_tempo(
            client,
            trace_id,
            until=lambda payload: both <= _services_in(payload),
            describe="spans from both services",
        )
        assert both <= _services_in(trace)

    def test_the_trace_contains_the_work_and_not_only_the_request(
        self, client: httpx.Client
    ) -> None:
        """The SQL spans specifically.

        Until D-023 the service emitted a `connect` span and nothing else,
        because the global SQLAlchemy instrumentation replaced a function name
        `adapters/db.py` had already imported. A trace with HTTP and queue spans
        but no statements is what that failure looks like from here.
        """
        trace_id = _newest_trace_id()
        if trace_id is None:
            pytest.skip("no audit rows with a trace id yet; run `make smoke`")

        trace = _trace_from_tempo(
            client,
            trace_id,
            until=lambda payload: any(
                name.startswith("process ") for name in _span_names_in(payload)
            ),
            describe="a worker span",
        )
        names = _span_names_in(trace)
        assert any(n.startswith("process ") for n in names), f"no worker span: {names}"
        assert {"SELECT", "INSERT"} & names, (
            f"no SQL spans; the engine is not instrumented. Spans were: {names}"
        )


def _newest_trace_id() -> str | None:
    """The most recent audit row's trace id, straight from Postgres."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def read() -> str | None:
        engine = create_async_engine(
            "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold"
        )
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "select trace_id from audit_event "
                        "where trace_id is not null order by occurred_at desc limit 1"
                    )
                )
                row = result.first()
                return None if row is None else str(row[0])
        finally:
            await engine.dispose()

    return asyncio.run(read())
