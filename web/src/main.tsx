import {startShell} from './v434/shell.js';
import {acceptSession,checkAccess,getAccountSession,logoutAccount,watchAccountEvents} from './platform-account-session';

type MountBusiness = Parameters<typeof startShell>[0]['mountBusiness'];
let warmScheduled = false;
let preparedMount: MountBusiness | undefined;

async function loadBusiness() {
  return preparedMount ??= (await import('./v434-business')).mountBusiness;
}

function warmBusiness(immediate = false) {
  if (warmScheduled || preparedMount) return;
  warmScheduled = true;
  const prepare = () => {
    const route = document.body.dataset.route;
    if (document.hidden || (route !== 'workspace' && route !== 'login')) {
      warmScheduled = false;
      return;
    }
    // Fetch and prepare code only. A mounted, current session still owns all API use.
    void loadBusiness().catch(() => { warmScheduled = false; });
  };
  if (immediate) prepare();
  else if (typeof window.requestIdleCallback === 'function') window.requestIdleCallback(prepare);
  else window.setTimeout(prepare, 120);
}

const mountBusiness: MountBusiness = (host, ...args) => {
  if (preparedMount) return preparedMount(host, ...args);
  const status = document.createElement('p');
  status.className = 'workbench-loading';
  status.setAttribute('role', 'status');
  status.textContent = '正在打开页面…';
  host.append(status);

  let active = true;
  let unmount: (() => void) | undefined;
  void loadBusiness().then(mount => {
    if (!active) return;
    status.remove();
    unmount = mount(host, ...args);
  }).catch(() => {
    if (!active) return;
    const error = document.createElement('p');
    error.setAttribute('role', 'alert');
    error.textContent = '暂时打不开这页。请刷新页面，或从菜单打开其他页面。';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'secondary';
    retry.textContent = '刷新页面';
    retry.addEventListener('click', () => window.location.reload());
    host.replaceChildren(error, retry);
  });

  return () => {
    active = false;
    unmount?.();
  };
};

startShell({acceptSession,checkAccess,getAccountSession,logoutAccount,watchAccountEvents,mountBusiness,warmBusiness});
