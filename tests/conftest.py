"""Suite-wide fixtures.

Importing `tests.metrics_reader` here is load-bearing rather than incidental:
pytest imports the rootdir conftest before it collects anything, and that module
has to install its meter provider before any test module imports `api.main`,
which installs one of its own at import time. See that file for why only the
first provider in a process counts.
"""

from __future__ import annotations

import pytest
from tests.metrics_reader import Recording, _read

from threshold.observability.metrics import reset_for_testing


@pytest.fixture
def recording() -> Recording:
    """A mark in the cumulative reader, to measure a test's effect against."""
    reset_for_testing()
    return Recording(before=_read())
