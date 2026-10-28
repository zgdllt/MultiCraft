import test from 'node:test';
import assert from 'node:assert/strict';
import {createViewerInput,pointerPosition} from './viewer-input.js';
function fixture(){
  const handlers=new Map(),sent=[];let captures=0,fullscreens=0;
  const listen={addEventListener(type,fn){handlers.set(type,fn);},removeEventListener(){}};
  const doc={...listen,pointerLockElement:null,fullscreenElement:null,exitPointerLock(){this.pointerLockElement=null;}};
  const win={...listen,setInterval(){return 1;},clearInterval(){}};doc.defaultView=win;
  const canvas={...listen,ownerDocument:doc,width:1920,height:1080,parentElement:{async requestFullscreen(){fullscreens++;doc.fullscreenElement=this;}},focus(){doc.activeElement=this;},async requestPointerLock(){captures++;doc.pointerLockElement=canvas;},getBoundingClientRect:()=>({left:100,top:60,width:960,height:540})};
  const input=createViewerInput({canvas,doc,win,send:async(value,generation)=>{sent.push({value,generation});}});
  input.update({connectionEpoch:3,lastInputSeq:0},'a'.repeat(32),'lease');
  return {input,doc,canvas,sent,handlers,get captures(){return captures;},get fullscreens(){return fullscreens;}};
}
test('capture and F8 release never request or exit fullscreen',async()=>{
  const f=fixture();await f.input.capture();assert.equal(f.captures,1);assert.equal(f.fullscreens,0);assert.equal(f.sent[0].value.events[0].type,'grab');
  await f.input.fullscreen();assert.equal(f.fullscreens,1);await f.input.release();assert.equal(f.doc.pointerLockElement,null);assert.ok(f.doc.fullscreenElement);f.input.stop();
});
test('GUI absolute coordinates use rendered canvas rect with letterbox and DPI',()=>{
  const f=fixture();const p=pointerPosition(f.canvas,{clientX:580,clientY:330,movementX:2,movementY:3});assert.equal(p.x,960);assert.equal(p.y,540);assert.equal(p.dx,4);assert.equal(p.dy,6);f.input.stop();
});
test('late capture is released if a native reconnect changes epoch',async()=>{
  const f=fixture();let resolve;f.canvas.requestPointerLock=()=>new Promise(done=>{resolve=()=>{f.doc.pointerLockElement=f.canvas;done();};});
  const pending=f.input.capture();f.input.update({connectionEpoch:4,lastInputSeq:0},'a'.repeat(32),'lease');resolve();await assert.rejects(pending);assert.equal(f.doc.pointerLockElement,null);f.input.stop();
});
test('queued input cannot cross native runtime or connection generation',async()=>{
  const f=fixture();const pending=f.input.sendText('你好');f.input.update({connectionEpoch:4,lastInputSeq:0},'b'.repeat(32),'newlease');await assert.rejects(pending);assert.equal(f.sent.length,0);await f.input.sendText('新连接');assert.equal(f.sent[0].generation,'newlease');assert.equal(f.sent[0].value.expectedEpoch,4);f.input.stop();
});

test('F8 release cancels pending capture without leaving fullscreen',async()=>{
  const f=fixture();await f.input.fullscreen();let resolve;
  f.canvas.requestPointerLock=()=>new Promise(done=>{resolve=()=>{f.doc.pointerLockElement=f.canvas;done();};});
  const pending=f.input.capture();await f.input.release();resolve();
  await assert.rejects(pending);assert.equal(f.doc.pointerLockElement,null);assert.ok(f.doc.fullscreenElement);f.input.stop();
});
test('opening a menu cancels pending capture',async()=>{
  const f=fixture();let resolve;
  f.canvas.requestPointerLock=()=>new Promise(done=>{resolve=()=>{f.doc.pointerLockElement=f.canvas;done();};});
  const pending=f.input.capture();await f.input.menu();resolve();
  await assert.rejects(pending);assert.equal(f.doc.pointerLockElement,null);f.input.stop();
});


