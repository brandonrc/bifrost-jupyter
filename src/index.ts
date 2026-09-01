import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';

import { INotebookTracker } from '@jupyterlab/notebook';

import { ITranslator } from '@jupyterlab/translation';

import { BifrostPanel } from './BifrostPanel';

/**
 * Initialization data for the bifrost-jupyter extension.
 *
 * Registers the "Ray Clusters" panel in the left sidebar. The panel talks only
 * to the co-located ``/bifrost/*`` server extension, same-origin, via
 * ``ServerConnection`` (design §3.1) — no Bifrost URL or token in the browser.
 */
const plugin: JupyterFrontEndPlugin<void> = {
  id: 'bifrost-jupyter:plugin',
  description:
    'JupyterLab extension to start, stop, and connect to Bifrost-fronted Ray clusters',
  autoStart: true,
  requires: [INotebookTracker],
  optional: [ITranslator],
  activate: (
    app: JupyterFrontEnd,
    notebooks: INotebookTracker,
    translator: ITranslator | null
  ) => {
    const panel = new BifrostPanel(
      app.serviceManager.serverSettings,
      notebooks,
      translator ?? undefined
    );
    app.shell.add(panel, 'left', { rank: 200 });
  }
};

export default plugin;
