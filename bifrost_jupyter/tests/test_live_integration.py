"""Live acceptance: real handlers → real ``bifrost_client`` → a running Bifrost.

Skipped unless ``BIFROST_LIVE=1`` and ``BIFROST_API_URL``/``BIFROST_TOKEN`` are
set. Drives the same-origin ``/bifrost/*`` routes over HTTP (via ``jp_fetch``)
with the *real* Bifrost client — no fakes — against a dev Bifrost.

What this proves: the whole panel-facing surface of requirement 9 against a
real control plane — the profile catalogue the panel offers, the
profile→CreateCluster body, the status round-trip, suspend, resume and stop —
with the real credential resolver in front of it. Against a deployment with a
live KubeRay (grace) the cluster really converges; against an API-only Bifrost
(CI) the record is created and the observed state simply lags, which is why
nothing here asserts ``running``.

What it does NOT prove: requirement 11's env vars. Those attach to a Ray job
at submit time, and the jobs route deliberately talks straight to the head's
in-cluster address rather than through Bifrost, so it is only reachable from
inside the cluster. `clean_env_vars`/`build_submit_body` are unit-tested;
the live half needs the suite to run in-cluster.

The address route is intentionally NOT exercised here: it makes no backend call
(the address is derived client-side from id + namespace), so there is nothing
live to verify beyond the unit tests.

Needs ``BIFROST_PROJECT`` to name a project the token may write to. Its default
is ``jupyter``, and a token scoped elsewhere gets a 403 that reads like a
permission bug rather than a misconfiguration.
"""

import asyncio
import json
import os

import pytest

pytestmark = [
    # `live` is what tells the autouse credential fixture to hand this module
    # the real environment instead of a scrubbed one (see conftest.py).
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("BIFROST_LIVE") != "1",
        reason="live Bifrost not configured (set BIFROST_LIVE=1 + BIFROST_API_URL/BIFROST_TOKEN)",
    ),
]


async def test_live_post_clusters(jp_fetch):
    resp = await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}')
    assert resp.code == 200
    payload = json.loads(resp.body)
    assert payload["id"]
    assert payload["status"]  # 'running' desired; observed may lag without a controller
    assert os.environ["BIFROST_TOKEN"] not in resp.body.decode()


async def _create(jp_fetch) -> str:
    resp = await jp_fetch("bifrost", "clusters", method="POST", body='{"profile": "small"}')
    assert resp.code == 200
    return json.loads(resp.body)["id"]


async def _state(jp_fetch, cluster_id: str) -> str | None:
    listed = await jp_fetch("bifrost", "clusters")
    for cluster in json.loads(listed.body)["clusters"]:
        if cluster["id"] == cluster_id:
            return cluster.get("state")
    return None


async def _await_state(jp_fetch, cluster_id: str, want: str) -> bool:
    """Wait for the panel to report ``want``, or report that it never will.

    Against a deployment with a live KubeRay each transition is a matter of a
    minute. Against an API-only Bifrost (no ``--namespace``, which is how CI
    runs it) nothing reconciles, so the record never leaves pending — and the
    caller skips the parts that need a real cluster rather than failing on a
    deployment that was never going to get there.
    """
    budget = float(os.environ.get("BIFROST_LIVE_RUNNING_TIMEOUT", "180"))
    waited = 0.0
    while waited < budget:
        if await _state(jp_fetch, cluster_id) == want:
            return True
        await asyncio.sleep(5)
        waited += 5
    return False


async def _delete(jp_fetch, cluster_id: str) -> None:
    resp = await jp_fetch("bifrost", "clusters", cluster_id, method="DELETE")
    assert resp.code in (200, 202, 204)


async def test_live_profiles_are_offered(jp_fetch):
    """The panel's first call: what may this user start?"""
    resp = await jp_fetch("bifrost", "profiles")
    assert resp.code == 200
    payload = json.loads(resp.body)
    names = [p["name"] for p in payload["profiles"]]
    assert "small" in names, f"the live deployment offers {names}"
    assert os.environ["BIFROST_TOKEN"] not in resp.body.decode()


async def test_live_cluster_lifecycle(jp_fetch):
    """Start, read, suspend, resume, stop — the panel's buttons, in order.

    Each step is asserted against the real control plane's answer rather than a
    fake's, which is the whole point: the panel's contract with Bifrost
    (paths, bodies, status codes, and the scoping the token carries) is what
    this can get wrong between releases.
    """
    cluster_id = await _create(jp_fetch)
    try:
        # Status comes from the list route: `/clusters/{id}` carries DELETE
        # only, which is how the panel reads it too. Note the field name — a
        # listed cluster carries `state`, while create/stop/suspend answer with
        # `status`. Both are declared in src/api.ts; a client that assumes one
        # name everywhere reads undefined from the other.
        listed = await jp_fetch("bifrost", "clusters")
        assert listed.code == 200
        mine = [c for c in json.loads(listed.body)["clusters"] if c["id"] == cluster_id]
        assert mine, f"{cluster_id} is missing from the panel's list"
        assert mine[0].get("state"), "a listed cluster carries a state"

        # Each transition is legal only from the state before it, and Bifrost
        # judges that on the *observed* state: suspend before the cluster is
        # running, or resume before it has actually scaled down, is a 409. So
        # this waits between the two, which is also what a user does.
        if await _await_state(jp_fetch, cluster_id, "running"):
            resp = await jp_fetch(
                "bifrost", "clusters", cluster_id, "suspend", method="POST", body=""
            )
            assert resp.code in (200, 202, 204), f"suspend answered {resp.code}"
            assert await _await_state(jp_fetch, cluster_id, "suspended"), (
                "suspend was accepted but the cluster never reported suspended"
            )
            resp = await jp_fetch(
                "bifrost", "clusters", cluster_id, "resume", method="POST", body=""
            )
            assert resp.code in (200, 202, 204), f"resume answered {resp.code}"
            assert await _await_state(jp_fetch, cluster_id, "running"), (
                "resume was accepted but the cluster never came back"
            )
        else:
            print(
                f"suspend/resume not exercised: {cluster_id} never reached running "
                "(an API-only Bifrost has no controller)"
            )
    finally:
        await _delete(jp_fetch, cluster_id)

    # Stop is accepted, not immediate: the record is tombstoned and the
    # reconciler reaps. What the panel must not do is keep offering the cluster
    # as live once that has happened, so this waits for it to leave the live
    # set — and says so plainly if it never does.
    budget = float(os.environ.get("BIFROST_LIVE_RUNNING_TIMEOUT", "180"))
    waited = 0.0
    while waited < budget:
        listed = await jp_fetch("bifrost", "clusters")
        live = [
            c["id"]
            for c in json.loads(listed.body)["clusters"]
            if c.get("state") not in ("terminated", "terminating", "pending")
        ]
        if cluster_id not in live:
            return
        await asyncio.sleep(5)
        waited += 5
    pytest.fail(f"{cluster_id} was still listed as live {budget:.0f}s after stop")


async def test_live_a_token_is_never_echoed(jp_fetch):
    """No route may put the credential in a response body."""
    token = os.environ["BIFROST_TOKEN"]
    for path in (("bifrost", "profiles"), ("bifrost", "clusters")):
        resp = await jp_fetch(*path)
        assert token not in resp.body.decode()
