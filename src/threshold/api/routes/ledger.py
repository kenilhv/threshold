"""The trial balance, and the one number that says whether the books are sound.

Every account's balance, and their sum. The sum must be zero. That is the oldest
check in accounting and it is still the useful one: a single number that goes
non-zero the moment anything anywhere has gone wrong, computable in one query at
any size.

It is served rather than only tested because the failure this catches is not a
code failure. `domain/ledger.py` refuses an unbalanced transaction, and
`ck_ledger_entry_balanced` refuses one at COMMIT, so the arithmetic cannot drift
through either door. What this answers is the question those two cannot: has
something reached this table that did not come through either of them -- a
restore from a bad backup, a migration that moved rows, an afternoon of manual
SQL during an incident.

Behind `require_principal` and readable by any authenticated role including
`auditor`. A trial balance is exactly what an auditor is for, and it contains no
vendor, no invoice and no amount attributable to either -- five integers and
their sum.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from threshold.api.auth import Principal, require_principal
from threshold.api.dependencies import SessionDep
from threshold.domain.ledger import KIND_OF, Account, to_major
from threshold.services.payments import PaymentService

router = APIRouter(prefix="/v1", tags=["ledger"])


class AccountBalance(BaseModel):
    """One account, with enough context to read its sign.

    ``kind`` is here because the sign alone does not say whether a balance is
    healthy. Liabilities and equity run credit-negative, so `accounts_payable`
    at -482000 means we owe 4,820.00 and is exactly right, while the same number
    on `cash` means an overdraft.
    """

    account: str
    kind: str
    amount_minor: int
    amount: str


class TrialBalance(BaseModel):
    accounts: list[AccountBalance]
    #: The whole point. Anything but zero means the books do not add up, and no
    #: other number on this page can be trusted until it does.
    total_minor: int
    balanced: bool
    #: Every balance here is in this currency. The ledger stores a currency per
    #: transaction and this system has only ever written one; a second would
    #: make a single trial balance meaningless, which is why it is stated rather
    #: than assumed.
    currency: str


@router.get(
    "/ledger/trial-balance",
    response_model=TrialBalance,
    summary="Every account balance, and whether they sum to zero",
)
async def trial_balance(
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_principal)],
) -> TrialBalance:
    balances = await PaymentService().trial_balance(session)
    currency = "EUR"
    rows = [
        AccountBalance(
            account=account.value,
            kind=KIND_OF[account].value,
            amount_minor=balances.get(account, 0),
            amount=str(to_major(balances.get(account, 0), currency)),
        )
        # Every account in the chart, including the ones at zero. An account
        # missing from a trial balance reads as "not applicable" when it means
        # "nothing has happened here yet", and those are different facts.
        for account in Account
    ]
    total = sum(row.amount_minor for row in rows)
    return TrialBalance(
        accounts=rows,
        total_minor=total,
        balanced=total == 0,
        currency=currency,
    )
