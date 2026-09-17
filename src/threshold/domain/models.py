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
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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

    # INTERNAL rather than PUBLIC, and an identity: knowing who is billing is
    # enough to infer a company's suppliers, costs and negotiating position.
    # Staff see these; a model is not shown them. Nothing reads a vendor row on
    # the agent's behalf today, and the mark is here so that adding a tool which
    # does cannot quietly hand a model the supplier list.
    legal_name: Mapped[str] = mapped_column(
        String(300), nullable=False, info=classify(Sensitivity.INTERNAL, identity=True)
    )
    display_name: Mapped[str] = mapped_column(
        String(300), nullable=False, info=classify(Sensitivity.INTERNAL, identity=True)
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

    #: `clock_timestamp()`, not `now()`. Postgres's `now()` is the transaction's
    #: start time and does not advance within it, so two events written by one
    #: transaction -- `match.decided` and the `invoice.auto_approved` it causes,
    #: for instance -- shared a timestamp to the microsecond and could not be
    #: ordered against each other. In this table the order between them *is* the
    #: causality. See migration 0013.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
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


class Document(Base):
    """A file that arrived, stored once.

    ``content_sha256`` is unique. Two submissions of the same bytes are the same
    document no matter what they were called or who sent them, which is the
    cheapest of the three deduplication layers and the one that catches the most
    common real case: an accounts-payable inbox receiving the same PDF twice
    because a supplier resent it.

    The bytes live in object storage; this row holds only the pointer and the
    facts needed to find, verify and audit them.
    """

    __tablename__ = "document"

    id: Mapped[uuid.UUID] = _pk()

    content_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, info=classify(Sensitivity.PUBLIC)
    )

    #: Key in the object store. Derived from the hash, so the location is a pure
    #: function of the content and a re-upload cannot fork into two objects.
    storage_key: Mapped[str] = mapped_column(
        String(500), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    #: As supplied, by a stranger. INTERNAL because filenames routinely leak
    #: vendor names, amounts and project codes -- and an identity, so the agent
    #: never sees it. That is worth more than the privacy: this string is
    #: attacker-controlled, and it was one of the injection channels
    #: `test_agent_red_team.py` fires through. Withholding it removes the
    #: channel rather than defending it.
    original_filename: Mapped[str] = mapped_column(
        String(400), nullable=False, info=classify(Sensitivity.INTERNAL, identity=True)
    )
    content_type: Mapped[str] = mapped_column(
        String(120), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    byte_size: Mapped[int] = mapped_column(
        BigInteger, nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    page_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="upload", info=classify(Sensitivity.PUBLIC)
    )

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    extractions: Mapped[list[Extraction]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="Extraction.version"
    )

    __table_args__ = (
        CheckConstraint("byte_size > 0", name="ck_document_byte_size_positive"),
        CheckConstraint("length(content_sha256) = 64", name="ck_document_sha256_length"),
        CheckConstraint(
            "source in ('upload', 'email', 'api', 'fixture')", name="ck_document_source"
        ),
        CheckConstraint(
            "page_count is null or page_count > 0", name="ck_document_page_count_positive"
        ),
    )


class Extraction(Base):
    """One reading of one document. Immutable, versioned, never updated.

    Re-extracting inserts version N+1 rather than mutating N. That is what makes
    the evaluation harness possible — any historical document can be replayed
    against a new model or prompt and the two rows diffed — and it is what turns
    "the model changed and the numbers moved" from a mystery into a query.

    ``fields`` holds the full ``ExtractedInvoice`` as JSONB: every value with its
    own confidence, bounding box and the raw text it was parsed from.
    """

    __tablename__ = "extraction"

    id: Mapped[uuid.UUID] = _pk()

    document_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("document.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, info=classify(Sensitivity.PUBLIC))

    extractor_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    extractor_id: Mapped[str] = mapped_column(
        String(200), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    prompt_version: Mapped[str | None] = mapped_column(
        String(60), nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    fields: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, info=classify(Sensitivity.INTERNAL)
    )

    #: The minimum confidence across decision-relevant fields, denormalised so
    #: the review queue can filter on it without unpacking JSONB per row. A
    #: minimum, never a mean — see domain/extraction.py for why.
    lowest_decision_confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, info=classify(Sensitivity.PUBLIC)
    )
    output_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    document: Mapped[Document] = relationship(back_populates="extractions")

    __table_args__ = (
        # This unique constraint's index also serves lookups by (document_id)
        # and (document_id, version); a separate btree would be dead weight.
        UniqueConstraint("document_id", "version", name="uq_extraction_document_version"),
        CheckConstraint("version > 0", name="ck_extraction_version_positive"),
        CheckConstraint(
            "extractor_kind in ('deterministic', 'vision_model')",
            name="ck_extraction_extractor_kind",
        ),
        CheckConstraint(
            "lowest_decision_confidence >= 0 and lowest_decision_confidence <= 1",
            name="ck_extraction_confidence_is_a_probability",
        ),
        Index("ix_extraction_confidence", "lowest_decision_confidence"),
    )


class IdempotencyKey(Base):
    """Stored responses for replayed requests, following Stripe's published spec.

    Three behaviours, and the third is the one people miss:

    * Same key, same request  -> replay the stored response, do no work.
    * New key                 -> do the work, store the response.
    * Same key, DIFFERENT request -> 409. Reusing a key for different content is
      a client bug, and silently treating it as a replay would return the wrong
      resource with a 200 attached. Failing loudly is the safer error.

    The request fingerprint is what makes the third case detectable, which is why
    it is stored rather than just the key.
    """

    __tablename__ = "idempotency_key"

    id: Mapped[uuid.UUID] = _pk()

    key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, info=classify(Sensitivity.PUBLIC)
    )
    #: sha256 over method, path and body. Comparable without retaining the body.
    request_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    response_status: Mapped[int] = mapped_column(
        Integer, nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    response_body: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, info=classify(Sensitivity.INTERNAL)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    #: Stripe expires keys after 24 hours. Bounded retention keeps the table from
    #: growing without limit and matches what clients are told to expect.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    __table_args__ = (
        CheckConstraint("response_status between 100 and 599", name="ck_idempotency_status"),
        CheckConstraint("expires_at > created_at", name="ck_idempotency_expiry_after_creation"),
        Index("ix_idempotency_expires_at", "expires_at"),
    )


class Job(Base):
    """A unit of durable work.

    The queue is this table. Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED``
    so N workers poll concurrently without blocking each other and without ever
    handing the same row to two of them.

    ``dedupe_key`` is the second of three idempotency layers. The first is the
    HTTP ``Idempotency-Key``; this one stops at-least-once delivery from
    enqueueing the same work twice; the third is a unique constraint on the
    payment row in M8. Each catches something the others cannot.
    """

    __tablename__ = "job"

    id: Mapped[uuid.UUID] = _pk()

    kind: Mapped[str] = mapped_column(String(50), nullable=False, info=classify(Sensitivity.PUBLIC))
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, info=classify(Sensitivity.INTERNAL)
    )

    #: Nullable: not every job needs deduplication, and a partial unique index
    #: over the non-null values keeps the index small.
    dedupe_key: Mapped[str | None] = mapped_column(
        String(200), nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="pending",
        info=classify(Sensitivity.PUBLIC),
    )

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", info=classify(Sensitivity.PUBLIC)
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="3", info=classify(Sensitivity.PUBLIC)
    )

    #: Backoff is expressed as a future run time rather than a sleep, so a
    #: retry costs a worker nothing while it waits.
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    claimed_by: Mapped[str | None] = mapped_column(
        String(120), nullable=True, info=classify(Sensitivity.PUBLIC)
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, info=classify(Sensitivity.PUBLIC)
    )
    #: The heart of crash recovery. A worker that dies stops renewing this, and
    #: the reaper returns the job to the queue once it passes.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True, info=classify(Sensitivity.INTERNAL)
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

    __table_args__ = (
        CheckConstraint(
            "state in ('pending', 'claimed', 'succeeded', 'failed', 'dead')",
            name="ck_job_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_job_attempts_non_negative"),
        CheckConstraint("max_attempts > 0", name="ck_job_max_attempts_positive"),
        CheckConstraint(
            "(state = 'claimed') = (lease_expires_at is not null)",
            name="ck_job_claimed_iff_leased",
        ),
        # Partial by design — see migration 0003 for why each one is scoped.
        Index(
            "ix_job_claimable",
            "run_after",
            "created_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index(
            "ix_job_lease",
            "lease_expires_at",
            postgresql_where=text("state = 'claimed'"),
        ),
        Index(
            "uq_job_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND state IN ('pending', 'claimed')"),
        ),
    )


