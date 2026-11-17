/**
 * FIELD RECORDINGS / 08 — eight original, locally synthesised compositions.
 * Scores, instruments, percussion and room impulse are generated in the browser.
 * No files, samples, services, microphone or network access. Audio stays silent
 * until enable() is called from an explicit user gesture.
 */
const SCORES = [
  {
    id: 'glass-transit', title: 'GLASS TRANSIT', subtitle: '玻璃回廊 · 清透电子', bpm: 96, bars: 40, character: 'glass', swing: 0,
    chords: [
      [38,[50,53,57,64]],[34,[50,53,57,60]],[41,[53,57,60,67]],[36,[52,55,60,62]],
      [31,[50,53,58,62]],[34,[50,53,57,60]],[38,[50,53,57,64]],[33,[48,52,55,59]],
    ],
    motifs: [
      [[0,2,3,1],[4,3,2,.8],[7,2,1,.7],[8,1,4,.9],[14,0,2,.7]],
      [[0,1,6,.9],[7,2,1,.65],[8,3,4,1],[13,2,3,.75]],
      [[0,3,3,1],[4,2,2,.8],[6,1,2,.7],[9,2,3,.85],[13,0,3,.8]],
      [[0,1,4,.9],[5,2,3,.75],[8,0,7,1]],
    ],
  },
  {
    id: 'velvet-circuit', title: 'VELVET CIRCUIT', subtitle: '丝绒电路 · 慵懒律动', bpm: 84, bars: 40, character: 'velvet', swing: .14,
    chords: [
      [33,[48,52,55,59]],[36,[48,52,55,59]],[29,[48,52,57,60]],[28,[47,50,55,59]],
      [38,[50,53,57,64]],[29,[48,52,57,60]],[33,[48,52,55,59]],[28,[47,50,55,59]],
    ],
    motifs: [
      [[1,1,3,.9],[5,2,2,.7],[8,3,5,1],[14,2,2,.65]],
      [[0,1,5,.85],[7,0,2,.7],[10,2,4,.95]],
      [[0,3,4,1],[6,2,2,.7],[9,1,3,.9],[13,0,3,.7]],
      [[1,1,3,.85],[5,2,3,.75],[10,0,6,.95]],
    ],
  },
  {
    id: 'prism-current', title: 'PRISM CURRENT', subtitle: '棱镜脉冲 · 明亮节拍', bpm: 112, bars: 40, character: 'prism', swing: 0,
    chords: [
      [40,[52,55,59,66]],[36,[52,55,59,62]],[31,[50,54,59,62]],[38,[50,54,57,64]],
      [33,[52,55,60,64]],[36,[52,55,59,62]],[40,[52,55,59,66]],[35,[50,54,57,62]],
    ],
    motifs: [
      [[0,2,2,1],[3,2,1,.7],[4,3,3,.95],[8,1,2,.85],[11,2,1,.7],[12,0,3,.9]],
      [[0,1,3,.9],[4,2,2,.85],[7,3,1,.7],[8,2,4,1],[14,1,2,.75]],
      [[0,3,3,1],[4,2,3,.9],[8,1,2,.8],[11,0,1,.7],[12,1,4,.95]],
      [[0,2,4,1],[5,1,2,.8],[8,0,6,.95],[15,1,1,.65]],
    ],
  },
  {
    id: 'blue-hour', title: 'BLUE HOUR', subtitle: '蓝色时刻 · 夜色钢琴', bpm: 72, bars: 32, character: 'blue', swing: 0,
    chords: [
      [35,[50,54,57,61]],[31,[50,54,59,62]],[38,[50,54,57,61]],[33,[49,52,57,59]],
      [28,[50,54,55,59]],[31,[50,54,59,62]],[35,[50,54,57,61]],[30,[49,52,57,61]],
    ],
    motifs: [
      [[0,2,5,1],[7,3,3,.8],[12,1,4,.9]],
      [[0,0,7,.9],[8,1,3,.75],[12,2,4,.85]],
      [[0,3,6,1],[8,2,4,.85],[13,1,3,.7]],
      [[0,1,5,.9],[7,0,8,.85]],
    ],
  },
  {
    id: 'paper-orbit', title: 'PAPER ORBIT', subtitle: '纸上轨道 · 极简木音', bpm: 90, bars: 40, character: 'paper', swing: 0,
    chords: [
      [36,[48,55,59,62]],[40,[52,55,59,62]],[33,[48,52,57,60]],[29,[48,53,57,64]],
      [38,[50,53,57,60]],[43,[50,55,59,64]],[36,[48,52,55,62]],[31,[50,55,57,59]],
    ],
    motifs: [
      [[0,2,2,.9],[3,3,2,.65],[8,1,3,1],[13,0,2,.7]],
      [[2,1,3,.8],[7,3,2,.95],[10,2,3,.7]],
      [[0,0,4,.8],[6,2,2,.75],[11,3,3,1]],
      [[1,2,3,.9],[5,1,2,.7],[10,0,5,.85]],
    ],
    voice: { type:'mallet', octave:12, level:.88, tail:.20, attack:.004, cutoff:4200, sustain:.035, overtone:.10 },
    room: { echo:.13, reverb:.16 }, padCutoff:1350, padLevel:.82,
    rhythm: {
      arp:[0,5,8,13], degrees:[0,2,3,1], arpType:'mallet', arpOctave:12, arpDuration:.5, arpLevel:.024,
      bass:[0,8], bassDuration:.65, bassLevel:.115, bassCutoff:640,
      kicks:[0,10], snares:[6,14], hats:[2,6,10,14], ghosts:[], soft:true, drumLevel:.64,
      wood:[3,11], woodNote:77,
    },
  },
  {
    id: 'amber-signal', title: 'AMBER SIGNAL', subtitle: '琥珀信号 · 暖调碎拍', bpm: 102, bars: 40, character: 'amber', swing: .12,
    chords: [
      [34,[46,53,56,60]],[37,[49,53,56,60]],[32,[48,51,56,60]],[39,[51,54,58,61]],
      [30,[46,49,53,60]],[37,[49,53,56,63]],[34,[46,50,53,60]],[41,[48,53,57,63]],
    ],
    motifs: [
      [[1,1,3,.85],[6,3,2,.75],[9,2,4,1],[15,1,1,.6]],
      [[0,2,5,.95],[7,1,2,.7],[11,0,3,.85]],
      [[2,3,3,1],[6,2,2,.7],[10,1,5,.9]],
      [[1,0,4,.85],[8,2,3,.9],[13,1,3,.7]],
    ],
    voice: { type:'reed', octave:12, level:.77, tail:.24, attack:.028, cutoff:2500, sustain:.28, detuneLayer:.13 },
    room: { echo:.09, reverb:.13 }, padCutoff:1150, padLevel:.90,
    rhythm: {
      arp:[2,7,10,15], degrees:[2,1,3,0], arpType:'keys', arpOctave:0, arpDuration:.32, arpLevel:.026,
      bass:[0,6,10,14], bassDuration:.48, bassLevel:.13, bassCutoff:760,
      kicks:[0,6,11], snares:[4,12], hats:[0,2,4,6,8,10,12,14], ghosts:[3,7,15], soft:true, drumLevel:.88,
      stabs:[2,10], stabType:'felt',
    },
  },
  {
    id: 'quiet-proof', title: 'QUIET PROOF', subtitle: '静默证明 · 空间氛围', bpm: 66, bars: 32, character: 'quiet', swing: 0,
    chords: [
      [29,[48,52,57,64]],[36,[48,55,59,62]],[33,[48,52,59,64]],[31,[50,55,57,62]],
      [38,[50,53,60,64]],[34,[50,53,57,65]],[29,[48,52,57,64]],[36,[50,55,59,64]],
    ],
    motifs: [
      [[0,3,7,.9],[10,1,6,.8]],
      [[3,2,8,1]],
      [[0,1,6,.85],[9,0,7,.95]],
      [[2,2,6,.9],[11,3,5,.7]],
    ],
    voice: { type:'felt', octave:12, level:.92, tail:1.15, attack:.028, cutoff:2900, sustain:.08, overtone:.06 },
    room: { echo:.21, reverb:.34 }, padCutoff:950, padLevel:1.18,
    rhythm: {
      arp:[0,8], degrees:[0,3,1,2], arpType:'sine', arpOctave:12, arpDuration:1.65, arpLevel:.020,
      bass:[0], bassDuration:2.3, bassLevel:.10, bassCutoff:470,
      kicks:[0], snares:[], hats:[10], ghosts:[], soft:true, drumLevel:.32, sparse:true,
    },
  },
  {
    id: 'afterimage', title: 'AFTERIMAGE', subtitle: '残像采样 · 轻盈律动', bpm: 120, bars: 48, character: 'after', swing: .10,
    chords: [
      [37,[49,52,56,63]],[33,[49,52,57,61]],[40,[52,56,59,66]],[35,[51,54,59,63]],
      [30,[49,54,57,61]],[33,[49,52,56,61]],[37,[49,52,59,63]],[32,[48,51,56,63]],
    ],
    motifs: [
      [[0,2,2,.9],[3,3,1,.7],[6,1,3,.9],[11,2,2,.8],[14,0,2,.75]],
      [[1,1,3,.8],[5,2,2,.95],[8,3,3,1],[13,1,2,.75]],
      [[0,3,3,1],[4,2,2,.8],[9,0,3,.9],[15,1,1,.65]],
      [[2,1,2,.85],[6,0,3,.8],[10,2,2,.95],[13,3,3,.7]],
    ],
    voice: { type:'pluck', octave:12, level:.78, tail:.16, attack:.003, cutoff:5200, sustain:.04, overtone:.07 },
    room: { echo:.18, reverb:.19 }, padCutoff:1850, padLevel:.78,
    rhythm: {
      arp:[0,3,6,8,11,14], degrees:[0,2,1,3,2,1], arpType:'pluck', arpOctave:12, arpDuration:.29, arpLevel:.023,
      bass:[0,5,8,11,14], bassDuration:.36, bassLevel:.12, bassCutoff:900,
      kicks:[0,7,10], snares:[4,12], hats:[2,6,9,11,14,15], ghosts:[3,13], soft:false, drumLevel:.80,
    },
  },
];
const PLAYLIST = Object.freeze(SCORES.map((score, index) => Object.freeze({
  id: score.id, title: score.title, subtitle: score.subtitle, bpm: score.bpm,
  duration: score.bars * 4 * 60 / score.bpm, index,
})));

