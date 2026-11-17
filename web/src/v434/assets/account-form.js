function append(parent, tag, attributes = {}, text = '') {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    element.setAttribute(name, value);
  }
  if (text) element.textContent = text;
  parent.append(element);
  return element;
}

// The caller translates the server response into a confirmed or rejected
// receipt. This view makes no authentication decisions and sends no requests.
export function mountAccountForm(host, {
  id, heading, headingLevel = 2, description, fields, submitLabel,
  pendingLabel = '正在验证…', submit, motion = () => true,
  phaseTarget = host, onPhase, onConfirmed, onRejected,
}) {
  const life = new AbortController();
  const { signal } = life;
  const media = matchMedia('(prefers-reduced-motion: reduce)');
  const form = append(host, 'form', {
    class: 'account-form', method: 'post', 'aria-labelledby': id + '-heading',
  });
  append(form, 'h' + headingLevel, { id: id + '-heading' }, heading);
  if (description) append(form, 'p', { class: 'account-form-description' }, description);
  const inputs = new Map();
  const masks = [];
  const secrets = [];
  const toggles = [];

  for (const field of fields) {
    const inputId = id + '-' + field.name;
    const group = append(form, 'div', { class: 'account-form-field' });
    append(group, 'label', { for: inputId }, field.label);
    const row = append(group, 'div', { class: 'account-form-control' });
    const input = append(row, 'input', {
      id: inputId, name: field.name, type: field.type ?? 'text',
      autocomplete: field.autocomplete ?? 'off', autocapitalize: 'none', spellcheck: 'false',
    });
    input.required = field.required !== false;
    if (field.value != null && field.type !== 'password') input.value = field.value;
    if (field.maxLength) input.maxLength = field.maxLength;
    if (field.inputMode) input.inputMode = field.inputMode;
    input.setAttribute('aria-describedby', field.help ? inputId + '-help ' + id + '-error' : id + '-error');
    if (field.help) append(group, 'small', { id: inputId + '-help' }, field.help);
    inputs.set(field.name, input);
    if (field.type === 'password' || field.autocomplete === 'one-time-code') secrets.push(input);
    if (field.type !== 'password') continue;
    const toggle = append(row, 'button', { type: 'button', 'aria-controls': inputId });
    function setVisible(visible) {
      input.type = visible ? 'text' : 'password';
      toggle.textContent = visible ? '隐藏' : '显示';
      toggle.setAttribute('aria-label', (visible ? '隐藏' : '显示') + field.label);
    }
    setVisible(false);
    toggle.addEventListener('click', () => setVisible(input.type === 'password'), { signal });
    masks.push({ input, setVisible });
    toggles.push(toggle);
  }

  const error = append(form, 'p', { class: 'account-form-error', id: id + '-error', role: 'alert' });
  const feedback = append(form, 'span', { class: 'account-form-feedback', 'aria-hidden': 'true' });
  const actions = append(form, 'div', { class: 'account-form-actions' });
  const button = append(actions, 'button', { type: 'submit', class: 'account-form-submit' }, submitLabel);
  const status = append(form, 'p', { class: 'account-form-status', role: 'status' });
  let phase = 'idle';
  let animation;

  function updatePhase(next) {
    phase = next;
    phaseTarget.dataset.phase = next;
    form.dataset.phase = next;
    onPhase?.(next);
  }

  function cancelAnimation() {
    animation?.cancel();
    animation = undefined;
  }

  function canAnimate() {
    return motion() && !media.matches && !document.hidden;
  }

  function animate(element, frames, duration) {
    cancelAnimation();
    if (!canAnimate()) return;
    animation = element.animate(frames, { duration, easing: 'cubic-bezier(.2,.72,.28,1)' });
    return animation;
  }

  function setLocked(locked) {
    button.disabled = locked;
    for (const toggle of toggles) toggle.disabled = locked;
    for (const input of inputs.values()) input.readOnly = locked;
    for (const extra of form.querySelectorAll('.account-form-actions button:not([type="submit"]),input[type="checkbox"],select')) extra.disabled = locked;
    form.setAttribute('aria-busy', String(phase === 'verifying'));
    button.textContent = phase === 'verifying' ? pendingLabel : submitLabel;
  }

  function maskSecrets(clear = false) {
    for (const { setVisible } of masks) setVisible(false);
    if (clear) for (const input of secrets) input.value = '';
  }

  function clearError() {
    error.textContent = '';
    for (const input of inputs.values()) input.removeAttribute('aria-invalid');
  }

  form.addEventListener('input', event => {
    if (phase === 'verifying' || phase === 'granted') return;
    if (phase === 'error') {
      clearError();
      updatePhase('idle');
    }
    if (!masks.some(({ input }) => input === event.target)) return;
    if (animation?.playState === 'running' && animation.effect?.target === feedback) return;
    animate(feedback, [
      { opacity: 0, transform: 'scaleX(.06)' },
      { opacity: .85, transform: 'scaleX(.45)', offset: .28 },
      { opacity: 0, transform: 'scaleX(1)' },
    ], 280);
  }, { signal });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (phase === 'verifying' || phase === 'granted') return;
    clearError();
    cancelAnimation();
    const values = new FormData(form);
    maskSecrets();
    updatePhase('verifying');
    setLocked(true);
    status.textContent = pendingLabel;
    let receipt;
    try {
      receipt = await submit(values, signal);
    } catch {
      receipt = { confirmed: false, message: '暂时无法验证，请重试。' };
    } finally {
      for (const input of secrets) values.delete(input.name);
    }
    if (signal.aborted) return;
    maskSecrets(true);

    if (receipt?.confirmed === true) {
      updatePhase('granted');
      setLocked(true);
      status.textContent = receipt.message || '操作完成。';
      const completion = animate(status, [
        { opacity: .2, transform: 'translateY(5px)' },
        { opacity: 1, transform: 'translateY(0)' },
      ], 360);
      if (completion) await completion.finished.catch(() => {});
      if (!signal.aborted) onConfirmed?.(receipt);
      return;
    }

    status.textContent = '';
    updatePhase('error');
    setLocked(false);
    error.textContent = receipt?.message || '验证未通过，请重试。';
    const invalid = inputs.get(receipt?.field);
    invalid?.setAttribute('aria-invalid', 'true');
    animate(error, [
      { opacity: .35, transform: 'translateX(-4px)' },
      { opacity: 1, transform: 'translateX(2px)', offset: .42 },
      { opacity: 1, transform: 'translateX(0)' },
    ], 320);
    (invalid ?? masks[0]?.input ?? button).focus();
    onRejected?.(receipt);
  }, { signal });

  function refreshMotion() {
    form.classList.toggle('account-motion-off', !canAnimate());
    if (!canAnimate()) cancelAnimation();
    if (document.hidden) maskSecrets();
  }

  document.addEventListener('visibilitychange', refreshMotion, { signal });
  document.addEventListener('account-motion-change', refreshMotion, { signal });
  media.addEventListener('change', refreshMotion, { signal });
  updatePhase('idle');
  refreshMotion();

  return {
    form, actions, refreshMotion,
    reset() {
      if (phase === 'verifying') return;
      form.reset();
      maskSecrets(true);
      clearError();
      status.textContent = '';
      updatePhase('idle');
      setLocked(false);
    },
    destroy() {
      life.abort();
      cancelAnimation();
      maskSecrets(true);
      if (form.dataset.phase === phaseTarget.dataset.phase) delete phaseTarget.dataset.phase;
    },
  };
}
