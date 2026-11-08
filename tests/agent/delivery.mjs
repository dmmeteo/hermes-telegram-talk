// Fault injection for task submission and result delivery in static/talk/app.js (synthetic data, no network).
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';

const source = readFileSync(process.argv[2] || 'static/talk/app.js', 'utf8');
const tests = [];
const test = (group, name, run) => tests.push({group, name, run});

function fixture() {
  const nodes = new Map();
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, {hidden: false, textContent: '', disabled: false, dataset: {}, style: {setProperty() {}},
      setAttribute(name, value) { this[name] = value; }, querySelector() { return this; }, addEventListener() {},
      removeEventListener() {}, getContext() { return new Proxy({}, {get: () => () => {}}); }, pause() {}, srcObject: null});
    return nodes.get(id);
  };
  const timers = [];
  const context = vm.createContext({document: {getElementById: element, documentElement: element('html')},
    window: {addEventListener() {}, removeEventListener() {}},
    location: {hash: '', href: 'http://localhost/tg-voice/', origin: 'http://localhost'},
    URL, URLSearchParams, AbortController, DOMException, TextEncoder, crypto: globalThis.crypto,
    matchMedia: () => ({matches: false}), getComputedStyle: () => ({color: '#ffffff'}),
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; }, clearTimeout() {},
    cancelAnimationFrame() {}, requestAnimationFrame: () => 1, performance,
    fetch: async () => { throw new Error('Unexpected network'); }});
  vm.runInContext(source, context);
  vm.runInContext(`
    const owner = {id: 'conversation', auth: 'synthetic', pending: [], sequence: 0, batchMax: 4, retries: 0, tasks: true};
    const sent = [];
    const posts = [];
    const channel = {readyState: 'open', failAt: null, send(m) {
      if (this.failAt !== null && sent.length >= this.failAt) throw new DOMException('Synthetic send failure', 'OperationError');
      sent.push(JSON.parse(m)); }, close() {}};
    const current = {auth: 'synthetic', phase: 'ready', abort: new AbortController(), nodes: [], seen: new Set(),
      delegations: new Set(), jobs: new Map(), spoken: new Set(), cut: new Set(), polls: 1, conversation: owner,
      voiceId: 'vs_synthetic', muted: false, channel, peer: {connectionState: 'connected', close() {}},
      audio: {state: 'running', close: async () => {}}, playing: true, outputLevel: 0, inputLevel: 0};
    call = current; conversation = owner; initData = () => 'synthetic';
    let replies = {};
    post = async (route, body) => { posts.push({route, body: JSON.parse(JSON.stringify(body))});
      const next = (replies[route] || []).shift(); if (next instanceof Error) throw next;
      if (typeof next === 'function') return next(body); return next === undefined ? {} : next; };
    function failure(status) { const e = new Error('synthetic ' + status); e.status = status; e.serverMessage = 'Synthetic refusal.'; return e; }
    function emit(event) { onMessage(current, {data: JSON.stringify(event)}); }
    function texts() { return sent.map(m => m.content[0].text); }
  `, context);
  const run = code => vm.runInContext(code, context);
  const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
  const fire = async ms => {
    const due = timers.splice(0).filter(t => ms === undefined || t.ms === ms);
    for (const t of due) t.fn();
    await settle();
    return due.length;
  };
  return {run, settle, fire, timers};
}

const job = (over = {}) => ({id: 'jb_' + 'a'.repeat(24), number: 1, delegation_item_id: 'del_1', voice_session_id: 'vs_synthetic',
  state: 'succeeded', receipt: 'undelivered', spoken: 'Hermes task 1 (synthetic) finished. Result: SYNTHETIC-RESULT', ...over});

