// Keep the native dialog in the top layer until its short exit has finished.
// Reopening cancels the old close so a delayed callback cannot dismiss new content.
export function createDialogMotion(dialog, { motion = () => true } = {}) {
  const doc = dialog.ownerDocument;
  const view = doc.defaultView;
  const reduced = view.matchMedia?.('(prefers-reduced-motion: reduce)');
  const events = new view.AbortController();
  let pending = null;
  let destroyed = false;

  const canAnimate = () => motion() !== false && !doc.hidden && !reduced?.matches && typeof dialog.animate === 'function';

  function cancelClose() {
    const previous = pending;
    pending = null;
    if (previous) {
      view.clearTimeout(previous.timer);
      previous.animation?.cancel();
    }
    dialog.removeAttribute('data-dialog-closing');
  }

  function nativeClose(returnValue) {
    if (!dialog.open) return;
    if (returnValue === undefined) dialog.close();
    else dialog.close(returnValue);
  }

  function finish(task) {
    if (pending !== task) return;
    cancelClose();
    nativeClose(task.returnValue);
  }

  function close(returnValue, { immediate = false } = {}) {
    if (destroyed) return false;
    if (!dialog.open) { cancelClose(); return false; }
    if (pending) {
      if (immediate || !canAnimate()) finish(pending);
      return true;
    }
    if (immediate || !canAnimate()) { nativeClose(returnValue); return true; }

    // Sampling the entering dialog also makes an immediate click on Close smooth.
    const computed = view.getComputedStyle(dialog);
    const task = { returnValue, animation: null, timer: 0 };
    pending = task;
    dialog.dataset.dialogClosing = 'true';
    try {
      task.animation = dialog.animate([
        { opacity: computed.opacity, transform: computed.transform },
        { opacity: 0, transform: 'translateY(9px) scale(.985)' },
      ], { duration: 190, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards' });
      task.animation.finished.then(() => finish(task), () => finish(task));
      task.timer = view.setTimeout(() => finish(task), 240);
    } catch {
      finish(task);
    }
    return true;
  }

  function open() {
    if (destroyed) return false;
    cancelClose();
    if (!dialog.open) dialog.showModal();
    return true;
  }

  function refreshMotion() {
    if (pending && !canAnimate()) finish(pending);
  }

  dialog.addEventListener('cancel', event => {
    if (event.defaultPrevented || destroyed) return;
    event.preventDefault();
    close();
  }, { signal: events.signal });
  dialog.addEventListener('close', () => {
    // A close event can arrive after the same dialog has already been reopened.
    if (!dialog.open) cancelClose();
  }, { signal: events.signal });
  doc.addEventListener('visibilitychange', refreshMotion, { signal: events.signal });
  view.addEventListener('pagehide', () => { if (pending) finish(pending); }, { signal: events.signal });
  reduced?.addEventListener?.('change', refreshMotion, { signal: events.signal });

  return {
    open, close, refreshMotion,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      events.abort();
      const returnValue = pending?.returnValue;
      cancelClose();
      nativeClose(returnValue);
    },
  };
}
