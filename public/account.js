'use strict';
const el = id => document.getElementById(id);
let socket = null, secret = '', serial = 0, heartbeat = null, deadline = null, authenticated = false, deleting = false;
function say(text) { el('status').textContent = text; }
function stop() { clearInterval(heartbeat); clearTimeout(deadline); if (socket) { socket.onopen = null; socket.onmessage = null; socket.onerror = null; socket.onclose = null; socket.close(); } socket = null; secret = ''; authenticated = false; deleting = false; el('code').value = ''; el('confirmation').value = ''; el('profile').hidden = true; el('sign-in').hidden = false; el('check').disabled = false; el('cancel').disabled = false; }
function send(type, fields = {}) { if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type, request_id: String(++serial), ...fields})); }
function failure(message) { stop(); say(message); }
el('check').addEventListener('click', () => {
  const value = el('code').value.trim();
  if (!/^[A-Za-z0-9_-]{43}$/.test(value)) { say('Enter the complete 43-character recovery code from the game.'); return; }
  stop(); secret = value; el('check').disabled = true; say('Connecting to your collection. The free service may take a moment to wake up…');
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/signal`);
  deadline = setTimeout(() => failure('The service did not respond. Please try again; nothing was deleted.'), 90000);
  socket.onopen = () => send('hello', {protocol:'PONG_BREAKER_WEBRTC_1', name:'Profile tools'});
  socket.onerror = () => failure(deleting ? 'Connection lost. Re-enter the same code to check whether deletion completed.' : 'The service could not connect. Nothing was deleted. Try again in a moment.');
  socket.onclose = () => failure(deleting ? 'Connection ended before confirmation. Re-enter the same code to check the result.' : 'Your secure connection ended. Check the profile again to continue.');
  socket.onmessage = event => {
    let message; try { message = JSON.parse(event.data); } catch { failure('Unexpected service response. No deletion was requested.'); return; }
    if (message.type === 'welcome') { send('account_auth', {token:secret}); secret = ''; heartbeat = setInterval(() => send('ping'), 20000); }
    if (message.type === 'account') {
      clearTimeout(deadline); authenticated = true; const p = message.profile;
      el('summary').textContent = `Profile ${String(p.account_id).slice(0,8)} · ${Number(p.coins)} coins · ${p.owned.length} owned designs · ${Number(p.ranked.games)} ranked matches`;
      el('sign-in').hidden = true; el('profile').hidden = false; el('remove').disabled = true; say('Profile verified. Review the details before deleting.'); el('confirmation').focus();
      deadline = setTimeout(() => failure('For privacy, this page signed out after five minutes. Nothing was deleted.'), 300000);
    }
    if (message.type === 'account_deleted') { stop(); say('Your online profile has been deleted. Its recovery code no longer works.'); }
    if (message.type === 'error') {
      const help = {account_deleted:'This profile has already been deleted. Its recovery code no longer works.', invalid_token:'That recovery code was not recognized. Copy the current code from Profile → Recovery & Privacy in the game, then try again. Nothing was deleted.'};
      failure(help[message.code] || String(message.message || 'The request could not be completed.').slice(0,200));
    }
  };
});
el('confirmation').addEventListener('input', () => { el('remove').disabled = !authenticated || deleting || el('confirmation').value !== 'DELETE'; });
el('remove').addEventListener('click', () => {
  if (!authenticated || deleting || el('confirmation').value !== 'DELETE') return;
  deleting = true; el('remove').disabled = true; el('cancel').disabled = true;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  send('account_delete', {confirmation:'DELETE', operation_id:Array.from(bytes, b => b.toString(16).padStart(2,'0')).join('')});
  say('Deleting your profile. Keep this page open for confirmation.'); clearTimeout(deadline);
  deadline = setTimeout(() => failure('Confirmation did not arrive. Re-enter the same code to check whether deletion completed.'), 30000);
});
el('cancel').addEventListener('click', () => { stop(); say('Signed out. Your profile was kept.'); });
window.addEventListener('pagehide', stop);