export function createAudioEngine({ onState = () => {}, onBeat = () => {}, volume: initialVolume = .35 } = {}) {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  const supported = typeof AudioContextClass === 'function';
  const MAX_VOICES = 104;
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const frequency = midi => 440 * 2 ** ((midi - 69) / 12);
  const modes = {
    login: { pad: 1, melody: .95, beat: .86, filter: 6900 },
    workspace: { pad: .92, melody: 1, beat: 1, filter: 7800 },
    detail: { pad: .88, melody: .81, beat: .74, filter: 5700 },
  };
  let mode = 'login', volume = clamp(Number(initialVolume) || 0, 0, 1);
  let enabled = false, destroyed = false, pageHidden = false, shuffle = true, repeatOne = false;
  let status = supported ? 'off' : 'unavailable';
  let context = null, master = null, analyser = null, analyserSamples = null;
  let music = null, effects = null, toneFilter = null, echo = null, reverb = null, noiseBuffer = null;
  let scheduler = 0, suspendTimer = 0, revision = 0, nextStepTime = 0, step = 0;
  let trackIndex = Math.floor(Math.random() * SCORES.length);
  let positionOffset = 0, positionStartedAt = 0, currentDeck = null;
  let lastSnapshotKey = '', lastNotifyTime = -Infinity, playCount = 0;
  const voices = new Set(), beatTimers = new Set(), decks = new Set(), deckTimers = new Set();
  const cueTimes = new Map(), permanentNodes = [], history = [], queue = [];
  const eventController = new AbortController();
  let waves = {};
  const isHidden = () => document.hidden || pageHidden;
  const score = () => SCORES[trackIndex];
  const stepSeconds = () => 60 / score().bpm / 4;

  function elapsed() {
    const running = enabled && context?.state === 'running' && scheduler && !isHidden();
    return clamp(positionOffset + (running ? Math.max(0, context.currentTime - positionStartedAt) : 0), 0, PLAYLIST[trackIndex].duration);
  }
  function arrangement(bar, piece = score()) {
    const tail = piece.bars - 4;
    if (bar < 4) return { name: '序章', density: .42, lead: bar >= 2, drums: bar >= 2, pad: 1 };
    if (bar >= tail) return { name: '尾声', density: clamp((piece.bars - bar) / 4, .16, .75), lead: bar < piece.bars - 2, drums: bar < piece.bars - 2, pad: .9 };
    if (piece.character === 'quiet') {
      const space = bar >= 12 && bar < 16 || bar >= 24 && bar < 28;
      return { name:space ? '留白' : bar >= 16 ? '回声' : '展开', density:space ? .30 : .70, lead:true, drums:!space && bar >= 8, pad:space ? 1.18 : 1 };
    }
    if (piece.character === 'paper') {
      const interlude = bar >= 16 && bar < 20;
      return { name:interlude ? '留白' : bar >= 28 ? '折返' : '轨道', density:interlude ? .35 : bar >= 28 ? .94 : .78, lead:true, drums:!interlude, pad:interlude ? 1.05 : .88 };
    }
    if (piece.character === 'amber') {
      const interlude = bar >= 20 && bar < 24;
      return { name:interlude ? '间奏' : bar >= 24 ? '余温' : '碎拍', density:interlude ? .38 : bar >= 24 ? 1 : .85, lead:true, drums:!interlude, pad:interlude ? 1.1 : .92 };
    }
    if (piece.character === 'after') {
      const interlude = bar >= 16 && bar < 20 || bar >= 32 && bar < 36;
      return { name:interlude ? '悬停' : bar >= 36 ? '再现' : bar >= 20 ? '变奏' : '采样', density:interlude ? .32 : bar >= 36 ? 1.03 : .88, lead:true, drums:!interlude, pad:interlude ? 1.12 : .85 };
    }
    if (piece.character === 'blue') {
      if (bar >= 16 && bar < 20) return { name: '间奏', density: .40, lead: true, drums: false, pad: 1.15 };
      return { name: bar >= 20 ? '回响' : '主旋律', density: bar >= 20 ? 1 : .84, lead: true, drums: true, pad: 1 };
    }
    if (bar >= 20 && bar < 24) return { name: '间奏', density: .38, lead: true, drums: false, pad: 1.1 };
    if (bar < 8 && piece.character !== 'velvet') return { name: '渐进', density: .68, lead: true, drums: true, pad: .95 };
    return { name: bar >= 24 ? '再现' : '主旋律', density: bar >= 24 ? 1.05 : .93, lead: true, drums: true, pad: .94 };
  }
  function getState() {
    const playing = Boolean(enabled && context?.state === 'running' && !isHidden() && scheduler);
    const track = PLAYLIST[trackIndex], current = elapsed();
    const labels = {
      off: '点击开启音乐', starting: '正在开启音乐',
      playing: volume ? `${track.title} · ${track.bpm} BPM` : '音乐已静音 · 音量 0%',
      paused: isHidden() ? '标签页已暂停音乐' : '点击继续播放',
      unavailable: '此浏览器不支持音频', error: '音乐未开启，请重试',
    };
    return Object.freeze({
      enabled, playing, volume, status, statusLabel: labels[status],
      contextState: context?.state || 'not-created', supported, mode,
      bpm: track.bpm, title: track.title, playlist: PLAYLIST, currentTrack: track,
      trackIndex, elapsed: current, duration: track.duration, progress: current / track.duration,
      section: arrangement(Math.floor(current / (60 / track.bpm * 4))).name, shuffle, repeatOne,
      playbackMode: repeatOne ? 'repeat-one' : shuffle ? 'shuffle' : 'sequence',
    });
  }
  function notify(force = false) {
    const snapshot = getState();
    const key = [snapshot.status,snapshot.enabled,snapshot.playing,snapshot.volume,snapshot.contextState,mode,isHidden(),trackIndex,shuffle,repeatOne,Math.floor(snapshot.elapsed * 4)].join('/');
    if (!force && key === lastSnapshotKey) return;
    lastSnapshotKey = key;
    lastNotifyTime = context?.currentTime || 0;
    try { onState(snapshot); } catch { /* View failures must not prevent audio cleanup. */ }
  }
  function hold(parameter, at) {
    if (typeof parameter.cancelAndHoldAtTime === 'function') parameter.cancelAndHoldAtTime(at);
    else { const value = parameter.value; parameter.cancelScheduledValues(at); parameter.setValueAtTime(value, at); }
  }
  function fadeMaster(value, seconds = .35) {
    if (!master || !context || context.state === 'closed') return;
    hold(master.gain, context.currentTime);
    master.gain.linearRampToValueAtTime(value, context.currentTime + seconds);
  }
  function keep(node) { permanentNodes.push(node); return node; }
  function makeNoise(length, decay = 0) {
    const buffer = context.createBuffer(2, length, context.sampleRate);
    let seed = 0x464104;
    for (let channel = 0; channel < 2; channel++) {
      const data = buffer.getChannelData(channel);
      for (let i = 0; i < length; i++) {
        seed = (Math.imul(seed, 1664525) + 1013904223) | 0;
        data[i] = ((seed >>> 0) / 2147483648 - 1) * (decay ? (1 - i / length) ** decay : 1);
      }
    }
    return buffer;
  }
  function buildGraph() {
    context = new AudioContextClass({ latencyHint: 'interactive' });
    master = keep(context.createGain()); master.gain.value = 0;
    const compressor = keep(context.createDynamicsCompressor());
    compressor.threshold.value = -15; compressor.knee.value = 14; compressor.ratio.value = 4;
    compressor.attack.value = .006; compressor.release.value = .23;
    compressor.connect(master);
    analyser = keep(context.createAnalyser()); analyser.fftSize = 1024;
    analyserSamples = new Float32Array(analyser.fftSize);
    master.connect(analyser); analyser.connect(context.destination);
    music = keep(context.createGain()); music.gain.value = .76;
    toneFilter = keep(context.createBiquadFilter()); toneFilter.type = 'lowpass';
    toneFilter.frequency.value = modes[mode].filter; toneFilter.Q.value = .38;
    music.connect(toneFilter); toneFilter.connect(compressor);
    effects = keep(context.createGain()); effects.gain.value = .82; effects.connect(compressor);
    echo = keep(context.createDelay(2)); echo.delayTime.value = 60 / score().bpm * .75;
    const feedback = keep(context.createGain()), echoTone = keep(context.createBiquadFilter()), echoReturn = keep(context.createGain());
    feedback.gain.value = .23; echoTone.type = 'lowpass'; echoTone.frequency.value = 2450; echoReturn.gain.value = .15;
    echo.connect(echoTone); echoTone.connect(feedback); feedback.connect(echo);
    echoTone.connect(echoReturn); echoReturn.connect(music);
    reverb = keep(context.createConvolver()); reverb.buffer = makeNoise(Math.floor(context.sampleRate * 2.2), 2.65);
    const roomReturn = keep(context.createGain()); roomReturn.gain.value = .22;
    reverb.connect(roomReturn); roomReturn.connect(music);
    noiseBuffer = makeNoise(Math.floor(context.sampleRate * .7));
    if (typeof context.createPeriodicWave === 'function') {
      const wave = values => context.createPeriodicWave(new Float32Array(values.length), new Float32Array(values));
      waves = {
        keys: wave([0,1,.31,.17,.045,.035,.012]),
        glass: wave([0,1,.08,.19,.025,.055,.012,.025]),
        lead: wave([0,1,.28,.19,.13,.07,.045,.024]),
        bass: wave([0,1,.20,.09,.035]),
        mallet: wave([0,1,.025,.21,.018,.075,.009,.026]),
        reed: wave([0,1,.10,.36,.045,.12,.018,.045]),
        felt: wave([0,1,.16,.08,.022,.012]),
        pluck: wave([0,1,.39,.13,.09,.045,.02,.012]),
      };
    }
    context.addEventListener('statechange', onContextState, { signal: eventController.signal });
  }
  function disposeDeck(deck) {
    if (!deck || deck.disposed) return;
    deck.disposed = true;
    for (const node of deck.nodes) { try { node.disconnect(); } catch {} }
    decks.delete(deck);
  }
  function makeDeck(at, fade = .7) {
    const gain = context.createGain(), echoSend = context.createGain(), roomSend = context.createGain();
    const character = score().character;
    gain.gain.setValueAtTime(0, at); gain.gain.linearRampToValueAtTime(1, at + fade);
    echoSend.gain.value = score().room?.echo ?? (character === 'blue' ? .15 : character === 'velvet' ? .07 : .17);
    roomSend.gain.value = score().room?.reverb ?? (character === 'blue' ? .28 : character === 'velvet' ? .12 : .19);
    gain.connect(music); gain.connect(echoSend); gain.connect(roomSend);
    echoSend.connect(echo); roomSend.connect(reverb);
    const deck = { gain, nodes: [gain,echoSend,roomSend], retiredAt: Infinity, disposed: false };
    decks.add(deck);
    // Rapid skips keep only a small crossfade window, even under repeated input.
    while (decks.size > 3) {
      const oldest = decks.values().next().value;
      for (const voice of voices) if (voice.deck === oldest) voice.stop(context.currentTime + .015);
      disposeDeck(oldest);
    }
    return deck;
  }
  function retireDeck(deck, at, duration = .85) {
    if (!deck || deck.disposed) return;
    hold(deck.gain.gain, at);
    deck.gain.gain.linearRampToValueAtTime(0, at + duration);
    deck.retiredAt = at + duration + .08;
    for (const voice of voices) if (voice.deck === deck) voice.stop(at + duration, false);
  }
  function registerVoice(sources, nodes, envelope, stopAt, deck = currentDeck) {
    let remaining = sources.length, disposed = false;
    const voice = {
      deck,
      stop(at, fadeEnvelope = true) {
        if (disposed) return;
        if (envelope && fadeEnvelope) { hold(envelope.gain, context.currentTime); envelope.gain.linearRampToValueAtTime(0, Math.max(context.currentTime + .015, at)); }
        for (const source of sources) { try { source.stop(at + .012); } catch {} }
      },
    };
    function dispose() {
      if (disposed) return;
      disposed = true; voices.delete(voice);
      for (const node of [...sources,...nodes]) { try { node.disconnect(); } catch {} }
    }
    for (const source of sources) { source.onended = () => { if (--remaining <= 0) dispose(); }; source.stop(stopAt); }
    voices.add(voice);
    if (voices.size > MAX_VOICES) voices.values().next().value.stop(context.currentTime + .02);
  }
  function stereoPan(value, gain, target, nodes) {
    if (typeof context.createStereoPanner !== 'function') { gain.connect(target); return; }
    const pan = context.createStereoPanner(); pan.pan.value = value; gain.connect(pan); pan.connect(target); nodes.push(pan);
  }
  function tone(midi, at, duration, level, { type = 'sine', attack = .008, pan = 0, detune = 0, target = currentDeck?.gain, deck = currentDeck, cutoff = 0, sustain = .001, sweep = null } = {}) {
    if (!context || !target || level <= 0) return;
    const oscillator = context.createOscillator(), gain = context.createGain(), nodes = [gain];
    if (waves[type] && oscillator.setPeriodicWave) oscillator.setPeriodicWave(waves[type]); else oscillator.type = ['sine','triangle','sawtooth','square'].includes(type) ? type : 'triangle';
    oscillator.frequency.setValueAtTime(frequency(midi), at);
    if (sweep !== null) oscillator.frequency.exponentialRampToValueAtTime(frequency(sweep), at + duration * .85);
    oscillator.detune.value = detune;
    const start = Math.min(attack, duration * .24);
    gain.gain.setValueAtTime(0, at); gain.gain.linearRampToValueAtTime(level, at + start);
    if (sustain > .001) { gain.gain.exponentialRampToValueAtTime(Math.max(.0001, level * sustain), at + duration * .66); }
    gain.gain.exponentialRampToValueAtTime(.0001, at + duration);
    if (cutoff) { const filter = context.createBiquadFilter(); filter.type = 'lowpass'; filter.frequency.setValueAtTime(cutoff, at); filter.frequency.exponentialRampToValueAtTime(Math.max(280, cutoff * .45), at + duration); filter.Q.value = .45; oscillator.connect(filter); filter.connect(gain); nodes.push(filter); }
    else oscillator.connect(gain);
    stereoPan(pan, gain, target, nodes);
    oscillator.start(at); registerVoice([oscillator],nodes,gain,at + duration + .025,deck);
  }
  function pad(chord, at, duration, level) {
    const profile = modes[mode], piece = score(), kind = piece.character;
    level *= piece.padLevel ?? 1;
    chord[1].forEach((note,index) => {
      const envelope = context.createGain(), filter = context.createBiquadFilter(), nodes = [envelope,filter];
      filter.type = 'lowpass'; filter.frequency.value = piece.padCutoff ?? (kind === 'blue' ? 1250 : kind === 'prism' ? 2200 : 1700); filter.Q.value = .32;
      envelope.gain.setValueAtTime(0,at);
      envelope.gain.linearRampToValueAtTime(.025 * level * profile.pad,at + Math.min(.85,duration * .24));
      envelope.gain.linearRampToValueAtTime(.020 * level * profile.pad,at + Math.max(.9,duration - .45));
      envelope.gain.exponentialRampToValueAtTime(.0001,at + duration + .75);
      filter.connect(envelope); stereoPan((index - 1.5) * .36,envelope,currentDeck.gain,nodes);
      const sources = [context.createOscillator(),context.createOscillator()];
      sources.forEach((oscillator,voiceIndex) => { oscillator.type = voiceIndex ? 'triangle' : 'sine'; oscillator.frequency.setValueAtTime(frequency(note),at); oscillator.detune.setValueAtTime(voiceIndex ? 5.5 : -5.5,at); oscillator.detune.linearRampToValueAtTime(voiceIndex ? 8 : -8,at + duration); oscillator.connect(filter); oscillator.start(at); });
      registerVoice(sources,nodes,envelope,at + duration + .8);
    });
  }
  function noiseHit(at, level, duration, cutoff, target = currentDeck?.gain, deck = currentDeck, type = 'highpass', attack = .002) {
    if (!target || level <= 0) return;
    const source = context.createBufferSource(), filter = context.createBiquadFilter(), gain = context.createGain();
    source.buffer = noiseBuffer; filter.type = type; filter.frequency.value = cutoff; filter.Q.value = .65;
    gain.gain.setValueAtTime(0,at); gain.gain.linearRampToValueAtTime(level,at + Math.min(attack,duration * .7)); gain.gain.exponentialRampToValueAtTime(.0001,at + duration);
    source.connect(filter); filter.connect(gain); gain.connect(target); source.start(at);
    registerVoice([source],[filter,gain],gain,at + duration + .015,deck);
  }
  function kick(at,strength) {
    tone(47,at,.34,.27 * strength,{sweep:29,attack:.004});
    noiseHit(at,.018 * strength,.025,1800);
  }
  function snare(at,strength,soft = false) {
    noiseHit(at,(soft ? .066 : .09) * strength,soft ? .13 : .18,soft ? 1500 : 1000);
    tone(49,at,.14,.056 * strength,{type:'triangle',sweep:38});
  }
  function playMelody(chord,at,within,bar,section) {
    if (!section.lead) return;
    const piece = score(), motif = piece.motifs[bar % piece.motifs.length], event = motif.find(item => item[0] === within);
    if (!event) return;
    const [,degree,length,velocity] = event;
    if (piece.voice) {
      const voice = piece.voice, pitch = chord[1][degree % 4] + voice.octave;
      const level = .096 * velocity * voice.level * modes[mode].melody * Math.max(.55,section.density);
      const duration = Math.max(.20,length * stepSeconds() * .92) + voice.tail;
      const pan = Math.sin(bar * .47) * .25;
      tone(pitch,at,duration,level,{type:voice.type,pan,attack:voice.attack,cutoff:voice.cutoff,sustain:voice.sustain});
      if (voice.overtone) tone(pitch + 12,at + .008,duration * .48,level * voice.overtone,{type:'sine',pan:-pan,attack:.008});
      if (voice.detuneLayer) tone(pitch,at,duration * .94,level * voice.detuneLayer,{type:'triangle',pan:-pan,detune:5,attack:.035,cutoff:1900});
      return;
    }
    const octave = piece.character === 'velvet' ? 0 : 12;
    const pitch = chord[1][degree % 4] + octave;
    const level = .096 * velocity * modes[mode].melody * (section.name === '间奏' ? .76 : Math.max(.65,section.density));
    const duration = Math.max(.22,length * stepSeconds() * .92);
    const type = piece.character === 'blue' || piece.character === 'velvet' ? 'keys' : piece.character === 'prism' ? 'lead' : 'glass';
    const pan = Math.sin(bar * .71) * .22;
    tone(pitch,at,duration + (piece.character === 'blue' ? .7 : .15),level,{type,pan,attack:piece.character === 'prism' ? .014 : .005,cutoff:piece.character === 'prism' ? 3900 : 6200,sustain:piece.character === 'prism' ? .38 : .10});
    if (piece.character === 'blue' || piece.character === 'glass') tone(pitch + 12,at,duration * .48 + .14,level * .14,{type:'sine',pan:-pan});
    if (piece.character === 'prism') tone(pitch,at,duration + .09,level * .32,{type:'triangle',pan:-.22,detune:-7,attack:.015,sustain:.2});
  }
  function playRhythm(chord,time,within,bar,section) {
    const piece = score(), rhythm = piece.rhythm, d = section.density;
    const arpIndex = rhythm.arp.indexOf(within);
    if (arpIndex >= 0 && (bar >= 2 || within === 0)) {
      const degree = rhythm.degrees[(arpIndex + Math.floor(bar / 4)) % rhythm.degrees.length];
      tone(chord[1][degree] + rhythm.arpOctave,time,rhythm.arpDuration,rhythm.arpLevel * d,{
        type:rhythm.arpType,pan:arpIndex % 2 ? .42 : -.42,cutoff:piece.character === 'quiet' ? 2200 : 4400,
      });
    }
    if (rhythm.stabs?.includes(within) && section.drums) {
      chord[1].slice(1).forEach((note,i) => tone(note,time,.40,.021 * d,{type:rhythm.stabType,pan:(i - 1) * .20,cutoff:2300}));
    }
    if (rhythm.bass.includes(within) && (within === 0 || section.drums)) {
      const note = chord[0] + (within === 14 ? 7 : within === 8 && bar % 2 ? 12 : 0);
      tone(note,time,rhythm.bassDuration,rhythm.bassLevel * Math.max(.45,d) * modes[mode].beat,{
        type:'bass',attack:.026,cutoff:rhythm.bassCutoff,sustain:.20,
      });
    }
    if (!section.drums) return;
    const strength = d * modes[mode].beat * rhythm.drumLevel;
    if (rhythm.kicks.includes(within) && (!rhythm.sparse || bar % 2 === 0)) kick(time,strength * (within === 0 ? 1 : .72));
    if (rhythm.snares.includes(within)) snare(time,strength,rhythm.soft);
    if (rhythm.hats.includes(within)) noiseHit(time,.020 * strength * (within % 4 ? 1 : .58),within === 14 ? .12 : .05,6100);
    if (rhythm.ghosts.includes(within)) noiseHit(time,.009 * strength,.035,5400);
    if (rhythm.wood?.includes(within)) tone(rhythm.woodNote + bar % 2 * 3,time,.08,.035 * strength,{type:'mallet',pan:within < 8 ? -.28 : .28,cutoff:2800});
    if (!rhythm.sparse && bar % 8 === 7 && within === 15) snare(time,.25 * strength,true);
  }
  function dispatchBeat(at,beat,strength) {
    const timer = window.setTimeout(() => {
      beatTimers.delete(timer);
      if (!enabled || destroyed || isHidden() || context?.state !== 'running' || !scheduler) return;
      try { onBeat({ beat, strength: volume ? strength : 0, mode, trackId: score().id }); } catch {}
    },Math.max(0,(at - context.currentTime) * 1000));
    beatTimers.add(timer);
  }
  function scheduleStep(index,at) {
    const piece = score(), within = index % 16, bar = Math.floor(index / 16);
    const chord = piece.chords[Math.floor(bar / 2) % piece.chords.length], section = arrangement(bar,piece);
    const profile = modes[mode], d = section.density, seconds = stepSeconds();
    const offbeat = within % 2 ? seconds * piece.swing : 0;
    const time = at + offbeat;
    if (index % 32 === 0) pad(chord,at,seconds * 32,.92 * section.pad);
    playMelody(chord,time,within,bar,section);
    if (piece.rhythm) {
      playRhythm(chord,time,within,bar,section);
      if (piece.character !== 'quiet' && bar % 8 === 7 && within === 12 && bar < piece.bars - 4) noiseHit(at,.014,.58,1900,currentDeck.gain,currentDeck,'bandpass',.38);
      if (within % 4 === 0) dispatchBeat(at,playCount * 256 + Math.floor(index / 4),(within % 8 === 0 ? .66 : .30) * Math.max(.3,d));
      return;
    }
    const arpEvery = piece.character === 'glass' || piece.character === 'prism' ? 2 : 4;
    if (within % arpEvery === 0 && (bar >= 2 || within % 4 === 0)) {
      const pattern = piece.character === 'prism' ? [0,2,1,3,2,1,3,1] : [0,1,2,1,3,2,1,2];
      const degree = pattern[(within / arpEvery + bar % 2 * 2) % pattern.length];
      const pitch = chord[1][degree] + (piece.character === 'velvet' ? 0 : 12);
      tone(pitch,time,piece.character === 'blue' ? .9 : .35,.032 * d,{type:piece.character === 'blue' ? 'keys' : 'glass',pan:within % 4 ? .48 : -.48,cutoff:4700});
    }
    if (piece.character === 'velvet' && [0,6,10].includes(within)) {
      chord[1].slice(1).forEach((note,i) => tone(note,time,.45,.024 * d,{type:'keys',pan:(i - 1) * .15,cutoff:3400}));
    }
    if (within === 0 || (section.drums && (within === 8 || (piece.character === 'velvet' && within === 14)))) {
      const bassNote = chord[0] + (within === 14 ? 7 : within === 8 && bar % 2 ? 12 : 0);
      tone(bassNote,time,piece.character === 'blue' ? 1.35 : .72,.13 * Math.max(.5,d) * profile.beat,{type:'bass',attack:.025,cutoff:850,sustain:.2});
    }
    if (section.drums) {
      const strength = d * profile.beat;
      const kicks = piece.character === 'prism' ? [0,4,8,12] : piece.character === 'velvet' ? [0,7,10] : piece.character === 'blue' ? [0,10] : [0,8,11];
      if (kicks.includes(within)) kick(time,strength * (within === 0 ? 1 : .75));
      if ((piece.character === 'blue' && within === 12) || (piece.character !== 'blue' && [4,12].includes(within))) snare(time,strength,piece.character === 'velvet' || piece.character === 'blue');
      const hatEvery = piece.character === 'blue' ? 4 : 2;
      if (within % hatEvery === (piece.character === 'blue' ? 2 : 0)) noiseHit(time,.021 * strength * (within % 4 ? 1 : .58),within === 14 ? .15 : .055,6600);
      if (piece.character === 'velvet' && [3,11,15].includes(within)) noiseHit(time,.010 * strength,.04,5700);
      if (bar % 8 === 7 && within === 15) snare(time,.34 * strength,true);
    }
    if (bar % 8 === 7 && within === 12 && bar < piece.bars - 4) noiseHit(at,.018,.6,2300,currentDeck.gain,currentDeck,'bandpass',.42);
    if (within % 4 === 0) dispatchBeat(at,playCount * 256 + Math.floor(index / 4),(within % 8 === 0 ? .72 : .36) * Math.max(.4,d));
  }
  function refillQueue() {
    const candidates = SCORES.map((_,i) => i).filter(i => i !== trackIndex);
    for (let i = candidates.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [candidates[i],candidates[j]] = [candidates[j],candidates[i]]; }
    queue.splice(0,queue.length,...candidates);
  }
  function chooseNext() {
    if (!shuffle) return (trackIndex + 1) % SCORES.length;
    if (!queue.length) refillQueue();
    let next = queue.shift();
    if (next === trackIndex) { if (!queue.length) refillQueue(); next = queue.shift(); }
    return next;
  }
  function clearBeats() { beatTimers.forEach(timer => window.clearTimeout(timer)); beatTimers.clear(); }
  function switchTrack(next,{remember = true,at = context?.currentTime || 0,automatic = false} = {}) {
    if (destroyed || !Number.isInteger(next) || !SCORES[next]) return getState();
    if (remember && next !== trackIndex) { history.push(trackIndex); if (history.length > 40) history.shift(); }
    const oldDeck = currentDeck;
    clearBeats();
    trackIndex = next; positionOffset = 0; positionStartedAt = at; step = 0; playCount++;
    if (!automatic) { const queued = queue.indexOf(next); if (queued >= 0) queue.splice(queued,1); }
    if (context?.state === 'running' && enabled && !isHidden() && scheduler) {
      retireDeck(oldDeck,at,.9);
      currentDeck = makeDeck(at,.8);
      echo.delayTime.setTargetAtTime(60 / score().bpm * .75,at,.3);
      nextStepTime = at + (automatic ? 0 : .028);
      positionStartedAt = nextStepTime;
    } else { currentDeck = null; }
    notify(true);
    return getState();
  }
  function schedule() {
    if (!enabled || destroyed || isHidden() || context?.state !== 'running') return;
    const now = context.currentTime;
    for (const deck of decks) if (now > deck.retiredAt) disposeDeck(deck);
    // Skip stale notes after a blocked tab; never replay a burst of missed events.
    if (nextStepTime < now - .1) {
      step = Math.floor(elapsed() / stepSeconds());
      nextStepTime = now + .035;
    }
    let bounded = 0;
    while (nextStepTime < now + .13 && bounded++ < 8) {
      if (step >= score().bars * 16) {
        // Reuse the same crossfade and voice cleanup at a loop boundary. A
        // repeat is a new performance of this score, not a navigation entry.
        switchTrack(repeatOne ? trackIndex : chooseNext(),{at:nextStepTime,automatic:true,remember:!repeatOne});
      }
      scheduleStep(step++,nextStepTime);
      nextStepTime += stepSeconds();
    }
    if (now - lastNotifyTime >= .25) notify();
  }
  function startScheduler() {
    if (scheduler || destroyed || !enabled || isHidden() || context?.state !== 'running') return;
    const at = context.currentTime + .045;
    currentDeck = makeDeck(at,.55); positionStartedAt = at;
    step = Math.floor(positionOffset / stepSeconds()); nextStepTime = at;
    // Resuming mid-phrase restores its sustained harmony immediately.
    if (step % 32) {
      const chord = score().chords[Math.floor(step / 32) % score().chords.length];
      pad(chord,at,Math.max(1,(32 - step % 32) * stepSeconds()),.85);
    }
    scheduler = window.setInterval(schedule,30);
    fadeMaster(volume * .84,.48); status = 'playing';
    schedule(); notify(true);
  }
  function stopScheduler() {
    if (scheduler) {
      // Read the timeline before removing its running flag, including when the
      // document has just become hidden (elapsed() intentionally hides that).
      if (context) positionOffset = clamp(positionOffset + Math.max(0,context.currentTime - positionStartedAt),0,PLAYLIST[trackIndex].duration);
      window.clearInterval(scheduler);
    }
    scheduler = 0; clearBeats(); resetEffects();
    if (context && context.state !== 'closed') for (const voice of voices) voice.stop(context.currentTime + .045);
    const retiring = [...decks];
    if (retiring.length && context) {
      for (const deck of retiring) {
        hold(deck.gain.gain, context.currentTime);
        deck.gain.gain.linearRampToValueAtTime(0, context.currentTime + .045);
      }
      const timer = window.setTimeout(() => {
        deckTimers.delete(timer);
        for (const deck of retiring) disposeDeck(deck);
      }, 70);
      deckTimers.add(timer);
    }
    currentDeck = null;
  }
  function clearSuspendTimer() { if (suspendTimer) window.clearTimeout(suspendTimer); suspendTimer = 0; }
  function suspendWithFade() {
    clearSuspendTimer(); stopScheduler();
    if (!context || context.state === 'closed') return;
    fadeMaster(0,.075);
    const currentRevision = revision;
    suspendTimer = window.setTimeout(() => {
      suspendTimer = 0;
      if (destroyed || currentRevision !== revision || (enabled && !isHidden())) return;
      context.suspend().catch(() => {}).finally(notify);
    },95);
  }
  async function enable() {
    if (destroyed) return getState();
    if (!supported) { status = 'unavailable'; notify(); return getState(); }
    if (enabled && scheduler && context?.state === 'running' && !isHidden()) { status = 'playing'; notify(); return getState(); }
    const currentRevision = ++revision;
    enabled = true; clearSuspendTimer(); status = isHidden() ? 'paused' : 'starting'; notify();
    try {
      if (!context || context.state === 'closed') buildGraph();
      if (isHidden()) { suspendWithFade(); notify(); return getState(); }
      await context.resume();
      if (destroyed || !enabled || currentRevision !== revision || isHidden()) return getState();
      if (context.state === 'running') startScheduler(); else { status = 'paused'; notify(); }
    } catch {
      if (!destroyed && currentRevision === revision) { enabled = false; status = 'error'; stopScheduler(); notify(); }
    }
    return getState();
  }
  function disable() {
    if (destroyed) return getState();
    ++revision; enabled = false; status = supported ? 'off' : 'unavailable';
    suspendWithFade(); notify(); return getState();
  }
  function onContextState() {
    if (destroyed) return;
    if (context.state === 'running') {
      if (enabled && !isHidden()) startScheduler(); else { suspendWithFade(); notify(); }
    } else { stopScheduler(); if (enabled) status = 'paused'; notify(); }
  }
  async function updateVisibility() {
    if (!enabled || destroyed) return;
    ++revision;
    if (isHidden()) { status = 'paused'; suspendWithFade(); notify(); }
    else await enable();
  }
  function resetEffects() {
    cueTimes.clear();
    if (!context || context.state === 'closed') return;
    // Cancel future alert notes as well as the audible tail on mute/pause. The
    // music deck continues normally when only volume is set to zero.
    for (const voice of voices) if (voice.deck === null) voice.stop(context.currentTime + .02);
    if (music) {
      hold(music.gain,context.currentTime);
      music.gain.linearRampToValueAtTime(.76,context.currentTime + .12);
    }
  }
  function duckMusic(at) {
    hold(music.gain,at);
    music.gain.linearRampToValueAtTime(.045,at + .014);
    music.gain.setValueAtTime(.045,at + .95);
    music.gain.exponentialRampToValueAtTime(.76,at + 1.48);
  }
  function cue(name) {
    if (destroyed || !enabled || !volume || isHidden() || context?.state !== 'running' || !scheduler) return false;
    const aliases = { type:'typing',click:'click',navigate:'navigation',verify:'verify',granted:'success',complete:'complete',progress:'progress',confirm:'confirm',error:'error',navigation:'navigation',typing:'typing',success:'success' };
    const kind = aliases[name]; if (!kind) return false;
    const now = context.currentTime, throttle = kind === 'typing' ? .055 : kind === 'progress' ? .7 : kind === 'error' ? 1.3 : .12;
    if (now - (cueTimes.get(kind) ?? -Infinity) < throttle) return false;
    cueTimes.set(kind,now);
    const at = now + .008;
    const play = (note,delay,duration,gain,extra = {}) => tone(note,at + delay,duration,gain,{target:effects,deck:null,...extra});
    if (kind === 'typing') play(86 + Math.floor(now * 7) % 3 * 2,0,.052,.023);
    else if (kind === 'click') play(74,0,.075,.028);
    else if (kind === 'navigation') { play(62,0,.15,.046,{pan:-.2}); play(69,.07,.23,.042,{pan:.2}); }
    else if (kind === 'verify' || kind === 'progress') { play(74,0,.18,.032,{pan:-.15}); play(76,.11,.24,.028,{pan:.15}); }
    else if (kind === 'confirm') { play(69,0,.17,.044); play(74,.09,.3,.04); }
    else if (kind === 'error') {
      // 1.25 s rejection signature: physical onset, two short warning dyads,
      // a four-note descending denial and a subdued terminal latch. All notes
      // are scheduled on the audio clock and share mute/visibility cleanup.
      duckMusic(at);
      play(43,0,.42,.28,{sweep:27,attack:.003});
      noiseHit(at,.12,.12,900,effects,null,'lowpass');
      [0.035,0.205].forEach((delay,index) => {
        play(72,delay,.115,.070,{type:'triangle',attack:.006,cutoff:2700,pan:index ? .12 : -.12});
        play(78,delay,.105,.034,{type:'triangle',attack:.006,cutoff:3000,pan:index ? -.12 : .12});
      });
      [74,68,58,50].forEach((note,index) => play(note,.40 + index * .12,.19,.058 - index * .006,{
        type:'sawtooth',attack:.006,cutoff:1900 - index * 260,pan:(1.5 - index) * .07,
      }));
      play(53,.41,.32,.085,{type:'triangle',sweep:39,attack:.004,cutoff:1050});
      play(38,.43,.46,.16,{sweep:25,attack:.005});
      noiseHit(at + .43,.070,.16,760,effects,null,'bandpass',.004);
      // A few short, directional data-corruption bleeps accompany the ribbon's
      // packet-loss motion. Quiet and narrow-band; not continuous sharp noise.
      [0,.07,.16,.23].forEach((offset,index)=>{
        play(90 - index*9,.16+offset,.038,.019 - index*.002,{
          type:'square',attack:.002,cutoff:1900 - index*190,pan:index%2?.34:-.34,
        });
      });
      noiseHit(at + .28,.055,.075,1600,effects,null,'bandpass',.007);
      play(45,.99,.24,.052,{type:'triangle',attack:.007,cutoff:850,sweep:38});
      play(39,1.06,.17,.024,{type:'sine',attack:.008});
      noiseHit(at + .99,.025,.065,1300,effects,null,'bandpass',.003);
    } else [62,69,74,76].forEach((note,index) => play(note,index * .09,.55,.046 - index * .004,{pan:(index - 1.5) * .12}));
    return true;
  }
  document.addEventListener('visibilitychange',updateVisibility,{signal:eventController.signal});
  window.addEventListener('pagehide',() => { pageHidden = true; updateVisibility(); },{signal:eventController.signal});
  window.addEventListener('pageshow',() => { pageHidden = false; updateVisibility(); },{signal:eventController.signal});
  return {
    get state() { return getState(); },
    get supported() { return supported; },
    getDiagnostics() {
      let rms = 0, peak = 0;
      if (analyser && context?.state === 'running' && enabled && !isHidden()) {
        analyser.getFloatTimeDomainData(analyserSamples);
        for (const sample of analyserSamples) { rms += sample * sample; peak = Math.max(peak,Math.abs(sample)); }
        rms = Math.sqrt(rms / analyserSamples.length);
      }
      return Object.freeze({contextState:context?.state || 'not-created',currentTime:context?.currentTime || 0,activeVoiceCount:voices.size,schedulerRunning:Boolean(scheduler),pendingBeatCallbacks:beatTimers.size,activeDeckCount:decks.size,enabled,hidden:isHidden(),destroyed,rms,peak,bpm:score().bpm,mode,trackId:score().id,trackIndex,elapsed:elapsed(),duration:PLAYLIST[trackIndex].duration,shuffle,repeatOne,playbackMode:repeatOne?'repeat-one':shuffle?'shuffle':'sequence',queue:[...queue],history:[...history]});
    },
    enable,disable,
    toggle() { return enabled && status !== 'paused' ? Promise.resolve(disable()) : enable(); },
    nextTrack() { return switchTrack(chooseNext()); },
    previousTrack() {
      if (!history.length) return switchTrack(trackIndex,{remember:false});
      return switchTrack(history.pop(),{remember:false});
    },
    selectTrack(value) {
      const index = typeof value === 'string' ? SCORES.findIndex(item => item.id === value) : value;
      return switchTrack(index);
    },
    setShuffle(value) {
      if (destroyed) return getState();
      shuffle = Boolean(value);
      if (shuffle) repeatOne = false;
      queue.length = 0; notify(); return getState();
    },
    setRepeatOne(value) {
      if (destroyed) return getState();
      repeatOne = Boolean(value);
      if (repeatOne) shuffle = false;
      queue.length = 0; notify(); return getState();
    },
    setVolume(value) {
      if (destroyed || !Number.isFinite(Number(value))) return getState();
      volume = clamp(Number(value),0,1);
      if (!volume) resetEffects();
      if (enabled && context?.state === 'running' && !isHidden()) fadeMaster(volume * .84,.16);
      notify(); return getState();
    },
    setMode(next) {
      if (destroyed || !modes[next] || next === mode) return;
      mode = next;
      if (toneFilter && context?.state !== 'closed') toneFilter.frequency.setTargetAtTime(modes[mode].filter,context.currentTime,.7);
      notify();
    },
    cue,
    destroy() {
      if (destroyed) return;
      enabled = false; ++revision; clearSuspendTimer(); stopScheduler(); destroyed = true; status = 'off';
      eventController.abort();
      deckTimers.forEach(timer => window.clearTimeout(timer)); deckTimers.clear();
      for (const deck of [...decks]) disposeDeck(deck);
      for (const node of permanentNodes) { try { node.disconnect(); } catch {} }
      if (context && context.state !== 'closed') context.close().catch(() => {});
      voices.clear(); cueTimes.clear(); queue.length = 0; history.length = 0; notify(true);
    },
  };
}
