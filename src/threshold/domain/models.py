"""ORM models.

Conventions that hold across every table here:

* Money is ``NUMERIC(18, 4)`` and reaches Python as ``Decimal``. There is no
  float in any monetary path, and a test asserts a JSON round trip loses no
  precision.
* Timestamps are ``TIMESTAMPTZ`` and are set by the database, not the app, so
  two workers on skewed clocks cannot disagree about ordering.
* Every column carries a sensitivity classification. See ``classification.py``
  for why undeclared means restricted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from threshold.domain.classification import Sensitivity, classify


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        info=classify(Sensitivity.PUBLIC),
    )


class Vendor(Base):
    """A supplier. The root entity — an invoice without a resolved vendor cannot
    be matched, classified or paid, so vendor resolution is the first real
    decision the pipeline makes."""

    __tablename__ = "vendor"

    id: Mapped[uuid.UUID] = _pk()

    # INTERNAL rather than PUBLIC: the vendor's identity is exactly what the
    # review projection pseudonymises, because knowing who is billing is enough
    # to infer a company's suppliers, costs and negotiating position.
    legal_name: Mapped[str] = mapped_column(
        String(300), nullable=False, info=classify(Sensitivity.INTERNAL)
    )
    display_name: Mapped[str] = mapped_column(
        String(300), nullable=False, info=classify(Sensitivity.INTERNAL)
    )

    # Stable, human-meaningless supplier number. Safe to show a reviewer because
    # it identifies a row, not a company.
    vendor_number: Mapped[str] = mapped_column(
        String(40), nullable=False, unique=True, info=classify(Sensitivity.PUBLIC)
    )

    country_code: Mapped[str] = mapped_column(
        String(2), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    default_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    tax_id: Mapped[str | None] = mapped_column(
        String(60), nullable=True, info=classify(Sensitivity.RESTRICTED)
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active", info=classify(Sensitivity.PUBLIC)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    bank_accounts: Mapped[list[VendorBankAccount]] = relationship(
        back_populates="vendor", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("length(country_code) = 2", name="ck_vendor_country_iso3166"),
        CheckConstraint("length(default_currency) = 3", name="ck_vendor_currency_iso4217"),
        CheckConstraint("status in ('active', 'on_hold', 'inactive')", name="ck_vendor_status"),
        Index("ix_vendor_display_name_lower", func.lower(display_name)),
    )


class VendorBankAccount(Base):
    """Banking details, kept as history rather than overwritten.

    A vendor bank change is the highest-severity exception this system knows
    about — it is the shape almost every real AP fraud takes. Keeping prior rows
    is what lets the pipeline say *changed since the last payment* rather than
    just reporting the current value.
    """

    __tablename__ = "vendor_bank_account"

    id: Mapped[uuid.UUID] = _pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("vendor.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    # Dropped entirely from the review projection. Never enters a model context.
    account_number_last4: Mapped[str] = mapped_column(
        String(4), nullable=False, info=classify(Sensitivity.RESTRICTED)
    )
    routing_number_last4: Mapped[str] = mapped_column(
        String(4), nullable=False, info=classify(Sensitivity.RESTRICTED)
    )
    account_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        info=classify(Sensitivity.RESTRICTED),
        comment="HMAC of the full account details. Comparable, not reversible.",
    )

    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    vendor: Mapped[Vendor] = relationship(back_populates="bank_accounts")

    __table_args__ = (
        UniqueConstraint(
            "vendor_id",
            "account_fingerprint",
            "effective_from",
            name="uq_vendor_bank_account_version",
        ),
        Index(
            "ix_vendor_bank_account_current",
            "vendor_id",
            postgresql_where=effective_to.is_(None),
        ),
    )


class AuditEvent(Base):
    """Append-only. There is no update path and no delete path in the ORM.

    Every state transition in the system writes one of these in the same
    transaction as the transition itself, so the audit trail cannot disagree
    with the state it describes. ``trace_id`` is the same id as the distributed
    trace, which means "who approved this and what did the system know" and "why
    was this slow" answer from the same row.
    """

    __tablename__ = "audit_event"

    id: Mapped[uuid.UUID] = _pk()

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    actor_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True, info=classify(Sensitivity.INTERNAL)
    )

    action: Mapped[str] = mapped_column(
        String(100), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    entity_type: Mapped[str] = mapped_column(
        String(60), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    before: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True, info=classify(Sensitivity.INTERNAL)
    )
    after: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True, info=classify(Sensitivity.INTERNAL)
    )
    reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, info=classify(Sensitivity.INTERNAL)
    )

    trace_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    __table_args__ = (
        CheckConstraint("actor_type in ('human', 'system', 'agent')", name="ck_audit_actor_type"),
        Index("ix_audit_event_entity", "entity_type", "entity_id", "occurred_at"),
        Index("ix_audit_event_trace", "trace_id"),
    )


class MoneyExample(Base):
    """Temporary M0 table proving the monetary conventions end to end.

    Replaced by the real invoice/ledger tables in M1-M3. It exists now so the
    ``NUMERIC`` round-trip test has something to run against from the first
    commit rather than being written after the fact.
    """

    __tablename__ = "money_example"

    id: Mapped[uuid.UUID] = _pk()
    amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, info=classify(Sensitivity.INTERNAL)
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    __table_args__ = (CheckConstraint("length(currency) = 3", name="ck_money_example_currency"),)
