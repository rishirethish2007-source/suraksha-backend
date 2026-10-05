'use strict';
const $ = id => document.getElementById(id);
let token = '', user = null, events = [], selected = null, offset = 0, nextOffset = null;
let refreshSecret = '', accessExpires = 0, renewal;
let timer, controller, generation = 0, loading = false, detailSignature = '';
const notice = message => { $('notice').textContent = message; };
function date(value) {
  // Older backend rows may omit the UTC suffix on naive database timestamps.
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  return new Date(normalized).toLocaleString();
}
async function api(path, options = {}) {
  if (refreshSecret && Date.now() >= accessExpires - 60000 && !path.startsWith('/api/v1/accounts/')) {
    if (!renewal) renewal = (async () => {
      const currentGeneration = generation;
      const result = await api('/api/v1/accounts/refresh', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({refresh_token:refreshSecret})});
      if (currentGeneration !== generation) throw new DOMException('Signed out','AbortError');
      token=result.access_token;accessExpires=Date.now()+result.expires_in*1000;
    })().finally(()=>{renewal=undefined;});
    await renewal;
  }
  const localController = new AbortController();
  const cancel = () => localController.abort();
  const parent = controller; parent?.signal.addEventListener('abort', cancel, {once:true});
  const timeout = setTimeout(cancel, 10000);
  try {
    const response = await fetch(path, { ...options, cache: 'no-store', headers: { ...options.headers, Authorization: `Bearer ${token}` }, signal: localController.signal });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(response.status === 401 ? 'Your token is invalid or expired. Disconnect and use a fresh token.' : response.status === 403 ? 'Responder or administrator access is required.' : typeof data.detail === 'string' ? data.detail : `Backend request failed (${response.status}).`);
    if (data.success === false) throw new Error(data.message || 'Action was not accepted.');
    return data;
  } catch (error) {
    if (localController.signal.aborted && !parent?.signal.aborted) throw new Error('Backend request timed out. Check that the server and database are running.');
    throw error;
  } finally { clearTimeout(timeout); parent?.signal.removeEventListener('abort', cancel); }
}