class DeadLetter(Base):
    """A job that exhausted its attempts, kept with everything needed to replay.

    Separate from ``job`` rather than a terminal state on it, for one reason:
    the queue's hot path filters on ``state = 'pending'`` and a growing tail of
    permanently failed rows makes that index progressively worse. Moving them
    out keeps the working set proportional to live work.
    """

    __tablename__ = "dead_letter"

    id: Mapped[uuid.UUID] = _pk()

    original_job_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False, info=classify(Sensitivity.PUBLIC))
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, info=classify(Sensitivity.INTERNAL)
    )
    dedupe_key: Mapped[str | None] = mapped_column(
        String(200), nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    final_error: Mapped[str] = mapped_column(
        Text, nullable=False, info=classify(Sensitivity.INTERNAL)
    )
    #: Whether the failure was classified transient or permanent. A tail of
    #: permanent failures is a bug in the pipeline; a tail of transient ones is
    #: a dependency that needs attention. Different problems, different pages.
    failure_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    died_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    replayed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    __table_args__ = (
        CheckConstraint(
            "failure_kind in ('transient', 'permanent')", name="ck_dead_letter_failure_kind"
        ),
        Index("ix_dead_letter_unreplayed", "died_at", postgresql_where=replayed_at.is_(None)),
    )


# --------------------------------------------------------------- matching ---
# What was ordered, what arrived, and what the matcher concluded. These four
# tables are the other two legs of the three-way match: without them an invoice
# can only ever be checked against itself.


class PurchaseOrder(Base):
    """What was agreed. The reference the invoice is judged against.

    ``po_number`` is unique because it is the join key printed on the invoice.
    If two rows could share it, an invoice citing that number would match
    whichever the query returned first, and the wrong agreement is worse than no
    agreement -- it produces a confident approval against terms nobody signed.
    """

    __tablename__ = "purchase_order"

    id: Mapped[uuid.UUID] = _pk()

    po_number: Mapped[str] = mapped_column(
        String(60), nullable=False, unique=True, info=classify(Sensitivity.PUBLIC)
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("vendor.id", ondelete="RESTRICT"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="open", info=classify(Sensitivity.PUBLIC)
    )

    #: Who to route a price or quantity dispute to. INTERNAL rather than PUBLIC:
    #: it identifies an employee, which is also why the agent is not shown it.
    #: Routing is by role (`domain/taxonomy.py`), never by this address, so
    #: withholding it costs the triager nothing.
    buyer_email: Mapped[str | None] = mapped_column(
        String(320), nullable=True, info=classify(Sensitivity.INTERNAL, identity=True)
    )

    ordered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        order_by="PurchaseOrderLine.line_number",
    )
    receipts: Mapped[list[GoodsReceipt]] = relationship(back_populates="purchase_order")

    __table_args__ = (
        CheckConstraint("length(currency) = 3", name="ck_po_currency_iso4217"),
        CheckConstraint("status in ('open', 'closed', 'cancelled')", name="ck_po_status"),
        Index("ix_purchase_order_vendor", "vendor_id"),
    )


class PurchaseOrderLine(Base):
    """One agreed item, at one agreed price."""

    __tablename__ = "purchase_order_line"

    id: Mapped[uuid.UUID] = _pk()

    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("purchase_order.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    line_number: Mapped[int] = mapped_column(
        Integer, nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    #: The authoritative pairing key. Descriptions are a fallback because they
    #: are written by whoever raised the order.
    sku: Mapped[str | None] = mapped_column(
        String(80), nullable=True, info=classify(Sensitivity.INTERNAL)
    )
    #: INTERNAL: what a company buys reveals what it is building.
    description: Mapped[str] = mapped_column(
        Text, nullable=False, info=classify(Sensitivity.INTERNAL)
    )

    quantity_ordered: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, info=classify(Sensitivity.INTERNAL)
    )
    #: INTERNAL rather than PUBLIC: a unit price is a negotiated term, and the
    #: set of them across a vendor is that vendor's price list.
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, info=classify(Sensitivity.INTERNAL)
    )

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")

    __table_args__ = (
        UniqueConstraint("purchase_order_id", "line_number", name="uq_po_line_number"),
        CheckConstraint("quantity_ordered > 0", name="ck_po_line_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_po_line_price_non_negative"),
        CheckConstraint("line_number > 0", name="ck_po_line_number_positive"),
    )


