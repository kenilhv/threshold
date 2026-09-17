"""RESTRICTED never renders, as a property rather than a promise.

The point of every test here is that none of them names a column. They are all
derived from the mapper, so a RESTRICTED column added next month is covered by
tests written today — which is the only version of this guarantee worth having.
A hand-written list of forbidden fields protects the fields somebody remembered,
and the field at risk is the one on the table added last.
"""

from __future__ import annotations

import inspect as inspect_module
from decimal import Decimal

import pytest
from sqlalchemy import inspect

from threshold.agent import tools
from threshold.agent.tools import AGENT_IDENTITIES
from threshold.domain.classification import Sensitivity, classify, is_identity, sensitivity_of
from threshold.domain.extraction import RESTRICTED_FIELD_KEYS
from threshold.domain.models import Base, Document, PurchaseOrder, Vendor, VendorBankAccount
from threshold.domain.projection import identity_columns, project, restricted_columns

MODELS = [mapper.class_ for mapper in Base.registry.mappers]


class TestEveryModel:
    @pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
    def test_an_internal_projection_drops_every_restricted_column(self, model: type) -> None:
        row = model()

        visible = set(project(row, level=Sensitivity.INTERNAL))
        assert not (visible & restricted_columns(model)), (
            f"{model.__tablename__} leaked: {sorted(visible & restricted_columns(model))}"
        )

    @pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
    def test_a_public_projection_drops_internal_as_well(self, model: type) -> None:
        row = model()

        visible = set(project(row, level=Sensitivity.PUBLIC))
        for name in visible:
            column = inspect(model).columns[name]
            assert sensitivity_of(column) is Sensitivity.PUBLIC

    @pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
    def test_the_projection_is_not_empty(self, model: type) -> None:
        """A projection that shows nothing is safe and useless.

        Every table must have at least one PUBLIC column, or a reviewer cannot
        be shown the row at all — and the failure mode of over-classifying is
        silent, since nothing complains about a field nobody sees.
        """
        assert project(model(), level=Sensitivity.PUBLIC), (
            f"{model.__tablename__} has no PUBLIC column; it cannot be shown to anyone"
        )


class TestTheKnownRestrictedSurface:
    def test_it_is_exactly_what_we_think_it_is(self) -> None:
        """A canary, not a specification.

        This is the only test here that names columns, and it exists so that
        *adding* a RESTRICTED column is a deliberate act with a visible diff
        rather than something that happens quietly. Update it when the surface
        genuinely changes.
        """
        surface = {
            (mapper.class_.__tablename__, column)
            for mapper in Base.registry.mappers
            for column in restricted_columns(mapper.class_)
        }
        assert surface == {
            ("vendor", "tax_id"),
            ("vendor_bank_account", "account_number_last4"),
            ("vendor_bank_account", "routing_number_last4"),
            ("vendor_bank_account", "account_fingerprint"),
        }

    def test_a_vendors_tax_id_never_reaches_a_reviewer(self) -> None:
        vendor = Vendor(
            vendor_number="V-1",
            legal_name="Kestrel Components AB",
            display_name="Kestrel",
            country_code="SE",
            default_currency="EUR",
            tax_id="SE556677889901",
        )
        visible = project(vendor)
        assert "tax_id" not in visible
        assert visible["display_name"] == "Kestrel", "the reviewer still needs to know who"

    def test_bank_details_never_reach_a_reviewer(self) -> None:
        account = VendorBankAccount(
            account_number_last4="8291",
            routing_number_last4="9593",
            account_fingerprint="deadbeef",
        )
        visible = project(account)
        assert not {"account_number_last4", "routing_number_last4", "account_fingerprint"} & set(
            visible
        )

    def test_no_restricted_value_survives_even_when_set(self) -> None:
        """Values, not just keys. A dropped key that kept its value is a bug."""
        vendor = Vendor(
            vendor_number="V-2",
            legal_name="A",
            display_name="A",
            country_code="US",
            default_currency="USD",
            tax_id="SECRET-TAX-ID",
        )
        assert "SECRET-TAX-ID" not in str(project(vendor))


