"""Profile → CreateCluster mapping (design §5, §7)."""

import json

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


def test_unknown_profile_raises_unknown_profile_error():
    # A clear, typed error — not a default fallback.
    with pytest.raises(_profiles.UnknownProfileError) as exc:
        _profiles.profile_to_spec("enormous")
    assert "unknown profile" in str(exc.value)
    assert "enormous" in str(exc.value)


# ---- Invariants that must hold for EVERY default profile --------------------


@pytest.mark.parametrize("name", sorted(_profiles.DEFAULT_PROFILES))
def test_every_profile_sets_ttl_and_omits_owner(name):
    body = _profiles.profile_to_spec(name)
    assert body.spec.ttl_seconds is not None and body.spec.ttl_seconds > 0
    assert body.spec.owner is None
    assert "owner" not in body.spec.to_dict()


@pytest.mark.parametrize("name", sorted(_profiles.DEFAULT_PROFILES))
def test_every_profile_maps_to_valid_body(name):
    body = _profiles.profile_to_spec(name)
    assert body.id == body.spec.name
    assert body.spec.image and body.spec.ray_version
    assert body.spec.head_cpu and body.spec.head_memory
    assert body.spec.worker_groups
    for wg in body.spec.worker_groups:
        assert wg.min_replicas <= wg.replicas <= wg.max_replicas


# ---- Safe listing view (no raw manifest surface) ----------------------------


def test_list_profiles_returns_safe_view():
    views = _profiles.list_profiles()
    names = {v.name for v in views}
    assert {"small", "medium", "gpu"} <= names
    blob = json.dumps([v.to_dict() for v in views])
    # The view exposes the coarse shape...
    assert "head_cpu" in blob and "description" in blob
    # ...but never the image / ray_version / raw manifest surface.
    assert "image" not in blob
    assert "ray_version" not in blob
    assert "rayproject/ray" not in blob


def test_gpu_profile_view_reports_gpu_count():
    view = next(v for v in _profiles.list_profiles() if v.name == "gpu")
    assert view.gpu >= 1
    cpu_view = next(v for v in _profiles.list_profiles() if v.name == "small")
    assert cpu_view.gpu == 0


# ---- Generated-id length bound (KubeRay head-svc truncation guard) ----------


def test_generated_ids_stay_within_length_bound():
    for name in _profiles.DEFAULT_PROFILES:
        assert len(_profiles.profile_to_spec(name).id) <= _profiles._MAX_ID_LEN


def test_long_profile_name_id_is_bounded_and_dns_safe():
    # A deployment could name a profile far longer than the defaults; the id
    # must still be <= the bound so KubeRay does not truncate the head service.
    long_name = "x" * 100
    profiles = {
        long_name: _profiles.Profile(
            name=long_name,
            description="huge name",
            image="img:1",
            ray_version="2.9.0",
            head_cpu="1",
            head_memory="2Gi",
            ttl_seconds=3600,
            worker_groups=(
                _profiles.WorkerGroupShape(
                    name="w", cpu="1", memory="2Gi", replicas=1, min_replicas=1, max_replicas=1
                ),
            ),
        )
    }
    cid = _profiles.profile_to_spec(long_name, profiles).id
    assert len(cid) <= _profiles._MAX_ID_LEN
    assert cid.startswith("jl-")


# ---- Config resolution ------------------------------------------------------


def test_resolve_profiles_defaults_when_empty():
    assert _profiles.resolve_profiles([]) == _profiles.DEFAULT_PROFILES
    assert _profiles.resolve_profiles(None) == _profiles.DEFAULT_PROFILES


def test_resolve_profiles_replaces_defaults():
    resolved = _profiles.resolve_profiles(
        [
            {
                "name": "only",
                "description": "the only one",
                "image": "img:1",
                "ray_version": "2.9.0",
                "head_cpu": "1",
                "head_memory": "2Gi",
                "ttl_seconds": 1800,
                "worker_groups": [
                    {
                        "name": "w",
                        "cpu": "1",
                        "memory": "2Gi",
                        "replicas": 1,
                        "min_replicas": 1,
                        "max_replicas": 1,
                    }
                ],
            }
        ]
    )
    assert set(resolved) == {"only"}
    assert resolved["only"].ttl_seconds == 1800


def test_resolve_profiles_defaults_ttl_and_ignores_owner():
    # ttl_seconds is always set even if config omits it; an 'owner' key is ignored.
    resolved = _profiles.resolve_profiles(
        [
            {
                "name": "nottl",
                "image": "img:1",
                "ray_version": "2.9.0",
                "head_cpu": "1",
                "head_memory": "2Gi",
                "owner": "attacker",
                "worker_groups": [
                    {
                        "name": "w",
                        "cpu": "1",
                        "memory": "2Gi",
                        "replicas": 1,
                        "min_replicas": 1,
                        "max_replicas": 1,
                    }
                ],
            }
        ]
    )
    assert resolved["nottl"].ttl_seconds == _profiles._DEFAULT_TTL_SECONDS
    body = _profiles.profile_to_spec("nottl", resolved)
    assert body.spec.ttl_seconds == _profiles._DEFAULT_TTL_SECONDS
    assert body.spec.owner is None  # 'owner' in config never reaches the spec


def test_resolve_profiles_rejects_missing_fields():
    with pytest.raises(ValueError):
        _profiles.resolve_profiles([{"name": "bad"}])


def test_resolve_profiles_rejects_no_worker_groups():
    with pytest.raises(ValueError):
        _profiles.resolve_profiles(
            [
                {
                    "name": "bad",
                    "image": "img:1",
                    "ray_version": "2.9.0",
                    "head_cpu": "1",
                    "head_memory": "2Gi",
                    "worker_groups": [],
                }
            ]
        )


def test_resolve_profiles_rejects_duplicate_names():
    entry = {
        "name": "dup",
        "image": "img:1",
        "ray_version": "2.9.0",
        "head_cpu": "1",
        "head_memory": "2Gi",
        "worker_groups": [
            {
                "name": "w",
                "cpu": "1",
                "memory": "2Gi",
                "replicas": 1,
                "min_replicas": 1,
                "max_replicas": 1,
            }
        ],
    }
    with pytest.raises(ValueError):
        _profiles.resolve_profiles([entry, dict(entry)])
