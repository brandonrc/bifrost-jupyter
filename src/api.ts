import { URLExt } from '@jupyterlab/coreutils';

import { ServerConnection } from '@jupyterlab/services';

/**
 * Typed, same-origin client for the co-located ``/bifrost/*`` server extension
 * routes (design §3.1).
 *
 * SECURITY INVARIANT: every request in this file is issued through
 * ``ServerConnection.makeRequest`` against a URL rooted at
 * ``serverSettings.baseUrl`` — i.e. the user's own Jupyter server, same-origin,
 * authenticated by Jupyter's XSRF cookie. The browser never talks to Bifrost
 * directly and never holds a Bifrost/OIDC token: the credential lives
 * server-side in the Python extension (design §3.2, §4). There is therefore no
 * Bifrost URL and no bearer token anywhere in this module — a review greps for
 * exactly that.
 */

/** The extension's same-origin API namespace: ``<baseUrl>/bifrost/*``. */
const API_NAMESPACE = 'bifrost';

/** One approved worker group as exposed by ``GET /bifrost/profiles``. */
export interface IWorkerView {
  cpu: string;
  memory: string;
  gpu: string | null;
  min_replicas: number;
  max_replicas: number;
}

/**
 * The safe, user-facing view of an approved profile (mirrors the server's
 * ``ProfileView.to_dict``). Deliberately carries no image / ray_version / raw
 * manifest surface — only the coarse shape a user chooses from.
 */
export interface IProfileView {
  name: string;
  description: string;
  head_cpu: string;
  head_memory: string;
  workers: IWorkerView[];
  gpu: number;
}

/** Response of ``GET /bifrost/profiles``. */
export interface IProfilesResponse {
  profiles: IProfileView[];
}

/** Response of ``POST /bifrost/clusters`` (design §3.2). */
export interface ICreateClusterResponse {
  id: string;
  status: string;
}

/** One cluster in the list/status view (``GET /bifrost/clusters``). */
export interface ICluster {
  id: string;
  state: string;
}

/** Response of a lifecycle action (stop/suspend/resume). */
export interface IClusterActionResponse {
  id: string;
  status: string;
}

/**
 * Response of ``GET /bifrost/clusters/{id}/address`` (design §6).
 *
 * All fields are derived server-side from the cluster id + namespace (no Bifrost
 * call, no token). ``snippet`` is the ready-to-run ``JobSubmissionClient`` cell,
 * with the Ray Client path included as a commented advanced alternative.
 */
export interface IClusterAddress {
  jobs_address: string;
  ray_client_address: string;
  snippet: string;
}

/**
 * The env-var map submitted with a job — requirement #11.
 *
 * ``ClusterSpec`` has no env field, so these attach to the *job*: the server
 * puts them under Ray's ``runtime_env.env_vars`` at submit time (design §2).
 * Values must be strings; the server rejects anything else with a clean 400.
 */
export type EnvVars = Record<string, string>;

/** Response of ``POST /bifrost/clusters/{id}/jobs`` — Ray's submission id. */
export interface IJobSubmitResponse {
  job_id: string;
  submission_id: string;
}

/**
 * Response of ``GET /bifrost/clusters/{id}/jobs/{job_id}`` — the allowlisted
 * view of Ray's ``JobDetails`` (never the full passthrough).
 */
export interface IJobStatus {
  job_id: string;
  status?: string;
  message?: string | null;
  start_time?: number | null;
  end_time?: number | null;
}

/** Response of ``GET /bifrost/clusters`` (list/status, design §3.2). */
export interface IClustersResponse {
  clusters: ICluster[];
  /**
   * ``false`` when the server extension is installed but Bifrost is not
   * configured (no ``BIFROST_API_URL`` / ``BIFROST_TOKEN``). A bare install is
   * a normal state, not an error: the route answers 200 with this marker so the
   * panel can show a friendly note instead of error-spamming. Absent/``true``
   * means Bifrost is configured.
   */
  configured?: boolean;
}

/**
 * Issue a request to a ``/bifrost/*`` route on the user's own Jupyter server.
 *
 * The URL is always ``URLExt.join(serverSettings.baseUrl, 'bifrost', ...)`` so
 * it is same-origin by construction; there is no way to point this at an
 * external host. Auth is the server's XSRF cookie, attached by
 * ``ServerConnection.makeRequest`` — no token is read or set here.
 */
async function bifrostRequest<T>(
  serverSettings: ServerConnection.ISettings,
  endPoint: string,
  init: RequestInit = {}
): Promise<T> {
  const requestUrl = URLExt.join(
    serverSettings.baseUrl,
    API_NAMESPACE,
    endPoint
  );

  let response: Response;
  try {
    response = await ServerConnection.makeRequest(
      requestUrl,
      init,
      serverSettings
    );
  } catch (error) {
    throw new ServerConnection.NetworkError(error as TypeError);
  }

  const text = await response.text();
  let data: any = text;
  if (text.length > 0) {
    try {
      data = JSON.parse(text);
    } catch {
      console.error('bifrost: non-JSON response body', response);
    }
  }

  if (!response.ok) {
    // The server maps upstream failures to a safe ``{ error: <message> }``.
    const message =
      (data && (data.error || data.message)) || response.statusText;
    throw new ServerConnection.ResponseError(response, message);
  }

  return data as T;
}

