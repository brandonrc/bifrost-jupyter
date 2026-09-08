"""Profile name → CreateCluster body, against the catalog Bifrost hands back."""

import pytest
from bifrost_client.models.profile_spec import ProfileSpec
from bifrost_client.models.worker_group import WorkerGroup

from bifrost_jupyter import _profiles


def spec(name: str, *, gpu: str | None = None, max_replicas: int = 2, **overrides) -> ProfileSpec:
    base = ProfileSpec(
        name=name,
        description=f"the {name} shape",
        image="rayproject/ray:2.56.0",
        ray_version="2.56.0",
        head_cpu="1",
        head_memory="2Gi",
        ttl_seconds=3600,
        worker_groups=[
            WorkerGroup(
                name="w",
                cpu="1",
                memory="2Gi",
                gpu=gpu,
                replicas=1,
                min_replicas=0,
                max_replicas=max_replicas,
            )
        ],
    )
    return base.model_copy(update=overrides) if overrides else base


SMALL_SPEC = spec("small")
CATALOG = [SMALL_SPEC, spec("gpu", gpu="1", max_replicas=2)]


def test_a_name_from_the_catalog_maps_to_a_create_cluster():
    body = _profiles.profile_to_spec("small", CATALOG, project="team-a")

    assert body.id
    assert body.id == body.spec.name  # id is the RayCluster name / routing key
    assert body.spec.project == "team-a"
    assert body.spec.profile == "small"


def test_the_body_carries_the_name_and_no_shape():
    # "Zero-valued fields are filled from the profile; conflicting non-empty
    # fields are refused" — so the client sends nothing that could conflict.
    # The shape, the image, the Ray version, the TTL are Bifrost's to decide.
    body = _profiles.profile_to_spec("small", CATALOG, project="team-a")
    assert body.spec.image == ""
    assert body.spec.ray_version == ""
    assert body.spec.head_cpu == "" and body.spec.head_memory == ""
    assert body.spec.worker_groups == []
    assert body.spec.ttl_seconds is None
    assert body.spec.idle_timeout_secs is None


def test_owner_is_not_set():
    # Bifrost stamps the owner from the request identity; never from the body.
    body = _profiles.profile_to_spec("small", CATALOG, project="team-a")
    assert body.spec.owner is None


def test_ids_are_unique_per_call():
    a = _profiles.profile_to_spec("small", CATALOG, project="team-a")
    b = _profiles.profile_to_spec("small", CATALOG, project="team-a")
    assert a.id != b.id


def test_explicit_cluster_id_is_honored():
    body = _profiles.profile_to_spec("small", CATALOG, cluster_id="my-cluster", project="team-a")
    assert body.id == "my-cluster"
    assert body.spec.name == "my-cluster"


def test_a_body_without_a_project_is_refused():
    with pytest.raises(ValueError, match="project"):
        _profiles.profile_to_spec("small", CATALOG)


def test_a_name_the_catalog_does_not_carry_is_refused():
    # Never a fallback: the catalog is what this caller may start, and a name
    # outside it is answered with what is inside it.
    with pytest.raises(_profiles.UnknownProfileError) as exc:
        _profiles.profile_to_spec("enormous", CATALOG, project="team-a")
    assert exc.value.name == "enormous"
    assert exc.value.available == ["gpu", "small"]
    assert "enormous" in str(exc.value) and "small" in str(exc.value)
    assert isinstance(exc.value, KeyError)


def test_an_empty_catalog_refuses_everything():
    with pytest.raises(_profiles.UnknownProfileError):
        _profiles.profile_to_spec("small", [], project="team-a")


def test_list_profiles_returns_the_safe_view():
    views = _profiles.list_profiles(CATALOG)
    by_name = {v.name: v.to_dict() for v in views}
    assert set(by_name) == {"small", "gpu"}
    small = by_name["small"]
    assert small["description"] == "the small shape"
    assert small["head_cpu"] == "1" and small["head_memory"] == "2Gi"
    assert small["workers"] == [
        {"cpu": "1", "memory": "2Gi", "gpu": None, "min_replicas": 0, "max_replicas": 2}
    ]
    # Never the manifest surface.
    for view in by_name.values():
        assert "image" not in view and "ray_version" not in view


def test_gpu_profile_view_reports_gpu_count_at_max_scale():
    views = {v.name: v for v in _profiles.list_profiles(CATALOG)}
    assert views["small"].gpu == 0
    assert views["gpu"].gpu == 2  # 1 GPU × max 2 replicas


def test_a_profile_without_a_description_renders_an_empty_one():
    views = _profiles.list_profiles([spec("bare", description=None)])
    assert views[0].to_dict()["description"] == ""


def test_generated_ids_stay_within_length_bound():
    body = _profiles.profile_to_spec("small", CATALOG, project="team-a")
    assert len(body.id) <= _profiles._MAX_ID_LEN


def test_long_profile_name_id_is_bounded_and_dns_safe():
    name = "A Very Long Profile Name With Spaces And CAPS And !!! punctuation"
    body = _profiles.profile_to_spec(name, [spec(name)], project="team-a")
    assert len(body.id) <= _profiles._MAX_ID_LEN
    assert body.id.startswith("jl-")
    assert body.id == body.id.lower()
    assert "--" not in body.id
    assert not body.id.endswith("-")
