import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source=(await readFile(new URL('./m1-player.js',import.meta.url),'utf8'))
  .replace('export function createM1Player','function createM1Player')+'\nglobalThis.factory=createM1Player;';

function fixture(){
  const sockets=[],decoders=[],draws=[],audioStarts=[];let current=true;
  class Socket {
    static OPEN=1;static CLOSING=2;
    constructor(){this.readyState=1;sockets.push(this);}send(){}close(){this.readyState=3;}
    emit(data){this.onmessage({data});}
  }
  class Decoder {
    constructor(options){this.options=options;this.state='unconfigured';this.decodeQueueSize=0;decoders.push(this);}
    configure(){this.state='configured';}close(){this.state='closed';}decode(){}
  }
  class Audio {
    constructor(){this.currentTime=0;this.destination={};}async resume(){}async close(){}
    createBuffer(_channels,frames){return {getChannelData:()=>new Float32Array(frames)};}
    createBufferSource(){return {connect(){},disconnect(){},stop(){},start(){audioStarts.push(true);}};}
  }
  const context={WebSocket:Socket,VideoDecoder:Decoder,AudioContext:Audio,EncodedVideoChunk:class{}};
  vm.runInNewContext(source,context);
  const canvas={width:1280,height:720,getContext:()=>({drawImage:(...args)=>draws.push(args)})};
  const player=context.factory({canvas,videoUrl:'wss://example/video',audioUrl:'wss://example/audio',isCurrent:()=>current});
  return {player,sockets,decoders,draws,audioStarts,invalidate(){current=false;}};
}

test('obsolete decoder output is closed without touching the visible canvas',()=>{
  const f=fixture();f.player.start();
  f.sockets[0].emit(JSON.stringify({type:'video',codec:'avc1.42E01F',width:1280,height:720}));
  let closed=0;const frame={timestamp:1,close(){closed++;}};
  f.decoders[0].options.output(frame);assert.equal(f.draws.length,1);
  f.invalidate();f.decoders[0].options.output(frame);
  assert.equal(f.draws.length,1);assert.equal(closed,2);
  f.player.stop();
});

test('obsolete PCM frames cannot schedule new audio playback',async()=>{
  const f=fixture();f.player.start();await f.player.startAudio();
  const pcm=new ArrayBuffer(16),view=new DataView(pcm);view.setUint32(0,1);view.setBigUint64(4,0n);
  f.sockets[1].emit(pcm);assert.equal(f.audioStarts.length,1);
  f.invalidate();f.sockets[1].emit(pcm);assert.equal(f.audioStarts.length,1);
  f.player.stop();
});
