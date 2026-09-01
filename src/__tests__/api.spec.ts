import { ServerConnection } from '@jupyterlab/services';

import {
  createCluster,
  getAddress,
  listClusters,
  listProfiles,
  resumeCluster,
  stopCluster,
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
