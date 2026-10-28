import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const source=(await readFile(new URL('./viewer.js',import.meta.url),'utf8')).replace(/^import .*;\n/gm,'');
async function fixture({fragment="viewer=12345678-1234-4321-8123-123456789abc",windowName=""}={}){
  let state={native:{id:'alpha',connectionEpoch:2,width:1280,height:720},runtimeOwner:'a'.repeat(32),controller:null},poll,onMediaStatus,unavailable=0,stops=0,clears=0,clock=1000,fetches=0;
  const messages=[],canvas={width:1280,height:720,style:{},dataset:{},getContext:()=>({clearRect(){clears++;}})},elements=new Map([['videoStream',canvas],['surface',{getBoundingClientRect:()=>({width:1280,height:720})}],['viewer-status',{}],['viewer-lock',{}],['viewer-menu',{}],['viewer-release',{}]]);
  const context={URL,location:{href:'https://example.test/t/abcdefghijklmnopqrstuvwx/viewer.html#'+fragment,origin:'https://example.test'},performance:{now:()=>clock},
    document:{getElementById:id=>elements.get(id),addEventListener(){}},window:{name:windowName,addEventListener(){}},parent:{postMessage:value=>messages.push(value)},setInterval(fn){poll=fn;},
    async fetch(url){fetches++;return {ok:true,json:async()=>String(url).endsWith('api/bootstrap')?{csrf:'csrf'}:state};},
    createViewerInput:()=>({snapshot:()=>({ready:true}),update(){},unavailable(){unavailable++;},stop(){},capture(){},release(){},menu(){}}),
    createM1Player:({onStatus})=>{onMediaStatus=onStatus;return {start(){},stop(){stops++;},snapshot:()=>({decodedFrames:60,receivedBytes:125000,audioFrames:0})};}};
  vm.runInNewContext(source,context);await new Promise(resolve=>setImmediate(resolve));
  return {messages,get fetches(){return fetches;},emitFrame(){clock+=1000;onMediaStatus({type:'video-frame',decodedFrames:60});},setState:value=>{state={...state,...value};},async poll(){poll();await new Promise(resolve=>setImmediate(resolve));},get unavailable(){return unavailable;},get stops(){return stops;},get clears(){return clears;}};
}
test('a native epoch change stops old playback and requests an explicit reconnect',async()=>{
  const f=await fixture();f.setState({native:{id:'alpha',connectionEpoch:3,width:1280,height:720}});await f.poll();
  assert.equal(f.unavailable,1);assert.equal(f.stops,1);assert.equal(f.clears,1);assert.ok(f.messages.some(value=>value.type==='viewer-error'&&value.message.includes('重新连接画面')));
});
test('a native host change stops old playback instead of adopting its ownership',async()=>{
  const f=await fixture();f.setState({runtimeOwner:'b'.repeat(32)});await f.poll();assert.equal(f.unavailable,1);assert.equal(f.stops,1);assert.ok(f.messages.some(value=>value.type==='viewer-error'));
});


test('stats carry the fragment UUID even if the browser clears window.name',async()=>{
  const f=await fixture({windowName:''});f.emitFrame();const stats=f.messages.find(value=>value.type==='stats');
  assert.equal(stats.viewerGeneration,'12345678-1234-4321-8123-123456789abc');assert.equal(stats.data.latest.mbps,1);assert.equal(stats.data.clientFps,60);
});
test('invalid viewer generation fails before opening authenticated media',async()=>{
  const f=await fixture({fragment:'viewer=not-a-uuid'});assert.equal(f.fetches,0);assert.equal(f.stops,0);assert.ok(f.messages.some(value=>value.type==='viewer-error'&&value.message.includes('连接标识无效')));
});
