"""Every table the ORM knows has a line in the privilege policy.

`tests/integration/test_least_privilege.py` compares the policy with a running
database, which is the comparison that matters and the one CI cannot make:
`make verify` has no Postgres. So the failure it exists to catch -- somebody adds
a table and nobody decides what the application may do to it -- would first
appear on a developer's machine, as `permission denied for table` from a worker.

This is the half of that check that needs no database. The models are the
schema's other description, and a `__tablename__` absent from the policy is the
same omission seen from the other side.
"""

from __future__ import annotations

from tests.integration.test_least_privilege import EXPECTED, MUTABLE_COLUMNS

from threshold.domain.models import Base


def _tables() -> dict[str, set[str]]:
    return {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.tables.values()
    }


def test_every_model_is_classified() -> None:
    unclassified = sorted(_tables().keys() - EXPECTED.keys())
    assert not unclassified, (
        f"{unclassified}: no privilege policy. Grant in the migration that creates the "
        "table, then add it to EXPECTED in tests/integration/test_least_privilege.py."
    )


def test_the_policy_describes_tables_that_exist() -> None:
    # alembic_version is Alembic's, not a model; it is in the policy so that the
    # integration test can insist the application holds nothing on it.
    stale = sorted(EXPECTED.keys() - _tables().keys() - {"alembic_version"})
    assert not stale, f"the policy names tables no model declares: {stale}"


def test_the_mutable_columns_are_real_columns() -> None:
    tables = _tables()
    for table, columns in MUTABLE_COLUMNS.items():
        assert columns <= tables[table], f"{table}: {sorted(columns - tables[table])}"


def test_a_table_with_mutable_columns_has_no_table_wide_update() -> None:
    """The two would contradict each other, and Postgres would resolve it in the
    permissive direction: a table-level UPDATE makes every column grant moot."""
    for table in MUTABLE_COLUMNS:
        for role, held in EXPECTED[table].items():
            assert "UPDATE" not in held, f"{role} on {table}"


def test_the_record_is_never_updatable() -> None:
    """The invariant behind the map, so that an edit to the map cannot quietly
    drop it: nothing in this schema is UPDATE-able or DELETE-able table-wide
    except the purge of finished jobs."""
    for table, by_role in EXPECTED.items():
        for role, held in by_role.items():
            assert "UPDATE" not in held, f"{role} on {table}"
            assert "TRUNCATE" not in held, f"{role} on {table}"
            if table != "job":
                assert "DELETE" not in held, f"{role} on {table}"


def test_the_two_roles_are_not_the_same_role() -> None:
    """The vacuity guard for the split. If every table granted both roles the
    same thing, the increment would be two names for one identity."""
    differing = {table for table, by_role in EXPECTED.items() if len(set(by_role.values())) > 1}
    assert len(differing) >= 8, sorted(differing)
