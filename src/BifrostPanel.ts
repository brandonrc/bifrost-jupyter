import { ServerConnection } from '@jupyterlab/services';

import {
  ITranslator,
  nullTranslator,
  TranslationBundle
} from '@jupyterlab/translation';

import { Message } from '@lumino/messaging';

import { Widget } from '@lumino/widgets';

import {
  createCluster,
  ICluster,
  IProfileView,
  listClusters,
  listProfiles
} from './api';

/** How often the status list re-polls ``GET /bifrost/clusters`` (ms). */
const POLL_INTERVAL_MS = 5000;

/**
 * The "Ray Clusters" sidebar panel (design §3.1).
 *
 * A profile dropdown (from ``GET /bifrost/profiles``), a Start button that is
 * disabled until a profile is chosen and posts to ``POST /bifrost/clusters``,
 * and a status list that polls ``GET /bifrost/clusters``. Every network call
 * goes through {@link api} — same-origin only, no token in the browser.
 */
export class BifrostPanel extends Widget {
  private _serverSettings: ServerConnection.ISettings;
  private _trans: TranslationBundle;
  private _select: HTMLSelectElement;
  private _startButton: HTMLButtonElement;
  private _statusList: HTMLUListElement;
  private _messageBar: HTMLDivElement;
  private _pollTimer: number | null = null;

  constructor(
    serverSettings: ServerConnection.ISettings,
    translator?: ITranslator
  ) {
    super();
    this._serverSettings = serverSettings;
    this._trans = (translator ?? nullTranslator).load('bifrost-jupyter');
    const trans = this._trans;

    this.id = 'bifrost-panel';
    this.title.label = trans.__('Ray Clusters');
    this.title.caption = trans.__(
      'Start and monitor Bifrost-fronted Ray clusters'
    );
    this.title.closable = true;
    this.addClass('jp-BifrostPanel');

    const header = document.createElement('h2');
    header.textContent = trans.__('Ray Clusters');
    header.className = 'jp-BifrostPanel-header';

    const controls = document.createElement('div');
    controls.className = 'jp-BifrostPanel-controls';

    this._select = document.createElement('select');
    this._select.className = 'jp-BifrostPanel-select';
    this._select.setAttribute('aria-label', trans.__('Cluster profile'));
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = trans.__('Loading profiles…');
    placeholder.disabled = true;
    placeholder.selected = true;
    this._select.appendChild(placeholder);
    this._select.addEventListener('change', () => this._syncStartEnabled());

    this._startButton = document.createElement('button');
    this._startButton.className = 'jp-BifrostPanel-start jp-mod-styled';
    this._startButton.textContent = trans.__('Start');
    // Disabled until a profile is chosen (design §3.1 / brief constraint).
    this._startButton.disabled = true;
    this._startButton.addEventListener('click', () => {
      void this._onStart();
    });

    controls.appendChild(this._select);
    controls.appendChild(this._startButton);

    this._messageBar = document.createElement('div');
    this._messageBar.className = 'jp-BifrostPanel-message';
    this._messageBar.hidden = true;

    const statusHeader = document.createElement('h3');
    statusHeader.textContent = trans.__('Clusters');
    statusHeader.className = 'jp-BifrostPanel-statusHeader';

    this._statusList = document.createElement('ul');
    this._statusList.className = 'jp-BifrostPanel-statusList';

    this.node.appendChild(header);
    this.node.appendChild(controls);
    this.node.appendChild(this._messageBar);
    this.node.appendChild(statusHeader);
    this.node.appendChild(this._statusList);
  }

  /** Load profiles and start polling once the panel is shown. */
  protected onAfterAttach(msg: Message): void {
    super.onAfterAttach(msg);
    void this._loadProfiles();
    void this._refreshStatus();
    this._pollTimer = window.setInterval(() => {
      void this._refreshStatus();
    }, POLL_INTERVAL_MS);
  }

