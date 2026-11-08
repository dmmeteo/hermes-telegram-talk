import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
const source = readFileSync(process.argv[2] || 'static/talk/app.js','utf8');
const tests = [];
const test = (name, run) => tests.push({name,run});
test('Talk assets have content-qualified URLs distinct from the legacy cache', () => {
  const html = readFileSync('static/talk/index.html', 'utf8');
  for (const name of ['app.js', 'app.css']) {
    const hash = createHash('sha256').update(readFileSync(`static/talk/${name}`)).digest('hex').slice(0, 16);
    assert(html.includes(`"${name}?v=${hash}"`), `${name} URL must change with its bytes`);
    assert(!html.includes(`"${name}"`), `${name} must not reuse the legacy cache key`);
  }
});
function fixture() {
  const nodes = new Map();
  function element(id) {
    if (!nodes.has(id)) nodes.set(id,{hidden:false,textContent:'',disabled:false,dataset:{},style:{setProperty(){}},
      setAttribute(name,value){this[name]=value;},querySelector(){return this;},addEventListener(){},removeEventListener(){},
      getContext(){return new Proxy({}, {get:()=>()=>{}});},pause(){},srcObject:null});
    return nodes.get(id);
  }
  const context = vm.createContext({document:{getElementById:element,documentElement:element('html')},
    window:{addEventListener(){},removeEventListener(){}}, location:{hash:'',href:'http://localhost/tg-voice/',origin:'http://localhost'},
    URL, URLSearchParams, AbortController, DOMException, crypto:globalThis.crypto,
    matchMedia:()=>({matches:false}), getComputedStyle:()=>({color:'#ffffff'}),
    setTimeout, clearTimeout, cancelAnimationFrame(){}, requestAnimationFrame:()=>1, performance,
    fetch:async()=>{throw new Error('Unexpected network');}});
  vm.runInContext(source,context);
  vm.runInContext(`
    const owner = {id:'conversation',auth:'synthetic',pending:[],sequence:0,batchMax:4,retries:0};
    const sent = [];
    const current = {auth:'synthetic',phase:'connecting',abort:new AbortController(),nodes:[],
      seen:new Set(),delegations:new Set(),conversation:owner,voiceId:'voice',muted:false,
      stream:{getAudioTracks:()=>[track],getTracks:()=>[track]},
      channel:{readyState:'open',send:m=>sent.push(JSON.parse(m)),close(){}},
      peer:{connectionState:'connected',close(){}},audio:{state:'running',close:async()=>{}},
      playing:true, outputLevel:0, inputLevel:0};
    const track = {enabled:false,stop(){this.stopped=true;}};
    call = current; conversation = owner;
    initData = ()=>'synthetic';
    let flushCalls = 0;
    const realFlush = flush;
    flush = async()=>{flushCalls++;};
    function emit(event) { onMessage(current,{data:JSON.stringify(event)}); }
  `,context);
  return {context,nodes,run:code=>vm.runInContext(code,context)};
}

