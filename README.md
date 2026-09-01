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

> **Status:** scaffold only. This repo currently contains the
> `frontend-and-server` extension skeleton with dependencies, CI, and packaging
> wired up. Feature logic (cluster start/stop, profiles, the connect helper)
> lands in follow-up tasks.

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
