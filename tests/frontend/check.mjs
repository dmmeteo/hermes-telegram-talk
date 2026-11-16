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
function fixture(options = {}) {
  const nodes = new Map();
  let now = 0, nextTimer = 1;
  const timers = new Map();
  const clock = options.clock ? {setTimeout:(fn,ms=0)=>{timers.set(nextTimer,{fn,at:now+ms});return nextTimer++;},
    clearTimeout:id=>timers.delete(id), performance:{now:()=>now}} : {setTimeout, clearTimeout, performance};
  const wait = ms => {
    const until = now + ms;
    for (;;) {
      const due = [...timers].filter(([,t])=>t.at<=until).sort((a,b)=>a[1].at-b[1].at||a[0]-b[0])[0];
      if (!due) { now = until; return; }
      timers.delete(due[0]); now = due[1].at; due[1].fn();
    }
  };
  function element(id) {
    if (!nodes.has(id)) nodes.set(id,{hidden:false,textContent:'',disabled:false,dataset:{},style:{setProperty(){}},
      setAttribute(name,value){this[name]=value;},getAttribute(name){return this[name];},removeAttribute(name){delete this[name];},querySelector(){return this;},addEventListener(){},removeEventListener(){},
      getContext(){return new Proxy({}, {get:()=>()=>{}});},pause(){},srcObject:null});
    return nodes.get(id);
  }
  const context = vm.createContext({document:{getElementById:element,documentElement:element('html')},
    window:{addEventListener(){},removeEventListener(){}}, location:{hash:'',href:`http://localhost${options.base||'/tg-voice/'}`,origin:'http://localhost'},
    URL, URLSearchParams, AbortController, DOMException, TextEncoder, crypto:globalThis.crypto, localStorage:options.storage,
    matchMedia:()=>({matches:false}), getComputedStyle:()=>({color:'#ffffff'}),
    ...clock, cancelAnimationFrame(){}, requestAnimationFrame:()=>1,
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
      playing:true, outputLevel:0, inputLevel:0,
      meters:{you:{level:0,peak:0,hold:0},agent:{level:0,peak:0,hold:0},due:0}};
    const track = {enabled:false,stop(){this.stopped=true;}};
    call = current; conversation = owner;
    initData = ()=>'synthetic';
    let flushCalls = 0;
    const realFlush = flush;
    flush = async()=>{flushCalls++;};
    function emit(event) { onMessage(current,{data:JSON.stringify(event)}); }
  `,context);
  return {context,nodes,wait,run:code=>vm.runInContext(code,context)};
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
 const f=fixture({clock:true});f.run(`owner.greeting=true; advance(current);`);assert.equal(f.run('sent.length'),0);
 f.run(`current.handshake=true;current.playing=false;advance(current);`);assert.equal(f.run('sent.length'),0);
 f.run(`current.playing=true;advance(current);advance(current);`);
 assert.equal(f.run('sent.length'),0,'greeting_enabled: the server instructions open the call, so nothing is sent at usable');
 assert.equal(f.run('track.enabled'),false); assert.equal(f.run('current.phase'),'greeting');
 f.wait(4000);
 assert.equal(f.run('sent.length'),1,'a silent model is asked for the opening once at +4 s');assert.equal(f.run('sent[0].type'),'session.context.append');
 assert.equal(f.run('sent[0].channel'),'speakable');
 f.wait(11000);assert.equal(f.run('sent.length'),1,'never asked twice');
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
function serverFixture(answer) {
 const f=fixture();
 f.run(`Object.assign(current,{phase:'ready',jobs:new Map(),spoken:new Set(),cut:new Set(),polls:0,lastUserText:'Synthetic spoken request'});
  const posted=[];
  fetch=async(url,options)=>{const body=JSON.parse(options.body);posted.push({route:url.slice(4),body});
   const [status,data]=(${answer})(url.slice(4),body);return {ok:status<300,status,json:async()=>data};};`);
 return f;
}
const drain=async()=>{for(let n=0;n<20;n++)await new Promise(r=>setImmediate(r));};
test('delegation goes to the server once and its real refusal is relayed',async()=>{
 const f=serverFixture(`()=>[503,{error:'Tasks are not available for this account.'}]`);
 f.run(`for(let i=0;i<2;i++)emit({type:'delegation.created',item:{type:'delegation',id:'job1',target:'client'}});`);
 await drain();
 assert.equal(f.run('JSON.stringify(posted.map(p=>[p.route,p.body.delegation_item_id,p.body.request]))'),
  JSON.stringify([['task','job1','Synthetic spoken request']]));
 assert.equal(f.run('posted[0].body.initData'),'synthetic');
 assert.equal(f.run('sent.length'),1);
 assert.equal(f.run('JSON.stringify([sent[0].type,sent[0].delegation_item_id,sent[0].channel])'),
  JSON.stringify(['delegation.context.append','job1','speakable']));
 assert.equal(f.run('sent[0].content[0].text'),
  'Hermes could not take this task. Tasks are not available for this account. Nothing was executed.');
});
test('an admitted delegation relays the server reply on its channel and tracks the job',async()=>{
 const reply='Hermes is working on this as background task 1. Its result will follow on this delegation; do not guess it.';
 const f=serverFixture(`()=>[200,{job:{id:'j1',delegation_item_id:'job1'},created:true,channel:'commentary',reply:${JSON.stringify(reply)}}]`);
 f.run(`emit({type:'delegation.created',item:{type:'delegation',id:'job1',target:'client',content:[{type:'input_text',text:'Synthetic task'}]}});`);
 await drain();
 f.run('clearTimeout(current.pollTimer)');
 assert.equal(f.run('JSON.stringify(posted.map(p=>[p.route,p.body.request]))'),JSON.stringify([['task','Synthetic task']]));
 assert.equal(f.run('JSON.stringify(sent.map(m=>[m.delegation_item_id,m.channel,m.content[0].text]))'),
  JSON.stringify([['job1','commentary',reply]]));
 assert.equal(f.run('current.jobs.get("j1")'),'job1');
});
test('job results are polled from the server and delivered on their delegation',async()=>{
 const f=serverFixture(`route=>route==='tasks'?[200,{jobs:[{id:'j1',number:1,state:'succeeded',delegation_item_id:'job1',
  voice_session_id:'voice',spoken:'Synthetic result.'}],context_pending:false}]:[200,{ok:true}]`);
 f.run(`current.delegations.add('job1');current.jobs.set('j1','job1');void poll(current);`);
 await drain();
 f.run('clearTimeout(current.pollTimer)');
 assert.equal(f.run('posted[0].route'),'tasks');
 assert.equal(f.run('JSON.stringify(sent.map(m=>[m.delegation_item_id,m.channel,m.content[0].text]))'),
  JSON.stringify([['job1','speakable','Synthetic result.']]));
 assert(f.run('posted.some(p=>p.route==="task/ack"&&p.body.job_id==="j1")'),'a delivered result is acknowledged');
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
const SAVE_FAILED='Conversation could not be saved. Tap Retry saving.';
const shownNotice=f=>{const n=id=>f.run(`document.getElementById('${id}')`);return n('notice').hidden?null:{kind:n('notice').dataset.kind,title:n('notice-title').textContent,
 message:n('error').hidden?'':n('error').textContent,action:n('start').hidden?null:n('start').textContent};};
test('the notice follows the call phase, the save state and the error text, and its button runs the real action',async()=>{
 const f=fixture();
 f.run(`current.phase='ready';showError();`);
 assert.equal(shownNotice(f),null,'a working call shows no notice');
 f.run(`emit({type:'turn.done',turn:{role:'user',transcript:'Synthetic unsaved line'}});owner.retries=3;post=async()=>{throw new Error('network');};`);
 await f.run('realFlush(owner)');
 assert.deepEqual(shownNotice(f),{kind:'error',title:'Error',message:SAVE_FAILED,action:'Retry saving'},'a failed save is an error');
 f.run(`flush=realFlush;globalThis.saved=[];post=async(route,body)=>{if(route!=='events')return {ok:true};
  saved.push(...body.events.map(e=>e.text));return {conversation_id:owner.id,accepted_ids:body.events.map(e=>e.id)};};`);
 f.run('start()');await drain();
 assert.deepEqual(JSON.parse(f.run('JSON.stringify(saved)')),['Synthetic unsaved line'],'Retry saving sends the unsaved line again');
 assert.equal(f.run('owner.pending.length'),0);assert.equal(f.run('call===current'),true,'saving again keeps the call');
 assert.equal(shownNotice(f),null,'the notice clears once the conversation is saved');
 f.run(`current.phase='blocked';render();`);
 assert.deepEqual(shownNotice(f),{kind:'prompt',title:'',message:'',action:'Enable audio'},'blocked audio is a prompt');
 f.run(`current.phase='ready';render();`);assert.equal(shownNotice(f),null,'the prompt clears with audio');
 f.run(`showError('That voice is not available.')`);
 assert.deepEqual(shownNotice(f),{kind:'error',title:'Error',message:'That voice is not available.',action:null},'an error during a call adds no button');
 f.run('showError()');assert.equal(shownNotice(f),null);
 f.run(`stop('Connection lost. Tap Retry to reconnect.')`);
 assert.deepEqual(shownNotice(f),{kind:'error',title:'Error',message:'Connection lost. Tap Retry to reconnect.',action:'Retry'},'a stopped call with an error offers Retry');
 f.run('stop()');
 assert.deepEqual(shownNotice(f),{kind:'prompt',title:'',message:'',action:'Connect'},'a stopped call without an error offers Connect');
});
test('a save that failed while audio was blocked stays an error after Enable audio',async()=>{
 const f=fixture();
 f.run(`emit({type:'turn.done',turn:{role:'user',transcript:'Synthetic blocked line'}});owner.retries=3;post=async()=>{throw new Error('network');};
  current.audio={state:'suspended',resume:async()=>{},close:async()=>{}};current.phase='blocked';showError();`);
 await f.run('realFlush(owner)');
 assert.equal(shownNotice(f).action,'Enable audio','audio comes first, as start() does');
 f.run(`current.audio.state='running';`);await f.run('enableAudio(current)');
 assert.deepEqual(shownNotice(f),{kind:'error',title:'Error',message:SAVE_FAILED,action:'Retry saving'},
  'enabling audio clears the error text but not the failed save');
 assert.equal(f.run('owner.pending.length'),1,'the unsaved line is kept');
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
test('a second voice pick made while the first one saves is saved too',async()=>{
 const f=fixture();
 f.run(`voiceState={current:'cove',default:'cove',choices:[{id:'cove',label:'Cove'},{id:'marin',label:'Marin'},{id:'sol',label:'Sol'}],applies:'next_connection'};current.voice='cove';renderVoice(current);
 globalThis.posted=[];globalThis.release=[];post=(route,body)=>{posted.push(body.voice);return new Promise(done=>release.push(()=>done({voice:{...voiceState,current:body.voice}})));};
 document.getElementById('voice').value='marin';globalThis.first=chooseVoice();document.getElementById('voice').value='sol';`);
 await f.run('chooseVoice()');
 f.run('release.shift()()');await f.run('first');
 assert.equal(f.run('JSON.stringify(posted)'),JSON.stringify(['marin','sol']),'the later pick is posted after the first save');
 f.run('release.shift()()');await f.run('new Promise(r=>setTimeout(r,0))');
 assert.equal(f.run('voiceState.current'),'sol');
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
test('avatar at a custom deployment base becomes visible once decoded, and other paths are refused',()=>{
 const f=fixture({base:'/tg-voice-michael/'});
 const profile=url=>f.run(`applyProfile({profile:{display_name:'Synthetic',avatar_url:'${url}'},appearance:{listen_color:'#4fc3f7',speak_color:'#ffb74d'}})`);
 profile('/tg-voice-michael/avatar');
 const avatar=f.nodes.get('avatar');
 assert.equal(avatar.src,'http://localhost/tg-voice-michael/avatar','the fallback at this base loads');
 assert.equal(avatar.hidden,true,'hidden until decoded');
 avatar.onload();
 assert.equal(avatar.hidden,false,'a decoded avatar at a custom base is shown');
 avatar.src='blob:synthetic';avatar.hidden=true;avatar.onload();
 assert.equal(avatar.hidden,false,'the authenticated blob is shown at a custom base');
 avatar.onerror();
 assert.equal(avatar.hidden,true,'a decode failure falls back to the neutral face');
 for(const url of ['/tg-voice/avatar','/tg-voice-michael/avatar?x=1','https://example.invalid/tg-voice-michael/avatar']){
  profile(url);
  assert.equal(avatar.src,undefined,`${url} is not loaded`);
  assert.equal(avatar.hidden,true,`${url} leaves the neutral face`);
 }
});
test('blocked output shows one short status line and Enable audio, never a repeated error',async()=>{
 const f=fixture();f.run(`current.audio={state:'suspended',resume:async()=>{},close:async()=>{}};`);
 await f.run('enableAudio(current)');
 assert.equal(f.run('current.phase'),'blocked');
 assert.equal(f.nodes.get('call-status').textContent,'Audio and microphone paused.');
 assert.equal(f.nodes.get('error').hidden,true,'no error line repeats the blocked state');
 assert.equal(f.nodes.get('start').hidden,false);assert.equal(f.nodes.get('start').textContent,'Enable audio');
});
test('an unbound or refused chat link shows no note',async()=>{
 for(const status of ['unbound','expired','used','invalid','foreign','not_private','unavailable']){
  const f=fixture();
  f.run(`post=async()=>({voice:{current:'cove',default:'cove',choices:[{id:'cove',label:'Cove'},{id:'sol',label:'Sol'}],applies:'next_connection'},thread:{status:'${status}'}});`);
  await f.run('loadVoice(current)');
  const shown=[...f.nodes.values()].filter(node=>!node.hidden&&/\/talk|\blink|chat input/i.test(node.textContent));
  assert.deepEqual(shown.map(node=>node.textContent),[],`${status} shows nothing`);
 }
});
test('status text follows a raw state only after it holds, while the ring follows every frame',()=>{
 const f=fixture({clock:true});
 const text=()=>f.nodes.get('call-status').textContent;
 const seen=new Set(), states=new Set();
 const frame=(input,output)=>{f.run(`current.inputLevel=${input};current.outputLevel=${output};render();`);
  seen.add(text());states.add(f.nodes.get('orb').dataset.state);f.wait(20);};
 f.run(`current.phase='ready';current.handshake=true;updateMic(current);`);
 assert.equal(text(),'Listening.','a ready call shows Listening at once');
 for(let burst=0;burst<6;burst++){
  for(let t=0;t<700;t+=20)frame(0,.2);
  for(let t=0;t<100;t+=20)frame(0,0);
  for(let t=0;t<300;t+=20)frame(.1,0);
 }
 assert.deepEqual([...seen],['Listening.'],'speech shorter than the hold, with short pauses, never changes the text');
 assert.deepEqual([...states].sort(),['listening','silent','speaking'],'the ring state follows every frame');
 for(let t=0;t<980;t+=20)frame(0,.2);
 assert.equal(text(),'Listening.','no change before the hold');
 for(let t=0;t<60;t+=20)frame(0,.2);
 assert.equal(text(),'Speaking.','speech held for about 1 s changes the text');
 assert.equal(f.nodes.get('orb')['aria-label'],'Speaking.','the orb label follows the text');
 for(let t=0;t<120;t+=20)frame(0,0);
 assert.equal(text(),'Speaking.','a short silence changes nothing');
 frame(0,0);f.run(`current.muted=true;updateMic(current);`);
 assert.equal(text(),'Microphone muted.','mute shows at once');
 frame(0,.2);frame(0,.2);f.wait(2000);frame(0,.2);
 assert.equal(f.nodes.get('orb').dataset.state,'speaking');assert.equal(text(),'Microphone muted.','assistant output keeps the muted text');
 f.run(`current.muted=false;updateMic(current);current.outputLevel=0;current.phase='blocked';render();`);
 assert.equal(text(),'Audio and microphone paused.','blocked shows at once');
 f.run(`current.phase='connecting';render();`);assert.equal(text(),'Connecting. Microphone paused.','connecting shows at once');
 f.run(`call=null;render();`);assert.equal(text(),'Call stopped.','idle shows at once');
});
function meterFixture(options) {
 const f=fixture(options);
 f.run(`current.phase='ready';current.handshake=true;updateMic(current);globalThis.levels={input:0,output:0};
  Object.assign(current,{inputAnalyser:{getFloatTimeDomainData:a=>a.fill(levels.input)},inputSamples:new Float32Array(8),
   outputAnalyser:{getFloatTimeDomainData:a=>a.fill(levels.output)},outputSamples:new Float32Array(8)});`);
 const shown=source=>{const {level,peak}=f.nodes.get(`meter-${source}`).dataset;return [Number(level),Number(peak)];};
 const frame=(input,output)=>{f.run(`levels.input=${input};levels.output=${output};sample(current);`);return {you:shown('you'),agent:shown('agent')};};
 return {f,frame,shown};
}
test('each level meter follows its own side: instant rise, a 0.07 fall per frame and an 8-frame peak hold',()=>{
 const {frame}=meterFixture();
 assert.deepEqual(frame(0,0),{you:[0,0],agent:[0,0]},'silence reads empty');
 assert.deepEqual(frame(.108,0),{you:[1,1],agent:[0,0]},'the microphone alone lights only the human meter, at once');
 assert.deepEqual(frame(.108,.058).agent,[.66,.66],'assistant output rises at once on its own meter, by the Full Duplex curve');
 assert.deepEqual(frame(.108,.058).you,[1,1],'both sides speaking at once keep their own levels');
 const fall=[];for(let n=0;n<26;n++)fall.push(frame(0,.058));
 assert.deepEqual(fall.map(f=>f.you[0]),[.93,.86,.79,.72,.65,.58,.51,.44,.37,.3,.23,.16,.09,.02,0,0,0,0,0,0,0,0,0,0,0,0],'the level falls 0.07 a frame');
 assert.deepEqual(fall.map(f=>f.you[1]),[1,1,1,1,1,1,1,1,.94,.88,.82,.76,.7,.64,.58,.52,.46,.4,.34,.28,.22,.16,.1,.04,0,0],'the peak holds 8 frames, then falls 0.06 a frame');
 assert(fall.every(f=>f.agent[0]===.66),'the assistant meter is unaffected by the microphone falling silent');
 assert.deepEqual(frame(.004,0),{you:[0,0],agent:[.59,.66]},'a level below the noise gate reads empty');
});
test('the human meter empties on mute, both on disconnect and playback end, and stop clears them at once',()=>{
 const {f,frame,shown}=meterFixture();
 frame(.108,.058);
 f.run('toggleMute()');
 let last;for(let n=0;n<25;n++)last=frame(.108,.058);
 assert.deepEqual(last,{you:[0,0],agent:[.66,.66]},'mute empties the human meter while the assistant meter keeps following');
 f.run('toggleMute()');
 assert.deepEqual(frame(.108,.058).you,[1,1],'unmute shows the microphone at once');
 f.run(`current.peer.connectionState='disconnected';current.phase='connecting';updateMic(current);`);
 for(let n=0;n<25;n++)last=frame(.108,.058);
 assert.deepEqual(last,{you:[0,0],agent:[0,0]},'a disconnect empties both meters');
 f.run(`current.peer.connectionState='connected';current.phase='ready';updateMic(current);`);
 assert.deepEqual(frame(.108,.058),{you:[1,1],agent:[.66,.66]},'reconnected media lights both again');
 f.run(`remoteAudio.srcObject={};onAudioPause();`);
 for(let n=0;n<25;n++)last=frame(.108,.058);
 assert.deepEqual(last.agent,[0,0],'playback ending empties the assistant meter');
 f.run(`current.phase='ready';current.playing=true;updateMic(current);`);
 frame(.108,.058);
 f.run('stop()');
 assert.deepEqual([shown('you'),shown('agent')],[[0,0],[0,0]],'stop empties both meters without waiting for a frame');
});
test('with reduced motion a meter shows the plain level a few times a second, with no fall or peak',()=>{
 const {f,frame}=meterFixture({clock:true});
 f.run('reducedMotion.matches=true');
 assert.deepEqual(frame(.108,.058),{you:[1,1],agent:[.66,.66]});
 f.wait(100);
 assert.deepEqual(frame(0,0),{you:[1,1],agent:[.66,.66]},'the meter holds still between updates');
 f.wait(200);
 assert.deepEqual(frame(0,0),{you:[0,0],agent:[0,0]},'the next update shows silence at once, with no peak left');
});
test('a stored theme applies at load, and an unknown or unreadable value falls back to Modern',()=>{
 const saved=[];
 const store=value=>({getItem:()=>value,setItem:(key,next)=>saved.push([key,next])});
 const f=fixture({storage:store('typesafe')});
 assert.equal(f.nodes.get('html').dataset.theme,'typesafe');
 assert.equal(f.nodes.get('theme-color').content,'#b9dff8');
 assert.equal(f.nodes.get('theme-typesafe').checked,true);assert.equal(f.nodes.get('theme-modern').checked,false);
 f.run(`chooseTheme({target:{value:'modern'}})`);
 assert.equal(f.nodes.get('html').dataset.theme,'modern');assert.equal(f.nodes.get('theme-color').content,'#30302e');
 assert.deepEqual(saved,[['talk-theme','modern']]);
 for(const value of [null,'constructor','__proto__','dark']) assert.equal(fixture({storage:store(value)}).nodes.get('html').dataset.theme,'modern',String(value));
 assert.equal(fixture({storage:{getItem(){throw new Error('blocked');}}}).nodes.get('html').dataset.theme,'modern','unreadable storage');
 assert.equal(fixture().nodes.get('html').dataset.theme,'modern','no storage');
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
