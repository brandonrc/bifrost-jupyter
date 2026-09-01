"""Live acceptance: real handlers → real ``bifrost_client`` → a running Bifrost.

Skipped unless ``BIFROST_LIVE=1`` and ``BIFROST_API_URL``/``BIFROST_TOKEN`` are
set. Drives the same-origin ``/bifrost/*`` routes over HTTP (via ``jp_fetch``)
with the *real* Bifrost client — no fakes — against a dev Bifrost.

What this proves without a Ray backend: the profile→CreateCluster body is
accepted by the real control plane (create uses AuthorizeScoped Write) and the
get round-trip returns a status (Read). What it does NOT prove (needs a live
KubeRay): the cluster reaching ``observed_state=running`` and a job actually
running over the in-cluster Jobs API.

The address route is intentionally NOT exercised here: it makes no backend call
(the address is derived client-side from id + namespace), so there is nothing
live to verify beyond the unit tests.
"""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BIFROST_LIVE") != "1",
    reason="live Bifrost not configured (set BIFROST_LIVE=1 + BIFROST_API_URL/BIFROST_TOKEN)",
)


async def test_live_post_clusters(jp_fetch):
    resp = await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}')
    assert resp.code == 200
    payload = json.loads(resp.body)
    assert payload["id"]
    assert payload["status"]  # 'running' desired; observed may lag without a controller
    assert os.environ["BIFROST_TOKEN"] not in resp.body.decode()
