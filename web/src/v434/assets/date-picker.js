// Calendar dates are deliberately local strings: no timezone shifts, downloads,
// native white popups or replacement of the original form's field names.
const MONTHS = ['一月','二月','三月','四月','五月','六月','七月','八月','九月','十月','十一月','十二月'];
const WEEKDAYS = ['一','二','三','四','五','六','日'];
const pad = value => String(value).padStart(2, '0');
const isoDate = (year, month, day) => `${String(year).padStart(4, '0')}-${pad(month)}-${pad(day)}`;
export const daysInMonth = (year, month) => month === 2 ? (year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28) : [4,6,9,11].includes(month) ? 30 : 31;
export function parseISODate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [year, month, day] = value.split('-').map(Number);
  if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return null;
  return { year, month, day, iso: value };
}
const utc = date => { const result = new Date(0); result.setUTCHours(0,0,0,0); result.setUTCFullYear(date.year,date.month-1,date.day); return result; };
const fromUTC = value => parseISODate(isoDate(value.getUTCFullYear(),value.getUTCMonth()+1,value.getUTCDate()));
const weekday = date => (utc(date).getUTCDay()+6)%7;
const addDays = (date, amount) => { const value = utc(date); value.setUTCDate(value.getUTCDate()+amount); return fromUTC(value); };
const addMonths = (date, amount) => {
  const offset = date.year*12+date.month-1+amount, year = Math.floor(offset/12), month = offset%12+1;
  return year < 1 || year > 9999 ? null : parseISODate(isoDate(year,month,Math.min(date.day,daysInMonth(year,month))));
};
export function buildMonth(year, month, min = '', max = '') {
  const first = parseISODate(isoDate(year,month,1));
  if (!first) return [];
  const date = utc(first); date.setUTCDate(date.getUTCDate()-weekday(first));
  return Array.from({length:42},() => {
    const parsed = fromUTC(date), day = date.getUTCDate();
    const result = { day, iso: parsed?.iso || '', outside: date.getUTCMonth()+1 !== month,
      disabled: !parsed || Boolean(min && parsed.iso<min) || Boolean(max && parsed.iso>max) };
    date.setUTCDate(date.getUTCDate()+1);
    return result;
  });
}

