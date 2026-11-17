import { researchMark } from './brand.js';

// Lock-screen composition. All controls are connected by app.js / auth-motion.js.
// This is a local demonstration identity; it is not an authentication boundary.
export function terminalView() {
  return `<section class="terminal-screen" id="terminal" data-phase="idle" aria-label="Finance Analyze 研究终端登录">
    <div class="auth-registration auth-registration-nw" aria-hidden="true">+</div>
    <div class="auth-registration auth-registration-se" aria-hidden="true">+</div>
    <aside class="terminal-sidebar" aria-label="终端信息">
      <div class="terminal-brand">
        <span class="auth-micro auth-brand-overline">金融研究工作台</span>
        <div class="terminal-brand-heading">${researchMark({anchor:true})}<h1>FINANCE LAB</h1></div>
        <span class="terminal-brand-sub">历史回测与 AI 研究</span>
      </div>
      <div class="terminal-sidebar-data">
        <span class="auth-dash" aria-hidden="true"></span>
        <div class="terminal-log-heading">FINANCE LAB / TERMINAL</div>
        <p class="terminal-system-note">PERSONAL RESEARCH ENVIRONMENT<br>LOCAL DEMONSTRATION SESSION<br>FOUR MODULES · ONE WORKSPACE</p>
        <div class="terminal-barcode" aria-hidden="true"></div>
      </div>
      <div class="terminal-log-block">
        <div class="terminal-log-heading">SESSION EVENT LOG</div>
        <div id="auth-log" class="terminal-log" aria-label="当前演示会话日志" aria-live="off">
          <div data-log-line><span>01</span> SYSTEM INITIALIZED</div>
          <div data-log-line><span>02</span> INTERFACE LOADED</div>
          <div data-log-line><span>03</span> SESSION LOCKED <i></i></div>
          <div data-log-line class="log-current"><span>04</span> AWAITING INPUT <b>_</b></div>
        </div>
      </div>
      <div class="terminal-sidebar-bottom">
        <div class="terminal-shortcuts"><span><kbd>ENTER</kbd> REQUEST</span><span><kbd>ESC</kbd> CLEAR</span></div>
        <div class="terminal-sealed" aria-hidden="true"><i></i></div>
        <div class="terminal-version"><span>版本 04.03.4</span></div>
        <div class="terminal-mini-modules" aria-label="A、B、C、D 四个模块已就绪"><b>A</b><b>B</b><b>C</b><b>D</b><span>MODULES READY</span></div>
      </div>
    </aside>

    <div class="terminal-field" aria-hidden="true">
      <div class="sculpture-field"><div class="sculpture-orbit orbit-one"><i></i></div><div class="sculpture-orbit orbit-two"><i></i></div><div class="sculpture-orbit orbit-three"></div><span class="field-particle particle-one">+</span><span class="field-particle particle-two">+</span><span class="field-particle particle-three"></span><span class="field-particle particle-four"></span></div>
      <svg class="auth-leader auth-leader-top" viewBox="0 0 220 140" fill="none"><path d="M20 123 99 23h117"/><circle cx="20" cy="123" r="2.6"/></svg>
      <div class="auth-annotation auth-annotation-top"><span></span><strong></strong><small></small></div>
      <svg class="auth-leader auth-leader-bottom" viewBox="0 0 230 82" fill="none"><path d="m9 10 100 44h118"/><circle cx="9" cy="10" r="2.6"/></svg>
      <div class="auth-annotation auth-annotation-bottom"></div>
      <div class="auth-division-label"><i></i></div>
      <div class="auth-coordinate"><i>+</i></div>
      <div id="auth-scan" class="auth-scan"><div class="auth-scan-ring"></div><div class="auth-scan-ring auth-scan-ring-inner"></div><span>正在验证</span><small id="auth-stage-label"></small><i></i></div>
    </div>

    <div class="session-seal-wrap">
      <span id="auth-state-label" class="auth-seal-state">正在检查</span>
      <div id="session-seal" class="session-seal" aria-label="Finance Analyze 登录">
        <span class="seal-corner" aria-hidden="true"></span>
        <span class="seal-orbit" aria-hidden="true"></span>
        <svg class="seal-monogram" viewBox="10 0 142 79" fill="none" aria-hidden="true"><path d="M21 67V12h49M21 37h37M86 67l23-55h8l24 55M96 45h35" stroke="currentColor" stroke-width="9" stroke-linejoin="miter"/></svg>
        <span class="seal-subtitle">FINANCE ANALYZE</span>
        <div class="seal-spectrum" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
      </div>
    </div>

    <div class="terminal-clock"><time data-auth-clock>00:00<span>00</span></time><span data-auth-date>香港时间（UTC+8）</span></div>

    <section class="auth-dossier" aria-labelledby="auth-profile-heading">
      <span class="dossier-watermark" aria-hidden="true">RESEARCH</span>
      <div class="dossier-topline"><h2 id="auth-profile-heading">■ SECTION PROFILE</h2><span>PERSONAL ACCESS CONTENT<br><b>FINANCE LAB.</b></span></div>
      <div class="dossier-body">
        <div class="dossier-visual">
          <div class="dossier-portrait" aria-label="研究员身份示意头像">
            <span class="portrait-insignia" aria-hidden="true">${researchMark()}</span><span class="portrait-cross" aria-hidden="true">+</span>
            <svg viewBox="0 0 150 186" role="img" aria-label="网格研究员剪影"><defs><pattern id="auth-portrait-grid" width="4" height="4" patternUnits="userSpaceOnUse"><path d="M4 0H0V4" stroke="#a5b695" stroke-width=".38" opacity=".65"/></pattern><clipPath id="auth-portrait-mask"><path d="M75 23c-20 0-31 15-31 36 0 17 7 31 18 39v13C23 121 8 142 5 181h140c-3-39-18-60-57-70V98c11-8 18-22 18-39 0-21-11-36-31-36Z"/></clipPath></defs><g clip-path="url(#auth-portrait-mask)"><path d="M0 0h150v186H0z" fill="#111813"/><path d="M0 0h150v186H0z" fill="url(#auth-portrait-grid)"/></g><path d="M1 1h148v184H1Z" stroke="currentColor" stroke-opacity=".14" fill="none"/></svg>
            <span class="portrait-scan" aria-hidden="true"></span><span class="portrait-classified">PHOTO CLASSIFIED</span>
          </div>
          <span class="dossier-info-word" aria-hidden="true">INFO</span>
          <div class="auth-indicators" aria-label="界面、会话、模块已就绪"><span><i></i><small>UI</small></span><span><i></i><small>SESSION</small></span><span><i></i><small>MODULE</small></span></div>
        </div>
        <div class="dossier-details">
          <label class="operator-label" for="operator-name"><b>OPERATOR</b><span>EDITABLE IDENTITY</span></label>
          <div class="operator-name-wrap"><input id="operator-name" form="login-form" name="name" value="RESEARCHER" autocomplete="off" maxlength="32" required spellcheck="false" aria-label="研究员名称"><span aria-hidden="true">↗</span></div>
          <div class="operator-session"><div class="session-mini-mark" aria-hidden="true">${researchMark()}</div><div><span>PERSONAL INFORMATION LOADED <i></i></span><small>■ FA—001 <span id="auth-phase-code">LOCKED / 00:00</span></small></div><b class="operator-brand-mark" aria-hidden="true">${researchMark()}</b></div>
          <div class="operator-record"><h3>PROFILE</h3><dl><dt>HOST</dt><dd>LOCAL DEMO</dd><dt>SESSION</dt><dd>RESEARCH OS</dd><dt>ENVIRONMENT</dt><dd>FRONTEND</dd><dt>KEYMAP</dt><dd>US / ZH</dd></dl><span>FINANCE LAB. / 001</span></div>
        </div>
      </div>
      <form id="login-form" class="auth-key-form">
        <label for="access-code"><span>— AUTHORIZATION KEY</span><span>授权访问口令</span></label>
        <div class="auth-key-row"><input id="access-code" name="code" type="password" placeholder="ENTER AUTHORIZATION KEY" autocomplete="off" required aria-describedby="demo-key-note login-error" maxlength="64"><button type="submit" id="login-submit"><span>REQUEST</span><b aria-hidden="true">→</b></button><div id="auth-progress" aria-hidden="true"></div></div>
        <p class="auth-error" id="login-error" role="alert"></p>
      </form>
      <div class="dossier-foot"><span id="demo-key-note">■ DEMO KEY: <b>FINANCE</b></span><button type="button" data-action="demo-enter">直接体验 <span aria-hidden="true">↗</span></button></div>
    <nav class="terminal-controls" aria-label="锁屏终端操作">
      <button type="button" data-action="terminal-reset"><i></i>RESET KEY</button><button type="button" data-action="terminal-motion" aria-pressed="true"><i></i><span>MOTION ON</span></button><button type="button" data-action="settings"><i></i>DISPLAY</button><span class="terminal-powered">POWERED BY <b>FINANCE LAB</b><i></i></span>
    </nav>
    </section>

    <div class="auth-impact-field" aria-hidden="true"><span class="auth-impact-ring"></span><span class="auth-impact-trace"></span><span class="auth-impact-vignette"></span><span class="auth-corrupt-strips"></span><span class="auth-corrupt-status">SIGNAL FAULT&nbsp; / &nbsp;KEY REJECTED<br><small>ERR 401&nbsp; · &nbsp;SESSION SEALED</small></span></div>
    <div id="auth-warning" class="auth-warning" aria-hidden="true"><div class="auth-warning-meta"><span>!</span><b>KEY REJECTED</b><em>401</em></div><p>ACCESS<br><strong>DENIED</strong></p><small>口令不正确，请重新输入。<span>SESSION REMAINS LOCKED</span></small><i></i></div>
    <div class="terminal-status" role="status" aria-live="polite"><p id="auth-status">研究会话已锁定。请输入授权口令。</p><span id="auth-status-en">YOUR RESEARCH SESSION IS LOCKED. PRESENT YOUR AUTHORIZATION KEY.</span></div>
    <div class="terminal-demo-caption">界面演示 <i></i> LOCAL SESSION</div>
  </section>`;
}
