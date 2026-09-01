import { NotebookActions } from '@jupyterlab/notebook';

import { ServerConnection } from '@jupyterlab/services';

import { Widget } from '@lumino/widgets';

import * as api from '../api';
import { BifrostPanel } from '../BifrostPanel';

jest.mock('../api');

// The notebook API is mocked so the panel's cell injection can be asserted
// without a live JupyterLab notebook. Only NotebookActions is used at runtime;
// INotebookTracker / NotebookPanel are type-only imports (erased at compile).
jest.mock('@jupyterlab/notebook', () => ({
  NotebookActions: { insertBelow: jest.fn() }
}));

const insertBelow = NotebookActions.insertBelow as jest.MockedFunction<
  typeof NotebookActions.insertBelow
>;

const listProfiles = api.listProfiles as jest.MockedFunction<
  typeof api.listProfiles
>;
const listClusters = api.listClusters as jest.MockedFunction<
  typeof api.listClusters
>;
const createCluster = api.createCluster as jest.MockedFunction<
  typeof api.createCluster
>;
const stopCluster = api.stopCluster as jest.MockedFunction<
  typeof api.stopCluster
>;
const suspendCluster = api.suspendCluster as jest.MockedFunction<
  typeof api.suspendCluster
>;
const resumeCluster = api.resumeCluster as jest.MockedFunction<
  typeof api.resumeCluster
>;
const getAddress = api.getAddress as jest.MockedFunction<typeof api.getAddress>;

/**
 * A fake notebook tracker + panel exposing exactly what the injection path
 * touches: ``currentWidget.content.activeCell.model.sharedModel.setSource``.
 */
