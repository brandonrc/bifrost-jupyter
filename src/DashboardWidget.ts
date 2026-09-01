import { TranslationBundle } from '@jupyterlab/translation';

import { Widget } from '@lumino/widgets';

/**
 * A JupyterLab main-area tab showing a cluster's Ray dashboard in an iframe.
 *
 * The dashboard is **not** loaded from the cluster: it is re-served same-origin
 * by the co-located server extension at ``/bifrost/clusters/{id}/dashboard/``
 * (see ``api.dashboardUrl``), so this iframe stays on the Jupyter origin and
 * needs no token.
 *
 * Framing is safe here, and that was checked rather than assumed:
 * - Ray's dashboard server sets no ``X-Frame-Options`` and no
 *   ``Content-Security-Policy`` of its own (``ray/dashboard/http_server_head.py``
 *   sets only ``Cache-Control``), so nothing upstream forbids embedding — and the
 *   proxy drops those headers anyway if a future Ray adds them.
 * - The response carries jupyter-server's own CSP, ``frame-ancestors 'self'``,
 *   which permits exactly this same-origin embed. (The proxy route is a plain
 *   ``JupyterHandler``, not an ``APIHandler``, so it does *not* pick up
 *   ``default-src 'none'`` — that would have left the frame blank.)
 * - Ray builds the dashboard with ``PUBLIC_URL="."`` and routes with a
 *   ``HashRouter``, so its assets and API calls resolve relative to this URL.
 *
 * A plain link to the same URL sits above the frame regardless: some users just
 * want the dashboard in its own browser tab, and it is a free escape hatch if a
 * future Ray release ever does start refusing to be framed.
 */
export function createDashboardWidget(
  clusterId: string,
  url: string,
  trans: TranslationBundle
): Widget {
  const widget = new Widget();
  widget.id = dashboardWidgetId(clusterId);
  widget.addClass('jp-BifrostDashboard');
  widget.title.label = trans.__('Ray dashboard: %1', clusterId);
  widget.title.caption = trans.__('Ray dashboard for cluster %1', clusterId);
  widget.title.closable = true;

  const toolbar = document.createElement('div');
  toolbar.className = 'jp-BifrostDashboard-toolbar';

  const label = document.createElement('span');
  label.className = 'jp-BifrostDashboard-cluster';
  label.textContent = clusterId;
  toolbar.appendChild(label);

  const link = document.createElement('a');
  link.className = 'jp-BifrostDashboard-link';
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.textContent = trans.__('Open in a new browser tab');
  toolbar.appendChild(link);

  const frame = document.createElement('iframe');
  frame.className = 'jp-BifrostDashboard-frame';
  frame.src = url;
  frame.title = widget.title.caption;

  widget.node.appendChild(toolbar);
  widget.node.appendChild(frame);
  return widget;
}

/** The stable, DOM-safe widget id for one cluster's dashboard tab. */
export function dashboardWidgetId(clusterId: string): string {
  // Cluster ids are Bifrost-generated slugs, but an id is a DOM attribute — keep
  // it to characters that are unambiguously safe there.
  return `bifrost-dashboard-${clusterId.replace(/[^\w-]/g, '_')}`;
}
