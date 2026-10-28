import {createM1Player} from './m1-player.js';

const $ = id => document.getElementById(id);
let csrf, connected = false, refreshing;
const panels = new Map();
const scans = {Enter:40,Escape:41,Backspace:42,Tab:43,Space:44,Minus:45,Equal:46,BracketLeft:47,BracketRight:48,Backslash:49,Semicolon:51,Quote:52,Backquote:53,Comma:54,Period:55,Slash:56,CapsLock:57,PrintScreen:70,ScrollLock:71,Pause:72,Insert:73,Home:74,PageUp:75,Delete:76,End:77,PageDown:78,ArrowRight:79,ArrowLeft:80,ArrowDown:81,ArrowUp:82,ControlLeft:224,ShiftLeft:225,AltLeft:226,MetaLeft:227,ControlRight:228,ShiftRight:229,AltRight:230,MetaRight:231};
for (let n=0;n<26;n++) scans[`Key${String.fromCharCode(65+n)}`] = 4+n;
for (let n=1;n<=9;n++) scans[`Digit${n}`] = 29+n;
scans.Digit0=39;
for (let n=1;n<=12;n++) scans[`F${n}`]=57+n;

async function api(path, value, {keepalive=false,controlGeneration}={}) {
  const response = await fetch(path, {method:value===undefined?'GET':'POST',credentials:'same-origin',keepalive,
    headers:{'Content-Type':'application/json','X-CSRF-Token':csrf||'',...(controlGeneration?{'X-MultiCraft-Control':controlGeneration}:{})},...(value===undefined?{}:{body:JSON.stringify(value)})});
  const data = await response.json();
  if (!response.ok) {const error=new Error(data.error || `请求失败 (${response.status})`);error.status=response.status;throw error;}
  return data;
}

function message(text, error=false) {$('message').textContent=text;$('message').classList.toggle('error',error);}
function keyEvent(code, action, event={}) {
  const scancode=scans[code];
  if (scancode===undefined) return;
  const printable=typeof event.key==='string'&&[...event.key].length===1;
  const keycode=printable?event.key.codePointAt(0):(scancode>=224?0x40000000|scancode:({Enter:13,Escape:27,Backspace:8,Tab:9,Space:32,Delete:127}[code]??(0x40000000|scancode)));
  return {type:'key',scancode,keycode,action,modifiers:(event.shiftKey?1:0)|(event.ctrlKey?64:0)|(event.altKey?256:0)|(event.metaKey?1024:0)};
}