function link(label, url) {
  const a = document.createElement('a'); a.className = 'button secondary'; a.textContent = label;
  a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
}
function field(list, name, value) {
  const dt = document.createElement('dt'), dd = document.createElement('dd');
  dt.textContent = name; dd.textContent = value == null || value === '' ? 'Not provided' : String(value); list.append(dt, dd);
}
function routeURL(event) {
  const lat = $('origin-lat').value.trim(), lng = $('origin-lng').value.trim();
  const url = new URL('https://www.google.com/maps/dir/');
  url.searchParams.set('api', '1'); url.searchParams.set('destination', `${event.location.lat},${event.location.lng}`);
  url.searchParams.set('travelmode', $('mode').value); url.searchParams.set('dir_action', 'navigate');
  if (lat || lng) {
    if (!lat || !lng || !Number.isFinite(Number(lat)) || !Number.isFinite(Number(lng)) || Math.abs(Number(lat)) > 90 || Math.abs(Number(lng)) > 180) throw new Error('Enter valid latitude and longitude for the route starting point, or leave both blank.');
    url.searchParams.set('origin', `${Number(lat)},${Number(lng)}`);
  }
  return url.href;
}
function renderDetail() {
  const box = $('detail');
  const event = events.find(e => e.sos_id === selected);
  const signature = JSON.stringify(event) || 'none';
  if (signature === detailSignature && box.childNodes.length) return;
  detailSignature = signature; box.replaceChildren();
  const title = document.createElement('h2'); title.textContent = event ? `${event.sos_type.replaceAll('_', ' ')} · ${event.user_name}` : 'Select an SOS'; box.append(title);
  if (!event) { const p = document.createElement('p'); p.textContent = 'Choose an active alert. Cancelled or expired alerts leave this list on the next refresh.'; box.append(p); return; }
  const fields = document.createElement('dl'); fields.className = 'fields';
  field(fields, 'SOS ID', event.sos_id); field(fields, 'Status', event.status); field(fields, 'Name', event.user_name);
  field(fields, 'Phone', event.user_phone); field(fields, 'Message', event.message);
  field(fields, 'Coordinates', `${event.location.lat.toFixed(6)}, ${event.location.lng.toFixed(6)}`);
  field(fields, 'GPS accuracy', event.location.accuracy == null ? null : `±${event.location.accuracy} m`);
  field(fields, 'Triggered', date(event.client_timestamp)); field(fields, 'Received', date(event.created_at));
  field(fields, 'Delivery', `${event.delivery_method} · ${event.hop_count} relay hops`);
  field(fields, 'En route', event.responders_en_route); box.append(fields);
  const actions = document.createElement('div'); actions.className = 'actions';
  const route = link('Directions & ETA', '#');
  route.addEventListener('click', e => { try { route.href = routeURL(event); notice(''); } catch (error) { e.preventDefault(); notice(error.message); } });
  const mapURL = new URL('https://www.google.com/maps/search/'); mapURL.searchParams.set('api', '1'); mapURL.searchParams.set('query', `${event.location.lat},${event.location.lng}`);
  actions.append(route, link('Open location', mapURL.href));
  if (event.user_phone && /^[+\d\s().-]{3,30}$/.test(event.user_phone)) actions.append(link('Call person', `tel:${event.user_phone.replace(/[^+\d]/g, '')}`));
  for (const [action,label] of [['acknowledge','Acknowledge'],['respond','Mark me en route']]) {
    const button = document.createElement('button'); button.textContent = label; button.type = 'button';
    button.addEventListener('click', async () => {
      const currentGeneration = generation; button.disabled = true;
      try {
        await api(`/api/v1/sos/${encodeURIComponent(event.sos_id)}/${action}`, {method:'POST',body:new URLSearchParams({user_id:user.user_id})});
        if (currentGeneration !== generation) return;
        notice(action === 'respond' ? 'You are recorded as en route. Open Directions & ETA to navigate.' : 'SOS acknowledged.'); await refresh();
      } catch (error) { if (currentGeneration === generation && error.name !== 'AbortError') notice(error.message); }
      finally { button.disabled = false; }
    }); actions.append(button);
  }
  const mapButton = document.createElement('button'); mapButton.textContent = 'Show location map'; mapButton.className = 'secondary';
  mapButton.addEventListener('click', () => {
    const {lat,lng} = event.location; const url = new URL('https://www.openstreetmap.org/export/embed.html');
    url.searchParams.set('bbox', `${Math.max(-180,lng-.015)},${Math.max(-90,lat-.01)},${Math.min(180,lng+.015)},${Math.min(90,lat+.01)}`);
    url.searchParams.set('layer','mapnik'); url.searchParams.set('marker',`${lat},${lng}`);
    const frame = document.createElement('iframe'); frame.title = 'SOS location on OpenStreetMap'; frame.src = url.href; frame.referrerPolicy = 'no-referrer'; box.append(frame); mapButton.disabled = true;
  }); actions.append(mapButton); box.append(actions);
}
function render() {
  $('events').replaceChildren(); $('count').textContent = `${events.length} on this page`;
  if (!events.length) { const p = document.createElement('p'); p.textContent = 'No active SOS on this page. New alerts appear here automatically.'; $('events').append(p); }
  for (const event of events) {
    const button = document.createElement('button'); button.className = `incident${selected === event.sos_id ? ' selected' : ''}`;
    const type = document.createElement('strong'); type.textContent = `${event.sos_type.replaceAll('_',' ')} · ${event.status}`;
    const name = document.createElement('span'); name.textContent = event.user_name;
    const detail = document.createElement('small'); detail.textContent = `${date(event.created_at)} · ${event.hop_count} hops · ${event.responders_en_route} en route`;
    button.append(type,name,detail); button.addEventListener('click',()=>{selected=event.sos_id;render();}); $('events').append(button);
  }
  $('previous').disabled = offset === 0; $('next').disabled = nextOffset === null; renderDetail();
}
async function refresh() {
  if (!token || !user || loading) return;
  loading = true; $('refresh').disabled = true; const currentGeneration = generation;
  try {
    const data = await api(`/api/v1/dashboard/events?offset=${offset}&limit=50`);
    if (currentGeneration !== generation) return;
    events = data.events; nextOffset = data.next_offset;
    if (!events.some(e=>e.sos_id===selected)) selected=events[0]?.sos_id ?? null;
    $('connection').textContent = 'Connected'; $('updated').textContent = `Updated ${date(data.server_time)}`; render();
  } catch (error) { if (currentGeneration === generation && error.name !== 'AbortError') { notice(error.message); $('connection').textContent = 'Refresh failed — data may be stale'; } }
  finally { if (currentGeneration === generation) { loading=false; $('refresh').disabled=false; } }
}
function disconnect() {
  if (refreshSecret) void fetch('/api/v1/accounts/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh_token:refreshSecret}),signal:AbortSignal.timeout(10000)}).catch(()=>{});
  refreshSecret='';accessExpires=0;
  generation++; detailSignature=''; controller?.abort(); clearInterval(timer); token='';user=null;events=[];selected=null;offset=0;loading=false;
  $('token').value='';$('desk').hidden=true;$('login').hidden=false;$('connection').textContent='Not connected';$('events').replaceChildren();$('detail').replaceChildren();notice('');
}
// Keep the secret in tab memory only, never in URLs or external map requests.
$('login-form').addEventListener('submit',async e=>{
  e.preventDefault();const secret=$('token').value.trim();disconnect();token=secret;controller=new AbortController();const currentGeneration=generation;
  try {
    const identity=await api('/api/v1/identity/me'); if(currentGeneration!==generation)return;user=identity;
    if(!['responder','admin'].includes(user.role))throw new Error('Responder or administrator access is required.');
    $('identity').textContent=`Connected as ${user.name || user.user_id}`;$('login').hidden=true;$('desk').hidden=false;
    await refresh();if(currentGeneration!==generation)return;timer=setInterval(()=>{if(!document.hidden)void refresh();},15000);
  } catch(error) { if(currentGeneration===generation){disconnect();notice(error.message);} }
});
$('account-form').addEventListener('submit',async e=>{
  e.preventDefault();const email=$('account-email').value.trim(),password=$('account-password').value;
  disconnect();controller=new AbortController();const currentGeneration=generation;
  try {
    const result=await api('/api/v1/accounts/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
    if(currentGeneration!==generation)return;
    token=result.access_token;refreshSecret=result.refresh_token;accessExpires=Date.now()+result.expires_in*1000;
    const identity=await api('/api/v1/identity/me');if(currentGeneration!==generation)return;user=identity;
    if(!['responder','admin'].includes(user.role))throw new Error('Ask the backend operator to grant your account responder access.');
    $('account-password').value='';$('identity').textContent=`Connected as ${user.name || user.user_id}`;$('login').hidden=true;$('desk').hidden=false;
    await refresh();if(currentGeneration!==generation)return;timer=setInterval(()=>{if(!document.hidden)void refresh();},15000);
  } catch(error) {if(currentGeneration===generation){disconnect();notice(error.message);}}
});
$('logout').addEventListener('click',disconnect);$('refresh').addEventListener('click',()=>{notice('');void refresh();});
$('next').addEventListener('click',()=>{if(!loading&&nextOffset!==null){offset=nextOffset;void refresh();}});
$('previous').addEventListener('click',()=>{if(!loading){offset=Math.max(0,offset-50);void refresh();}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refresh();});
$('locate').addEventListener('click',()=>{
  if(!navigator.geolocation){notice('Location is unavailable. Enter the responder coordinates manually.');return;}
  navigator.geolocation.getCurrentPosition(p=>{$('origin-lat').value=p.coords.latitude;$('origin-lng').value=p.coords.longitude;notice('Route starting point updated.');},()=>notice('Location permission was denied or unavailable. Use localhost/HTTPS, or enter coordinates manually.'),{enableHighAccuracy:true,timeout:10000,maximumAge:30000});
});