test('ambiguous_task_submission', 'lost response after acceptance is resubmitted with the same delegation id, never called not executed', async () => {
  const f = fixture();
  f.run(`replies.task = [new Error('Network error. Check your connection, then tap Retry.'),
    {job: {id: 'jb_${'b'.repeat(24)}', delegation_item_id: 'del_lost', number: 1}, created: false, channel: 'commentary',
     reply: 'This was already sent to Hermes as task 1; it is running.'}];
    emit({type: 'delegation.created', item: {type: 'delegation', id: 'del_lost', target: 'client', content: [{type: 'input_text', text: 'synthetic lookup'}]}});`);
  await f.settle();
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(texts())')), ['Checking whether Hermes received this task.']);
  assert.equal(await f.fire(1000), 1, 'one bounded retry scheduled');
  const posts = JSON.parse(f.run('JSON.stringify(posts.filter(p => p.route === "task").map(p => p.body.delegation_item_id))'));
  assert.deepEqual(posts, ['del_lost', 'del_lost']);
  assert.equal(f.run('current.jobs.get("jb_' + 'b'.repeat(24) + '")'), 'del_lost');
  assert.ok(!f.run('texts().join(" ")').includes('Nothing was executed'));
  assert.equal(f.run('sent.every(m => m.delegation_item_id === "del_lost")'), true);
});
test('ambiguous_task_submission', 'server error three times ends uncertain, not as a non-execution claim', async () => {
  const f = fixture();
  f.run(`replies.task = [failure(500), failure(502), new Error('Network error.')];
    emit({type: 'delegation.created', item: {type: 'delegation', id: 'del_500', target: 'client', content: [{type: 'input_text', text: 'x'}]}});`);
  await f.settle(); await f.fire(1000); await f.fire(2000);
  const said = f.run('texts().join(" | ")');
  assert.match(said, /not confirmed whether Hermes received this task/);
  assert.ok(!said.includes('Nothing was executed'), said);
  assert.equal(f.run('posts.filter(p => p.route === "task").length'), 3);
  assert.equal(f.run('sent[sent.length - 1].channel'), 'speakable');
});
test('ambiguous_task_submission', 'a definitive refusal is said once as not executed and is not retried', async () => {
  const f = fixture();
  f.run(`replies.task = [failure(503)];
    emit({type: 'delegation.created', item: {type: 'delegation', id: 'del_503', target: 'client', content: [{type: 'input_text', text: 'x'}]}});`);
  await f.settle();
  assert.match(f.run('texts().join(" ")'), /could not take this task\. Synthetic refusal\. Nothing was executed\./);
  assert.equal(f.timers.length, 0);
  assert.equal(f.run('posts.length'), 1);
});
test('ambiguous_task_submission', 'a refusal of the resubmission is not a non-execution claim', async () => {
  const f = fixture();
  f.run(`replies.task = [new Error('Network error.'), failure(503)];
    emit({type: 'delegation.created', item: {type: 'delegation', id: 'del_r503', target: 'client', content: [{type: 'input_text', text: 'x'}]}});`);
  await f.settle(); await f.fire(1000);
  const said = f.run('texts().join(" | ")');
  assert.match(said, /not confirmed whether Hermes received this task/);
  assert.ok(!said.includes('Nothing was executed'), said);
});
test('ambiguous_task_submission', 'cancel and result replies about another task are spoken, not treated as unconfirmed', async () => {
  const f = fixture();
  f.run(`replies.task = [{job: {id: 'jb_${'c'.repeat(24)}', delegation_item_id: 'del_original', number: 1}, created: false,
      channel: 'speakable', reply: 'Hermes task 1 (synthetic) finished. Result: STORED-RESULT'}];
    emit({type: 'delegation.created', item: {type: 'delegation', id: 'del_recall', target: 'client', content: [{type: 'input_text', text: 'result of task 1'}]}});`);
  await f.settle();
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(sent.map(m => [m.delegation_item_id, m.channel, m.content[0].text]))')),
    [['del_recall', 'speakable', 'Hermes task 1 (synthetic) finished. Result: STORED-RESULT']]);
  assert.equal(f.run('posts.filter(p => p.route === "task").length'), 1);
  assert.equal(f.run('current.jobs.size'), 0, 'the named job is not adopted for this delegation');
});
test('partial_append_recovery', 'a closed channel queues nothing and acks nothing; the next open poll delivers once', async () => {
  const f = fixture();
  f.run(`current.jobs.set('${job().id}', 'del_1'); current.delegations.add('del_1'); channel.readyState = 'closing';
    replies.tasks = [{jobs: [${JSON.stringify(job())}]}, {jobs: [${JSON.stringify(job())}]}];
    replies['task/ack'] = [{job: {}}]; poll(current);`);
  await f.settle();
  assert.equal(f.run('sent.length'), 0);
  assert.equal(f.run('posts.filter(p => p.route === "task/ack").length'), 0);
  assert.equal(f.timers.at(-1).ms, 2000, 'polling continues while a result is undelivered');
  f.run(`channel.readyState = 'open';`);
  await f.fire(2000);
  assert.equal(f.run('sent.length'), 1);
  assert.equal(f.run('posts.filter(p => p.route === "task/ack").length'), 1);
  assert.equal(f.run('sent[0].delegation_item_id'), 'del_1');
});
test('partial_append_recovery', 'a send failing mid-chunk is never acked or repeated and polling continues', async () => {
  const f = fixture();
  const long = job({spoken: 'Hermes task 1 (synthetic) finished. Result: ' + 'Z'.repeat(1400)});
  f.run(`current.jobs.set('${long.id}', 'del_1'); current.delegations.add('del_1'); channel.failAt = 1;
    replies.tasks = [{jobs: [${JSON.stringify(long)}]}, {jobs: [${JSON.stringify(long)}]}]; poll(current);`);
  await f.settle();
  assert.equal(f.run('sent.length'), 1, 'one chunk queued before the failure');
  assert.equal(f.run('posts.filter(p => p.route === "task/ack").length'), 0);
  assert.equal(f.run(`current.cut.has('${long.id}')`), true);
  f.run('channel.failAt = null;');
  assert.equal(await f.fire(), 1, 'a next poll was scheduled');
  assert.equal(f.run('sent.length'), 1, 'the cut result is not repeated in this call');
  assert.equal(f.run('posts.filter(p => p.route === "tasks").length'), 2);
  assert.equal(f.timers.length, 1, 'polling is still scheduled');
});
test('partial_append_recovery', 'a failing tasks request or a throwing channel never stops polling or Pulse refresh', async () => {
  const f = fixture();
  f.run(`replies.tasks = [new Error('Network error.')]; poll(current);`);
  await f.settle();
  assert.equal(f.timers.at(-1).ms, 5000);
  f.run(`channel.failAt = 0; replies.tasks = [{jobs: [], context_pending: true}];
    replies.context = [{revision: 1, text: '## Context update 1\\n- synthetic pulse item', sources: {jobs: 0, activity: 1}}];`);
  await f.fire(5000);
  assert.equal(f.run('posts.filter(p => p.route === "context/ack").length'), 0, 'an unsent update is not acked');
  assert.equal(f.run('current.contextCut === true'), false, 'nothing was queued, so a later refresh may send it');
  assert.equal(f.timers.length, 1, 'polling continues after the channel threw');
});
let failed = 0;
const results = {};
for (const {group, name, run} of tests) {
  try { await run(); console.log('PASS', group, name); (results[group] ||= []).push({name, pass: true}); }
  catch (error) { failed++; console.log('FAIL', group, name, error.message); (results[group] ||= []).push({name, pass: false, error: error.message}); }
}
console.log('DELIVERY_RESULTS ' + JSON.stringify(results));
console.log(`${tests.length - failed}/${tests.length} delivery fault checks passed`);
process.exitCode = failed ? 1 : 0;
