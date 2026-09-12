"""The alert rules, checked against the registry that names them and the
runbooks they send someone to at three in the morning.

`promtool test rules` already proves each rule fires when it should and stays
quiet when it should not — that is `infra/local/prometheus/alerts_test.yml`, and
it needs Prometheus's own binary. What it cannot check is everything around the
rules: that the metric registry and the rules agree about which alerts exist,
that every rule links to a runbook, and that the runbook is a file rather than a
plausible path.

A dead runbook link is a specific and nasty failure. It is discovered by the one
person least able to cope with it: someone paged at 3am, following the link
because they do not already know the answer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from threshold.observability import metrics as m

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "infra/local/prometheus/alerts.yml"
RULE_TESTS = ROOT / "infra/local/prometheus/alerts_test.yml"

SEVERITIES = {"page", "ticket"}


@pytest.fixture(scope="module")
def rules() -> list[dict[str, Any]]:
    assert RULES.exists(), f"the alert rules are missing: {RULES}"
    document = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return [rule for group in document["groups"] for rule in group["rules"]]


@pytest.fixture(scope="module")
def rule_tests() -> list[dict[str, Any]]:
    document = yaml.safe_load(RULE_TESTS.read_text(encoding="utf-8"))
    return list(document["tests"])


def _names(rules: list[dict[str, Any]]) -> set[str]:
    return {rule["alert"] for rule in rules}


class TestEveryRuleIsUsable:
    def test_each_rule_names_a_severity_that_means_something(
        self, rules: list[dict[str, Any]]
    ) -> None:
        """Two values, and they answer different questions: wake someone now, or
        look at it in business hours. A third would mean nobody knows which."""
        for rule in rules:
            severity = rule["labels"]["severity"]
            assert severity in SEVERITIES, f"{rule['alert']}: {severity}"

    def test_each_rule_waits_before_firing(self, rules: list[dict[str, Any]]) -> None:
        """A rule with no `for` fires on a single evaluation.

        Every one of these reads a rate or a gauge that moves, so an instant
        rule turns ordinary jitter into a page.
        """
        for rule in rules:
            assert rule.get("for"), f"{rule['alert']} has no `for` duration"

    def test_each_rule_says_what_happened_and_what_to_do(self, rules: list[dict[str, Any]]) -> None:
        for rule in rules:
            annotations = rule.get("annotations", {})
            assert annotations.get("summary"), f"{rule['alert']} has no summary"
            assert annotations.get("description"), f"{rule['alert']} has no description"

    def test_each_rule_links_to_a_runbook_that_exists(self, rules: list[dict[str, Any]]) -> None:
        """The assertion this file exists for.

        A path that looks right and resolves to nothing is discovered by
        whoever was paged, at the moment they least want to discover it.
        """
        missing = []
        for rule in rules:
            url = rule.get("annotations", {}).get("runbook_url")
            if not url:
                missing.append(f"{rule['alert']}: no runbook_url")
            elif not (ROOT / url).is_file():
                missing.append(f"{rule['alert']}: {url} does not exist")
        assert not missing, missing

    def test_the_runbook_check_can_fail(self) -> None:
        """The guard for the guard."""
        assert not (ROOT / "docs/runbooks/no-such-runbook.md").is_file()

    def test_every_runbook_is_reached_by_some_rule(self, rules: list[dict[str, Any]]) -> None:
        """A runbook nobody links to is a document nobody will find."""
        linked = {rule["annotations"]["runbook_url"] for rule in rules}
        on_disk = {f"docs/runbooks/{path.name}" for path in (ROOT / "docs/runbooks").glob("*.md")}
        assert on_disk - linked == set(), f"unreferenced runbooks: {sorted(on_disk - linked)}"

    def test_each_runbook_names_the_alerts_it_serves(self, rules: list[dict[str, Any]]) -> None:
        """Someone arriving from a page must see their alert named at the top,
        or they cannot tell whether they are in the right document."""
        for rule in rules:
            text = (ROOT / rule["annotations"]["runbook_url"]).read_text(encoding="utf-8")
            assert rule["alert"] in text, (
                f"{rule['annotations']['runbook_url']} never mentions {rule['alert']}"
            )


class TestTheRegistryAndTheRulesAgree:
    def test_every_declared_alert_exists(self, rules: list[dict[str, Any]]) -> None:
        """The registry's side. Same contract the dashboard panels have."""
        defined = _names(rules)
        missing = {
            spec.name: [a for a in spec.alerts if a not in defined]
            for spec in m.REGISTRY
            if any(a not in defined for a in spec.alerts)
        }
        assert not missing, f"metrics naming alert rules that do not exist: {missing}"

    def test_every_rule_is_claimed_by_a_metric(self, rules: list[dict[str, Any]]) -> None:
        """The rules' side. An alert no metric claims is one nobody maintains,
        and the first sign of it is a rule quietly evaluating to nothing."""
        claimed = {alert for spec in m.REGISTRY for alert in spec.alerts}
        unclaimed = _names(rules) - claimed
        assert not unclaimed, f"alert rules no metric in REGISTRY claims: {sorted(unclaimed)}"

    def test_every_rule_queries_a_declared_metric(self, rules: list[dict[str, Any]]) -> None:
        """The expression has to reference a series the registry accounts for.

        Catches the rule that outlived its metric — which evaluates forever
        without ever firing, and looks exactly like a rule that is working.
        """
        known: set[str] = set()
        for spec in m.REGISTRY:
            known.add(spec.prometheus_name)
            known.update(f"{spec.prometheus_name}{part}" for part in ("_bucket", "_count", "_sum"))

        for rule in rules:
            referenced = set(re.findall(r"\b(?:threshold|http_server|db_client)\w*", rule["expr"]))
            assert referenced, f"{rule['alert']} references no metric at all"
            unknown = referenced - known
            assert not unknown, f"{rule['alert']} queries undeclared series: {sorted(unknown)}"