test('fragments and public Realtime events never persist',()=>{
 const f=fixture(); f.run(`emit({type:'input_transcript.added',item:{text:'partial'}});
 emit({type:'output_transcript.added',item:{text:'partial'}});
 emit({type:'response.audio_transcript.done',transcript:'wrong dialect'});`);
 assert.equal(f.run('owner.pending.length'),0);
});
test('turn.done settles exact role/text in arrival order',()=>{
 const f=fixture(); f.run(`emit({type:'turn.done',turn:{role:'user',transcript:' final user '}});
 emit({type:'turn.done',turn:{role:'assistant',transcript:'final reply'}});`);
 assert.equal(f.run('JSON.stringify(owner.pending.map(p=>[p.event.sequence,p.event.role,p.event.text]))'),JSON.stringify([[0,'user','final user'],[1,'assistant','final reply']]));
});
test('duplicate turn IDs and duplicate no-ID settlements are suppressed',()=>{
 const f=fixture();f.run(`for(let i=0;i<2;i++)emit({type:'turn.done',turn:{id:'turn1',role:'user',transcript:'same'}});
 for(let i=0;i<2;i++)emit({type:'turn.done',turn:{role:'assistant',transcript:'same'}});`);
 assert.equal(f.run('owner.pending.length'),2);
});
test('new fragment permits a later identical spoken turn',()=>{
 const f=fixture();f.run(`emit({type:'turn.done',turn:{role:'user',transcript:'yes'}});
 emit({type:'input_transcript.added',item:{text:'yes'}});
 emit({type:'turn.done',turn:{role:'user',transcript:'yes'}});`);
 assert.equal(f.run('owner.pending.length'),2);
});
test('long final text splits within backend limits without losing Unicode',()=>{
 const f=fixture();f.run(`emit({type:'turn.done',turn:{role:'user',transcript:'😀'.repeat(2001)}});`);
 assert.equal(f.run('owner.pending.length'),2);
 assert.equal(f.run("owner.pending.map(p=>p.event.text).join('')==='😀'.repeat(2001)"),true);
});
test('invalid roles, empty text and tool output are ignored' ,()=>{
 const f=fixture();f.run(`for(const role of ['system','tool','reasoning'])emit({type:'turn.done',turn:{role,transcript:'private'}});
 emit({type:'turn.done',turn:{role:'user',transcript:' '}}); emit({type:'turn.done',turn:{role:'user',transcript:123}});`);
 assert.equal(f.run('owner.pending.length'),0);
});
test('handshake and playing output required before greeting',()=>{
 const f=fixture();f.run(`owner.greeting=true; advance(current);`);assert.equal(f.run('sent.length'),0);
 f.run(`current.handshake=true;current.playing=false;advance(current);`);assert.equal(f.run('sent.length'),0);
 f.run(`current.playing=true;advance(current);advance(current);`);
 assert.equal(f.run('sent.length'),1);assert.equal(f.run('sent[0].type'),'session.context.append');
 assert.equal(f.run('track.enabled'),false); assert.equal(f.run('current.phase'),'greeting');
});
test('greeting disabled becomes ready only after actual handshake',()=>{
 const f=fixture(); f.run(`owner.greeting=false;advance(current);`);assert.equal(f.run('track.enabled'),false);
 f.run(`current.handshake=true;advance(current);`);assert.equal(f.run('track.enabled'),true);
});
test('mute cannot reopen mic before ready',()=>{
 const f=fixture();f.run(`toggleMute();updateMic(current);`);assert.equal(f.run('track.enabled'),false);
 f.run(`current.phase='ready';toggleMute();`);assert.equal(f.run('track.enabled'),false);
 f.run('toggleMute()');assert.equal(f.run('track.enabled'),true);
});
test('delegation is refused once through subscription protocol',()=>{
 const f=fixture();f.run(`for(let i=0;i<2;i++)emit({type:'delegation.created',item:{type:'delegation',id:'job1',target:'client'}});`);
 assert.equal(f.run('sent.length'),1);assert.equal(f.run('sent[0].type'),'delegation.context.append');
 assert.match(f.run('sent[0].content[0].text'),/Nothing was executed/);
});
test('stale async data cannot write into replacement call',()=>{
 const f=fixture();f.run(`call=null;emit({type:'turn.done',turn:{role:'user',transcript:'stale'}});`);
 assert.equal(f.run('owner.pending.length'),0);
});
test('RMS measures silence and known incoming samples',()=>{
 const f=fixture();assert.equal(f.run('rms({getFloatTimeDomainData:a=>a.fill(0)},new Float32Array(4))'),0);
 assert.equal(f.run('rms({getFloatTimeDomainData:a=>a.fill(.25)},new Float32Array(4))'),.25);
});
test('retries keep the exact event IDs and sequences',async()=>{
 const f=fixture();f.run(`emit({type:'turn.done',turn:{role:'user',transcript:'retry'}});
 const original=JSON.stringify(owner.pending);let attempts=[];
 post=async(route,body)=>{attempts.push(body);if(attempts.length===1)throw new Error('network');
 return {conversation_id:owner.id,accepted_ids:body.events.map(e=>e.id)};};
 setTimeout=()=>1;`);
 await f.run('realFlush(owner)');assert.equal(f.run('JSON.stringify(owner.pending)===original'),true);
 await f.run('realFlush(owner)');assert.equal(f.run('owner.pending.length'),0);
 assert.equal(f.run('JSON.stringify(attempts[0])===JSON.stringify(attempts[1])'),true);
});
test('exhausted saving retries expose a usable retry without reopening media',async()=>{
 const f=fixture();f.run(`emit({type:'turn.done',turn:{role:'user',transcript:'save me'}});
 current.phase='ready';owner.retries=3;post=async()=>{throw new Error('network');};`);
 await f.run('realFlush(owner)');
 assert.equal(f.nodes.get('start').hidden,false);
 assert.equal(f.nodes.get('start').textContent,'Retry saving');
 const before=f.run('flushCalls');await f.run('start()');assert.equal(f.run('flushCalls'),before+1);
 assert.equal(f.run('call===current'),true);assert.equal(f.run('owner.retries'),0);
});
test('foreign identity prevents queued event retry',async()=>{
 const f=fixture();f.run(`emit({type:'turn.done',turn:{role:'user',transcript:'old account'}});
 initData=()=>'different';post=()=>{throw new Error('must not send');};`);
 await f.run('realFlush(owner)');assert.equal(f.run('owner.pending.length'),1);
});
test('authentication refusal discards pending events',async()=>{
 const f=fixture();f.run(`emit({type:'turn.done',turn:{role:'user',transcript:'pending'}});
 call=null;post=async()=>{const e=new Error('refused');e.status=401;throw e;};`);
 await f.run('realFlush(owner)');assert.equal(f.run('owner.pending.length'),0);
 assert.equal(f.run('owner.disabled'),true);
});
test('transcript shows interim text, replaces it with the final correction and shows a turn once',()=>{
 const f=fixture();f.run(`emit({type:'input_transcript.added',item:{text:'Synthetic par'}});
 emit({type:'input_transcript.added',item:{text:'tial'}});`);
 assert.equal(f.run('JSON.stringify(transcript.lines)'),JSON.stringify([{role:'user',text:'Synthetic partial',state:'interim'}]));
 f.run(`emit({type:'turn.done',turn:{id:'u1',role:'user',transcript:'Synthetic corrected final'}});
 emit({type:'turn.done',turn:{id:'u1',role:'user',transcript:'Synthetic corrected final'}});`);
 assert.equal(f.run('JSON.stringify(transcript.lines)'),JSON.stringify([{role:'user',text:'Synthetic corrected final',state:'final'}]));
 assert.equal(f.run('sent.length'),0,'the transcript is display only: nothing is sent for it');
});
test('transcript marks an assistant reply the user spoke over and cuts unsettled lines on reconnect',()=>{
 const f=fixture();f.run(`emit({type:'output_transcript.added',item:{text:'Synthetic long answer '}});
 emit({type:'input_transcript.added',item:{text:'wait'}});
 emit({type:'turn.done',turn:{id:'a1',role:'assistant',transcript:'Synthetic long answer'}});
 emit({type:'output_transcript.added',item:{text:'Half a sentence'}});`);
 assert.deepEqual(JSON.parse(f.run('JSON.stringify(transcript.lines.map(l=>[l.role,l.state]))')),[['assistant','interrupted'],['user','interim'],['assistant','interim']]);
 f.run('transcriptCut()');
 assert.deepEqual(JSON.parse(f.run('JSON.stringify(transcript.lines.map(l=>[l.role,l.state,l.text]))')),[['assistant','interrupted','Synthetic long answer'],['user','cut','wait'],['assistant','cut','Half a sentence']]);
 assert.equal(f.run('owner.pending.some(e=>e.event.text==="Half a sentence")'),false,'unsettled text is never saved');
});
test('reply voice: hidden for one voice, a live change asks for an explicit reconnect',async()=>{
 const f=fixture();
 f.run(`voiceState={current:'cove',default:'cove',choices:[{id:'cove',label:'Cove'}],applies:'next_connection'};current.voice='cove';renderVoice(current);`);
 assert.equal(f.nodes.get('voice-box').hidden,true);
 f.run(`voiceState={current:'cove',default:'cove',choices:[{id:'cove',label:'Cove'},{id:'marin',label:'Marin'}],applies:'next_connection'};renderVoice(current);`);
 assert.equal(f.nodes.get('voice-box').hidden,false);assert.equal(f.nodes.get('reconnect').hidden,true);
 f.run(`globalThis.posted=[];post=async(route,body)=>{posted.push([route,body]);return {voice:{...voiceState,current:body.voice}};};
 document.getElementById('voice').value='marin';`);
 await f.run('chooseVoice()');
 assert.equal(f.run('JSON.stringify(posted)'),JSON.stringify([['voice',{initData:'synthetic',voice:'marin'}]]));
 assert.equal(f.nodes.get('reconnect').hidden,false,'the live call keeps its voice until an explicit reconnect');
 assert.equal(f.nodes.get('voice-note').hidden,false);
 assert.equal(f.run('call===current'),true,'choosing a voice never drops the call by itself');
});
test('silent media is neutral and only enabled microphone energy means listening',()=>{
 const f=fixture();f.run(`current.phase='ready';current.handshake=true;updateMic(current);render();`);
 assert.equal(f.nodes.get('orb').dataset.state,'silent');
 f.run(`current.inputLevel=.1;render();`);assert.equal(f.nodes.get('orb').dataset.state,'listening');
 f.run(`current.muted=true;updateMic(current);current.outputLevel=.2;render();`);
 assert.equal(f.run('track.enabled'),false);assert.equal(f.nodes.get('orb').dataset.state,'speaking');
 f.run(`remoteAudio.muted=true;render();`);assert.equal(f.nodes.get('orb').dataset.state,'muted');
 f.run(`remoteAudio.muted=false;remoteAudio.volume=0;render();`);assert.equal(f.nodes.get('orb').dataset.state,'muted');
 f.run(`remoteAudio.volume=1;current.outputLevel=0;render();`);assert.equal(f.nodes.get('orb').dataset.state,'muted');
 f.run(`current.muted=false;current.playing=false;render();`);assert.notEqual(f.nodes.get('orb').dataset.state,'speaking');
});
test('mute preference survives a replacement call and audio state transitions',()=>{
 const f=fixture();f.run(`current.phase='ready';toggleMute();current.phase='blocked';updateMic(current);current.phase='ready';updateMic(current);`);
 assert.equal(f.run('track.enabled'),false);assert.equal(f.run('microphoneMuted'),true);
});
test('an avatar body completed after call replacement cannot overwrite the new avatar',async()=>{
 const f=fixture();f.run(`
 let releaseBlob;
 globalThis.URL=class extends URL {static createObjectURL(){return 'blob:stale';} static revokeObjectURL(){}};
 fetch=async()=>({ok:true,blob:()=>new Promise(resolve=>{releaseBlob=resolve;})});
 document.getElementById('avatar').src='blob:new-call';
 globalThis.refresh=refreshAvatar(current);
 `);
 await Promise.resolve();
 f.run(`call={};releaseBlob({type:'image/png',size:100});`);
 await f.run('refresh');
 assert.equal(f.nodes.get('avatar').src,'blob:new-call');
 assert.equal(f.run('current.avatarObject'),undefined);
});
test('synthetic provider waits for native channel readiness and sends one handshake',async()=>{
 const html=readFileSync('tests/browser/provider.html','utf8');
 const sent=[],timers=[];let peer,releaseStats;
 const channel={id:0,readyState:'open',send:data=>sent.push(JSON.parse(data))};
 const audioNode=()=>({frequency:{},gain:{},connect(){return this;},start(){},stop(){},stream:{getTracks:()=>[]}});
 const context=vm.createContext({window:{},setTimeout:fn=>timers.push(fn),
  AudioContext:class {async resume(){} createOscillator(){return audioNode();} createGain(){return audioNode();} createMediaStreamDestination(){return audioNode();}},
  RTCPeerConnection:class {
   constructor(){peer=this;this.iceGatheringState='complete';this.localDescription={sdp:'synthetic'};}
   async setRemoteDescription(){this.ondatachannel({channel});}
   async createAnswer(){return {};}
   async setLocalDescription(){}
   getStats(){return new Promise(resolve=>{releaseStats=state=>resolve(new Map([['channel',{type:'data-channel',dataChannelIdentifier:0,state}]]));});}
  },fetch:async url=>url==='/offer'?{status:200,json:async()=>({sdp:'synthetic'})}:new Promise(()=>{})});
 vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
 for(let n=0;n<12;n++)await Promise.resolve();
 assert.equal(sent.length,0,'Blink open alone must not send before native SCTP readiness');
 channel.onopen();
 releaseStats('connecting');
 for(let n=0;n<12;n++)await Promise.resolve();
 assert.equal(sent.length,0,'native connecting must not send a handshake');
 timers.shift()();
 for(let n=0;n<12;n++)await Promise.resolve();
 releaseStats('open');
 for(let n=0;n<12;n++)await Promise.resolve();
 channel.onopen();
 assert.equal(sent.length,1,'duplicate open notifications must not send twice');
 assert.equal(sent[0].type,'session.started');
 assert.equal(context.window.peers[0].peer,peer);
});
let failed=0;
for(const {name,run} of tests){try{await run();console.log('PASS',name);}catch(e){failed++;console.error('FAIL',name,e.message);}}
console.log(`${tests.length-failed}/${tests.length} Talk frontend checks passed`);
process.exitCode=failed?1:0;