class TestJsonPayloads:
    """One INTERNAL column carries restricted keys inside its blob.

    A column-level rule alone lets the payment digits through inside a value it
    was told was safe.
    """

    def test_restricted_keys_are_dropped_from_a_nested_payload(self) -> None:
        from threshold.domain.models import Extraction

        extraction = Extraction(
            document_id=None,
            version=1,
            extractor_kind="deterministic",
            extractor_id="test",
            fields={
                "invoice_number": {"value": "INV-1", "confidence": 0.9},
                "bank_account_last4": {"value": "8291", "confidence": 0.9},
                "bank_routing_last4": {"value": "9593", "confidence": 0.9},
            },
            lowest_decision_confidence=Decimal("0.9"),
            duration_ms=1,
        )
        visible = project(extraction)
        assert "fields" in visible, "the extraction is the reviewer's evidence"
        assert set(visible["fields"]) == {"invoice_number"}
        assert "8291" not in str(visible)

    def test_restricted_keys_are_dropped_from_inside_lists(self) -> None:
        """Nesting is not an escape hatch."""
        from threshold.domain.models import Extraction

        extraction = Extraction(
            version=1,
            extractor_kind="deterministic",
            extractor_id="test",
            fields={"line_items": [{"sku": "A", "bank_account_last4": {"value": "8291"}}]},
            lowest_decision_confidence=Decimal("0.9"),
            duration_ms=1,
        )
        assert "8291" not in str(project(extraction))

    def test_the_restricted_key_set_is_not_empty(self) -> None:
        """If this empties, the JSON rule silently stops doing anything."""
        assert RESTRICTED_FIELD_KEYS

    def test_a_tax_id_is_restricted_wherever_it_appears(self) -> None:
        """The same identifier, classified the same way in both places.

        Vendor.tax_id is RESTRICTED on the column. The value read off the
        invoice is the same government identifier and was being served to any
        caller inside the extraction blob, so one copy was secret and the other
        was not.
        """
        from threshold.domain.models import Extraction

        extraction = Extraction(
            version=1,
            extractor_kind="deterministic",
            extractor_id="test",
            fields={"vendor_tax_id": {"value": "DE811907980", "confidence": 0.9}},
            lowest_decision_confidence=Decimal("0.9"),
            duration_ms=1,
        )
        assert "DE811907980" not in str(project(extraction))


class TestFailClosed:
    def test_an_unclassified_column_is_treated_as_restricted(self) -> None:
        """Forgetting to classify hides a column rather than leaking it.

        The schema guard in test_money_and_classification.py makes an
        unclassified column a build failure, so this is defence behind that —
        the behaviour if the guard were ever removed or bypassed.
        """
        from sqlalchemy import Column, String

        undeclared = Column("mystery", String(10))
        assert sensitivity_of(undeclared) is Sensitivity.RESTRICTED


class TestIdentityColumnsAreWithheldFromTheAgent:
    """The second axis, added in M9. Derived from the mapper like everything
    above: naming the columns here would protect the ones somebody remembered.

    What is being tested is narrow and worth stating plainly. Identity columns
    are *dropped* from the agent's projection, not aliased. The README promised
    a keyed alias scheme for two milestones; D-036 records why writing it down
    was what killed it.
    """

    @pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
    def test_dropping_identities_never_adds_a_column(self, model: type) -> None:
        row = model()
        assert set(project(row, identities=False)) <= set(project(row, identities=True))

    @pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
    def test_it_drops_exactly_the_columns_that_declare_it(self, model: type) -> None:
        row = model()
        withheld = set(project(row, identities=True)) - set(project(row, identities=False))
        assert withheld == identity_columns(model)

    def test_the_agent_is_shown_neither_a_name_nor_an_address_nor_a_filename(self) -> None:
        """The three that exist today, by name, because a test that is only
        derived cannot fail when the declaration is removed from all of them."""
        assert identity_columns(Vendor) == {"legal_name", "display_name"}
        assert identity_columns(Document) == {"original_filename"}
        assert identity_columns(PurchaseOrder) == {"buyer_email"}

    def test_the_agent_projects_without_them(self) -> None:
        """The wiring, not the mechanism: `agent/tools.py` must actually pass it."""
        assert AGENT_IDENTITIES is False
        source = inspect_module.getsource(tools)
        calls = source.count("project(")
        wired = source.count("identities=AGENT_IDENTITIES")
        assert calls == wired, f"{calls} projections, {wired} of them identity-aware"

    def test_a_document_keeps_what_the_agent_needs(self) -> None:
        """The vacuity guard. Dropping every column would satisfy everything above.

        A filename goes; the content type, the source and the hash stay, and
        those are what a triager reasons about.
        """
        document = Document(
            original_filename="ACME Holdings - INV-4471.pdf",
            content_type="application/pdf",
            source="upload",
            content_sha256="a" * 64,
            storage_key="documents/x",
            byte_size=1,
        )
        shown = project(document, identities=False)
        assert "original_filename" not in shown
        assert "ACME" not in str(shown)
        assert shown["content_type"] == "application/pdf"
        assert shown["source"] == "upload"

    def test_staff_still_see_the_name(self) -> None:
        """The other vacuity guard: the console reads this projection too, and a
        review queue that will not say who is billing is not a review queue."""
        vendor = Vendor(legal_name="ACME Holdings BV", display_name="ACME")
        assert project(vendor)["legal_name"] == "ACME Holdings BV"


class TestTheTwoAxesDoNotContradictEachOther:
    def test_a_public_column_cannot_be_an_identity(self) -> None:
        with pytest.raises(ValueError, match="PUBLIC"):
            classify(Sensitivity.PUBLIC, identity=True)

    def test_a_restricted_column_need_not_say_so_twice(self) -> None:
        with pytest.raises(ValueError, match="already dropped"):
            classify(Sensitivity.RESTRICTED, identity=True)

    def test_an_ordinary_column_carries_exactly_what_it_did_before(self) -> None:
        """The key is absent rather than false, so the overwhelming majority of
        columns are byte-identical to how they were declared before M9."""
        assert classify(Sensitivity.INTERNAL) == {"sensitivity": Sensitivity.INTERNAL}

    def test_every_identity_is_internal(self) -> None:
        for model in MODELS:
            for column in inspect(model).columns:
                if is_identity(column):
                    assert sensitivity_of(column) is Sensitivity.INTERNAL, column