class GoodsReceipt(Base):
    """What actually arrived, and when. The leg that makes the match three-way.

    Kept as separate rows per delivery rather than a running total on the order
    line. Partial deliveries are normal, and a total that is overwritten cannot
    answer *when* the goods arrived -- which is the question asked when an
    invoice is billed before its receipt.
    """

    __tablename__ = "goods_receipt"

    id: Mapped[uuid.UUID] = _pk()

    receipt_number: Mapped[str] = mapped_column(
        String(60), nullable=False, unique=True, info=classify(Sensitivity.PUBLIC)
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("purchase_order.id", ondelete="RESTRICT"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    received_by: Mapped[str | None] = mapped_column(
        String(320), nullable=True, info=classify(Sensitivity.INTERNAL)
    )

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="receipts")
    lines: Mapped[list[GoodsReceiptLine]] = relationship(
        back_populates="goods_receipt", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_goods_receipt_po", "purchase_order_id"),)


class GoodsReceiptLine(Base):
    """How much of one ordered line arrived in one delivery."""

    __tablename__ = "goods_receipt_line"

    id: Mapped[uuid.UUID] = _pk()

    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("goods_receipt.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    purchase_order_line_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("purchase_order_line.id", ondelete="RESTRICT"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    #: Signed. A return or correction is a negative receipt rather than an edit
    #: to the original row, so the delivery history stays append-only and the
    #: aggregate still lands on the right number.
    quantity_received: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, info=classify(Sensitivity.INTERNAL)
    )

    goods_receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")

    __table_args__ = (
        UniqueConstraint(
            "goods_receipt_id", "purchase_order_line_id", name="uq_receipt_line_once_per_receipt"
        ),
        Index("ix_goods_receipt_line_po_line", "purchase_order_line_id"),
    )


class MatchResult(Base):
    """What the matcher concluded, and on what evidence. Immutable, versioned.

    Re-matching inserts version N+1 rather than updating N, for the same reason
    extractions are versioned: the useful question is not only "what do we think
    now" but "what did we think then, and why did it change". A receipt
    arriving, a purchase order being corrected, or the tolerance policy moving
    all legitimately change the answer, and each of those should leave a row
    that can be diffed against the one before it.

    ``extraction_id`` and ``policy_version`` together are what make the
    conclusion reproducible: the same reading, judged under the same thresholds,
    must produce the same disposition.
    """

    __tablename__ = "match_result"

    id: Mapped[uuid.UUID] = _pk()

    document_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("document.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("extraction.id", ondelete="RESTRICT"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    #: Null when no purchase order was cited or none was found. Both are
    #: recorded as exceptions; the null is the evidence, not the explanation.
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("purchase_order.id", ondelete="RESTRICT"),
        nullable=True,
        info=classify(Sensitivity.PUBLIC),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, info=classify(Sensitivity.PUBLIC))
    disposition: Mapped[str] = mapped_column(
        String(20), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    policy_version: Mapped[str] = mapped_column(
        String(40), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    #: What this decision is about, in integer minor units.
    #:
    #: Added at M8, and its absence until then was a real gap rather than an
    #: oversight worth shrugging at: this schema could say an invoice was
    #: blocked and could not say for how much. "How much is held up right now"
    #: is the first question an AP manager asks and it was unanswerable without
    #: parsing a JSON blob.
    #:
    #: Nullable, because a match result whose total could not be read must still
    #: exist -- that is precisely the invoice that raised exceptions. A payment
    #: refuses to be created against a null, which is the correct place for that
    #: refusal to live.
    #:
    #: The exponent is stored alongside for the reason `policy_version` is:
    #: `ledger.MINOR_UNITS` is a present-tense table, and reading a historical
    #: row through a corrected entry in it would move the decimal point.
    amount_minor: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, info=classify(Sensitivity.PUBLIC)
    )
    #: From the page, not from the purchase order. `CURRENCY_MISMATCH` exists
    #: precisely because those two can disagree, and the amount a supplier
    #: billed is denominated in what they printed.
    currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, info=classify(Sensitivity.PUBLIC)
    )
    currency_exponent: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    #: Which invoice line the matcher believed each purchase-order line to be.
    #: Stored so a wrong pairing is visible directly rather than inferred from
    #: the exceptions it caused.
    line_pairs: Mapped[list[dict[str, int]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        info=classify(Sensitivity.INTERNAL),
    )
    #: Checks that were not run, and why. A skipped check recorded as an absence
    #: is indistinguishable from a check that passed.
    skipped_checks: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        info=classify(Sensitivity.INTERNAL),
    )

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    exceptions: Mapped[list[MatchException]] = relationship(
        back_populates="match_result", cascade="all, delete-orphan"
    )
    decisions: Mapped[list[ApprovalDecision]] = relationship(
        back_populates="match_result",
        cascade="all, delete-orphan",
        order_by="ApprovalDecision.decided_at",
    )

    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_match_result_version"),
        CheckConstraint(
            "disposition in ('auto_approve', 'needs_review', 'blocked')",
            name="ck_match_result_disposition",
        ),
        CheckConstraint("version > 0", name="ck_match_result_version_positive"),
        # The three money columns arrive together or not at all. A currency with
        # no amount, or an amount with no currency, is a number nobody can spend
        # and the sort of half-written row a nullable column invites.
        CheckConstraint(
            "num_nonnulls(amount_minor, currency, currency_exponent) IN (0, 3)",
            name="ck_match_result_amount_is_whole",
        ),
        CheckConstraint(
            "amount_minor IS NULL OR amount_minor >= 0",
            name="ck_match_result_amount_not_negative",
        ),
        # Three uppercase letters, enforced rather than assumed. This column is
        # read off a document the supplier controls and is one of the few
        # supplier-influenced strings the triage agent can see, so the question
        # "could an instruction be written into it" needs a structural answer
        # rather than an argument about how short three characters is.
        CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="ck_match_result_currency_code",
        ),
        Index("ix_match_result_document", "document_id", "version"),
    )


