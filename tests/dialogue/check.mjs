// Turn-taking regressions for the per-call Dialogue in static/talk/app.js: the real file in node:vm with a virtual
// clock, a synthetic data channel and synthetic backend replies (no network). The vm context has no TextEncoder.
// Usage: node tests/dialogue/check.mjs [path/to/app.js]
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';

const source = readFileSync(process.argv[2] || 'static/talk/app.js', 'utf8');
const OPENING = 'Begin the configured opening greeting now.';
const tests = [];
const test = (name, run) => tests.push({name, run});

function fixture({greeting = true, phase = 'ready'} = {}) {
  let now = 0, nextTimer = 1;
  const timers = new Map();
  const nodes = new Map();
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, {hidden: false, textContent: '', disabled: false, dataset: {}, style: {setProperty() {}},
      setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; }, querySelector() { return this; }, addEventListener() {},
      removeEventListener() {}, getContext() { return new Proxy({}, {get: () => () => {}}); }, pause() {}, srcObject: null});
    return nodes.get(id);
  };
  const context = vm.createContext({document: {getElementById: element, documentElement: element('html')},
    window: {addEventListener() {}, removeEventListener() {}},
    location: {hash: '', href: 'http://localhost/tg-voice/', origin: 'http://localhost'},
    URL, URLSearchParams, AbortController, DOMException, crypto: globalThis.crypto,
    matchMedia: () => ({matches: false}), getComputedStyle: () => ({color: '#ffffff'}),
    setTimeout: (fn, ms = 0) => { timers.set(nextTimer, {fn, at: now + ms}); return nextTimer++; },
    clearTimeout: id => timers.delete(id), cancelAnimationFrame() {}, requestAnimationFrame: () => 1,
    performance: {now: () => now}, fetch: async () => { throw new Error('Unexpected network'); }});
  vm.runInContext(source, context);
  vm.runInContext(`
    const owner = {id: 'conversation', auth: 'synthetic', pending: [], sequence: 0, batchMax: 4, retries: 0, tasks: true,
      greeting: ${greeting}};
    const track = {enabled: false, stop() {}};
    function makeCall(voiceId, log) {
      return {auth: 'synthetic', phase: '${phase}', abort: new AbortController(), nodes: [], seen: new Set(),
        delegations: new Set(), jobs: new Map(), spoken: new Set(), cut: new Set(), polls: 1, conversation: owner,
        voiceId, muted: false, handshake: true, playing: true,
        stream: {getAudioTracks: () => [track], getTracks: () => [track]},
        channel: {readyState: 'open', send: m => log.push({at: performance.now(), ...JSON.parse(m)}), close() {}},
        peer: {connectionState: 'connected', close() {}}, audio: {state: 'running', resume: async () => {}, close: async () => {}},
        outputLevel: 0, inputLevel: 0, meters: {you: SILENT, agent: SILENT, due: 0}};
    }
    const sent = [];
    const current = makeCall('vs_synthetic', sent);
    call = current; conversation = owner; initData = () => 'synthetic';
    const posts = [];
    let replies = {};
    const realFlush = flush;
    flush = async () => {};
    post = async (route, body) => { posts.push({route, body: JSON.parse(JSON.stringify(body))});
      const reply = replies[route];
      const next = typeof reply === 'function' ? reply : (reply || []).shift();
      if (next instanceof Error) throw next;
      if (typeof next === 'function') return next(body);
      return next === undefined ? {} : next; };
    function emit(event, target = current) { onMessage(target, {data: JSON.stringify(event)}); }
    function routes(route) { return posts.filter(p => p.route === route); }
    function finished(id, delegationId, spoken) {
      return {id, number: 1, delegation_item_id: delegationId, voice_session_id: 'vs_synthetic', state: 'succeeded',
        receipt: 'undelivered', spoken};
    }
    function adopt(...jobs) {
      for (const job of jobs) { current.jobs.set(job.id, job.delegation_item_id); current.delegations.add(job.delegation_item_id); }
      replies.tasks = () => ({jobs});
    }
  `, context);
  const run = code => vm.runInContext(code, context);
  const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };
  const wait = async ms => {
    const until = now + ms;
    for (;;) {
      await settle();
      const due = [...timers].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!due) { now = until; await settle(); return; }
      timers.delete(due[0]); now = due[1].at; due[1].fn();
    }
  };
  const json = code => JSON.parse(run(`JSON.stringify(${code})`));
  const usable = () => run(`current.phase = 'connecting'; advance(current);`);
  return {run, json, wait, settle, usable, get now() { return now; }};
}

