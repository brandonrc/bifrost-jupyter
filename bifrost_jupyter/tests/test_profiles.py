"""Profile → CreateCluster mapping (design §5, §7)."""

import pytest

from bifrost_jupyter import _profiles


def test_small_profile_maps_to_create_cluster():
    body = _profiles.build_create_cluster(_profiles.SMALL)

    assert body.id
    assert body.id == body.spec.name  # id is the RayCluster name / routing key
    assert body.spec.image
    assert body.spec.ray_version
    assert body.spec.head_cpu and body.spec.head_memory
    assert len(body.spec.worker_groups) == 1
    wg = body.spec.worker_groups[0]
    assert wg.replicas >= wg.min_replicas
    assert wg.max_replicas >= wg.replicas


def test_ttl_seconds_is_set():
    # Interactive clusters submit no gateway jobs, so idle_timeout can't reap
    # them — ttl_seconds is the only reaper that can. It must be set.
    body = _profiles.build_create_cluster(_profiles.SMALL)
    assert body.spec.ttl_seconds is not None
    assert body.spec.ttl_seconds > 0


def test_owner_is_not_set():
    # owner is stamped control-plane-side from the token, never from the body.
    body = _profiles.build_create_cluster(_profiles.SMALL)
    assert body.spec.owner is None
    # And it must not appear in the serialized wire body either.
    assert "owner" not in body.spec.to_dict()


def test_ids_are_unique_per_call():
    a = _profiles.build_create_cluster(_profiles.SMALL)
    b = _profiles.build_create_cluster(_profiles.SMALL)
    assert a.id != b.id


def test_explicit_cluster_id_is_honored():
    body = _profiles.build_create_cluster(_profiles.SMALL, cluster_id="fixed-id")
    assert body.id == "fixed-id"
    assert body.spec.name == "fixed-id"


def test_project_from_env(monkeypatch):
    monkeypatch.setenv("BIFROST_PROJECT", "team-x")
    body = _profiles.build_create_cluster(_profiles.SMALL)
    assert body.spec.project == "team-x"


def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        _profiles.build_create_cluster("enormous")
