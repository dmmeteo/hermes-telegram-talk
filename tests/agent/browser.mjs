import {spawn} from 'node:child_process';
import {writeFileSync,appendFileSync,createWriteStream} from 'node:fs';
import {chromium} from 'playwright';
const OUT=process.argv[2] || '/output';
const PY=process.env.HERMES_TALK_HERMES_PYTHON;
const APP='http://127.0.0.1:8811/tg-voice/';
const FIX='http://127.0.0.1:8812';
const LOG=`${OUT}/browser.log`;
writeFileSync(LOG,'');
const log=(step,detail={})=>appendFileSync(LOG,JSON.stringify({time:new Date().toISOString(),step,...detail})+'\n');
const assert=(value,message)=>{if(!value)throw new Error(message);};
const until=async(fn,message,ms=60000)=>{
 const deadline=Date.now()+ms;
 do{const result=await fn();if(result)return result;await new Promise(r=>setTimeout(r,150));}while(Date.now()<deadline);
 throw new Error(`timeout: ${message}`);
};
let failures=0,passes=0;
async function step(name,fn){log(name,{started:true});try{const detail=await fn();passes++;log(name,{result:'pass',...detail});}
 catch(e){failures++;log(name,{result:'fail',error:String(e.stack||e).slice(0,4000)});}}
const fixture=spawn(PY,['/src/tests/agent/browser_fixture.py'],{stdio:['ignore','pipe','pipe'],env:{...process.env,HOME:'/tmp/home',PYTHONDONTWRITEBYTECODE:'1'}});
fixture.stderr.pipe(createWriteStream(`${OUT}/fixture.stderr`));
await new Promise((resolve,reject)=>{
 const timer=setTimeout(()=>reject(new Error('agent fixture did not start')),60000);
 fixture.stdout.on('data',d=>{if(String(d).includes('TALK AGENT FIXTURE READY')){clearTimeout(timer);resolve();}});
 fixture.on('exit',c=>reject(new Error(`fixture exit ${c}`)));
});
const browser=await chromium.launch({args:['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',
 '--allow-loopback-in-peer-connection','--disable-features=WebRtcHideLocalIpsWithMdns','--autoplay-policy=no-user-gesture-required']});
log('runtime',{browser:browser.version(),fixture:'real Talk server + real hermes chat tasks against the labelled scripted model; local Chromium WebRTC provider peer',network:'none'});
const provider=await browser.newPage();
await provider.goto(`${FIX}/provider.html`);
const identity=await(await fetch(`${FIX}/identity`)).json();
const state=async()=>(await fetch(`${FIX}/state`)).json();
const errors=[];
async function open(){
 const ctx=await browser.newContext({viewport:{width:390,height:844},hasTouch:true,permissions:['microphone']});
 const page=await ctx.newPage();page.on('pageerror',e=>errors.push(String(e)));
 await page.route('https://telegram.org/**',r=>r.fulfill({contentType:'text/javascript',body:`window.Telegram={WebApp:{initData:new URLSearchParams(location.hash.slice(1)).get('tgWebAppData')||'',ready(){},expand(){}}};`}));
 await page.route('**/*',r=>{const url=r.request().url();if(url.startsWith('http://127.0.0.1:')||url.startsWith('https://telegram.org/'))return r.fallback();errors.push('external '+url);return r.abort();});
 await page.goto(APP+'#tgWebAppData='+encodeURIComponent(identity.initData));
 return {ctx,page};
}
const ready=page=>until(async()=>['listening','speaking','silent'].includes(await page.evaluate(()=>document.getElementById('orb').dataset.state)),'call ready',30000);
const peer=()=>provider.evaluate(()=>window.peers.length-1);
const send=(index,event)=>provider.evaluate(({index,event})=>window.peers[index].send(event),{index,event});
const got=(index,filter)=>provider.evaluate(({index,filter})=>window.peers[index].got.filter(e=>new Function('e',`return ${filter}`)(e)),{index,filter});
const bytes=text=>new TextEncoder().encode(text).length;

let first;
await step('delegation-runs-a-native-task-and-answers-on-its-own-id',async()=>{
 first=await open();await ready(first.page);
 const index=await peer();
 await send(index,{type:'turn.done',turn:{id:'u1',role:'user',transcript:'read my fixture please'}});
 const item={id:'del_browser_1',type:'delegation',target:'client',content:[{type:'input_text',text:'read '},{type:'input_text',text:identity.fixture}]};
 await send(index,{type:'delegation.created',item});
 await send(index,{type:'delegation.created',item});
 const progress=await until(()=>got(index,`e.type==='delegation.context.append'&&e.channel==='commentary'`).then(a=>a.length&&a),'progress append');
 assert(progress.length===1&&progress[0].delegation_item_id==='del_browser_1'&&/background task 1/.test(progress[0].content[0].text),'one progress append on the delegation');
 const spoken=await until(()=>got(index,`e.type==='delegation.context.append'&&e.channel==='speakable'`).then(a=>a.length&&a),'speakable result',90000);
 await new Promise(r=>setTimeout(r,3000));
 const all=await got(index,`e.type==='delegation.context.append'&&e.channel==='speakable'`);
 const text=all.map(e=>e.content[0].text).join('');
 assert(all.every(e=>e.delegation_item_id==='del_browser_1'&&e.content.length===1&&e.content[0].type==='input_text'),'every result chunk answers the original delegation');
 assert(all.every(e=>bytes(e.content[0].text)<=500)&&all.length>1,`chunks within 500 bytes (${all.map(e=>bytes(e.content[0].text))})`);
 assert(text.includes(identity.marker),'the result carries the real tool output');
 const server=await until(async()=>{const s=await state();const j=s.jobs.find(j=>j.delegation_item_id==='del_browser_1');return j&&j.receipt==='delivered'&&{s,j};},'ack');
 assert(server.j.delivered_via==='delegation'&&server.j.acks===1,'acknowledged once');
 assert(server.s.jobs.length===1&&server.s.runs.filter(r=>r.includes(identity.fixture)).length===1,'one native run for a repeated delegation');
 assert(text.replace(/\s+/g,' ').trim()===server.j.spoken.replace(/\s+/g,' ').trim(),'the spoken text is exactly the server result, sent once');
 const forbidden=await got(index,`e.type==='session.update'`);
 assert(!forbidden.length,'no session.update substitutes for delegation delivery');
 return {chunks:all.length,max_bytes:Math.max(...all.map(e=>bytes(e.content[0].text))),job:server.j.id,native_session:server.j.native_session_id};
});

