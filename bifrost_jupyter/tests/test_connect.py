"""connect() derives the in-cluster address and makes NO Bifrost call."""

import sys
import types

import pytest

from bifrost_jupyter.connect import connect


@pytest.fixture
def fake_ray(monkeypatch):
    """Install a fake ``ray.job_submission.JobSubmissionClient`` that records args."""
    calls = {}

    class FakeJSC:
        def __init__(self, address, *args, **kwargs):
            calls["address"] = address
            calls["args"] = args
            calls["kwargs"] = kwargs

    ray_pkg = types.ModuleType("ray")
    js_mod = types.ModuleType("ray.job_submission")
    js_mod.JobSubmissionClient = FakeJSC
    ray_pkg.job_submission = js_mod
    monkeypatch.setitem(sys.modules, "ray", ray_pkg)
    monkeypatch.setitem(sys.modules, "ray.job_submission", js_mod)
    return calls


def test_connect_uses_in_cluster_address_and_no_auth(fake_ray, monkeypatch):
    # No Bifrost credentials set at all: if connect made a control-plane/registry
    # call it would need these and fail. It must not.
    monkeypatch.delenv("BIFROST_API_URL", raising=False)
    monkeypatch.delenv("BIFROST_TOKEN", raising=False)
    monkeypatch.delenv("BIFROST_CLUSTER_NAMESPACE", raising=False)

    connect("cl-1")

    assert fake_ray["address"] == "http://cl-1-head-svc.bifrost.svc:8265"
    # No auth header is passed to the Ray client for the in-cluster path.
    assert fake_ray["args"] == ()
    assert fake_ray["kwargs"] == {}


def test_connect_honors_namespace(fake_ray):
    connect("cl-1", namespace="team-x")
    assert fake_ray["address"] == "http://cl-1-head-svc.team-x.svc:8265"


def test_connect_namespace_from_env(fake_ray, monkeypatch):
    monkeypatch.setenv("BIFROST_CLUSTER_NAMESPACE", "from-env")
    connect("cl-1")
    assert fake_ray["address"] == "http://cl-1-head-svc.from-env.svc:8265"
