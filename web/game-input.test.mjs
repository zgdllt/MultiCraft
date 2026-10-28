import test from 'node:test';
import assert from 'node:assert/strict';
import {createGameInputController,nativeCursorVisible} from './game-input.js';

function events(object={}){
  const listeners=new Map();
  return Object.assign(object,{
    addEventListener(type,fn){if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(fn);},
    removeEventListener(type,fn){listeners.set(type,(listeners.get(type)||[]).filter(f=>f!==fn));},
    emit(type,extra={}){const event={type,repeat:false,preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};for(const fn of listeners.get(type)||[]){fn(event);if(event.stopped)break;}return event;},
  });
}

function fixture({denyPointer=false,denyKeyboard=false,keyboardAvailable=true,inspectCursor,overlays=false}={}){
  const calls=[], sent=[];
  const document=events({pointerLockElement:null,fullscreenElement:null,getElementById:id=>id==='videoCanvas'?canvas:null});
  const element={id:'overlayInput',focus(){calls.push('focus');},getBoundingClientRect:()=>({left:0,top:0,width:828,height:466}),requestPointerLock(){calls.push('pointer-request');if(denyPointer)return Promise.reject(Error('permission denied'));document.pointerLockElement=element;document.emit('pointerlockchange');return Promise.resolve();}};
  const canvas={id:'videoCanvas',tagName:'CANVAS',width:1920,height:1080,getBoundingClientRect:()=>({left:0,top:0,width:828,height:466})};
  document.documentElement={contains:e=>e===element,requestFullscreen(){calls.push('fullscreen-request');document.fullscreenElement=this;document.emit('fullscreenchange');return Promise.resolve();}};
  document.exitPointerLock=()=>{document.pointerLockElement=null;document.emit('pointerlockchange');};
  document.exitFullscreen=()=>{document.fullscreenElement=null;document.emit('fullscreenchange');return Promise.resolve();};
  const keyboard={async lock(keys){calls.push(['keyboard-lock',keys]);if(denyKeyboard)throw Error('keyboard denied');},unlock(){calls.push('keyboard-unlock');}};
  const outer=events({setTimeout,clearTimeout,location:{origin:'https://example.test'},navigator:keyboardAvailable?{keyboard}:{}});
  const child=events({document,devicePixelRatio:2,navigator:{},postMessage(message){calls.push(['message',message]);}});
  const input={element,gamingMode:false,inputAttached:true,send(message){sent.push(message);},resetKeyboard(){calls.push('reset-keyboard');},resize(){calls.push('resize');},isInputAttached(){return this.inputAttached;},attach_context(){this.inputAttached=true;},enterGamingMode(){calls.push('gaming');this.gamingMode=true;document.documentElement.requestFullscreen();},_setGamingMode(value){this.gamingMode=value;},
    use_browser_cursors:false,cursorDiv:{style:{}},async updateServerCursor(payload){this._cursorBase64Data=payload.curdata;},
    _sinkBox(_canvas,_video,force=false){if(!force)return null;return {boxLeft:0,boxTop:0,boxW:828,boxH:466,sinkW:1920,sinkH:1080};},
    _streamBox(){const box=this._sinkBox(canvas,null);return box?{scaleX:box.sinkW/box.boxW,scaleY:box.sinkH/box.boxH}:null;},
  };
  // Simulate Selkies' pre-existing capture handler running before our listener.
  const keySym=code=>code==='F8'?65477:code==='F9'?65478:65307;
  child.addEventListener('keydown',event=>input.send(`kd,${keySym(event.code)}`),true);
  child.addEventListener('keyup',event=>input.send(`ku,${keySym(event.code)}`),true);
  child.webrtcInput=input;
  const nodes=new Map([['videoCanvas',canvas]]);
  if(overlays){
    document.getElementById=id=>nodes.get(id)||null;
    document.body={append(...elements){for(const node of elements)nodes.set(node.id,node);}};
    document.createElement=tag=>events({tagName:tag.toUpperCase(),style:{},dataset:{},naturalWidth:32,naturalHeight:32,setAttribute(){},append(...children){this.children=(this.children||[]).concat(children);},remove(){nodes.delete(this.id);}});
  }
  const iframe={contentWindow:child};
  const controller=createGameInputController({iframe,outerWindow:outer,...(inspectCursor?{inspectCursor}:{})});
  return {controller,input,document,outer,child,calls,sent,canvas,nodes};
}

async function lock(f){await f.controller.enter({forcePointer:true});return f.controller.enter({forcePointer:true});}

