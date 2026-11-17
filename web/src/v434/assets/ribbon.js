/**
 * Finance Analyze / suspended ivory ribbon.
 * A sculpture of separate, nearly square ivory slices along a looped path.
 * Every module is a closed six-face rigid box; the spaces between modules are
 * empty geometry. WebGL and Canvas consume the same independent box vertices.
 * No external models, textures, libraries, network requests or audio access are needed.
 */
export function initRibbon(canvas, { mode = 'hero' } = {}) {
  const noop = () => {};
  const inert = { setMode: noop, setState: noop, setPointer: noop, setAudioLevel: noop,
    pulse: noop, triggerGlitch: noop, clearGlitch: noop, hoverAt: noop, clearHover: noop, setPaused: noop, setNavigationBudget:noop, destroy: noop, getStats: () => ({ renderer: 'none', running: false }) };
  if (!canvas || typeof canvas.getContext !== 'function') return inert;
  const ctx = canvas.getContext('2d', { alpha: true });
  if (!ctx) return inert;

  // Keep the original 2D canvas as a robust fallback / static floor shadow.
  // Move the *presentation* of the sculpture with a compositor-only transform
  // instead of tying its screen position to the expensive geometry paint rate.
  const layer = document.createElement('div');
  layer.className = 'fa-ribbon-layer';
  canvas.parentNode?.insertBefore(layer, canvas);
  layer.appendChild(canvas);
  const TAU = Math.PI * 2;
  // The login holds a deliberate three-quarter pose. Internal pages turn slowly
  // around it, then ease back to this pose when the terminal is reopened.
  const pose = Object.freeze({ angle: 3.0, tilt: -.12, roll: -.10 });
  const blockCount = 272;
  const occupancy = .88;
  const pathDepth = 1.32;
  // A broader, quieter centreline keeps the individual square faces legible.
  // Shared dimensions are used for both the curve and its analytic tangent.
  const pathWidth = 1.075, pathRise = .54;
  const pathSway = .035, pathLift = .07, pathDepthSway = .035;
  const largeFaceHalfWidth = .25;
  const largeFaceAspect = .88;
  const largeFaceRadius = Math.hypot(1, largeFaceAspect);
  const rippleLimit = .075;
  const flowRate = .055;
  const ambientSpeed = .84;
  const arcSamples = 4096;
  const arcLookup = new Float32Array(arcSamples + 1);
  const pathStride = 11;
  const track = new Float32Array((arcSamples + 1) * pathStride);
  const wrap = angle => angle - TAU * Math.floor((angle + Math.PI) / TAU);
  const vertexStride = 9; // position xyz, flat face normal xyz, material t/kind/id
  const faces = [
    { corners:[0,4,6,2], axis:0, sign:-1, kind:0 },
    { corners:[1,3,7,5], axis:0, sign: 1, kind:1 },
    { corners:[0,1,5,4], axis:1, sign:-1, kind:3 },
    { corners:[2,6,7,3], axis:1, sign: 1, kind:3 },
    { corners:[0,2,3,1], axis:2, sign:-1, kind:2 },
    { corners:[4,5,7,6], axis:2, sign: 1, kind:2 },
  ];
  const orientation = { ...pose };
  const profiles = {
    hero: { x: .435, y: .485, scale: .435, width: .370, alpha: 1 },
    workspace: { x: .510, y: .500, scale: .455, width: .410, alpha: .33 },
    detail: { x: .590, y: .480, scale: .440, width: .390, alpha: .15 },
  };
  const states = {
    idle: { speed: 1, tension: 0, heat: 0, signal: .13, expansion: 1 },
    verifying: { speed: 1.8, tension: .32, heat: 0, signal: .85, expansion: 1.035 },
    error: { speed: 1.5, tension: .6, heat: 1, signal: .38, expansion: 1.02 },
    success: { speed: 1.35, tension: .12, heat: 0, signal: 1, expansion: 1.075 },
  };
  let selected = profiles[mode] ? mode : 'hero';
  let selectedState = 'idle';
  const energy = { ...states.idle };
  let camera = { x:0, y:0, scale:1 }, shadowDirty = true;
  function positionCamera() {
    const narrow=width<720, original=profiles.hero, target=profiles[selected];
    const originalX=width*(narrow?.46:original.x);
    const originalY=narrow?Math.min(height*.34,310):height*original.y;
    const targetX=width*(narrow?.46:target.x);
    const targetY=selected==='hero'?originalY:height*target.y;
    const originalSize=Math.min(width*(narrow?.50:original.width),height*original.scale);
    const targetSize=Math.min(width*(narrow?.50:target.width),height*target.scale);
    const scale=targetSize/Math.max(.001,originalSize);
    // CSS transform-origin is the viewport center, not the sculpture center.
    const dx=targetX-((originalX-width/2)*scale+width/2);
    const dy=targetY-((originalY-height/2)*scale+height/2);
    camera={x:dx,y:dy,scale};
    layer.style.transform=`translate3d(${dx.toFixed(2)}px,${dy.toFixed(2)}px,0) scale(${scale.toFixed(5)})`;
    layer.style.setProperty('--fa-ribbon-alpha',String(target.alpha));
    layer.dataset.scene=selected;
  }
  const pointer = { x: 0, y: 0, targetX: 0, targetY: 0 };
  const hover = { amount: 0, target: 0, phase: 1.5, targetPhase: 1.5, x: 0, y: 0, tracking: false, needsHit: false, nextCheck:0 };
  let lastInteraction = 'ambient', flowOffset = 0;
  let glitchStart = -Infinity, glitchStrength = 0;
  const glitchDuration = 1.08;
  function glitchAmount() {
    const age = elapsed - glitchStart;
    if (age < 0 || age >= glitchDuration) return 0;
    // One short, damped error burst. No continuous flashing while DENIED stays
    // on screen, and no procedural randomness on individual animation frames.
    return glitchStrength * Math.min(1, age / .055) * Math.pow(1 - age / glitchDuration, 1.38);
  }
  const media = window.matchMedia('(prefers-reduced-motion: reduce)');
  let width = 1, height = 1, dpr = 1, elapsed = 0, ambientElapsed = 0, flow = 0, last = 0, raf = 0;
  let lastPaint = 0, avgPaintMs = 0, adaptivePenalty = 0, sampledFrames = 0;
  let navigationBudgetUntil = 0;
  // Screen-space travel is independent of paints (CSS compositor). Never
  // throttle the band to 20 FPS specifically during navigation: that created
  // the visible staircase in V4.3.3. Use display-synchronized 60/30 cadences
  // rather than uneven 42/24 FPS sampling on a 60 Hz display.
  function targetFps() {
    const gpu = Boolean(renderer && !renderer.lost);
    const desired = gpu && width >= 720 && selected !== 'detail' ? 60 : 30;
    // Never carry a desktop quality reduction over to a narrower viewport.
    return desired===60 ? (adaptivePenalty?30:60) : (avgPaintMs>32?20:30);
  }
  let paused = false, destroyed = false, audioLevel = 0, drawCount = 0;
  let waves = [];
  let mesh = [], facets = [];
  const visibleFacets = [];
  const neighbourPairs = Array.from({length:blockCount},()=>({a:null,b:null,distance:0,axis:[0,0,0]}));
  const vertexData = new Float32Array(blockCount * 24 * vertexStride);
  const indexData = new Uint16Array(blockCount * 36);
  let minimumGap = 0, visibleFaces = 0;
  let renderer = null;
  let fallbackReason = 'not-initialized';

  const vertexSource = `
    precision highp float;
    attribute vec3 aPosition, aNormal, aMaterial;
    uniform vec4 uScreen;
    uniform vec3 uAngleCS, uAngleSN;
    uniform float uShadowPass;
    varying vec3 vNormal, vPosition, vMaterial;
    varying vec4 vShadow;
    vec3 rotatePoint(vec3 p) {
      float cy=uAngleCS.x, sy=uAngleSN.x;
      float ct=uAngleCS.y, st=uAngleSN.y;
      float cr=uAngleCS.z, sr=uAngleSN.z;
      p=vec3(p.x*cy+p.z*sy,p.y,p.z*cy-p.x*sy);
      p=vec3(p.x,p.y*ct-p.z*st,p.y*st+p.z*ct);
      return vec3(p.x*cr-p.y*sr,p.x*sr+p.y*cr,p.z);
    }
    vec4 shadowPosition(vec3 p) {
      vec3 light=normalize(vec3(-.48,-.72,-.85));
      vec3 f=-light;
      vec3 r=normalize(cross(vec3(0.,1.,0.),f));
      vec3 u=cross(f,r);
      return vec4(dot(p,r)/1.9,dot(p,u)/1.9,dot(p,f)/2.8,1.);
    }
    void main() {
      vec3 p=rotatePoint(aPosition);
      vPosition=p;
      vNormal=rotatePoint(aNormal);
      vMaterial=aMaterial;
      vShadow=shadowPosition(p);
      if(uShadowPass>.5) { gl_Position=vShadow; return; }
      float depth=4.6+p.z;
      gl_Position=vec4(uScreen.x*depth+p.x*uScreen.z,
        uScreen.y*depth-p.y*uScreen.w,1.222222*depth-2.222222,depth);
    }
  `;
  const fragmentSource = `
    precision highp float;
    uniform sampler2D uShadowMap;
    uniform float uShadowPass, uShadowTexel, uTime, uGlitchTime, uHeat, uSignal, uAudio, uGlitch;
    varying vec3 vNormal, vPosition, vMaterial;
    varying vec4 vShadow;
    vec4 packDepth(float depth) {
      vec4 bits=fract(depth*vec4(16777216.,65536.,256.,1.));
      return bits-bits.xxyz*vec4(0.,.00390625,.00390625,.00390625);
    }
    float unpackDepth(vec4 colour) {
      return dot(colour,vec4(.000000059604644775390625,.0000152587890625,.00390625,1.));
    }
    void main() {
      if(uShadowPass>.5) { gl_FragColor=packDepth(gl_FragCoord.z); return; }
      vec3 normal=normalize(vNormal);
      vec3 light=normalize(vec3(-.48,-.72,-.85));
      vec3 view=normalize(vec3(-vPosition.x,-vPosition.y,-4.6-vPosition.z));
      vec3 halfVector=normalize(light+view);
      float diffuse=max(dot(normal,light),0.);
      vec3 sc=vShadow.xyz*.5+.5;
      float bias=max(.0013,.0026*(1.-diffuse));
      float shade=0.;
      for(int x=-1;x<=1;x++) for(int y=-1;y<=1;y++) {
        vec2 uv=sc.xy+vec2(float(x),float(y))*uShadowTexel*1.5;
        shade+=step(sc.z-bias,unpackDepth(texture2D(uShadowMap,uv)));
      }
      shade/=9.;
      float endFace=step(1.5,vMaterial.y);
      float reverseFace=step(.5,vMaterial.y)*(1.-endFace);
      vec3 ivory=mix(vec3(.980,.965,.940),vec3(.84,.825,.79),endFace*.23+reverseFace*.10);
      float ambient=.675+.028*(.5-.5*normal.y);
      float lighting=ambient+diffuse*.335*(.42+.58*shade);
      float specular=pow(max(dot(normal,halfVector),0.),24.)*.075*shade;
      vec3 colour=ivory*lighting+vec3(1.,.99,.93)*specular;
      float distance=mod(vMaterial.x-uTime*1.7+3.14159265,6.28318530)-3.14159265;
      float beam=exp(-distance*distance*23.);
      vec3 signal=mix(vec3(.67,.88,.34),vec3(1.,.43,.16),uHeat);
      colour=mix(colour,signal,.060*uHeat+.14*beam*(uSignal+uAudio*.4));
      // Discrete groups of square-faced modules briefly report a corrupted
      // signal: dark packet loss and muted copper highlights, never full-white
      // strobe. The same bounds work in Canvas fallback below.
      if(uGlitch>0.) {
        float packet=step(.48,sin(floor(vMaterial.z/5.)*3.13+floor(uGlitchTime*23.)*1.77));
        vec3 corrupted=mix(vec3(.13,.22,.13),vec3(.96,.43,.13),step(.2,sin(vMaterial.z*.58+floor(uGlitchTime*21.)*2.1)));
        colour=mix(colour,corrupted,uGlitch*packet*.64);
      }
      gl_FragColor=vec4(clamp(colour,0.,1.),1.);
    }
  `;

  function buildRenderer() {
    if (!document.createElement) { fallbackReason = 'canvas-creation-unavailable'; return null; }
    // Render to a *visible* GPU surface. Offscreen WebGL -> 2D drawImage
    // forced a GPU/CPU compositing copy every paint on several browsers.
    const surface = document.createElement('canvas');
    surface.className = 'fa-ribbon-gpu';
    surface.setAttribute('aria-hidden','true');
    let gl;
    try {
      gl = surface.getContext('webgl', { alpha: true, antialias: true, depth: true,
        premultipliedAlpha: false, preserveDrawingBuffer: false, powerPreference: 'low-power' });
    } catch { fallbackReason = 'context-creation-failed'; return null; }
    if (!gl) { fallbackReason = 'context-unavailable'; return null; }
    const resources = { shaders: [], buffers: [], textures: [], programs: [], renderbuffers: [], framebuffers: [] };
    function dispose() {
      for (const s of resources.shaders) gl.deleteShader(s);
      for (const b of resources.buffers) gl.deleteBuffer(b);
      for (const t of resources.textures) gl.deleteTexture(t);
      for (const p of resources.programs) gl.deleteProgram(p);
      for (const b of resources.renderbuffers) gl.deleteRenderbuffer(b);
      for (const b of resources.framebuffers) gl.deleteFramebuffer(b);
      surface.removeEventListener('webglcontextlost', lost);
      surface.removeEventListener('webglcontextrestored', restored);
      surface.remove();
    }
    function lost(event) { event.preventDefault(); result.lost = true; surface.hidden=true; fallbackReason = 'context-lost'; shadowDirty=true; sync(); }
    function restored() {
      if (destroyed) return;
      dispose();
      renderer = buildRenderer();
      setRendererLabel();
      sync();
    }
    let result;
    try {
      const program = gl.createProgram(); resources.programs.push(program);
      for (const [type, source] of [[gl.VERTEX_SHADER, vertexSource], [gl.FRAGMENT_SHADER, fragmentSource]]) {
        const shader = gl.createShader(type); resources.shaders.push(shader);
        gl.shaderSource(shader, source); gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
          const stage=type===gl.VERTEX_SHADER?'vertex':'fragment';
          throw new Error(`shader-${stage}: ${gl.getShaderInfoLog(shader)||'compile failed'}`);
        }
        gl.attachShader(program, shader);
      }
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(`shader-link: ${gl.getProgramInfoLog(program)||'link failed'}`);
      const vertexBuffer=gl.createBuffer(), indexBuffer=gl.createBuffer();
      resources.buffers.push(vertexBuffer,indexBuffer);
      gl.bindBuffer(gl.ARRAY_BUFFER,vertexBuffer);
      gl.bufferData(gl.ARRAY_BUFFER,vertexData,gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,indexBuffer);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,indexData,gl.STATIC_DRAW);
      const attributes=['aPosition','aNormal','aMaterial'].map(name=>gl.getAttribLocation(program,name));
      const uniform={};
      for(const name of ['uScreen','uAngleCS','uAngleSN','uTime','uGlitchTime','uAudio','uShadowPass',
        'uShadowMap','uShadowTexel','uHeat','uSignal','uGlitch']) uniform[name]=gl.getUniformLocation(program,name);
      const shadowSize=width<720?512:1024;
      const shadowTexture=gl.createTexture(); resources.textures.push(shadowTexture);
      gl.bindTexture(gl.TEXTURE_2D,shadowTexture);
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,shadowSize,shadowSize,0,gl.RGBA,gl.UNSIGNED_BYTE,null);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
      const whiteTexture=gl.createTexture(); resources.textures.push(whiteTexture);
      gl.bindTexture(gl.TEXTURE_2D,whiteTexture);
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,1,1,0,gl.RGBA,gl.UNSIGNED_BYTE,new Uint8Array([255,255,255,255]));
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.NEAREST);
      const depth=gl.createRenderbuffer(); resources.renderbuffers.push(depth);
      gl.bindRenderbuffer(gl.RENDERBUFFER,depth);
      gl.renderbufferStorage(gl.RENDERBUFFER,gl.DEPTH_COMPONENT16,shadowSize,shadowSize);
      const framebuffer=gl.createFramebuffer(); resources.framebuffers.push(framebuffer);
      gl.bindFramebuffer(gl.FRAMEBUFFER,framebuffer);
      gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,shadowTexture,0);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER,gl.DEPTH_ATTACHMENT,gl.RENDERBUFFER,depth);
      const framebufferStatus=gl.checkFramebufferStatus(gl.FRAMEBUFFER);
      if(framebufferStatus!==gl.FRAMEBUFFER_COMPLETE) throw new Error(`framebuffer-incomplete: ${framebufferStatus}`);
      gl.bindFramebuffer(gl.FRAMEBUFFER,null);
      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
      gl.disable(gl.CULL_FACE);
      gl.disable(gl.BLEND);
      // This WebGL context only draws the ribbon; attributes and static
      // uniforms stay bound until the context is rebuilt.
      gl.useProgram(program);
      for(let i=0;i<attributes.length;i++) {
        gl.enableVertexAttribArray(attributes[i]);
        gl.vertexAttribPointer(attributes[i],3,gl.FLOAT,false,vertexStride*4,i*3*4);
      }
      gl.uniform1f(uniform.uShadowTexel,1/shadowSize); gl.uniform1i(uniform.uShadowMap,0);
      gl.activeTexture(gl.TEXTURE0);
      result={surface,lost:false,dispose,draw(view) {
        if(result.lost) return false;
        const pixelWidth=Math.round(width*dpr), pixelHeight=Math.round(height*dpr);
        if(surface.width!==pixelWidth || surface.height!==pixelHeight) {
          surface.width=pixelWidth; surface.height=pixelHeight;
        }
        gl.bufferSubData(gl.ARRAY_BUFFER,0,vertexData);
        gl.uniform4f(uniform.uScreen,view.x/width*2-1,1-view.y/height*2,view.size/width*9.2,view.size/height*9.2);
        gl.uniform3f(uniform.uAngleCS,view.cy,view.ct,view.cr);
        gl.uniform3f(uniform.uAngleSN,view.sy,view.st,view.sr);
        gl.uniform1f(uniform.uTime,(ambientElapsed * 1.7 % TAU) / 1.7);
        gl.uniform1f(uniform.uGlitchTime,elapsed); gl.uniform1f(uniform.uAudio,audioLevel);
        gl.uniform1f(uniform.uHeat,energy.heat); gl.uniform1f(uniform.uSignal,energy.signal); gl.uniform1f(uniform.uGlitch,glitchAmount());
        gl.bindTexture(gl.TEXTURE_2D,whiteTexture);
        gl.bindFramebuffer(gl.FRAMEBUFFER,framebuffer);
        gl.viewport(0,0,shadowSize,shadowSize);
        gl.clearColor(1,1,1,1); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
        gl.uniform1f(uniform.uShadowPass,1);
        gl.drawElements(gl.TRIANGLES,indexData.length,gl.UNSIGNED_SHORT,0);
        gl.bindFramebuffer(gl.FRAMEBUFFER,null);
        gl.bindTexture(gl.TEXTURE_2D,shadowTexture);
        gl.viewport(0,0,pixelWidth,pixelHeight);
        gl.clearColor(0,0,0,0); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
        gl.uniform1f(uniform.uShadowPass,0);
        gl.drawElements(gl.TRIANGLES,indexData.length,gl.UNSIGNED_SHORT,0);
        return true;
      }};
      fallbackReason = '';
      layer.appendChild(surface);
      surface.addEventListener('webglcontextlost',lost);
      surface.addEventListener('webglcontextrestored',restored);
      return result;
    } catch (error) {
      fallbackReason = String(error?.message || 'renderer-initialization-failed').replace(/\s+/g,' ').slice(0,240);
      dispose();
      return null;
    }
  }

  function setRendererLabel() {
    canvas.setAttribute('data-ribbon-renderer',renderer && !renderer.lost?'webgl-solid':'canvas-solid');
    canvas.setAttribute('data-ribbon-fallback',fallbackReason);
    canvas.setAttribute('data-ribbon-pose',selected==='hero'?'suspended':canAnimate()?'rotating':'static');
    canvas.setAttribute('data-ribbon-structure','cuboids');
    canvas.setAttribute('data-ribbon-block-count',String(blockCount));
    canvas.setAttribute('data-ribbon-density','fine');
    canvas.setAttribute('data-ribbon-large-face','near-square');
    canvas.setAttribute('data-ribbon-motion','flow-and-spin');
  }
  function rotate(x,y,z,view) {
    const rx=x*view.cy+z*view.sy, rz=z*view.cy-x*view.sy;
    const ry=y*view.ct-rz*view.st, depth=y*view.st+rz*view.ct;
    return [rx*view.cr-ry*view.sr,rx*view.sr+ry*view.cr,depth];
  }
  function project(p,view) {
    const scale=4.6/(4.6+p[2]);
    return {x:view.x+p[0]*view.size*scale,y:view.y+p[1]*view.size*scale,z:p[2]};
  }
  function dot(a,b) { return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
  function pathPoint(t) {
    return [pathWidth*Math.sin(t)+pathSway*Math.sin(2*t+.043),
      pathRise*Math.sin(2*t)+pathLift*Math.cos(t+.043),
      pathDepth*Math.cos(t)+pathDepthSway*Math.sin(2*t+.043)];
  }
  function makeBlockMesh() {
    // Invert a sampled arc-length map once. Every module then travels at the
    // same speed around the fixed sculpture, instead of bunching at its bends.
    const lengths = new Float64Array(arcSamples + 1);
    let previous = pathPoint(0);
    for(let i=1;i<=arcSamples;i++) {
      const point=pathPoint(i*TAU/arcSamples);
      lengths[i]=lengths[i-1]+Math.hypot(...point.map((v,k)=>v-previous[k]));
      previous=point;
    }
    let cursor=1;
    for(let i=0;i<=arcSamples;i++) {
      const distance=lengths[arcSamples]*i/arcSamples;
      while(cursor<arcSamples && lengths[cursor]<distance)cursor++;
      const blend=(distance-lengths[cursor-1])/Math.max(1e-9,lengths[cursor]-lengths[cursor-1]);
      arcLookup[i]=(cursor-1+blend)*TAU/arcSamples;
      const t=arcLookup[i], drift=.043;
      const sinT=Math.sin(t),cosT=Math.cos(t),cosDoubled=Math.cos(2*t+drift);
      const dx=pathWidth*cosT+2*pathSway*cosDoubled;
      const dy=2*pathRise*Math.cos(2*t)-pathLift*Math.sin(t+drift);
      const dz=-pathDepth*sinT+2*pathDepthSway*cosDoubled;
      const length=Math.hypot(dx,dy,dz), plane=Math.max(.001,Math.hypot(dx,dy));
      const j=i*pathStride, point=pathPoint(t);
      track.set(point,j);
      track[j+3]=dx/length;track[j+4]=dy/length;track[j+5]=dz/length;
      track[j+6]=-dz*dx/(plane*length);track[j+7]=-dz*dy/(plane*length);track[j+8]=plane/length;
      track[j+9]=Math.sin(t+.18)*.24;
      track[j+10]=largeFaceHalfWidth*(.96+.10*Math.sin(t*2+.32));
    }
    mesh=Array.from({length:blockCount},(_,i)=>({
      origin:(i+.5)*TAU/blockCount,phase:0,center:[0,0,0],axes:[[1,0,0],[0,1,0],[0,0,1]],
      pathTangent:[1,0,0],spin:0,
      // Every slice still turns around its own tangent. A common slow rate and
      // smooth phase offsets prevent neighbouring squares drifting into a
      // disordered fan after a long session.
      spinPhase:.035*Math.sin(i*TAU/blockCount)+.018*Math.cos(i*TAU*2/blockCount),
      spinRate:.10,
      half:[.025,.25,.22],corners:Array.from({length:8},()=>[0,0,0]),
      projected:Array.from({length:8},()=>({x:0,y:0,z:0})),
      screenFace:Array.from({length:4},()=>({x:0,y:0,z:0})),screenDepth:0,screenRadius:7,
    }));
    facets=Array.from({length:blockCount*6},(_,i)=>({i:Math.floor(i/6),face:i%6,z:0,normal:[0,0,0],visible:true}));
    for(let i=0;i<blockCount;i++) for(let f=0;f<6;f++) {
      const base=(i*6+f)*4, offset=(i*6+f)*6;
      indexData.set([base,base+1,base+2,base,base+2,base+3],offset);
      for(let corner=0;corner<4;corner++){
        const material=(base+corner)*vertexStride+7;
        vertexData[material]=faces[f].kind;
        vertexData[material+1]=i;
      }
    }
  }
  const samplePath = (n, blend) => track[n] + (track[n+pathStride] - track[n]) * blend;
  function updateNeighbourPairs() {
    // Every pair was previously rebuilt TWICE per painted frame, including
    // temporary arrays for delta, tangent and the pair itself. Keep one stable
    // object and calculate the shared separating axis once per frame.
    for(let i=0;i<blockCount;i++){
      const pair=neighbourPairs[i],a=mesh[i],b=mesh[i+1]??mesh[0];
      pair.a=a;pair.b=b;
      const axis=pair.axis;
      const x=a.pathTangent[0]+b.pathTangent[0];
      const y=a.pathTangent[1]+b.pathTangent[1];
      const z=a.pathTangent[2]+b.pathTangent[2];
      const inverse=1/Math.max(.00001,Math.sqrt(x*x+y*y+z*z));
      axis[0]=x*inverse;axis[1]=y*inverse;axis[2]=z*inverse;
      pair.distance=Math.max(.00001,
        (b.center[0]-a.center[0])*axis[0]+(b.center[1]-a.center[1])*axis[1]+(b.center[2]-a.center[2])*axis[2]);
    }
  }
  function updateGeometry(gpu=false) {
    const phase=flow*.25, hovering=hover.amount>0, audioWidth=1+audioLevel*.010;
    const errorSignal=glitchAmount(), packetClock=Math.floor(elapsed*23);
    flowOffset=(flow*flowRate)%TAU;
    for(let i=0;i<mesh.length;i++) {
      const block=mesh[i];
      const pathPhase=block.origin+flowOffset;
      block.phase=pathPhase<TAU?pathPhase:pathPhase-TAU;
      const position=block.phase*arcSamples/TAU,index=Math.floor(position),blend=position-index,j=index*pathStride;
      const tx=samplePath(j+3,blend),ty=samplePath(j+4,blend),tz=samplePath(j+5,blend);
      const plane=Math.max(.001,Math.sqrt(tx*tx+ty*ty));
      const nx=-ty/plane,ny=tx/plane;
      const bx=samplePath(j+6,blend),by=samplePath(j+7,blend),bz=samplePath(j+8,blend);
      let ripple=Math.sin(block.phase*3-flow*.68)*.0035+Math.sin(block.phase*7-phase*2.2)*.0012;
      ripple+=Math.sin(block.phase*12-ambientElapsed*4)*energy.tension*.006;
      let hoverStrength=0;
      if(hovering){
        const distance=wrap(block.phase-hover.phase);
        hoverStrength=Math.exp(-distance*distance*6)*hover.amount;
        ripple+=Math.sin(distance*6-elapsed*2)*hoverStrength*.005;
      }
      // Consecutive groups of five slices briefly lose synchronization. A
      // stable packet clock avoids noisy flicker and restores the exact pose
      // when the short error impulse has expired.
      const packet=Math.floor(i/5);
      const affected=errorSignal>0 && Math.sin(packet*3.13+packetClock*1.77)>.48;
      const glitchSlip=affected?errorSignal*Math.sin(packet*8.71+packetClock*2.41)*.075:0;
      const glitchSpin=affected?errorSignal*Math.sin(packet*2.73+packetClock)*.25:0;
      for(const wave of waves) {
        const age=elapsed-wave.start, impact=wave.kind==='error';
        const distance=wrap(block.phase-wave.origin-age*(impact?3.8:3.1));
        const carrier=Math.sin(distance*10)*Math.exp(-distance*distance*2.6);
        const ring=carrier*Math.exp(-age*(impact?2:1.6))*.055*(impact?1.8:1);
        const kick=impact?Math.exp(-distance*distance*6)*Math.exp(-age*10)*.024:0;
        ripple+=(ring+kick)*wave.strength;
      }
      // Each block receives one rigid transform. No corner is bent or connected
      // to its neighbour; click waves move and tilt whole independent modules.
      ripple=rippleLimit*Math.tanh(ripple/rippleLimit);
      // The two large dimensions form the facing square of each slice. Turn
      // that square around its thin, along-path axis at one calm pace; gentle
      // phase offsets preserve the layered surface without accumulating drift.
      block.spin=block.spinPhase+ambientElapsed*block.spinRate;
      // A bounded, closed tilt replaces a full extra turn around the loop.
      // This leaves flow and self-spin visible without four crowded folds.
      const twist=.70+samplePath(j+9,blend)+block.spin+ripple*.38+hoverStrength*.010+glitchSpin;
      const c=Math.cos(twist),s=Math.sin(twist);
      const across=block.axes[1], tangent=block.pathTangent, thin=block.axes[0], normal=block.axes[2];
      tangent[0]=tx;tangent[1]=ty;tangent[2]=tz;
      thin[0]=tangent[0];thin[1]=tangent[1];thin[2]=tangent[2];
      across[0]=nx*c+bx*s;
      across[1]=ny*c+by*s;across[2]=bz*s;
      normal[0]=tangent[1]*across[2]-tangent[2]*across[1];
      normal[1]=tangent[2]*across[0]-tangent[0]*across[2];
      normal[2]=tangent[0]*across[1]-tangent[1]*across[0];
      block.center[0]=samplePath(j,blend)+bx*(ripple+glitchSlip);
      block.center[1]=samplePath(j+1,blend)+by*(ripple+glitchSlip);
      block.center[2]=samplePath(j+2,blend)+bz*(ripple+glitchSlip);
      block.half[0]=.025;
      block.half[1]=samplePath(j+10,blend)*audioWidth;
      block.half[2]=block.half[1]*largeFaceAspect;
    }
    // Reserve the entire large face's circumcircle for arbitrary self-rotation.
    // The two broad sides never shrink to negotiate a bend. Only slice thickness
    // uses the remaining tangent gap; the deeper curve leaves positive space
    // even for maximum overlapping waves and the square's diagonal orientation.
    updateNeighbourPairs();
    for(let i=0;i<blockCount;i++) {
      const {a,b,distance,axis}=neighbourPairs[i];
      let cross=0;
      const projA=dot(a.pathTangent,axis),projB=dot(b.pathTangent,axis);
      cross+=a.half[1]*largeFaceRadius*Math.sqrt(Math.max(0,1-projA*projA));
      cross+=b.half[1]*largeFaceRadius*Math.sqrt(Math.max(0,1-projB*projB));
      const budget=Math.max(.000001,distance-cross)*occupancy;
      a.half[0]=Math.min(a.half[0],budget*.5/Math.max(.001,Math.abs(projA)));
      b.half[0]=Math.min(b.half[0],budget*.5/Math.max(.001,Math.abs(projB)));
    }
    // This minimum is diagnostic only, not used to position blocks.
    // Check periodically instead of doing six extra dot products per pair
    // on every frame, including frames with no interaction.
    if(drawCount%30===0 || errorSignal>.01 || waves.length) {
      minimumGap=Infinity;
      for(let i=0;i<blockCount;i++) {
        const {a,b,distance,axis}=neighbourPairs[i];
        let extent=0;
        for(let k=0;k<3;k++) extent+=Math.abs(dot(a.axes[k],axis))*a.half[k]+Math.abs(dot(b.axes[k],axis))*b.half[k];
        minimumGap=Math.min(minimumGap,distance-extent);
      }
    }
    for(let i=0;i<blockCount;i++) {
      const block=mesh[i];
      for(let k=0;k<3;k++) {
        const x=block.axes[0][k]*block.half[0],y=block.axes[1][k]*block.half[1],z=block.axes[2][k]*block.half[2];
        for(let corner=0;corner<8;corner++){
          block.corners[corner][k]=block.center[k]+(corner&1?x:-x)+(corner&2?y:-y)+(corner&4?z:-z);
        }
      }
      // The Canvas path never consumes this GPU vertex buffer. Skipping its
      // 58,752 float writes per painted frame removes needless fallback work.
      if(gpu)for(let f=0;f<6;f++) {
        const face=faces[f],normal=block.axes[face.axis];
        for(let corner=0;corner<4;corner++) {
          const offset=((i*6+f)*4+corner)*vertexStride,p=block.corners[face.corners[corner]];
          vertexData[offset]=p[0];vertexData[offset+1]=p[1];vertexData[offset+2]=p[2];
          vertexData[offset+3]=normal[0]*face.sign;vertexData[offset+4]=normal[1]*face.sign;vertexData[offset+5]=normal[2]*face.sign;
          vertexData[offset+6]=block.phase;
        }
      }
    }
  }
  function rotateInto(x,y,z,view,out) {
    const rx=x*view.cy+z*view.sy,rz=z*view.cy-x*view.sy;
    const ry=y*view.ct-rz*view.st,depth=y*view.st+rz*view.ct;
    out[0]=rx*view.cr-ry*view.sr;
    out[1]=rx*view.sr+ry*view.cr;
    out[2]=depth;
    return out;
  }
  const fallbackPoint=[0,0,0];
  const channel=value=>Math.max(0,Math.min(255,Math.round(value)));
  function drawFallback(view) {
    const glitch=glitchAmount(), packetClock=Math.floor(elapsed*23);
    const hot=energy.heat>.4;
    const signalR=hot?230:171,signalG=hot?132:206,signalB=hot?71:108;
    visibleFacets.length=0;
    for(const block of mesh)for(let i=0;i<8;i++){
      const c=block.corners[i],projected=block.projected[i];
      rotateInto(c[0],c[1],c[2],view,fallbackPoint);
      const scale=4.6/(4.6+fallbackPoint[2]);
      projected.x=view.x+fallbackPoint[0]*view.size*scale;
      projected.y=view.y+fallbackPoint[1]*view.size*scale;
      projected.z=fallbackPoint[2];
    }
    for(const facet of facets) {
      const block=mesh[facet.i],face=faces[facet.face];
      const axis=block.axes[face.axis];
      rotateInto(axis[0]*face.sign,axis[1]*face.sign,axis[2]*face.sign,view,facet.normal);
      const extent=block.half[face.axis]*face.sign;
      rotateInto(block.center[0]+axis[0]*extent,block.center[1]+axis[1]*extent,block.center[2]+axis[2]*extent,view,fallbackPoint);
      facet.z=fallbackPoint[2];
      facet.visible=facet.normal[0]*(-fallbackPoint[0])+facet.normal[1]*(-fallbackPoint[1])+facet.normal[2]*(-4.6-fallbackPoint[2])>0;
      if(facet.visible)visibleFacets.push(facet);
    }
    // Do not sort all 1632 faces when back-facing faces cannot be drawn.
    // Reuse this array per frame to avoid an extra allocation/GC source.
    visibleFacets.sort((a,b)=>b.z-a.z);
    visibleFaces=visibleFacets.length;
    for(const facet of visibleFacets) {
      const block=mesh[facet.i],face=faces[facet.face],normal=facet.normal;
      const diffuse=Math.max(0,-normal[0]*.395-normal[1]*.594-normal[2]*.701);
      const tint=face.kind>=2?.95:face.kind===1?.985:1;
      const grey=(180+65*diffuse)*tint;
      const distance=wrap(block.phase-ambientElapsed*1.7);
      const beam=Math.exp(-distance*distance*23);
      const mix=.06*energy.heat+.14*beam*(energy.signal+audioLevel*.4);
      let r=(grey+3)*(1-mix)+signalR*mix;
      let g=(grey+1)*(1-mix)+signalG*mix;
      let b=(grey-3)*(1-mix)+signalB*mix;
      if(glitch>0 && Math.sin(Math.floor(facet.i/5)*3.13+packetClock*1.77)>.48) {
        const copper=Math.sin(facet.i*.58+Math.floor(elapsed*21)*2.1)>.2;
        const corruption=glitch*.64;
        r=r*(1-corruption)+(copper?240:33)*corruption;
        g=g*(1-corruption)+(copper?108:56)*corruption;
        b=b*(1-corruption)+(copper?45:33)*corruption;
      }
      ctx.fillStyle=`rgb(${channel(r)},${channel(g)},${channel(b)})`;
      ctx.beginPath();
      for(let i=0;i<4;i++) {
        const p=block.projected[face.corners[i]];
        if(i===0)ctx.moveTo(p.x,p.y);else ctx.lineTo(p.x,p.y);
      }
      ctx.closePath();ctx.fill();
      // These <1 px strokes disappear at the signed-in opacity (0.15–0.33),
      // yet used to launch hundreds of extra Canvas strokes per frame.
      // Keep them only for the large full-opacity login sculpture.
      if(selected==='hero') {
        ctx.lineWidth=.22;
        ctx.strokeStyle=diffuse>.5?'rgba(255,255,244,.16)':'rgba(75,70,55,.05)';
        ctx.stroke();
      }
    }
  }

  function updateHitTargets(view) {
    for(const block of mesh) {
      const across=block.axes[1],normal=block.axes[2];
      for(let i=0;i<4;i++) {
        const sx=i===0||i===3?-1:1,sy=i<2?-1:1;
        const x=block.center[0]+across[0]*block.half[1]*sx+normal[0]*block.half[2]*sy;
        const y=block.center[1]+across[1]*block.half[1]*sx+normal[1]*block.half[2]*sy;
        const z=block.center[2]+across[2]*block.half[1]*sx+normal[2]*block.half[2]*sy;
        rotateInto(x,y,z,view,fallbackPoint);
        const projected=block.screenFace[i],scale=4.6/(4.6+fallbackPoint[2]);
        projected.x=view.x+fallbackPoint[0]*view.size*scale;
        projected.y=view.y+fallbackPoint[1]*view.size*scale;
        projected.z=fallbackPoint[2];
      }
      block.screenDepth=block.screenFace.reduce((sum,p)=>sum+p.z,0)/4;
      block.screenRadius=Math.max(7,view.size*block.half[0]*1.8+4);
    }
    if(hover.tracking) findHover();
  }
  function findHover() {
    let nearest=null,score=Infinity;
    for(const block of mesh) {
      let distance=Infinity,positive=false,negative=false;
      for(let i=0;i<4;i++) {
        const a=block.screenFace[i],b=block.screenFace[(i+1)%4],dx=b.x-a.x,dy=b.y-a.y;
        const side=dx*(hover.y-a.y)-dy*(hover.x-a.x);
        if(side>.0001)positive=true;if(side<-.0001)negative=true;
        const u=Math.max(0,Math.min(1,((hover.x-a.x)*dx+(hover.y-a.y)*dy)/Math.max(1,dx*dx+dy*dy)));
        distance=Math.min(distance,Math.hypot(hover.x-a.x-dx*u,hover.y-a.y-dy*u));
      }
      if(!(positive&&negative))distance=0;
      if(distance>block.screenRadius)continue;
      const rank=distance+block.screenDepth*.16;
      if(rank<score){score=rank;nearest=block;}
    }
    hover.target=nearest?1:0;
    if(nearest){hover.targetPhase=nearest.phase;if(!waves.length)lastInteraction='hover';}
  }
  function hoverAt(clientX,clientY) {
    if(destroyed||paused||media.matches||document.hidden)return;
    const rect=canvas.getBoundingClientRect();
    // getBoundingClientRect includes the compositor camera transform.
    // Map viewport pointer coordinates back to the unscaled canvas space.
    hover.x=(Number(clientX)-rect.left)*width/Math.max(1,rect.width);
    hover.y=(Number(clientY)-rect.top)*height/Math.max(1,rect.height);
    if(!Number.isFinite(hover.x)||!Number.isFinite(hover.y))return;
    // Find the target on the next paint, using the same projected geometry as
    // the image. Pointer events no longer trigger a full 272-block scan each time.
    hover.tracking=true;hover.needsHit=true;
  }
  function clearHover() { hover.target=0;hover.tracking=false;hover.needsHit=false; }

  function getBaseView() {
    const narrow=width<720;
    const angle=orientation.angle,tilt=orientation.tilt,roll=orientation.roll;
    return {
      x:width*(narrow?.46:profiles.hero.x)+pointer.x*(narrow?2:5),
      y:(narrow?Math.min(height*.34,310):height*profiles.hero.y)+Math.sin(flow*.38)*2+pointer.y*2,
      size:Math.min(width*(narrow?.50:profiles.hero.width),height*profiles.hero.scale)*energy.expansion,
      angle,tilt,roll,
      cy:Math.cos(angle),sy:Math.sin(angle),ct:Math.cos(tilt),st:Math.sin(tilt),cr:Math.cos(roll),sr:Math.sin(roll),
    };
  }
  function paintFloorShadow(view) {
    ctx.save();
    ctx.translate(view.x+view.size*.05,view.y+view.size*.83);
    ctx.scale(view.size*1.04,view.size*.115);
    const shadow=ctx.createRadialGradient(0,0,.02,0,0,1);
    shadow.addColorStop(0,'rgba(39,46,30,.09)');
    shadow.addColorStop(.48,'rgba(39,46,30,.04)');
    shadow.addColorStop(1,'rgba(39,46,30,0)');
    ctx.fillStyle=shadow;ctx.fillRect(-1,-1,2,2);ctx.restore();
  }
  function draw() {
    if(width<2||height<2) return;
    const view=getBaseView();
    const gpu=Boolean(renderer && !renderer.lost);
    // On WebGL, 2D is only the floor shadow, refreshed on resize. The main
    // sculpture goes straight to the visible GPU canvas; no readback/copy.
    if(!gpu || shadowDirty) {
      ctx.setTransform(dpr,0,0,dpr,0,0);
      ctx.clearRect(0,0,width,height);
      paintFloorShadow(view);
      shadowDirty=false;
    }
    updateGeometry(gpu);
    if(hover.tracking && (hover.needsHit || elapsed >= hover.nextCheck)) {
      updateHitTargets(view);hover.needsHit=false;hover.nextCheck=elapsed+.16;
    }
    if(gpu) {
      if(!renderer.draw(view)) { shadowDirty=true; drawFallback(view); }
    } else drawFallback(view);
    drawCount++;
  }

  const canPaint = () => !destroyed && !document.hidden && inViewport && width > 1 && height > 1;
  function canAnimate() { return canPaint() && !paused && !media.matches; }
  function frame(now) {
    raf=0;
    if(!canAnimate()) return;
    const interval = 1000 / targetFps();
    if (lastPaint && now - lastPaint < interval - 1) {
      raf = requestAnimationFrame(frame);
      return;
    }
    const dt=last?Math.min((now-last)/1000,.08):0;
    const ambientStep=dt*ambientSpeed;
    last=now;lastPaint=now;elapsed+=dt;ambientElapsed+=ambientStep;flow+=ambientStep*energy.speed;
    const ease=1-Math.exp(-dt*4.5);
    const turnEase=1-Math.exp(-dt*2.8);
    if(selected==='hero') {
      const angleDelta=Math.atan2(Math.sin(pose.angle-orientation.angle),Math.cos(pose.angle-orientation.angle));
      orientation.angle+=angleDelta*turnEase;
      if(Math.abs(angleDelta)<.0002) orientation.angle=pose.angle;
      orientation.tilt+=(pose.tilt-orientation.tilt)*turnEase;
      orientation.roll+=(pose.roll-orientation.roll)*turnEase;
    } else {
      orientation.angle+=ambientStep*.085;
      orientation.tilt+=(pose.tilt+Math.sin(flow*.14)*.09-orientation.tilt)*turnEase;
      orientation.roll+=(pose.roll+Math.sin(flow*.11)*.045-orientation.roll)*turnEase;
    }
    for(const key of Object.keys(energy)) energy[key]+=(states[selectedState][key]-energy[key])*ease;
    const pointerEase=1-Math.exp(-dt*4);
    pointer.x+=(pointer.targetX-pointer.x)*pointerEase;pointer.y+=(pointer.targetY-pointer.y)*pointerEase;
    hover.amount+=(hover.target-hover.amount)*(1-Math.exp(-dt*9));
    if(!hover.target&&hover.amount<.0001)hover.amount=0;
    const hoverAngle=Math.atan2(Math.sin(hover.targetPhase-hover.phase),Math.cos(hover.targetPhase-hover.phase));
    hover.phase+=hoverAngle*(1-Math.exp(-dt*10));
    audioLevel*=Math.exp(-dt*4.2);
    // Drop expired waves without allocating a new array each animation frame.
    for(let i=waves.length-1;i>=0;i--)if(elapsed-waves[i].start>=3.2)waves.splice(i,1);
    const paintStarted = performance.now();
    draw();
    const duration = performance.now() - paintStarted;
    avgPaintMs = avgPaintMs ? avgPaintMs * .92 + duration * .08 : duration;
    // React slowly to sustained load; never oscillate quality on a single frame.
    if (++sampledFrames % 90 === 0) {
      // Pick a steady refresh divisor rather than fractional frame cadences;
      // the camera transform stays smooth even if a slower device needs 30 Hz
      // geometry updates. Change only after sustained load, with hysteresis.
      if(renderer && !renderer.lost && avgPaintMs > 15) adaptivePenalty=30;
      else if(renderer && !renderer.lost && avgPaintMs < 8) adaptivePenalty=0;
      else if(!renderer || renderer.lost) adaptivePenalty=avgPaintMs>31?10:0;
    }
    raf=requestAnimationFrame(frame);
  }
  let staticPaint=0;
  let inViewport=typeof IntersectionObserver==='undefined';
  function sync() {
    if(destroyed) return;
    if(raf) cancelAnimationFrame(raf);
    if(staticPaint) cancelAnimationFrame(staticPaint);
    raf=0;staticPaint=0;last=0;lastPaint=0;
    if(paused||media.matches) {
      Object.assign(energy,states[selectedState]);
      pointer.x=0;pointer.y=0;audioLevel=0;waves.length=0;hover.amount=0;clearHover();glitchStart=-Infinity;glitchStrength=0;
      if(selected==='hero') Object.assign(orientation,pose);
    }
    positionCamera();setRendererLabel();
    // Avoid a full synchronous 272-piece paint inside route/auth handlers.
    if(canAnimate()) raf=requestAnimationFrame(frame);
    else if(canPaint()) staticPaint=requestAnimationFrame(()=>{staticPaint=0; if(canPaint())draw();});
  }
  function resize() {
    if(destroyed) return;
    // CSS camera transforms change the visual bounding rectangle but not
    // backing-store dimensions. clientWidth/Height avoids resize feedback.
    const nextWidth=Math.max(1,canvas.clientWidth),nextHeight=Math.max(1,canvas.clientHeight);
    const nextDpr=Math.min(window.devicePixelRatio||1,nextWidth<720?1.3:1.35);
    if(width===nextWidth&&height===nextHeight&&dpr===nextDpr&&mesh.length&&drawCount>0) return;
    width=nextWidth;height=nextHeight;dpr=nextDpr;
    canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
    shadowDirty=true;
    sync();
  }
  function clearGlitch() { glitchStart=-Infinity; glitchStrength=0; }
  function triggerGlitch(strength=1) {
    if(destroyed || paused || media.matches || document.hidden) return;
    glitchStart=elapsed;
    glitchStrength=Math.max(.2,Math.min(1.35,Number(strength)||1));
    lastInteraction='signal-glitch';
    // The existing physical impact wave remains independent of packet loss.
  }
  function pulse(strength=1,{kind='click',origin}={}) {
    if(destroyed||paused||media.matches||document.hidden) return;
    const amount=Math.max(0,Math.min(2,Number(strength)||0));if(!amount) return;
    const phase=Number.isFinite(origin)?origin:hover.target>0?hover.targetPhase:1.5+pointer.x*.7;
    lastInteraction=String(kind);
    waves.push({start:elapsed,origin:phase,strength:amount,kind:lastInteraction});if(waves.length>4) waves.shift();
  }
  width=Math.max(1,canvas.clientWidth);height=Math.max(1,canvas.clientHeight);
  makeBlockMesh();
  renderer=buildRenderer();
  const observer=typeof ResizeObserver!=='undefined'?new ResizeObserver(resize):null;
  if(observer) observer.observe(canvas);else window.addEventListener('resize',resize,{passive:true});
  const visibility=typeof IntersectionObserver!=='undefined'?new IntersectionObserver(([entry])=>{
    const visible=entry.isIntersecting && entry.intersectionRatio>0;
    if(visible!==inViewport){inViewport=visible;sync();}
  }):null;
  visibility?.observe(layer);
  document.addEventListener('visibilitychange',sync);
  if(media.addEventListener) media.addEventListener('change',sync);else media.addListener(sync);
  canvas.setAttribute('aria-hidden','true');resize();
  return {
    setMode(next) {
      if(destroyed||!profiles[next]||next===selected)return;
      selected=next;
      positionCamera();setRendererLabel();
      if(!canAnimate()) {
        if(selected==='hero')Object.assign(orientation,pose);
        sync();
      }
    },
    setState(next) {
      if(destroyed||!states[next]||next===selectedState)return;selectedState=next;
      if(next!=='idle')pulse(next==='error'?1.8:1.2,{kind:next});
      if(!canAnimate()){Object.assign(energy,states[selectedState]);sync();}
    },
    setPointer(x,y) {
      if(destroyed||paused||media.matches)return;
      pointer.targetX=Math.max(-1,Math.min(1,Number(x)||0));pointer.targetY=Math.max(-1,Math.min(1,Number(y)||0));
    },
    setAudioLevel(value) {
      if(destroyed||paused||media.matches||document.hidden)return;
      audioLevel=Math.max(audioLevel,Math.max(0,Math.min(1,Number(value)||0)));
    },
    setNavigationBudget(durationMs=900) {
      if(destroyed || paused || media.matches || document.hidden)return;
      // Keep a diagnostic flag, but do not lower frame cadence during route
      // travel. The compositor independently interpolates position at 60Hz.
      const seconds=Math.min(2,Math.max(0,Number(durationMs)||0)/1000);
      navigationBudgetUntil=Math.max(navigationBudgetUntil,elapsed+seconds);
    },
    pulse,triggerGlitch,clearGlitch,hoverAt,clearHover,
    setPaused(value){if(destroyed||Boolean(value)===paused)return;paused=Boolean(value);sync();},
    getStats(){return {renderer:renderer&&!renderer.lost?'webgl-solid':'canvas-solid',running:Boolean(raf),frames:drawCount,targetFps:targetFps(),averagePaintMs:Number(avgPaintMs.toFixed(2)),adaptivePenalty,mode:selected,compositorCamera:{...camera},state:selectedState,fallback:fallbackReason,navigationBudgetActive:elapsed<navigationBudgetUntil,pose:{...orientation},rotation:selected!=='hero'&&canAnimate(),fixedPose:selected==='hero',activeWaves:waves.length,structure:'cuboids',blockCount,facesPerBlock:6,vertices:vertexData.length/vertexStride,triangles:indexData.length/3,occupancy,largeFaceAspect,largeFaceWidth:largeFaceHalfWidth*2,selfSpinAxis:'path-tangent',selfSpinning:canAnimate(),minNeighbourGap:minimumGap,flowOffset,flowing:canAnimate(),hoverStrength:hover.amount,lastInteraction,maxWaveStrength:waves.reduce((max,wave)=>Math.max(max,wave.strength),0),glitchActive:glitchAmount()>0,glitchLevel:Number(glitchAmount().toFixed(3)),visibleFaces:renderer&&!renderer.lost?null:visibleFaces};},
    getGeometrySnapshot() {
      return {structure:'cuboids',blockCount,stride:vertexStride,attributes:['aPosition','aNormal','aMaterial'],
        vertices:Array.from(vertexData),indices:Array.from(indexData),pose:{...orientation},
        blocks:mesh.map(block=>({center:[...block.center],axes:block.axes.map(axis=>[...axis]),pathTangent:[...block.pathTangent],half:[...block.half],spin:block.spin,spinRate:block.spinRate}))};
    },
    destroy() {
      if(destroyed)return;destroyed=true;
      if(raf)cancelAnimationFrame(raf);
      if(staticPaint)cancelAnimationFrame(staticPaint);
      raf=0;staticPaint=0;
      observer?.disconnect();visibility?.disconnect();window.removeEventListener('resize',resize);document.removeEventListener('visibilitychange',sync);
      if(media.removeEventListener)media.removeEventListener('change',sync);else media.removeListener(sync);
      renderer?.dispose();renderer=null;ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,canvas.width,canvas.height);
      mesh.length=0;facets.length=0;waves.length=0;clearHover();
      layer.replaceWith(canvas);
    },
  };
}