function makePanel(id) {
  const article=document.createElement('article');
  article.innerHTML=`<h2>玩家 ${id} · M1Player${id}</h2><div class="tools"><button data-action="video">连接画面</button><button data-action="audio">开启声音</button><button data-action="lock">捕获鼠标</button><button data-action="menu">游戏菜单 · F9</button><button data-action="release">释放 · F8</button></div><canvas width="1280" height="720" tabindex="0" aria-label="玩家 ${id} 游戏画面"></canvas><div class="status" role="status">等待画面</div><div class="tools"><button data-action="disconnect">断开游戏连接</button><button data-action="connect">重连</button><button data-action="respawn">重生</button></div><div class="chat"><input type="text" maxlength="256" placeholder="测试聊天文字" aria-label="玩家 ${id} 聊天文字"><button data-action="chat">发送聊天</button></div>`;
  $('sessions').append(article);
  const canvas=article.querySelector('canvas'),status=article.querySelector('.status');
  let native={}, queue=[], flushing=false, lastFrames=0, errors=0, inputSeq=0, inputChain=Promise.resolve(), controllerGeneration, runtimeOwner;
  let availability='ready', panelEpoch=0,player,playbackActive=false,playbackGeneration=0;
  const buttons=[...article.querySelectorAll('button')],chatInput=article.querySelector('input'),pendingActions=new Map();
  function clearCanvas() {canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);}
  function updateControls() {
    const blocked=availability!=='ready';
    for(const button of buttons){button.disabled=pendingActions.has(button)||(blocked&&!(availability==='unreachable'&&button.dataset.action==='video'))||(!blocked&&!playbackActive&&button.dataset.action!=='video');}
    buttons.find(button=>button.dataset.action==='video').textContent=availability==='unreachable'?'重新检查连接':'连接画面';
    chatInput.disabled=blocked||!playbackActive;
    canvas.tabIndex=blocked||!playbackActive?-1:0;
    article.dataset.availability=availability;
  }
  function quiesce(mode,text) {
    if(availability==='ready') {
      const epoch=native.connectionEpoch,owner=runtimeOwner,generation=controllerGeneration;
      if(Number.isSafeInteger(epoch)&&typeof owner==='string'&&generation) {
        inputSeq=Math.max(inputSeq,native.lastInputSeq||0)+1;
        const envelope={expectedRuntimeOwner:owner,expectedEpoch:epoch,inputSeq,events:[{type:'release'}]};
        inputChain.catch(()=>{}).then(()=>api(`/api/session/${id}/input`,envelope,{controlGeneration:generation})).catch(()=>{});
      }
      panelEpoch++;inputChain=Promise.resolve();flushing=false;pendingActions.clear();
      playbackActive=false;playbackGeneration++;const previous=player;player=undefined;previous?.stop();
    }
    availability=mode;queue=[];controllerGeneration=undefined;lastFrames=0;
    if(document.pointerLockElement===canvas)document.exitPointerLock();
    clearCanvas();article.dataset.decodedFrames='0';article.dataset.audioFrames='0';
    status.textContent=text;status.classList.add('error');updateControls();
  }
  const protocol=location.protocol==='https:'?'wss:':'ws:';
  function newPlayer() {
    const generation=playbackGeneration;
    return createM1Player({canvas,videoUrl:`${protocol}//${location.host}/media/video/${id}`,
    isCurrent:()=>availability==='ready'&&playbackActive&&generation===playbackGeneration,
    audioUrl:`${protocol}//${location.host}/media/audio/${id}`,onStatus(value){
      if(availability!=='ready'||!playbackActive||generation!==playbackGeneration){if(!playbackActive&&value.type==='video-frame')clearCanvas();return;}
      if(value.type==='video-frame'){lastFrames=value.decodedFrames;status.textContent=`已解码 ${lastFrames} 帧${native.dimension?' · '+native.dimension.split('/').pop().replace(']',''):''}`;}
      else if(value.type==='video-closed'){quiesce('unreachable','画面连接已断开，重新检查连接后可再次接入。');return;}
      else if(value.type.endsWith('error')) {status.textContent=value.message||'媒体连接失败';status.classList.add('error');}
      else if(value.type==='audio-frames') article.dataset.audioFrames=String(value.audioFrames);
      else if(value.type==='video')refresh().catch(error=>{if(availability==='ready')status.textContent=error.message;});
      article.dataset.decodedFrames=String(lastFrames);
    }});
  }
  function enqueue(event) {if(availability!=='ready'||!playbackActive)return;if(event) queue.push(event);if(queue.length>128){queue=[{type:'release'}];status.textContent='输入积压，已释放按键';}}
  function sendInput(value) {
    if(availability!=='ready')return Promise.reject(new Error('该会话当前不可操作'));
    if(!playbackActive)return Promise.reject(new Error('请先连接该会话的画面'));
    const expectedEpoch=native.connectionEpoch;
    if(!Number.isSafeInteger(expectedEpoch))return Promise.reject(new Error('等待支持连接代际的 M1 运行时'));
    if(typeof runtimeOwner!=='string')return Promise.reject(new Error('等待 M1 运行时启动代际'));
    if(!controllerGeneration)return Promise.reject(new Error('请先连接画面并等待控制权确认'));
    inputSeq=Math.max(inputSeq,native.lastInputSeq||0)+1;
    const envelope={expectedRuntimeOwner:runtimeOwner,expectedEpoch,inputSeq,events:value.events||[value]};
    const generation=controllerGeneration,epoch=panelEpoch;
    const result=inputChain.catch(()=>{}).then(()=>{if(availability!=='ready'||epoch!==panelEpoch)throw new Error('会话状态已改变，旧输入已丢弃');return api(`/api/session/${id}/input`,envelope,{controlGeneration:generation});});inputChain=result;return result;
  }
  async function flush() {
    if(flushing||!queue.length||!csrf)return;
    const ticket={epoch:panelEpoch};flushing=ticket;const events=queue.splice(0,128);
    try {await sendInput({events});if(ticket.epoch===panelEpoch)errors=0;}catch(error){if(availability==='ready'&&ticket.epoch===panelEpoch){queue=[{type:'release'}];errors++;status.textContent=error.message;status.classList.add('error');}}finally{if(flushing===ticket)flushing=false;}
  }
  const timer=setInterval(flush,20);
  async function release() {
    const epoch=panelEpoch;
    queue=[];
    if(document.pointerLockElement===canvas)document.exitPointerLock();
    if(availability!=='ready'||!playbackActive)return;
    try {await sendInput({type:'release'});}catch(error){if(availability==='ready'&&epoch===panelEpoch)status.textContent=error.message;}
  }
  async function menu() {const epoch=panelEpoch;queue=[];await sendInput({events:[keyEvent('Escape',1),keyEvent('Escape',0)]});if(epoch===panelEpoch&&document.pointerLockElement===canvas)document.exitPointerLock();}
  async function lock() {
    const epoch=panelEpoch;let cancelled=false;canvas.focus();
    const capture=Promise.resolve(canvas.requestPointerLock()).then(()=>{if(cancelled||availability!=='ready'||epoch!==panelEpoch){if(document.pointerLockElement===canvas)document.exitPointerLock();throw new Error('会话状态已改变，鼠标捕获已释放');}});
    capture.catch(()=>{});
    try{await sendInput({type:'grab'});await capture;}catch(error){cancelled=true;if(document.pointerLockElement===canvas)document.exitPointerLock();throw error;}
  }
  async function act(action) {
    if(availability!=='ready') {if(availability==='unreachable'&&action==='video'){await refresh();return;}throw new Error('该会话已停止，需 OP 重启后恢复');}
    const created={epoch:panelEpoch,owner:runtimeOwner,nativeEpoch:native.connectionEpoch,generation:controllerGeneration};
    if(action==='video') {if(!player||!(player.snapshot().videoState<2)){playbackActive=false;playbackGeneration++;player?.stop();playbackActive=true;player=newPlayer();}player.start();status.classList.remove('error');}
    else if(action==='audio') {if(!player||!playbackActive)throw new Error('请先连接该会话的画面');await player.startAudio();}
    else if(action==='lock') await lock();
    else if(action==='release') await release();
    else if(action==='menu') await menu();
    else if(action==='chat') {
      const input=article.querySelector('input'),text=input.value;
      if(!text)return;
      await sendInput({events:[keyEvent('KeyT',1,{key:'t'}),{type:'text',text:'t'},keyEvent('KeyT',0,{key:'t'})]});
      if(availability!=='ready'||created.epoch!==panelEpoch)throw new Error('会话状态已改变，旧聊天已丢弃');
      await sendInput({events:[{type:'text',text},keyEvent('Enter',1),keyEvent('Enter',0)]});input.value='';
    } else {await release();if(availability!=='ready'||created.epoch!==panelEpoch)throw new Error('会话状态已改变，旧命令已丢弃');await api(`/api/session/${id}/command`,{type:action,expectedRuntimeOwner:created.owner,expectedEpoch:created.nativeEpoch},{controlGeneration:created.generation});await refresh();}
  }
  for(const button of buttons)button.onclick=async()=>{const ticket={epoch:panelEpoch};pendingActions.set(button,ticket);updateControls();try{await act(button.dataset.action);}catch(error){if(availability==='ready'&&ticket.epoch===panelEpoch){status.textContent=error.message;status.classList.add('error');}}finally{if(pendingActions.get(button)===ticket)pendingActions.delete(button);updateControls();}};
  canvas.oncontextmenu=event=>event.preventDefault();
  canvas.onkeydown=event=>{
    if(availability!=='ready'||!playbackActive){event.preventDefault();return;}
    if(event.code==='F8'){event.preventDefault();release();return;}
    if(event.code==='F9'){event.preventDefault();if(!event.repeat)menu();return;}
    event.preventDefault();enqueue(keyEvent(event.code,event.repeat?-1:1,event));
    if(event.key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey)enqueue({type:'text',text:event.key});
  };
  canvas.onkeyup=event=>{event.preventDefault();if(!['F8','F9'].includes(event.code))enqueue(keyEvent(event.code,0,event));};
  function position(event) {const box=canvas.getBoundingClientRect();return {type:'move',x:Math.max(0,Math.min(canvas.width,(event.clientX-box.left)*canvas.width/box.width)),y:Math.max(0,Math.min(canvas.height,(event.clientY-box.top)*canvas.height/box.height)),dx:event.movementX*canvas.width/box.width,dy:event.movementY*canvas.height/box.height};}
  canvas.onmousemove=event=>{if(document.activeElement===canvas||native.screen||native.mouseGrabbed===false)enqueue(position(event));};
  canvas.onmousedown=event=>{event.preventDefault();if(availability!=='ready')return;canvas.focus();enqueue(position(event));enqueue({type:'button',button:[1,2,3][event.button]||event.button+1,action:1});};
  window.addEventListener('mouseup',event=>{if(document.activeElement===canvas)enqueue({type:'button',button:[1,2,3][event.button]||event.button+1,action:0});});
  canvas.onwheel=event=>{event.preventDefault();enqueue({type:'scroll',x:-event.deltaX/100,y:-event.deltaY/100});};
  canvas.onblur=()=>release();
  document.addEventListener('pointerlockchange',()=>{if(document.pointerLockElement!==canvas&&document.activeElement===canvas)release();});
  return {update(value,generation,owner){
    if(value.closed===true||value.failure){quiesce(value.failure?'failed':'closed',`会话 ${id} ${value.failure?'运行失败':'已关闭'}，需 OP 重启后恢复。`);native=value;article.dataset.nativeSession=JSON.stringify({id:value.id,closed:value.closed,failure:value.failure});return;}
    if(typeof runtimeOwner==='string'&&runtimeOwner!==owner)quiesce('unreachable','运行时已重启，请重新连接画面。');
    const resumed=availability!=='ready';
    if(native.connectionEpoch!==value.connectionEpoch||controllerGeneration!==generation||runtimeOwner!==owner||resumed){panelEpoch++;queue=[];inputSeq=value.lastInputSeq||0;inputChain=Promise.resolve();flushing=false;pendingActions.clear();}
    availability='ready';controllerGeneration=generation;runtimeOwner=owner;native=value;
    article.dataset.nativeSession=JSON.stringify({id:value.id,dimension:value.dimension,mouseGrabbed:value.mouseGrabbed,screen:value.screen});
    if(resumed){status.textContent='会话已恢复，点击“连接画面”重新接入。';status.classList.remove('error');}
    updateControls();
    if(value.screen&&document.pointerLockElement===canvas)document.exitPointerLock();
  },unavailable(){quiesce('unreachable','运行时暂不可达，已停止旧画面；恢复后可重新连接。');},release,stop(){clearInterval(timer);player?.stop();release();},snapshot(){return {media:player?.snapshot()||{decodedFrames:0,audioFrames:0},native,errors,availability,displayedFrames:lastFrames};}};
}

