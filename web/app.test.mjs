import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const source=(await readFile(new URL('./app.js',import.meta.url),'utf8')).replace(/^import .*;\n/gm,'');
const generation='12345678-1234-4321-8123-123456789abc';
const limits={width:[320,1920],height:[180,1080],fps:[10,1000],bitrateKbps:[500,20000]};
function element(){const classes=new Set();return {value:'',hidden:false,disabled:false,textContent:'',dataset:{},options:[],selectedOptions:[],listeners:{},classList:{toggle(name,enabled){if(enabled)classes.add(name);else classes.delete(name);},contains:name=>classes.has(name),add:name=>classes.add(name),remove:name=>classes.delete(name)},addEventListener(type,fn){this.listeners[type]=fn;}};}
async function fixture(){
  const elements=new Map(),windowListeners={};let poll,stateError=false,configError=false;
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  get('game').hidden=true;get('resolution').options=[{value:'1280,720',disabled:false},{value:'custom',disabled:false}];get('resolution').selectedOptions=[get('resolution').options[0]];
  get('stream').contentWindow={};
  const context={URL,console,location:{href:'https://example.test/t/abcdefghijklmnopqrstuvwx/',origin:'https://example.test',reload(){},replace(){}},performance:{now:()=>1000},crypto:{randomUUID:()=>generation},
    document:{title:'',getElementById:get},window:{addEventListener(type,fn){windowListeners[type]=fn;}},setInterval(fn){poll=fn;},
    createGameInputController:()=>({snapshot:()=>({}),bind(){},release(){}}),
    async fetch(url){const path=new URL(url).pathname;let status=200,data={};
      if(path.endsWith('/api/bootstrap'))data={csrf:'csrf',limits,policyRevision:0};
      else if(path.endsWith('/api/me')){status=401;data={error:'not logged in'};}
      else if(path.endsWith('/api/state')){if(stateError){status=503;data={error:'temporary backend failure'};}else data={account:{isOp:false},limits,policyRevision:0,stream:{effective:{width:1280,height:720,fps:60,bitrateKbps:6000,adaptive:true}},native:{width:1280,height:720},runtime:{nativeReady:true}};}
      else if(path.endsWith('/api/stream-config')&&configError){status=400;data={error:'invalid user config'};}
      else if(path.endsWith('/api/lease'))data={url:'/t/abcdefghijklmnopqrstuvwx/viewer.html'};
      return {ok:status<400,status,text:async()=>JSON.stringify(data)};
    }};
  vm.runInNewContext(source,context);await new Promise(resolve=>setImmediate(resolve));
  return {get,context,setStateError(value){stateError=value;},setConfigError(value){configError=value;},async poll(){poll();await new Promise(resolve=>setImmediate(resolve));},async click(id){await get(id).listeners.click();},async emit(value){await windowListeners.message({origin:context.location.origin,source:get('stream').contentWindow,data:value});}};
}
test('viewer connection carries its generation in the URL fragment',async()=>{
  const f=await fixture();f.get('connection-message').textContent='old connection error';await f.click('connect');const url=new URL(f.get('stream').src);
  assert.equal(url.hash,'#viewer='+generation);assert.equal(f.get('stream').dataset.viewerGeneration,generation);assert.equal(f.get('connection-message').textContent,'');assert.equal(f.get('connection-message').hidden,true);
});
test('matching iframe stats update actual parent metrics and stale generations cannot overwrite them',async()=>{
  const f=await fixture();await f.click('connect');const stats={type:'stats',viewerGeneration:generation,data:{client:{resolution:'1280×720',decoder:'WebCodecs VideoDecoder'},clientFps:59.5,latest:{mbps:5.25}}};
  await f.emit(stats);assert.equal(f.get('received').textContent,'1280×720 · 59.5 fps');assert.equal(f.get('decoder').textContent,'WebCodecs VideoDecoder');assert.equal(f.get('received-bitrate').textContent,'5.25 Mbps');
  await f.emit({...stats,viewerGeneration:'obsolete',data:{}});assert.equal(f.get('received-bitrate').textContent,'5.25 Mbps');
});
test('state refresh errors clear on recovery without erasing a user configuration failure',async()=>{
  const f=await fixture();f.get('game').hidden=false;f.setConfigError(true);await f.get('stream-config').onsubmit({preventDefault(){}});
  assert.equal(f.get('config-message').textContent,'invalid user config');assert.equal(f.get('config-message').classList.contains('error'),true);
  f.setStateError(true);await f.poll();assert.equal(f.get('state-message').textContent,'temporary backend failure');assert.equal(f.get('state-message').hidden,false);assert.equal(f.get('config-message').textContent,'invalid user config');
  f.setStateError(false);await f.poll();assert.equal(f.get('state-message').hidden,true);assert.equal(f.get('state-message').textContent,'');assert.equal(f.get('config-message').textContent,'invalid user config');assert.equal(f.get('config-message').classList.contains('error'),true);
});
