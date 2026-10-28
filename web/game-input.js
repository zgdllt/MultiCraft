// MultiCraft's adapter for the pinned Selkies 2.1 input object. Third-party
// sources stay unchanged; actual browser lock state is the source of truth.
const F8_KEYSYM = 65477;
const F9_KEYSYM = 65478;

// The WS renderer can replace the main canvas with a decoded video or worker
// canvas. Its hidden main canvas can retain an earlier backing-buffer size.
// Pick a visible, decoded sink before asking Selkies to do its own box math.
export function visibleDecodedSink(child,canvas,video) {
  const document=child.document;
  const candidates=[document.getElementById('videoStream'),document.getElementById('videoWorkerCanvas'),canvas,video];
  for(const node of [...new Set(candidates)]){
    if(!node||node.isConnected===false)continue;
    const style=child.getComputedStyle?.(node)||node.style;
    if(style?.display==='none'||style?.visibility==='hidden'||style?.visibility==='collapse'||style?.opacity==='0')continue;
    const rect=node.getBoundingClientRect?.();
    if(!(rect?.width>0&&rect?.height>0))continue;
    const isVideo=node.tagName==='VIDEO';
    const width=isVideo?node.videoWidth:node.width,height=isVideo?node.videoHeight:node.height;
    if(width>0&&height>0)return node;
  }
  return null;
}

export async function nativeCursorVisible(child,payload) {
  if(!payload?.curdata||Number(payload.handle)===0||payload.visible===false)return false;
  if(typeof child.createImageBitmap!=='function'||typeof child.Blob!=='function')return true;
  let bitmap;
  try{
    const binary=child.atob(payload.curdata);
    if(binary.length>262144)return true;
    bitmap=await child.createImageBitmap(new child.Blob([Uint8Array.from(binary,c=>c.charCodeAt(0))],{type:'image/png'}));
    if(bitmap.width>512||bitmap.height>512)return true;
    const canvas=typeof child.OffscreenCanvas==='function'?new child.OffscreenCanvas(bitmap.width,bitmap.height):child.document.createElement('canvas');
    canvas.width=bitmap.width;canvas.height=bitmap.height;
    const graphics=canvas.getContext('2d',{willReadFrequently:true});graphics.drawImage(bitmap,0,0);
    const pixels=graphics.getImageData(0,0,bitmap.width,bitmap.height).data;
    for(let index=3;index<pixels.length;index+=4)if(pixels[index]>0)return true;
    return false;
  }catch{return true;}finally{bitmap?.close?.();}
}

