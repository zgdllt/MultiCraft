import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source=(await readFile(new URL('./m1-app.js',import.meta.url),'utf8'))
  .replace("import {createM1Player} from './m1-player.js';",'')+'\nglobalThis.testRefresh=refresh;';

function classes(){const values=new Set();return {add(value){values.add(value);},remove(value){values.delete(value);},toggle(value,enabled){if(enabled)values.add(value);else values.delete(value);},contains:value=>values.has(value)};}
function node(){return {dataset:{},classList:classes(),textContent:'',hidden:false,disabled:false};}
function fixture(){
  const elements=new Map(['sessions','login','workbench','message','host-status','retry'].map(id=>[id,node()]));
  const articles=[],players=[],intervals=[],listeners=new Map(),posts=[];
  const state={csrf:'csrf',controllers:{A:'control-A',B:'control-B'},native:{pid:1,runtimeOwner:'a'.repeat(32),audioMode:'loopback-pcm',sessions:['A','B'].map(id=>({id,closed:false,failure:null,connectionEpoch:2,lastInputSeq:0,dimension:'overworld',mouseGrabbed:true}))}};
  let failure,postGate;
  const document={activeElement:null,pointerLockElement:null,
    getElementById:id=>elements.get(id),
    addEventListener(type,callback){listeners.set(type,[...(listeners.get(type)||[]),callback]);},
    exitPointerLock(){this.pointerLockElement=null;for(const callback of listeners.get('pointerlockchange')||[])callback();},
    createElement(){
      const article=node(),status=node(),input=node();
      const context={clearCalls:0,clearRect(){this.clearCalls++;}};
      const canvas={...node(),width:1280,height:720,tabIndex:0,getContext:()=>context,
        getBoundingClientRect:()=>({left:0,top:0,width:1280,height:720}),focus(){document.activeElement=canvas;}};
      const buttons=['video','audio','lock','menu','release','disconnect','connect','respawn','chat'].map(action=>({...node(),dataset:{action}}));
      article.querySelector=selector=>selector==='canvas'?canvas:selector==='.status'?status:input;
      article.querySelectorAll=()=>buttons;
      article.canvas=canvas;article.status=status;article.buttons=buttons;article.context=context;article.input=input;
      articles.push(article);return article;
    }};
  elements.get('sessions').append=()=>{};
  const window={addEventListener(){}};
  const context={document,window,location:{protocol:'https:',host:'example.test'},console,
    setInterval(callback,ms){const timer={callback,ms};intervals.push(timer);return timer;},clearInterval(){},
    createM1Player(options){
      const player={stopCalls:0,startCalls:0,decodedFrames:0,videoState:3,start(){this.startCalls++;this.videoState=1;},startAudio:async()=>{},
        stop(){this.stopCalls++;this.videoState=3;options.onStatus({type:'video-closed'});},snapshot(){return {decodedFrames:this.decodedFrames,videoState:this.videoState};},
        emit(value){if(value.type==='video-frame')this.decodedFrames=value.decodedFrames;options.onStatus(value);},isCurrent:options.isCurrent};
      players.push(player);return player;
    },
    async fetch(path,options){
      if(path==='/api/state'){
        if(failure)throw failure;
        return {ok:true,json:async()=>JSON.parse(JSON.stringify(state))};
      }
      posts.push({path,body:JSON.parse(options.body),headers:options.headers});
      if(postGate){const pending=postGate;postGate=undefined;await pending;}
      return {ok:true,json:async()=>({ok:true})};
    }};
  vm.runInNewContext(source,context,{filename:'m1-app.js'});
  return {articles,players,intervals,posts,state,elements,document,window,refresh:()=>context.testRefresh(),
    fail(error){failure=error;},recover(){failure=undefined;},stallNextPost(promise){postGate=promise;}};
}

async function readyFixture(){const f=fixture();await f.refresh();for(const article of f.articles)await article.buttons.find(button=>button.dataset.action==='video').onclick();return f;}

