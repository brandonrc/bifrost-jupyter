import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';

import { requestAPI } from './request';

/**
 * Initialization data for the bifrost-jupyter extension.
 */
const plugin: JupyterFrontEndPlugin<void> = {
  id: 'bifrost-jupyter:plugin',
  description:
    'JupyterLab extension to start, stop, and connect to Bifrost-fronted Ray clusters',
  autoStart: true,
  activate: (app: JupyterFrontEnd) => {
    console.log('JupyterLab extension bifrost-jupyter is activated!');

    requestAPI<any>('hello', app.serviceManager.serverSettings)
      .then(data => {
        console.log(data);
      })
      .catch(reason => {
        console.error(
          `The bifrost_jupyter server extension appears to be missing.\n${reason}`
        );
      });
  }
};

export default plugin;