const openings = f => f.json(`sent.filter(m => m.content[0].text === ${JSON.stringify(OPENING)}).map(m => [m.at, m.type, m.channel])`);
const resultSends = (f, text) => f.json(`sent.filter(m => m.content[0].text === ${JSON.stringify(text)}).map(m => m.at)`);

test('O1 greeting enabled: nothing is sent at usable (duplicate greeting on top of server speak-first)', async () => {
  const f = fixture();
  f.usable();
  assert.equal(f.run('current.phase'), 'greeting');
  assert.equal(f.run('sent.length'), 0);
  assert.notEqual(f.run('talkReadiness.stages.greeting'), undefined, 'the greeting stage is still marked at usable');
  assert.equal(f.run('track.enabled'), false, 'the mic stays closed for the greeting');
});

test('O2 silent model: exactly one opening prompt at +4 s and none more by +15 s (missing or repeated opening)', async () => {
  const f = fixture();
  f.usable();
  await f.wait(3999);
  assert.deepEqual(openings(f), []);
  await f.wait(1);
  assert.deepEqual(openings(f), [[4000, 'session.context.append', 'speakable']]);
  await f.wait(11000);
  assert.equal(f.run('sent.length'), 1);
});

test('O3 model or user speaks before 4 s: the opening is never asked for (fallback ignoring a self-greeting model)', async () => {
  for (const event of [{type: 'output_transcript.added', item: {text: 'Synthetic hello'}},
    {type: 'input_transcript.added', item: {text: 'Synthetic user first'}}]) {
    const f = fixture();
    f.usable();
    await f.wait(1000);
    f.run(`emit(${JSON.stringify(event)})`);
    await f.wait(14000);
    assert.deepEqual(openings(f), [], event.type);
  }
});

test('O4 audio blocked through the hold: no prompt while blocked, one after unblock only if the model is still silent (greeting into a paused speaker)', async () => {
  const silent = fixture();
  silent.usable();
  await silent.wait(1000);
  silent.run(`current.phase = 'blocked'; updateMic(current);`);
  await silent.wait(14000);
  assert.deepEqual(openings(silent), [], 'nothing while nobody can hear it');
  await silent.run('enableAudio(current)');
  await silent.wait(0);
  assert.equal(openings(silent).length, 1, 'asked once when audio returns');
  await silent.wait(15000);
  assert.equal(openings(silent).length, 1, 'and only once');

  const greeted = fixture();
  greeted.usable();
  await greeted.wait(1000);
  greeted.run(`current.phase = 'blocked'; updateMic(current);
    emit({type: 'output_transcript.added', item: {text: 'Synthetic greeting into a paused speaker'}});`);
  await greeted.wait(14000);
  await greeted.run('enableAudio(current)');
  await greeted.wait(15000);
  assert.deepEqual(openings(greeted), [], 'a model that already greeted is not asked again');
});

test('O5 greeting disabled: no prompt, no hold, a speakable result goes at once (WAIT_FOR_USER calls delayed by a hold)', async () => {
  const f = fixture({greeting: false});
  f.usable();
  assert.equal(f.run('current.phase'), 'ready');
  f.run(`adopt(finished('jb_o5', 'del_o5', 'Synthetic O5 result')); poll(current);`);
  await f.settle();
  assert.deepEqual(resultSends(f, 'Synthetic O5 result'), [0]);
  await f.wait(15000);
  assert.deepEqual(openings(f), []);
});

