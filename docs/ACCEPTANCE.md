# Acceptance — Wave 2-C

The Task 3 spike's loop ("one call spins a cluster and a job runs"), generalised
to the full panel UX: **start → status → connect cell → submit a job with env
vars → suspend/resume → stop**, plus the two authorization behaviours the design
calls out as make-or-break (the operator-role prerequisite, and credential
expiry).

Two gates:

| Gate                              | Runs                                           | Proves                                                         |
| --------------------------------- | ---------------------------------------------- | -------------------------------------------------------------- |
| **A. Dev Bifrost, auth enforced** | reproducible locally, and suitable for CI      | the whole control-plane loop and both authorization behaviours |
| **B. Nebari + Keycloak**          | manual / `workflow_dispatch` — **not** push CI | OIDC passthrough, owner-match, and the in-cluster Ray paths    |

## The one non-negotiable rule

**Never run acceptance against `--dev-allow-unauthenticated`.** That flag makes
`identity == nil`, which short-circuits every authorization check in Bifrost. A
Wave 2-C task once "live-verified" a feature that only worked for that reason;
the finding was hollow and had to be retracted. Gate A therefore runs Bifrost
with `--local-auth` and drives the extension with a **real `mob_` Bearer**, and
the transcripts below are from exactly that.

## Gate A — dev Bifrost with auth enforced

### Setup

```bash
# 1. Bifrost: local auth on, sqlite store, loopback bind. No --dev-allow-unauthenticated.
go build -o /tmp/bifrost ./cmd/bifrost          # in the bifrost repo
BIFROST_LOCAL_ADMIN_PASSWORD='<demo-password>' /tmp/bifrost serve \
  --local-auth --store sqlite --db /tmp/bifrost.db --bind 127.0.0.1:8585

# 2. Two users, so the role prerequisite is actually exercised.
#    admin logs in, creates them; each logs in for its own PAT.
POST /api/v1/auth/login   {"username":"admin","password":"…"}
POST /api/v1/auth/users   {"username":"nbuser","password":"…","role":"operator"}
POST /api/v1/auth/users   {"username":"nbdev","password":"…","role":"developer"}

# 3. The extension, installed from the built wheel into a clean venv:
pip install dist/bifrost_jupyter-*.whl jupyterlab
BIFROST_API_URL=http://127.0.0.1:8585 BIFROST_TOKEN=<operator PAT> \
  jupyter server --port 8899 --ServerApp.token=<jupyter token>
```

Then drive the panel's own routes — the same requests `src/api.ts` issues.

### A1. The full loop (operator PAT)

Recorded run:

```
1. GET profiles (dropdown)         HTTP 200  {"names": ["small", "medium", "gpu"]}
2. POST clusters (Start)           HTTP 200  {"id": "jl-small-17546c5b3a5f", "status": "running"}
3. GET clusters (status poll)      HTTP 200  {"clusters": [{"id": "jl-small-17546c5b3a5f",
                                              "state": "running"}], "configured": true}
4. GET address (Connect cell)      HTTP 200  {"jobs_address":
                                    "http://jl-small-17546c5b3a5f-head-svc.bifrost.svc:8265",
                                    "ray_client_address":
                                    "ray://jl-small-17546c5b3a5f-head-svc.bifrost.svc:10001"}
   injected cell:
     from ray.job_submission import JobSubmissionClient
     client = JobSubmissionClient("http://jl-small-17546c5b3a5f-head-svc.bifrost.svc:8265")
5. POST jobs (env vars, #11)       HTTP 502  {"error": "ray cluster unreachable"}   [see note]
6. POST suspend                    HTTP 409  {"error": "conflict"}                  [see note]
7. POST resume                     HTTP 409  {"error": "conflict"}                  [see note]
8. DELETE cluster (Stop)           HTTP 202  {"id": "jl-small-17546c5b3a5f", "status": "stopping"}
9. GET clusters (after stop)       HTTP 200  {"clusters": [{"id": "jl-small-17546c5b3a5f",
                                              "state": "terminated"}], "configured": true}
```

Bifrost's audit trail for the same run, showing the calls were authorized
against a real identity rather than waved through:

```
audit event decision=allow subject=nbuser action=create_cluster cluster=jl-small-… status=201
audit event decision=deny  subject=nbuser reason=illegal_state_transition action=suspend_cluster status=409
audit event decision=allow subject=nbuser action=delete_cluster cluster=jl-small-… status=202
```

**Notes on steps 5–7 — environment, not defects.**

- Step 5 needs a real Ray head service on `:8265`. Without KubeRay there is none,
  and the extension answers the documented `502 ray cluster unreachable`. Env
  vars reaching `runtime_env.env_vars` is covered by unit tests against a faked
  Ray Jobs API; a _live_ job run is Gate B (or a kind-hosted KubeRay).
- Steps 6–7: run without `--namespace`, Bifrost has no reconciler, so
  `observed_state` is never set and its state machine refuses the transition
  (`illegal_state_transition`). The panel's status therefore shows the _desired_
  state via its documented fallback. The extension behaved correctly — it
  relayed Bifrost's 409 as a clean `{"error": "conflict"}`. Running Bifrost with
  `--namespace` against a real KubeRay is what makes 6–7 succeed.

