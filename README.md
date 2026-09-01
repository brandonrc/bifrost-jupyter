# bifrost_jupyter

[![Github Actions Status](https://github.com/brandonrc/bifrost-jupyter/workflows/Build/badge.svg)](https://github.com/brandonrc/bifrost-jupyter/actions/workflows/build.yml)

JupyterLab extension to start, stop, and connect to Bifrost-fronted Ray clusters

This extension is composed of a Python package named `bifrost_jupyter`
for the server extension and a NPM package named `bifrost-jupyter`
for the frontend extension.

The Python `jupyter-server` extension is a same-origin proxy: it holds the user
credential server-side and forwards it to Bifrost as a bearer token (Bifrost
emits no CORS headers, so a browser-only extension cannot call it directly). The
TS labextension talks only to its co-located server routes under `/bifrost/*`.
A kernel-side helper (`from bifrost_jupyter import connect`) returns a
preconfigured Ray `JobSubmissionClient` pointed at the cluster's **in-cluster
head service** (`http://<id>-head-svc.<namespace>.svc:8265`) — not a Bifrost
gateway host, per the design §2 amendment.

## Authentication

The extension never puts a credential in the browser. The Python server
extension resolves one inside the user's notebook pod and attaches it as
`Authorization: Bearer …` on **Bifrost control-plane calls only** (start, list,
stop, suspend, resume). The in-cluster paths — job submit, job status, the
dashboard proxy — deliberately carry no credential at all; see the sections
below.

### Credential precedence

Highest first. The first one that resolves wins:

| #   | Source                                                                   | Environment                                                                                                                                 |
| --- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | OIDC access token injected by JupyterHub's `auth_state` hook             | `BIFROST_OIDC_TOKEN_FILE`, else `BIFROST_OIDC_TOKEN`, else `ACCESS_TOKEN`                                                                   |
| 2   | …renewed at the IdP when it has expired (`grant_type=refresh_token`)     | `BIFROST_OIDC_REFRESH_TOKEN_FILE` / `BIFROST_OIDC_REFRESH_TOKEN` + `BIFROST_OIDC_TOKEN_URL`                                                 |
| 3   | …exchanged for a Bifrost-audience token (RFC 8693) when audiences differ | `BIFROST_OIDC_AUDIENCE` (+ `BIFROST_OIDC_TOKEN_URL`, `BIFROST_OIDC_CLIENT_ID`, `BIFROST_OIDC_CLIENT_SECRET`, optional `BIFROST_OIDC_SCOPE`) |
| 4   | …traded once per session for a Bifrost PAT — **opt-in, off by default**  | `BIFROST_MINT_PAT=1` (+ `BIFROST_PAT_TTL_DAYS`, default 1, capped at Bifrost's 90)                                                          |
| 5   | Dev / non-Hub fallback: a pasted `mob_` PAT                              | `BIFROST_TOKEN`                                                                                                                             |

Plus `BIFROST_API_URL`, the Bifrost control-plane base URL, which is required in
every case. With none of the credential variables set the extension reports
itself as _not configured_ — a normal state for a bare install — and the panel
shows a plain note rather than an error.

**OIDC beats the dev PAT.** If both are present the OIDC token is used, because
it is the identity that has to match the pod's owner label (below). Prefer the
`…_FILE` forms: an environment variable is frozen at spawn time, so only a file
can be rotated under a long-running session.

The token endpoint must be `https://` — the exchange sends the user's live
credential in the request body and receives a bearer in the response.

### Deployment prerequisite: `auth_state` injection (Nebari / JupyterHub)

This is **deployment configuration, not code**, and it is off by default in
JupyterHub. Without it there is no OIDC token in the pod and the extension falls
back to `BIFROST_TOKEN`.

```python
# jupyterhub_config.py (Nebari: the equivalent stanza in nebari-config.yaml's
# jupyterhub.overrides / hub.extraConfig)
c.Authenticator.enable_auth_state = True

async def bifrost_auth_state_hook(spawner, auth_state):
    if not auth_state:
        return
    spawner.environment["BIFROST_OIDC_TOKEN"] = auth_state["access_token"]
    # Optional, but this is what lets a long session survive token expiry:
    if auth_state.get("refresh_token"):
        spawner.environment["BIFROST_OIDC_REFRESH_TOKEN"] = auth_state["refresh_token"]
        spawner.environment["BIFROST_OIDC_TOKEN_URL"] = (
            "https://<keycloak>/realms/<realm>/protocol/openid-connect/token"
        )
        spawner.environment["BIFROST_OIDC_CLIENT_ID"] = "<hub client id>"

c.Spawner.auth_state_hook = bifrost_auth_state_hook
c.Spawner.environment.update({"BIFROST_API_URL": "https://<bifrost>"})
```

`enable_auth_state` also requires `JUPYTERHUB_CRYPT_KEY` to be set on the hub;
JupyterHub refuses to start with auth state enabled and no key.

### Prerequisite: the notebook user's role must grant cluster Write

Creating a cluster is `Write` on target `cluster` in Bifrost's RBAC, which only
the **operator** (or admin) role grants — `developer` and `viewer` get a `403`.
A Nebari notebook user's token must therefore carry operator, mapped from an IdP
group in Bifrost's auth config:

```json
{
  "issuer": "https://<keycloak>/realms/<realm>",
  "audience": "bifrost",
  "groups_claim": "groups",
  "roles": { "operator": ["/analysts"] },
  "project_roles": { "operator": ["*"], "strip_prefix": "/" }
}
```

`roles.operator` grants it globally to members of the listed groups;
`project_roles.operator` is the self-service form — a member of group `team-a`
gets operator scoped to `project:team-a`. Note the extension's profiles stamp a
fixed project (`BIFROST_PROJECT`, default `jupyter`), so with `project_roles`
that project name must match a group the user is in.

Without this mapping the extension is unusable, so the panel says so instead of
showing a bare "forbidden": a 403 on a lifecycle action reports that cluster
lifecycle needs the operator role and that an admin must map the group.

### The owner-match caveat

Bifrost stamps `ClusterSpec.owner` from the **request identity** —
`preferred_username` when the token carries one, else `sub` — and the per-owner
NetworkPolicy admits only the pod labeled `bifrost.dev/owner: <that owner>` to
the cluster's `:8265` (Jobs API + dashboard) and `:10001` (Ray Client). So **the
identity the extension presents must equal the identity that labels the notebook
pod**. OIDC passthrough is what guarantees that: the token is the user's own, so
`preferred_username` is the JupyterHub username the spawner labels the pod with.

If they diverge, the failure is quiet and confusing: the control plane still
works (clusters start, stop and list normally) while every in-cluster call —
job submit, job status, the dashboard — times out or is refused, because the
NetworkPolicy is keyed on an owner the pod does not carry. With the dev
`mob_` PAT this is the expected state whenever the PAT's subject is not the
notebook user.

### Why the session PAT mint is opt-in

The design called for minting a longer-lived Bifrost PAT at session start
(`POST /api/v1/auth/tokens`) and using it thereafter. Verified against the
Bifrost source, that is not usable on the production OIDC path:

- `CreateToken` calls `requireLocal()` first, so on an OIDC-only deployment (no
  `--local-auth`) it answers `404 local auth is not enabled`;
- with local auth enabled it mints via `IssueToken(identity.Subject, …)`, which
  requires a **local user row** whose username equals the OIDC `sub` (a Keycloak
  UUID) — absent, that is a `500`;
- and where such a row does exist, the resulting PAT authenticates as that local
  user, whose owner is the `sub`, not `preferred_username`. Using it would
  re-label new clusters with the UUID and silently break the owner match
  described above. Group-derived project roles are lost too, since local
  identities carry none.

So the OIDC path keeps the OIDC identity and handles lifetime by refreshing it.
`BIFROST_MINT_PAT=1` enables the mint for deployments where Bifrost's identity
for the PAT _is_ the pod owner; a mint that fails degrades to the OIDC token
with a warning rather than ending the session.

### Expiry is refreshed, never surfaced as a mystery 401

A credential is re-resolved when it is within 60s of expiry, and a `401` from
Bifrost triggers exactly one refresh-and-retry. When a refresh is impossible —
an expired access token with no refresh token in the pod — the panel gets a
`401` that says the token expired and what to do about it, not a bare
"unauthorized" or an unhandled 500.

## Submitting jobs with environment variables

A Ray `ClusterSpec` has no `env`/`runtime_env` field, so per-run environment
variables attach to a **job**, not a cluster. The panel's "Run job" action on a
running cluster opens an entrypoint field plus a key/value env-var editor, and
posts to:

```
POST /bifrost/clusters/{id}/jobs   {"entrypoint": "python train.py",
                                    "env_vars": {"HF_TOKEN": "..."}}
GET  /bifrost/clusters/{id}/jobs/{job_id}
```

The server extension forwards this to the cluster's own Ray Jobs REST API
(`POST http://<id>-head-svc.<namespace>.svc:8265/api/jobs/`) with the variables
under `runtime_env.env_vars`, and returns the Ray submission id.

Note this path does **not** go through Bifrost and carries **no bearer token**.
Two things stand in for one. The jupyter-server extension runs inside the user's
notebook pod, which carries the `bifrost.dev/owner` label, so the per-owner
NetworkPolicy admits it to the head service on `:8265`; **and** the cluster id is
validated to a single DNS label before it is interpolated into that host, which
pins the target to a head service in the configured namespace. Reachability alone
would not be authorization — the id is a path segment the caller controls, so an
unvalidated one would let the caller choose the host the _server_ connects to.
Every `/bifrost/clusters/{id}/...` route rejects a malformed id with a clean
`400 invalid cluster id` before making any upstream call. The Bifrost credential
stays on the control-plane routes (start/list/stop/suspend/resume).
Ray itself is **not** installed in the server environment for this: the Jobs
REST contract is spoken directly over HTTP. Ray stays the optional
`bifrost-jupyter[kernel]` extra, used only by the kernel-side `connect()`
helper.

## Viewing the Ray dashboard

Each running cluster gets a **Dashboard** button in the panel. It opens the
cluster's Ray dashboard in a JupyterLab tab, served **same-origin** by this
extension:

```
GET /bifrost/clusters/{id}/dashboard/         -> http://<id>-head-svc.<ns>.svc:8265/
GET /bifrost/clusters/{id}/dashboard/<path>   -> the same, sub-path and query preserved
GET /bifrost/clusters/{id}/dashboard          -> 302 to the trailing-slash form
```

Ray serves the dashboard on the _same_ port as the Jobs API (`:8265`), so this
reuses the same in-cluster address derivation and the same authorization story
as job submission: **no Bifrost bearer token is sent on this path** — the gate is
the per-owner NetworkPolicy that admits the notebook pod to the head service,
plus the validated cluster id that pins the target (see the note above). Nothing
from the browser is forwarded upstream either — in particular Jupyter's own
session cookie never reaches the Ray head — and only an allowlist of response
headers comes back.

Notes and deliberate limits:

- **Read-only.** Only `GET`/`HEAD` are proxied; any write verb gets a 405. Ray's
  dashboard has _write_ access to the cluster, and proxying writes would mean
  exempting the route from Jupyter's XSRF check, turning this into a CSRF path
  into the cluster. Use the panel's own job routes to act on a cluster.
- **Trailing slash matters.** Ray's dashboard resolves its assets and API calls
  relative to the document URL (it is built with `PUBLIC_URL="."` and routes with
  a `HashRouter`), which is why the mount point ends in `/` and the slash-less
  form redirects. Because of this, no asset-path rewriting is needed and
  `jupyter-server-proxy` is **not** a dependency.
- **Validated id.** A cluster id must be a single RFC 1123 DNS label
  (`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`), which is what the extension generates
  (`jl-<slug>-<12 hex>`). Anything else — a `:`, `?`, `#`, `@`, `%`, `/`, a dot,
  whitespace, uppercase, or over 63 characters — is a `400` before any connection
  is opened. Without this, an id could restructure the derived URL and choose the
  host the server connects to.
- **State-gated.** The button appears only for `running` clusters. A stopped,
  suspended or still-starting cluster has no head service to reach, and the route
  answers a clean `502 ray cluster unreachable` rather than failing loudly.
- **In-cluster only.** Like the jobs path, this works from a notebook running in
  the cluster (the Nebari target). Remote/off-cluster dashboard access is
  deferred with the rest of the remote-access story.

## Requirements

- JupyterLab >= 4.0.0
- The Bifrost Python client, [`bifrost_client`](https://github.com/brandonrc/bifrost-api)

### Prerequisite: `bifrost_client`

`bifrost_client` is **not published to PyPI**. It ships as a GitHub release
asset of the `bifrost-api` repo. This project depends on a pinned wheel via a
PEP 508 direct reference in `pyproject.toml`, so a normal install resolves it
automatically as long as `github.com` is reachable:

```
bifrost_client @ https://github.com/brandonrc/bifrost-api/releases/download/python-v0.1.4/bifrost_client-0.1.4-py3-none-any.whl
```

To install it on its own (pinned):

```bash
pip install https://github.com/brandonrc/bifrost-api/releases/download/python-v0.1.4/bifrost_client-0.1.4-py3-none-any.whl
```

> **Caveat:** the direct-reference install works because `bifrost-api` is
> currently a **public** repo. If `bifrost-api` is ever made private, this
> unauthenticated URL install will break (HTTP 404) and you will instead need an
> authenticated asset download (e.g. `gh release download python-v0.1.4 -R
brandonrc/bifrost-api` with a token, then `pip install` the local wheel).
> Bump the pinned version here and in `pyproject.toml` in lockstep with
> `bifrost_client` releases.

## Install

To install the extension, execute:

```bash
pip install bifrost_jupyter
```

The kernel-side helper (`from bifrost_jupyter import connect`) additionally
needs Ray, kept as an optional extra so it does not bloat the server env:

```bash
pip install "bifrost_jupyter[kernel]"
```

## Uninstall

To remove the extension, execute:

```bash
pip uninstall bifrost_jupyter
```

## Troubleshoot

If you are seeing the frontend extension, but it is not working, check
that the server extension is enabled:

```bash
jupyter server extension list
```

If the server extension is installed and enabled, but you are not seeing
the frontend extension, check the frontend extension is installed:

```bash
jupyter labextension list
```

## Contributing

If you would like to contribute to this extension, please refer to the [Contributing Guide](CONTRIBUTING.md).
