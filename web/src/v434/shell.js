import {initRibbon} from './assets/ribbon.js';
import {terminalView} from './assets/auth-view.js';
import {createAudioEngine} from './assets/audio.js';
import {createAudioControls} from './assets/audio-controls.js';
import {createMotionDirector} from './assets/motion.js';
import {createDatePicker} from './assets/date-picker.js';
import {createDialogMotion} from './assets/dialog-motion.js';
import {researchMark,mountBrandMarks} from './assets/brand.js';
import {playOpening} from './assets/opening.js';
import {visualParts} from './visual-parts.js';
import {mountAccountWelcome} from './account-welcome.js';
export function startShell({checkAccess,getAccountSession,acceptSession,logoutAccount,watchAccountEvents,mountBusiness,warmBusiness}) {
 const $=(s,c=document)=>c.querySelector(s),escapeHTML=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const safePrefs=(()=>{try{return JSON.parse(localStorage.getItem('finance-analyze-preferences-v1'))||{};}catch{return {};}})();
 const state={theme:safePrefs.theme==='dark'?'dark':'light',motion:safePrefs.motion!==false,route:'login'};
 const persistPrefs=()=>{try{localStorage.setItem('finance-analyze-preferences-v1',JSON.stringify({theme:state.theme,motion:state.motion,musicVolume:audio.state.volume}));}catch{}};
 const ribbon=initRibbon($('#ribbon'),{mode:'hero'});ribbon.setPaused(true);
 const motionDirector=createMotionDirector({root:$('#main'),ribbon,motion:()=>state.motion});
 const visual=visualParts({$,ribbon,motionDirector,state,safePrefs,persistPrefs});let audioControls=null;
 const audio=createAudioEngine({onState:visual.updateAudioUI,onBeat:({beat,strength})=>{ribbon.setAudioLevel?.(strength);motionDirector.beat(strength);if(state.motion)document.body.dataset.beat=beat%2?'a':'b';}});
 audioControls=createAudioControls($('.audio-dock'),{motion:()=>state.motion,onModeChange:mode=>{if(mode==='shuffle')audio.setShuffle(true);else if(mode==='repeat-one')audio.setRepeatOne(true);else{audio.setRepeatOne(false);audio.setShuffle(false);}}});visual.setControls(audioControls);audioControls.update(audio.state);
 const datePicker=createDatePicker(),modalMotion=createDialogMotion($('#modal'),{motion:()=>state.motion});
 const modules=[
  ['A','module-a','策略与回测','BACKTEST STUDIO','用已保存的行情看看一种买卖方法过去表现如何。'],
  ['B','module-b','AI 研究','RESEARCH INTELLIGENCE','说明想研究什么，选择模型，查看任务和报告。'],
  ['C','module-c','数据管理','DATA OBSERVATORY','选择来源，下载行情，管理已保存的数据。'],
  ['D','module-d','研究结果','EVIDENCE ARCHIVE','查阅已保存的回测与研究报告。'],
 ];
 const tools={settings:'服务设置',security:'账户安全',jobs:'任务记录',strategies:'策略中心',models:'模型与研究文件',results:'回测报告',accounts:'模拟账户',simulation:'模拟交易',operations:'运行状态与记录'};
 const aliases={research:'module-a',intelligent:'module-b','agent-teams':'module-b',data:'module-c','my-results':'module-d',overview:'workspace'};
 const main=$('#main');
 const retainedModules=new Set(['module-b','module-c','module-d']);
 let session=null,access=null,failed=false,unmount=null,welcome=null,opening=null,renderedRoute=null,clockTimer;
 let connecting=null,requestedRoute=null,exiting=false,exitLocation='';
 const current=()=>{const key=location.hash.slice(1).split('?')[0];return aliases[key]||key||'login';};
 const go=route=>{
  if(exiting)return;
  if(route!=='login'&&!session&&access?.kind!=='preview'){
   if($('#modal').open)modalMotion.close();
   return;
  }
  if(location.hash!=='#'+route)location.hash=route;
  else if($('#modal').open)modalMotion.close();
 };
 const exitLabel=()=>session?.accountMode?'退出登录':'退出工作区，返回欢迎页';
 function updateExitControls(){
  for(const button of document.querySelectorAll('[data-workbench-exit]')){
   button.disabled=exiting;
   button.dataset.busy=String(exiting);
   button.setAttribute('aria-label',exitLabel());
   const label=button.querySelector('[data-exit-label]');
   if(label)label.textContent=exitLabel();
  }
  const note=$('[data-exit-note]');
  if(note)note.textContent=session?.accountMode?'退出后，下次进入工作区需要重新登录。':'已提交的任务会在后台继续运行。';
 }
 function loadAccess(){
  if(connecting)return;
  const attempt=new AbortController();connecting=attempt;failed=false;
  void checkAccess(attempt.signal).then(value=>{
   if(attempt.signal.aborted)return;
   access=value;session=value.session??null;
   if(session)acceptSession(session);
   if((session||access.kind==='preview')&&requestedRoute){
    history.replaceState(null,'','#'+requestedRoute);requestedRoute=null;render();
   }else{
    if(current()!=='login'&&!session&&access.kind!=='preview')history.replaceState(null,'','#login');
    updateWelcome();
   }
  }).catch(()=>{
   if(attempt.signal.aborted)return;
   failed=true;updateWelcome();
  }).finally(()=>{if(connecting===attempt)connecting=null;});
 }
 function signedIn(value){
  session=value;acceptSession(value);
  const next=requestedRoute||'workspace';requestedRoute=null;
  history.replaceState(null,'','#'+next);render();
 }
 function reopenAccount(kind){
  connecting?.abort();unmount?.();unmount=null;welcome?.destroy();welcome=null;session=null;
  if($('#modal').open)modalMotion.close(undefined,{immediate:true});
  main.replaceChildren();
  if(kind==='ended')history.replaceState(null,'','#login');
  location.reload();
 }
 const sendAccountEvent=watchAccountEvents(reopenAccount);
 function signedOut(){sendAccountEvent('ended');reopenAccount('ended');}
 function installRotation(value){
  rechecking?.abort();
  sendAccountEvent('rotated');
  session=value;acceptSession(value);
 }
 async function sessionRotated(receipt,signal){
  signal.throwIfAborted();
  if(!session?.accountMode||session.csrf_token===receipt.csrf_token)throw Error('Missing authentication rotation');
  const expected={...session,csrf_token:receipt.csrf_token,expires_at:receipt.expires_at,recent_until:receipt.recent_until};
  installRotation(expected);
  const current=await getAccountSession(signal);
  if(session!==expected||current.user.user_id!==expected.user.user_id||current.user.role!==expected.user.role||current.csrf_token!==expected.csrf_token){
   if(session===expected)reopenAccount('rotated');
   throw Error('Session changed during verification');
  }
  session=current;acceptSession(current);
 }
 let rechecking=null;
 window.addEventListener('finance-account-session-ended',recheckAccount);
 function recheckAccount(){
  if(!session?.accountMode||document.hidden||exiting||rechecking)return;
  const previous=session;const attempt=new AbortController();rechecking=attempt;
  void getAccountSession(attempt.signal).then(value=>{
   if(session!==previous||attempt.signal.aborted)return;
   if(value.user.user_id!==previous.user.user_id||value.user.role!==previous.user.role)signedOut();
   else if(value.csrf_token!==previous.csrf_token)installRotation(value);
  }).catch(error=>{
   if(!attempt.signal.aborted&&session===previous&&error?.code==='NOT_AUTHENTICATED')signedOut();
  }).finally(()=>{if(rechecking===attempt)rechecking=null;});
 }
 async function leaveWorkbench(){
  if(exiting||current()==='login')return;
  if(!session?.accountMode){go('login');return;}
  exiting=true;exitLocation=location.hash;updateExitControls();
  const status=$('#workbench-exit-status');
  status.textContent='正在退出登录…';updateExitStatus();
  try{
   await logoutAccount();
   signedOut();
  }catch{
   status.textContent='退出登录失败，请重试。';updateExitStatus();
  }finally{
   exiting=false;updateExitControls();
  }
 }
 function updateExitStatus(){
  const note=$('[data-exit-state]');
  if(note)note.textContent=$('#workbench-exit-status')?.textContent??'';
 }
 const syncRibbon=()=>ribbon.setPaused(!state.motion||state.route==='login'&&!!document.body.dataset.opening);
 function preferences(){document.body.dataset.theme=state.theme;document.body.classList.toggle('motion-paused',!state.motion);document.documentElement.classList.toggle('fa-reduced-motion',!state.motion);syncRibbon();motionDirector.refreshMotion();modalMotion.refreshMotion();welcome?.refreshMotion();document.dispatchEvent(new Event('account-motion-change'));if(!state.motion)opening?.finish();}
 function updateDisplayControls(){
  const modal=$('#modal');
  modal.querySelectorAll('[data-theme]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.theme===state.theme)));
  document.querySelectorAll('[data-motion]').forEach(button=>button.setAttribute('aria-pressed',String(state.motion)));
 }
 function settings(){
  const modal=$('#modal');
  if(!modal.open){
   modal.innerHTML=`<div class="modal-header"><h2 id="modal-title">显示与工作区</h2><button data-close>关闭</button></div>
    <div class="modal-content workbench-settings">
      <section aria-labelledby="appearance-heading">
        <h3 id="appearance-heading">外观</h3>
        <div class="workbench-settings-actions">
          <button data-theme="light" aria-pressed="false">象牙白</button>
          <button data-theme="dark" aria-pressed="false">墨黑</button>
          <button data-motion aria-pressed="false">流动动画</button>
        </div>
      </section>
      <section aria-labelledby="tools-heading">
        <h3 id="tools-heading">工作区</h3>
        <div class="workbench-settings-actions"><button data-route="settings">服务设置</button><button data-route="jobs">任务记录</button>
        ${session?.accountMode?'<button data-route="security">账户安全</button>':''}
        ${renderedRoute==='login'?'':'<button data-workbench-exit><span data-exit-label></span></button>'}</div>
        ${renderedRoute==='login'?'':'<p class="workbench-exit-note" data-exit-note></p>'}
        <p class="workbench-exit-note" data-exit-state role="status"></p>
        <details><summary>更多工具</summary><div class="v434-advanced-links"><button data-route="strategies">策略中心</button><button data-route="models">模型与研究文件</button><button data-route="results">更多回测报告</button><button data-route="accounts">模拟账户</button><button data-route="simulation">模拟交易</button><button data-route="operations">运行状态与记录</button></div></details>
      </section>
    </div>`;
  }
  updateDisplayControls();
  updateExitControls();
  updateExitStatus();
  modalMotion.open();
 }
 function intro(){ $('#main').innerHTML=terminalView();$('#login-form').outerHTML='<div class="auth-key-form" data-welcome-entry></div>';
 $('#operator-name').outerHTML='<strong id="operator-name-display" class="operator-name-wrap">'+escapeHTML(session?.user.username||'PERSONAL RESEARCH')+'</strong>';
 $('.dossier-foot').innerHTML='';
 $('.terminal-controls').innerHTML='<button data-motion>流动动画</button><button data-action="settings">显示设置</button>';
 $('.terminal-system-note').textContent='RESEARCH ENVIRONMENT';$('#auth-log').textContent='';$('#auth-phase-code').textContent='';$('.operator-record dd').textContent='';$('.terminal-demo-caption').textContent='研究工作区';$('.session-seal').setAttribute('aria-label','研究工作区标识');$('.auth-warning')?.remove();$('.auth-impact-field')?.remove();
 updateDisplayControls();updateWelcome();
 }
 function updateWelcome(){
  if(renderedRoute!=='login')return;
  if(access?.kind==='accounts'&&!session){
   if(!welcome)welcome=mountAccountWelcome($('.terminal-screen'),ribbon,{
    status:access.status,motion:()=>state.motion,onAuthenticated:()=>{
     sendAccountEvent('changed');
     warmBusiness(true);
    },
    onSignedIn:signedIn,onRecovered:signedOut,
    onRetry:()=>{welcome?.destroy();welcome=null;access=null;intro();loadAccess();},
   });
   return;
  }
  const entry=$('[data-welcome-entry]');if(!entry)return;
  const title=session?(session.accountMode?'登录成功，可以进入工作区。':'本机个人工作区已准备好。')
   :access?.kind==='preview'?'这是界面预览。要使用研究功能，请打开电脑上的 FinanceAnalyze.exe，或登录已部署的研究服务。'
   :failed?'连不上研究服务。请确认服务已启动，再点“重新检查”。':'正在检查研究服务…';
  entry.innerHTML='<p role="status">'+title+'</p>'+(session||access?.kind==='preview'
   ?'<button class="primary-button" data-route="workspace">'+(session?'进入工作区 →':'浏览界面 →')+'</button>'
   :failed?'<button class="primary-button" data-retry-access>重新检查</button>':'');
  $('#auth-state-label').textContent=session?'会话已准备好':access?.kind==='preview'?'界面预览':failed?'暂未连接':'正在连接';
  $('#auth-status').textContent='';$('#auth-status-en').textContent='';
  if(session)$('#operator-name-display').textContent=session.user.username;
 }
 function workspace(){
  main.innerHTML=`<section class="page workspace-page">
    <header class="workspace-intro">
      <div class="workspace-intro-copy"><h1>从一个问题，<br><span>开始研究。</span></h1><p>选择数据，运行回测或 AI 研究，再打开保存的报告。</p></div>
      ${session?`<div class="workspace-greeting"><p>${escapeHTML(session.user.username)}</p><span class="badge badge-dot">${session.accountMode?'已登录账户':'本机个人模式'}</span></div>`:''}
    </header>
    ${session?`<p class="form-actions">${session.user.role!=='viewer'?'<a class="primary-button" href="#module-a?first_example=1">先跑一个例子</a>':''}<a class="text-button" href="#jobs">查看任务进度</a></p>`:''}
    <div class="module-grid">${modules.map(([letter,route,label,en,description])=>`<button class="module-card" data-route="${route}" aria-label="进入${label}">
      <div class="module-card-top"><span>${en}</span><span aria-hidden="true">↗</span></div>
      <div class="module-card-art" aria-hidden="true"><span class="module-letter">${letter}</span><span class="module-glyph-frame">${visual.glyph(letter)}</span></div>
      <h2>${label}</h2><p>${description}</p>
      <div class="module-card-bottom"><span>打开</span>${visual.icon('arrow')}</div>
    </button>`).join('')}</div>
  </section>`;
 }
 function render({fromHash=false}={}){
 if(exiting){
  if(location.hash!==exitLocation)history.replaceState(null,'',exitLocation);
  return;
 }
 let next=current();
 if(next!=='login'&&!session&&access?.kind!=='preview'){
  if(next==='module-a'||next==='module-c'||next==='module-d'||next==='jobs'||location.hash==='#module-b?research_tab=external'||location.hash==='#settings?section=migration')requestedRoute=location.hash.slice(1);
  else if(!access)requestedRoute=next;
  next='login';
  if(access||failed)history.replaceState(null,'','#login');
 }
 if(next==='security'&&!session?.accountMode){next=session?'workspace':'login';history.replaceState(null,'','#'+next);}
 if(next===renderedRoute){
  if($('#modal').open)modalMotion.close();
  if(!fromHash||retainedModules.has(next)||next==='workspace'||next==='login')return;
 }
 const firstRender=renderedRoute===null;
 renderedRoute=next;
 unmount?.();unmount=null;welcome?.destroy();welcome=null;datePicker.close();
 if($('#modal').open)modalMotion.close(undefined,{immediate:true});
 state.route=next;document.body.classList.toggle('auth-screen',next==='login');document.body.classList.toggle('is-signed-in',next!=='login');document.body.dataset.route=next;
 ribbon.setMode(next==='login'?'hero':next==='workspace'?'workspace':'detail');syncRibbon();audio.setMode(next==='login'?'login':next==='workspace'?'workspace':'detail');
 if(!$('#rail').children.length)$('#rail').innerHTML='<div class="rail-top">菜单</div><button class="rail-button" data-route="workspace" aria-label="工作台">'+visual.icon('grid')+'</button>'+modules.map(([letter,route,label])=>'<button class="rail-button" data-route="'+route+'" aria-label="模块 '+letter+' · '+label+'">'+letter+'<span class="rail-tooltip">'+label+'</span></button>').join('')+'<div class="rail-bottom"><button class="rail-button" data-action="settings" aria-label="设置">'+visual.icon('settings')+'</button><button class="rail-button workspace-exit" data-workbench-exit>'+visual.icon('logout')+'<span class="rail-tooltip" data-exit-label></span></button></div>';
 updateExitControls();
 $('#rail').querySelectorAll('[data-route]').forEach(b=>{b.classList.toggle('active',b.dataset.route===next);if(b.dataset.route===next)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
 main.classList.remove('fa-enter-detail','fa-enter-workspace');
 if(next==='login'){
  intro();visual.updateClock();
 }else if(next==='workspace'){
  workspace();
  if(session)warmBusiness();
 }else{
  const module=modules.find(m=>m[1]===next);
  const title=module?.[2]??tools[next]??'工作区工具';
  const nextView = `<section class="page detail-page">
   <div class="workspace-breadcrumb"><button data-route="workspace">工作台</button><span>/</span><span>${module?.[0]??title}</span></div>
   <header class="view-heading">
     ${module?`<span class="view-letter" aria-hidden="true">${module[0]}</span>`:''}
     <div>${module?`<div class="eyebrow">${module[3]}</div>`:''}<h1>${title}</h1>${module?`<p>${module[4]}</p>`:''}</div>
     <div class="view-heading-actions"><span class="badge badge-dot">${session?(session.accountMode?'已登录账户':'本机个人模式'):'界面预览'}</span></div>
   </header>
   <div class="business-surface"></div>
 </section>`;
 main.innerHTML=nextView;
 unmount=mountBusiness($('.business-surface'),next,session,signedOut,sessionRotated,()=>state.motion);
 }
 if(!firstRender&&next!=='login'&&state.motion&&!document.hidden&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
  main.classList.add(next==='workspace'?'fa-enter-workspace':'fa-enter-detail');
 }
 motionDirector.refresh(next);$('#footer-center').textContent=(session?.accountMode?'ACCOUNT':session?'LOCAL PERSONAL':access?.kind==='preview'?'PREVIEW':'FINANCE ANALYZE')+' / '+next.toUpperCase();
 if(!firstRender)main.focus({preventScroll:true});
 }
 document.addEventListener('click',event=>{const el=event.target.closest('button,a');if(!el)return;if(el.matches('.skip-link')){event.preventDefault();main.focus();return;}if(el.matches('[data-workbench-exit]')){void leaveWorkbench();return;}if(el.dataset.route){go(el.dataset.route);return;}if(el.matches('[data-action="home"]'))go('workspace');if(el.matches('[data-action="settings"],[data-action="command"]'))settings();if(el.matches('[data-close]'))modalMotion.close();if(el.dataset.theme){state.theme=el.dataset.theme;preferences();updateDisplayControls();persistPrefs();}if(el.hasAttribute('data-motion')){state.motion=!state.motion;preferences();updateDisplayControls();persistPrefs();}if(el.matches('[data-audio-toggle],[data-audio-play]'))void audio.toggle();if(el.matches('[data-audio-next]'))audio.nextTrack();if(el.matches('[data-audio-previous]'))audio.previousTrack();if(el.dataset.audioTrack){audio.selectTrack(el.dataset.audioTrack);if(!audio.state.enabled)void audio.enable();}});
 document.addEventListener('input',event=>{if(event.target.matches('[data-audio-volume]')){audio.setVolume(Number(event.target.value)/100);persistPrefs();}});
 document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();settings();return;}if(event.target.closest('input,textarea,select,[contenteditable],dialog')||event.ctrlKey||event.metaKey||event.altKey)return;const i=Number(event.key)-1;if(i>=0&&i<4)go(modules[i][1]);if(event.key==='g')go('workspace');});
 function syncClock(){
  clearInterval(clockTimer);
  if(document.hidden)return;
  visual.updateClock();
  clockTimer=setInterval(visual.updateClock,1000);
 }
 window.addEventListener('hashchange',()=>{opening?.finish();render({fromHash:true});});document.addEventListener('visibilitychange',()=>{document.body.classList.toggle('page-hidden',document.hidden);syncClock();recheckAccount();});window.addEventListener('pagehide',()=>{connecting?.abort();rechecking?.abort();welcome?.destroy();unmount?.();unmount=null;clearInterval(clockTimer);opening?.destroy();audioControls.destroy();audio.destroy();ribbon.destroy?.();motionDirector.destroy();datePicker.destroy();modalMotion.destroy();});
 const exitStatus=document.createElement('p');
 exitStatus.id='workbench-exit-status';
 exitStatus.setAttribute('role','status');
 exitStatus.className='system-exit-status';
 $('#rail').after(exitStatus);
 mountBrandMarks();preferences();render();syncClock();
 opening=playOpening({target:$('[data-brand-anchor]')||$('.brand-symbol .research-mark'),mark:researchMark(),enabled:state.motion&&state.route==='login',onComplete:syncRibbon});
 document.addEventListener('click',event=>{if(event.target.closest('[data-retry-access]')){loadAccess();updateWelcome();}});
 loadAccess();
 return {render};
}
