"""Job states and the vocabulary of durable execution.

The queue is a Postgres table rather than a broker. That is the significant
design decision in this milestone and it is argued in docs/ARCHITECTURE.md; the
short version is that a broker makes "enqueue the job" and "commit the state
change" two systems, which is a distributed transaction. One table gives both in
one commit.

The honest cost is stated there too: it does not fan out, it does not replay a
topic, and somewhere above a few hundred jobs per second the polling loop
becomes the bottleneck. Naming the limit of your own design is the point.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final


class JobState(StrEnum):
    """Where a job is.

    ``CLAIMED`` is deliberately distinct from ``RUNNING``: a worker that dies
    holds a claim it can no longer act on, and the reaper needs to tell that
    apart from a job nobody has picked up. The lease is what makes the
    difference observable without the worker being alive to report it.
    """

    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Exhausted its attempts. Terminal until a human replays it.
    DEAD = "dead"


class JobKind(StrEnum):
    EXTRACT_DOCUMENT = "extract_document"
    MATCH_INVOICE = "match_invoice"
    #: Runs after a match that raised exceptions, and only then. Its own job
    #: rather than a tail on `match_invoice` because it is allowed to fail: an
    #: escalation is advice, and an invoice must be routed and clocked whether
    #: or not the advice arrives. Chaining it inline would let a model tier
    #: being slow, down, or captured hold up the decision that blocks a payment.
    TRIAGE_INVOICE = "triage_invoice"
    #: Runs after an approval and advances one payment by one state. Its own job
    #: rather than a loop inside one, because the row has to be committed as
    #: `submitting` before the rail is called -- and a handler owns exactly one
    #: transaction, so the commit between the two is the queue's to provide.
    EXECUTE_PAYMENT = "execute_payment"


#: How long a claim is good for. A worker must finish or heartbeat within this,
#: or the reaper assumes it died and returns the job to the queue.
#:
#: Tuned against the work, not picked round: extraction runs in tens of
#: milliseconds on the deterministic tier and is budgeted at 90 seconds on the
#: model tier (M7), so two minutes leaves headroom for a slow provider without
#: leaving a genuinely dead worker's job stuck for long.
DEFAULT_LEASE: Final = timedelta(minutes=2)

#: Attempts include the first. Three means one try and two retries.
DEFAULT_MAX_ATTEMPTS: Final = 3

#: Exponential, from a small base. The failures this retries are transient —
#: a provider blip, a connection reset — and a document that is genuinely
#: unreadable fails identically on every attempt, so long backoffs would only
#: delay its arrival in the dead-letter queue where a human can see it.
BACKOFF_BASE: Final = timedelta(seconds=5)
BACKOFF_MAX: Final = timedelta(minutes=10)


def backoff_for(attempt: int) -> timedelta:
    """Delay before attempt N+1, capped.

    No jitter, deliberately. Jitter matters when many clients retry against one
    dependency and would otherwise synchronise into a thundering herd; here the
    jobs are already spread by their own arrival times and the worker pool is
    small. Adding it would be cargo cult — and it would make the retry schedule
    untestable without controlling a random source.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    delay: timedelta = BACKOFF_BASE * (2 ** (attempt - 1))
    return delay if delay < BACKOFF_MAX else BACKOFF_MAX


class TransientJobError(Exception):
    """Failed in a way that another attempt might fix.

    A provider timeout, a connection reset, a rate limit. The job goes back to
    the queue with backoff.
    """


class PermanentJobError(Exception):
    """Failed in a way that no number of attempts will fix.

    A corrupt document, an unsupported format, a payload referring to a row that
    no longer exists. Retrying wastes the attempt budget and delays the job
    reaching a human, so it goes straight to the dead-letter queue.

    Distinguishing these two is the difference between a queue that recovers and
    a queue that grinds. Most implementations retry everything and discover the
    distinction the first time a poison message consumes a worker for an hour.
    """
