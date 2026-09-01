import { expect, test } from '@jupyterlab/galata';

/**
 * Integration smoke test: the extension actually activates.
 *
 * The scaffold's default test asserted a console line
 * ('JupyterLab extension bifrost-jupyter is activated!') that the rewritten
 * plugin no longer emits. Instead we assert a real activation signal: the
 * plugin adds its "Ray Clusters" panel to the left sidebar, so the sidebar tab
 * bar renders a tab with the panel's widget id and, once opened, the panel's
 * own content mounts.
 */
test('registers the Ray Clusters panel on activation', async ({ page }) => {
  // JupyterLab stamps each sidebar tab with the widget id as `data-id`
  // (`app.shell.add(panel, 'left')` in src/index.ts uses panel id
  // 'bifrost-panel'). The tab renders even while the sidebar is collapsed.
  const tab = page.locator(
    '.jp-SideBar .lm-TabBar-tab[data-id="bifrost-panel"]'
  );
  await expect(tab).toHaveCount(1);

  // Opening the tab mounts the panel; its own header proves the widget (not
  // just a tab stub) rendered — i.e. the extension activated end to end.
  await tab.click();
  const panel = page.locator('#bifrost-panel');
  await expect(panel).toBeVisible();
  await expect(panel.locator('.jp-BifrostPanel-header')).toHaveText(
    'Ray Clusters'
  );
});