test('Q1 a result during the opening hold waits for the greeting turn.done + 1 s (result interrupting the greeting)', async () => {
  const f = fixture();
  f.usable();
  await f.wait(500);
  f.run(`emit({type: 'output_transcript.added', item: {text: 'Synthetic greeting'}});`);
  await f.wait(500);
  f.run(`adopt(finished('jb_q1', 'del_q1', 'Synthetic Q1 result')); poll(current);`);
  await f.wait(4000);
  f.run(`emit({type: 'turn.done', turn: {id: 'g1', role: 'assistant', transcript: 'Synthetic greeting'}});`);
  await f.wait(999);
  assert.deepEqual(resultSends(f, 'Synthetic Q1 result'), []);
  await f.wait(1);
  assert.deepEqual(resultSends(f, 'Synthetic Q1 result'), [6000]);
  assert.deepEqual(openings(f), []);
});

test('Q2 a result during user speech waits for user turn.done + 2 s, and an open turn without turn.done releases at 6 s (cutting into the user; deadlock on a lost turn.done)', async () => {
  const closed = fixture();
  closed.run(`emit({type: 'input_transcript.added', item: {text: 'Synthetic user'}});
    adopt(finished('jb_q2', 'del_q2', 'Synthetic Q2 result')); poll(current);`);
  await closed.wait(1000);
  closed.run(`emit({type: 'turn.done', turn: {id: 'u1', role: 'user', transcript: 'Synthetic user'}});`);
  await closed.wait(1999);
  assert.deepEqual(resultSends(closed, 'Synthetic Q2 result'), []);
  await closed.wait(1);
  assert.deepEqual(resultSends(closed, 'Synthetic Q2 result'), [3000]);

  const open = fixture();
  open.run(`emit({type: 'input_transcript.added', item: {text: 'Synthetic user'}});
    adopt(finished('jb_q2', 'del_q2', 'Synthetic Q2 result')); poll(current);`);
  await open.wait(5999);
  assert.deepEqual(resultSends(open, 'Synthetic Q2 result'), []);
  await open.wait(1);
  assert.deepEqual(resultSends(open, 'Synthetic Q2 result'), [6000]);
});

test('Q3 a second result waits for the first result\'s assistant turn.done + 1 s, or a 6 s lapse (back-to-back speakables; a wedged queue)', async () => {
  const answered = fixture();
  answered.run(`adopt(finished('jb_q3a', 'del_q3a', 'Synthetic first'), finished('jb_q3b', 'del_q3b', 'Synthetic second'));
    poll(current);`);
  await answered.settle();
  assert.deepEqual(answered.json('sent.map(m => m.content[0].text)'), ['Synthetic first']);
  await answered.wait(500);
  answered.run(`emit({type: 'output_transcript.added', item: {text: 'Synthetic first'}});`);
  await answered.wait(2500);
  answered.run(`emit({type: 'turn.done', turn: {id: 'a1', role: 'assistant', transcript: 'Synthetic first'}});`);
  await answered.wait(999);
  assert.deepEqual(resultSends(answered, 'Synthetic second'), []);
  await answered.wait(1);
  assert.deepEqual(resultSends(answered, 'Synthetic second'), [4000]);

  const unanswered = fixture();
  unanswered.run(`adopt(finished('jb_q3a', 'del_q3a', 'Synthetic first'), finished('jb_q3b', 'del_q3b', 'Synthetic second'));
    poll(current);`);
  await unanswered.wait(5999);
  assert.deepEqual(resultSends(unanswered, 'Synthetic second'), []);
  await unanswered.wait(1);
  assert.deepEqual(resultSends(unanswered, 'Synthetic second'), [6000]);
});

test('Q5 commentary while the model speaks goes at once (quiet updates held behind the gate)', async () => {
  const f = fixture();
  f.run(`emit({type: 'output_transcript.added', item: {text: 'Synthetic answer in progress'}});
    replies.task = [{job: {id: 'jb_q5', delegation_item_id: 'del_q5', number: 5}, created: true, channel: 'commentary',
      reply: 'Synthetic: working on it as task 5.'}];
    replies.context = [{revision: 3, text: 'Synthetic quiet pulse context', sources: {jobs: 0, activity: 1}}];
    emit({type: 'delegation.created', item: {type: 'delegation', id: 'del_q5', target: 'client',
      content: [{type: 'input_text', text: 'synthetic job'}]}});`);
  await f.settle();
  await f.run('refresh(current)');
  assert.deepEqual(f.json('sent.map(m => [m.at, m.channel, m.delegation_item_id || null, m.content[0].text])'), [
    [0, 'commentary', 'del_q5', 'Synthetic: working on it as task 5.'],
    [0, 'commentary', null, 'Synthetic quiet pulse context']]);
  assert.deepEqual(f.json('routes("context/ack").map(p => p.body.revision)'), [3]);
});

