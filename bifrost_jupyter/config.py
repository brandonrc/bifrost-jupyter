"""Server-extension configuration (namespace of the provisioned RayClusters).

The in-cluster head-service address is derived from the cluster id and the
Kubernetes namespace Bifrost provisions into (``<id>-head-svc.<namespace>.svc``),
so the namespace is the one piece of deployment context the extension needs.
It is a traitlet (configurable via ``c.BifrostConfig.cluster_namespace`` in
``jupyter_server_config.py``) that defaults from ``BIFROST_CLUSTER_NAMESPACE``,
else a sane fixed default.
"""

from __future__ import annotations

import os

from traitlets import List, Unicode, default
from traitlets.config import Configurable

NAMESPACE_ENV_VAR = "BIFROST_CLUSTER_NAMESPACE"
DEFAULT_NAMESPACE = "bifrost"


def default_namespace() -> str:
    """Namespace default shared by the traitlet and the kernel-side helper."""
    return os.environ.get(NAMESPACE_ENV_VAR) or DEFAULT_NAMESPACE


class BifrostConfig(Configurable):
    """Configurable settings for the bifrost-jupyter server extension."""

    cluster_namespace = Unicode(
        help=(
            "Kubernetes namespace Bifrost provisions RayClusters into. The "
            "in-cluster Ray head service is <id>-head-svc.<namespace>.svc."
        ),
    ).tag(config=True)

    @default("cluster_namespace")
    def _cluster_namespace_default(self) -> str:
        return default_namespace()

    profiles: List = List(
        trait=None,  # list of profile dicts; validated by _profiles.resolve_profiles
        default_value=[],
        help=(
            "Admin-approved cluster profiles (the allowlist). Each entry is a "
            "dict with name/description/image/ray_version/head_cpu/head_memory/"
            "ttl_seconds and a non-empty worker_groups list (name/cpu/memory/"
            "replicas/min_replicas/max_replicas, optional gpu). An 'owner' field "
            "is ignored — Bifrost stamps the owner from the token. Leave empty to "
            "use the built-in small/medium/gpu defaults; a non-empty list fully "
            "replaces them."
        ),
    ).tag(config=True)