test('an asynchronously rejected capture releases its native grab and explains unsupported windows',async()=>{
  const f=fixture();await f.input.fullscreen();
  f.canvas.requestPointerLock=()=>Promise.reject(Error('The root document of this element is not valid for pointer lock.'));
  await assert.rejects(f.input.capture(),/当前窗口不支持鼠标捕获.*Chrome\/Edge.*菜单仍可用/);
  assert.deepEqual(f.sent.map(item=>item.value.events[0].type),['grab','release']);
  assert.equal(f.doc.pointerLockElement,null);assert.ok(f.doc.fullscreenElement);assert.match(f.input.snapshot().error,/当前窗口不支持/);f.input.stop();
});
test('a synchronously thrown capture failure is localized and leaves native input released',async()=>{
  const f=fixture();f.canvas.requestPointerLock=()=>{throw Error('The root document of this element is not valid for pointer lock.');};
  await assert.rejects(f.input.capture(),/当前窗口不支持鼠标捕获/);
  assert.deepEqual(f.sent.map(item=>item.value.events[0].type),['release']);assert.equal(f.input.snapshot().ready,true);f.input.stop();
});
test('an old capture rejection cannot release or overwrite a newer capture in the same connection',async()=>{
  const f=fixture();let rejectOld;
  f.canvas.requestPointerLock=()=>new Promise((_resolve,reject)=>{rejectOld=reject;});
  const old=f.input.capture();await Promise.resolve();await Promise.resolve();
  f.canvas.requestPointerLock=async()=>{f.doc.pointerLockElement=f.canvas;};await f.input.capture();
  const before=f.sent.length;rejectOld(Error('The root document of this element is not valid for pointer lock.'));
  await assert.rejects(old,/当前窗口不支持鼠标捕获/);assert.equal(f.sent.length,before);assert.equal(f.doc.pointerLockElement,f.canvas);assert.equal(f.input.snapshot().error,'');f.input.stop();
});
test('an old capture rejection cannot release native input after the runtime or connection changes',async()=>{
  const f=fixture();let rejectOld;
  f.canvas.requestPointerLock=()=>new Promise((_resolve,reject)=>{rejectOld=reject;});
  const old=f.input.capture();await Promise.resolve();await Promise.resolve();
  f.input.update({connectionEpoch:4,lastInputSeq:0},'b'.repeat(32),'newlease');
  f.canvas.requestPointerLock=async()=>{f.doc.pointerLockElement=f.canvas;};await f.input.capture();
  const before=f.sent.length;rejectOld(Error('The root document of this element is not valid for pointer lock.'));
  await assert.rejects(old,/当前窗口不支持鼠标捕获/);assert.equal(f.sent.length,before);assert.equal(f.doc.pointerLockElement,f.canvas);assert.equal(f.input.snapshot().error,'');assert.equal(f.sent.at(-1).value.expectedRuntimeOwner,'b'.repeat(32));f.input.stop();
});


test('an old successful capture cannot exit a newer successful capture on the same canvas',async()=>{
  const f=fixture();let resolveOld;
  f.canvas.requestPointerLock=()=>new Promise(resolve=>{resolveOld=()=>{f.doc.pointerLockElement=f.canvas;resolve();};});
  const old=f.input.capture();await Promise.resolve();await Promise.resolve();
  f.canvas.requestPointerLock=async()=>{f.doc.pointerLockElement=f.canvas;};await f.input.capture();
  const before=f.sent.length;resolveOld();await assert.rejects(old,/会话已改变/);
  assert.equal(f.doc.pointerLockElement,f.canvas);assert.equal(f.input.snapshot().pointerLocked,true);assert.equal(f.sent.length,before);f.input.stop();
});
test('an old successful capture cannot exit a newer successful capture after a native epoch change',async()=>{
  const f=fixture();let resolveOld;
  f.canvas.requestPointerLock=()=>new Promise(resolve=>{resolveOld=()=>{f.doc.pointerLockElement=f.canvas;resolve();};});
  const old=f.input.capture();await Promise.resolve();await Promise.resolve();
  f.input.update({connectionEpoch:4,lastInputSeq:0},'b'.repeat(32),'newlease');
  f.canvas.requestPointerLock=async()=>{f.doc.pointerLockElement=f.canvas;};await f.input.capture();
  const before=f.sent.length;resolveOld();await assert.rejects(old,/会话已改变/);
  assert.equal(f.doc.pointerLockElement,f.canvas);assert.equal(f.input.snapshot().pointerLocked,true);assert.equal(f.sent.length,before);assert.equal(f.sent.at(-1).value.expectedEpoch,4);f.input.stop();
});
