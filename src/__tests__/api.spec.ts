import { ServerConnection } from '@jupyterlab/services';

import {
  createCluster,
  dashboardUrl,
  getAddress,
  getJobStatus,
  listClusters,
  listProfiles,
  resumeCluster,
  stopCluster,
  submitJob,
  suspendCluster
} from '../api';

/**
 * The security invariant under test: api.ts only ever calls the co-located
 * ``/bifrost/*`` routes on the user's own Jupyter server (same-origin), never a
 * Bifrost/external URL, and never attaches a bearer token from the browser.
 */

const BASE_URL = 'https://jupyter.example.test/user/alice/';

function makeSettings(): ServerConnection.ISettings {
  return ServerConnection.makeSettings({ baseUrl: BASE_URL });
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    text: async () => JSON.stringify(body)
  } as unknown as Response;
}

describe('api same-origin invariant', () => {
  let spy: jest.SpyInstance;

  afterEach(() => {
    spy?.mockRestore();
  });

  function captureRequests(body: unknown): void {
    spy = jest
      .spyOn(ServerConnection, 'makeRequest')
      .mockResolvedValue(jsonResponse(body));
  }

  function requestedUrls(): string[] {
    return spy.mock.calls.map(call => call[0] as string);
  }

  it('listProfiles hits <baseUrl>/bifrost/profiles only', async () => {
    captureRequests({ profiles: [] });
    await listProfiles(makeSettings());

    const urls = requestedUrls();
    expect(urls).toHaveLength(1);
    expect(urls[0]).toBe(`${BASE_URL}bifrost/profiles`);
  });

  it('createCluster POSTs to <baseUrl>/bifrost/clusters with only a profile name', async () => {
    captureRequests({ id: 'jl-small-abc', status: 'pending' });
    await createCluster(makeSettings(), 'small');

    const urls = requestedUrls();
    expect(urls).toHaveLength(1);
    expect(urls[0]).toBe(`${BASE_URL}bifrost/clusters`);

    const init = spy.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ profile: 'small' });
  });

  it('listClusters hits <baseUrl>/bifrost/clusters only', async () => {
    captureRequests({ clusters: [] });
    await listClusters(makeSettings());

    const urls = requestedUrls();
    expect(urls).toHaveLength(1);
    expect(urls[0]).toBe(`${BASE_URL}bifrost/clusters`);
  });

  it('never requests a non-baseUrl / external origin and carries no bearer token', async () => {
    captureRequests({ profiles: [], clusters: [], id: 'x', status: 'pending' });
    const settings = makeSettings();
    await listProfiles(settings);
    await createCluster(settings, 'small');
    await listClusters(settings);

    for (const [url, init] of spy.mock.calls) {
      // Same-origin: every URL is rooted at the user's own Jupyter server.
      expect(url as string).toContain(`${BASE_URL}bifrost/`);
      expect(url as string).not.toMatch(/bifrost.*\.(com|net|io|svc)/i);

      // No Authorization / bearer token is set by the browser client.
      const headers = ((init as RequestInit)?.headers ?? {}) as Record<
        string,
        string
      >;
      const headerKeys = Object.keys(headers).map(k => k.toLowerCase());
      expect(headerKeys).not.toContain('authorization');
    }
  });

  it('stopCluster DELETEs <baseUrl>/bifrost/clusters/{id}', async () => {
    captureRequests({ id: 'jl-small-abc', status: 'stopping' });
    await stopCluster(makeSettings(), 'jl-small-abc');

    const urls = requestedUrls();
    expect(urls).toHaveLength(1);
    expect(urls[0]).toBe(`${BASE_URL}bifrost/clusters/jl-small-abc`);
    expect((spy.mock.calls[0][1] as RequestInit).method).toBe('DELETE');
  });

  it('suspendCluster POSTs <baseUrl>/bifrost/clusters/{id}/suspend', async () => {
    captureRequests({ id: 'jl-small-abc', status: 'suspending' });
    await suspendCluster(makeSettings(), 'jl-small-abc');

    const urls = requestedUrls();
    expect(urls).toHaveLength(1);
    expect(urls[0]).toBe(`${BASE_URL}bifrost/clusters/jl-small-abc/suspend`);
    expect((spy.mock.calls[0][1] as RequestInit).method).toBe('POST');
  });

  it('resumeCluster POSTs <baseUrl>/bifrost/clusters/{id}/resume', async () => {
    captureRequests({ id: 'jl-small-abc', status: 'resuming' });
    await resumeCluster(makeSettings(), 'jl-small-abc');

    const urls = requestedUrls();
    expect(urls).toHaveLength(1);
    expect(urls[0]).toBe(`${BASE_URL}bifrost/clusters/jl-small-abc/resume`);
    expect((spy.mock.calls[0][1] as RequestInit).method).toBe('POST');
  });

  it('getAddress GETs <baseUrl>/bifrost/clusters/{id}/address', async () => {
    captureRequests({
      jobs_address: 'http://x-head-svc.bifrost.svc:8265',
      ray_client_address: 'ray://x-head-svc.bifrost.svc:10001',
      snippet: 'from ray.job_submission import JobSubmissionClient\n'
    });
    const address = await getAddress(makeSettings(), 'jl-small-abc');

    const urls = requestedUrls();
    expect(urls).toHaveLength(1);
    expect(urls[0]).toBe(`${BASE_URL}bifrost/clusters/jl-small-abc/address`);
    expect(address.snippet).toContain('JobSubmissionClient');
  });

  it('all lifecycle + address calls stay same-origin and tokenless', async () => {
    captureRequests({
      id: 'x',
      status: 'ok',
      jobs_address: 'a',
      ray_client_address: 'b',
      snippet: 's'
    });
    const s = makeSettings();
    await stopCluster(s, 'jl-x');
    await suspendCluster(s, 'jl-x');
    await resumeCluster(s, 'jl-x');
    await getAddress(s, 'jl-x');

    for (const [url, init] of spy.mock.calls) {
      expect(url as string).toContain(`${BASE_URL}bifrost/`);
      expect(url as string).not.toMatch(/bifrost.*\.(com|net|io|svc)/i);
      const headers = ((init as RequestInit)?.headers ?? {}) as Record<
        string,
        string
      >;
      const headerKeys = Object.keys(headers).map(k => k.toLowerCase());
      expect(headerKeys).not.toContain('authorization');
    }
  });

  it('submitJob POSTs <baseUrl>/bifrost/clusters/{id}/jobs with entrypoint + env_vars', async () => {
    captureRequests({
      job_id: 'raysubmit_abc',
      submission_id: 'raysubmit_abc'
    });
    await submitJob(makeSettings(), 'jl-run-aaa', 'python train.py', {
      HF_TOKEN: 't',
      SEED: '7'
    });

    const urls = requestedUrls();
    expect(urls).toHaveLength(1);
    expect(urls[0]).toBe(`${BASE_URL}bifrost/clusters/jl-run-aaa/jobs`);

    const init = spy.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe('POST');
    // Requirement #11: the env vars ride on the job; the server maps them to
    // Ray's runtime_env.env_vars.
    expect(JSON.parse(init.body as string)).toEqual({
      entrypoint: 'python train.py',
      env_vars: { HF_TOKEN: 't', SEED: '7' }
    });
  });

  it('submitJob sends an empty env_vars map when none are given', async () => {
    captureRequests({
      job_id: 'raysubmit_abc',
      submission_id: 'raysubmit_abc'
    });
    await submitJob(makeSettings(), 'jl-run-aaa', 'echo hi');

    const init = spy.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({
      entrypoint: 'echo hi',
      env_vars: {}
    });
  });

  it('getJobStatus GETs <baseUrl>/bifrost/clusters/{id}/jobs/{job_id}', async () => {
    captureRequests({ job_id: 'raysubmit_abc', status: 'RUNNING' });
    const job = await getJobStatus(
      makeSettings(),
      'jl-run-aaa',
      'raysubmit_abc'
    );

    const urls = requestedUrls();
    expect(urls).toHaveLength(1);
    expect(urls[0]).toBe(
      `${BASE_URL}bifrost/clusters/jl-run-aaa/jobs/raysubmit_abc`
    );
    expect(job.status).toBe('RUNNING');
  });

  it('job routes stay same-origin and tokenless', async () => {
    captureRequests({
      job_id: 'raysubmit_abc',
      submission_id: 'raysubmit_abc'
    });
    const s = makeSettings();
    await submitJob(s, 'jl-x', 'echo hi', { A: '1' });
    await getJobStatus(s, 'jl-x', 'raysubmit_abc');

    for (const [url, init] of spy.mock.calls) {
      expect(url as string).toContain(`${BASE_URL}bifrost/`);
      expect(url as string).not.toMatch(/bifrost.*\.(com|net|io|svc)/i);
      // Never the cluster's own head service / Ray Jobs API from the browser.
      expect(url as string).not.toContain('8265');
      expect(url as string).not.toContain('/api/jobs/');
      const headers = ((init as RequestInit)?.headers ?? {}) as Record<
        string,
        string
      >;
      const headerKeys = Object.keys(headers).map(k => k.toLowerCase());
      expect(headerKeys).not.toContain('authorization');
    }
  });

  it('unwraps the server error message on a non-ok response', async () => {
    spy = jest
      .spyOn(ServerConnection, 'makeRequest')
      .mockResolvedValue(jsonResponse({ error: 'unknown profile' }, 400));

    await expect(listProfiles(makeSettings())).rejects.toThrow(
      'unknown profile'
    );
  });
});

