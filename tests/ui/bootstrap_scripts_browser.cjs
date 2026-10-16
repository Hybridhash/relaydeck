// Shipped finish/bootstrap script behavior. No rendered-browser acceptance claim.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const finish = fs.readFileSync(process.argv[2], 'utf8');
const recovery = fs.readFileSync(process.argv[3], 'utf8');
async function checkFinish(complete, check, expected) {
  const sent = [], removed = [], navigated = [], appended = [], status = {};
  const window = {
    sessionStorage: {getItem: k => k === '_flet_session_id' ? 'actual-tab-id' : null,
                     removeItem: k => removed.push(k)},
    location: {replace: url => navigated.push(url)},
  };
  const document = {getElementById: () => status, createElement: () => ({}),
                    body: {append: node => appended.push(node)}};
  const answer = (value) => { if (value === 'throw') throw new Error('network');
    return {ok: value === 200, status: value}; };
  const fetch = async (url, args) => {
    sent.push([url, args]);
    return url === '/v1/auth/pairing/complete' ? answer(complete) : answer(check);
  };
  vm.runInNewContext(finish, {window, document, fetch});
  for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[0][0], '/v1/auth/pairing/complete');
  assert.equal(sent[0][1].credentials, 'same-origin');
  assert.equal(sent[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(sent[0][1].body), {flet_session_id:'actual-tab-id'});
  const observed = sent.slice(1).map(call => call[0]);
  assert.ok(sent.every(call => call[0] !== '/v1/auth/session/recovery'));
  if (expected === 'home') {
    assert.deepEqual(navigated, ['/']); assert.deepEqual(removed, ['_flet_session_id']);
  } else if (expected === 'refused') {
    assert.deepEqual(navigated, []); assert.deepEqual(removed, ['_flet_session_id']);
    assert.match(status.textContent, /explicitly pair again/); assert.equal(appended[0].href, '/');
  } else {
    // Transient: ids kept, retry offered, nothing navigates.
    assert.deepEqual(navigated, []); assert.deepEqual(removed, []);
    assert.match(status.textContent, /try again/); assert.equal(appended[0].textContent, 'Retry');
  }
  if (complete === 200) assert.deepEqual(observed, []);
  return sent;
}
async function checkBootstrap(status) {
  const sent = [], removed = [];
  let reloads = 0;
  class Socket {}
  const window = {WebSocket: Socket, location: {host:'mac.ts.net',protocol:'https:',
                   reload: () => ++reloads},
  addEventListener: () => {},
    sessionStorage: {removeItem: key => removed.push(key)}};
  const fetch = async (url, args) => {sent.push([url,args]); return {status};};
  vm.runInNewContext(recovery, {window, fetch, Proxy, Reflect, document:{}, Set});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[0][0], '/v1/auth/pairing/browser');
  assert.equal(sent[0][1].method, 'POST');
  assert.equal(sent[0][1].credentials, 'same-origin');
  assert.deepEqual(removed, status === 201 ? ['_flet_session_id'] : []);
  assert.equal(reloads, status === 201 ? 1 : 0); // active authenticated tabs keep their parent/draft
}
(async () => {await checkFinish(200, null, 'home');
  await checkFinish(404, 204, 'home');
  await checkFinish(404, 401, 'refused');
  await checkFinish(404, 503, 'retry');
  await checkFinish(404, 'throw', 'retry');
  await checkFinish('throw', null, 'retry');
  await checkFinish(503, null, 'retry');
  await checkBootstrap(201); await checkBootstrap(204); await checkBootstrap(403);
})().catch(error => {console.error(error);process.exitCode = 1;});