await step('hangup-keeps-the-task-and-the-next-call-gets-its-result-as-context',async()=>{
 const index=await peer();
 await send(index,{type:'delegation.created',item:{id:'del_browser_2',type:'delegation',target:'client',content:[{type:'input_text',text:'slow 25'}]}});
 await until(()=>got(index,`e.type==='delegation.context.append'&&e.delegation_item_id==='del_browser_2'`).then(a=>a.length),'progress');
 await first.page.close();await first.ctx.close();
 const s1=await state();const job=s1.jobs.find(j=>j.delegation_item_id==='del_browser_2');
 assert(job&&['accepted','running'].includes(job.state),'closing the page did not cancel the task');
 const second=await open();await ready(second.page);
 const next=await peer();
 let context;
 try{context=await until(()=>got(next,`e.type==='session.context.append'&&e.content[0].text.includes('SCRIPTED-SLOW-DONE')`).then(a=>a.length&&a),'result as context',90000);}
 catch(e){const s=await state();throw new Error(e.message+' '+JSON.stringify({jobs:s.jobs.map(j=>[j.delegation_item_id,j.state,j.receipt,j.delivered_via]),sessions:s.sessions.map(x=>x.slice(-400)),got:await got(next,'true')}).slice(0,3000));}
 assert(context.every(e=>e.channel==='speakable'&&!e.delegation_item_id),'a previous call result arrives as speakable session context, not a delegation');
 const done=await until(async()=>{const s=await state();const j=s.jobs.find(j=>j.delegation_item_id==='del_browser_2');return j&&j.receipt==='delivered'&&j;},'context ack');
 assert(done.state==='succeeded'&&done.delivered_via==='context','delivered through the acknowledged context update');
 const stale=await got(next,`e.type==='delegation.context.append'&&e.delegation_item_id==='del_browser_2'`);
 assert(!stale.length,'the old delegation id is not reused in a new call');
 await second.page.close();await second.ctx.close();
 return {job:done.id,delivered_via:done.delivered_via};
});

await step('pulse-interruption-and-feedback-cross-the-real-client',async()=>{
 const card=async()=>(await state()).pulse.find(c=>c.title==='Synthetic browser pulse');
 const before=await card();
 assert(before&&before.state==='exposed'&&before.mentions===0,`greetings told nothing: ${JSON.stringify(before)}`);
 const third=await open();await ready(third.page);
 const index=await peer();
 const sessions=(await state()).sessions;
 assert(/^- P1 \[active\] Synthetic browser pulse /m.test(sessions[sessions.length-1]),'the unread card is offered again as P1');
 await send(index,{type:'output_transcript.added',item:{text:'The synthetic '}});
 await send(index,{type:'input_transcript.added',item:{text:'wait'}});
 await send(index,{type:'turn.done',turn:{id:'a_cut',role:'assistant',transcript:'The synthetic browser pulse is'}});
 await send(index,{type:'turn.done',turn:{id:'u_cut',role:'user',transcript:'wait, later'}});
 const cut=await until(async()=>{const c=await card();return c&&c.interrupted===1&&c;},'interrupted mention recorded');
 assert(cut.state==='exposed'&&cut.mentions===0,`an interrupted mention is not evidence: ${JSON.stringify(cut)}`);
 const item={id:'del_pulse_1',type:'delegation',target:'client',content:[{type:'input_text',text:'pulse P1 useful'}]};
 await send(index,{type:'delegation.created',item});
 await send(index,{type:'delegation.created',item});
 const reply=await until(()=>got(index,`e.type==='delegation.context.append'&&e.delegation_item_id==='del_pulse_1'`).then(a=>a.length&&a),'feedback reply');
 await new Promise(r=>setTimeout(r,1500));
 const all=await got(index,`e.type==='delegation.context.append'&&e.delegation_item_id==='del_pulse_1'`);
 assert(all.length===1&&all[0].channel==='speakable'&&/P1 was useful/.test(all[0].content[0].text),`one speakable feedback reply: ${JSON.stringify(all)}`);
 const done=await card();
 assert(done.state==='acknowledged'&&done.feedback.kind==='useful'&&done.acknowledged.by==='feedback','feedback acknowledged the card');
 const s=await state();
 assert(!s.jobs.some(j=>j.delegation_item_id==='del_pulse_1'),'feedback started no Hermes task');
 await third.page.close();await third.ctx.close();
 return {interrupted:cut.interrupted,reply:all[0].content[0].text,state:done.state};
});

await step('no-page-errors-or-external-requests',async()=>{assert(!errors.length,errors.join('; '));return {};});
log('summary',{passes,failures});
await browser.close();fixture.kill();
process.exit(failures?1:0);