class MatchException(Base):
    """One reason an invoice did not pass, with its owner and its deadline.

    ``owner_role`` and ``due_at`` are written here rather than looked up from
    the taxonomy at read time, and both for the same reason: this row is the
    record of a decision made at a moment. Routing rules change, and a report
    that re-derives ownership from today's table would silently restate who was
    responsible for an exception raised last quarter. ``due_at`` is stamped
    because an SLA measured from ``now()`` is a deadline that never arrives.
    """

    __tablename__ = "match_exception"

    id: Mapped[uuid.UUID] = _pk()

    match_result_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("match_result.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    code: Mapped[str] = mapped_column(String(40), nullable=False, info=classify(Sensitivity.PUBLIC))
    severity: Mapped[str] = mapped_column(
        String(10), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    owner_role: Mapped[str] = mapped_column(
        String(20), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    #: Human-readable, shown to a reviewer. INTERNAL: it quotes descriptions and
    #: amounts from the invoice.
    detail: Mapped[str] = mapped_column(Text, nullable=False, info=classify(Sensitivity.INTERNAL))

    invoice_line_number: Mapped[int | None] = mapped_column(
        Integer, nullable=True, info=classify(Sensitivity.PUBLIC)
    )
    purchase_order_line_number: Mapped[int | None] = mapped_column(
        Integer, nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    #: The numbers behind the judgment: expected, actual, variance, and which
    #: threshold it tripped. Carried rather than recomputed for display, because
    #: a number derived twice eventually disagrees with itself.
    breach: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True, info=classify(Sensitivity.INTERNAL)
    )

    match_result: Mapped[MatchResult] = relationship(back_populates="exceptions")

    __table_args__ = (
        CheckConstraint(
            "severity in ('block', 'review', 'info')", name="ck_match_exception_severity"
        ),
        # One lookup per queue row: `WHERE match_result_id = ? ORDER BY due_at`.
        # `match_exception` carried no index on `match_result_id` at all, so a
        # fifty-row page was fifty sequential scans of a growing table.
        Index("ix_match_exception_match_result", "match_result_id", "due_at"),
        Index("ix_match_exception_code", "code"),
    )


class ApprovalDecision(Base):
    """One decision about one invoice, by a person or by the system.

    The machine and the human are peers here. When a match raises nothing, the
    matcher writes an ``auto_approved`` row with ``actor_type = 'system'``; when
    a person decides, the row differs in its actor and its reason and in nothing
    else. That is what turns "what did this system decide without asking
    anybody, and who decided the rest" into one query against one table, rather
    than an inference from the absence of a human record.

    **Attached to a match_result, not to a document.** A decision is about a
    body of evidence: this extraction, judged under this policy version, with
    these exceptions. When a receipt arrives or the tolerance policy moves, the
    matcher writes a new ``match_result`` version and the earlier approval
    simply does not refer to it. Nothing needs to be invalidated, because
    nothing was ever attached to the document in general — which is the failure
    this shape avoids: an approval granted on evidence that later changed, still
    reading as current.

    Append-only in the same spirit as ``audit_event``. There is no un-approve,
    no edit and no delete. A decision that turns out to be wrong is followed by
    another decision, and both are visible.
    """

    __tablename__ = "approval_decision"

    id: Mapped[uuid.UUID] = _pk()

    match_result_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("match_result.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    #: Denormalised from match_result so the queue can be read without a join,
    #: and so a decision remains attributable to a document if the matching
    #: history is ever archived. It is a foreign key, not a copy of a string.
    document_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("document.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    decision: Mapped[str] = mapped_column(
        String(20), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    #: The issuer's subject, not the username. Usernames get reassigned, and a
    #: row reading "clerk" three years from now may mean a different person.
    actor_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True, info=classify(Sensitivity.INTERNAL)
    )
    actor_label: Mapped[str | None] = mapped_column(
        String(200), nullable=True, info=classify(Sensitivity.INTERNAL)
    )

    #: Why. Required of every human decision and absent on the system's own,
    #: which has no judgement to explain. The only part of this row a later
    #: reader cannot reconstruct from the others.
    reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, info=classify(Sensitivity.INTERNAL)
    )

    #: The exception codes this decision cleared. An approval that records only
    #: that somebody clicked is the fail-open path wearing a justification
    #: field; this is what makes "approved over a bank-detail change" countable.
    overrode: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        info=classify(Sensitivity.PUBLIC),
    )

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    match_result: Mapped[MatchResult] = relationship(back_populates="decisions")

    __table_args__ = (
        CheckConstraint(
            "decision in ('auto_approved', 'approved', 'rejected', 'held')",
            name="ck_approval_decision_kind",
        ),
        CheckConstraint(
            "actor_type in ('system', 'human', 'agent')",
            name="ck_approval_decision_actor_type",
        ),
        # A human decision must say why; the system's must not pretend to.
        CheckConstraint(
            "(actor_type = 'system') = (reason IS NULL)",
            name="ck_approval_decision_reason_matches_actor",
        ),
        CheckConstraint(
            "(actor_type = 'human') = (actor_id IS NOT NULL)",
            name="ck_approval_decision_human_is_identified",
        ),
        # At most one terminal decision per match result, enforced by the
        # database rather than by a read-then-write in application code. Two
        # reviewers opening the same invoice is not hypothetical, and the loser
        # of that race must be told, not silently second.
        #
        # `auto_approved` and `held` are absent on purpose. An invoice the
        # machine cleared must remain stoppable by a person — see D-016 — and a
        # hold is a state somebody is in the middle of rather than an outcome.
        Index(
            "uq_approval_decision_terminal",
            "match_result_id",
            unique=True,
            postgresql_where=text("decision IN ('approved', 'rejected')"),
        ),
        Index("ix_approval_decision_document", "document_id", "decided_at"),
    )


class Escalation(Base):
    """What the triage agent said, kept separate from what the system decided.

    A row here changes nothing. The exception it cites already carries its own
    ``owner_role`` and ``due_at``, written when the control fired; this says
    which of those desks should look first and offers a sentence explaining why.
    Deleting every row in this table would cost the reviewer context and would
    not move a single invoice.

    That separation is the reason it is its own table rather than three columns
    on ``match_result``. Columns on the decision row read as part of the
    decision, and six months from now somebody would join them into a report
    that treats an agent's opinion as a system finding. A foreign key with a
    different name does not invite that.

    ``summary`` is the only free text a model wrote anywhere in this schema. It
    is INTERNAL, which is what the review projection needs, and there is no
    agent tool that reads this table -- so a later escalation cannot be
    influenced by an earlier one's prose.
    """

    __tablename__ = "escalation"

    id: Mapped[uuid.UUID] = _pk()

    match_result_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("match_result.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    #: Which desk should look first. Not authoritative: `match_exception`
    #: carries the ownership that routing and authorisation actually read, and
    #: `domain/triage.py` refuses any value not already recorded there.
    owner_role: Mapped[str] = mapped_column(
        String(20), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    #: The exception codes cited, which must be a subset of those recorded.
    #: Stored so the uncited remainder is recoverable by difference -- an agent
    #: that stays quiet about one of three exceptions leaves that trace here.
    codes: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        info=classify(Sensitivity.PUBLIC),
    )

    #: Attacker-influenced by construction: the model that wrote it read
    #: exception details containing the supplier's own line descriptions. The
    #: console must render it as something the agent said rather than as
    #: something the system knows.
    summary: Mapped[str] = mapped_column(Text, nullable=False, info=classify(Sensitivity.INTERNAL))

    #: Which tier produced it -- the deterministic ruleset and its version, or a
    #: model and its version. Recorded for the same reason `Extraction` records
    #: its extractor: a change in output must be attributable to a change in
    #: producer rather than investigated as a mystery.
    triager_id: Mapped[str] = mapped_column(
        String(100), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        info=classify(Sensitivity.PUBLIC),
    )

    match_result: Mapped[MatchResult] = relationship()

    __table_args__ = (
        # The domain rule, repeated at the table. `domain/triage.py` refuses an
        # empty code list, which protects the path through the agent; this
        # protects the table from everything else.
        CheckConstraint(
            "jsonb_array_length(codes) > 0",
            name="ck_escalation_cites_something",
        ),
        # One escalation per match result. A second is not a correction -- the
        # match result is itself versioned, so a re-decided invoice gets a new
        # row to escalate against, and two escalations on one decision would be
        # an agent that ran twice and a reviewer with two opinions to reconcile.
        Index("uq_escalation_match_result", "match_result_id", unique=True),
        Index("ix_escalation_owner", "owner_role", "created_at"),
    )


class LedgerTransaction(Base):
    """One balanced movement of money. Its entries are the two or more sides.

    A transaction exists so that the postings have something to sum to zero
    *within*. The balance rule is enforced by `ck_ledger_entry_balanced`, a
    deferred constraint trigger that runs at COMMIT rather than per statement --
    because the rows arrive one INSERT at a time and the set is unbalanced
    between them, which is normal and must not be an error.

    Both `payment_id` and `match_result_id` are nullable and at most one is set
    on any row. An accrual belongs to a decision, a payment movement belongs to
    a payment, and the opening balance belongs to neither.
    """

    __tablename__ = "ledger_transaction"

    id: Mapped[uuid.UUID] = _pk()

    kind: Mapped[str] = mapped_column(String(30), nullable=False, info=classify(Sensitivity.PUBLIC))

    #: ISO 4217, and the exponent that was in force when this was written. The
    #: exponent is stored rather than looked up for the reason `policy_version`
    #: is stored on `match_result`: a table is a present-tense fact, and
    #: re-deriving a historical row from it moves every decimal point in it.
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    currency_exponent: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("payment.id", ondelete="RESTRICT"),
        nullable=True,
        info=classify(Sensitivity.PUBLIC),
    )
    match_result_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("match_result.id", ondelete="RESTRICT"),
        nullable=True,
        info=classify(Sensitivity.PUBLIC),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        info=classify(Sensitivity.PUBLIC),
    )

    entries: Mapped[list[LedgerEntry]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(payment_id, match_result_id) <= 1",
            name="ck_ledger_transaction_one_subject",
        ),
        CheckConstraint("currency_exponent >= 0", name="ck_ledger_transaction_exponent"),
        Index("ix_ledger_transaction_payment", "payment_id"),
        Index("ix_ledger_transaction_match_result", "match_result_id"),
        Index("ix_ledger_transaction_kind", "kind", "created_at"),
    )


