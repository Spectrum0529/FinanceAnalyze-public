export function visualParts({$,ribbon,motionDirector,state,safePrefs,persistPrefs}) { let audioControls=null;const chartReducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
function updateAudioUI(snapshot) {
  const dock = $('.audio-dock');
  if (!dock) return;
  dock.dataset.audioStatus = snapshot.status;
  dock.dataset.playbackMode = snapshot.playbackMode;
  dock.dataset.contextState = snapshot.contextState;
  document.body.dataset.musicPlaying = String(snapshot.playing);
  const toggle = $('[data-audio-toggle]', dock);
  toggle.setAttribute('aria-pressed', String(snapshot.enabled));
  toggle.setAttribute('aria-label', snapshot.enabled ? '关闭背景音乐' : '开启背景音乐');
  $('[data-audio-title]', dock).textContent = snapshot.playing ? '正在播放' : snapshot.enabled ? '已暂停' : '音乐已关闭';
  const track = snapshot.currentTrack;
  const changedTrack = Boolean(track && track.id !== dock.dataset.trackId);
  const audioShortLabel = snapshot.playing && track ? track.title : snapshot.statusLabel;
  const shortLabel = $('[data-audio-label]', dock);
  if (shortLabel.textContent !== audioShortLabel) shortLabel.textContent = audioShortLabel;
  if (track) {
    dock.dataset.trackId = track.id;
    $('[data-audio-track-name]', dock).textContent = track.title;
    $('[data-audio-track-description]', dock).textContent = `${track.subtitle} · ${track.bpm} BPM`;
    $('[data-audio-number]', dock).textContent = `${String(snapshot.trackIndex + 1).padStart(2,'0')} / ${String(snapshot.playlist.length).padStart(2,'0')}`;
  }
  const playlist = $('[data-audio-playlist]', dock);
  playlist.setAttribute('aria-label', `${snapshot.playlist?.length || 0}首原创音乐`);
  if (snapshot.playlist?.length && !playlist.children.length) {
    snapshot.playlist.forEach((item, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'audio-track';
      button.dataset.audioTrack = item.id;
      const number = document.createElement('span');
      number.className = 'audio-track-number';
      number.textContent = String(index + 1).padStart(2,'0');
      const copy = document.createElement('span');
      const title = document.createElement('strong');
      title.textContent = item.title;
      const subtitle = document.createElement('small');
      subtitle.textContent = `${item.subtitle} · ${item.bpm} BPM`;
      copy.append(title, subtitle);
      const marker = document.createElement('span');
      marker.className = 'audio-track-marker';
      marker.setAttribute('aria-hidden', 'true');
      marker.textContent = '↗';
      button.append(number, copy, marker);
      playlist.append(button);
    });
  }
  const playlistState = `${track?.id || ''}/${snapshot.playing}`;
  if (playlist.dataset.lastState !== playlistState) {
    playlist.dataset.lastState = playlistState;
    playlist.querySelectorAll('[data-audio-track]').forEach(button => {
      const selected = button.dataset.audioTrack === track?.id;
      button.classList.toggle('is-current', selected);
      button.setAttribute('aria-pressed', String(selected));
      $('.audio-track-marker', button).textContent = selected ? (snapshot.playing ? 'Ⅱ' : '•') : '↗';
    });
  }
  if (changedTrack && !$('#audio-panel').hidden) {
    playlist.querySelector('.is-current')?.scrollIntoView({block:'nearest',behavior:state.motion && !chartReducedMotion.matches ? 'smooth' : 'instant'});
  }
  const formatTime = value => `${Math.floor(Math.max(0, Number(value) || 0) / 60)}:${String(Math.floor(Math.max(0, Number(value) || 0) % 60)).padStart(2,'0')}`;
  const progress = Math.max(0, Math.min(1, snapshot.progress || 0));
  $('[data-audio-progress]', dock).style.transform = `scaleX(${progress})`;
  $('[data-audio-mini-progress]', dock).style.transform = `scaleX(${progress})`;
  $('.audio-progress', dock).setAttribute('aria-valuenow', String(Math.round(progress * 100)));
  $('[data-audio-elapsed]', dock).textContent = formatTime(snapshot.elapsed);
  $('[data-audio-duration]', dock).textContent = formatTime(snapshot.duration);
  $('[data-audio-section]', dock).textContent = snapshot.playing ? snapshot.section : snapshot.enabled ? '播放已暂停' : '等待播放';
  const play = $('[data-audio-play]', dock);
  play.textContent = snapshot.enabled ? '暂停音乐' : '播放音乐';
  play.setAttribute('aria-label', snapshot.enabled ? '暂停音乐' : '播放音乐');
  audioControls?.update(snapshot);
  const volume = Math.round(snapshot.volume * 100);
  $('[data-audio-volume]', dock).value = String(volume);
  $('[data-audio-volume-label]', dock).textContent = `${volume}%`;
  if (!snapshot.playing) {
    ribbon.setAudioLevel?.(0);
    delete document.body.dataset.beat;
  }
}

const paths = {
  arrow:'<path d="M4 10h12m-5-5 5 5-5 5"/>', diagonal:'<path d="M5 15 15 5M5 5h10v10"/>',
  grid:'<rect x="3" y="3" width="5" height="5"/><rect x="12" y="3" width="5" height="5"/><rect x="3" y="12" width="5" height="5"/><rect x="12" y="12" width="5" height="5"/>',
  settings:'<path d="M3 5h14M3 10h14M3 15h14"/><rect x="6" y="3" width="3" height="4" fill="var(--surface)"/><rect x="12" y="8" width="3" height="4" fill="var(--surface)"/><rect x="5" y="13" width="3" height="4" fill="var(--surface)"/>',
  logout:'<path d="M9 4H4v12h5M8 10h9m-4-4 4 4-4 4"/>',close:'<path d="m5 5 10 10M5 15 15 5"/>',
  search:'<circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/>',plus:'<path d="M10 3v14M3 10h14"/>',
  chart:'<path d="M3 3v14h14M6 12l4-5 3 3 4-6"/>',database:'<ellipse cx="10" cy="5" rx="7" ry="3"/><path d="M3 5v10c0 1.7 3.1 3 7 3s7-1.3 7-3V5M3 10c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  orbit:'<circle cx="10" cy="10" r="3"/><ellipse cx="10" cy="10" rx="9" ry="5" transform="rotate(-35 10 10)"/><path d="m5 2 1 2m9 12 1 2"/>',
  report:'<path d="M5 2h7l4 4v12H5zM12 2v5h4M8 10h5M8 13h5"/>',download:'<path d="M10 2v11m-4-4 4 4 4-4M3 13v5h14v-5"/>',
  shield:'<path d="m10 2 7 3v5c0 4-7 8-7 8s-7-4-7-8V5zM6 9l3 3 5-5"/>',clock:'<circle cx="10" cy="10" r="8"/><path d="M10 5v5l3 2"/>',
  layers:'<path d="m10 2 8 4-8 4-8-4zM2 10l8 4 8-4M2 14l8 4 8-4"/>',link:'<path d="m8 12 4-4M7 13l-1 1a3.5 3.5 0 0 1-5-5l4-4a3.5 3.5 0 0 1 5 0m3 2 1-1a3.5 3.5 0 0 1 5 5l-4 4a3.5 3.5 0 0 1-5 0" transform="translate(0 0) scale(.95)"/>',
  check:'<path d="m4 10 4 4 8-9"/>',trash:'<path d="M3 5h14M7 5V2h6v3M5 5l1 13h8l1-13M8 8v6M12 8v6"/>',info:'<circle cx="10" cy="10" r="8"/><path d="M10 9v6M10 5v1"/>',
};
const icon = (name, extra='') => `<svg class="icon ${extra}" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">${paths[name] || paths.grid}</svg>`;
const glyph = letter => {
  const forms = {
    A:'<path d="M10 80V15m0 65h80M20 67l17-22 15 8 20-29 18 6M20 80V67m17 13V45m15 35V53m20 27V24m18 56V30"/><path d="M10 60h80M10 40h80M10 20h80" opacity=".25"/><circle cx="72" cy="24" r="3" fill="currentColor"/>',
    B:'<circle cx="50" cy="50" r="32"/><circle cx="50" cy="50" r="21"/><circle cx="50" cy="50" r="9"/><ellipse cx="50" cy="50" rx="47" ry="17" transform="rotate(-40 50 50)"/><path d="M50 8v15m0 54v15M8 50h15m54 0h15"/><circle cx="75" cy="29" r="3" fill="currentColor"/>',
    C:'<path d="m50 10 40 21-40 21L10 31zM10 45l40 21 40-21M10 59l40 21 40-21M10 73l40 21 40-21M50 10v42M30 21l40 21M70 21 30 42"/><path d="M10 31v42M90 31v42M50 52v42" opacity=".3"/>',
    D:'<path d="M20 10h45l17 17v63H20zM65 10v20h17M30 42h40M30 48h24M30 76h43"/><path d="M34 72V61h7v11M47 72V54h7v18M60 72V58h7v14"/><path d="M14 16H9v80h66v-4" opacity=".4"/>',
  };
  return `<svg class="module-glyph" viewBox="0 0 100 100" fill="none" stroke="currentColor" stroke-width=".8" aria-hidden="true">${forms[letter]}</svg>`;
};
const clockFormat = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Hong_Kong',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const terminalDateFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Hong_Kong',weekday:'short',day:'2-digit',month:'short',year:'numeric'});
const shortDateFormat = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'});
let cachedClockDate = '';
const chartAssetFormatter = new Intl.NumberFormat('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
function updateClock(){
  if (document.hidden) return;
  const now = new Date();
  const [hour, minute, seconds] = clockFormat.format(now).split(':');
  const hourAndMinute = `${hour}:${minute}`;
  const timestamp = now.toISOString();
  for (const clock of document.querySelectorAll('#clock, [data-auth-clock]')) {
    if (clock.firstChild.nodeValue !== hourAndMinute) clock.firstChild.nodeValue = hourAndMinute;
    if (clock.lastElementChild.textContent !== seconds) clock.lastElementChild.firstChild.nodeValue = seconds;
    clock.dateTime = timestamp;
  }
  const authDate = $('[data-auth-date]');
  if (authDate) {
    const label = terminalDateFormat.format(now) + ' · 香港时间';
    if (authDate.textContent !== label) authDate.textContent = label;
  }
  const date = shortDateFormat.format(now);
  if (date !== cachedClockDate) {cachedClockDate = date; $('#clock-date').textContent = `${date} · 香港时间`;}

}
return {updateAudioUI,icon,glyph,updateClock,setControls(value){audioControls=value;}};
}