test('closed A clears its old image and input while B keeps receiving frames',async()=>{
  const f=await readyFixture();
  f.players[0].emit({type:'video-frame',decodedFrames:12});f.players[1].emit({type:'video-frame',decodedFrames:20});
  f.document.activeElement=f.articles[0].canvas;f.document.pointerLockElement=f.articles[0].canvas;
  f.state.native.sessions[0].closed=true;await f.refresh();
  assert.equal(f.players[0].stopCalls,1);assert.equal(f.players[1].stopCalls,0);
  assert.equal(f.document.pointerLockElement,null);
  assert.equal(f.articles[0].dataset.availability,'closed');
  assert.equal(f.articles[0].dataset.decodedFrames,'0');
  assert.match(f.articles[0].status.textContent,/已关闭.*OP 重启/);
  assert.match(f.elements.get('host-status').textContent,/1 \/ 2 个会话运行中/);
  assert.ok(f.articles[0].buttons.every(button=>button.disabled));
  const cleared=f.articles[0].context.clearCalls;
  f.players[0].emit({type:'video-frame',decodedFrames:99});
  assert.ok(f.articles[0].context.clearCalls>cleared,'late decoded frames must be erased');
  assert.match(f.articles[0].status.textContent,/已关闭/);
  assert.equal(f.articles[0].dataset.decodedFrames,'0');
  f.players[1].emit({type:'video-frame',decodedFrames:21});
  assert.match(f.articles[1].status.textContent,/已解码 21 帧/);
});

test('native failure stops only that panel and preserves a clear failure message',async()=>{
  const f=await readyFixture();
  f.state.native.sessions[0].failure='native cleanup exception';await f.refresh();
  assert.equal(f.articles[0].dataset.availability,'failed');
  assert.match(f.articles[0].status.textContent,/运行失败.*OP 重启/);
  assert.equal(f.players[0].stopCalls,1);assert.equal(f.players[1].stopCalls,0);
  await f.refresh();assert.equal(f.players[0].stopCalls,1,'repeated failed snapshots must not enqueue cleanup repeatedly');
});

test('unreachable host clears both live images and can recover without reusing old playback',async()=>{
  const f=await readyFixture();
  f.players[0].emit({type:'video-frame',decodedFrames:12});f.players[1].emit({type:'video-frame',decodedFrames:20});
  f.fail(new Error('backend unavailable'));await assert.rejects(f.refresh(),/backend unavailable/);
  for(const [index,article] of f.articles.entries()){
    assert.equal(f.players[index].stopCalls,1);assert.equal(article.dataset.availability,'unreachable');
    assert.equal(article.dataset.decodedFrames,'0');assert.match(article.status.textContent,/运行时暂不可达/);
    const reconnect=article.buttons.find(button=>button.dataset.action==='video');
    assert.equal(reconnect.disabled,false);assert.equal(reconnect.textContent,'重新检查连接');
  }
  f.recover();f.state.native.runtimeOwner='b'.repeat(32);await f.refresh();
  for(const [index,article] of f.articles.entries()){
    assert.equal(article.dataset.availability,'ready');assert.match(article.status.textContent,/已恢复.*重新接入/);
    assert.equal(f.players[index].startCalls,1,'state recovery must not restart the previous stream');
  }
  f.players[0].emit({type:'video-frame',decodedFrames:99});
  assert.match(f.articles[0].status.textContent,/已恢复.*重新接入/);
  assert.equal(f.articles[0].dataset.decodedFrames,'0');
  assert.equal(f.players[0].isCurrent(),false);
  await f.articles[0].buttons.find(button=>button.dataset.action==='video').onclick();
  assert.equal(f.players.length,3);assert.equal(f.players[2].startCalls,1);assert.equal(f.players[1].startCalls,1);
  assert.equal(f.players[0].isCurrent(),false);assert.equal(f.players[2].isCurrent(),true);
});

