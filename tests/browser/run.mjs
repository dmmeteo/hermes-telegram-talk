import {spawn} from 'node:child_process';
import {writeFileSync,appendFileSync,readFileSync,createWriteStream,existsSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {chromium} from 'playwright';
const OUT=process.argv[2] || '/output/talk';
const APP='http://127.0.0.1:8801/tg-voice/';
const FIX='http://127.0.0.1:8802';
const LOG=`${OUT}/browser.log`;
writeFileSync(LOG,'');
const log=(step,detail={})=>appendFileSync(LOG,JSON.stringify({time:new Date().toISOString(),step,...detail})+'\n');
const assert=(value,message)=>{if(!value)throw new Error(message);};
const until=async(fn,message,ms=15000)=>{
 const deadline=Date.now()+ms;
 do{const result=await fn();if(result)return result;await new Promise(r=>setTimeout(r,80));}while(Date.now()<deadline);
 throw new Error(`timeout: ${message}`);
};
let failures=0,passes=0;
async function step(name,fn){log(name,{started:true});try{const detail=await fn();passes++;log(name,{result:'pass',...detail});}
 catch(e){failures++;log(name,{result:'fail',error:String(e.stack||e).slice(0,6000),resources:Object.fromEntries(['memory.current','memory.events','pids.current','pids.events'].map(name=>[name,readFileSync('/sys/fs/cgroup/'+name,'utf8')]))});}}
const fixture=spawn('python3',['/src/tests/browser/fixture.py'],{stdio:['ignore','pipe','pipe']});
fixture.stderr.pipe(createWriteStream(`${OUT}/fixture.stderr`));
await new Promise((resolve,reject)=>{
 const timer=setTimeout(()=>reject(new Error('Talk fixture did not start')),10000);
 fixture.stdout.on('data',d=>{if(String(d).includes('TALK FIXTURE READY')){clearTimeout(timer);resolve();}});
 fixture.on('exit',c=>reject(new Error(`fixture exit ${c}`)));
});
const args=['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--allow-loopback-in-peer-connection',
 '--disable-features=WebRtcHideLocalIpsWithMdns','--autoplay-policy=no-user-gesture-required'];
const browser=await chromium.launch({args});
let visibilityBrowser,visibilityChrome;
let nativePolicyBrowser;
log('runtime',{browser:browser.version(),display:'headless Chromium; native CDP context for real tab visibility',fixture:'real frozen backend + local Chromium WebRTC peers; all identities/context/media synthetic',network:'none'});
const provider=await browser.newPage();
await provider.goto(`${FIX}/provider.html`);
const {initData,launch}=await(await fetch(`${FIX}/identity`)).json();
const appUrl=APP+'?launch='+launch+'#tgWebAppData='+encodeURIComponent(initData);
const errors=[];
const contexts=[];
function instrument() {
 window.__pcs=[];window.__streams=[];window.__audios=[];window.__gum=0;window.__analysers=[];window.__raf=0;
 const nativePeer=RTCPeerConnection;
 window.RTCPeerConnection=function(...args){const pc=new nativePeer(...args);window.__pcs.push(pc);return pc;};
 window.RTCPeerConnection.prototype=nativePeer.prototype;
 const nativeAudio=AudioContext;
 window.AudioContext=function(...args){const ctx=new nativeAudio(...args);window.__audios.push(ctx);
  const create=ctx.createAnalyser.bind(ctx);
  ctx.createAnalyser=()=>{const node=create();const read=node.getFloatTimeDomainData.bind(node);
   const record={reads:0,max:0,last:0};window.__analysers.push(record);
   node.getFloatTimeDomainData=array=>{read(array);record.reads++;record.last=Math.sqrt(array.reduce((s,x)=>s+x*x,0)/array.length);record.max=Math.max(record.max,record.last);};
   return node;};return ctx;};
 window.AudioContext.prototype=nativeAudio.prototype;
 const gum=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
 navigator.mediaDevices.getUserMedia=async c=>{window.__gum++;const stream=await gum(c);window.__streams.push(stream);return stream;};
 const raf=requestAnimationFrame;window.requestAnimationFrame=fn=>raf(t=>{window.__raf++;fn(t);});
}
async function open(options={}){
 const ctx=options.context || await (options.browser || browser).newContext({viewport:options.viewport||{width:390,height:844},hasTouch:true,permissions:['microphone'],reducedMotion:options.reducedMotion||'no-preference'});
 if(!options.context)contexts.push(ctx);await ctx.addInitScript(instrument);
 if(options.init)await ctx.addInitScript(options.init);
 const page=await ctx.newPage();if(options.context)await page.setViewportSize({width:390,height:844});page.on('pageerror',e=>errors.push(String(e)));
 await page.route('https://telegram.org/**',r=>r.fulfill({contentType:'text/javascript',body:`window.Telegram={WebApp:{initData:new URLSearchParams(location.hash.slice(1)).get('tgWebAppData')||'',ready(){},expand(){}}};`}));
 await page.route('**/*',r=>{
  const url=r.request().url();if(url.startsWith('http://127.0.0.1:')||url.startsWith('https://telegram.org/'))return r.fallback();
  errors.push('Unexpected external request');return r.abort();
 });
 await page.goto(options.noAuth?APP:appUrl);
 return page;
}
const state=page=>page.evaluate(()=>({phase:document.getElementById('orb').dataset.state,
 label:document.getElementById('orb').getAttribute('aria-label'),error:document.getElementById('error').textContent,
 fallback:!document.getElementById('start').hidden,muteDisabled:document.getElementById('mute').disabled,
 gum:window.__gum,tracks:window.__streams.flatMap(s=>s.getTracks()).map(t=>({enabled:t.enabled,state:t.readyState})),
 peers:window.__pcs.map(p=>p.connectionState),audio:window.__audios.map(a=>a.state),
 playing:!document.getElementById('remote').paused,analysers:window.__analysers,raf:window.__raf}));
const ready=async page=>{try{return await until(async()=>{const s=await state(page);return ['listening','speaking','silent'].includes(s.phase)&&s.tracks.some(t=>t.enabled)&&s;},'call ready');}catch(error){error.message+=' '+JSON.stringify(await state(page));throw error;}};
const lastPeer=()=>provider.evaluate(()=>window.peers.length-1);
const send=(index,event)=>provider.evaluate(({index,event})=>window.peers[index].send(event),{index,event});
const gain=(index,value)=>provider.evaluate(({index,value})=>{window.peers[index].gain.gain.value=value;},{index,value});
const messages=async()=>Object.values((await(await fetch(`${FIX}/state`)).json()).messages).flat();
async function screenshot(page,name){await page.screenshot({path:`${OUT}/${name}.png`});const bytes=readFileSync(`${OUT}/${name}.png`);
 assert(bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])),'PNG signature');
 appendFileSync(`${OUT}/screenshots.jsonl`,JSON.stringify({name,viewport:page.viewportSize(),sha256:createHash('sha256').update(bytes).digest('hex')})+'\n');}
