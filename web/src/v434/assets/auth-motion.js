// Local presentation state only. This controller never sends credentials.
export function createTerminalController(root, ribbon, { motion = () => true, onPhase = () => {}, onTyping = () => {} } = {}) {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const events = new AbortController();
  const timers = new Map();
  const impactAnimations = new Set();
  const find = selector => root.querySelector(selector);
  let alive = true;
  let busy = false;
  let sequence = 0;
  let pointerFrame = 0;
  let typingTimer = 0;
  let alarmTimer = 0;
  let impactTimer = 0;
  let impactEchoTimer = 0;
  let rippleSerial = 0;
  let logNumber = find('#auth-log')?.children.length || 0;
  let pointer = { x: 0, y: 0 };

  const animated = () => motion() && !reducedMotion.matches && !document.hidden;
  const schedule = (callback, delay, cancelled) => {
    const id = window.setTimeout(() => {
      timers.delete(id);
      if (alive) callback();
    }, delay);
    timers.set(id, cancelled);
    return id;
  };
  const clear = id => {
    if (!id) return;
    window.clearTimeout(id);
    timers.get(id)?.();
    timers.delete(id);
  };
  const wait = duration => new Promise(resolve => {
    if (!alive) return resolve(false);
    schedule(() => resolve(true), animated() ? duration : 30, () => resolve(false));
  });
  const text = (selector, value) => {
    const element = find(selector);
    if (element) element.textContent = value;
  };

  function log(message, tone = '') {
    const container = find('#auth-log');
    if (!container) return;
    const row = document.createElement('div');
    row.className = `auth-log-line ${tone}`;
    const number = document.createElement('span');
    number.textContent = String(++logNumber).padStart(2, '0');
    const content = document.createElement('span');
    content.textContent = message;
    row.append(number, content);
    container.append(row);
    while (container.children.length > 7) container.firstElementChild.remove();
  }

  function setBusy(value) {
    busy = value;
    find('#login-form')?.setAttribute('aria-busy', String(value));
    root.querySelectorAll('#login-form input, #operator-name, #login-submit, [data-action="demo-enter"], [data-action="terminal-reset"]').forEach(element => {
      element.disabled = value;
    });
  }

  function progress(value) {
    const bar = find('#auth-progress');
    if (bar) {
      bar.style.width = `${value}%`;
      bar.setAttribute('aria-valuenow', String(value));
    }
  }

  function setPhase(phase, message, english) {
    if (!alive) return;
    if (phase !== 'error') stopImpact();
    root.dataset.phase = phase;
    document.body.classList.toggle('auth-alarm', phase === 'error');
    const labels = { idle: 'LOCKED', verifying: 'VERIFYING', error: 'DENIED', granted: 'GRANTED', exiting: 'UNLOCKED' };
    text('#auth-state-label', labels[phase] || 'LOCKED');
    text('#auth-phase-code', { idle: '01 / STANDBY', verifying: '02 / VERIFY', error: '00 / REJECTED', granted: '03 / GRANTED', exiting: '04 / ENTER' }[phase]);
    if (message) text('#auth-status', message);
    if (english) text('#auth-status-en', english);
    find('#session-seal')?.setAttribute('aria-label', phase === 'error' ? '访问口令错误，演示会话仍锁定' : phase === 'granted' || phase === 'exiting' ? '演示会话已解锁' : 'Finance Analyze 演示会话已锁定');
    ribbon.setState?.(phase === 'granted' || phase === 'exiting' ? 'success' : phase === 'verifying' ? 'verifying' : phase === 'error' ? 'error' : 'idle');
    if (phase === 'error') startImpact();
    onPhase(phase);
  }

  // A single physical response, not a flashing alarm. The dark warning remains
  // readable after the recoil; typing or resetting dismisses it immediately.
  function stopImpact() {
    clear(impactTimer);
    clear(impactEchoTimer);
    impactTimer = 0;
    impactEchoTimer = 0;
    ribbon.clearGlitch?.();
    for (const animation of impactAnimations) animation.cancel();
    impactAnimations.clear();
    delete root.dataset.impact;
    document.body.classList.remove('auth-impact');
  }

  function startImpact() {
    stopImpact();
    root.querySelectorAll('.auth-ripple').forEach(item => item.remove());
    if (!animated()) return;
    root.dataset.impact = 'active';
    document.body.classList.add('auth-impact');
    ribbon.pulse?.(2, { kind: 'error' });
    ribbon.triggerGlitch?.(1.2);
    // Independent transform components preserve each element's placement and
    // entrance animation. Cancelling this recoil never replays its entrance.
    const recoil = (selector, id, duration, frames) => {
      const element = find(selector);
      if (!element?.animate) return;
      const animation = element.animate(frames.map(frame => ({ ...frame, easing: 'cubic-bezier(.18,.72,.3,1)' })), { duration, fill: 'none' });
      animation.id = id;
      impactAnimations.add(animation);
      animation.onfinish = () => {
        impactAnimations.delete(animation);
        animation.cancel();
      };
    };
    recoil('.auth-dossier', 'auth-denied-dossier', 650, [
      { offset: 0, translate: '0 0', rotate: '0deg', scale: '1' },
      { offset: .07, translate: '-17px 4px', rotate: '-.65deg', scale: '.993' },
      { offset: .18, translate: '12px -3px', rotate: '.42deg', scale: '1.004' },
      { offset: .32, translate: '-8px 2px', rotate: '-.27deg', scale: '1' },
      { offset: .49, translate: '4px -1px', rotate: '.14deg', scale: '1' },
      { offset: .70, translate: '-1.5px 0', rotate: '-.05deg', scale: '1' },
      { offset: 1, translate: '0 0', rotate: '0deg', scale: '1' },
    ]);
    recoil('#session-seal', 'auth-denied-seal', 740, [
      { offset: 0, translate: '0 0', rotate: '0deg', scale: '1' },
      { offset: .07, translate: '21px -8px', rotate: '1.5deg', scale: '1.025' },
      { offset: .18, translate: '-14px 5px', rotate: '-1deg', scale: '.988' },
      { offset: .34, translate: '8px -3px', rotate: '.6deg', scale: '1.006' },
      { offset: .53, translate: '-4px 1px', rotate: '-.28deg', scale: '1' },
      { offset: .74, translate: '1px 0', rotate: '.08deg', scale: '1' },
      { offset: 1, translate: '0 0', rotate: '0deg', scale: '1' },
    ]);
    impactEchoTimer = schedule(() => {
      impactEchoTimer = 0;
      if (root.dataset.phase === 'error' && animated()) ribbon.pulse?.(.55, { kind: 'error' });
    }, 240);
    impactTimer = schedule(stopImpact, 980);
  }

  function ripple() {
    if (!alive || !animated() || busy) return;
    const item = document.createElement('span');
    const bounds = root.getBoundingClientRect();
    item.className = 'auth-ripple';
    item.setAttribute('aria-hidden', 'true');
    // A changing focal point echoes keystrokes without exposing their values.
    const offset = Math.sin(++rippleSerial * 1.71);
    item.style.left = `${bounds.width * (bounds.width > 850 ? .46 : .49) + offset * 28}px`;
    item.style.top = `${Math.min(bounds.height * .48, window.innerHeight * .5) + offset * 18}px`;
    root.append(item);
    const old = root.querySelectorAll('.auth-ripple');
    if (old.length > 4) old[0].remove();
    schedule(() => item.remove(), 1100);
    ribbon.pulse?.(1.05, { kind: 'input' });
  }

  function reset() {
    if (!alive || busy) return;
    clear(alarmTimer);
    const input = find('#access-code');
    if (input) {
      input.value = '';
      input.removeAttribute('aria-invalid');
      input.focus({ preventScroll: true });
    }
    text('#login-error', '');
    text('#login-submit', 'REQUEST →');
    progress(0);
    setPhase('idle', '会话已锁定。请出示访问口令。', 'YOUR SESSION IS LOCKED. PRESENT YOUR AUTHORIZATION KEY.');
    log('INPUT CLEARED');
  }

  async function authenticate(valid = true) {
    if (!alive || busy) return false;
    const current = ++sequence;
    const active = () => alive && sequence === current && root.isConnected;
    clear(alarmTimer);
    text('#login-error', '');
    find('#access-code')?.removeAttribute('aria-invalid');
    setBusy(true);
    text('#login-submit', 'PENDING');
    setPhase('verifying', '正在核验演示身份…', 'VERIFYING DEMO IDENTITY');
    text('#auth-stage-label', 'READING LOCAL INPUT');
    log('REQUEST RECEIVED');
    progress(15);
    if (!await wait(320) || !active()) return false;
    text('#auth-stage-label', 'CHECKING DEMO ACCESS KEY');
    log('DEMO KEY CHECK');
    progress(52);
    if (!await wait(390) || !active()) return false;

    if (!valid) {
      progress(0);
      text('#login-error', '访问码不正确。演示访问码为 FINANCE。');
      find('#access-code')?.setAttribute('aria-invalid', 'true');
      text('#login-submit', 'RETRY →');
      text('#auth-stage-label', 'AUTHORIZATION KEY REJECTED');
      setPhase('error', '访问被拒绝。检查口令后重新发起请求。', 'ACCESS DENIED · SESSION REMAINS LOCKED');
      log('ACCESS DENIED / DEMO KEY', 'is-error');
      setBusy(false);
      find('#access-code')?.focus({ preventScroll: true });
      find('#access-code')?.select();
      alarmTimer = schedule(() => {
        if (root.dataset.phase !== 'error') return;
        setPhase('idle', '会话已锁定。请重新输入口令。', 'SESSION LOCKED · READY FOR ANOTHER REQUEST');
        text('#login-submit', 'REQUEST →');
      }, 3200);
      return false;
    }

    text('#auth-stage-label', 'PREPARING FOUR MODULES');
    progress(100);
    log('WORKSPACE PREPARED / A B C D');
    if (!await wait(280) || !active()) return false;
    setPhase('granted', '身份已确认。欢迎进入研究现场。', 'ACCESS GRANTED · WELCOME TO FINANCE ANALYZE');
    text('#login-submit', 'GRANTED ✓');
    log('ACCESS GRANTED', 'is-success');
    if (!await wait(460) || !active()) return false;
    setPhase('exiting', '正在打开研究工作台。', 'OPENING YOUR RESEARCH WORKSPACE');
    if (!await wait(650) || !active()) return false;
    return true;
  }

  root.addEventListener('input', event => {
    if (event.target.id !== 'access-code' || busy) return;
    onTyping();
    clear(typingTimer);
    root.dataset.typing = 'true';
    if (root.dataset.phase === 'error') {
      clear(alarmTimer);
      setPhase('idle', '会话已锁定。请重新输入口令。', 'SESSION LOCKED · READY FOR ANOTHER REQUEST');
    }
    text('#login-error', '');
    event.target.removeAttribute('aria-invalid');
    text('#login-submit', 'REQUEST →');
    ripple();
    typingTimer = schedule(() => { delete root.dataset.typing; }, 520);
  }, { signal: events.signal });

  root.addEventListener('pointermove', event => {
    if (!animated() || event.pointerType === 'touch') return;
    const rect = root.getBoundingClientRect();
    pointer = { x: ((event.clientX - rect.left) / rect.width - .5) * 2, y: ((event.clientY - rect.top) / Math.min(rect.height, window.innerHeight) - .5) * 2 };
    if (pointerFrame) return;
    pointerFrame = requestAnimationFrame(() => {
      pointerFrame = 0;
      if (!alive || !animated()) return;
      root.style.setProperty('--pointer-x', pointer.x.toFixed(3));
      root.style.setProperty('--pointer-y', pointer.y.toFixed(3));
      ribbon.setPointer?.(pointer.x, pointer.y);
      if (event.target.closest('.auth-dossier, #session-seal, input, button, [popover], dialog')) ribbon.clearHover?.();
      else ribbon.hoverAt?.(event.clientX, event.clientY);
    });
  }, { passive: true, signal: events.signal });

  root.addEventListener('pointerleave', () => {
    pointer = { x: 0, y: 0 };
    root.style.setProperty('--pointer-x', '0');
    root.style.setProperty('--pointer-y', '0');
    ribbon.setPointer?.(0, 0);
    ribbon.clearHover?.();
  }, { signal: events.signal });

  root.addEventListener('click', event => {
    if (!animated() || busy || !event.target.closest('button, input, textarea, select')) return;
    ribbon.pulse?.(1.35, { kind: 'click' });
  }, { passive: true, signal: events.signal });

  root.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !busy && !document.querySelector('dialog[open]')) {
      event.preventDefault();
      reset();
    }
  }, { signal: events.signal });

  function refreshMotion() {
    if (!animated()) {
      stopImpact();
      if (pointerFrame) cancelAnimationFrame(pointerFrame);
      pointerFrame = 0;
      root.style.setProperty('--pointer-x', '0');
      root.style.setProperty('--pointer-y', '0');
      root.querySelectorAll('.auth-ripple').forEach(item => item.remove());
      ribbon.setPointer?.(0, 0);
      ribbon.clearHover?.();
    }
    if (document.hidden && root.dataset.phase === 'error') {
      clear(alarmTimer);
      alarmTimer = 0;
      setPhase('idle', '会话已锁定。请重新输入口令。', 'SESSION LOCKED · READY FOR ANOTHER REQUEST');
      text('#login-submit', 'REQUEST →');
    }
  }
  reducedMotion.addEventListener('change', refreshMotion, { signal: events.signal });
  document.addEventListener('visibilitychange', refreshMotion, { signal: events.signal });

  schedule(() => log('RESEARCH INTERFACE READY'), 340);
  schedule(() => log('FOUR MODULES / STANDBY'), 720);
  setPhase('idle');

  return {
    authenticate,
    reset,
    get busy() { return busy; },
    refreshMotion,
    destroy() {
      stopImpact();
      alive = false;
      sequence++;
      events.abort();
      for (const id of [...timers.keys()]) clear(id);
      if (pointerFrame) cancelAnimationFrame(pointerFrame);
      document.body.classList.remove('auth-alarm');
      delete root.dataset.typing;
      root.querySelectorAll('.auth-ripple').forEach(item => item.remove());
      ribbon.setPointer?.(0, 0);
      ribbon.clearHover?.();
      ribbon.setState?.('idle');
    },
  };
}