class LedgerEntry(Base):
    """One side of a movement. Debit positive, credit negative.

    Append-only in practice and enforced as such in intent: a mistake is
    corrected by a reversing transaction, never by an UPDATE. The trigger covers
    UPDATE and DELETE as well as INSERT, so an attempt to rewrite history still
    has to leave the books balanced -- which is much harder to do quietly than
    deleting a row.
    """

    __tablename__ = "ledger_entry"

    id: Mapped[uuid.UUID] = _pk()

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("ledger_transaction.id", ondelete="CASCADE"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    account: Mapped[str] = mapped_column(
        String(30), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    #: Integer minor units, signed. BIGINT because cents overflow an INTEGER at
    #: about 21 million in a two-decimal currency, which is not a large invoice.
    amount_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    transaction: Mapped[LedgerTransaction] = relationship(back_populates="entries")

    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="ck_ledger_entry_moves_something"),
        # An account appears once per transaction, so the ledger reads as what
        # happened rather than as how the code built it.
        Index("uq_ledger_entry_account", "transaction_id", "account", unique=True),
        Index("ix_ledger_entry_account", "account"),
    )


class Payment(Base):
    """One attempt to pay one authorised invoice, and its whole history of state.

    ``approval_decision_id`` is unique, and that is the third idempotency layer
    the `Job` docstring has promised since M0. The approval decision is the right
    root because it is the only row in this schema that means "this may be paid":
    it is append-only, it is attached to a body of evidence rather than to a
    document, and it is stable under re-matching.

    The other two candidates are wrong in instructive ways. ``match_result_id``
    identifies evidence rather than authorisation, and one match result can
    legitimately carry both a system ``auto_approved`` row and a later human
    ``approved`` one -- so keying on it would silently collapse two
    authorisations into one payment. ``document_id`` is both too strict, because
    re-matching after a correction is a designed operation, and too loose,
    because the duplicate this system exists to catch is one invoice arriving as
    two documents with two ids.

    ``idempotency_key`` is what the rail is told, and it is derived from the same
    approval decision. So the key and the constraint say the same thing in two
    places, which is what lets them catch each other failing.
    """

    __tablename__ = "payment"

    id: Mapped[uuid.UUID] = _pk()

    approval_decision_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("approval_decision.id", ondelete="RESTRICT"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    match_result_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("match_result.id", ondelete="RESTRICT"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("document.id", ondelete="RESTRICT"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("vendor.id", ondelete="RESTRICT"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )
    #: The payee, by this system own identifier. The account and routing digits
    #: on that row are RESTRICTED and are never copied here: a real rail adapter
    #: resolves them at the last moment inside itself, and the sandbox never
    #: needs them at all.
    vendor_bank_account_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("vendor_bank_account.id", ondelete="RESTRICT"),
        nullable=False,
        info=classify(Sensitivity.PUBLIC),
    )

    amount_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    currency_exponent: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    state: Mapped[str] = mapped_column(
        String(20), nullable=False, info=classify(Sensitivity.PUBLIC)
    )

    #: What the rail is told, so that asking twice is safe. Unique here as well
    #: as derived from a unique column, because the derivation is code and this
    #: is not.
    idempotency_key: Mapped[str] = mapped_column(
        String(120), nullable=False, unique=True, info=classify(Sensitivity.PUBLIC)
    )
    rail_id: Mapped[str] = mapped_column(
        String(100), nullable=False, info=classify(Sensitivity.PUBLIC)
    )
    #: The rail own identifier for this payment, so a dispute can be taken to it
    #: with something it recognises. Absent until the rail has answered once.
    rail_reference: Mapped[str | None] = mapped_column(
        String(120), nullable=True, info=classify(Sensitivity.PUBLIC)
    )
    failure_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, info=classify(Sensitivity.INTERNAL)
    )

    #: How many times the rail has been asked. Distinct from the job attempt
    #: count, which resets on a replay: this is how many times money could have
    #: moved, and it is the number an incident review wants.
    rail_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), info=classify(Sensitivity.PUBLIC)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        info=classify(Sensitivity.PUBLIC),
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, info=classify(Sensitivity.PUBLIC)
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_payment_amount_positive"),
        CheckConstraint("currency_exponent >= 0", name="ck_payment_exponent"),
        CheckConstraint(
            "state in ('pending', 'submitting', 'submitted', 'settled', 'failed')",
            name="ck_payment_state",
        ),
        # A failure must say why, and a success must not invent a reason. The
        # same shape as `ck_approval_decision_reason_matches_actor`, and for the
        # same reason: a nullable explanation nobody enforces is an explanation
        # that is absent exactly when it is needed.
        CheckConstraint(
            "(state = 'failed') = (failure_reason IS NOT NULL)",
            name="ck_payment_failure_is_explained",
        ),
        # The third idempotency layer. See the class docstring.
        Index("uq_payment_authorisation", "approval_decision_id", unique=True),
        # At most one live payment per body of evidence, shaped like
        # `uq_approval_decision_terminal` and partial for the same reason: a
        # failed payment must not block a corrected one.
        Index(
            "uq_payment_live_per_match",
            "match_result_id",
            unique=True,
            postgresql_where=text("state IN ('pending', 'submitting', 'submitted', 'settled')"),
        ),
        Index("ix_payment_state", "state", "created_at"),
        Index("ix_payment_document", "document_id"),
    )
