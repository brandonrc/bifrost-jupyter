"""The one place a ``bifrost_client`` API client is built (Task 11).

The generated client leaves ``timeout = None`` unless a caller passes
``_request_timeout`` (``bifrost_client/rest.py``), so urllib3 waits forever. That
is a property of **the generated client**, not of any particular route: every
call through it inherits the defect, and a call site that forgets the argument
inherits it silently.

Passing the argument at each call site does not fix that — it fixes the call
sites that exist today and leaves the next one to rediscover the problem. So the
bound lives at the chokepoint instead: every generated API method funnels through
``ApiClient.call_api``, and :class:`BoundedApiClient` supplies a default there.
A new route, a new endpoint wrapper, or a new module that builds its client with
:func:`bounded_api_client` is bounded whether or not its author knows this file
exists; an explicit ``_request_timeout`` still wins where a caller wants a
different budget.

``bifrost_jupyter.tests.test_no_unbounded_clients`` fails if anything in the
package constructs a raw ``ApiClient``, so the seam cannot be bypassed by
accident.
"""

from __future__ import annotations

from typing import Any

from bifrost_client import ApiClient, Configuration
from bifrost_client.rest import RESTResponse

#: Default per-request budget, in seconds.
#:
#: Not a tuning knob — a liveness guarantee. A Bifrost that accepts the
#: connection and then never answers would otherwise pin the calling thread for
#: the life of the notebook server; enough of those starve the handler thread
#: pool and the panel stops responding entirely.
#:
#: Generous rather than tight: a cluster create is a real control-plane
#: operation, and a spurious timeout is a worse failure than a slow success.
DEFAULT_TIMEOUT_SECONDS = 30.0


class BoundedApiClient(ApiClient):
    """An ``ApiClient`` that never issues an unbounded request.

    Overrides the single method every generated endpoint funnels through, so the
    default applies to endpoints this extension has not wrapped yet.
    """

    def __init__(self, configuration: Configuration, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        super().__init__(configuration)
        self.default_timeout = timeout

    def call_api(  # type: ignore[override]
        self,
        method: Any,
        url: Any,
        header_params: Any = None,
        body: Any = None,
        post_params: Any = None,
        _request_timeout: Any = None,
    ) -> RESTResponse:
        # The upstream signature is mirrored rather than *args-ed, because
        # ``_request_timeout`` is also reachable positionally and a passthrough
        # would then supply it twice.
        if _request_timeout is None:
            _request_timeout = self.default_timeout
        return super().call_api(
            method,
            url,
            header_params=header_params,
            body=body,
            post_params=post_params,
            _request_timeout=_request_timeout,
        )


def bounded_api_client(
    configuration: Configuration, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> BoundedApiClient:
    """Build the API client every Bifrost-facing module in this package uses."""
    return BoundedApiClient(configuration, timeout)