function sinkFixture(){
  const f=fixture({overlays:true}),selected=[];
  // Honor the selected node like the pinned upstream method. In particular,
  // a VIDEO's decoded pixels and object-fit content box are independent of a
  // hidden canvas's buffer. The old fixed-size mock could not catch that bug.
  f.input._sinkBox=(canvas,video,force)=>{
    assert.equal(force,true);
    const node=canvas||video;selected.push(node);
    if(!node)return null;
    const rect=node.getBoundingClientRect();
    const width=node.videoWidth||node.width,height=node.videoHeight||node.height;
    let {left,top,width:boxW,height:boxH}=rect;
    if(node.tagName==='VIDEO'&&(node.style.objectFit||'contain')!=='fill'){
      const scale=Math.min(boxW/width,boxH/height);
      left+=(boxW-width*scale)/2;top+=(boxH-height*scale)/2;boxW=width*scale;boxH=height*scale;
    }
    return {boxLeft:left,boxTop:top,boxW,boxH,sinkW:width,sinkH:height};
  };
  return {...f,selected};
}

test('fullscreen and pointer lock use two gestures without Selkies auto-arm duplication',async()=>{
  const f=fixture();assert.equal(f.controller.snapshot().pointerLocked,false);
  const first=await f.controller.enter();
  assert.equal(first.pointerLocked,false);assert.equal(first.needsPointerClick,false,'unknown cursor metadata must not capture a normal GUI click');
  assert.equal(first.fullscreen,true);assert.equal(f.calls.filter(call=>call==='pointer-request').length,0);
  const state=await f.controller.enter({forcePointer:true});
  assert.equal(state.pointerLocked,true);assert.equal(state.fullscreen,true);assert.equal(state.keyboardLock,'active');
  assert.equal(f.calls.filter(call=>call==='pointer-request').length,1);
  assert.ok(!f.calls.includes('gaming'),'enterGamingMode would create another pointer request');
  f.controller.dispose();
});

test('denied pointer permission is reported instead of a false locked state',async()=>{
  const f=fixture({denyPointer:true});
  await f.controller.enter();
  await assert.rejects(f.controller.enter({forcePointer:true}),/鼠标锁定未获准/);
  assert.equal(f.controller.snapshot().pointerLocked,false);
  assert.equal(f.controller.snapshot().needsPointerClick,false);
  assert.equal(f.controller.snapshot().inputMode,'gui');
  f.controller.dispose();
});

test('F8 releases pointer/keyboard/fullscreen without reaching Minecraft',async()=>{
  const f=fixture();await lock(f);
  const down=f.child.emit('keydown',{code:'F8'});f.child.emit('keyup',{code:'F8'});
  assert.equal(down.prevented,true);assert.equal(f.controller.snapshot().pointerLocked,false);
  assert.equal(f.controller.snapshot().fullscreen,false);assert.equal(f.input.gamingMode,false);
  assert.ok(f.calls.includes('keyboard-unlock'));
  assert.ok(f.sent.includes('kr'));assert.ok(!f.sent.some(text=>text==='kd,65477'||text==='ku,65477'));
  f.controller.dispose();
});

test('Escape remains an ordinary remote Minecraft key',async()=>{
  const f=fixture();await lock(f);
  const event=f.child.emit('keydown',{code:'Escape'});f.child.emit('keyup',{code:'Escape'});
  assert.ok(f.sent.includes('kd,65307'));assert.ok(f.sent.includes('ku,65307'));
  assert.equal(event.prevented,undefined);assert.equal(f.controller.snapshot().pointerLocked,true);
  f.controller.dispose();
});

test('unsupported or denied Keyboard Lock is explicit and does not claim Escape capture',async()=>{
  for(const options of [{keyboardAvailable:false},{denyKeyboard:true}]){
    const f=fixture(options);const state=await lock(f);
    assert.equal(state.pointerLocked,true);assert.notEqual(state.keyboardLock,'active');f.controller.dispose();
  }
});

test('fixed stream GUI mapping uses decoded pixels even when the child retained auto-size mode',()=>{
  const f=fixture();const original=f.input._sinkBox;
  assert.equal(f.input._sinkBox(f.canvas,null),null);
  f.controller.bind();
  const box=f.input._sinkBox(f.canvas,null);
  const remoteX=(414-box.boxLeft)*box.sinkW/box.boxW;
  const remoteY=(233-box.boxTop)*box.sinkH/box.boxH;
  assert.equal(remoteX,960);assert.equal(remoteY,540);
  assert.equal(f.controller.snapshot().streamBox.scaleX,1920/828);
  f.controller.dispose();assert.equal(f.input._sinkBox,original);
});

