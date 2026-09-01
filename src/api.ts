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

/** Response of ``GET /bifrost/clusters`` (list/status, design §3.2). */
export interface IClustersResponse {
  clusters: ICluster[];
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

/** ``GET /bifrost/clusters`` — the list/status view (design §3.2). */
export async function listClusters(
  serverSettings: ServerConnection.ISettings
): Promise<ICluster[]> {
  const data = await bifrostRequest<IClustersResponse>(
    serverSettings,
    'clusters'
  );
  return data.clusters;
}
