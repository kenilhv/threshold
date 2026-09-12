"""Run `promtool test rules` against the alert rules.

Prometheus's own binary, because the rules are PromQL and only Prometheus can
say what PromQL means. `tests/unit/test_alerts.py` checks everything around the
rules -- the registry agreeing with them, the runbooks existing, every rule
tested both ways -- but it cannot evaluate an expression.

Run through Docker rather than asking for promtool on the PATH: the version that
evaluates the rules in `make verify` is then the same version that evaluates
them in the stack, which for a tool whose output is "did this alert fire" is
worth the container start.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

# Pinned to the Prometheus that runs in docker-compose. A rule that passes
# under one version and fires differently under another is the exact surprise
# this check exists to prevent.
IMAGE = "prom/prometheus:v3.14.0"
RULES_DIR = pathlib.Path(__file__).resolve().parents[1] / "infra/local/prometheus"


def main() -> int:
    if shutil.which("docker") is None:
        # Deliberately a failure rather than a skip. A gate that quietly does
        # not run is worse than one that is not there, because it reports green.
        print(
            f"docker is required to evaluate the alert rules ({IMAGE} provides promtool)",
            file=sys.stderr,
        )
        return 1

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "promtool",
            "-v",
            f"{RULES_DIR.as_posix()}:/rules:ro",
            "-w",
            "/rules",
            IMAGE,
            "test",
            "rules",
            "alerts_test.yml",
        ],
        capture_output=True,
        text=True,
        # Git Bash rewrites /rules into a Windows path before Docker sees it.
        env={**_environ(), "MSYS_NO_PATHCONV": "1"},
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