describe('api response typing', () => {
  it('returns the parsed profiles array', async () => {
    const spy = jest.spyOn(ServerConnection, 'makeRequest').mockResolvedValue(
      jsonResponse({
        profiles: [
          {
            name: 'small',
            description: '1 CPU head',
            head_cpu: '1',
            head_memory: '2Gi',
            workers: [],
            gpu: 0
          }
        ]
      })
    );

    const profiles = await listProfiles(makeSettings());
    expect(profiles).toHaveLength(1);
    expect(profiles[0].name).toBe('small');
    spy.mockRestore();
  });

  it('returns the parsed clusters response', async () => {
    const spy = jest.spyOn(ServerConnection, 'makeRequest').mockResolvedValue(
      jsonResponse({
        clusters: [{ id: 'jl-small-abc', state: 'running' }],
        configured: true
      })
    );

    const response = await listClusters(makeSettings());
    expect(response.clusters).toEqual([
      { id: 'jl-small-abc', state: 'running' }
    ]);
    expect(response.configured).toBe(true);
    spy.mockRestore();
  });

  it('surfaces configured:false for a bare, unconfigured install', async () => {
    const spy = jest
      .spyOn(ServerConnection, 'makeRequest')
      .mockResolvedValue(jsonResponse({ clusters: [], configured: false }));

    const response = await listClusters(makeSettings());
    expect(response.clusters).toEqual([]);
    expect(response.configured).toBe(false);
    spy.mockRestore();
  });
});

describe('dashboardUrl', () => {
  it('builds a same-origin, tokenless URL with the required trailing slash', () => {
    const url = dashboardUrl(makeSettings(), 'jl-small-abc');

    // Same-origin by construction: rooted at baseUrl, never the head service.
    expect(url).toBe(`${BASE_URL}bifrost/clusters/jl-small-abc/dashboard/`);
    expect(url).not.toContain('8265');
    expect(url).not.toContain('head-svc');
    expect(url).not.toContain('token');
  });

  it('keeps the trailing slash Ray needs to resolve its relative assets', () => {
    // Without it the browser resolves ./static/... one segment too high and the
    // dashboard loads blank; Ray's reverse-proxy docs call this out explicitly.
    expect(dashboardUrl(makeSettings(), 'jl-x')).toMatch(/\/dashboard\/$/);
  });

  it('encodes the cluster id', () => {
    expect(dashboardUrl(makeSettings(), 'a/b')).toBe(
      `${BASE_URL}bifrost/clusters/a%2Fb/dashboard/`
    );
  });

  it('issues no request — it is a URL builder', () => {
    const spy = jest.spyOn(ServerConnection, 'makeRequest');
    dashboardUrl(makeSettings(), 'jl-x');
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });
});
