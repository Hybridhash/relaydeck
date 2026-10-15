// Shipped finish/bootstrap script behavior. No rendered-browser acceptance claim.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const finish = fs.readFileSync(process.argv[2], 'utf8');
const recovery = fs.readFileSync(process.argv[3], 'utf8');
async function checkFinish(ok) {
  const sent = [], removed = [], navigated = [], links = [], status = {};
  const window = {
    sessionStorage: {getItem: k => k === '_flet_session_id' ? 'actual-tab-id' : null,
                     removeItem: k => removed.push(k)},
    location: {replace: url => navigated.push(url)},
  };
  const document = {getElementById: () => status, createElement: () => ({}),
                    body: {append: link => links.push(link)}};
  const fetch = async (url, args) => { sent.push([url,args]); return {ok}; };
  await vm.runInNewContext(finish, {window, document, fetch});
  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], '/v1/auth/pairing/complete');
  assert.equal(sent[0][1].credentials, 'same-origin');
  assert.equal(sent[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(sent[0][1].body), {flet_session_id:'actual-tab-id'});
  assert.deepEqual(removed, ['_flet_session_id']);
  assert.deepEqual(navigated, ok ? ['/'] : []);
  if (!ok) { assert.match(status.textContent, /explicitly pair again/); assert.equal(links[0].href, '/'); }
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
(async () => {await checkFinish(true); await checkFinish(false);
  await checkBootstrap(201); await checkBootstrap(204); await checkBootstrap(403);
})().catch(error => {console.error(error);process.exitCode = 1;});
