import { request } from './api';
import { accountWrite, hasCode, isAccountUser, isSessionRotation, reauthenticateAccount } from './platform-account-session';
import type { AccountRole, AccountUser, SessionRotation, WorkbenchSession } from './platform-account-session';
import { mountAccountForm } from './v434/assets/account-form.js';
import type { AccountField, AccountForm, FormReceipt } from './v434/assets/account-form.js';

type Page<T> = { items: T[]; total: number; limit: number; offset: number };
type SessionRow = { session_id: string; created_at: string; expires_at: string; current: boolean };
type UserRow = AccountUser & { enabled: boolean; created_at: string };
type Revocation = { revoked: number; current_revoked: boolean };
type Factors = { available: boolean; enabled: boolean };
const pageSize = 50;
const roles: Record<AccountRole, string> = { admin: '管理员', researcher: '研究员', viewer: '只读成员' };
const password = (name: string, label: string, autocomplete = 'current-password'): AccountField =>
  ({ name, label, autocomplete, type: 'password' });
const otp: AccountField = { name: 'otp', label: '验证码', autocomplete: 'one-time-code', inputMode: 'numeric', maxLength: 6 };
const value = (data: FormData, field: string) => String(data.get(field) ?? '');

function element<K extends keyof HTMLElementTagNameMap>(parent: HTMLElement, tag: K, text?: string) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  parent.append(node);
  return node;
}
function button(parent: HTMLElement, label: string, action: () => void) {
  const node = element(parent, 'button', label);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}
function when(time: string) {
  const date = new Date(time);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}
function failure(error: unknown): FormReceipt {
  if (hasCode(error, 'PASSWORD_POLICY')) {
    return { message: '请用 15 到 128 个字符，并避开常见密码。', field: 'new_password' };
  }
  if (hasCode(error, 'BAD_CREDENTIALS')) return { message: '验证未通过，请检查密码后重试。' };
  if (hasCode(error, 'MFA_INVALID') || hasCode(error, 'MFA_REQUIRED')) {
    return { message: '请输入验证器应用上当前显示的验证码。', field: 'otp' };
  }
  if (hasCode(error, 'LAST_ADMIN_REQUIRED')) return { message: '至少需要保留一名启用的管理员。' };
  if (hasCode(error, 'FORBIDDEN')) return { message: '当前账户无权进行这项操作。' };
  if (hasCode(error, 'CONFLICT')) return { message: '设置已发生变化，请刷新后重试。' };
  if (hasCode(error, 'RATE_LIMITED')) return { message: '尝试次数过多，请稍后重试。' };
  return { message: '暂时无法确认操作结果。请刷新后查看当前状态。' };
}