export function createGameInputController({iframe, outerWindow = window, onStatus = () => {}, inspectCursor=nativeCursorVisible}) {
  let binding, keyboardLock = 'inactive', lastError = '', entering = false, operation=0, wantedLock=null;
  let keyboardEpoch=0;
  let wantedGame=false,cursorVisible=null,cursorRevision=0,needsWorldClick=false,needsPointerClick=false;
  let inputMode='auto',pointerBlocked=false,keyboardError=null,pointerError=null;
  let menuPress=null;
  const wait = (ms) => new Promise(resolve => outerWindow.setTimeout(resolve, ms));

  function context() {
    try {
      const child = iframe.contentWindow;
      const input = child?.webrtcInput;
      const document = child?.document;
      if (!input?.element || !document) return null;
      return {child, input, document};
    } catch { return null; }
  }

  function snapshot() {
    const ctx = context();
    if (!ctx) return {ready:false, pointerLocked:false, fullscreen:false, keyboardLock, error:lastError};
    const {input, document, child} = ctx;
    const targets = [input.element, ...['videoCanvas','videoWorkerCanvas','videoStream','stream'].map(id=>document.getElementById(id))];
    const pointerLocked = !!document.pointerLockElement && targets.includes(document.pointerLockElement);
    const fullscreen = !!document.fullscreenElement?.contains(input.element);
    const canvas = document.getElementById('videoCanvas');
    const rect = canvas?.getBoundingClientRect?.() || input.element.getBoundingClientRect?.();
    const streamBox=input._streamBox?.()||null;
    return {ready:true, pointerLocked, fullscreen, keyboardLock, gamingMode:!!input.gamingMode,
      viewerTopLevel:outerWindow.top===outerWindow,
      nativeCursorVisible:cursorVisible,guiMode:wantedLock==='gui'||inputMode==='gui'||(cursorVisible===null&&!pointerLocked),needsWorldClick,needsPointerClick,wantedGame,
      inputMode,pointerBlocked,keyboardError,pointerError,
      cursorFallback:!pointerLocked&&cursorVisible!==true&&(cursorVisible===null||inputMode==='gui'||pointerBlocked),
      cursorOverlayVisible:binding?.cursorImage?.style.display==='block',
      inputAttached:!!input.isInputAttached?.(), pointerTarget:document.pointerLockElement?.id || null,
      pixelWidth:canvas?.width || null, pixelHeight:canvas?.height || null,
      viewRect:rect ? {left:rect.left,top:rect.top,width:rect.width,height:rect.height} : null,
      devicePixelRatio:child.devicePixelRatio || 1,
      streamBox,mappedSink:binding?.mappedSink||null,lastAbsolute:binding?.lastAbsolute||null,error:lastError};
  }

  function report() {
    const state=snapshot();
    onStatus(state);return state;
  }

  function renderCursor() {
    const ctx=context();if(!binding||!ctx)return;
    const {cursorImage,mousePoint,mouseInside}=binding;
    if(ctx.input.cursorDiv)ctx.input.cursorDiv.style.display='none';
    ctx.input.element.style?.setProperty?.('cursor',snapshot().cursorFallback?'default':'none','important');
    if(!cursorImage)return;
    const visible=cursorVisible===true&&!snapshot().pointerLocked&&mouseInside&&cursorImage.naturalWidth>0;
    cursorImage.style.display=visible?'block':'none';
    if(!visible)return;
    const density=ctx.input._cursorDensity?.()||1;
    cursorImage.style.width=`${cursorImage.naturalWidth/density}px`;
    cursorImage.style.height=`${cursorImage.naturalHeight/density}px`;
    cursorImage.style.left=`${mousePoint.x-(Number(binding.cursorPayload?.hotx)||0)/density}px`;
    cursorImage.style.top=`${mousePoint.y-(Number(binding.cursorPayload?.hoty)||0)/density}px`;
  }

  function cancelIfStale(id,ctx) {
    if(id===operation)return false;
    // Do not tear down a newer request for the same document. A cancelled
    // request may otherwise complete after F8 and silently re-lock the user.
    if(wantedLock!==true||context()?.document!==ctx.document){
      try{if(ctx.document.pointerLockElement)ctx.document.exitPointerLock?.();}catch{}
      if(wantedLock!=='gui'||context()?.document!==ctx.document){
        if(ctx.document.fullscreenElement)Promise.resolve(ctx.document.exitFullscreen?.()).catch(()=>{});
        if(wantedLock!==true)outerWindow.navigator?.keyboard?.unlock?.();
      }
    }
    return true;
  }

  function release({leaveFullscreen=true} = {}) {
    finishMenuPress();
    const ctx = context();
    operation++;keyboardEpoch++;wantedGame=false;needsWorldClick=false;needsPointerClick=false;wantedLock=false;entering=false;keyboardLock='inactive';lastError='';inputMode='gui';
    if (ctx) {
      const {input, document}=ctx;
      // Reset keys and buttons before leaving relative mode, including F8
      // key-up which must never reach Minecraft as a cinematic-camera toggle.
      input.resetKeyboard?.(); input.buttonMask=0;
      input.send?.('m2,0,0,0,0'); input.send?.('kr');
      input.gamingMode=false;input.ongamingmode?.(false);
      outerWindow.navigator?.keyboard?.unlock?.();
      if (document.pointerLockElement) document.exitPointerLock?.();
      if (leaveFullscreen && document.fullscreenElement) {
        Promise.resolve(document.exitFullscreen?.()).catch(()=>{});
      }
    }
    renderCursor();return report();
  }

  function useGuiPointer() {
    const ctx=context();operation++;entering=false;wantedGame=false;wantedLock='gui';needsWorldClick=false;needsPointerClick=false;
    inputMode='gui';lastError='';
    if(ctx){
      const {input,document}=ctx;input.gamingMode=false;input.ongamingmode?.(false);
      input.resetKeyboard?.();input.buttonMask=0;input.send?.('m2,0,0,0,0');input.send?.('kr');
      if(document.pointerLockElement)document.exitPointerLock?.();
    }
    renderCursor();return report();
  }

  function finishMenuPress() {
    if(!menuPress)return;
    const press=menuPress;menuPress=null;
    outerWindow.clearTimeout?.(press.timer);
    try{press.input.send?.('ku,65307');}catch{}
  }

  function sendMenuKey() {
    const ctx=context();
    if(!ctx?.input?.send){lastError='游戏输入尚未就绪，暂时无法发送菜单键。';report();return false;}
    finishMenuPress();
    const press={input:ctx.input,timer:null};
    try{ctx.input.send('kd,65307');}
    catch{lastError='游戏连接未就绪，菜单键尚未发送。';report();return false;}
    menuPress=press;
    press.timer=outerWindow.setTimeout(()=>{if(menuPress===press)finishMenuPress();},60);
    return true;
  }

  const onKey = (event) => {
    if (event.code !== 'F8'&&event.code !== 'F9') return; // Physical Escape remains untouched.
    event.preventDefault();event.stopImmediatePropagation();
    if (event.type === 'keydown' && !event.repeat){if(event.code==='F8')release();else sendMenuKey();}
  };

  async function requestEscape(ctx,id) {
    if(!ctx.document.fullscreenElement)return;
    if(keyboardLock==='active')return;
    const keyboard=outerWindow.navigator?.keyboard;
    if(typeof keyboard?.lock!=='function'){keyboardLock='unsupported';return;}
    const epoch=keyboardEpoch;
    keyboardLock='requesting-api';
    try{
      await keyboard.lock(['Escape']);
      if(epoch!==keyboardEpoch||(!wantedGame&&wantedLock!=='gui')||context()?.document!==ctx.document||!ctx.document.fullscreenElement){
        if(!wantedGame&&wantedLock!=='gui')keyboard.unlock?.();return;
      }
      keyboardLock='active';keyboardError=null;
    }catch(error){
      if(epoch!==keyboardEpoch||(!wantedGame&&wantedLock!=='gui')||context()?.document!==ctx.document)return;
      keyboardLock='denied';keyboardError={name:error?.name||'Error',message:error?.message||String(error)};
      lastError='当前浏览器没有允许保留 Esc；F8 可释放，F9 或菜单按钮仍可打开 Minecraft 菜单。';
    }
    report();
  }

  function suspendForGui(ctx) {
    if(!wantedGame)return report();
    if(wantedLock==='gui'&&!ctx.document.pointerLockElement)return report();
    operation++;entering=false;wantedLock='gui';needsWorldClick=false;needsPointerClick=false;lastError='';
    const {input,document}=ctx;
    input.resetKeyboard?.();input.buttonMask=0;input.send?.('m2,0,0,0,0');input.send?.('kr');
    // The public setter also unlocks the keyboard. GUI mode deliberately
    // keeps fullscreen + Escape capture while switching only pointer mode.
    input.gamingMode=false;input.ongamingmode?.(false);
    if(document.pointerLockElement)document.exitPointerLock?.();
    if(document.fullscreenElement&&keyboardLock==='inactive')requestEscape(ctx,operation);
    return report();
  }

  const onWorldClick=(event)=>{
    if(event.button!==0||!wantedGame||pointerBlocked||inputMode==='gui'||(!needsWorldClick&&!needsPointerClick)||cursorVisible!==false)return;
    const ctx=context();
    const surfaces=ctx?[ctx.input.element,...['videoCanvas','videoWorkerCanvas','videoStream','stream'].map(id=>ctx.document.getElementById(id))]:[];
    if(!event.target||!surfaces.includes(event.target))return;
    event.preventDefault();event.stopImmediatePropagation();
    enter().catch(error=>{lastError=error.message;report();});
  };

  function unbind() {
    if (!binding) return;
    finishMenuPress();
    operation++;keyboardEpoch++;cursorRevision++;wantedGame=false;needsWorldClick=false;needsPointerClick=false;cursorVisible=null;wantedLock=false;entering=false;
    const {child,input,document,send,wrappedSend,sinkBox,wrappedSinkBox,updateCursor,wrappedCursor,browserCursors,updateBrowserCursor,hideBrowserCursor,onChange,onMove,onLeave,cursorImage,controls}=binding;
    child.removeEventListener('keydown',onKey,true);child.removeEventListener('keyup',onKey,true);
    document.removeEventListener('pointerlockchange',onChange);
    document.removeEventListener('fullscreenchange',onChange);
    document.removeEventListener('pointerlockerror',onChange);
    document.removeEventListener('pointerdown',onWorldClick,true);
    document.removeEventListener('mousemove',onMove,true);
    document.removeEventListener('pointermove',onMove,true);
    document.removeEventListener('mouseleave',onLeave,true);document.removeEventListener('pointerleave',onLeave,true);
    child.removeEventListener('blur',onLeave);
    if (input.send===wrappedSend) input.send=send;
    if (input._sinkBox===wrappedSinkBox) input._sinkBox=sinkBox;
    if(input.updateServerCursor===wrappedCursor)input.updateServerCursor=updateCursor;
    if(input._updateBrowserCursor===hideBrowserCursor)input._updateBrowserCursor=updateBrowserCursor;
    input.use_browser_cursors=browserCursors;
    cursorImage?.remove?.();controls?.remove?.();
    binding=null;
  }

  function bind() {
    const ctx=context();
    if (!ctx) return report();
    if (binding?.input===ctx.input && binding?.document===ctx.document) return report();
    unbind();
    const {child,input,document}=ctx;
    const send=input.send;
    const wrappedSend=function(message,...rest){
      const parts=typeof message==='string'?message.split(','):[];
      // Selkies already has capture listeners when our adapter loads. Filter
      // their reserved F8/F9 wire events too, even when that listener runs first.
      if ((parts[0]==='kd'||parts[0]==='ku') && [F8_KEYSYM,F9_KEYSYM].includes(Number(parts[1]))) return;
      if(parts[0]==='m'&&binding?.input===input&&Number.isFinite(Number(parts[1]))&&Number.isFinite(Number(parts[2])))binding.lastAbsolute={x:Number(parts[1]),y:Number(parts[2])};
      return send?.call(this,message,...rest);
    };
    input.send=wrappedSend;
    const sinkBox=input._sinkBox;
    const wrappedSinkBox=typeof sinkBox==='function'?function(canvas,video){
      const node=visibleDecodedSink(child,canvas,video);
      // A hidden canvas must never borrow a live video's rect while keeping
      // its own stale pixels. Pass only the selected sink; retain upstream
      // VIDEO object-fit/letterbox math and avoid a second coordinate scale.
      const box=node?sinkBox.call(this,node.tagName==='VIDEO'?null:node,node.tagName==='VIDEO'?node:null,true):null;
      if(binding?.input===input)binding.mappedSink=box?{id:node.id||'',tag:node.tagName,width:box.sinkW,height:box.sinkH,boxLeft:box.boxLeft,boxTop:box.boxTop,boxW:box.boxW,boxH:box.boxH}:null;
      return box;
    }:undefined;
    if(wrappedSinkBox)input._sinkBox=wrappedSinkBox;
    const browserCursors=input.use_browser_cursors;
    input.use_browser_cursors=true;
    const updateBrowserCursor=input._updateBrowserCursor;
    const hideBrowserCursor=()=>{input.element.style?.setProperty?.('cursor',snapshot().cursorFallback?'default':'none','important');};
    input._updateBrowserCursor=hideBrowserCursor;
    let cursorImage,controls;
    if(typeof document.createElement==='function'&&document.body?.append){
      cursorImage=document.createElement('img');cursorImage.id='multicraft-native-cursor';cursorImage.alt='';cursorImage.setAttribute('aria-hidden','true');
      cursorImage.style.cssText='position:fixed;pointer-events:none;z-index:2147483647;display:none;max-width:none;max-height:none;object-fit:fill;';
      controls=document.createElement('div');controls.id='multicraft-input-controls';
      controls.style.cssText='position:fixed;right:10px;bottom:10px;z-index:2147483647;display:flex;gap:6px;pointer-events:auto;';
      for(const [label,action] of [
        ['尝试锁定鼠标',()=>enter({forcePointer:true}).catch(error=>{lastError=error.message;report();})],
        ['普通鼠标 / GUI',useGuiPointer],
        ['MC 菜单 / F9',sendMenuKey],
        ['释放 / F8',()=>release()],
      ]){
        const button=document.createElement('button');button.type='button';button.textContent=label;
        button.style.cssText='border:1px solid #9bc5a3;border-radius:5px;background:rgba(12,32,18,.88);color:#e1f4e4;font:12px sans-serif;padding:8px 10px;cursor:pointer;';
        button.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();action();});controls.append(button);
      }
      document.body.append(cursorImage,controls);
    }
    const updateCursor=input.updateServerCursor;
    const observeCursor=async(payload,revision)=>{
      const visible=await inspectCursor(child,payload);
      if(revision!==cursorRevision||binding?.input!==input)return;
      cursorVisible=visible;
      if(visible){needsWorldClick=false;if(wantedGame)suspendForGui(ctx);}
      else if(wantedGame&&wantedLock==='gui'){
        needsWorldClick=true;
        if(!pointerBlocked&&inputMode!=='gui'&&(child.navigator?.userActivation?.isActive||outerWindow.navigator?.userActivation?.isActive)){
          enter().catch(error=>{lastError=error.message;report();});
        }
      }
      renderCursor();
      report();
    };
    const wrappedCursor=function(payload){
      const revision=++cursorRevision;
      if(binding?.input===input){
        binding.cursorPayload=payload;
        if(cursorImage&&payload.curdata){
          cursorImage.onload=()=>{if(revision!==cursorRevision)return;renderCursor();report();};
          cursorImage.src='data:image/png;base64,'+payload.curdata;
        }
      }
      const result=updateCursor?.call(this,payload);
      Promise.resolve(result).then(()=>{
        if(this.cursorDiv)this.cursorDiv.style.display='none';
        if(revision!==cursorRevision||binding?.input!==input)return;
        // Native PNG/CSS cursor only; do not draw an additional cursorDiv.
        return observeCursor(payload,revision);
      }).catch(()=>{});
      return result;
    };
    input.updateServerCursor=wrappedCursor;
    const onChange=()=>{
      if(wantedLock===false||wantedLock==='gui'){
        if(document.pointerLockElement){document.exitPointerLock?.();return;}
        if(wantedLock===false&&document.fullscreenElement){Promise.resolve(document.exitFullscreen?.()).catch(()=>{});return;}
      }
      if (!document.fullscreenElement){keyboardEpoch++;keyboardLock='inactive';}
      if(wantedLock==='gui'&&document.fullscreenElement&&!entering&&keyboardLock==='inactive')requestEscape(ctx,operation);
      renderCursor();report();
    };
    const onMove=(event)=>{
      if(binding?.input!==input)return;
      const surfaces=[input.element,...['videoCanvas','videoWorkerCanvas','videoStream','stream'].map(id=>document.getElementById(id))];
      binding.mouseInside=surfaces.includes(event.target);
      if(binding.mouseInside)binding.mousePoint={x:event.clientX,y:event.clientY};
      renderCursor();
    };
    const onLeave=()=>{if(binding?.input!==input)return;binding.mouseInside=false;renderCursor();report();};
    child.addEventListener('keydown',onKey,true);child.addEventListener('keyup',onKey,true);
    document.addEventListener('pointerlockchange',onChange);
    document.addEventListener('fullscreenchange',onChange);
    document.addEventListener('pointerlockerror',onChange);
    document.addEventListener('pointerdown',onWorldClick,true);
    document.addEventListener('mousemove',onMove,true);document.addEventListener('pointermove',onMove,true);
    document.addEventListener('mouseleave',onLeave,true);document.addEventListener('pointerleave',onLeave,true);child.addEventListener('blur',onLeave);
    binding={child,input,document,send,wrappedSend,sinkBox,wrappedSinkBox,updateCursor,wrappedCursor,browserCursors,updateBrowserCursor,hideBrowserCursor,onChange,onMove,onLeave,cursorImage,controls,mousePoint:{x:0,y:0},mouseInside:false,cursorPayload:null};
    // Fit the actual decoded canvas and let Selkies recompute its own input
    // transform. Avoid inventing a second coordinate scale in the wrapper.
    child.postMessage({type:'setScaleLocally',value:true},outerWindow.location.origin);
    input.resize?.();
    if(input._cursorBase64Data)wrappedCursor.call(input,{handle:1,curdata:input._cursorBase64Data});
    renderCursor();
    return report();
  }

  async function enter({forcePointer=false}={}) {
    if(entering)throw Error('鼠标锁定请求仍在进行，请先完成浏览器权限选择。');
    bind();
    const ctx=context();
    if (!ctx) throw Error('游戏输入尚未就绪，请等画面加载后重试。');
    const {input,document}=ctx;
    if (typeof input.element.requestPointerLock!=='function') {
      lastError='当前浏览器或媒体客户端不支持鼠标锁定。';report();throw Error(lastError);
    }
    if (typeof document.documentElement.requestFullscreen!=='function') {
      lastError='当前浏览器不支持游戏全屏，无法可靠保留 Esc。';report();throw Error(lastError);
    }
    const id=++operation;
    wantedGame=true;inputMode='game';pointerBlocked=false;needsWorldClick=false;entering=true;lastError='';pointerError=null;
    input.attach_context?.();input.resize?.();input.element.focus?.();
    input.gamingMode=false;input.ongamingmode?.(false);
    // Deliberately split the two browser permissions into two genuine user
    // gestures. Calling enterGamingMode here would auto-arm a second pointer
    // request during fullscreenchange in Selkies and race Chromium's first.
    if(!snapshot().fullscreen){
      wantedLock=cursorVisible===true?'gui':'pending';needsPointerClick=cursorVisible===false;
      try{await document.documentElement.requestFullscreen({navigationUI:'hide'});}
      catch{entering=false;lastError='浏览器没有允许游戏全屏，请检查站点权限后重试。';report();throw Error(lastError);}
      if(cancelIfStale(id,ctx))return snapshot();
      await requestEscape(ctx,id);
      if(cancelIfStale(id,ctx))return snapshot();
      entering=false;return report();
    }
    if(cursorVisible===true){
      wantedLock='gui';needsPointerClick=false;await requestEscape(ctx,id);
      if(cancelIfStale(id,ctx))return snapshot();entering=false;return report();
    }
    if(cursorVisible===null&&!forcePointer){entering=false;useGuiPointer();return report();}
    wantedLock=true;needsPointerClick=false;
    let pointerRequest;
    try{pointerRequest=Promise.resolve(input.element.requestPointerLock());}
    catch(error){pointerRequest=Promise.reject(error);}
    pointerRequest.then(()=>cancelIfStale(id,ctx),()=>{});
    try{await pointerRequest;}catch(error){
      if(cancelIfStale(id,ctx))return snapshot();
      pointerError={name:error?.name||'Error',message:error?.message||String(error)};
      entering=false;pointerBlocked=true;useGuiPointer();lastError='鼠标锁定未获准，已切换普通鼠标；GUI 点击仍可用。可在独立浏览器中尝试锁定。';
      report();throw Error(lastError);
    }
    for(let i=0;i<20&&!snapshot().pointerLocked;i++){await wait(50);if(cancelIfStale(id,ctx))return snapshot();}
    if(cancelIfStale(id,ctx))return snapshot();
    if(!snapshot().pointerLocked){
      entering=false;pointerBlocked=true;useGuiPointer();lastError='鼠标尚未锁定，已保留普通鼠标操作；请使用明确的锁定按钮重试。';report();throw Error(lastError);
    }
    input.gamingMode=true;input.ongamingmode?.(true);
    await requestEscape(ctx,id);if(cancelIfStale(id,ctx))return snapshot();
    entering=false;return report();
  }

  outerWindow.addEventListener('keydown',onKey,true);
  outerWindow.addEventListener('keyup',onKey,true);
  return {bind,enter,release,useGuiPointer,sendMenuKey,snapshot,dispose(){release();unbind();outerWindow.removeEventListener('keydown',onKey,true);outerWindow.removeEventListener('keyup',onKey,true);}};
}