test('Q6 the same job polled 3 times while queued is sent once and acknowledged once (duplicate speech or ack)', async () => {
  const f = fixture();
  f.run(`emit({type: 'input_transcript.added', item: {text: 'Synthetic user'}});
    adopt(finished('jb_q6', 'del_q6', 'Synthetic Q6 result'));
    for (let i = 0; i < 3; i++) poll(current);`);
  await f.settle();
  assert.equal(f.run('routes("tasks").length'), 3);
  f.run(`emit({type: 'turn.done', turn: {id: 'u1', role: 'user', transcript: 'Synthetic user'}});`);
  await f.wait(20000);
  assert.equal(resultSends(f, 'Synthetic Q6 result').length, 1);
  assert.deepEqual(f.json('routes("task/ack").map(p => p.body.job_id)'), ['jb_q6']);
});

test('Q7 a queued result then stop(): no send, no ack, and the next call starts clean (ack without append; send into a replaced call)', async () => {
  const f = fixture();
  f.run(`emit({type: 'input_transcript.added', item: {text: 'Synthetic user'}});
    adopt(finished('jb_q7', 'del_q7', 'Synthetic Q7 result')); poll(current);`);
  await f.settle();
  f.run('stop()');
  await f.wait(20000);
  assert.equal(f.run('sent.length'), 0);
  assert.equal(f.run('routes("task/ack").length'), 0);
  assert.equal(f.run('current.spoken.size + current.cut.size'), 0, 'nothing settled for a dropped item');
  f.run(`globalThis.nextSent = []; globalThis.next = makeCall('vs_next', nextSent); call = next;
    dialogueOf(next).say('job:jb_other', 'Synthetic next-call result', 'del_next');`);
  await f.wait(20000);
  assert.deepEqual(f.json('nextSent.map(m => m.content[0].text)'), ['Synthetic next-call result']);
  assert.equal(f.run('sent.length'), 0, 'the stopped call still sends nothing');
});

test('Q8 an overlapping refresh while a jobs context slot is queued or its ack is pending posts context once and speaks once (same job outcomes said twice)', async () => {
  const jobs = `replies.context = [{revision: 1, text: 'Synthetic job outcomes', sources: {jobs: 1, activity: 0}},
    {revision: 2, text: 'Synthetic job outcomes again', sources: {jobs: 1, activity: 0}}];`;
  const spoken = f => f.json('sent.filter(m => m.channel === "speakable").map(m => m.content[0].text)');

  const queued = fixture();
  queued.run(`emit({type: 'input_transcript.added', item: {text: 'Synthetic user'}}); ${jobs}
    void refresh(current);`);
  await queued.settle();
  queued.run('void refresh(current)');
  await queued.settle();
  assert.equal(queued.run('routes("context").length'), 1, 'nothing new is fetched while the slot is queued');
  queued.run(`emit({type: 'turn.done', turn: {id: 'u1', role: 'user', transcript: 'Synthetic user'}});`);
  await queued.wait(20000);
  assert.deepEqual(spoken(queued), ['Synthetic job outcomes']);
  assert.deepEqual(queued.json('routes("context/ack").map(p => p.body.revision)'), [1]);

  const acking = fixture();
  acking.run(`${jobs} replies['context/ack'] = [() => new Promise(resolve => { globalThis.releaseAck = resolve; })];
    void refresh(current);`);
  await acking.settle();
  assert.deepEqual(spoken(acking), ['Synthetic job outcomes'], 'sent at once when nobody talks');
  acking.run('void refresh(current)');
  await acking.wait(20000);
  assert.equal(acking.run('routes("context").length'), 1, 'nothing new is fetched while the ack is in flight');
  assert.deepEqual(spoken(acking), ['Synthetic job outcomes']);
  acking.run(`releaseAck({}); replies.context = [{}];`);
  await acking.settle();
  acking.run('void refresh(current)');
  await acking.settle();
  assert.equal(acking.run('routes("context").length'), 2, 'the hold ends once the ack settles');

  const failing = fixture();
  failing.run(`${jobs} replies['context/ack'] = [new Error('Network error.')]; void refresh(current);`);
  await failing.settle();
  failing.run('void refresh(current)');
  await failing.settle();
  assert.equal(failing.run('routes("context").length'), 2, 'a failed ack lets a later refresh offer the jobs again');
});

