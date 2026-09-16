import assert from "node:assert/strict";
import {setMicContext,setMicConnection,toggleMic,ingestMic,disposeMic,getMicState} from "../frontend/main-deck/src/state/micStore";
import {setChatState,initialChatState} from "../frontend/main-deck/src/chat/stateCore";
import {OFFLINE_HOLD_MS} from "../frontend/main-deck/src/connectionUi";

export async function run() {
  const pause=(ms=0)=>new Promise(resolve=>setTimeout(resolve,ms));
  let open=true,stops=0;
  const accepted:Record<string,unknown>[]=[];
  let worklet:{port:{onmessage:((event:MessageEvent)=>void)|null}};
  const track={stop(){stops++;},addEventListener(){},removeEventListener(){}};
  const stream={getTracks:()=>[track],getAudioTracks:()=>[track]};
  Object.defineProperty(navigator,"mediaDevices",{configurable:true,value:{getUserMedia:async()=>stream,addEventListener(){},removeEventListener(){}}});
  class AudioContextFixture {
    sampleRate=48000;state="running";destination={};audioWorklet={addModule:async()=>{}};
    async resume(){} async close(){}
    createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createGain(){return {gain:{value:0},connect(){},disconnect(){}};}
  }
  class WorkletFixture {
    port={onmessage:null as ((event:MessageEvent)=>void)|null};
    constructor(){worklet=this;}
    connect(){}disconnect(){}
  }
  Object.assign(window,{AudioContext:AudioContextFixture});
  Object.assign(globalThis,{AudioWorkletNode:WorkletFixture});
  const frame=()=>worklet.port.onmessage?.({data:{type:"frame",samples:new Float32Array([.2,-.2,.2]),rms:.2,frames:3}} as MessageEvent);
  setChatState({...initialChatState(),sessionId:"mic-owner",connected:true});
  setMicContext({isOpen:()=>open,send:command=>{if(!open)return false;accepted.push(command);return true;},notify(){}});
  try {
    setMicConnection("connected");toggleMic();await pause();
    assert.equal(getMicState().phase,"recording");
    open=false;setMicConnection("connecting");await pause(20);
    assert.equal(stops,0,"reconnect probe does not stop the microphone track");
    open=true;setMicConnection("connected");frame();toggleMic();
    assert.equal(getMicState().phase,"transcribing");
    const sent=accepted.at(-1)!;
    setMicConnection("connecting");setMicConnection("connected");
    ingestMic({type:"transcript",request_id:sent.request_id,session_id:"mic-owner",cancelled:true});
    assert.equal(getMicState().phase,"idle","in-flight response survives a brief reconnect probe");
    toggleMic();await pause();frame();open=false;setMicConnection("connecting");toggleMic();
    assert.equal(getMicState().phase,"transcribing","unsent encoded clip waits within the offline hold");
    assert.equal(accepted.length,1);
    open=true;setMicConnection("connected");setMicConnection("connected");
    assert.equal(accepted.length,2,"unsent clip retries once; accepted clips are never replayed");
    const retry=accepted.at(-1)!;ingestMic({type:"transcript",request_id:retry.request_id,session_id:"mic-owner",cancelled:true});
    toggleMic();await pause();const before=stops;
    open=false;setMicConnection("offline");await pause(OFFLINE_HOLD_MS+30);
    assert.equal(stops,before+1,"confirmed offline tears down capture resources");
    assert.equal(getMicState().phase,"idle");
    console.log("Mic reconnect: capture/transcription survive probes, unsent clip retries once, stable offline cleans up");
  } finally {disposeMic();}
}