  protected onBeforeDetach(msg: Message): void {
    this._stopPolling();
    super.onBeforeDetach(msg);
  }

  dispose(): void {
    this._stopPolling();
    super.dispose();
  }

  private _stopPolling(): void {
    if (this._pollTimer !== null) {
      window.clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  private async _loadProfiles(): Promise<void> {
    try {
      const profiles = await listProfiles(this._serverSettings);
      this._populateProfiles(profiles);
    } catch (error) {
      this._showMessage(
        this._trans.__('Could not load profiles: %1', errorText(error)),
        true
      );
      this._select.innerHTML = '';
      const failed = document.createElement('option');
      failed.value = '';
      failed.textContent = this._trans.__('No profiles available');
      failed.disabled = true;
      failed.selected = true;
      this._select.appendChild(failed);
      this._syncStartEnabled();
    }
  }

  private _populateProfiles(profiles: IProfileView[]): void {
    this._select.innerHTML = '';

    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent =
      profiles.length > 0
        ? this._trans.__('Choose a profile…')
        : this._trans.__('No profiles available');
    placeholder.disabled = true;
    placeholder.selected = true;
    this._select.appendChild(placeholder);

    for (const profile of profiles) {
      const option = document.createElement('option');
      option.value = profile.name;
      option.textContent = profile.description
        ? `${profile.name} — ${profile.description}`
        : profile.name;
      this._select.appendChild(option);
    }

    this._syncStartEnabled();
  }

  /** Start stays disabled unless a non-empty profile is selected. */
  private _syncStartEnabled(): void {
    this._startButton.disabled = this._select.value === '';
  }

  private async _onStart(): Promise<void> {
    const profile = this._select.value;
    if (profile === '') {
      return;
    }
    this._startButton.disabled = true;
    this._showMessage(this._trans.__('Starting %1…', profile), false);
    try {
      const result = await createCluster(this._serverSettings, profile);
      this._showMessage(
        this._trans.__('Started %1 (%2).', result.id, result.status),
        false
      );
      await this._refreshStatus();
    } catch (error) {
      this._showMessage(
        this._trans.__('Start failed: %1', errorText(error)),
        true
      );
    } finally {
      this._syncStartEnabled();
    }
  }

  private async _refreshStatus(): Promise<void> {
    try {
      const clusters = await listClusters(this._serverSettings);
      this._renderStatus(clusters);
    } catch (error) {
      // A transient poll failure should not spam the message bar; render an
      // inline note in the list instead.
      this._renderStatusError(errorText(error));
    }
  }

  private _renderStatus(clusters: ICluster[]): void {
    this._statusList.innerHTML = '';

    if (clusters.length === 0) {
      const empty = document.createElement('li');
      empty.className = 'jp-BifrostPanel-empty';
      empty.textContent = this._trans.__('No clusters yet.');
      this._statusList.appendChild(empty);
      return;
    }

    for (const cluster of clusters) {
      const item = document.createElement('li');
      item.className = 'jp-BifrostPanel-statusItem';

      const id = document.createElement('span');
      id.className = 'jp-BifrostPanel-clusterId';
      id.textContent = cluster.id;

      const state = document.createElement('span');
      state.className = 'jp-BifrostPanel-clusterState';
      state.dataset.state = cluster.state;
      state.textContent = cluster.state;

      item.appendChild(id);
      item.appendChild(state);
      this._statusList.appendChild(item);
    }
  }

  private _renderStatusError(message: string): void {
    this._statusList.innerHTML = '';
    const item = document.createElement('li');
    item.className = 'jp-BifrostPanel-statusError';
    item.textContent = this._trans.__('Could not load clusters: %1', message);
    this._statusList.appendChild(item);
  }

  private _showMessage(message: string, isError: boolean): void {
    this._messageBar.hidden = false;
    this._messageBar.textContent = message;
    this._messageBar.classList.toggle('jp-mod-error', isError);
  }
}

/** Best-effort human-readable text from an unknown thrown value. */
function errorText(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}
