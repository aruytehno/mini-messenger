const $ = id => document.getElementById(id);
let user = null, selected = null, dialogList = [], messages = new Map();
let registering = false, socket = null, reconnectTimer = null, generation = 0;
let pending = null, sending = false;
function messageId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
function notice(text = '') { $('notice').textContent = text; }
async function api(path, method = 'GET', body) {
  const response = await fetch('/api' + path, {
    method, credentials: 'same-origin',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && user) showAuth();
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте введённые данные.');
  }
  return data;
}
function showAuth() {
  user = null; selected = null; generation++; messages.clear(); pending = null;
  clearTimeout(reconnectTimer);
  if (socket) { socket.onclose = null; socket.close(); socket = null; }
  $('password-dialog').close();
  $('workspace').hidden = true; $('auth').hidden = false;
  $('message').value = ''; $('password').value = ''; $('messages').replaceChildren();
}
async function enter(account) {
  user = account; generation++; selected = null; messages.clear();
  $('auth').hidden = true; $('workspace').hidden = false;
  $('account').textContent = '@' + user.login;
  $('password').value = ''; $('chat-title').textContent = 'Выберите собеседника';
  $('message-form').hidden = true; $('older').hidden = true; renderMessages();
  await loadDialogs(); connect();
}
function renderDialogs() {
  $('dialogs').replaceChildren();
  for (const dialog of dialogList) {
    const button = document.createElement('button');
    button.textContent = '@' + dialog.login;
    button.className = selected?.id === dialog.id ? 'active' : '';
    button.onclick = () => openDialog(dialog).catch(e => notice(e.message));
    $('dialogs').append(button);
  }
}
async function loadDialogs() {
  const current = generation;
  const rows = await api('/dialogs');
  if (current !== generation) return;
  dialogList = rows; renderDialogs();
}
async function openDialog(dialog) {
  selected = dialog; messages.clear(); pending = null;
  $('message').value = ''; $('chat-title').textContent = '@' + dialog.login;
  $('message-form').hidden = false; $('older').hidden = true;
  renderDialogs(); renderMessages(); await loadMessages(); $('message').focus();
}
function renderMessages() {
  const panel = $('messages');
  const stick = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 100;
  const scrollTop = panel.scrollTop;
  panel.replaceChildren();
  const stamp = Date.now();
  for (const [id, message] of messages) {
    if (Date.parse(message.expires_at) <= stamp) messages.delete(id);
  }
  const ordered = [...messages.values()].sort((a,b) => a.id - b.id);
  for (const message of ordered) {
    const bubble = document.createElement('article');
    bubble.className = 'bubble' + (message.sender_id === user?.id ? ' mine' : '');
    const text = document.createElement('p'); text.textContent = message.text;
    const time = document.createElement('time'); time.dateTime = message.created_at;
    time.textContent = new Date(message.created_at).toLocaleTimeString('ru', {hour:'2-digit', minute:'2-digit'});
    time.title = 'Исчезнет: ' + new Date(message.expires_at).toLocaleString('ru');
    bubble.append(text,time); panel.append(bubble);
  }
  if (!ordered.length) {
    const empty = document.createElement('p'); empty.className = 'empty';
    empty.textContent = selected ? 'Пока нет сообщений за последние 24 часа.' : 'Найдите друга по логину и начните разговор.';
    panel.append(empty);
  }
  panel.scrollTop = stick ? panel.scrollHeight : scrollTop;
}
async function loadMessages(older = false) {
  if (!selected || !user) return;
  const id = selected.id, current = generation;
  const oldest = messages.size ? Math.min(...messages.keys()) : null;
  const rows = await api(`/dialogs/${id}/messages` + (older && oldest ? `?before=${oldest}` : ''));
  if (selected?.id !== id || current !== generation) return;
  for (const message of rows) messages.set(message.id, message);
  if (older || oldest === null) $('older').hidden = rows.length < 100;
  renderMessages();
}
function connect() {
  if (!user) return;
  clearTimeout(reconnectTimer);
  if (socket) { socket.onclose = null; socket.close(); }
  const current = generation;
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  socket = ws;
  ws.onopen = () => {
    $('connection').textContent = 'На связи';
    Promise.all([loadDialogs(), loadMessages()]).catch(e => notice(e.message));
  };
  ws.onmessage = event => {
    if (current !== generation) return;
    const data = JSON.parse(event.data);
    if (data.type === 'message') {
      if (selected?.id === data.message.dialog_id) {
        messages.set(data.message.id, data.message); renderMessages();
      }
      loadDialogs().catch(e => notice(e.message));
    }
  };
  ws.onclose = () => {
    if (!user || current !== generation) return;
    $('connection').textContent = 'Переподключение…';
    reconnectTimer = setTimeout(async () => {
      try { await api('/me'); connect(); } catch (e) { notice(e.message); if (user) connect(); }
    }, 2500);
  };
}
$('auth-toggle').onclick = () => {
  registering = !registering; notice();
  $('auth-title').textContent = registering ? 'Создать аккаунт' : 'С возвращением';
  $('auth-submit').textContent = registering ? 'Зарегистрироваться' : 'Войти';
  $('auth-toggle').textContent = registering ? 'Уже есть аккаунт — войти' : 'Создать аккаунт';
  $('password').autocomplete = registering ? 'new-password' : 'current-password';
};
$('auth-form').onsubmit = async event => {
  event.preventDefault(); notice(); $('auth-submit').disabled = true;
  try { await enter(await api(registering ? '/register' : '/login', 'POST', {login: $('login').value, password: $('password').value})); }
  catch (e) { notice(e.message); }
  finally { $('auth-submit').disabled = false; }
};
$('logout').onclick = async () => {
  try { await api('/logout', 'POST'); showAuth(); notice(); } catch(e) { notice(e.message); }
};
$('search-form').onsubmit = async event => {
  event.preventDefault(); notice();
  try { const dialog = await api('/dialogs', 'POST', {login: $('peer').value}); await loadDialogs(); await openDialog(dialog); $('peer').value = ''; }
  catch(e) { notice(e.message); }
};
$('message-form').onsubmit = async event => {
  event.preventDefault();
  if (sending || !selected) return;
  const text = $('message').value.trim(); if (!text) return;
  const id = selected.id, current = generation;
  // Retain the UUID after a network failure to make a retry idempotent.
  if (!pending || pending.text !== text || pending.dialog !== id) {
    pending = {text, dialog: id, client_id: messageId()};
  }
  sending = true; $('send').disabled = true; $('message').disabled = true; notice();
  try {
    const message = await api(`/dialogs/${id}/messages`, 'POST', {text, client_id: pending.client_id});
    if (current === generation && selected?.id === id) {
      messages.set(message.id, message); $('message').value = ''; pending = null;
      renderMessages(); $('messages').scrollTop = $('messages').scrollHeight;
    }
  } catch(e) { notice(e.message + ' Можно повторить отправку.'); }
  finally { sending = false; $('send').disabled = false; $('message').disabled = false; }
};
$('message').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('message-form').requestSubmit(); }
};
$('older').onclick = () => loadMessages(true).catch(e => notice(e.message));
$('settings').onclick = () => $('password-dialog').showModal();
$('close-settings').onclick = () => $('password-dialog').close();
$('password-form').onsubmit = async event => {
  event.preventDefault();
  try {
    await api('/password', 'POST', {current_password: $('old-password').value, new_password: $('new-password').value});
    $('password-form').reset(); $('password-dialog').close(); notice('Пароль изменён. Другие сессии завершены.'); connect();
  } catch(e) { notice(e.message); }
};
setInterval(() => {
  if (user && [...messages.values()].some(m => Date.parse(m.expires_at) <= Date.now())) renderMessages();
}, 1000);
setInterval(() => { if (user) Promise.all([loadDialogs(), loadMessages()]).catch(e => notice(e.message)); }, 15000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && user) { renderMessages(); loadMessages().catch(e => notice(e.message)); }
});
api('/me').then(enter).catch(e => { if (user) notice(e.message); });
