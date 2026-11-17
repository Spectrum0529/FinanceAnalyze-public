// Presentation only. Playback and persistence stay with the audio engine.
const MODES = {
  sequence: { label: '顺序播放', icon: '→' },
  'repeat-one': { label: '循环播放', icon: '↻' },
  shuffle: { label: '随机播放', icon: '↝' },
};

export function createAudioControls(dock, { motion = () => true, onModeChange = () => {} } = {}) {
  const panel = dock.querySelector('#audio-panel');
  const trigger = dock.querySelector('[data-audio-settings]');
  const menu = dock.querySelector('#audio-mode-menu');
  const modeTrigger = dock.querySelector('[data-audio-mode-toggle]');
  const choices = [...menu.querySelectorAll('[data-audio-mode]')];
  const events = new AbortController();
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const transitions = new Map(), accents = new Set();
  let panelOpen = false, menuOpen = false, destroyed = false, layoutFrame = 0;
  let currentMode = null, currentTrack = null;
  let nativePopover = typeof panel.showPopover === 'function' && typeof panel.hidePopover === 'function';
  if (!nativePopover) panel.removeAttribute('popover');
  const animated = () => !document.hidden && motion() && !document.body.classList.contains('motion-paused') && !reduced.matches;
  const focus = element => element?.focus({ preventScroll: true });

  function cancelTransition(surface) {
    const pending = transitions.get(surface);
    if (!pending) return;
    window.clearTimeout(pending.timer);
    surface.removeEventListener('animationend', pending.listener);
    transitions.delete(surface);
  }

  function transition(surface, state, duration, finish) {
    cancelTransition(surface);
    surface.dataset.state = state;
    if (!animated()) { finish(); return; }
    const complete = () => { cancelTransition(surface); if (!destroyed) finish(); };
    const listener = event => { if (event.target === surface) complete(); };
    const timer = window.setTimeout(complete, duration + 60);
    transitions.set(surface, { listener, timer, complete });
    surface.addEventListener('animationend', listener);
  }

  function accent(element, keyframes, duration = 200) {
    if (!panelOpen || !animated() || !element?.animate) return;
    for (const animation of accents) if (animation.effect?.target === element) { animation.cancel(); accents.delete(animation); }
    const animation = element.animate(keyframes, { duration, easing: 'cubic-bezier(.2,.7,.2,1)' });
    accents.add(animation);
    animation.onfinish = () => { accents.delete(animation); animation.cancel(); };
  }

  function positionPanel() {
    layoutFrame = 0;
    if (!panelOpen || destroyed) return;
    const anchor = dock.querySelector('.audio-dock-main').getBoundingClientRect();
    const width = window.innerWidth, height = window.innerHeight;
    if (anchor.bottom < 0 || anchor.top > height) { closePanel({ immediate: true, restoreFocus: false }); return; }
    panel.style.maxHeight = `${Math.max(180, Math.min(430, height - 24))}px`;
    const bounds = panel.getBoundingClientRect();
    const left = Math.max(12, Math.min(width - bounds.width - 12, anchor.right - bounds.width));
    const above = anchor.top - bounds.height - 10;
    const top = above >= 12 ? above : Math.max(12, Math.min(height - bounds.height - 12, anchor.bottom + 10));
    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
  }

  function queuePosition() {
    if (panelOpen && !layoutFrame) layoutFrame = requestAnimationFrame(positionPanel);
  }

  function closeMenu({ restoreFocus = false, immediate = false } = {}) {
    const wasOpen = menuOpen || menu.dataset.state === 'closing';
    menuOpen = false;
    modeTrigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus && wasOpen) focus(modeTrigger);
    menu.inert = true;
    const finish = () => { menu.hidden = true; menu.dataset.state = 'closed'; menu.inert = false; };
    if (immediate || !wasOpen) { cancelTransition(menu); finish(); }
    else transition(menu, 'closing', 140, finish);
  }

  function focusChoice(index) {
    const next = (index + choices.length) % choices.length;
    choices.forEach((choice, position) => { choice.tabIndex = position === next ? 0 : -1; });
    focus(choices[next]);
  }

  function openMenu({ last = false } = {}) {
    if (!panelOpen || destroyed) return;
    menuOpen = true;
    menu.hidden = false;
    menu.inert = false;
    modeTrigger.setAttribute('aria-expanded', 'true');
    transition(menu, 'opening', 180, () => { menu.dataset.state = 'open'; });
    const selected = choices.findIndex(choice => choice.dataset.audioMode === currentMode);
    focusChoice(last ? choices.length - 1 : Math.max(0, selected));
  }

  function hidePanel() {
    if (nativePopover && panel.matches(':popover-open')) panel.hidePopover();
    panel.hidden = true;
    panel.inert = false;
    panel.dataset.state = 'closed';
  }

  function closePanel({ restoreFocus = true, immediate = false } = {}) {
    const wasOpen = panelOpen || panel.dataset.state === 'closing';
    panelOpen = false;
    closeMenu({ immediate: true });
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-label', '打开播放列表和音量');
    if (wasOpen && (restoreFocus || panel.contains(document.activeElement))) focus(trigger);
    panel.inert = true;
    for (const animation of accents) animation.cancel();
    accents.clear();
    if (immediate || !wasOpen) { cancelTransition(panel); hidePanel(); }
    else transition(panel, 'closing', 170, hidePanel);
  }

  function openPanel() {
    if (destroyed) return;
    cancelTransition(panel);
    panelOpen = true;
    panel.hidden = false;
    panel.inert = false;
    panel.dataset.state = 'opening';
    if (nativePopover && !panel.matches(':popover-open')) panel.showPopover();
    trigger.setAttribute('aria-expanded', 'true');
    trigger.setAttribute('aria-label', '关闭播放列表和音量');
    positionPanel();
    const playlist = panel.querySelector('[data-audio-playlist]');
    const current = playlist?.querySelector('.is-current');
    if (current && playlist.clientHeight > 0) {
      const bounds = playlist.getBoundingClientRect(), row = current.getBoundingClientRect();
      // Account for the panel's opening scale, then scroll only the list.
      const scale = playlist.offsetHeight ? bounds.height / playlist.offsetHeight : 1;
      const top = bounds.top + playlist.clientTop * scale;
      const bottom = top + playlist.clientHeight * scale;
      const delta = row.top < top ? row.top - top : row.bottom > bottom ? row.bottom - bottom : 0;
      if (delta && scale > 0) {
        const next = Math.max(0, Math.min(playlist.scrollHeight - playlist.clientHeight, playlist.scrollTop + delta / scale));
        playlist.scrollTo({ top: next, behavior: animated() ? 'smooth' : 'instant' });
      }
    }
    transition(panel, 'opening', 220, () => { panel.dataset.state = 'open'; });
    focus(panel.querySelector('[data-audio-play]'));
  }

  dock.addEventListener('click', event => {
    const target = event.target.closest('button');
    if (!target || target.disabled) return;
    if (target === trigger) { event.preventDefault(); panelOpen ? closePanel() : openPanel(); }
    else if (target.matches('[data-audio-close]')) { event.preventDefault(); closePanel(); }
    else if (target === modeTrigger) { event.preventDefault(); menuOpen ? closeMenu({ restoreFocus: true }) : openMenu(); }
    else if (target.matches('[data-audio-mode]') && menuOpen) {
      event.preventDefault();
      const next = target.dataset.audioMode;
      if (MODES[next]) onModeChange(next);
      closeMenu({ restoreFocus: true });
    }
  }, { signal: events.signal });

  dock.addEventListener('keydown', event => {
    const target = event.target;
    if (menuOpen && menu.contains(target)) {
      const index = choices.indexOf(target.closest('[data-audio-mode]'));
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); event.stopPropagation(); focusChoice(index + (event.key === 'ArrowDown' ? 1 : -1));
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault(); event.stopPropagation(); focusChoice(event.key === 'Home' ? 0 : choices.length - 1);
      } else if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); closeMenu({ restoreFocus: true });
      } else if (event.key === 'Tab') closeMenu({ restoreFocus: true });
      return;
    }
    if ((target === modeTrigger || target === trigger) && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault(); event.stopPropagation();
      target === modeTrigger ? openMenu({ last: event.key === 'ArrowUp' }) : openPanel();
    } else if (panelOpen && event.key === 'Escape' && (panel.contains(target) || target === trigger)) {
      event.preventDefault(); event.stopPropagation(); closePanel();
    }
  }, { signal: events.signal });

  document.addEventListener('pointerdown', event => {
    if (panelOpen && !dock.contains(event.target)) closePanel({ restoreFocus: false });
    else if (menuOpen && !menu.contains(event.target) && !modeTrigger.contains(event.target)) closeMenu();
  }, { capture: true, signal: events.signal });
  document.addEventListener('focusin', event => {
    if (panelOpen && !dock.contains(event.target)) closePanel({ restoreFocus: false });
    else if (menuOpen && !menu.contains(event.target) && event.target !== modeTrigger) closeMenu();
  }, { signal: events.signal });
  panel.addEventListener('toggle', event => {
    if (event.newState === 'closed' && panelOpen && !panel.matches(':popover-open')) closePanel({ immediate: true, restoreFocus: false });
  }, { signal: events.signal });

  function refreshMotion() {
    if (animated()) return;
    for (const pending of [...transitions.values()]) pending.complete();
    for (const animation of accents) animation.cancel();
    accents.clear();
  }
  const preferenceObserver = new MutationObserver(refreshMotion);
  preferenceObserver.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  reduced.addEventListener('change', refreshMotion, { signal: events.signal });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) closePanel({ immediate: true, restoreFocus: false });
  }, { signal: events.signal });
  window.addEventListener('resize', queuePosition, { passive: true, signal: events.signal });
  window.addEventListener('scroll', queuePosition, { passive: true, capture: true, signal: events.signal });
  window.addEventListener('pagehide', () => closePanel({ immediate: true, restoreFocus: false }), { signal: events.signal });

  return {
    update(snapshot) {
      if (destroyed) return;
      const mode = snapshot.playbackMode || (snapshot.repeatOne ? 'repeat-one' : snapshot.shuffle ? 'shuffle' : 'sequence');
      if (MODES[mode] && currentMode !== mode) {
        const changed = currentMode !== null;
        currentMode = mode;
        const label = modeTrigger.querySelector('[data-audio-mode-label]');
        label.textContent = MODES[mode].label;
        modeTrigger.querySelector('[data-audio-mode-icon]').textContent = MODES[mode].icon;
        modeTrigger.setAttribute('aria-label', `播放方式：${MODES[mode].label}`);
        modeTrigger.title = mode === 'repeat-one' ? '循环当前曲目' : MODES[mode].label;
        choices.forEach(choice => {
          const selected = choice.dataset.audioMode === mode;
          choice.setAttribute('aria-checked', String(selected));
          choice.tabIndex = selected ? 0 : -1;
        });
        if (changed) accent(label, [{ opacity: .3, transform: 'translateY(3px)' }, { opacity: 1, transform: 'none' }], 180);
      }
      const track = snapshot.currentTrack?.id;
      if (track && track !== currentTrack) {
        if (currentTrack !== null) accent(dock.querySelector('.audio-now-playing'), [{ opacity: .45, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 220);
        currentTrack = track;
      }
    },
    close: closePanel,
    destroy() {
      if (destroyed) return;
      closePanel({ immediate: true, restoreFocus: false });
      destroyed = true;
      events.abort(); preferenceObserver.disconnect();
      for (const surface of [...transitions.keys()]) cancelTransition(surface);
      if (layoutFrame) cancelAnimationFrame(layoutFrame);
    },
  };
}