test('hidden stale canvas never supplies dimensions for a live video mirror',()=>{
  const f=sinkFixture();f.canvas.style={display:'none'};
  const video={id:'videoStream',tagName:'VIDEO',videoWidth:640,videoHeight:360,style:{objectFit:'fill'},getBoundingClientRect:()=>({left:0,top:0,width:1800,height:1012.5})};
  f.nodes.set(video.id,video);f.controller.bind();
  const box=f.input._sinkBox(f.canvas,null);
  assert.equal(f.selected.at(-1),video);
  assert.equal(box.sinkW,640);assert.equal(box.sinkH,360);
  assert.equal((1710-box.boxLeft)*box.sinkW/box.boxW,608);
  assert.equal((729-box.boxTop)*box.sinkH/box.boxH,259.2);
  assert.equal(f.controller.snapshot().mappedSink.id,'videoStream');
  assert.equal(f.controller.snapshot().mappedSink.width,640);
  f.controller.dispose();
});

test('fullscreen video mapping retains upstream top and bottom letterboxes at high DPI',()=>{
  const f=sinkFixture();f.canvas.style={display:'none'};
  const video={id:'videoStream',tagName:'VIDEO',videoWidth:640,videoHeight:360,style:{objectFit:'contain'},getBoundingClientRect:()=>({left:0,top:0,width:1800,height:1130})};
  f.nodes.set(video.id,video);f.controller.bind();
  const box=f.input._sinkBox(f.canvas,null);
  assert.equal(box.boxTop,58.75);assert.equal(box.boxH,1012.5);
  assert.equal((565-box.boxTop)*box.sinkH/box.boxH,180);
  assert.equal((900-box.boxLeft)*box.sinkW/box.boxW,320);
  assert.equal(f.controller.snapshot().devicePixelRatio,2,'coordinates already use decoded physical pixels; do not multiply by DPR again');
  f.controller.dispose();
});

test('mapping follows visible sink switches and resize without retaining an old buffer or rect',()=>{
  const f=sinkFixture();f.controller.bind();
  assert.equal(f.controller.snapshot().mappedSink.id,'videoCanvas');
  let rect={left:10,top:30,width:1280,height:720};
  const video={id:'videoStream',tagName:'VIDEO',videoWidth:640,videoHeight:360,style:{objectFit:'fill'},getBoundingClientRect:()=>rect};
  f.nodes.set(video.id,video);f.canvas.style={display:'none'};
  assert.equal(f.controller.snapshot().mappedSink.width,640);
  rect={left:0,top:60,width:1800,height:1012.5};video.videoWidth=1920;video.videoHeight=1080;
  let state=f.controller.snapshot();assert.equal(state.mappedSink.width,1920);assert.equal(state.mappedSink.boxTop,60);assert.equal(state.mappedSink.boxW,1800);
  video.style.display='none';
  const worker={id:'videoWorkerCanvas',tagName:'CANVAS',width:800,height:600,style:{},getBoundingClientRect:()=>({left:100,top:20,width:1000,height:750})};
  f.nodes.set(worker.id,worker);state=f.controller.snapshot();
  assert.equal(state.mappedSink.id,'videoWorkerCanvas');assert.equal(state.mappedSink.width,800);assert.equal(state.streamBox.scaleX,.8);
  worker.style.display='none';assert.equal(f.controller.snapshot().mappedSink,null,'no visible decoded sink must not reuse a hidden buffer');
  f.controller.dispose();
});

test('F8 cancels an in-flight permission request and rejects late pointer/fullscreen acquisition',async()=>{
  const f=fixture();await f.controller.enter();let grant;
  const keyboardRequests=f.calls.filter(value=>Array.isArray(value)&&value[0]==='keyboard-lock').length;
  f.input.element.requestPointerLock=()=>new Promise(resolve=>{grant=()=>{f.document.pointerLockElement=f.input.element;f.document.emit('pointerlockchange');resolve();};});
  const pending=f.controller.enter({forcePointer:true});
  f.controller.release();grant();
  f.document.fullscreenElement=f.document.documentElement;f.document.emit('fullscreenchange');
  await pending;
  assert.equal(f.controller.snapshot().pointerLocked,false);
  assert.equal(f.controller.snapshot().fullscreen,false);
  assert.equal(f.controller.snapshot().keyboardLock,'inactive');
  assert.equal(f.calls.filter(value=>Array.isArray(value)&&value[0]==='keyboard-lock').length,keyboardRequests);
  f.controller.dispose();
});