### A2. The operator-role prerequisite (T3 carry-forward)

Same server, restarted with the **developer** PAT:

```
POST /bifrost/clusters {"profile":"small"}   HTTP 403
{"error": "forbidden: your Bifrost identity may not manage clusters. Cluster lifecycle
  needs Write on 'cluster' — the 'operator' role. Ask a Bifrost admin to map your IdP
  group to operator (roles.operator or project_roles.operator in Bifrost's auth config)"}

GET  /bifrost/clusters                       HTTP 200   (Read still works)
```

Bifrost's side of the same request:

```
audit event decision=deny subject=nbdev reason=insufficient_permission
            status=403 required_action=write required_target=cluster granted=[developer]
```

This is the failure that makes the extension unusable for a Nebari user whose
IdP group is not mapped to operator, and it now says so.

### A3. Credential expiry refreshes, then reports (Task 9)

With the extension holding a valid PAT, the PAT is revoked server-side
mid-session (`POST /api/v1/auth/logout`), then the panel route is called again:

```
before   GET /bifrost/clusters   HTTP 200   {"clusters": […], "configured": true}
revoke   POST /api/v1/auth/logout           HTTP 204
after    GET /bifrost/clusters   HTTP 401
{"error": "unauthorized: bifrost rejected this session's credential even after
  refreshing it (see 'Authentication' in the bifrost-jupyter README)"}
```

Bifrost logged **two** denials for that single panel request:

```
access denied decision=deny reason=invalid_token method=GET path=/api/v1/clusters
access denied decision=deny reason=invalid_token method=GET path=/api/v1/clusters
```

— the refresh-and-retry, visible at the wire. The user gets an actionable 401,
not an unexplained failure and not a 500.

### A4. Bare install degrades, and a hostile id is refused

From the same wheel install with **no** `BIFROST_API_URL`/`BIFROST_TOKEN`:

```
GET /bifrost/clusters                                   HTTP 200  {"clusters": [], "configured": false}
GET /bifrost/clusters/evil.example%3A9999%3F/address    HTTP 400  {"error": "invalid cluster id"}
```

with **zero** `[E]` lines in the ServerApp log — the state a bare `pip install`
lands in must not break JupyterLab's startup (the Task 5 regression) and must
not open the Task 8 SSRF.

## Gate B — Nebari + Keycloak (manual / dispatch)

Everything below needs a real hub, a real IdP and a real KubeRay, so it is a
**manual or `workflow_dispatch` gate**, like Bifrost's contract replay — never
push CI.

Prerequisites are in the README: `enable_auth_state: true` + the spawner hook,
and the operator role mapping.

1. **OIDC passthrough.** From a Keycloak-authenticated notebook, with no
   `BIFROST_TOKEN` set at all, the panel starts a cluster. Proves the pod's
   `auth_state` token authenticated the call.
2. **Owner-match — and note how it must be checked.** `ClusterView` exposes no
   `owner` field (`id`, `project`, `engine`, `ray_version`, `desired`,
   `observed_state`, `generation`, `observed_generation`, `condition`,
   `est_*_hourly` — verified against `openapi.json`), so owner-match **cannot be
   observed through the Bifrost API at all**. Check it in-cluster:

   ```bash
   kubectl get raycluster <id> -n <ns> -o jsonpath='{.metadata.labels.bifrost\.dev/owner}'
   kubectl get pod <singleuser-pod> -o jsonpath='{.metadata.labels.bifrost\.dev/owner}'
   ```

   They must be equal. If they are not, the tell is asymmetric and easy to
   misread: the control plane keeps working while every in-cluster call fails.

3. **The in-cluster paths.** Submit a job with env vars through the panel and see
   it run; open the dashboard tab and confirm the iframe paints (never yet
   rendered against a live Ray — flagged at Task 8); run the injected connect
   cell in a kernel.
4. **Ray Client `:10001`.** The advanced path in the connect snippet. Expected to
   work only from the owner's own notebook pod.

## Status

| Behaviour                             | Gate A (live here)                                 | Gate B (pending)                   |
| ------------------------------------- | -------------------------------------------------- | ---------------------------------- |
| profiles → panel dropdown             | ✅                                                 |                                    |
| start / status / stop                 | ✅                                                 |                                    |
| connect address + snippet             | ✅ (derivation)                                    | ✅ snippet runs in a kernel        |
| suspend / resume                      | relayed correctly; needs a reconciler              | ✅ with `--namespace` + KubeRay    |
| job submit with env vars (#11)        | unit-tested; live route reachable, upstream absent | ✅ job actually runs               |
| dashboard proxy                       | unit-tested; route mounted                         | ✅ iframe paints                   |
| operator-role 403                     | ✅                                                 | ✅ via IdP group mapping           |
| credential refresh → actionable 401   | ✅                                                 | ✅ with a real expiring OIDC token |
| OIDC passthrough / exchange / refresh | unit-tested against a faked IdP                    | ✅ against Keycloak                |
| owner-match                           | not observable via the API                         | ✅ only via the k8s labels         |
| wheel install enables both extensions | ✅                                                 |                                    |
