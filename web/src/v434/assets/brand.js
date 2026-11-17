// Research mark: an observation window, a measured curve and one sample point.
// Pure SVG geometry keeps the identity crisp and lets the opening draw each path.
export const BRAND_PATHS = Object.freeze([
  { d: 'M8 40V18L19 7H56V20', width: 4.3, kind: 'frame' },
  { d: 'M56 34V49Q56 57 48 57H8V50', width: 4.3, kind: 'frame' },
  { d: 'M8 45C20 45 24 41 31 34S39 28 42.5 26.7', width: 3, kind: 'curve' },
  { d: 'M51 24.5Q54 23.3 56 23', width: 3, kind: 'curve' },
  { d: 'M38 45.5H49', width: 1.9, kind: 'baseline' },
]);

export function researchMark({ className = '', anchor = false } = {}) {
  const classes = String(className).replace(/[^a-zA-Z0-9_ -]/g, '');
  return `<svg class="research-mark ${classes}" viewBox="0 0 64 64" fill="none" aria-hidden="true" focusable="false"${anchor ? ' data-brand-anchor=""' : ''}>${BRAND_PATHS.map(path => `<path data-logo-stroke="${path.kind}" data-logo-part="${path.kind}" pathLength="1" d="${path.d}" stroke="currentColor" stroke-width="${path.width}" stroke-linecap="${path.kind === 'baseline' ? 'round' : 'butt'}" stroke-linejoin="miter"/>`).join('')}<circle data-logo-dot="" cx="47" cy="25.3" r="2.8" fill="var(--brand-accent, #b79554)"/></svg>`;
}

export function mountBrandMarks(root = document) {
  root.querySelectorAll('[data-brand-slot]').forEach(slot => {
    slot.innerHTML = researchMark({ className: slot.dataset.brandSlot });
  });
}
