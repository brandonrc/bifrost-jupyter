# checkmaite on Bifrost: the three paths and their deltas

Written 2026-09-09 against grace (Bifrost `--ray-autoscaling`, KubeRay in-tree
autoscaler, Kueue v0.19.1, JupyterHub with this extension, checkmaite 0.3.0 in the
notebook image `jupyter-ray:2.56.0-r5` and the cluster image
`checkmaite-api:2.56.0-r1`, checkmaite API `checkmaite-api:2.56.0-r2-mobula`).
What works is what the sim lane (grace-e2e) drives; what does not is listed with
where the fix lives.

## 1. A notebook user runs checkmaite on their own cluster

**How it works.** The sidebar (or `examples/notebooks/01-my-cluster.ipynb`) starts a
cluster from the `checkmaite` profile. From the kernel, checkmaite's own job backend
is pointed at the cluster's Ray Client port:

```python
from bifrost_jupyter._address import ray_client_address
configure_job_backend("ray", address=ray_client_address(cluster_id, "bifrost"),
                      analytics_store={"backend": "parquet", "uri": "/app/data/analytics/notebooks/alice"},
                      idempotency_scope="notebook-alice")
job = submit_capability(DataevalCleaning(), datasets=[ds]); ref = job.result()
```

Nothing carries a credential: `ray://<id>-head-svc.bifrost.svc:10001` is reachable
only from the owner's notebook pod (the per-owner NetworkPolicy Bifrost writes), so
reachability *is* the authorization. Everything after `configure_job_backend` is
plain checkmaite.

**Deltas.**

| # | delta | where | status |
|---|---|---|---|
| 1 | A profile could not carry storage, so a sidebar-started `checkmaite` cluster had no analytics volume: no datasets on the nodes, results nowhere durable. | Bifrost `ProfileSpec.storage` | bifrost#44; then set `storage: [checkmaite-analytics]` on the profile and widen the entry's `projects` to team-a/team-b |
| 2 | With `--ray-autoscaling`, a project in a **non-elastic Kueue pool** had every cluster refused by Kueue's webhook. Fixed: such clusters are fixed-size; only pool-less or elastic-pool clusters autoscale, and elastic pools need Kueue's `ElasticJobsViaWorkloadSlices` gate (grace's Kueue does not enable it). | Bifrost `provision.EffectiveAutoscaling` | bifrost#43 |
| 3 | checkmaite's `ray_jobs` backend (Ray Jobs API over HTTP, which could go through Bifrost's authenticated gateway instead of the pod-only Ray Client) exists only in a **Draft MR** (checkmaite !617; token exchange !619). The notebook image's checkmaite 0.3.0 has `ray` and `ray-simple` only. | checkmaite | unmerged upstream; the notebooks use the Ray Client on purpose |
| 4 | Version parity is a hard requirement (capability code ships by reference): Python 3.12 / Ray 2.56 / checkmaite 0.3.0 on both images today. A `rayproject/ray` profile cannot run checkmaite jobs. Notebook 02 checks and stops. | image builds | ok today; keep the two images on one BOM |
| 5 | The analytics volume is `0750 uid 999` (`app` in both images). A profile on a uid-1000 image (rayproject) cannot write it. | image / hostPath perms | constraint, documented |
| 6 | Cluster image has `polars` (parquet works) but not `pyarrow`, `s3fs`, `datamaite`; an S3 `analytics_store` would need `s3fs`. | checkmaite-api image | add if S3 stores are wanted |
| 7 | `import checkmaite` writes `~/.cache/checkmaite`; fine in pods (writable `$HOME`), fails in a bare `docker run`. | checkmaite | cosmetic |
| 8 | **Concurrency is bounded by head memory, not CPU — and the cost is checkmaite's controllers.** The `ray` job backend runs one `JobController` actor per job plus one `JobRegistry` per idempotency scope, all on the head (`num_cpus=0.01`), each **~0.9 GiB RSS** (they import torch), retained for **3600 s** after the job ends (`DEFAULT_CONTROLLER_RETENTION_S`, up to 1000 of them). Nine controllers + two registries measured at 11.9 GiB on the 8 GiB head; Ray's memory monitor then OOM-killed two of eight concurrent runs, and a later submit failed outright at 7.58/8.00 GiB. Mitigations the notebooks apply: `controller_retention_s=30`, `max_retained_terminal_controllers=2`, one scope per user, at most 3 runs in flight, and 2 CPUs per run so the runs themselves queue for workers rather than crowd the head. Upstream: a lighter controller (no torch import) or a head-sized default retention. | checkmaite `jobs.backends.ray` | finding; worth an upstream issue |
| 9 | The head's Ray Client proxier forks a server per connection; under quick successive connects it occasionally dies at birth (`ev_epoll1_linux.cc: Check failed: next_worker->state == KICKED`) and the client sees `ConnectionAbortedError`. The notebooks reuse one connection where they can and retry the reconnect. | Ray | upstream flake; retry |

## 2. A UI user runs checkmaite through the frontend API

**How it works.** checkmaite's API (`checkmaite-frontend/api` is its consumer) runs
with `CHECKMAITE_JOB_BACKEND=ray-jobs` against `https://checkmaite-jobs.ray.<ip>.sslip.io`
— Bifrost's per-cluster gateway route for the **shared** `checkmaite-jobs` cluster
(owner `checkmaite-svc`, project `checkmaite`). Every UI user's run lands on that one
cluster; `sim/checkmaite-load.spec.ts` drives ten runs each for alice and bob and
checks every run's recorded owner.

**Deltas.**

| # | delta | where | status |
|---|---|---|---|
| 10 | The deployed API is a dev build (`0.3.0.post1.dev0+293621c`) carrying the unmerged `ray_jobs` backend (!617) and the gateway token exchange (!619). Release checkmaite cannot talk to Bifrost's gateway. | checkmaite | unmerged upstream |
| 11 | The shared cluster is **not autoscaled**: created before the flag (generation 4, `enableInTreeAutoscaling: false`, workers fixed at 1 of max 3). Re-applying it under autoscaling (a spec bump) restarts the head; the API's jobs backend reconnects, in-flight runs fail. | grace operations | do it in a quiet window, then re-run `checkmaite-load` for the scaling evidence |
| 12 | Isolation on this path is checkmaite's, not Bifrost's: one owner, one cluster, one NetworkPolicy. Per-user quotas would have to be checkmaite-side or the API would need to start per-user clusters (path 1's model). | design | known; acceptable for a shared service account |

## 3. Users only reach their own resources; does it scale?

- **Reach.** alice's pod reaches alice's head; bob's request to alice's cluster fails
  (asserted by `extension-lifecycle`). The dashboard and `/usage` are project-scoped
  (bifrost#35/#39). Grafana is read-only for notebook users (Viewer), Admin for the
  platform admin.
- **Limits.** grace has no quotas or admission rules set (`quotas: {}`), so the only
  caps are the profiles' `max_workers: 2` and the node. Pools exist (`demo-pool`) but
  team-a/team-b are not allocated to one — which is also why their clusters autoscale
  (see delta 2).
- **Scaling.** Evidence is the sampler's per-cluster worker series plus, for the
  notebook path, `03-stress-ramp.ipynb`'s own timeline: workers `0 → 2 → 0` under a
  ramp of checkmaite runs, scale-down about a minute after the last run (KubeRay's
  idle timeout). Run 7 of `extension-lifecycle` recorded the same shape under 20 jobs.