const settled=()=>new Promise(resolve=>setTimeout(resolve,0));
test('native GUI cursor releases only the pointer, keeps Escape lock, and resumes after a real world click',async()=>{
  const f=fixture();await lock(f);
  await f.input.updateServerCursor({handle:1,curdata:'native PNG'});await settled();
  let state=f.controller.snapshot();
  assert.equal(state.nativeCursorVisible,true);assert.equal(state.pointerLocked,false);
  assert.equal(state.fullscreen,true);assert.equal(state.keyboardLock,'active');assert.equal(f.input.gamingMode,false);
  const resets=f.calls.filter(call=>call==='reset-keyboard').length;
  await f.input.updateServerCursor({handle:2,curdata:'another native PNG'});await settled();
  assert.equal(f.calls.filter(call=>call==='reset-keyboard').length,resets,'GUI cursor shape changes must not break shift-click or dragging');
  await f.input.updateServerCursor({handle:0,curdata:null});await settled();
  assert.equal(f.controller.snapshot().needsWorldClick,true);
  const event=f.document.emit('pointerdown',{button:0,target:f.input.element});await settled();
  state=f.controller.snapshot();assert.equal(event.prevented,true);assert.equal(state.pointerLocked,true);
  assert.equal(state.keyboardLock,'active');f.controller.dispose();
});

test('F8 prevents later native cursor updates from auto-relocking',async()=>{
  const f=fixture();await lock(f);
  await f.input.updateServerCursor({handle:1,curdata:'visible'});await settled();
  f.controller.release();
  await f.input.updateServerCursor({handle:0,curdata:null});await settled();
  assert.equal(f.controller.snapshot().wantedGame,false);assert.equal(f.controller.snapshot().needsWorldClick,false);
  f.document.emit('pointerdown',{button:0,target:f.input.element});await settled();
  assert.equal(f.controller.snapshot().pointerLocked,false);f.controller.dispose();
});

test('a late transparent-PNG classification cannot override a newer native cursor',async()=>{
  const pending=[];
  const f=fixture({inspectCursor:(_child,payload)=>new Promise(resolve=>pending.push({payload,resolve}))});
  f.controller.bind();
  await f.input.updateServerCursor({handle:1,curdata:'old'});await settled();
  await f.input.updateServerCursor({handle:2,curdata:'new'});await settled();
  pending[1].resolve(true);await settled();pending[0].resolve(false);await settled();
  assert.equal(f.controller.snapshot().nativeCursorVisible,true);f.controller.dispose();
});

test('fully transparent native PNGs are hidden while visible alpha is a GUI cursor',async()=>{
  let alpha=0;
  const child={atob,Blob,async createImageBitmap(){return{width:1,height:1,close(){}};},OffscreenCanvas:class{getContext(){return{drawImage(){},getImageData(){return{data:new Uint8ClampedArray([0,0,0,alpha])};}};}}};
  assert.equal(await nativeCursorVisible(child,{handle:1,curdata:btoa('PNG')}),false);
  alpha=255;
  assert.equal(await nativeCursorVisible(child,{handle:1,curdata:btoa('PNG')}),true);
});

test('late original cursor decoding cannot make an older update the newest classification',async()=>{
  const f=fixture(),pending=[];
  f.input.updateServerCursor=payload=>new Promise(resolve=>pending.push({payload,resolve}));
  f.controller.bind();
  const old=f.input.updateServerCursor({handle:0,curdata:null});
  const latest=f.input.updateServerCursor({handle:1,curdata:'visible'});
  pending[1].resolve();await latest;await settled();
  pending[0].resolve();await old;await settled();
  assert.equal(f.controller.snapshot().nativeCursorVisible,true);f.controller.dispose();
});

test('waiting to return to the world does not capture clicks on a media sidebar or other UI',async()=>{
  const f=fixture();await lock(f);
  await f.input.updateServerCursor({handle:1,curdata:'visible'});await settled();
  await f.input.updateServerCursor({handle:0,curdata:null});await settled();
  const requests=f.calls.filter(call=>call==='pointer-request').length;
  const event=f.document.emit('pointerdown',{button:0,target:{id:'sidebar-button'}});await settled();
  assert.equal(event.prevented,undefined);assert.equal(f.controller.snapshot().pointerLocked,false);
  assert.equal(f.calls.filter(call=>call==='pointer-request').length,requests);f.controller.dispose();
});

