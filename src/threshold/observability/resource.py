"""Who is speaking.

Every signal this process emits carries the same resource, and that is the
point of putting it here rather than building one in each module. A resource is
the answer to "which service produced this", and traces and metrics disagreeing
about the answer is not a cosmetic problem: it makes a trace and a metric that
describe the same incident impossible to line up.

This repository has already had that bug once in the other direction. The
compose environment anchor is shared between the API and the worker, so
``service_name`` defaulted to ``threshold-api`` in both and the worker called
itself the API for three milestones.
"""

from __future__ import annotations

from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.resource import ResourceAttributes

from threshold.config.settings import Settings


def resource_for(settings: Settings) -> Resource:
    return Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: settings.service_name,
            ResourceAttributes.DEPLOYMENT_ENVIRONMENT: settings.environment,
        }
    )