test('Q9 a whitespace-only spoken result uses the fallback text and is acknowledged only after that append (ack with nothing appended)', async () => {
  const f = fixture();
  f.run(`emit({type: 'input_transcript.added', item: {text: 'Synthetic user'}});
    adopt(finished('jb_q9', 'del_q9', '   ')); poll(current);`);
  await f.settle();
  assert.equal(f.run('routes("task/ack").length'), 0, 'not acknowledged before anything is appended');
  f.run(`emit({type: 'turn.done', turn: {id: 'u1', role: 'user', transcript: 'Synthetic user'}});`);
  await f.wait(2000);
  assert.deepEqual(f.json('sent.map(m => [m.type, m.delegation_item_id, m.content[0].text])'),
    [['delegation.context.append', 'del_q9', 'Hermes task 1 succeeded.']]);
  assert.deepEqual(f.json('routes("task/ack").map(p => p.body.job_id)'), ['jb_q9']);

  const blank = fixture();
  assert.equal(blank.run(`dialogueOf(current).note('   ', 'del_blank')`), 'none', 'empty text is never reported sent');
  blank.run(`dialogueOf(current).say('reply:del_blank', '   ', 'del_blank');
    dialogueOf(current).say('reply:del_next', 'Synthetic next reply', 'del_next');`);
  await blank.wait(1000);
  assert.deepEqual(blank.json('sent.map(m => m.content[0].text)'), ['Synthetic next reply'], 'a blank item never wedges the queue');
});

test('Q10 a speaking model with no turn.done releases a queued speakable after 6 s without output fragments, not before (results held until the call ends)', async () => {
  const f = fixture();
  f.run(`emit({type: 'output_transcript.added', item: {text: 'Synthetic refusal'}});
    adopt(finished('jb_q10', 'del_q10', 'Synthetic Q10 result')); poll(current);`);
  await f.wait(3000);
  f.run(`emit({type: 'output_transcript.added', item: {text: ' still going'}});`);
  await f.wait(5999);
  assert.deepEqual(resultSends(f, 'Synthetic Q10 result'), [], 'held while fragments are recent');
  await f.wait(1);
  assert.deepEqual(resultSends(f, 'Synthetic Q10 result'), [9000]);
});

test('D1 a held result is framed on its own delegation after another delegation\'s reply (correlation lost when the queue reorders)', async () => {
  const f = fixture();
  f.run(`current.jobs.set('jb_d1a', 'del_a'); current.delegations.add('del_a');
    replies.task = [{job: {id: 'jb_d1b', delegation_item_id: 'del_b', number: 2}, created: false, channel: 'speakable',
      reply: 'Synthetic reply for B'}];
    emit({type: 'delegation.created', item: {type: 'delegation', id: 'del_b', target: 'client',
      content: [{type: 'input_text', text: 'synthetic B'}]}});`);
  await f.settle();
  f.run(`replies.tasks = () => ({jobs: [finished('jb_d1a', 'del_a', 'Synthetic result for A')]}); poll(current);`);
  await f.settle();
  f.run(`emit({type: 'output_transcript.added', item: {text: 'Synthetic reply for B'}});`);
  await f.wait(2000);
  f.run(`emit({type: 'turn.done', turn: {id: 'a1', role: 'assistant', transcript: 'Synthetic reply for B'}});`);
  await f.wait(2000);
  assert.deepEqual(f.json('sent.map(m => [m.type, m.delegation_item_id, m.content[0].text])'), [
    ['delegation.context.append', 'del_b', 'Synthetic reply for B'],
    ['delegation.context.append', 'del_a', 'Synthetic result for A']]);
});