test('a GUI cursor arriving during fullscreen transition still acquires Escape lock when fullscreen becomes ready',async()=>{
  const f=fixture();let grantFullscreen;
  f.document.documentElement.requestFullscreen=()=>new Promise(resolve=>{grantFullscreen=()=>{f.document.fullscreenElement=f.document.documentElement;f.document.emit('fullscreenchange');resolve();};});
  const enter=f.controller.enter();
  await f.input.updateServerCursor({handle:1,curdata:'visible'});await settled();
  grantFullscreen();
  await settled();await enter;
  const state=f.controller.snapshot();assert.equal(state.guiMode,true);assert.equal(state.fullscreen,true);
  assert.equal(state.keyboardLock,'active');assert.equal(state.pointerLocked,false);f.controller.dispose();
});

test('native PNG overlay uses server image/hotspot and disappears on leaving the iframe',async()=>{
  const f=fixture({overlays:true});f.input._cursorDensity=()=>2;f.controller.bind();
  await f.input.updateServerCursor({handle:1,curdata:'server-native-png',hotx:4,hoty:6});await settled();
  f.document.emit('mousemove',{target:f.input.element,clientX:100,clientY:90});
  const image=f.nodes.get('multicraft-native-cursor');
  assert.equal(image.src,'data:image/png;base64,server-native-png');
  assert.equal(image.style.display,'block');assert.equal(image.style.width,'16px');
  assert.equal(image.style.left,'98px');assert.equal(image.style.top,'87px');
  assert.equal(f.controller.snapshot().cursorOverlayVisible,true);
  f.document.emit('mouseleave');assert.equal(image.style.display,'none');
  f.controller.dispose();assert.equal(f.nodes.has('multicraft-native-cursor'),false);
});

test('unknown cursor metadata keeps normal GUI click propagation and a local pointer fallback',async()=>{
  const f=fixture();await f.controller.enter();
  const event=f.document.emit('pointerdown',{button:0,target:f.input.element});
  assert.equal(event.prevented,undefined);assert.equal(f.controller.snapshot().cursorFallback,true);
  assert.equal(f.calls.filter(call=>call==='pointer-request').length,0);f.controller.dispose();
});

test('denied pointer requests fall back to GUI without swallowing subsequent clicks',async()=>{
  const f=fixture({denyPointer:true});await f.controller.enter();
  await assert.rejects(f.controller.enter({forcePointer:true}));
  const requests=f.calls.filter(call=>call==='pointer-request').length;
  const event=f.document.emit('pointerdown',{button:0,target:f.input.element});
  assert.equal(event.prevented,undefined);assert.equal(f.controller.snapshot().cursorFallback,true);
  assert.equal(f.controller.snapshot().pointerError.name,'Error');
  assert.equal(f.calls.filter(call=>call==='pointer-request').length,requests);f.controller.dispose();
});

test('keyboard lock exceptions remain available by name and message for host diagnostics',async()=>{
  const f=fixture({denyKeyboard:true});await f.controller.enter();
  assert.deepEqual(f.controller.snapshot().keyboardError,{name:'Error',message:'keyboard denied'});f.controller.dispose();
});

test('F9 emits one remote Escape press without pointer or Keyboard Lock permissions',async()=>{
  const f=fixture({denyPointer:true,denyKeyboard:true});f.controller.bind();
  const down=f.child.emit('keydown',{code:'F9'});
  f.child.emit('keydown',{code:'F9',repeat:true});f.child.emit('keyup',{code:'F9'});
  await new Promise(resolve=>setTimeout(resolve,75));
  assert.equal(down.prevented,true);
  assert.equal(f.sent.filter(text=>text==='kd,65307').length,1);
  assert.equal(f.sent.filter(text=>text==='ku,65307').length,1);
  assert.ok(!f.sent.some(text=>text==='kd,65478'||text==='ku,65478'));
  assert.ok(!f.calls.includes('pointer-request'));assert.equal(f.controller.snapshot().keyboardLock,'inactive');f.controller.dispose();
});

test('F8 finishes a pending F9/menu key immediately and cancels its late key-up',async()=>{
  const f=fixture();f.controller.bind();
  f.outer.emit('keydown',{code:'F9'});
  assert.deepEqual(f.sent,['kd,65307']);
  f.outer.emit('keydown',{code:'F8'});
  assert.equal(f.sent.filter(text=>text==='ku,65307').length,1);
  await new Promise(resolve=>setTimeout(resolve,75));
  assert.equal(f.sent.filter(text=>text==='ku,65307').length,1);f.controller.dispose();
});

test('repeated menu-button presses release the prior Escape before pressing again',()=>{
  const f=fixture();f.controller.bind();f.controller.sendMenuKey();f.controller.sendMenuKey();
  assert.deepEqual(f.sent,['kd,65307','ku,65307','kd,65307']);
  f.controller.dispose();assert.equal(f.sent.filter(text=>text==='ku,65307').length,2);
});