test('closing a panel discards queued old input and a late rejection cannot overwrite its terminal status',async()=>{
  const f=await readyFixture();
  let finish;const pending=new Promise(resolve=>{finish=resolve;});f.stallNextPost(pending);
  f.articles[0].canvas.onkeydown({code:'KeyW',key:'w',preventDefault(){}});
  const inFlight=f.intervals.find(timer=>timer.ms===20).callback();
  await Promise.resolve();await Promise.resolve();
  const queuedMenu=f.articles[0].buttons.find(button=>button.dataset.action==='menu').onclick();
  f.state.native.sessions[0].closed=true;await f.refresh();finish();
  await inFlight;await queuedMenu;await Promise.resolve();await Promise.resolve();
  assert.ok(!f.posts.some(post=>post.body.events.some(event=>event.scancode===41)),'old queued menu must never reach a stopped panel');
  assert.match(f.articles[0].status.textContent,/已关闭.*OP 重启/);
  assert.equal(f.articles[0].dataset.availability,'closed');
});

test('an old command and chat cannot retarget a recovered host after waiting for their first request',async()=>{
  for(const action of ['disconnect','chat']){
    const f=await readyFixture();
    let finish;const pending=new Promise(resolve=>{finish=resolve;});f.stallNextPost(pending);
    f.articles[0].input.value='old chat';
    const oldAction=f.articles[0].buttons.find(button=>button.dataset.action===action).onclick();
    await Promise.resolve();await Promise.resolve();
    f.fail(new Error('offline'));await assert.rejects(f.refresh());
    f.recover();f.state.native.runtimeOwner='b'.repeat(32);f.state.controllers.A='new-controller';await f.refresh();
    finish();await oldAction;await Promise.resolve();await Promise.resolve();
    assert.ok(!f.posts.some(post=>post.path.endsWith('/command')));
    assert.ok(!f.posts.some(post=>post.body.events?.some(event=>event.type==='text'&&event.text==='old chat')));
    assert.match(f.articles[0].status.textContent,/已恢复.*重新接入/);
  }
});

test('recovered host can accept new input while an old request remains unresolved',async()=>{
  const f=await readyFixture();
  let finish;const pending=new Promise(resolve=>{finish=resolve;});f.stallNextPost(pending);
  f.articles[0].canvas.onkeydown({code:'KeyW',key:'w',preventDefault(){}});
  const oldFlush=f.intervals.find(timer=>timer.ms===20).callback();await Promise.resolve();await Promise.resolve();
  f.fail(new Error('offline'));await assert.rejects(f.refresh());
  f.recover();f.state.native.runtimeOwner='b'.repeat(32);f.state.controllers.A='new-controller';await f.refresh();
  await f.articles[0].buttons.find(button=>button.dataset.action==='video').onclick();
  f.articles[0].canvas.onkeydown({code:'KeyD',key:'d',preventDefault(){}});
  await f.intervals.find(timer=>timer.ms===20).callback();
  assert.ok(f.posts.some(post=>post.body.expectedRuntimeOwner==='b'.repeat(32)&&post.body.events.some(event=>event.scancode===7)));
  finish();await oldFlush;await Promise.resolve();
  assert.match(f.articles[0].status.textContent,/已恢复.*重新接入/);
});

test('a pointer lock request completing after native close is immediately released',async()=>{
  const f=await readyFixture();
  let complete;
  f.articles[0].canvas.requestPointerLock=()=>new Promise(resolve=>{complete=()=>{f.document.pointerLockElement=f.articles[0].canvas;resolve();};});
  const locking=f.articles[0].buttons.find(button=>button.dataset.action==='lock').onclick();
  await Promise.resolve();await Promise.resolve();
  f.state.native.sessions[0].closed=true;await f.refresh();complete();await locking;
  assert.equal(f.document.pointerLockElement,null);
  assert.match(f.articles[0].status.textContent,/已关闭.*OP 重启/);
});
