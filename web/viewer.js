import {createM1Player} from './m1-player.js';
import {createViewerInput} from './viewer-input.js';
const canvas=document.getElementById('videoStream'),label=document.getElementById('viewer-status');
let csrf,player,active=true,controller,native={},runtimeOwner,decodedFrames=0,mediaGeneration,lastRate=performance.now(),lastBytes=0,lastFrames=0;
// The fragment is non-secret routing metadata; unlike window.name it survives
// the initial about:blank-to-viewer navigation consistently across browsers.
const viewerGeneration=new URL(location.href).hash.match(/^#viewer=([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/i)?.[1]?.toLowerCase();
const prefix=new URL('./',location.href),ws=path=>new URL(path,prefix).href.replace(/^http/,'ws');
async function api(path,value,generation){const response=await fetch(new URL(path,prefix),{method:value===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf||'','X-MultiCraft-Control':generation||''},...(value===undefined?{}:{body:JSON.stringify(value)})});const result=await response.json();if(!response.ok)throw Error(result.error||'游戏连接失败');return result;}
function fit(){const box=document.getElementById('surface').getBoundingClientRect();const scale=Math.min(box.width/canvas.width,box.height/canvas.height);canvas.style.width=`${canvas.width*scale}px`;canvas.style.height=`${canvas.height*scale}px`;}
function publish(value){parent.postMessage({...value,viewerGeneration},location.origin);}
const input=createViewerInput({canvas,send:(value,generation)=>api('api/session/input',value,generation),startAudio:()=>player?.startAudio(),onStatus:state=>{canvas.style.cursor=state.pointerLocked?'none':'default';publish({type:'input-status',state});}});
function failed(error){if(!active)return;active=false;input.unavailable();player?.stop();canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);label.hidden=false;label.textContent=error.message||String(error);publish({type:'viewer-error',message:label.textContent});}
async function refresh(){if(!active)return;const state=await api('api/state');if(runtimeOwner&&state.runtimeOwner!==runtimeOwner)throw Error('客户端运行时已重启，请重新连接画面');if(Number.isSafeInteger(native.connectionEpoch)&&state.native?.connectionEpoch!==native.connectionEpoch)throw Error('游戏连接已改变，请重新连接画面');native=state.native;runtimeOwner=state.runtimeOwner;controller=state.controller;input.update(native,runtimeOwner,controller);if(native.closed||native.failure)throw Error(native.failure||'客户端已关闭，需 OP 重启');publish({type:'native-state',native});}
function status(value){
  if(!active)return;
  if(value.type==='control'){controller=value.generation;refresh().catch(failed);}
  if(value.type==='video'){mediaGeneration=value.generation;fit();refresh().catch(failed);}
  if(value.type==='video-frame'){decodedFrames=value.decodedFrames;label.hidden=true;canvas.dataset.decodedFrames=String(decodedFrames);}
  if(value.type==='audio-frames')canvas.dataset.audioFrames=String(value.audioFrames);
  if(value.type==='video-closed'){failed(Error('画面连接已断开，请重新连接'));return;}
  if(value.type.endsWith('error')){label.hidden=false;label.textContent=value.message;}
  const now=performance.now(),elapsed=now-lastRate;if(elapsed<500)return;
  const sample=player?.snapshot()||{},bytes=sample.receivedBytes||0;
  const mbps=Math.max(0,bytes-lastBytes)*8/(elapsed*1000),fps=Math.max(0,decodedFrames-lastFrames)*1000/elapsed;
  lastRate=now;lastBytes=bytes;lastFrames=decodedFrames;
  publish({type:'stats',data:{client:{resolution:`${canvas.width}×${canvas.height}`,decoder:'WebCodecs VideoDecoder'},decodedFrames,clientFps:fps,latest:{mbps},audioFrames:sample.audioFrames||0,audioActive:(sample.audioFrames||0)>0,pcm:true,mediaGeneration}});
}
window.multicraftViewer={input,snapshot:()=>({input:input.snapshot(),media:player?.snapshot(),native,decodedFrames})};
document.getElementById('viewer-lock').onclick=()=>input.capture().catch(error=>{label.hidden=false;label.textContent=error.message;});
document.getElementById('viewer-menu').onclick=()=>input.menu().catch(failed);
document.getElementById('viewer-release').onclick=()=>input.release();
window.addEventListener('resize',fit);document.addEventListener('fullscreenchange',fit);
window.addEventListener('pagehide',()=>{active=false;input.stop();player?.stop();});
(async()=>{try{if(!viewerGeneration)throw Error('画面连接标识无效，请通过“连接游戏”重新进入');csrf=(await api('api/bootstrap')).csrf;await refresh();player=createM1Player({canvas,videoUrl:ws('media/video'),audioUrl:ws('media/audio'),isCurrent:()=>active,onStatus:status});player.start();fit();}catch(error){failed(error);}})();
setInterval(()=>refresh().catch(failed),1500);
