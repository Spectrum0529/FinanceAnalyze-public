// Keep the installed welcome scene and place the account form in its dossier.
// The shell supplies form choices after checking the active service.
export function prepareAccountTerminal(terminal, ribbon, { motion }) {
  terminal.classList.add('account-terminal');
  terminal.setAttribute('aria-label', 'Finance Analyze 账户入口');
  const find = selector => terminal.querySelector(selector);
  const dossier = find('.auth-dossier');
  const panel = document.createElement('div');
  panel.className = 'account-terminal-panel';
  const controls = find('.terminal-controls');
  const portrait = find('.dossier-portrait');
  portrait.removeAttribute('aria-label');
  portrait.setAttribute('aria-hidden', 'true');
  portrait.querySelector('.portrait-classified')?.remove();
  dossier.replaceChildren(portrait, panel, controls);
  dossier.removeAttribute('aria-labelledby');

  const clearDemo = [
    '.terminal-sidebar-data', '.terminal-log-block', '.terminal-shortcuts',
    '.terminal-mini-modules', '.auth-warning', '.auth-impact-field', '.terminal-status',
  ];
  for (const selector of clearDemo) find(selector)?.remove();
  find('.auth-annotation-top small').textContent = 'FINANCE ANALYZE';
  find('.terminal-demo-caption').textContent = '账户入口';
  find('#session-seal').setAttribute('aria-label', 'Finance Analyze 登录标识');
  const stage = find('#auth-stage-label');
  stage.textContent = '';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');

  function setPhase(phase) {
    find('#auth-state-label').textContent = {
      idle: '账户入口', verifying: '正在验证', error: '请重试', granted: '验证成功',
    }[phase];
    if (motion() && !reduced.matches && !document.hidden) {
      ribbon.setState?.(phase === 'granted' ? 'success' : phase);
    }
  }

  setPhase('idle');
  return { panel, setPhase };
}