class TestEveryRuleIsTestedBothWays:
    """The paired-negative contract.

    A rule only ever seen to fire is a rule whose quiet behaviour nobody has
    checked, and alerting does not die from a missed page — it dies from a rule
    that fires during normal operation until people stop reading it.
    """

    def test_each_rule_has_a_test_that_makes_it_fire(
        self, rules: list[dict[str, Any]], rule_tests: list[dict[str, Any]]
    ) -> None:
        firing = {
            case["alertname"]
            for test in rule_tests
            for case in test["alert_rule_test"]
            if case.get("exp_alerts")
        }
        assert _names(rules) <= firing, f"never proved to fire: {sorted(_names(rules) - firing)}"

    def test_each_rule_has_a_test_that_keeps_it_quiet(
        self, rules: list[dict[str, Any]], rule_tests: list[dict[str, Any]]
    ) -> None:
        quiet = {
            case["alertname"]
            for test in rule_tests
            for case in test["alert_rule_test"]
            if case.get("exp_alerts") == []
        }
        assert _names(rules) <= quiet, (
            f"never proved to stay quiet: {sorted(_names(rules) - quiet)}"
        )

    def test_the_guarded_rules_are_tested_against_their_guard(
        self, rule_tests: list[dict[str, Any]]
    ) -> None:
        """Two rules carry a denominator guard, and both had a negative test
        that passed without it.

        Each had two flat counters, so the ratio was 0/0, no alert fired for
        reasons unrelated to the guard, and deleting the guard changed nothing.
        Found by mutating the rules and watching the tests not notice. The
        inputs must move, or the guard is not what is being tested.
        """
        for name in (
            "a quiet night does not page about arithmetic",
            "a nearly idle api where every request fails does not page",
        ):
            test = next(t for t in rule_tests if t["name"] == name)
            moving = [s for s in test["input_series"] if not re.search(r"\+0x|^\d+$", s["values"])]
            assert moving, f"{name!r} has no counter that moves; it cannot exercise a guard"