export function createDatePicker({ root = document } = {}) {
  const documentRoot = root.ownerDocument || root;
  const win = documentRoot.defaultView || window;
  const events = new AbortController();
  const records = new Map();
  let serial = 0, current = null, popup = null, selectedDay = null, viewMonth = null;
  let chooser = false, positionFrame = 0, destroyed = false, popoverUsed = false;
  let transitionTimer = 0, transitionView = null, transitionGhost = null, transitionEnd = null;
  const reducedMotion = win.matchMedia?.('(prefers-reduced-motion: reduce)');
  const today = () => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(part=>[part.type,part.value]));
    return parseISODate(`${parts.year}-${parts.month}-${parts.day}`);
  };
  const bounds = input => ({ min:parseISODate(input.min)?.iso || '', max:parseISODate(input.max)?.iso || '' });
  const allowed = (date, range) => Boolean(date && (!range.min || date.iso >= range.min) && (!range.max || date.iso <= range.max));
  const clamp = (date, range) => parseISODate(range.min && date.iso < range.min ? range.min : range.max && date.iso > range.max ? range.max : date.iso);
  const label = input => input.labels?.[0]?.textContent?.trim() || input.getAttribute('aria-label') || '日期';
  const fieldRecord = target => target?.closest?.('[data-date-input]') ? records.get(target.closest('[data-date-input]')) : null;
  const buttonRecord = target => { const wrapper = target?.closest?.('.date-input-wrap'); return wrapper ? records.get(wrapper.querySelector('[data-date-input]')) : null; };

  function validate(record, showEmpty = false) {
    const { input, feedback } = record;
    const value = input.value, date = parseISODate(value), range = bounds(input);
    let message = '';
    if (value && !date) message = '请输入有效日期，格式为 YYYY-MM-DD。';
    else if (date && range.min && value < range.min) message = `日期不得早于 ${range.min}。`;
    else if (date && range.max && value > range.max) message = `日期不得晚于 ${range.max}。`;
    else if (!value && input.required && showEmpty) message = '请选择日期。';
    input.setCustomValidity(message);
    if (message) input.setAttribute('aria-invalid','true'); else input.removeAttribute('aria-invalid');
    feedback.textContent = message;
    feedback.hidden = !message;
    record.button.disabled = input.disabled || input.readOnly;
    record.button.setAttribute('aria-label', `选择${label(input)}${date ? `，当前 ${value}` : ''}`);
    return !message && (!input.required || Boolean(value));
  }

  function enhance(input) {
    const id = `fa-date-${++serial}`;
    const original = { type:input.type, placeholder:input.getAttribute('placeholder'), inputmode:input.getAttribute('inputmode'), autocomplete:input.getAttribute('autocomplete'), describedby:input.getAttribute('aria-describedby'), maxlength:input.getAttribute('maxlength') };
    const wrapper = documentRoot.createElement('div'); wrapper.className='date-input-wrap';
    input.before(wrapper); wrapper.append(input);
    input.type='text'; input.dataset.dateInput=id; input.placeholder='YYYY-MM-DD'; input.inputMode='text'; input.autocomplete='off'; input.maxLength=10;
    const button=documentRoot.createElement('button'); button.type='button';button.className='date-input-button';button.dataset.dateToggle='';
    button.setAttribute('aria-haspopup','dialog');button.setAttribute('aria-expanded','false');button.setAttribute('aria-controls',`${id}-popup`);
    button.innerHTML='<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.2" aria-hidden="true"><rect x="3" y="4.5" width="14" height="12.5"/><path d="M3 8.5h14M6.5 2v5M13.5 2v5M6 11.5h2M10 11.5h2M6 14.5h2M10 14.5h2"/></svg>';
    wrapper.append(button);
    const feedback=documentRoot.createElement('span'); feedback.id=`${id}-feedback`;feedback.className='date-input-feedback';feedback.hidden=true;
    wrapper.after(feedback);
    input.setAttribute('aria-describedby',[original.describedby,feedback.id].filter(Boolean).join(' '));
    const record={input,wrapper,button,feedback,original}; records.set(input,record); validate(record);
  }

  function finishViewTransition() {
    if (transitionTimer) win.clearTimeout(transitionTimer);
    transitionTimer=0;
    if (transitionView) {
      transitionView.classList.remove('is-entering');
      if (transitionEnd) transitionView.removeEventListener('animationend',transitionEnd);
    }
    transitionGhost?.remove();
    transitionView=null;transitionGhost=null;transitionEnd=null;
    if (popup) delete popup.dataset.dateTransition;
  }
  function replaceView(stage, nextView, previousMode, previousMonth) {
    finishViewTransition();
    const previous=stage.querySelector('.date-picker-view');
    const mode=chooser?'months':'days', month=viewMonth.year*12+viewMonth.month;
    const changed=Boolean(previous&&(previousMode!==mode||previousMonth!==month));
    const animate=changed&&!documentRoot.hidden&&!reducedMotion?.matches&&!documentRoot.body.classList.contains('motion-paused');
    const ghost=animate?previous.cloneNode(true):null;
    stage.replaceChildren(nextView);
    if (!ghost) return;
    // The outgoing layer is only a visual snapshot. Remove its interaction
    // hooks as well as focusability so selectors and keyboard navigation always
    // address the new, live view, including during rapid repeated switches.
    ghost.classList.remove('is-entering');ghost.classList.add('is-leaving');
    ghost.setAttribute('aria-hidden','true');ghost.setAttribute('inert','');
    delete ghost.dataset.dateView;
    ghost.querySelectorAll('[id]').forEach(node=>node.removeAttribute('id'));
    ghost.querySelectorAll('button,input,[tabindex]').forEach(node=>node.tabIndex=-1);
    ghost.querySelectorAll('[data-date-day],[data-date-month],[data-date-year],[data-date-action]').forEach(node=>{
      for(const name of ['data-date-day','data-date-month','data-date-year','data-date-action'])node.removeAttribute(name);
    });
    const switchMode=previousMode!==mode, direction=month>=previousMonth?1:-1;
    const x=switchMode?0:direction*6, y=switchMode?(chooser?4:-4):0;
    nextView.style.setProperty('--date-view-enter-x',`${x}px`);
    nextView.style.setProperty('--date-view-enter-y',`${y}px`);
    nextView.style.setProperty('--date-view-enter-scale',switchMode?'.99':'1');
    ghost.style.setProperty('--date-view-exit-x',`${-x}px`);
    ghost.style.setProperty('--date-view-exit-y',`${-y}px`);
    nextView.classList.add('is-entering');stage.append(ghost);
    popup.dataset.dateTransition=switchMode?(chooser?'to-months':'to-days'):`${chooser?'year':'month'}-${direction>0?'next':'previous'}`;
    transitionView=nextView;transitionGhost=ghost;
    transitionEnd=event=>{if(event.target===nextView&&event.animationName==='date-view-enter')finishViewTransition();};
    nextView.addEventListener('animationend',transitionEnd);
    // Reduced-motion or an app-wide motion change can suppress animationend.
    // The static CSS state is already visible; this only retires the snapshot.
    transitionTimer=win.setTimeout(finishViewTransition,260);
  }

  function close({ restoreFocus = false } = {}) {
    if (!current) return;
    const previous=current;
    finishViewTransition();
    previous.button.setAttribute('aria-expanded','false');
    if (positionFrame) win.cancelAnimationFrame(positionFrame);
    positionFrame=0;
    if (popup) {
      if (popoverUsed) { try { popup.hidePopover(); } catch { /* Already closed by browser. */ } }
      const host=popup.closest('dialog');host?.classList.remove('date-picker-host');
      popup.remove();
    }
    popup=null;current=null;selectedDay=null;viewMonth=null;chooser=false;popoverUsed=false;
    if (restoreFocus && previous.input.isConnected && !previous.input.disabled) previous.input.focus({preventScroll:true});
  }

  function position() {
    if (!popup || !current || !current.input.isConnected) return close();
    const rect=current.wrapper.getBoundingClientRect();
    const viewport=win.visualViewport;
    const leftEdge=viewport?.offsetLeft || 0, topEdge=viewport?.offsetTop || 0;
    const screenWidth=viewport?.width || win.innerWidth, screenHeight=viewport?.height || win.innerHeight;
    popup.style.maxHeight=`${Math.max(180,screenHeight-24)}px`;
    const popupRect=popup.getBoundingClientRect(), gap=8;
    const width=popupRect.width, height=Math.min(popupRect.height,screenHeight-24);
    const left=Math.max(leftEdge+12,Math.min(rect.left,leftEdge+screenWidth-width-12));
    let top=rect.bottom+gap;
    if (top+height>topEdge+screenHeight-12) top=rect.top-height-gap;
    top=Math.max(topEdge+12,Math.min(top,topEdge+screenHeight-height-12));
    popup.style.left=`${Math.round(left)}px`;popup.style.top=`${Math.round(top)}px`;
  }
  function schedulePosition() {
    if (!popup || positionFrame) return;
    positionFrame=win.requestAnimationFrame(()=>{positionFrame=0;position();});
  }
  function moveView(amount) {
    const next=addMonths(selectedDay || viewMonth,amount);
    if (!next) return;
    const candidate=clamp(next,bounds(current.input));
    selectedDay=candidate;viewMonth={...candidate,day:1};render(true);
  }
  function monthAllowed(year, month, range) {
    return (!range.min || isoDate(year,month,daysInMonth(year,month))>=range.min) && (!range.max || isoDate(year,month,1)<=range.max);
  }

  function render(focusDay = false) {
    if (!popup || !current) return;
    const range=bounds(current.input), value=current.input.value;
    const previousMode=popup.dataset.dateView || '', previousMonth=Number(popup.dataset.datePeriod || 0);
    const heading=`${viewMonth.year} 年 ${viewMonth.month} 月`, todayDate=today();
    const prev=addMonths(viewMonth,-1), next=addMonths(viewMonth,1);
    const previousDisabled=!prev || !monthAllowed(prev.year,prev.month,range), nextDisabled=!next || !monthAllowed(next.year,next.month,range);
    const rows=buildMonth(viewMonth.year,viewMonth.month,range.min,range.max);
    let content='';
    if (chooser) {
      const minYear=parseISODate(range.min)?.year || 1, maxYear=parseISODate(range.max)?.year || 9999;
      content=`<div class="date-year-row"><button type="button" data-date-action="previous-year" aria-label="上一年" ${viewMonth.year<=minYear?'disabled':''}>−</button><label>年份<input type="text" inputmode="numeric" maxlength="4" data-date-year value="${viewMonth.year}" aria-label="跳转年份"></label><button type="button" data-date-action="apply-year" aria-label="确认年份">↵</button><button type="button" data-date-action="next-year" aria-label="下一年" ${viewMonth.year>=maxYear?'disabled':''}>＋</button></div><div class="date-month-grid" role="group" aria-label="选择月份">${MONTHS.map((name,index)=>`<button type="button" data-date-month="${index+1}" ${monthAllowed(viewMonth.year,index+1,range)?'':'disabled'} class="${index+1===viewMonth.month?'is-current':''}" aria-label="${viewMonth.year}年${index+1}月">${name}<small>${pad(index+1)}</small></button>`).join('')}</div><p class="date-picker-help">输入年份，快速跳转历史区间。</p>`;
    } else {
      content=`<div class="date-calendar-grid" role="grid" aria-label="${heading}"><div class="date-calendar-row date-weekdays" role="row">${WEEKDAYS.map(day=>`<span role="columnheader">${day}</span>`).join('')}</div>${Array.from({length:6},(_,week)=>`<div class="date-calendar-row" role="row">${rows.slice(week*7,week*7+7).map(day=>`<button type="button" role="gridcell" data-date-day="${day.iso}" class="date-day${day.outside?' is-outside':''}${day.iso===value?' is-selected':''}${day.iso===todayDate.iso?' is-today':''}" tabindex="${!day.disabled&&day.iso===selectedDay.iso?'0':'-1'}" aria-selected="${day.iso===value}" ${day.iso===todayDate.iso?'aria-current="date"':''} aria-label="${day.iso || '范围外日期'}" ${day.disabled?'disabled':''}>${day.day}</button>`).join('')}</div>`).join('')}</div><p class="date-picker-help">方向键选择 · Enter 确认 · Esc 关闭</p>`;
    }
    if (!popup.querySelector('[data-date-stage]')) {
      popup.innerHTML=`<div class="date-picker-kicker"><span></span><button type="button" data-date-action="close" aria-label="关闭日历">×</button></div><div class="date-picker-heading"><button type="button" data-date-action="previous" aria-label="上个月">‹</button><button type="button" class="date-month-title" data-date-action="choose"><span id="${current.input.dataset.dateInput}-heading" aria-live="polite"></span><small><span class="date-mode-label"></span><span class="date-view-chevron" aria-hidden="true">▾</span></small></button><button type="button" data-date-action="next" aria-label="下个月">›</button></div><div class="date-picker-stage" data-date-stage></div><div class="date-picker-footer"><span data-date-range></span><div><button type="button" data-date-action="today">今天</button></div></div>`;
    }
    popup.querySelector('.date-picker-kicker>span').textContent=`DATE / ${label(current.input)}`;
    popup.querySelector('.date-month-title>span').textContent=heading;
    popup.querySelector('.date-mode-label').textContent=chooser?'返回日历':'选择年月';
    const chooseButton=popup.querySelector('[data-date-action="choose"]');
    chooseButton.setAttribute('aria-label',chooser?'返回日期选择':'选择年月');
    chooseButton.setAttribute('aria-expanded',String(chooser));
    popup.querySelector('[data-date-action="previous"]').disabled=previousDisabled||chooser;
    popup.querySelector('[data-date-action="next"]').disabled=nextDisabled||chooser;
    popup.querySelector('[data-date-range]').textContent=range.min||range.max?`${range.min||'不限'} — ${range.max||'不限'}`:'YYYY / MM / DD';
    popup.querySelector('[data-date-action="today"]').disabled=!allowed(todayDate,range);
    const clearButton=popup.querySelector('[data-date-action="clear"]');
    if(current.input.required)clearButton?.remove();
    else if(!clearButton){const clear=documentRoot.createElement('button');clear.type='button';clear.dataset.dateAction='clear';clear.textContent='清除';popup.querySelector('.date-picker-footer>div').append(clear);}
    const nextView=documentRoot.createElement('div');nextView.className='date-picker-view';nextView.dataset.dateView=chooser?'months':'days';nextView.innerHTML=content;
    replaceView(popup.querySelector('[data-date-stage]'),nextView,previousMode,previousMonth);
    popup.dataset.dateView=chooser?'months':'days';popup.dataset.datePeriod=String(viewMonth.year*12+viewMonth.month);
    position();
    if (focusDay) {
      const target=chooser ? popup.querySelector(`button[data-date-month="${viewMonth.month}"]:not(:disabled)`) || popup.querySelector('[data-date-month]:not(:disabled)') : popup.querySelector('[data-date-day][tabindex="0"]');
      (target || popup.querySelector('[data-date-action="choose"]')).focus({preventScroll:true});
    }
  }

  function open(record) {
    if (destroyed || record.input.disabled || record.input.readOnly) return;
    if (current===record) return close({restoreFocus:true});
    close();current=record;
    const range=bounds(record.input);
    selectedDay=clamp(parseISODate(record.input.value)||today(),range);viewMonth={...selectedDay,day:1};
    popup=documentRoot.createElement('div');popup.className='date-popover';popup.id=`${record.input.dataset.dateInput}-popup`;
    popup.setAttribute('role','dialog');popup.setAttribute('aria-modal','false');popup.setAttribute('aria-labelledby',`${record.input.dataset.dateInput}-heading`);
    const host=record.input.closest('dialog[open]') || documentRoot.body;
    host.append(popup);
    if (host.tagName==='DIALOG') host.classList.add('date-picker-host');
    if (typeof popup.showPopover==='function') {
      try { popup.setAttribute('popover','manual');popup.showPopover();popoverUsed=true; } catch {popup.removeAttribute('popover');}
    }
    record.button.setAttribute('aria-expanded','true');render(true);
  }
  function commit(iso) {
    if (!current || (iso && !allowed(parseISODate(iso),bounds(current.input)))) return;
    const record=current;record.input.value=iso;validate(record);close({restoreFocus:true});
    record.input.dispatchEvent(new Event('input',{bubbles:true}));
    record.input.dispatchEvent(new Event('change',{bubbles:true}));
  }
  function applyYear(offset=0) {
    const range=bounds(current.input), input=popup.querySelector('[data-date-year]');
    const entered=input?.value || String(viewMonth.year);
    if (!/^\d{1,4}$/.test(entered)) { input?.setCustomValidity('请输入 1 到 9999 之间的年份。');input?.reportValidity();return; }
    const minYear=parseISODate(range.min)?.year || 1,maxYear=parseISODate(range.max)?.year || 9999;
    const year=Math.max(minYear,Math.min(maxYear,Number(entered)+offset));
    viewMonth={year,month:viewMonth.month,day:1};render();popup.querySelector('[data-date-year]')?.focus({preventScroll:true});
  }

  function refresh() {
    if (destroyed) return;
    for (const [input,record] of records) {
      if (!input.isConnected) { if (current===record) close(); records.delete(input); }
      else validate(record);
    }
    root.querySelectorAll('input[type="date"]:not([data-date-input])').forEach(enhance);
    if (current) {selectedDay=clamp(selectedDay,bounds(current.input));viewMonth={...selectedDay,day:1};render();}
  }
  documentRoot.addEventListener('click',event=>{
    const toggle=event.target.closest?.('[data-date-toggle]');
    if (toggle) {const record=buttonRecord(toggle);if(record){event.preventDefault();open(record);}return;}
    if (!popup || !popup.contains(event.target)) return;
    const day=event.target.closest('[data-date-day]');if(day&&!day.disabled)return commit(day.dataset.dateDay);
    const month=event.target.closest('button[data-date-month]');
    if(month&&!month.disabled){const date=parseISODate(isoDate(viewMonth.year,Number(month.dataset.dateMonth),Math.min(selectedDay.day,daysInMonth(viewMonth.year,Number(month.dataset.dateMonth)))));selectedDay=clamp(date,bounds(current.input));viewMonth={...selectedDay,day:1};chooser=false;return render(true);}
    const action=event.target.closest('[data-date-action]')?.dataset.dateAction;
    if(action==='previous')moveView(-1);else if(action==='next')moveView(1);
    else if(action==='choose'){chooser=!chooser;render(true);}
    else if(action==='today')commit(today().iso);else if(action==='clear')commit('');else if(action==='close')close({restoreFocus:true});
    else if(action==='apply-year')applyYear();else if(action==='previous-year')applyYear(-1);else if(action==='next-year')applyYear(1);
  },{signal:events.signal});
  root.addEventListener('input',event=>{const record=fieldRecord(event.target);if(record)validate(record);},{signal:events.signal});
  root.addEventListener('change',event=>{const record=fieldRecord(event.target);if(record)validate(record,true);},{signal:events.signal});
  root.addEventListener('focusout',event=>{const record=fieldRecord(event.target);if(record)validate(record,true);},{signal:events.signal});
  root.addEventListener('invalid',event=>{const record=fieldRecord(event.target);if(record)validate(record,true);},{capture:true,signal:events.signal});
  documentRoot.addEventListener('pointerdown',event=>{if(popup&&!popup.contains(event.target)&&!current.wrapper.contains(event.target))close();},{capture:true,signal:events.signal});
  documentRoot.addEventListener('focusin',event=>{if(popup&&!popup.contains(event.target)&&!current.wrapper.contains(event.target))close();},{signal:events.signal});
  documentRoot.addEventListener('keydown',event=>{
    const record=fieldRecord(event.target);
    if(record&&(event.key==='ArrowDown'||event.key==='F4')&&!popup){event.preventDefault();open(record);return;}
    if(!popup)return;
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();return close({restoreFocus:true});}
    if(!popup.contains(event.target))return;
    event.stopPropagation();
    if(event.target.matches('[data-date-year]')&&event.key==='Enter'){event.preventDefault();return applyYear();}
    const activeDay=event.target.closest('[data-date-day]');
    if(activeDay){
      let next=null;
      if(event.key==='ArrowLeft')next=addDays(selectedDay,-1);else if(event.key==='ArrowRight')next=addDays(selectedDay,1);
      else if(event.key==='ArrowUp')next=addDays(selectedDay,-7);else if(event.key==='ArrowDown')next=addDays(selectedDay,7);
      else if(event.key==='Home')next=addDays(selectedDay,-weekday(selectedDay));else if(event.key==='End')next=addDays(selectedDay,6-weekday(selectedDay));
      else if(event.key==='PageUp')next=addMonths(selectedDay,event.shiftKey?-12:-1);else if(event.key==='PageDown')next=addMonths(selectedDay,event.shiftKey?12:1);
      else if(event.key==='Enter'||event.key===' '){event.preventDefault();return commit(activeDay.dataset.dateDay);}
      else return;
      event.preventDefault();if(next){selectedDay=clamp(next,bounds(current.input));viewMonth={...selectedDay,day:1};render(true);}
    } else if(event.target.matches('[data-date-month]')) {
      const buttons=[...popup.querySelectorAll('[data-date-month]:not(:disabled)')],index=buttons.indexOf(event.target);
      const amount={ArrowLeft:-1,ArrowRight:1,ArrowUp:-3,ArrowDown:3}[event.key];
      if(amount||event.key==='Home'||event.key==='End'){event.preventDefault();buttons[Math.max(0,Math.min(buttons.length-1,event.key==='Home'?0:event.key==='End'?buttons.length-1:index+amount))]?.focus();}
    }
  },{capture:true,signal:events.signal});
  documentRoot.addEventListener('close',event=>{if(popup&&event.target.contains?.(popup))close();},{capture:true,signal:events.signal});
  documentRoot.addEventListener('visibilitychange',()=>{if(documentRoot.hidden)close();},{signal:events.signal});
  win.addEventListener('resize',schedulePosition,{signal:events.signal});
  documentRoot.addEventListener('scroll',schedulePosition,{capture:true,passive:true,signal:events.signal});
  win.visualViewport?.addEventListener('resize',schedulePosition,{signal:events.signal});
  win.visualViewport?.addEventListener('scroll',schedulePosition,{signal:events.signal});
  refresh();
  return { refresh, close, syncConstraints:refresh, destroy() {
    if(destroyed)return;close();destroyed=true;events.abort();
    for(const record of records.values()) {
      const {input,wrapper,feedback,original}=record;
      if(!input.isConnected)continue;
      input.setCustomValidity('');input.removeAttribute('aria-invalid');delete input.dataset.dateInput;
      for(const [name,value] of Object.entries(original))if(name!=='type'){const attribute=name==='describedby'?'aria-describedby':name;value===null?input.removeAttribute(attribute):input.setAttribute(attribute,value);}
      input.type=original.type;wrapper.before(input);wrapper.remove();feedback.remove();
    }
    records.clear();
  } };
}
