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
preconfigured Ray `JobSubmissionClient` pointed at the Bifrost gateway.

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
The jupyter-server extension runs inside the user's notebook pod, which carries
the `bifrost.dev/owner` label, so the per-owner NetworkPolicy admits it to the
head service on `:8265` — reachability is the authorization. The Bifrost
credential stays on the control-plane routes (start/list/stop/suspend/resume).
Ray itself is **not** installed in the server environment for this: the Jobs
REST contract is spoken directly over HTTP. Ray stays the optional
`bifrost-jupyter[kernel]` extra, used only by the kernel-side `connect()`
helper.

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
