import { mountAccountForm } from './assets/account-form.js';
import { prepareAccountTerminal } from './assets/account-terminal.js';
import { accountWrite, hasCode, isAccountUser, signIn } from '../platform-account-session';
import './assets/account-form.css';

const copy = {
  login: { heading: '登录研究工作区', action: '登录' },
  otp: { heading: '完成两步验证', description: '输入密码和验证器应用上当前显示的验证码。', action: '验证并登录' },
  setup: { heading: '建立管理员账户', description: '请输入这台电脑的启动器或管理命令生成的一次性设置码。', action: '建立并登录' },
  recovery: {
    heading: '恢复账户', description: '用你保存的一次性恢复码设置新密码，然后重新登录。',
    action: '重设密码',
  },
  'local-recovery': {
    heading: '在本机恢复管理员',
    description: '请输入本机管理命令生成的恢复码。恢复码只能使用一次，15 分钟后过期。重设密码会同时停用原有的两步验证和恢复码，并让所有设备退出。',
    action: '重设管理员密码',
  },
};

const secretField = (name, label, autocomplete = 'off', help) => ({
  name, label, type: 'password', autocomplete, help,
});
const passwordPolicy = '密码需要 15 到 128 个字符。';

export function mountAccountWelcome(terminal, ribbon, {
  status, motion, onAuthenticated, onSignedIn, onRecovered, onRetry,
}) {
  const { panel, setPhase } = prepareAccountTerminal(terminal, ribbon, { motion });
  let mount;
  let rememberedUser = '';
  let remember = false;
  let setupComplete = false;
  const defaultMode = status.initialized ? 'login' : 'setup';

  function render(mode, { focus = false, note } = {}) {
    mount?.destroy();
    panel.replaceChildren();
    const config = copy[mode];
    if (mode === 'setup' && !['first_admin', 'upgrade_personal'].includes(status.setup_kind)) {
      const p = document.createElement('p');
      p.setAttribute('role', 'status');
      p.textContent = status.account_state === 'ambiguous'
        ? '暂时无法确认这份工作区属于哪个账户。请管理员在运行服务的电脑上核对原有账户和数据，再点“重新检查”。'
        : '请管理员先在运行服务的电脑上完成账户设置，再点“重新检查”。';
      panel.append(p);
      addButton(panel, '重新检查', onRetry);
      return;
    }
    const username = { name: 'username', label: '用户名', value: rememberedUser, autocomplete: 'username', maxLength: 64 };
    const password = secretField('password', '密码', 'current-password');
    const newPassword = secretField('new_password', '新密码', 'new-password', passwordPolicy);
    const recovering = mode === 'recovery' || mode === 'local-recovery';
    const fields = mode === 'setup' ? [
      secretField('setup_token', '一次性设置码'), username,
      secretField('password', '新密码', 'new-password', passwordPolicy),
    ] : recovering ? [username, secretField('recovery_code', '恢复码'), newPassword]
      : [username, password, ...(mode === 'otp' ? [{
        name: 'otp', label: '验证码', autocomplete: 'one-time-code', inputMode: 'numeric', maxLength: 6,
      }] : [])];
    let description = config.description;
    if (mode === 'setup' && status.setup_kind === 'upgrade_personal') {
      description = '请输入本机生成的一次性设置码。新管理员账户将接管原有个人工作区。';
    }
    if (mode === 'login' && status.account_state === 'ambiguous') {
      description = '请用已有账户登录。要接管原有个人工作区，请管理员先在运行服务的电脑上核对原有数据和账户归属。';
    }
    mount = mountAccountForm(panel, {
      id: 'account-' + mode, heading: config.heading, headingLevel: 2, description: note ?? description, fields,
      submitLabel: config.action, phaseTarget: terminal, motion, onPhase: setPhase,
      async submit(data, signal) {
        const value = name => String(data.get(name) ?? '');
        rememberedUser = value('username');
        if (mode === 'login' || mode === 'otp') remember = data.has('remember');
        try {
          if (recovering) {
            const receipt = await accountWrite(mode === 'local-recovery' ? '/auth/recover/local' : '/auth/recover', 'POST', {
              username: rememberedUser, recovery_code: value('recovery_code'), new_password: value('new_password'),
            }, signal);
            if (receipt?.recovered !== true) throw new Error('Recovery receipt missing');
            return { confirmed: true, message: '密码已重设，请重新登录。' };
          }
          if (mode === 'setup') {
            const receipt = await accountWrite('/auth/initialize', 'POST', {
              setup_token: value('setup_token'), username: rememberedUser, password: value('password'),
            }, signal);
            if (!isAccountUser(receipt)) throw new Error('Initialization receipt missing');
            setupComplete = true;
          }
          const session = await signIn({
            username: rememberedUser, password: value('password'), remember,
            ...(mode === 'otp' ? { otp: value('otp') } : {}),
          }, signal);
          // The cookie has already changed. Invalidate other views before the success animation.
          onAuthenticated();
          return { confirmed: true, message: '登录成功，正在打开研究工作区。', session };
        } catch (error) {
          if (setupComplete && mode === 'setup') {
            return { next: 'login', message: '管理员账户已建立，请使用新密码登录。' };
          }
          if (hasCode(error, 'MFA_REQUIRED') && mode === 'login') {
            return { next: 'otp', message: copy.otp.description };
          }
          if (hasCode(error, 'MFA_INVALID')) return { message: '验证码无效或已过期，请重试。', field: 'otp' };
          if (hasCode(error, 'MFA_UNAVAILABLE')) return { message: '服务暂时无法检查验证码。请联系管理员。' };
          if (hasCode(error, 'BAD_CREDENTIALS')) return { message: '登录未通过，请检查用户名和密码。', field: 'password' };
          if (hasCode(error, 'SETUP_REJECTED')) return { message: '设置码无效或已过期，请在本机重新获取。', field: 'setup_token' };
          if (hasCode(error, 'RECOVERY_REJECTED')) return { message: '恢复未通过，请检查用户名和恢复码。', field: 'recovery_code' };
          if (hasCode(error, 'PASSWORD_POLICY')) return { message: '请用 15 到 128 个字符，并避开常见密码。', field: mode === 'setup' ? 'password' : 'new_password' };
          if (hasCode(error, 'RATE_LIMITED')) return { message: '尝试次数过多，请稍后重试。' };
          return { message: '暂时无法验证，请稍后重试。' };
        }
      },
      onRejected(receipt) {
        if (receipt?.next) render(receipt.next, { focus: true, note: receipt.message });
      },
      onConfirmed(receipt) {
        if (recovering) onRecovered();
        else onSignedIn(receipt.session);
      },
    });
    if (mode === 'login' || mode === 'otp') {
      const label = document.createElement('label');
      label.className = 'account-remember';
      const check = document.createElement('input');
      check.type = 'checkbox';
      check.name = 'remember';
      check.checked = remember;
      label.append(check, ' 此设备保持登录（最多 7 天）');
      mount.actions.before(label);
      if (mode === 'login' && status.recovery_supported) addButton(mount.actions, '使用恢复码', () => render('recovery', { focus: true }));
      if (mode === 'login' && ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) {
        addButton(mount.actions, '本机管理员恢复', () => render('local-recovery', { focus: true }));
      }
    }
    if (mode === 'otp' || recovering) addButton(mount.actions, '返回登录', () => render('login', { focus: true }));
    if (mode === 'setup') addButton(mount.actions, '重新检查', onRetry);
    if (focus) mount.form.querySelector(rememberedUser && mode === 'otp' ? '[name="password"]' : 'input').focus();
  }
  render(defaultMode);
  return {
    refreshMotion() { mount?.refreshMotion(); },
    destroy() {
      mount?.destroy();
      ribbon.setState?.('idle');
    },
  };
}

function addButton(parent, label, action) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'account-secondary';
  button.textContent = label;
  button.addEventListener('click', action);
  parent.append(button);
}
