"""The extraction boundary.

One interface, two tiers. The deterministic tier is worse and always available;
the model tier is better and needs weights, credentials and network. Both return
the same schema-validated result, and every stored extraction records which tier
produced it so an evaluation can never silently average them together.

This pattern is carried over deliberately from an earlier project in this
portfolio where it worked: a fallback tier that runs hermetically means CI
exercises the whole pipeline on every commit instead of mocking the interesting
part away.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from threshold.domain.extraction import ExtractionResult


class ExtractionError(RuntimeError):
    """The document could not be read at all.

    Distinct from low confidence. A low-confidence extraction is a normal result
    that routes to a human; an ExtractionError means there is nothing to route —
    a corrupt file, an unsupported format, a provider that stayed down through
    its retries. The two must not collapse into one path, because one is a
    business outcome and the other is an incident.
    """


@runtime_checkable
class Extractor(Protocol):
    """Turns document bytes into a validated, confidence-annotated result."""

    @property
    def extractor_id(self) -> str:
        """Identifies this extractor precisely enough to reproduce a result.

        A model name and version, or the deterministic tier's own version. Stored
        on every extraction row so a change in output can be attributed to a
        change in extractor rather than investigated as a mystery.
        """
        ...

    async def extract(self, content: bytes, content_type: str) -> ExtractionResult: ...
