/**
 * Research OS presentation director. One demand-driven frame handles pointer
 * interpolation and counters. Final metric text stays in the accessibility
 * tree while its aria-hidden visual copy counts into place. No data is changed.
 */
export function createMotionDirector({ root, ribbon = null, motion = () => true } = {}) {
  const noop = () => {};
  if (!root) return { refresh: noop, refreshMotion: noop, setProcessing: noop, beat: noop, destroy: noop };
  const media = window.matchMedia('(prefers-reduced-motion: reduce)');
  const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)');
  const abort = new AbortController();
  const counters = new Map();
  const numberFormatters = new Map();
  function formatMetric(value,digits){
    let formatter=numberFormatters.get(digits);
    if(!formatter){
      formatter=new Intl.NumberFormat('en-US',{minimumFractionDigits:digits,maximumFractionDigits:digits});
      numberFormatters.set(digits,formatter);
    }
    return formatter.format(value);
  }
  const rings = new Set();
  const observedSurfaces = new Set();
  // Pausing infinite decorative CSS animations outside the viewport prevents
  // hidden cards from consuming compositor work while the user reads a module.
  const visibilityObserver = typeof IntersectionObserver === 'function'
    ? new IntersectionObserver(entries => {
        for (const entry of entries) entry.target.classList.toggle('motion-offscreen', !entry.isIntersecting);
      }, {rootMargin:'85px 0px'}) : null;
  function watchVisibleSurfaces() {
    if (!visibilityObserver) return;
    for (const element of observedSurfaces) {
      if (!element.isConnected) { visibilityObserver.unobserve(element); observedSurfaces.delete(element); }
    }
    for (const element of selectAll('.module-card, .role-card, .environment-panel, .empty-orbit')) {
      if (observedSurfaces.has(element)) continue;
      observedSurfaces.add(element);visibilityObserver.observe(element);
    }
  }
  let route = 'login', destroyed = false, active = false, frameID = 0, lastFrame = 0, dirty = false;
  let selectedCard = null, cardRect = null;
  let pointerX = 0, pointerY = 0, tiltX = 0, tiltY = 0, targetX = 0, targetY = 0;
  let shineX = '', shineY = '';
  let beatEnergy = 0;

  const sparks = document.createElement('div');
  sparks.className = 'motion-spark-layer';
  sparks.setAttribute('aria-hidden', 'true');
  document.body.append(sparks);
  const allowed = () => !destroyed && route !== 'login' && Boolean(motion()) && !media.matches && !document.hidden;
  const selectAll = selector => [...root.querySelectorAll(selector)].filter(node => !node.closest('.business-surface'));

  function restoreCounter(item) {
    if (!item.element.isConnected || item.done) return;
    // Replace text in-place rather than destroy/rebuild metric child nodes at
    // 1050ms. That DOM operation overlapped the route reveal's final frames.
    item.visual.textContent=item.finalText;
    item.done = true;
  }
  function finishCounters() {
    for (const [element, item] of counters) {
      if (element.isConnected) restoreCounter(item); else counters.delete(element);
    }
  }
  function resetCard() {
    if (selectedCard) {
      for (const property of ['--tilt-x', '--tilt-y', '--pointer-x', '--pointer-y']) selectedCard.style.removeProperty(property);
      delete selectedCard.dataset.motionHover;
    }
    selectedCard = null; cardRect = null;
    tiltX = tiltY = targetX = targetY = 0;
    shineX = shineY = '';
  }
  function clearRings() { for (const ring of rings) ring.remove(); rings.clear(); }
  function requestFrame() { if (!frameID && active && !destroyed) frameID = requestAnimationFrame(frame); }

  function addCounter(element) {
    if (counters.has(element) || element.dataset.motionCounter || !active) return;
    const match = element.textContent.trim().match(/^([+−-]?)([\d,]+)(\.\d+)?(.*)$/);
    if (!match) return;
    const value = Number((match[2] + (match[3] || '')).replaceAll(',', ''));
    if (!Number.isFinite(value)) return;
    const digits = match[3] ? match[3].length - 1 : 0;
    const original = [...element.childNodes];
    const final = document.createElement('span');
    final.className = 'motion-metric-final';
    final.append(...original);
    const visual = document.createElement('span');
    visual.className = 'motion-counter-value';
    visual.setAttribute('aria-hidden', 'true');
    visual.textContent = `${match[1]}${Number(0).toFixed(digits)}${match[4]}`;
    element.dataset.motionCounter = 'true';
    element.append(final, visual);
    const finalText=match[1]+formatMetric(value,digits)+match[4];
    counters.set(element, {element, original, final, visual, value, digits, prefix:match[1], suffix:match[4], finalText,start:performance.now(),done:false});
    requestFrame();
  }

  function prepare() {
    dirty = false;
    if (destroyed) return;
    if (route === 'login') { root.classList.remove('motion-processing'); return; }
    for (const [element] of counters) if (!element.isConnected) counters.delete(element);
    for (const selector of ['.module-card', '.role-card', '.team-step', '.data-table tbody tr', '.activity-row', '.env-graphic i']) {
      selectAll(selector).forEach((element, index) => element.style.setProperty('--motion-index', String(Math.min(index, 14))));
    }
    watchVisibleSurfaces();
    selectAll('.summary-metrics strong').forEach(addCounter);
    selectAll('.performance-chart .series-line').forEach(path => {
      if (path.dataset.motionPath) return;
      path.dataset.motionPath = 'true';
      try {
        const length = path.getTotalLength();
        if (Number.isFinite(length) && length > 0) path.style.setProperty('--series-length', String(length));
      } catch { /* The chart still renders in non-SVG environments. */ }
    });
    const running = Boolean(root.querySelector('.run-bar, #research-form button[type="submit"]:disabled'));
    root.classList.toggle('motion-processing', running || Boolean(root.dataset.processing));
  }

  function frame(now) {
    frameID = 0;
    if (!active || destroyed) return;
    const dt = lastFrame ? Math.min((now - lastFrame) / 1000, .06) : .016;
    lastFrame = now;
    if (dirty) prepare();
    let workRemains = false;
    if (selectedCard?.isConnected) {
      if (!cardRect) cardRect = selectedCard.getBoundingClientRect();
      const x = Math.min(1, Math.max(-1, (pointerX - cardRect.left) / Math.max(1, cardRect.width) * 2 - 1));
      const y = Math.min(1, Math.max(-1, (pointerY - cardRect.top) / Math.max(1, cardRect.height) * 2 - 1));
      targetX = -y * 4.5; targetY = x * 6;
      const ease = 1 - Math.exp(-dt * 13);
      tiltX += (targetX - tiltX) * ease;
      tiltY += (targetY - tiltY) * ease;
      selectedCard.style.setProperty('--tilt-x', `${tiltX.toFixed(3)}deg`);
      selectedCard.style.setProperty('--tilt-y', `${tiltY.toFixed(3)}deg`);
      const nextShineX=`${((x + 1) * 50).toFixed(2)}%`, nextShineY=`${((y + 1) * 50).toFixed(2)}%`;
      if(shineX!==nextShineX)selectedCard.style.setProperty('--pointer-x',shineX=nextShineX);
      if(shineY!==nextShineY)selectedCard.style.setProperty('--pointer-y',shineY=nextShineY);
      workRemains ||= Math.abs(targetX - tiltX) + Math.abs(targetY - tiltY) > .015;
    } else if (selectedCard) resetCard();
    for (const [element, item] of counters) {
      if (!element.isConnected) { counters.delete(element); continue; }
      if (item.done) continue;
      const progress = Math.min(1, (now - item.start) / 1050);
      if (progress >= 1) { restoreCounter(item); continue; }
      const value = item.value * (1 - Math.pow(1 - progress, 3));
      item.visual.textContent = item.prefix+formatMetric(value,item.digits)+item.suffix;
      workRemains = true;
    }
    if (beatEnergy > .008) {
      beatEnergy *= Math.exp(-dt * 6);
      root.style.setProperty('--audio-pulse', beatEnergy.toFixed(3));
      workRemains = true;
    } else if (beatEnergy) { beatEnergy = 0; root.style.removeProperty('--audio-pulse'); }
    if (workRemains || dirty) requestFrame(); else lastFrame = 0;
  }

  function refreshMotion() {
    active = allowed();
    root.dataset.motionActive = String(active);
    document.body.classList.toggle('motion-document-hidden', document.hidden);
    if (!active) {
      if (frameID) cancelAnimationFrame(frameID);
      frameID = 0; lastFrame = 0; beatEnergy = 0;
      root.style.removeProperty('--audio-pulse');
      resetCard(); ribbon?.clearHover?.(); clearRings(); finishCounters();
      if (!motion() || media.matches) for (const element of observedSurfaces) element.classList.remove('motion-offscreen');
      if (dirty) prepare();
    } else { dirty = true; requestFrame(); }
  }

  function pointerMove(event) {
    if (!active || route !== 'workspace' || !finePointer.matches || event.pointerType === 'touch') return;
    ribbon?.setPointer?.(event.clientX / Math.max(1,window.innerWidth)*2-1,event.clientY / Math.max(1,window.innerHeight)*2-1);
    if(event.target.closest('.panel, .module-card, .role-card, input, button, dialog, [popover], .chart-inspector')) ribbon?.clearHover?.();
    else ribbon?.hoverAt?.(event.clientX,event.clientY);
    const card = event.target.closest('.module-card, .role-card');
    if (!card || !root.contains(card)) { resetCard(); return; }
    if (selectedCard !== card) {
      resetCard(); selectedCard = card; selectedCard.dataset.motionHover = 'true';
      // Card hover remains a light local tilt; it does not launch an impact wave.
    }
    pointerX = event.clientX; pointerY = event.clientY;
    requestFrame();
  }

  function click(event) {
    if (!active || route !== 'workspace') return;
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    const rect = event.detail ? null : button.getBoundingClientRect();
    const ring = document.createElement('i');
    ring.className = 'motion-click-ring';
    ring.style.left = `${rect ? rect.left + rect.width / 2 : event.clientX}px`;
    ring.style.top = `${rect ? rect.top + rect.height / 2 : event.clientY}px`;
    ring.addEventListener('animationend', () => { rings.delete(ring); ring.remove(); }, { once: true });
    sparks.append(ring); rings.add(ring);
    if (rings.size > 6) { const oldest = rings.values().next().value; oldest.remove(); rings.delete(oldest); }
    ribbon?.pulse(button.dataset.route ? 1.35 : 1.05, { kind: 'click' });
  }


  function backgroundClick(event) {
    if(!active || route !== 'workspace' || event.button !== 0 || event.detail === 0 || event.defaultPrevented) return;
    const interactive='button,a,input,textarea,select,summary,label,dialog,[role="dialog"],[role="button"],[role="slider"],[popover],[contenteditable="true"],.chart-inspector,.audio-dock';
    if(event.target.closest(interactive)) return;
    const ring=document.createElement('i');
    ring.className='motion-background-ring';
    ring.style.left=`${event.clientX}px`;ring.style.top=`${event.clientY}px`;
    ring.setAttribute('aria-hidden','true');
    ring.addEventListener('animationend',()=>{rings.delete(ring);ring.remove();},{once:true});
    sparks.append(ring);rings.add(ring);
    if(rings.size>8){const oldest=rings.values().next().value;oldest.remove();rings.delete(oldest);}
    ribbon?.pulse(1.65,{kind:'background-click'});
  }
  function leaveSurface() { resetCard();ribbon?.clearHover?.();ribbon?.setPointer?.(0,0); }

  const observer = typeof MutationObserver !== 'undefined' ? new MutationObserver(records => {
    const relevant = records.some(record => {
      const target = record.target.nodeType === 1 ? record.target : record.target.parentElement;
      return !target?.closest('.business-surface, [data-motion-counter]');
    });
    if (!relevant || destroyed || route === 'login') return;
    dirty = true;
    if (active) requestFrame(); else prepare();
  }) : null;
  observer?.observe(root, { childList: true, subtree: true });
  root.addEventListener('pointermove', pointerMove, { passive: true, signal: abort.signal });
  root.addEventListener('pointerleave', leaveSurface, { passive: true, signal: abort.signal });
  root.addEventListener('click', click, { passive: true, signal: abort.signal });
  document.addEventListener('click', backgroundClick, { passive: true, signal: abort.signal });
  const invalidateRect = () => { cardRect = null; if (selectedCard) requestFrame(); };
  window.addEventListener('resize', invalidateRect, { passive: true, signal: abort.signal });
  window.addEventListener('scroll', invalidateRect, { passive: true, capture: true, signal: abort.signal });
  document.addEventListener('visibilitychange', refreshMotion, { signal: abort.signal });
  if (media.addEventListener) media.addEventListener('change', refreshMotion); else media.addListener(refreshMotion);
  if (finePointer.addEventListener) finePointer.addEventListener('change', resetCard); else finePointer.addListener(resetCard);

  return {
    refresh(nextRoute = route) {
      if (destroyed) return;
      if (route !== nextRoute) { resetCard(); finishCounters(); delete root.dataset.processing; }
      route = nextRoute; root.dataset.motionRoute = route; dirty = true; refreshMotion();
    },
    refreshMotion,
    setProcessing(type, running) {
      if (destroyed) return;
      if (running) root.dataset.processing = String(type || 'demo'); else delete root.dataset.processing;
      prepare();
      if (running && active) ribbon?.pulse(.8);
    },
    beat(strength = 1) { if (active && route === 'workspace') { beatEnergy = Math.min(1, Math.max(0, Number(strength) || 0)); requestFrame(); } },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      if (frameID) cancelAnimationFrame(frameID);
      frameID = 0; abort.abort(); observer?.disconnect(); visibilityObserver?.disconnect();
      for (const element of observedSurfaces) element.classList.remove('motion-offscreen'); observedSurfaces.clear();
      if (media.removeEventListener) media.removeEventListener('change', refreshMotion); else media.removeListener(refreshMotion);
      if (finePointer.removeEventListener) finePointer.removeEventListener('change', resetCard); else finePointer.removeListener(resetCard);
      resetCard(); ribbon?.clearHover?.(); clearRings(); sparks.remove();
      for (const item of counters.values()) {
        if (item.element.isConnected) { item.element.replaceChildren(...item.original); delete item.element.dataset.motionCounter; }
      }
      counters.clear(); root.style.removeProperty('--audio-pulse');
      for (const key of ['motionRoute', 'motionActive', 'processing']) delete root.dataset[key];
      root.classList.remove('motion-processing'); document.body.classList.remove('motion-document-hidden');
    },
  };
}
