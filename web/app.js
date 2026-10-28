import {createGameInputController} from './viewer-bridge.js';

const $ = (id) => document.getElementById(id);
document.title='MultiCraft';
let csrf, challenge, keyPair, preAuthId, connected = false, configInitialized = false;
let policyRevision=-1, opFormRevision=-1, opFormDirty=false, latestLimits, captureSample;
const decode = (text) => Uint8Array.from(atob(text), c => c.charCodeAt(0));
const encode = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const gameInput=createGameInputController({iframe:$('stream'),onStatus(state){
  $('enter-game').disabled=!state.ready||!connected;
  $('enter-game').textContent=state.pointerLocked?'释放鼠标（F8）':state.guiMode?'捕获鼠标':'进入游戏 / 锁定鼠标';
  let text='连接画面后，点击“进入游戏”。';
  if(state.ready&&connected){
    if(state.pointerLocked)text='正在控制视角 · F9 菜单 · F8 释放鼠标';
    else if(state.needsPointerClick&&!state.guiMode)text='点击游戏画面锁定鼠标 · F8 释放';
    else if(state.nativeCursorVisible)text='菜单操作模式 · F9 返回';
    else if(state.needsWorldClick)text='点击游戏画面继续游戏';
    else text='点击“进入游戏”控制视角 · F9 菜单';
  }
  $('input-status').textContent=state.error||text;
  $('input-status').classList.toggle('error',!!state.error);
  for(const key of ['pointerLocked','fullscreen','keyboardLock','nativeCursorVisible','guiMode','needsWorldClick','needsPointerClick','cursorOverlayVisible'])$('input-status').dataset[key]=String(state[key]);
  $('input-status').dataset.keyboardErrorName=state.keyboardError?.name||'';
  $('input-status').dataset.keyboardErrorMessage=state.keyboardError?.message||'';
  $('input-status').dataset.pointerErrorName=state.pointerError?.name||'';
  $('input-status').dataset.pointerErrorMessage=state.pointerError?.message||'';
  $('input-status').dataset.viewerTopLevel=String(state.viewerTopLevel);
}});
window.multicraftInputStatus=()=>gameInput.snapshot();
async function api(path, data, method = 'POST') {
  const response = await fetch(new URL(path.replace(/^\//,''),new URL('./',location.href)), {method, credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  const text = await response.text();
  let result; try {result = JSON.parse(text);} catch {result = {error:text};}
  if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
  return result;
}
function message(id, value, error = false) {$(id).textContent=value; $(id).classList.toggle('error',error);if(['state-message','connection-message'].includes(id))$(id).hidden=!value;}
function guarded(id, fn, messageId='login-message') {$(id).addEventListener('click',async()=>{ $(id).disabled=true;try{await fn();}catch(e){message(messageId,e.message,true);}finally{$(id).disabled=false;}});}
async function finishLogin(result) {
  if (result.accounts) {
    preAuthId=result.preAuthId; $('account-select').hidden=false;
    const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='请选择本次身份';placeholder.disabled=true;placeholder.selected=true;
    $('account-options').replaceChildren(placeholder,...result.accounts.map(a=>{const option=document.createElement('option');option.value=a.id;option.textContent=`${a.displayName}${a.isOp?' · OP':''}`;return option;}));
    message('login-message','请选择本次登录身份。');return;
  }
  const account=result.account; document.title=`${account.displayName} · MultiCraft`; $('identity').textContent=`${account.displayName}${account.isOp?' · OP':''}`;
  $('op-limits').hidden=!account.isOp;
  $('login').hidden=true;$('game').hidden=false;message('login-message','');await refreshState();
}
$('browser-tab').onclick=()=>{ $('browser-login').hidden=false;$('ssh-login').hidden=true;$('browser-tab').classList.add('active');$('ssh-tab').classList.remove('active');};
$('ssh-tab').onclick=()=>{ $('browser-login').hidden=true;$('ssh-login').hidden=false;$('ssh-tab').classList.add('active');$('browser-tab').classList.remove('active');};
$('key-file').onchange=async()=>{try{keyPair=JSON.parse(await $('key-file').files[0].text());if(!keyPair.publicKey||!keyPair.privateKey)throw Error('需要包含 publicKey 和 privateKey 的游戏专用密钥文件');message('login-message','游戏密钥已在本机读取，尚未发送私钥。');}catch(e){keyPair=undefined;message('login-message',e.message,true);}};
guarded('login-browser',async()=>{
  if(!keyPair)throw Error('请先选择已登记的游戏专用密钥文件。');
  challenge=await api('/api/auth/challenge',{publicKey:{format:'spki',key:keyPair.publicKey}});
  const privateKey=await crypto.subtle.importKey('pkcs8',decode(keyPair.privateKey),{name:'Ed25519'},false,['sign']);
  const signature=await crypto.subtle.sign('Ed25519',privateKey,decode(challenge.challengeBase64));
  await finishLogin(await api('/api/auth/verify',{attemptId:challenge.attemptId,signature:encode(signature)}));
});
guarded('download-challenge',async()=>{
  challenge=await api('/api/auth/challenge',{publicKey:$('ssh-public').value.trim()});
  const blob=new Blob([JSON.stringify(challenge,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='challenge.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('login-message','挑战已下载，请在本机签名后粘贴结果。');
});
guarded('verify-ssh',async()=>{if(!challenge)throw Error('请先下载当前浏览器的登录挑战。');await finishLogin(await api('/api/auth/verify',{attemptId:challenge.attemptId,signature:$('ssh-signature').value.trim()}));});
guarded('select-account',async()=>{if(!$('account-options').value)throw Error('请先明确选择本次登录身份。');await finishLogin(await api('/api/auth/select',{preAuthId,accountId:$('account-options').value}));});
guarded('connect',async()=>{const lease=await api('/api/lease',{});const generation=crypto.randomUUID();const viewerUrl=new URL(lease.url,location.href);viewerUrl.hash=`viewer=${generation}`;$('stream').dataset.viewerGeneration=generation;$('stream').src=viewerUrl.href;$('stream').hidden=false;$('placeholder').hidden=true;connected=true;$('connection-status').textContent='正在接收原版游戏画面';message('connection-message','');},'connection-message');
guarded('release',async()=>{gameInput.release();await api('/api/lease',undefined,'DELETE');$('stream').src='about:blank';$('stream').hidden=true;$('placeholder').hidden=false;connected=false;$('connection-status').textContent='已释放控制权';message('connection-message','');},'connection-message');
guarded('logout',async()=>{gameInput.release();await api('/api/logout',{});document.title='MultiCraft';location.reload();},'connection-message');
const enterGame=async()=>{if($('stream').hidden)return;try{if(gameInput.snapshot().pointerLocked)gameInput.release();else await gameInput.enter({forcePointer:true});}catch(error){message('input-status',error.message,true);}};
$('enter-game').onclick=enterGame;
$('gui-mode').onclick=()=>gameInput.useGuiPointer();
$('fullscreen').onclick=()=>gameInput.fullscreen().catch(error=>message('input-status',error.message,true));
guarded('mc-menu',async()=>{
  if(!gameInput.sendMenuKey())throw Error('游戏输入尚未就绪');
},'input-status');
$('stream').addEventListener('load',()=>gameInput.bind());
$('chat-form').onsubmit=async(event)=>{event.preventDefault();try{await gameInput.sendChat($('chat-text').value);$('chat-text').value='';}catch(error){message('input-status',error.message,true);}};
$('resolution').onchange=()=>{if($('resolution').value==='custom')return;const[w,h]=$('resolution').value.split(',');$('width').value=w;$('height').value=h;};
for (const name of ['width','height']) $(name).oninput=()=>{$('resolution').value='custom';};
$('adaptive').onchange=()=>{$('abr-help').textContent=$('adaptive').checked?'根据链路拥塞，在所设码率上限内自动调节。':'保持所设编码目标，不启用链路自适应；实际流量仍随画面变化。';};
$('stream-config').onsubmit=async(event)=>{
  event.preventDefault();$('apply').disabled=true;
  try{const config={width:Number($('width').value),height:Number($('height').value),fps:Number($('fps').value),bitrateKbps:Number($('bitrate').value),adaptive:$('adaptive').checked};message('config-message','正在等待媒体后端确认…');const result=await api('/api/stream-config',config);message('config-message',result.measured?.ready===false?'参数已保存，将用于下一次采集。':'参数已由媒体后端确认。');await refreshState();}catch(e){message('config-message',e.message,true);}finally{$('apply').disabled=false;}
};
const policyFields={width:'width',height:'height',fps:'fps',bitrateKbps:'bitrate'};
function updateLimits(limits,revision,{forceOpForm=false}={}){
  if(!limits)return;
  latestLimits=limits;
  for(const [name,id] of Object.entries(policyFields)){
    const bounds=limits[name];if(!bounds)continue;
    $(id).min=bounds[0];$(id).max=bounds[1];
    if(policyRevision!==revision){
      let value=Number($(id).value);if(Number.isFinite(value)){value=Math.max(bounds[0],Math.min(bounds[1],value));if(name==='width'||name==='height')value-=value%2;$(id).value=value;}
    }
  }
  $('fps-range').textContent=`${limits.fps[0]}–${limits.fps[1]} fps`;
  $('bitrate-range').textContent=`${limits.bitrateKbps[0]}–${limits.bitrateKbps[1]} kbps`;
  $('resolution-cap').textContent=`OP 上限 ${limits.width[1]} × ${limits.height[1]}`;
  for(const option of $('resolution').options){if(option.value==='custom')continue;const[w,h]=option.value.split(',').map(Number);option.disabled=w>limits.width[1]||h>limits.height[1];}
  if($('resolution').selectedOptions[0]?.disabled)$('resolution').value='custom';
  if(!opFormDirty||forceOpForm){
    for(const name of Object.keys(policyFields))$(`limit-${name}`).value=limits[name][1];
    opFormRevision=revision;opFormDirty=false;
  }
  $('policy-revision').textContent=`当前版本 ${revision}`;
  policyRevision=revision;
}
for(const name of Object.keys(policyFields))$(`limit-${name}`).oninput=()=>{opFormDirty=true;};
$('op-limits-form').onsubmit=async(event)=>{
  event.preventDefault();$('save-limits').disabled=true;
  try{
    const limits=Object.fromEntries(Object.keys(policyFields).map(name=>[name,[latestLimits[name][0],Number($(`limit-${name}`).value)]]));
    message('limits-message','正在校验上限，并等待必要的传流调整…');
    const result=await api('/api/op/stream-limits',{limits,expectedRevision:opFormRevision},'PUT');
    updateLimits(result.limits,result.revision,{forceOpForm:true});
    message('limits-message',result.streamAdjusted?'上限已保存，当前传流已先调整到新范围。':'上限已保存，当前传流参数保持不变。');await refreshState();
  }catch(error){message('limits-message',error.message,true);}finally{$('save-limits').disabled=false;}
};
guarded('reload-limits',async()=>{const result=await api('/api/op/stream-limits',undefined,'GET');updateLimits(result.limits,result.revision,{forceOpForm:true});message('limits-message','已加载当前上限。');},'limits-message');
async function refreshState(){
  if($('game').hidden)return;
  try{await refreshStateOnce();message('state-message','');return true;}catch(error){message('state-message',error.message,true);return false;}
}
async function refreshStateOnce(){
  if($('game').hidden)return;
  const state=await api('/api/state',undefined,'GET');const effective=state.stream?.effective;
  $('op-limits').hidden=!state.account?.isOp;
  updateLimits(state.limits,state.policyRevision);
  if(effective){
    $('effective').textContent=`${effective.width}×${effective.height} · ${effective.fps} fps · ${effective.bitrateKbps} kbps`;$('adaptive-state').textContent=effective.adaptive?'开启':'关闭';
    if(!configInitialized){
      $('width').value=effective.width;$('height').value=effective.height;$('fps').value=effective.fps;$('bitrate').value=effective.bitrateKbps;$('adaptive').checked=effective.adaptive;
      const preset=`${effective.width},${effective.height}`;$('resolution').value=[...$('resolution').options].some(o=>o.value===preset)?preset:'custom';$('adaptive').onchange();configInitialized=true;
    }
  }
  const measured=state.stream?.measured||state.stream?.video||state.runtime?.measured||{};
  const now=performance.now(),frames=measured.frames;
  let captureFps;
  if(Number.isFinite(frames)&&captureSample?.generation===state.stream?.generation&&now>captureSample.time)captureFps=Math.max(0,frames-captureSample.frames)*1000/(now-captureSample.time);
  if(Number.isFinite(frames))captureSample={generation:state.stream?.generation,frames,time:now};
  const w=state.native?.width||measured.width||measured.actualWidth||measured.captureWidth;const h=state.native?.height||measured.height||measured.actualHeight||measured.captureHeight;
  $('measured').textContent=w&&h?`${w}×${h}${captureFps!==undefined?` · ${captureFps.toFixed(1)} fps`:measured.fps?` · ${Number(measured.fps).toFixed(1)} fps`:''}`:(measured.capturing===false||measured.ready===false?'等待采集':'正在采样');
  if(connected)$('connection-status').textContent=state.runtime.nativeReady?'原版客户端已就绪 · 点击画面操作':(state.runtime.error||'媒体已连接，等待原版客户端');
  if(connected&&!$('stream').hidden)gameInput.bind();
}
window.addEventListener('message',async(event)=>{
  if(event.origin!==location.origin||event.source!==$('stream').contentWindow||event.data?.viewerGeneration!==$('stream').dataset.viewerGeneration)return;
  if(event.data?.type==='viewer-error'){
    // The old viewer cannot adopt a new native epoch. Offer an explicit new
    // connection after its own lease has drained, keeping other tabs untouched.
    const reason=event.data.message||'会话已改变，请重新连接画面';
    connected=false;$('stream').src='about:blank';$('stream').hidden=true;$('placeholder').hidden=false;
    $('connect').textContent='重新连接画面';$('connect').disabled=true;$('connection-status').textContent='画面连接已停止';
    message('connection-message',reason,true);
    try{await api('/api/lease',undefined,'DELETE');}catch{}finally{$('connect').disabled=false;}
    return;
  }
  if(event.data?.type!=='stats')return;
  const data=event.data.data||{}, client=data.client||{}, sample=data.latest||{};
  $('received').textContent=client.resolution?`${client.resolution} · ${Number(data.clientFps||0).toFixed(1)} fps`:'等待解码';
  $('received-bitrate').textContent=typeof sample.mbps==='number'?`${sample.mbps.toFixed(2)} Mbps`:'正在采样';
  $('decoder').textContent=client.decoder||'等待解码';
  $('audio-state').textContent=data.audioActive?'PCM 音频播放中':'点击捕获鼠标以启用声音';
});
async function claimTabNamespace(){
  if(!globalThis.BroadcastChannel)return true;
  const id=crypto.randomUUID(),channel=new BroadcastChannel(`multicraft-tab:${location.pathname}`);let active=false,duplicate=false;
  channel.onmessage=event=>{if(event.data?.type==='probe'&&active)channel.postMessage({type:'occupied',to:event.data.id});if(event.data?.type==='occupied'&&event.data.to===id)duplicate=true;};
  channel.postMessage({type:'probe',id});await new Promise(resolve=>setTimeout(resolve,180));
  if(duplicate){channel.close();location.replace('/');return false;}active=true;window.addEventListener('pagehide',()=>channel.close(),{once:true});return true;
}
(async()=>{try{if(!await claimTabNamespace())return;const bootstrap=await api('/api/bootstrap',undefined,'GET');csrf=bootstrap.csrf;updateLimits(bootstrap.limits,bootstrap.policyRevision);try{await finishLogin(await api('/api/me',undefined,'GET'));}catch{}}catch(e){message('login-message',e.message,true);}})();
setInterval(()=>refreshState(),2500);
