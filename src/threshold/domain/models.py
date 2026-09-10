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

    #: As supplied. INTERNAL because filenames routinely leak vendor names,
    #: amounts and project codes.
    original_filename: Mapped[str] = mapped_column(
        String(400), nullable=False, info=classify(Sensitivity.INTERNAL)
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
    #: it identifies an employee.
    buyer_email: Mapped[str | None] = mapped_column(
        String(320), nullable=True, info=classify(Sensitivity.INTERNAL)
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

    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_match_result_version"),
        CheckConstraint(
            "disposition in ('auto_approve', 'needs_review', 'blocked')",
            name="ck_match_result_disposition",
        ),
        CheckConstraint("version > 0", name="ck_match_result_version_positive"),
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

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, info=classify(Sensitivity.PUBLIC)
    )

    match_result: Mapped[MatchResult] = relationship(back_populates="exceptions")

    __table_args__ = (
        CheckConstraint(
            "severity in ('block', 'review', 'info')", name="ck_match_exception_severity"
        ),
        Index(
            "ix_match_exception_open_by_owner",
            "owner_role",
            "due_at",
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index("ix_match_exception_code", "code"),
    )
