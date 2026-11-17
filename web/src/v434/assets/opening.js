/**
 * A single, silent opening: draw the research mark, then carry it into the page.
 * The caller supplies trusted local SVG markup and the already-rendered anchor.
 * No session flags are stored: route changes do not replay a document opening.
 */
let activeOpening = null;

const ENTRANCES = new Set([
  'auth-rise', 'auth-line-arrive', 'auth-fade', 'auth-draw',
  'auth-seal-arrive', 'auth-dossier-arrive',
]);
const DRAW_END = 390;
const DOCK_DURATION = 300;
const FAILSAFE = 5000;

export function playOpening({ target, mark, enabled = true, onComplete = () => {} } = {}) {
  activeOpening?.finish('replaced');

  const body = document.body;
  const media = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const events = new AbortController();
  const animations = new Set();
  const timers = new Set();
  const inerted = new Map();
  const previousFocus = document.activeElement;
  const previousTargetMarker = target?.getAttribute?.('data-opening-target');
  let layer = null, emblem = null, backdrop = null, skip = null;
  let observer = null, frame = 0, done = false;
  let markedTarget = false;

  const api = {
    finish: (reason = 'skipped') => finish(reason),
    destroy: () => finish('destroyed'),
    get active() { return !done && Boolean(layer); },
  };
  activeOpening = api;

  function allowed() {
    try {
      return Boolean(typeof enabled === 'function' ? enabled() : enabled)
        && !media?.matches && !document.hidden && !body.classList.contains('motion-paused');
    } catch { return false; }
  }

  function schedule(callback, delay) {
    const id = window.setTimeout(() => {
      timers.delete(id);
      if (!done) callback();
    }, delay);
    timers.add(id);
    return id;
  }

  function animate(element, keyframes, options) {
    const animation = element.animate(keyframes, options);
    animations.add(animation);
    return animation;
  }

  function finish(reason) {
    if (done) return;
    done = true;
    events.abort();
    observer?.disconnect();
    if (frame) window.cancelAnimationFrame(frame);
    frame = 0;
    for (const id of timers) window.clearTimeout(id);
    timers.clear();
    for (const animation of animations) {
      try { animation.cancel(); } catch { /* A detached animation is already inert. */ }
    }
    animations.clear();

    const restoreFocus = Boolean(layer && (layer.contains(document.activeElement) || document.activeElement === body));
    layer?.remove();
    if (markedTarget) {
      if (previousTargetMarker === null) target.removeAttribute('data-opening-target');
      else target.setAttribute('data-opening-target', previousTargetMarker);
    }
    for (const [element, previous] of inerted) {
      element.inert = previous.inert;
      if (previous.hidden === null) element.removeAttribute('aria-hidden');
      else element.setAttribute('aria-hidden', previous.hidden);
    }
    inerted.clear();
    if (activeOpening === api) {
      activeOpening = null;
      delete body.dataset.opening;
    }
    if (restoreFocus && !document.hidden) {
      const destination = previousFocus?.isConnected && previousFocus !== body
        ? previousFocus : document.querySelector('#main');
      try { destination?.focus({ preventScroll: true }); } catch { /* Focus must not block cleanup. */ }
    }
    try { onComplete(reason); } catch { /* The page is usable even if its callback fails. */ }
  }

  function validRect(element) {
    if (!element?.isConnected) return null;
    const rect = element.getBoundingClientRect();
    return [rect.left, rect.top, rect.width, rect.height].every(Number.isFinite)
      && rect.width > 0 && rect.height > 0 ? rect : null;
  }

  function visibleTargetRect() {
    const rect = validRect(target);
    if (!rect) return null;
    // Reload can restore a scrolled workspace whose header is above the viewport.
    // Do not fly the mark out of view; only its destination needs this check.
    return rect.left + rect.width > 0 && rect.top + rect.height > 0
      && rect.left < window.innerWidth && rect.top < window.innerHeight ? rect : null;
  }

  function matchTargetColors() {
    const style = window.getComputedStyle(target);
    layer.style.color = style.color;
    const accent = style.getPropertyValue('--brand-accent').trim();
    if (accent) layer.style.setProperty('--brand-accent', accent);
  }

  function dock() {
    if (!allowed()) return finish('motion-disabled');
    const destination = visibleTargetRect();
    const origin = validRect(emblem);
    if (!destination || !origin) return finish('target-unavailable');

    const dx = destination.left + destination.width / 2 - origin.left - origin.width / 2;
    const dy = destination.top + destination.height / 2 - origin.top - origin.height / 2;
    const scale = Math.min(destination.width / origin.width, destination.height / origin.height);
    if (!Number.isFinite(scale) || scale <= 0) return finish('target-unavailable');
    matchTargetColors();
    body.dataset.opening = 'docking';
    layer.dataset.phase = 'docking';
    try {
      const travel = animate(emblem, [
        { transform: 'translate(-50%, -50%) translate3d(0, 0, 0) scale(1)' },
        { transform: `translate(-50%, -50%) translate3d(${dx}px, ${dy}px, 0) scale(${scale})` },
      ], { duration: DOCK_DURATION, easing: 'cubic-bezier(.65,0,.22,1)', fill: 'forwards' });
      animate(backdrop, [{ opacity: 1 }, { opacity: 0 }], {
        duration: 250, easing: 'cubic-bezier(.3,0,.3,1)', fill: 'forwards',
      });
      animate(skip, [{ opacity: 1 }, { opacity: 0 }], { duration: 150, fill: 'forwards' });
      // WAAPI may enter its running state after a delayed rendering frame.
      // A wall-clock timer could remove the layer before that motion completes.
      // Keep the global failsafe, but let the actual handoff finish normally.
      travel.finished.then(
        () => { if (!done) finish('complete'); },
        () => { if (!done) finish('animation-unavailable'); },
      );
    } catch { finish('animation-unavailable'); }
  }

  function draw() {
    frame = 0;
    if (done) return;
    if (!allowed()) return finish('motion-disabled');
    if (!visibleTargetRect()) return finish('target-unavailable');
    body.dataset.opening = 'drawing';
    layer.dataset.phase = 'drawing';
    const paths = [...emblem.querySelectorAll('[data-logo-stroke]')];
    const dot = emblem.querySelector('[data-logo-dot]');
    if (!paths.length) return finish('mark-unavailable');
    try {
      animate(emblem, [{ opacity: 0 }, { opacity: 1 }], { duration: 100, fill: 'both' });
      let frameIndex = 0;
      paths.forEach((path, index) => {
        const named = path.getAttribute('data-logo-part') || path.getAttribute('data-logo-stroke');
        const role = named || (index >= paths.length - 2 ? (index === paths.length - 1 ? 'baseline' : 'curve') : 'frame');
        const timing = /curve|signal/.test(role) ? { delay: 120, duration: 210 }
          : /base/.test(role) ? { delay: 170, duration: 150 }
          : { delay: 30 + frameIndex++ * 24, duration: 205 };
        path.style.strokeDasharray = '1';
        path.style.strokeDashoffset = '1';
        animate(path, [{ strokeDashoffset: '1' }, { strokeDashoffset: '0' }], {
          ...timing, easing: 'cubic-bezier(.4,0,.2,1)', fill: 'both',
        });
      });
      if (dot) animate(dot, [
        { opacity: 0, transform: 'scale(.3)' },
        { opacity: 1, transform: 'scale(1.08)', offset: .75 },
        { opacity: 1, transform: 'scale(1)' },
      ], { delay: 220, duration: 135, easing: 'cubic-bezier(.16,1,.3,1)', fill: 'both' });
      schedule(dock, DRAW_END);
    } catch { finish('animation-unavailable'); }
  }

  if (!body || !allowed() || !visibleTargetRect() || typeof mark !== 'string' || !mark.trim()) {
    finish('not-needed');
    return api;
  }
  try {
    layer = document.createElement('section');
    layer.className = 'opening-screen';
    layer.setAttribute('role', 'dialog');
    layer.setAttribute('aria-modal', 'true');
    layer.setAttribute('aria-label', 'Finance Analyze 开场动画');
    layer.tabIndex = -1;
    backdrop = document.createElement('div');
    backdrop.className = 'opening-backdrop';
    backdrop.setAttribute('aria-hidden', 'true');
    emblem = document.createElement('div');
    emblem.className = 'opening-mark';
    emblem.setAttribute('aria-hidden', 'true');
    emblem.innerHTML = mark;
    skip = document.createElement('button');
    skip.type = 'button';
    skip.className = 'opening-skip';
    skip.textContent = '跳过开场';
    skip.setAttribute('aria-label', '跳过开场动画，进入界面');
    layer.append(backdrop, emblem, skip);
    if (typeof emblem.animate !== 'function') {
      finish('animation-unavailable');
      return api;
    }

    // Finish only finite login entrance animations while the pending mask covers
    // the page. Its final layout is then stable for a precise, seamless handoff.
    const terminal = target.closest('.terminal-screen');
    for (const animation of terminal?.getAnimations?.({ subtree: true }) || []) {
      if (ENTRANCES.has(animation.animationName)) {
        try { animation.finish(); } catch { /* Infinite/unsupported effects are left alone. */ }
      }
    }
    target.setAttribute('data-opening-target', 'true');
    markedTarget = true;
    body.append(layer);
    matchTargetColors();
    body.dataset.opening = 'drawing';
    // Move focus before hiding its old ancestors from the accessibility tree.
    layer.focus({ preventScroll: true });
    for (const child of body.children) {
      if (child === layer || ['SCRIPT', 'STYLE', 'LINK'].includes(child.tagName)) continue;
      inerted.set(child, { inert: Boolean(child.inert), hidden: child.getAttribute('aria-hidden') });
      child.inert = true;
      child.setAttribute('aria-hidden', 'true');
    }

    layer.addEventListener('click', event => {
      event.stopPropagation();
      if (event.target.closest('.opening-skip')) finish('skipped');
    }, { signal: events.signal });
    layer.addEventListener('wheel', event => event.preventDefault(), { passive: false, signal: events.signal });
    window.addEventListener('keydown', event => {
      // Global command shortcuts must not open a modal underneath this dialog.
      event.stopImmediatePropagation();
      if (['Escape', 'Enter'].includes(event.key)) {
        event.preventDefault();
        finish('skipped');
      } else if (event.key === 'Tab') {
        event.preventDefault();
        skip.focus({ preventScroll: true });
      } else if ([' ', 'PageDown', 'PageUp', 'Home', 'End', 'ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
      }
    }, { capture: true, signal: events.signal });
    window.addEventListener('resize', () => finish('resized'), { passive: true, signal: events.signal });
    window.addEventListener('hashchange', () => finish('navigation'), { signal: events.signal });
    window.addEventListener('pagehide', () => finish('page-hidden'), { signal: events.signal });
    document.addEventListener('visibilitychange', () => { if (document.hidden) finish('page-hidden'); }, { signal: events.signal });
    media?.addEventListener?.('change', () => { if (media.matches) finish('reduced-motion'); }, { signal: events.signal });
    if (typeof MutationObserver === 'function') {
      observer = new MutationObserver(() => { if (!allowed()) finish('motion-disabled'); });
      observer.observe(body, { attributes: true, attributeFilter: ['class'] });
    }
    schedule(() => finish('timeout'), FAILSAFE);
    frame = window.requestAnimationFrame(draw);
  } catch { finish('setup-unavailable'); }
  return api;
}