function refresh() {
  if(refreshing)return refreshing;
  refreshing=refreshOnce().catch(error=>{
    for(const panel of panels.values())panel.unavailable();
    $('host-status').textContent='运行时状态暂不可用，旧画面已停止。';
    if(error.status===401||error.status===403)$('login').hidden=false;
    throw error;
  }).finally(()=>{refreshing=undefined;});
  return refreshing;
}
async function refreshOnce() {
  const state=await api('/api/state');csrf=state.csrf;
  $('login').hidden=true;$('workbench').hidden=false;
  const sessions=state.native.sessions||[],active=sessions.filter(session=>session.closed!==true&&!session.failure).length;
  $('host-status').textContent=`运行时进程 ${state.native.pid} · ${active} / ${sessions.length} 个会话运行中 · ${state.native.audioMode==='loopback-pcm'?'独立 PCM 音频待验收':'当前音频禁用'}`;
  for(const session of state.native.sessions||[])panels.get(session.id)?.update(session,state.controllers?.[session.id],state.native.runtimeOwner);
  if(!connected){for(const id of ['A','B'])panels.set(id,makePanel(id));connected=true;for(const session of state.native.sessions||[])panels.get(session.id)?.update(session,state.controllers?.[session.id],state.native.runtimeOwner);}
  for(const [id,panel] of panels)if(!(state.native.sessions||[]).some(session=>session.id===id))panel.unavailable();
  message('已通过 M0 的 asta OP 登录验证。');
}
$('retry').onclick=()=>refresh().catch(error=>message(error.message,true));
window.addEventListener('blur',()=>{for(const panel of panels.values())panel.release();});
window.multicraftM1Snapshot=()=>Object.fromEntries([...panels].map(([id,panel])=>[id,panel.snapshot()]));
refresh().catch(error=>message(error.message,true));
setInterval(()=>refresh().catch(error=>message(error.message,true)),2000);
