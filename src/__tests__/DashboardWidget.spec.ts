import { createDashboardWidget, dashboardWidgetId } from '../DashboardWidget';

import { nullTranslator } from '@jupyterlab/translation';

const trans = nullTranslator.load('bifrost-jupyter');

const URL = 'https://x.test/bifrost/clusters/jl-run-aaa/dashboard/';

describe('createDashboardWidget', () => {
  it('frames the same-origin proxied URL', () => {
    const widget = createDashboardWidget('jl-run-aaa', URL, trans);
    const frame = widget.node.querySelector(
      '.jp-BifrostDashboard-frame'
    ) as HTMLIFrameElement;

    expect(frame).not.toBeNull();
    expect(frame.getAttribute('src')).toBe(URL);
    // Same-origin and no token: the server extension is the only hop.
    expect(frame.getAttribute('src')).not.toContain('8265');
    expect(frame.getAttribute('src')).not.toContain('head-svc');
    widget.dispose();
  });

  it('does not sandbox the frame', () => {
    // A ``sandbox`` attribute without allow-scripts/allow-same-origin would
    // render the dashboard blank; the content is our own origin, proxied.
    const widget = createDashboardWidget('jl-run-aaa', URL, trans);
    const frame = widget.node.querySelector(
      '.jp-BifrostDashboard-frame'
    ) as HTMLIFrameElement;

    expect(frame.hasAttribute('sandbox')).toBe(false);
    widget.dispose();
  });

  it('offers a new-browser-tab escape hatch to the same URL', () => {
    const widget = createDashboardWidget('jl-run-aaa', URL, trans);
    const link = widget.node.querySelector(
      '.jp-BifrostDashboard-link'
    ) as HTMLAnchorElement;

    expect(link.getAttribute('href')).toBe(URL);
    expect(link.target).toBe('_blank');
    expect(link.rel).toContain('noopener');
    widget.dispose();
  });

  it('is a closable tab titled with the cluster id', () => {
    const widget = createDashboardWidget('jl-run-aaa', URL, trans);

    expect(widget.id).toBe('bifrost-dashboard-jl-run-aaa');
    expect(widget.title.label).toContain('jl-run-aaa');
    expect(widget.title.closable).toBe(true);
    widget.dispose();
  });
});

describe('dashboardWidgetId', () => {
  it('is stable per cluster so a second click can reuse the tab', () => {
    expect(dashboardWidgetId('jl-run-aaa')).toBe(
      dashboardWidgetId('jl-run-aaa')
    );
    expect(dashboardWidgetId('jl-run-aaa')).not.toBe(
      dashboardWidgetId('jl-run-bbb')
    );
  });

  it('keeps the id DOM-safe', () => {
    expect(dashboardWidgetId('a b/c"d')).toBe('bifrost-dashboard-a_b_c_d');
  });
});