export function mountAccountSecurity(host: HTMLElement, session: WorkbenchSession, onSignedOut: () => void,
  onSessionRotated: (receipt: SessionRotation, signal: AbortSignal) => Promise<void>, motion: () => boolean) {
  const life = new AbortController();
  const root = element(host, 'div');
  root.className = 'account-security';
  element(root, 'p', `${session.user.username} · ${roles[session.user.role]}`);
  const notice = element(root, 'p');
  notice.setAttribute('role', 'status');
  const challenge = element(root, 'div');
  const content = element(root, 'div');
  content.className = 'account-security-content';
  const activeForms = new Set<AccountForm>();
  let factors: Factors | null = null;
  let reauth: AccountForm | undefined;
  let working = false;

  function section(heading: string) {
    const panel = element(content, 'section');
    element(panel, 'h2', heading);
    return panel;
  }
  function finishChallenge() {
    reauth?.destroy();
    reauth = undefined;
    challenge.replaceChildren();
    challenge.hidden = true;
    content.inert = false;
  }
  challenge.hidden = true;

  function authenticate(signal: AbortSignal, description = '输入密码验证身份，再继续刚才的操作。') {
    return new Promise<boolean>(resolve => {
      if (signal.aborted || life.signal.aborted || reauth) { resolve(false); return; }
      const previousFocus = document.activeElement;
      challenge.hidden = false;
      content.inert = true;
      function finish(confirmed: boolean) {
        signal.removeEventListener('abort', cancel);
        life.signal.removeEventListener('abort', cancel);
        finishChallenge();
        if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
        resolve(confirmed);
      }
      const cancel = () => finish(false);
      signal.addEventListener('abort', cancel, { once: true });
      life.signal.addEventListener('abort', cancel, { once: true });
      reauth = mountAccountForm(challenge, {
        id: 'security-confirm', heading: '再次验证身份', description,
        fields: [password('password', '密码'), ...(factors?.enabled ? [otp] : [])],
        submitLabel: '验证身份', motion,
        async submit(data, authSignal) {
          try {
            await reauthenticateAccount(data, authSignal, onSessionRotated);
            return { confirmed: true, message: '身份已验证。' };
          } catch (error) { return failure(error); }
        },
        onConfirmed: () => finish(true),
      });
      button(reauth.actions, '取消', cancel);
      reauth.form.querySelector('input')?.focus();
    });
  }
  async function withRecent<T>(action: () => Promise<T>, signal: AbortSignal) {
    try { return await action(); } catch (error) {
      if (!hasCode(error, 'REAUTH_REQUIRED')) throw error;
      if (!await authenticate(signal) || signal.aborted) throw Error('Verification cancelled');
      return action();
    }
  }
  async function run(action: () => Promise<void>) {
    if (working || life.signal.aborted) return;
    working = true;
    notice.textContent = '正在处理…';
    try { await action(); } catch (error) {
      if (!life.signal.aborted) notice.textContent = failure(error).message ?? '';
    } finally { working = false; }
  }
  function hideAfterRevocation(result: Revocation) {
    if (!result || !Number.isInteger(result.revoked) || typeof result.current_revoked !== 'boolean') throw Error('Invalid revocation receipt');
    if (result.current_revoked) { onSignedOut(); return true; }
    notice.textContent = `已结束 ${result.revoked} 条登录记录。`;
    return false;
  }
  button(root, '验证当前身份', () => void run(async () => {
    if (factors === null) await loadFactors();
    const ok = await authenticate(life.signal, '验证后，回到原页面完成操作。');
    if (!life.signal.aborted) notice.textContent = ok ? '身份已验证。' : '';
  })).className = 'account-verify-button';
  function navigation(panel: HTMLElement, page: Page<unknown>, load: (offset: number) => Promise<void>) {
    const group = element(panel, 'div');
    group.className = 'account-security-actions';
    if (page.offset > 0) button(group, '上一页', () => void run(() => load(Math.max(0, page.offset - pageSize))));
    if (page.offset + page.items.length < page.total) button(group, '下一页', () => void run(() => load(page.offset + pageSize)));
  }
  const sessions = section('已登录的设备');
  element(sessions, 'p', '让一台设备退出后，下次使用需要重新登录。');
  const sessionActions = element(sessions, 'div');
  sessionActions.className = 'account-security-actions';
  button(sessionActions, '刷新登录列表', () => void run(() => loadSessions(0)));
  for (const [scope, label] of [['others', '退出其他设备'], ['all', '退出所有设备']] as const) {
    button(sessionActions, label, () => void run(async () => {
      const result = await withRecent(() => accountWrite<Revocation>(
        '/auth/sessions/revoke', 'POST', { scope }, life.signal,
      ), life.signal);
      if (!hideAfterRevocation(result)) await loadSessions(0, true);
    }));
  }
  const sessionList = element(sessions, 'div');
  async function loadSessions(offset: number, preserveNotice = false) {
    const page = await request<Page<SessionRow>>(`/auth/sessions?limit=${pageSize}&offset=${offset}`, { signal: life.signal });
    if (life.signal.aborted) return;
    sessionList.replaceChildren();
    const list = element(sessionList, 'ul');
    for (const row of page.items) {
      const item = element(list, 'li');
      element(item, 'div', `${row.current ? '当前登录' : '其他设备'} · 登录于 ${when(row.created_at)} · 到期于 ${when(row.expires_at)}`);
      button(item, row.current ? '退出本次登录' : '让这台设备退出', () => void run(async () => {
        const result = await withRecent(() => accountWrite<Revocation>(
          '/auth/sessions/' + encodeURIComponent(row.session_id), 'DELETE', undefined, life.signal,
        ), life.signal);
        if (!hideAfterRevocation(result)) await loadSessions(offset, true);
      }));
    }
    if (!page.items.length) element(sessionList, 'p', '这一页没有有效的登录记录。');
    navigation(sessionList, page, loadSessions);
    if (!preserveNotice) notice.textContent = '';
  }

  const change = section('修改密码');
  const changeContent = element(change, 'div');
  let changeForm: AccountForm | undefined;
  function updatePasswordForm() {
    changeForm?.destroy();
    changeContent.replaceChildren();
    changeForm = mountAccountForm(changeContent, {
      id: 'security-password', heading: '设置新密码', headingLevel: 3,
      description: '密码需要 15 到 128 个字符。保存后所有设备都要重新登录。',
      fields: [password('current_password', '当前密码'), password('new_password', '新密码', 'new-password'), ...(factors?.enabled ? [otp] : [])],
      submitLabel: '保存新密码', motion,
      async submit(data, signal) {
        try {
          const receipt = await accountWrite<{ changed: boolean }>('/auth/password', 'POST', {
            current_password: value(data, 'current_password'), new_password: value(data, 'new_password'),
            ...(data.has('otp') ? { otp: value(data, 'otp') } : {}),
          }, signal);
          return receipt?.changed === true
            ? { confirmed: true, message: '密码已更新，请重新登录。' } : failure(null);
        } catch (error) { return failure(error); }
      },
      onConfirmed: onSignedOut,
    });
  }

  const recovery = section('恢复码');
  const recoveryStatus = element(recovery, 'p');
  const codes = element(recovery, 'div');
  button(recovery, '生成新一组恢复码', () => void run(async () => {
    codes.replaceChildren();
    const receipt = await withRecent(() => accountWrite<{ codes: string[]; expires_at: string }>(
      '/auth/recovery/codes', 'POST', undefined, life.signal,
    ), life.signal);
    if (life.signal.aborted) return;
    if (!Array.isArray(receipt?.codes) || !receipt.codes.length || !receipt.codes.every(code => typeof code === 'string')) throw Error('Invalid recovery receipt');
    element(codes, 'p', '请现在将恢复码保存在安全的离线位置。这组只显示一次，旧恢复码已失效。');
    const list = element(codes, 'ul');
    for (const code of receipt.codes) element(element(list, 'li'), 'code', code);
    button(codes, '保存后隐藏恢复码', () => codes.replaceChildren());
    await loadRecovery();
    notice.textContent = '新恢复码已生成。';
  }));
  element(recovery, 'p', '忘记密码时可用恢复码重设密码。生成新的一组会让旧恢复码失效。');
  async function loadRecovery() {
    const data = await request<{ remaining: number; expires_at: string | null }>('/auth/recovery', { signal: life.signal });
    if (!life.signal.aborted) recoveryStatus.textContent = `剩余 ${data.remaining} 个${data.expires_at ? ' · 到期于 ' + when(data.expires_at) : ''}`;
  }

  const mfa = section('两步验证');
  const refreshFactors = button(mfa, '刷新验证状态', () => void run(async () => { await loadFactors(); notice.textContent = ''; }));
  const factorContent = element(mfa, 'div');
  let enrolment: AccountForm | undefined;
  function clearEnrollment(message?: string) {
    enrolment?.destroy();
    enrolment = undefined;
    factorContent.replaceChildren();
    if (message) element(factorContent, 'p', message);
  }
  function updateFactors(updated: Factors) {
    const previouslyEnabled = factors?.enabled;
    factors = updated;
    if (previouslyEnabled !== updated.enabled) updatePasswordForm();
  }
  async function loadFactors() {
    const updated = await request<Factors>('/auth/mfa', { signal: life.signal });
    if (life.signal.aborted) return;
    updateFactors(updated);
    clearEnrollment();
    if (!updated.available) {
      element(factorContent, 'p', updated.enabled
        ? '服务暂时无法读取已设置的验证器。请联系运行服务的电脑的管理员。'
        : '请管理员先在启动器或服务器上启用两步验证。');
      return;
    }
    element(factorContent, 'p', updated.enabled ? '两步验证已开启。更改后，其他设备需要重新登录。' : '设置后，每次登录都需要输入验证器应用上的验证码。');
    if (updated.enabled) {
      button(factorContent, '停用两步验证', () => void run(async () => {
        const receipt = await withRecent(() => accountWrite<{ enabled: boolean }>(
          '/auth/mfa', 'DELETE', undefined, life.signal,
        ), life.signal);
        if (receipt?.enabled !== false) throw Error('Invalid factor receipt');
        await loadFactors();
        notice.textContent = '两步验证已停用。';
      }));
      return;
    }
    button(factorContent, '设置两步验证', () => void run(async () => {
      const receipt = await withRecent(() => accountWrite<{ setup_secret: string; expires_at: string }>(
        '/auth/mfa/enroll', 'POST', undefined, life.signal,
      ), life.signal);
      if (life.signal.aborted) return;
      if (typeof receipt?.setup_secret !== 'string' || !receipt.setup_secret) throw Error('Invalid enrollment receipt');
      factorContent.replaceChildren();
      element(factorContent, 'p', `把下方密钥手动添加到验证器应用，再输入应用显示的验证码。请于 ${when(receipt.expires_at)} 前完成。`);
      element(factorContent, 'code', receipt.setup_secret);
      enrolment = mountAccountForm(factorContent, {
        id: 'security-enable-mfa', heading: '输入验证码', headingLevel: 3, fields: [otp], submitLabel: '启用两步验证', motion,
        async submit(data, signal) {
          refreshFactors.disabled = true;
          try {
            const receipt = await withRecent(() => accountWrite<SessionRotation & { enabled: boolean }>(
              '/auth/mfa/confirm', 'POST', { otp: value(data, 'otp') }, signal,
            ), signal);
            if (receipt?.enabled !== true) return failure(null);
            clearEnrollment('两步验证已开启，正在更新本次登录…');
            updateFactors({ available: true, enabled: true });
            // Disposing the secret-bearing form aborts its signal. Rotation belongs to the panel.
            if (!isSessionRotation(receipt)) throw Error('Invalid rotation receipt');
            await onSessionRotated(receipt, life.signal);
            await loadFactors();
            if (!life.signal.aborted) notice.textContent = '两步验证已启用。';
            return { confirmed: true, message: '两步验证已启用。' };
          } catch (error) {
            const rejected = failure(error);
            if (!life.signal.aborted && !enrolment) {
              factorContent.replaceChildren();
              element(factorContent, 'p', '两步验证已启用。请刷新验证状态。');
              notice.textContent = rejected.message ?? '';
            }
            return rejected;
          } finally {
            refreshFactors.disabled = false;
          }
        },
      });
      button(enrolment.actions, '取消设置', () => {
        clearEnrollment('已取消设置。');
        refreshFactors.focus();
        void run(async () => { await loadFactors(); notice.textContent = ''; });
      });
      notice.textContent = '';
    }));
  }

  if (session.user.role === 'admin') {
    const admin = section('账户管理');
    const users = element(admin, 'div');
    function roleSelect(container: HTMLElement, initial: AccountRole) {
      const label = element(container, 'label', '角色 ');
      const select = element(label, 'select');
      select.name = 'role';
      for (const [key, caption] of Object.entries(roles)) element(select, 'option', caption).value = key;
      select.value = initial;
      return select;
    }
    const createUser = mountAccountForm(admin, {
      id: 'security-new-user', heading: '创建账户', headingLevel: 3, submitLabel: '创建账户', motion,
      description: '初始密码需要 15 到 128 个字符。请避开常见密码。',
      fields: [
        { name: 'username', label: '用户名', autocomplete: 'off', maxLength: 64 },
        password('password', '初始密码', 'new-password'),
      ],
      async submit(data, signal) {
        try {
          const result = await withRecent(() => accountWrite('/users', 'POST', {
            username: value(data, 'username'), password: value(data, 'password'), role: value(data, 'role'),
          }, signal), signal);
          return isAccountUser(result) ? { confirmed: true, message: '账户已创建。' } : failure(null);
        } catch (error) { return failure(error); }
      },
      onConfirmed: () => {
        // Refreshing reads the authoritative list. Reset the form for another creation.
        createUser.reset();
        void run(async () => { await loadUsers(0); notice.textContent = '账户已创建。'; });
      },
    });
    const creationRole = roleSelect(createUser.form, 'researcher');
    createUser.actions.before(creationRole.parentElement!);
    activeForms.add(createUser);
    async function loadUsers(offset: number) {
      const page = await request<Page<UserRow>>(`/users?limit=${pageSize}&offset=${offset}`, { signal: life.signal });
      if (life.signal.aborted) return;
      users.replaceChildren();
      const list = element(users, 'ul');
      for (const user of page.items) {
        const item = element(list, 'li');
        element(item, 'p', `${user.username} · ${user.enabled ? '启用' : '停用'}`);
        const role = roleSelect(item, user.role);
        button(item, '保存角色', () => void updateUser(user, { role: role.value as AccountRole }, offset));
        button(item, user.enabled ? '停用账户' : '启用账户', () => void updateUser(user, { enabled: !user.enabled }, offset));
      }
      navigation(users, page, loadUsers);
      notice.textContent = '';
    }
    async function updateUser(user: UserRow, payload: { role?: AccountRole; enabled?: boolean }, offset: number) {
      await run(async () => {
        const result = await withRecent(() => accountWrite('/users/' + encodeURIComponent(user.user_id), 'PATCH', payload, life.signal), life.signal);
        if (!isAccountUser(result)) throw Error('Invalid user receipt');
        if (user.user_id === session.user.user_id && (result.role !== user.role || payload.enabled === false)) { onSignedOut(); return; }
        await loadUsers(offset);
        notice.textContent = '账户设置已保存。';
      });
    }
    button(admin, '刷新账户列表', () => void run(() => loadUsers(0)));
    void loadUsers(0).catch(() => { if (!life.signal.aborted) notice.textContent = '无法读取账户列表。'; });
  }
  void Promise.allSettled([loadSessions(0), loadRecovery(), loadFactors()]).then(results => {
    if (!life.signal.aborted && results.some(result => result.status === 'rejected')) {
      notice.textContent = '部分账户状态暂时无法读取。请刷新后重试。';
    }
  });
  return () => {
    life.abort();
    reauth?.destroy();
    changeForm?.destroy();
    enrolment?.destroy();
    for (const form of activeForms) form.destroy();
    root.remove();
  };
}
