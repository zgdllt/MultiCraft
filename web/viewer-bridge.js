/** Same-origin iframe bridge keeps M0 layout and native runtime ownership separate. */
export function createGameInputController({iframe,onStatus=()=>{}}){
  let last={ready:false,pointerLocked:false,fullscreen:false,guiMode:false,error:''};
  const viewer=()=>iframe.contentWindow?.multicraftViewer;
  const emit=()=>{last=viewer()?.input.snapshot()||{...last,ready:false};onStatus(last);};
  window.addEventListener('message',event=>{if(event.origin!==location.origin||event.source!==iframe.contentWindow||event.data?.viewerGeneration!==iframe.dataset.viewerGeneration)return;if(event.data?.type==='input-status'){last=event.data.state;onStatus(last);}if(event.data?.type==='viewer-error'){last={...last,ready:false,error:event.data.message};onStatus(last);}});
  return {sendChat(text){const input=viewer()?.input;if(!input)throw Error('游戏画面尚未就绪');return input.sendChat(text);},snapshot:()=>({...last}),bind:emit,async enter(){const input=viewer()?.input;if(!input)throw Error('游戏画面尚未就绪');await input.capture();emit();},
    async fullscreen(){const input=viewer()?.input;if(!input)throw Error('游戏画面尚未就绪');await input.fullscreen();emit();},
    release(){viewer()?.input.release();emit();},useGuiPointer(){viewer()?.input.release();emit();},sendMenuKey(){const input=viewer()?.input;if(!input)return false;input.menu().catch(error=>{last={...last,error:error.message};onStatus(last);});return true;}};
}
