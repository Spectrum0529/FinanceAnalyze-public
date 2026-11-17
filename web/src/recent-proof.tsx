import { useEffect, useId, useRef } from 'react';
import { keepDialogFocus } from './area-help';
import { hasCode } from './platform-account-session';
import type { AccountReauthentication } from './platform-account-session';
import { mountAccountForm } from './v434/assets/account-form.js';

export function RecentProof({ needsOtp, opener, verify, done, motion, description = '验证后继续刚才选择的连接。请填写平台账户密码。' }: {
  needsOtp: boolean; opener: HTMLElement | null; verify: AccountReauthentication;
  done: (verified: boolean) => void; motion: () => boolean; description?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const id = useId();
  // This dialog belongs to one immutable authorization action.
  const options = useRef({ needsOtp, opener, verify, done, motion, description }).current;
  useEffect(() => {
    const element = dialog.current;
    const host = content.current;
    if (!element || !host) return;
    const previous = options.opener ?? document.activeElement;
    const form = mountAccountForm(host, {
      id,
      heading: '再次验证身份',
      description: options.description,
      fields: [
        { name: 'password', label: '密码', type: 'password', autocomplete: 'current-password' },
        ...(options.needsOtp ? [{
          name: 'otp', label: '验证码', autocomplete: 'one-time-code', inputMode: 'numeric', maxLength: 6,
        }] : []),
      ],
      submitLabel: '验证身份',
      motion: options.motion,
      async submit(data, signal) {
        try {
          await options.verify(data, signal);
          return { confirmed: true, message: '身份已验证。' };
        } catch (error) {
          if (hasCode(error, 'MFA_INVALID') || hasCode(error, 'MFA_REQUIRED')) {
            return { message: '请检查验证器上的当前验证码。', field: 'otp' };
          }
          return { message: '身份验证未完成。请核对后重试。', field: 'password' };
        }
      },
      onConfirmed: () => options.done(true),
    });
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = '取消';
    cancel.addEventListener('click', () => options.done(false));
    form.actions.append(cancel);
    element.showModal();
    form.form.querySelector('input')?.focus();
    return () => {
      form.destroy();
      host.replaceChildren();
      element.close();
      if (previous instanceof HTMLElement) requestAnimationFrame(() => {
        if (previous.isConnected) previous.focus({ preventScroll: true });
      });
    };
  }, [id, options]);
  return <dialog className="help-dialog" ref={dialog} aria-labelledby={id + '-heading'} onKeyDown={keepDialogFocus}
    onCancel={event => { event.preventDefault(); options.done(false); }}>
    <div ref={content} />
  </dialog>;
}
