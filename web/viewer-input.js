/** One canvas, one fenced native connection. Pointer capture never implies fullscreen. */
const scans = {Enter:40,Escape:41,Backspace:42,Tab:43,Space:44,Minus:45,Equal:46,BracketLeft:47,BracketRight:48,Backslash:49,Semicolon:51,Quote:52,Backquote:53,Comma:54,Period:55,Slash:56,CapsLock:57,PrintScreen:70,ScrollLock:71,Pause:72,Insert:73,Home:74,PageUp:75,Delete:76,End:77,PageDown:78,ArrowRight:79,ArrowLeft:80,ArrowDown:81,ArrowUp:82,ControlLeft:224,ShiftLeft:225,AltLeft:226,MetaLeft:227,ControlRight:228,ShiftRight:229,AltRight:230,MetaRight:231};
for(let n=0;n<26;n++)scans[`Key${String.fromCharCode(65+n)}`]=4+n;
for(let n=1;n<10;n++)scans[`Digit${n}`]=29+n;
scans.Digit0=39;
for(let n=1;n<=12;n++)scans[`F${n}`]=57+n;
export function keyEvent(code,action,event={}){
  const scancode=scans[code];if(scancode===undefined)return;
  const printable=typeof event.key==='string'&&[...event.key].length===1;
  const keycode=printable?event.key.codePointAt(0):({Enter:13,Escape:27,Backspace:8,Tab:9,Space:32,Delete:127}[code]??(0x40000000|scancode));
  return {type:'key',scancode,keycode,action,modifiers:(event.shiftKey?1:0)|(event.ctrlKey?64:0)|(event.altKey?256:0)|(event.metaKey?1024:0)};
}
export function pointerPosition(canvas,event,width=canvas.width,height=canvas.height){
  // The canvas itself is fitted inside its container; black bars are outside its rect.
  // Native coordinates refer to the render surface, independent of CSS scale/DPR.
  const box=canvas.getBoundingClientRect();
  return {type:'move',x:Math.max(0,Math.min(width-1,(event.clientX-box.left)*width/box.width)),
    y:Math.max(0,Math.min(height-1,(event.clientY-box.top)*height/box.height)),
    dx:(event.movementX||0)*width/box.width,dy:(event.movementY||0)*height/box.height};
}
export function createViewerInput({canvas,doc=canvas.ownerDocument,win=doc.defaultView,send,onStatus=()=>{},startAudio=()=>{}}){
  let native={},owner,controller,epoch=0,seq=0,chain=Promise.resolve(),queue=[],flushing=false,error='',ready=false,stopped=false,captureGeneration=0,successfulCapture;
  const listeners=[];
  function listen(target,event,fn,options){target.addEventListener(event,fn,options);listeners.push(()=>target.removeEventListener(event,fn,options));}
  function snapshot(){return {ready,pointerLocked:doc.pointerLockElement===canvas,fullscreen:!!doc.fullscreenElement,
    guiMode:!!native.screen||native.mouseGrabbed===false,nativeCursorVisible:!!native.screen,keyboardLock:'not-requested',error};}
  function status(){onStatus(snapshot());}
  function report(cause){error=cause?.message||String(cause);status();}
  function input(events){
    if(!ready||stopped||!controller||!Number.isSafeInteger(native.connectionEpoch)||!owner)return Promise.reject(Error('请先连接画面并等待控制权确认'));
    seq=Math.max(seq,native.lastInputSeq||0)+1;
    const value={expectedRuntimeOwner:owner,expectedEpoch:native.connectionEpoch,inputSeq:seq,events},ticket=epoch,generation=controller;
    const pending=chain.catch(()=>{}).then(()=>{if(ticket!==epoch||!ready||stopped)throw Error('旧输入已丢弃');return send(value,generation);});chain=pending;return pending;
  }
  async function release(){captureGeneration++;queue=[];if(doc.pointerLockElement===canvas)doc.exitPointerLock();if(ready)await input([{type:'release'}]).catch(report);status();}
  async function capture(){
    if(!ready)throw Error('等待游戏画面就绪');
    const ticket=epoch,captureTicket=++captureGeneration;let cancelled=false;canvas.focus();
    try{
      // Invoke synchronously while the trusted click still has browser activation.
      // The call itself can throw before returning a permission promise.
      if(typeof canvas.requestPointerLock!=='function')throw new Error('Pointer lock is not supported');
      const capture=Promise.resolve(canvas.requestPointerLock()).then(()=>{
        if(cancelled||captureTicket!==captureGeneration||ticket!==epoch||!ready||stopped||native.screen){
          // The same canvas may already hold a newer, successful capture. An
          // obsolete success must not release that newer capture's browser lock.
          const newerCapture=successfulCapture?.epoch===epoch&&successfulCapture.generation===captureGeneration
            &&successfulCapture.generation>captureTicket&&ready&&!stopped;
          if(doc.pointerLockElement===canvas&&!newerCapture)doc.exitPointerLock();
          throw Error('会话已改变，捕获已释放');
        }
        successfulCapture={epoch:ticket,generation:captureTicket};
      });
      capture.catch(()=>{});
      const audio=Promise.resolve(startAudio());audio.catch(report);await input([{type:'grab'}]);await capture;error='';status();
    }catch(cause){
      cancelled=true;
      const unsupported=cause?.name==='NotSupportedError'||/root document|not valid for pointer lock|pointer lock is not supported/i.test(cause?.message||'');
      const failure=unsupported?new Error('当前窗口不支持鼠标捕获，请在独立 Chrome/Edge 窗口中游玩；菜单仍可用',{cause}):cause;
      // A rejected browser capture can arrive after the native grab was sent.
      // Clean up only this still-current attempt, never a newer capture/connection.
      if(ticket===epoch&&captureTicket===captureGeneration&&ready&&!stopped){
        queue=[];report(failure);
        if(doc.pointerLockElement===canvas)doc.exitPointerLock();
        await input([{type:'release'}]).catch(()=>{});
      }
      throw failure;
    }
  }
  async function fullscreen(){await canvas.parentElement.requestFullscreen();status();}
  async function menu(){captureGeneration++;if(doc.pointerLockElement===canvas)doc.exitPointerLock();await input([keyEvent('Escape',1),keyEvent('Escape',0)]);status();}
  function enqueue(value){if(!ready||!value)return;queue.push(value);if(queue.length>128){queue=[{type:'release'}];report(Error('输入积压，已释放按键'));}}
  async function flush(){if(flushing||!queue.length)return;const ticket=epoch;flushing=true;const values=queue.splice(0,128);try{await input(values);}catch(cause){if(ticket===epoch)report(cause);}finally{if(ticket===epoch)flushing=false;}}
  const timer=win.setInterval(flush,20);
  listen(canvas,'contextmenu',event=>event.preventDefault());
  listen(canvas,'keydown',event=>{
    event.preventDefault();
    if(event.code==='F8'){release();return;}
    if(event.code==='F9'){if(!event.repeat)menu().catch(report);return;}
    enqueue(keyEvent(event.code,event.repeat?-1:1,event));
    if(event.key?.length===1&&!event.isComposing&&!event.ctrlKey&&!event.metaKey&&!event.altKey)enqueue({type:'text',text:event.key});
  });
  listen(canvas,'keyup',event=>{event.preventDefault();if(!['F8','F9'].includes(event.code))enqueue(keyEvent(event.code,0,event));});
  const position=event=>pointerPosition(canvas,event,native.width||canvas.width,native.height||canvas.height);
  listen(canvas,'mousemove',event=>enqueue(position(event)));
  listen(canvas,'mousedown',event=>{event.preventDefault();canvas.focus();Promise.resolve(startAudio()).catch(report);enqueue(position(event));enqueue({type:'button',button:[1,2,3][event.button]||event.button+1,action:1});});
  listen(win,'mouseup',event=>{if(doc.activeElement===canvas)enqueue({type:'button',button:[1,2,3][event.button]||event.button+1,action:0});});
  listen(canvas,'wheel',event=>{event.preventDefault();enqueue({type:'scroll',x:-event.deltaX/100,y:-event.deltaY/100});},{passive:false});
  listen(canvas,'blur',()=>release());
  listen(win,'blur',()=>release());
  listen(doc,'pointerlockchange',()=>{if(doc.pointerLockElement!==canvas&&doc.activeElement===canvas)release();status();});
  listen(doc,'fullscreenchange',status);
  return {capture,fullscreen,menu,release,snapshot,sendText(text){return input([{type:'text',text}]);},async sendChat(text){const ticket=epoch;if(!text||text.length>256)throw Error('聊天文字须为 1–256 字符');await input([keyEvent('KeyT',1,{key:'t'}),{type:'text',text:'t'},keyEvent('KeyT',0,{key:'t'})]);if(ticket!==epoch)throw Error('会话已改变，旧聊天已丢弃');await input([{type:'text',text},keyEvent('Enter',1),keyEvent('Enter',0)]);},update(value,runtimeOwner,generation){
    if(value.connectionEpoch!==native.connectionEpoch||runtimeOwner!==owner||generation!==controller){epoch++;seq=value.lastInputSeq||0;chain=Promise.resolve();queue=[];flushing=false;}
    native=value;owner=runtimeOwner;controller=generation;ready=!!controller&&!value.closed&&!value.failure;stopped=false;
    if(native.screen){captureGeneration++;if(doc.pointerLockElement===canvas)doc.exitPointerLock();}status();
  },unavailable(){epoch++;ready=false;queue=[];chain=Promise.resolve();flushing=false;if(doc.pointerLockElement===canvas)doc.exitPointerLock();status();},stop(){release();stopped=true;ready=false;epoch++;win.clearInterval(timer);for(const remove of listeners)remove();}};
}
