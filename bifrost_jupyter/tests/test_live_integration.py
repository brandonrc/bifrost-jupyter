"""Live acceptance: real handlers → real ``bifrost_client`` → a running Bifrost.

Skipped unless ``BIFROST_LIVE=1`` and ``BIFROST_API_URL``/``BIFROST_TOKEN`` are
set. Drives the same-origin ``/bifrost/*`` routes over HTTP (via ``jp_fetch``)
with the *real* Bifrost client — no fakes — against a dev Bifrost.

What this proves without a Ray backend: the profile→CreateCluster body is
accepted by the real control plane, the get round-trip returns a status, and the
address route resolves a gateway host from the live registry into a runnable
snippet. What it does NOT prove (needs a live KubeRay): the cluster reaching
``observed_state=running`` and a job actually running over the gateway Jobs API.

Point ``--registry`` at an entry whose ``id`` is ``BIFROST_LIVE_REGISTERED_ID``
(default ``cl-test``) so the address route has a host to resolve.
"""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BIFROST_LIVE") != "1",
    reason="live Bifrost not configured (set BIFROST_LIVE=1 + BIFROST_API_URL/BIFROST_TOKEN)",
)


async def test_live_post_clusters(jp_fetch):
    resp = await jp_fetch("bifrost", "clusters", method="POST", body="{}")
    assert resp.code == 200
    payload = json.loads(resp.body)
    assert payload["id"]
    assert payload["status"]  # 'running' desired; observed may lag without a controller
    assert os.environ["BIFROST_TOKEN"] not in resp.body.decode()


async def test_live_get_address(jp_fetch):
    registered_id = os.environ.get("BIFROST_LIVE_REGISTERED_ID", "cl-test")
    resp = await jp_fetch("bifrost", "clusters", registered_id, "address")
    assert resp.code == 200
    payload = json.loads(resp.body)
    assert payload["jobs_address"].startswith("https://")
    assert "JobSubmissionClient" in payload["snippet"]
    assert os.environ["BIFROST_TOKEN"] not in resp.body.decode()
