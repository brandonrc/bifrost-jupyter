import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';

import { INotebookTracker } from '@jupyterlab/notebook';

import {
  ITranslator,
  nullTranslator,
  TranslationBundle
} from '@jupyterlab/translation';

import { Widget } from '@lumino/widgets';

import { BifrostPanel } from './BifrostPanel';

import { createDashboardWidget, dashboardWidgetId } from './DashboardWidget';

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
    const trans = (translator ?? nullTranslator).load('bifrost-jupyter');
    const panel = new BifrostPanel(
      app.serviceManager.serverSettings,
      notebooks,
      translator ?? undefined,
      openDashboardHandler(app, trans)
    );
    app.shell.add(panel, 'left', { rank: 200 });
  }
};

/**
 * Show a cluster's proxied Ray dashboard in a main-area tab, reusing the tab if
 * one is already open for that cluster.
 *
 * The URL comes from the panel (``api.dashboardUrl``) and is same-origin, so the
 * iframe is embeddable — see ``DashboardWidget`` for the framing evidence.
 */
function openDashboardHandler(
  app: JupyterFrontEnd,
  trans: TranslationBundle
): (args: { id: string; url: string }) => void {
  const open = new Map<string, Widget>();

  return ({ id, url }) => {
    const existing = open.get(id);
    if (existing && !existing.isDisposed) {
      app.shell.activateById(existing.id);
      return;
    }

    const widget = createDashboardWidget(id, url, trans);
    // Keyed by cluster id so a second click focuses the tab instead of stacking
    // duplicates; dropped again when the user closes it.
    open.set(id, widget);
    widget.disposed.connect(() => {
      if (open.get(id) === widget) {
        open.delete(id);
      }
    });

    app.shell.add(widget, 'main');
    app.shell.activateById(dashboardWidgetId(id));
  };
}

export default plugin;
