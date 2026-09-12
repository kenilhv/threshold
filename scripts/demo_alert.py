"""Stop the worker and watch an alert notice.

The demo is `QueueObserverStopped`, which is the alert the metric design exists
to make possible. Queue depth is published by exactly one worker; an empty queue
reports zero rather than nothing, and the snapshot expires after ninety seconds.
So absence means one thing — nobody is watching the queue — and because metrics
are pushed rather than scraped, there is no `up` series and this is the entire
mechanism by which a dead worker is noticed.

It takes about seven minutes, and the wait is worth understanding. A pushed
series does not become absent when the pusher stops; it becomes absent when
Prometheus's five-minute lookback passes the last sample. Only then does the
rule's own `for: 2m` begin. So the run below shows a long flat stretch, then
`pending`, then `firing` two minutes later -- and that shape, not the total, is
what tells you where the latency actually lives.

The worker is restarted on the way out, including on Ctrl-C.
"""

from __future__ import annotations

import subprocess
import sys
import time

import httpx

PROMETHEUS = "http://localhost:9090"
ALERT = "QueueObserverStopped"
BUDGET_SECONDS = 600
POLL_SECONDS = 10


def rule_state(alert: str) -> tuple[str, float]:
    """`inactive`/`pending`/`firing`, and the rule's `for`, from Prometheus.

    The duration is read rather than written down here, so this narration cannot
    drift away from the rule -- which it already did once, while the rule was
    being corrected from five minutes to two.
    """
    response = httpx.get(f"{PROMETHEUS}/api/v1/rules", timeout=10)
    response.raise_for_status()
    for group in response.json()["data"]["groups"]:
        for rule in group["rules"]:
            if rule.get("name") == alert:
                return str(rule.get("state", "unknown")), float(rule.get("duration", 0))
    return "missing", 0.0


def state_of(alert: str) -> str:
    return rule_state(alert)[0]


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


def main() -> int:
    try:
        before = state_of(ALERT)
    except httpx.HTTPError as exc:
        print(f"Prometheus is not reachable: {exc}. Try `make up`.", file=sys.stderr)
        return 1

    if before != "inactive":
        print(f"{ALERT} is already {before}; nothing to demonstrate. Is the worker running?")
        return 1

    print(f"{ALERT} is inactive. Stopping the worker.\n")
    compose("stop", "worker")

    seen = {"inactive"}
    started = time.monotonic()
    try:
        while time.monotonic() - started < BUDGET_SECONDS:
            elapsed = int(time.monotonic() - started)
            state = state_of(ALERT)
            if state not in seen:
                seen.add(state)
                print(f"  t+{elapsed:>3}s  {state.upper()}")
                if state == "pending":
                    waits = rule_state(ALERT)[1]
                    print(
                        "         Prometheus agrees the expression is true. It will not\n"
                        f"         fire until it stays true for the rule's for: {waits:.0f}s.\n"
                        "         Note how long that took: the series did not go absent\n"
                        "         when the worker stopped, but when Prometheus's\n"
                        "         five-minute lookback passed its last sample."
                    )
            elif elapsed % 60 == 0:
                print(f"  t+{elapsed:>3}s  still {state}")
            if state == "firing":
                break
            time.sleep(POLL_SECONDS)
        else:
            print(f"\n{ALERT} never fired within {BUDGET_SECONDS}s.", file=sys.stderr)
            return 1

        alerts = httpx.get(f"{PROMETHEUS}/api/v1/alerts", timeout=10).json()["data"]["alerts"]
        firing = next(a for a in alerts if a["labels"]["alertname"] == ALERT)
        print("\n  summary  :", firing["annotations"]["summary"])
        print("  severity :", firing["labels"]["severity"])
        print("  runbook  :", firing["annotations"]["runbook_url"])
        print("\n  " + firing["annotations"]["description"].strip())
    finally:
        print("\nRestarting the worker.")
        compose("start", "worker")
        print("Nothing was lost: a claim is a state change, not a delete, so every")
        print("job the stopped worker held is still in the table and the reaper")
        print("returns it once a live worker takes the lock.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
