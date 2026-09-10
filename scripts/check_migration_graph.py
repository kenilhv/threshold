"""Assert the migration history is linear, single-headed and reversible.

The database round trip -- upgrade, downgrade to base, upgrade again -- is the
stronger check and it needs Postgres, so it stays in `make migrate-roundtrip`.
This is the weaker property that can be checked with nothing running, which
makes it the one CI can enforce on every push:

* every revision file imports, so a syntax error or a bad import cannot reach a
  deploy disguised as a migration nobody ran yet;
* the graph has exactly one head, because a branched history does not fail at
  merge time -- it fails at deploy time, when `alembic upgrade head` finds two
  heads and refuses, usually during the deploy that was already going badly;
* every revision declares both `upgrade` and `downgrade`, since a revision that
  cannot reverse turns a bad release into a restore-from-backup.

Deliberately reads the scripts rather than a database, so it needs no
connection, no settings and no container.

    python scripts/check_migration_graph.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)

    problems: list[str] = []

    heads = scripts.get_heads()
    if len(heads) != 1:
        problems.append(
            f"the revision graph has {len(heads)} heads ({', '.join(sorted(heads))}). "
            "`alembic upgrade head` refuses to run against a branched history, and it "
            "refuses at deploy time rather than at merge time."
        )

    revisions = list(scripts.walk_revisions())
    if not revisions:
        problems.append("no revisions found; check script_location")

    for revision in revisions:
        module = revision.module
        for direction in ("upgrade", "downgrade"):
            if not callable(getattr(module, direction, None)):
                problems.append(f"{revision.revision} has no {direction}()")

    bases = scripts.get_bases()
    if len(bases) > 1:
        problems.append(f"more than one base revision: {', '.join(sorted(bases))}")

    if problems:
        for problem in problems:
            print(f"  FAIL  {problem}")
        return 1

    print(f"  OK    {len(revisions)} revisions, one head ({heads[0]}), all reversible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
