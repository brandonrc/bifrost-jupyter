import { ServerConnection } from '@jupyterlab/services';

import { Widget } from '@lumino/widgets';

import * as api from '../api';
import { BifrostPanel } from '../BifrostPanel';

jest.mock('../api');

const listProfiles = api.listProfiles as jest.MockedFunction<
  typeof api.listProfiles
>;
const listClusters = api.listClusters as jest.MockedFunction<
  typeof api.listClusters
>;
const createCluster = api.createCluster as jest.MockedFunction<
  typeof api.createCluster
>;

const SMALL: api.IProfileView = {
  name: 'small',
  description: '1 CPU head + 2 workers',
  head_cpu: '1',
  head_memory: '2Gi',
  workers: [],
  gpu: 0
};

/** Let the async work kicked off by onAfterAttach settle. */
function flush(): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, 0));
}

function settings(): ServerConnection.ISettings {
  return ServerConnection.makeSettings({ baseUrl: 'https://x.test/' });
}

describe('BifrostPanel', () => {
  let panel: BifrostPanel;

  beforeEach(() => {
    listProfiles.mockReset();
    listClusters.mockReset();
    createCluster.mockReset();
    listProfiles.mockResolvedValue([SMALL]);
    listClusters.mockResolvedValue([]);
  });

  afterEach(() => {
    panel?.dispose();
  });

  function selectEl(): HTMLSelectElement {
    return panel.node.querySelector(
      '.jp-BifrostPanel-select'
    ) as HTMLSelectElement;
  }

  function startButton(): HTMLButtonElement {
    return panel.node.querySelector(
      '.jp-BifrostPanel-start'
    ) as HTMLButtonElement;
  }

  it('disables Start until a profile is chosen, then enables it', async () => {
    panel = new BifrostPanel(settings());
    Widget.attach(panel, document.body);
    await flush();

    const select = selectEl();
    // Placeholder + one profile option.
    expect(select.options.length).toBe(2);
    expect(select.value).toBe('');
    expect(startButton().disabled).toBe(true);

    // Choosing a profile enables Start.
    select.value = 'small';
    select.dispatchEvent(new Event('change'));
    expect(startButton().disabled).toBe(false);
  });

  it('keeps Start disabled when no profiles are available', async () => {
    listProfiles.mockResolvedValue([]);
    panel = new BifrostPanel(settings());
    Widget.attach(panel, document.body);
    await flush();

    expect(startButton().disabled).toBe(true);
  });

  it('renders each cluster id + state from listClusters', async () => {
    listClusters.mockResolvedValue([
      { id: 'jl-small-aaa', state: 'running' },
      { id: 'jl-gpu-bbb', state: 'pending' }
    ]);
    panel = new BifrostPanel(settings());
    Widget.attach(panel, document.body);
    await flush();

    const items = panel.node.querySelectorAll('.jp-BifrostPanel-statusItem');
    expect(items).toHaveLength(2);

    const ids = Array.from(
      panel.node.querySelectorAll('.jp-BifrostPanel-clusterId')
    ).map(el => el.textContent);
    const states = Array.from(
      panel.node.querySelectorAll('.jp-BifrostPanel-clusterState')
    ).map(el => el.textContent);

    expect(ids).toEqual(['jl-small-aaa', 'jl-gpu-bbb']);
    expect(states).toEqual(['running', 'pending']);
  });

  it('shows an empty note when there are no clusters', async () => {
    listClusters.mockResolvedValue([]);
    panel = new BifrostPanel(settings());
    Widget.attach(panel, document.body);
    await flush();

    expect(
      panel.node.querySelector('.jp-BifrostPanel-empty')?.textContent
    ).toContain('No clusters');
  });

  it('calls createCluster with the chosen profile on Start', async () => {
    createCluster.mockResolvedValue({ id: 'jl-small-ccc', status: 'pending' });
    panel = new BifrostPanel(settings());
    Widget.attach(panel, document.body);
    await flush();

    selectEl().value = 'small';
    selectEl().dispatchEvent(new Event('change'));
    startButton().click();
    await flush();

    expect(createCluster).toHaveBeenCalledTimes(1);
    expect(createCluster).toHaveBeenCalledWith(expect.anything(), 'small');
  });
});