async function closed(page){return until(async()=>{const s=await state(page);return s.tracks.every(t=>t.state==='ended')&&s.peers.every(p=>p==='closed')&&s.audio.every(a=>a==='closed')&&s;},'all media and contexts closed');}
let page,index;
try {
 await step('automatic-entry-authenticated-bootstrap-real-webrtc',async()=>{
  page=await open();const s=await ready(page);index=await lastPeer();
  const bytes=await page.evaluate(async()=>{let n=0;for(const p of window.__pcs)(await p.getStats()).forEach(r=>{if(r.type==='inbound-rtp'&&r.kind==='audio')n+=r.bytesReceived;});return n;});
  assert(s.gum===1&&s.peers.length===1&&s.playing&&bytes>0,'one actual peer, mic, output and incoming RTP');
  return {inboundBytes:bytes};
 });
 await step('portrait-dark-bottom-controls-and-menu',async()=>{
  assert(await page.title()==='Hermes Telegram Talk','title');
  const d=await page.evaluate(()=>({buttons:[...document.querySelectorAll('button')].filter(b=>b.getClientRects().length).map(b=>b.textContent.trim()),
   overflow:document.documentElement.scrollWidth>innerWidth,face:document.getElementById('avatar').complete,
   name:document.getElementById('name').textContent,bg:getComputedStyle(document.body).backgroundColor}));
  assert(await page.evaluate(()=>getComputedStyle(document.getElementById('face')).display==='none' && !document.getElementById('avatar').hidden),'configured profile avatar painted');
  assert(JSON.stringify(d.buttons)==='["•••","Mute"]'&&!d.overflow&&d.face&&d.name==='Synthetic Talk'&&d.bg==='rgb(16, 20, 22)','minimal dark avatar UI');
  await screenshot(page,'mobile-390-listening');return d;
 });
 await step('real-incoming-energy-drives-speaking-ring',async()=>{
  const before=await page.locator('#ring').screenshot();await gain(index,.18);
  await until(async()=>{const s=await state(page);return s.phase==='speaking'&&s.analysers.at(-1)?.last>.02;},'measured incoming energy');
  const after=await page.locator('#ring').screenshot();assert(!before.equals(after),'rendered ring changed with measured energy');
  await screenshot(page,'mobile-390-speaking');await gain(index,0);
  await until(async()=>['listening','silent'].includes((await state(page)).phase),'silence returns to listening');
 });
 await step('real-outgoing-microphone-energy-and-configured-colors',async()=>{
  await until(async()=>(await state(page)).analysers[0]?.max>.005,'actual fake microphone energy measured');
  const colors=await page.evaluate(()=>({listen:getComputedStyle(document.documentElement).getPropertyValue('--listen').trim(),speak:getComputedStyle(document.documentElement).getPropertyValue('--speak').trim()}));
  assert(colors.listen==='#4FC3F7'&&colors.speak==='#FFB74D','configured distinct colors');return colors;
 });
 await step('touch-mute-and-keyboard-unmute',async()=>{
  await page.tap('#mute');assert((await state(page)).tracks.every(t=>!t.enabled),'touch mutes actual track');
  assert(await page.locator('#mute').getAttribute('aria-pressed')==='true','pressed state');
  assert((await state(page)).phase==='muted','mute geometry state');await screenshot(page,'mobile-390-muted');
  await page.locator('#mute').focus();assert(await page.locator('#mute').evaluate(e=>getComputedStyle(e).outlineWidth)==='3px','keyboard focus visible');
  await page.keyboard.press('Enter');assert((await state(page)).tracks.every(t=>t.enabled),'keyboard unmutes actual track');
 });
 await step('desktop-landscape-and-200-percent-text-layout',async()=>{
  for(const [width,height,name] of [[1280,900,'desktop-listening'],[844,390,'landscape-listening']]){
   await page.setViewportSize({width,height});const b=await page.locator('#mute').boundingBox();
   assert(b.width>=44&&b.height>=44&&b.x>=0&&b.y>=0&&b.y+b.height<=height,'visible touch control');
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'no horizontal overflow');await screenshot(page,name);
  }
  await page.evaluate(()=>{const entries=[...document.querySelectorAll('h1,h2,button,.brand-mark,#error')].map(element=>({element,size:parseFloat(getComputedStyle(element).fontSize)}));for(const {element,size} of entries)element.style.fontSize=(size*2)+'px';});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'double-size actual text no horizontal overflow');
  const zoomed=await page.locator('#mute').boundingBox();assert(zoomed.x>=0&&zoomed.x+zoomed.width<=844&&zoomed.y+zoomed.height<=390,'double-size text control remains visible');
  await screenshot(page,'landscape-double-text');
  await page.evaluate(()=>{for(const element of document.querySelectorAll('h1,h2,button,.brand-mark,#error'))element.style.fontSize='';});await page.setViewportSize({width:390,height:844});
 });
 await step('settled-subscription-text-only-ordered-idempotent',async()=>{
  const before=(await messages()).length;
  await send(index,{type:'input_transcript.added',item:{text:'partial must not save'}});
  await send(index,{type:'response.audio_transcript.done',transcript:'wrong API dialect'});
  await send(index,{type:'turn.done',turn:{id:'user-final',role:'user',transcript:'Synthetic settled question.'}});
  await send(index,{type:'turn.done',turn:{id:'assistant-final',role:'assistant',transcript:'Synthetic settled answer.'}});
  await send(index,{type:'turn.done',turn:{id:'assistant-final',role:'assistant',transcript:'Synthetic settled answer.'}});
  await until(async()=>(await messages()).length===before+2,'two stored settled turns');
  const rows=(await messages()).slice(-2);assert(rows[0].role==='user'&&rows[1].role==='assistant','arrival order');
  assert(!JSON.stringify(await messages()).includes('partial must not save'),'no fragments persisted');
 });
 await step('delegation-refused-no-task-route-or-replay',async()=>{
  await send(index,{type:'delegation.created',item:{id:'delegation1',type:'delegation',target:'client'}});
  await send(index,{type:'delegation.created',item:{id:'delegation1',type:'delegation',target:'client'}});
  const got=await until(()=>provider.evaluate(i=>window.peers[i].got.filter(e=>e.type==='delegation.context.append'),index).then(a=>a.length&&a),'refusal');
  assert(got.length===1&&got[0].delegation_item_id==='delegation1'&&/Nothing was executed/.test(got[0].content[0].text),'one protocol refusal');
 });
 await step('retry-events-with-identical-ids-after-lost-ack',async()=>{
  let first=true;const batches=[];
  await page.route('**/api/events',async route=>{
   batches.push(route.request().postDataJSON());
   if(first){first=false;const response=await route.fetch();assert(response.ok(),'real server persisted first delivery');return route.abort();}
   return route.continue();
  });
  await send(index,{type:'turn.done',turn:{id:'retry-turn',role:'user',transcript:'Duplicate safe synthetic retry.'}});
  await until(()=>batches.length>=2,'bounded retry');
  assert(JSON.stringify(batches[0])===JSON.stringify(batches[1]),'retry exact identity sequence and text');
  await until(async()=>(await messages()).filter(m=>m.content==='Duplicate safe synthetic retry.').length===1,'one stored retry');
  await page.unroute('**/api/events');
 });
 await step('exhausted-save-retries-can-recover-without-reconnecting',async()=>{
  const batches=[];
  await page.route('**/api/events',route=>{batches.push(route.request().postDataJSON());return route.abort();});
  await send(index,{type:'turn.done',turn:{id:'save-retry-control',role:'user',transcript:'Retry saving the settled synthetic turn.'}});
  await until(async()=>!await page.locator('#start').isHidden()&&await page.locator('#start').textContent()==='Retry saving','save retry exposed',20000);
  assert(batches.length===4,'initial delivery plus three bounded retries');
  assert(batches.every(b=>JSON.stringify(b)===JSON.stringify(batches[0])),'all bounded retry IDs remain stable');
  const peerCount=(await state(page)).peers.length;
  await page.unroute('**/api/events');await page.click('#start');
  await until(()=>page.locator('#start').isHidden(),'save retry acknowledged');
  assert((await state(page)).peers.length===peerCount,'saving retry does not reopen peer');
  assert((await messages()).filter(m=>m.content==='Retry saving the settled synthetic turn.').length===1,'one stored retry turn');
 });
 await step('pending-turn-survives-reconnect-only-in-verified-conversation',async()=>{
  const batches=[];
  await page.route('**/api/events',route=>{batches.push(route.request().postDataJSON());return route.abort();});
  await send(index,{type:'turn.done',turn:{id:'reconnect-pending',role:'user',transcript:'Synthetic text retained across reconnect.'}});
  await until(()=>batches.length>=1,'pending event request observed');
  await provider.evaluate(i=>window.peers[i].channel.close(),index);
  await until(async()=>(await state(page)).phase==='idle','closed call offers reconnect');
  const original=batches[0];await page.unroute('**/api/events');
  const retried=[];await page.route('**/api/events',route=>{retried.push(route.request().postDataJSON());return route.continue();});
  await page.click('#start');await ready(page);index=await lastPeer();
  await until(()=>retried.some(b=>b.events.some(e=>e.text==='Synthetic text retained across reconnect.')),'same conversation replay');
  const retry=retried.find(b=>b.events.some(e=>e.text==='Synthetic text retained across reconnect.'));
  assert(JSON.stringify(retry)===JSON.stringify(original),'reconnect retains old voice session and exact duplicate-safe event');
  assert((await messages()).filter(m=>m.content==='Synthetic text retained across reconnect.').length===1,'one stored reconnect turn');
  await page.unroute('**/api/events');
 });
 await step('real-background-tab-does-not-end-call',async()=>{
  visibilityChrome=spawn(chromium.executablePath(),['--headless=new','--no-sandbox','--disable-dev-shm-usage',
   '--remote-debugging-port=9222','--user-data-dir=/tmp/visibility-profile',...args,'about:blank'],{stdio:'ignore'});
  await until(async()=>{try{return(await fetch('http://127.0.0.1:9222/json/version')).ok;}catch{return false;}},'native visibility browser');
  visibilityBrowser=await chromium.connectOverCDP('http://127.0.0.1:9222',{noDefaults:true});
  const context=visibilityBrowser.contexts()[0];
  const native=await open({context});await ready(native);
  const before=await state(native);const other=await context.newPage();await other.goto('about:blank');await other.bringToFront();
  await until(()=>native.evaluate(()=>document.visibilityState==='hidden'),'native hidden tab');
  const during=await state(native);const visibility=await native.evaluate(()=>document.visibilityState);
  assert(visibility==='hidden','background tab is actually hidden');
  assert(during.peers.at(-1)==='connected'&&during.tracks.at(-1).state==='live','current hidden page media preserved');
  assert(JSON.stringify(during.tracks.map(t=>t.state))===JSON.stringify(before.tracks.map(t=>t.state)),'hiding preserves every prior live and ended track state');
  await native.bringToFront();await other.close();await ready(native);await native.close();
  await visibilityBrowser.close();visibilityBrowser=null;visibilityChrome.kill('SIGTERM');visibilityChrome=null;
  return {before:before.peers,after:during.peers,visibility,nativeContext:'CDP noDefaults avoids Playwright forced visible focus emulation'};
 });
 await step('visibility-hidden-and-persisted-exit-are-distinct',async()=>{
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  assert((await state(page)).peers.at(-1)==='connected','visibility event never ends');
  await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true})));
  await closed(page);
  await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));
  await ready(page);index=await lastPeer();
  await page.click('#mute');assert((await state(page)).tracks.filter(t=>t.state==='live').every(t=>!t.enabled),'restored document has working controls');
  await page.click('#mute');
 });
 await step('reopen-and-mute-after-true-page-exit',async()=>{
  await page.reload();await ready(page);index=await lastPeer();await page.click('#mute');
  const ended=page.waitForResponse(response=>response.url().endsWith('/api/end'));
  await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:false})));
  assert((await ended).ok(),'real backend acknowledged authenticated end');
  const s=await closed(page);assert(!s.playing,'audio detached on exit');
  const frames=s.raf;await page.waitForTimeout(250);assert((await state(page)).raf===frames,'animation stopped');
  await page.reload();await ready(page);index=await lastPeer();
 });
 await step('duplicate-start-does-not-create-second-peer',async()=>{
  const before=(await state(page)).gum;
  await page.evaluate(()=>{document.getElementById('start').click();document.getElementById('start').click();});
  assert((await state(page)).gum===before,'start idempotent');
 });
 await step('network-session-failure-cleanup-and-retry',async()=>{
  await page.route('**/api/session',r=>r.abort());await page.reload();
  await until(async()=>(await state(page)).error.includes('Network error'),'actionable network error');await closed(page);
  await screenshot(page,'mobile-network-error');await page.unroute('**/api/session');await page.click('#start');await ready(page);index=await lastPeer();
 });
 await step('permission-denial-and-recovery',async()=>{
  const denied=await open({init:()=>{const gum=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);let deny=true;
   navigator.mediaDevices.getUserMedia=c=>{if(deny){deny=false;return Promise.reject(new DOMException('Fixture denial','NotAllowedError'));}return gum(c);};}});
  await until(async()=>(await state(denied)).error.includes('Microphone access denied'),'actionable permission denial');
  assert((await state(denied)).peers.length===0,'no peer after denial');await screenshot(denied,'mobile-permission-error');
  await denied.click('#start');await ready(denied);await denied.context().close();
 });
 await step('autoplay-denial-one-tap-unlock-retains-one-peer',async()=>{
  const blocked=await open({init:()=>{window.__allowPlay=false;const play=HTMLMediaElement.prototype.play;
   HTMLMediaElement.prototype.play=function(){if(this.id==='remote'&&!window.__allowPlay)return Promise.reject(new DOMException('Fixture autoplay denial','NotAllowedError'));return play.call(this);};
   document.addEventListener('click',()=>{window.__allowPlay=true;},{capture:true});}});
  await until(async()=>(await state(blocked)).phase==='blocked','autoplay blocked fallback');
  const s=await state(blocked);assert(s.tracks.every(t=>!t.enabled)&&s.fallback&&s.muteDisabled,'blocked output cannot send mic');
  await screenshot(blocked,'mobile-enable-audio');await blocked.click('#start');await ready(blocked);
  assert((await state(blocked)).peers.length===1,'unlock same peer');await blocked.context().close();
 });
 await step('handshake-and-first-greeting-gate-actual-mic',async()=>{
  await provider.evaluate(()=>window.behavior={handshake:false,greeting:false});
  const held=await open();await until(async()=>(await state(held)).peers.at(-1)==='connected','held peer connected');const i=await lastPeer();
  assert((await state(held)).tracks.every(t=>!t.enabled),'mic disabled before handshake');
  await send(i,{type:'session.started'});await until(()=>provider.evaluate(i=>window.peers[i].got.some(e=>e.type==='session.context.append'),i),'greeting requested');
  assert((await state(held)).tracks.every(t=>!t.enabled),'mic disabled during greeting');
  await send(i,{type:'turn.done',turn:{id:'held-opening',role:'assistant',transcript:'Synthetic opening.'}});
  await held.waitForTimeout(500);assert((await state(held)).tracks.every(t=>!t.enabled),'text without actual greeting energy cannot enable microphone');
  await gain(i,.18);
  await held.waitForTimeout(500);assert((await state(held)).tracks.every(t=>!t.enabled),'settled text alone cannot bypass outgoing audio tail');
  await gain(i,0);await ready(held);await held.context().close();await provider.evaluate(()=>window.behavior={handshake:true,greeting:true});
 });
 await step('native-autoplay-policy-needs-real-user-activation',async()=>{
  nativePolicyBrowser=await chromium.launch({args:args.filter(a=>!a.startsWith('--autoplay-policy')).concat('--autoplay-policy=document-user-activation-required')});
  const native=await open({browser:nativePolicyBrowser,init:()=>{
   const gum=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
   navigator.mediaDevices.getUserMedia=async constraints=>{await new Promise(resolve=>{window.__releaseMic=resolve;});return gum(constraints);};
  }});
  await until(async()=>{const s=await state(native);return s.phase==='blocked'&&s.audio.at(-1)==='suspended';},'native activation policy fallback before media permission');
  assert((await state(native)).peers.length===0&&(await state(native)).fallback,'native blocked audio exposes activation before microphone access');
  await native.click('#start');await native.evaluate(()=>window.__releaseMic());await ready(native);await native.context().close();await nativePolicyBrowser.close();nativePolicyBrowser=null;
 });
 await step('suspended-output-pauses-mic-and-unlocks-same-call',async()=>{
  const paused=await open();await ready(paused);
  await paused.evaluate(()=>window.__audios.at(-1).suspend());
  await until(async()=>(await state(paused)).phase==='blocked','suspended audio blocked state');
  assert((await state(paused)).tracks.every(t=>!t.enabled),'suspended audio cannot transmit microphone');
  await paused.click('#start');await ready(paused);
  assert((await state(paused)).peers.length===1,'same call after output resume');await paused.context().close();
 });
 await step('greeting-suspension-exposes-unlock-before-ready',async()=>{
  await provider.evaluate(()=>window.behavior={handshake:true,greeting:false});
  const held=await open();await until(async()=>(await state(held)).peers.at(-1)==='connected','held greeting connected');const i=await lastPeer();
  await until(()=>provider.evaluate(i=>window.peers[i].got.some(e=>e.type==='session.context.append'),i),'held greeting requested');
  await held.evaluate(()=>window.__audios.at(-1).suspend());
  await until(async()=>(await state(held)).phase==='blocked','greeting suspension exposes fallback');
  assert((await state(held)).fallback&&(await state(held)).tracks.every(t=>!t.enabled),'greeting suspension remains paused');
  await held.click('#start');await gain(i,.18);
  await held.waitForTimeout(250);await send(i,{type:'turn.done',turn:{id:'resumed-greeting',role:'assistant',transcript:'Synthetic resumed greeting.'}});
  await gain(i,0);await ready(held);await held.context().close();await provider.evaluate(()=>window.behavior={handshake:true,greeting:true});
 });
 await step('reduced-motion-uses-static-circle-with-real-audio-state',async()=>{
  const reduced=await open({reducedMotion:'reduce'});await ready(reduced);const i=await lastPeer();
  await gain(i,.18);await until(async()=>(await state(reduced)).phase==='speaking','reduced motion speaking state');
  assert(await reduced.evaluate(()=>matchMedia('(prefers-reduced-motion: reduce)').matches),'preference respected');
  await screenshot(reduced,'mobile-reduced-motion');await reduced.context().close();
 });
 await step('missing-telegram-auth-never-opens-microphone',async()=>{
  const missing=await open({noAuth:true});const s=await state(missing);
  assert(s.gum===0&&s.peers.length===0&&s.error.includes('Telegram'),'authenticated entry required');await missing.context().close();
 });
 await step('stale-getusermedia-completion-stops-returned-track',async()=>{
  const delayed=await open({init:()=>{const gum=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
   navigator.mediaDevices.getUserMedia=async c=>{const stream=await gum(c);await new Promise(r=>{window.__releaseMic=r;});return stream;};}});
  await until(()=>delayed.evaluate(()=>Boolean(window.__releaseMic)),'microphone pending');
  await delayed.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pagehide')));
  await delayed.evaluate(()=>window.__releaseMic());await closed(delayed);await delayed.context().close();
 });
 await step('auth-errors-never-show-raw-upstream-or-leak-context',async()=>{
  await page.route('**/api/bootstrap',r=>r.fulfill({status:401,contentType:'application/json',body:JSON.stringify({error:'SYNTHETIC-PRIVATE-UPSTREAM'})}));
  await page.reload();await until(async()=>(await state(page)).error.includes('sign-in expired'),'actionable expired auth');
  const text=await page.locator('body').innerText();assert(!text.includes('SYNTHETIC-PRIVATE-UPSTREAM'),'untrusted upstream not rendered');
  const backendState=await(await fetch(`${FIX}/state`)).json();
  for(const secret of ['SYNTHETIC-SOUL-MARKER','SYNTHETIC-SKILL-TALK-BRIEF','sk-synthetic','hash=','v=0'])assert(!backendState.log.includes(secret),'no secrets SDP or auth in backend log');
  assert(await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0),'no browser persistence');
  await page.unroute('**/api/bootstrap');
 });
 await step('transcript-and-reply-voice-at-phone-widths',async()=>{
  const talk=await open();await ready(talk);const i=await lastPeer();
  for(let n=0;n<14;n++)await send(i,{type:'turn.done',turn:{id:`overflow-${n}`,role:n%2?'assistant':'user',
   transcript:`Synthetic transcript line ${n}. `+'A long synthetic sentence that wraps on a phone screen. '.repeat(2)}});
  await send(i,{type:'output_transcript.added',item:{text:'Synthetic interim reply'}});
  const t=await until(()=>talk.evaluate(()=>{const l=document.getElementById('transcript');const last=l.lastElementChild;
   return last?.dataset.state==='interim'&&{hidden:l.hidden,items:l.children.length,scroll:l.scrollHeight>l.clientHeight,
   atEnd:l.scrollTop+l.clientHeight>=l.scrollHeight-30,text:last.textContent};}),'interim line rendered');
  assert(!t.hidden&&t.items>=15&&t.scroll&&t.atEnd&&t.text==='Synthetic interim reply','bounded transcript scrolls and follows the newest line');
  await send(i,{type:'turn.done',turn:{id:'interim-final',role:'assistant',transcript:'Synthetic corrected reply.'}});
  await until(()=>talk.evaluate(()=>document.getElementById('transcript').lastElementChild.textContent==='Synthetic corrected reply.'),'final text replaces the interim line');
  for(const [width,height,name] of [[390,844,'transcript-390-portrait'],[360,640,'transcript-360-portrait'],[844,390,'transcript-landscape']]){
   await talk.setViewportSize({width,height});const b=await talk.locator('#mute').boundingBox();
   if(await talk.locator('#call-menu').isHidden())await talk.click('#menu-toggle');
   const v=await talk.locator('#voice').boundingBox();
   assert(b&&b.y>=0&&b.y+b.height<=height&&v&&v.height>=44&&v.y+v.height<=height,`mute and voice selector visible ${name}`);
   assert(await talk.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`no horizontal overflow ${name}`);
   await screenshot(talk,name);
  }
  await talk.setViewportSize({width:390,height:844});
  const options=await talk.evaluate(()=>[...document.querySelectorAll('#voice option')].map(o=>o.value));
  assert(JSON.stringify([...options].sort())===JSON.stringify(['arbor','breeze','cove','ember','juniper','maple','sol','spruce','vale'].sort()),'selector lists exactly the native subscription voices');
  await talk.focus('#voice');await screenshot(talk,'voice-selector-focused');
  const conversationId=await talk.evaluate(()=>conversation.id);
  const stored=talk.waitForResponse(r=>r.url().endsWith('/api/voice')&&r.request().postData().includes('"voice":"sol"'));
  await talk.selectOption('#voice','sol');assert((await stored).ok(),'choice stored through the authenticated route');
  await until(()=>talk.evaluate(()=>!document.getElementById('reconnect').hidden&&!document.getElementById('voice-note').hidden),'explicit reconnect offered');
  assert((await state(talk)).peers.filter(p=>p==='connected').length===1,'a choice alone keeps the live call');
  for(const [width,height,name] of [[390,844,'voice-change-pending'],[360,640,'voice-change-pending-360'],[844,390,'voice-change-pending-landscape']]){
   await talk.setViewportSize({width,height});
   const m=await talk.locator('#mute').boundingBox(),r=await talk.locator('#reconnect').boundingBox();
   assert(m&&r&&m.y>=0&&m.y+m.height<=height&&r.y>=0&&r.y+r.height<=height&&r.height>=44,`mute and reconnect both visible while the change is pending ${name}`);
   await screenshot(talk,name);
  }
  await talk.setViewportSize({width:390,height:844});
  await talk.click('#reconnect');await ready(talk);
  const after=await talk.evaluate(()=>({conversation:conversation.id,reconnect:document.getElementById('reconnect').hidden,
   kept:[...document.getElementById('transcript').children].some(li=>li.textContent==='Synthetic corrected reply.')}));
  assert(after.conversation===conversationId&&after.reconnect&&after.kept,'reconnect continues the same conversation and transcript');
  await screenshot(talk,'voice-after-reconnect');
  await talk.context().close();
 });
 await step('no-uncaught-browser-errors',async()=>{assert(errors.length===0,JSON.stringify(errors));});
} finally {
 for(const context of contexts)await context.close();
 await nativePolicyBrowser?.close();await visibilityBrowser?.close();visibilityChrome?.kill('SIGTERM');await browser.close();fixture.kill('SIGTERM');
}
log('summary',{passes,failures,mandatory:28});
assert(passes+failures===28,'complete mandatory inventory ran');
process.exitCode=failures?1:0;