function fakeNotebooks(): {
  tracker: any;
  setSource: jest.Mock;
} {
  const setSource = jest.fn();
  const activeCell = { model: { sharedModel: { setSource } } };
  const tracker = { currentWidget: { content: { activeCell } } };
  return { tracker, setSource };
}

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
    stopCluster.mockReset();
    suspendCluster.mockReset();
    resumeCluster.mockReset();
    getAddress.mockReset();
    insertBelow.mockReset();
    listProfiles.mockResolvedValue([SMALL]);
    listClusters.mockResolvedValue({ clusters: [], configured: true });
    stopCluster.mockResolvedValue({ id: 'x', status: 'stopping' });
    suspendCluster.mockResolvedValue({ id: 'x', status: 'suspending' });
    resumeCluster.mockResolvedValue({ id: 'x', status: 'resuming' });
    getAddress.mockResolvedValue({
      jobs_address: 'http://jl-run-aaa-head-svc.bifrost.svc:8265',
      ray_client_address: 'ray://jl-run-aaa-head-svc.bifrost.svc:10001',
      snippet: 'from ray.job_submission import JobSubmissionClient\n'
    });
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
    listClusters.mockResolvedValue({
      clusters: [
        { id: 'jl-small-aaa', state: 'running' },
        { id: 'jl-gpu-bbb', state: 'pending' }
      ],
      configured: true
    });
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
    listClusters.mockResolvedValue({ clusters: [], configured: true });
    panel = new BifrostPanel(settings());
    Widget.attach(panel, document.body);
    await flush();

    expect(
      panel.node.querySelector('.jp-BifrostPanel-empty')?.textContent
    ).toContain('No clusters');
  });

  it('renders a friendly note and disables Start when Bifrost is unconfigured', async () => {
    listClusters.mockResolvedValue({ clusters: [], configured: false });
    panel = new BifrostPanel(settings());
    Widget.attach(panel, document.body);
    await flush();

    const note = panel.node.querySelector('.jp-BifrostPanel-unconfigured');
    expect(note?.textContent).toContain('not configured');
    expect(note?.textContent).toContain('BIFROST_API_URL');
    // No error styling / spam for a normal unconfigured state.
    expect(panel.node.querySelector('.jp-BifrostPanel-statusError')).toBeNull();

    // Start stays disabled even after a profile is chosen — there is no backend.
    selectEl().value = 'small';
    selectEl().dispatchEvent(new Event('change'));
    expect(startButton().disabled).toBe(true);
  });

  it('stops polling once the server reports it is unconfigured', async () => {
    jest.useFakeTimers();
    listClusters.mockResolvedValue({ clusters: [], configured: false });
    panel = new BifrostPanel(settings());
    Widget.attach(panel, document.body);
    // Let the initial onAfterAttach refresh settle.
    await Promise.resolve();
    await Promise.resolve();

    const callsAfterAttach = listClusters.mock.calls.length;
    jest.advanceTimersByTime(30000);
    expect(listClusters.mock.calls.length).toBe(callsAfterAttach);
    jest.useRealTimers();
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

  async function attachedWith(clusterState: string, notebooks?: any) {
    listClusters.mockResolvedValue({
      clusters: [{ id: 'jl-run-aaa', state: clusterState }],
      configured: true
    });
    panel = new BifrostPanel(settings(), notebooks);
    Widget.attach(panel, document.body);
    await flush();
  }

  function actionButton(cls: string): HTMLButtonElement | null {
    return panel.node.querySelector(cls) as HTMLButtonElement | null;
  }

  it('shows Connect, Suspend and Stop for a running cluster (no Resume)', async () => {
    await attachedWith('running');
    expect(actionButton('.jp-BifrostPanel-connect')).not.toBeNull();
    expect(actionButton('.jp-BifrostPanel-suspend')).not.toBeNull();
    expect(actionButton('.jp-BifrostPanel-stop')).not.toBeNull();
    expect(actionButton('.jp-BifrostPanel-resume')).toBeNull();
  });

  it('shows Resume and Stop for a suspended cluster (no Suspend/Connect)', async () => {
    await attachedWith('suspended');
    expect(actionButton('.jp-BifrostPanel-resume')).not.toBeNull();
    expect(actionButton('.jp-BifrostPanel-stop')).not.toBeNull();
    expect(actionButton('.jp-BifrostPanel-suspend')).toBeNull();
    expect(actionButton('.jp-BifrostPanel-connect')).toBeNull();
  });

  it('shows only Stop for a pending cluster', async () => {
    await attachedWith('pending');
    expect(actionButton('.jp-BifrostPanel-stop')).not.toBeNull();
    expect(actionButton('.jp-BifrostPanel-connect')).toBeNull();
    expect(actionButton('.jp-BifrostPanel-suspend')).toBeNull();
    expect(actionButton('.jp-BifrostPanel-resume')).toBeNull();
  });

  it('confirms before stopping and calls stopCluster on confirm', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    await attachedWith('running');

    actionButton('.jp-BifrostPanel-stop')!.click();
    await flush();

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(stopCluster).toHaveBeenCalledWith(expect.anything(), 'jl-run-aaa');
    confirmSpy.mockRestore();
  });

  it('does not stop when the confirm is dismissed', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    await attachedWith('running');

    actionButton('.jp-BifrostPanel-stop')!.click();
    await flush();

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(stopCluster).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('calls suspendCluster from the Suspend button', async () => {
    await attachedWith('running');
    actionButton('.jp-BifrostPanel-suspend')!.click();
    await flush();
    expect(suspendCluster).toHaveBeenCalledWith(
      expect.anything(),
      'jl-run-aaa'
    );
  });

  it('calls resumeCluster from the Resume button', async () => {
    await attachedWith('suspended');
    actionButton('.jp-BifrostPanel-resume')!.click();
    await flush();
    expect(resumeCluster).toHaveBeenCalledWith(expect.anything(), 'jl-run-aaa');
  });

  it('Connect fetches the address and injects a runnable cell', async () => {
    const { tracker, setSource } = fakeNotebooks();
    await attachedWith('running', tracker);

    actionButton('.jp-BifrostPanel-connect')!.click();
    await flush();

    expect(getAddress).toHaveBeenCalledWith(expect.anything(), 'jl-run-aaa');
    expect(insertBelow).toHaveBeenCalledTimes(1);
    expect(setSource).toHaveBeenCalledWith(
      'from ray.job_submission import JobSubmissionClient\n'
    );
  });

  it('Connect prompts to open a notebook when none is active', async () => {
    const tracker = { currentWidget: null };
    await attachedWith('running', tracker);

    actionButton('.jp-BifrostPanel-connect')!.click();
    await flush();

    expect(getAddress).not.toHaveBeenCalled();
    expect(insertBelow).not.toHaveBeenCalled();
    expect(
      panel.node.querySelector('.jp-BifrostPanel-message')?.textContent
    ).toContain('Open a notebook');
  });
});