/** ``GET /bifrost/profiles`` — the approved-profile allowlist view. */
export async function listProfiles(
  serverSettings: ServerConnection.ISettings
): Promise<IProfileView[]> {
  const data = await bifrostRequest<IProfilesResponse>(
    serverSettings,
    'profiles'
  );
  return data.profiles;
}

/**
 * ``POST /bifrost/clusters`` — start a cluster from an approved profile.
 *
 * The body carries only the profile *name*; the server owns the
 * profile→ClusterSpec mapping so the browser never sends a raw manifest.
 */
export async function createCluster(
  serverSettings: ServerConnection.ISettings,
  profile: string
): Promise<ICreateClusterResponse> {
  return bifrostRequest<ICreateClusterResponse>(serverSettings, 'clusters', {
    method: 'POST',
    body: JSON.stringify({ profile })
  });
}

/**
 * ``GET /bifrost/clusters`` — the list/status view (design §3.2).
 *
 * Returns the full response (not just the array) so callers can read the
 * ``configured`` marker and distinguish a bare, unconfigured install from a
 * genuine upstream failure.
 */
export async function listClusters(
  serverSettings: ServerConnection.ISettings
): Promise<IClustersResponse> {
  return bifrostRequest<IClustersResponse>(serverSettings, 'clusters');
}

/** The ``clusters/{id}`` sub-path for one cluster, id-encoded, same-origin. */
function clusterPath(id: string, ...rest: string[]): string {
  return ['clusters', encodeURIComponent(id), ...rest].join('/');
}

/**
 * ``DELETE /bifrost/clusters/{id}`` — stop (tear down) a cluster.
 *
 * Destructive; the panel gates this behind a confirm. The server attaches the
 * credential; this call carries no token and no external URL.
 */
export async function stopCluster(
  serverSettings: ServerConnection.ISettings,
  id: string
): Promise<IClusterActionResponse> {
  return bifrostRequest<IClusterActionResponse>(
    serverSettings,
    clusterPath(id),
    { method: 'DELETE' }
  );
}

/** ``POST /bifrost/clusters/{id}/suspend`` — scale a running cluster to zero. */
export async function suspendCluster(
  serverSettings: ServerConnection.ISettings,
  id: string
): Promise<IClusterActionResponse> {
  return bifrostRequest<IClusterActionResponse>(
    serverSettings,
    clusterPath(id, 'suspend'),
    { method: 'POST' }
  );
}

/** ``POST /bifrost/clusters/{id}/resume`` — bring a suspended cluster back up. */
export async function resumeCluster(
  serverSettings: ServerConnection.ISettings,
  id: string
): Promise<IClusterActionResponse> {
  return bifrostRequest<IClusterActionResponse>(
    serverSettings,
    clusterPath(id, 'resume'),
    { method: 'POST' }
  );
}

/**
 * ``GET /bifrost/clusters/{id}/address`` — the in-cluster Jobs address + a
 * ready-to-run ``JobSubmissionClient`` snippet (design §6). Derived server-side;
 * no Bifrost call, no token.
 */
export async function getAddress(
  serverSettings: ServerConnection.ISettings,
  id: string
): Promise<IClusterAddress> {
  return bifrostRequest<IClusterAddress>(
    serverSettings,
    clusterPath(id, 'address')
  );
}

/**
 * ``POST /bifrost/clusters/{id}/jobs`` — submit a Ray job with env vars (#11).
 *
 * Like every call in this module this goes to the user's own Jupyter server,
 * same-origin, with no token: the server forwards it to the cluster's in-cluster
 * Ray Jobs API and puts ``envVars`` under ``runtime_env.env_vars``.
 */
export async function submitJob(
  serverSettings: ServerConnection.ISettings,
  id: string,
  entrypoint: string,
  envVars: EnvVars = {}
): Promise<IJobSubmitResponse> {
  return bifrostRequest<IJobSubmitResponse>(
    serverSettings,
    clusterPath(id, 'jobs'),
    {
      method: 'POST',
      body: JSON.stringify({ entrypoint, env_vars: envVars })
    }
  );
}

/** ``GET /bifrost/clusters/{id}/jobs/{job_id}`` — one submitted job's status. */
export async function getJobStatus(
  serverSettings: ServerConnection.ISettings,
  id: string,
  jobId: string
): Promise<IJobStatus> {
  return bifrostRequest<IJobStatus>(
    serverSettings,
    clusterPath(id, 'jobs', encodeURIComponent(jobId))
  );
}