test('D2 a delegate() retry resolving after call replacement posts no further task and says nothing in the new call (duplicate launch or cross-call leak)', async () => {
  const f = fixture();
  f.run(`let release;
    replies.task = [new Error('Network error.'), () => new Promise(resolve => { release = resolve; })];
    emit({type: 'delegation.created', item: {type: 'delegation', id: 'del_d2', target: 'client',
      content: [{type: 'input_text', text: 'synthetic job'}]}});`);
  await f.wait(1000);
  assert.equal(f.run('routes("task").length'), 2, 'one bounded retry with the same delegation id');
  f.run(`globalThis.nextSent = []; call = makeCall('vs_next', nextSent);
    release({job: {id: 'jb_d2', delegation_item_id: 'del_d2', number: 1}, created: true, channel: 'speakable',
      reply: 'Synthetic late reply'});`);
  await f.wait(20000);
  assert.equal(f.run('routes("task").length'), 2);
  assert.equal(f.run('nextSent.length'), 0);
  assert.deepEqual(f.json('sent.map(m => m.content[0].text)'), ['Checking whether Hermes received this task.']);
});

test('D3 a duplicate delegation.created posts one api/task (duplicate launch)', async () => {
  const f = fixture();
  f.run(`replies.task = [{job: {id: 'jb_d3', delegation_item_id: 'del_d3', number: 1}, created: true, channel: 'commentary',
      reply: 'Synthetic: working on it.'}];
    for (let i = 0; i < 2; i++) emit({type: 'delegation.created', item: {type: 'delegation', id: 'del_d3', target: 'client',
      content: [{type: 'input_text', text: 'synthetic job'}]}});`);
  await f.settle();
  assert.deepEqual(f.json('routes("task").map(p => p.body.delegation_item_id)'), ['del_d3']);
});

test('C1 stop() flushes pending user and assistant turns (the last answer missing from history)', async () => {
  const f = fixture();
  f.run(`flush = realFlush;
    replies.events = [new Error('Network error.'), body => ({conversation_id: owner.id, accepted_ids: body.events.map(e => e.id)})];
    emit({type: 'turn.done', turn: {id: 'u1', role: 'user', transcript: 'Synthetic question'}});
    emit({type: 'turn.done', turn: {id: 'a1', role: 'assistant', transcript: 'Synthetic last answer'}});`);
  await f.settle();
  assert.deepEqual(f.json('owner.pending.map(p => p.event.role)'), ['user', 'assistant'], 'both roles retained after a failed save');
  f.run('stop()');
  await f.settle();
  assert.deepEqual(f.json('routes("events").at(-1).body.events.map(e => [e.role, e.text])'),
    [['user', 'Synthetic question'], ['assistant', 'Synthetic last answer']]);
  assert.equal(f.run('owner.pending.length'), 0);
});

test('U1 chunks of emoji, Cyrillic and mixed text stay within 500 UTF-8 bytes, rejoin exactly and never split a code point (wrong byte boundary)', async () => {
  const encoder = new TextEncoder();
  const reference = text => {
    const out = [];
    let part = '', bytes = 0;
    for (const character of text.trim()) {
      const size = encoder.encode(character).length;
      if (bytes + size > 500 && part) { out.push(part); part = ''; bytes = 0; }
      part += character; bytes += size;
    }
    return part ? [...out, part] : out;
  };
  for (const text of ['😀'.repeat(300), 'Привет, мир. '.repeat(60), ' a😀Жє中𝄞b '.repeat(90)]) {
    const f = fixture();
    f.run(`dialogueOf(current).note(${JSON.stringify(text)}, 'del_u1')`);
    const chunks = f.json('sent.map(m => m.content[0].text)');
    assert.ok(chunks.length > 1, 'the text needs several chunks');
    assert.ok(chunks.every(chunk => encoder.encode(chunk).length <= 500), 'a chunk exceeds 500 bytes');
    assert.ok(chunks.every(chunk => !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(chunk)),
      'a chunk splits a surrogate pair');
    assert.equal(chunks.join(''), text.trim());
    assert.deepEqual(chunks, reference(text), 'boundaries differ from a TextEncoder byte count');
  }
});

let failed = 0;
for (const {name, run} of tests) {
  try { await run(); console.log('PASS', name); }
  catch (error) { failed++; console.log('FAIL', name, error.message); }
}
console.log(`${tests.length - failed}/${tests.length} dialogue checks passed`);
process.exitCode = failed ? 1 : 0;
