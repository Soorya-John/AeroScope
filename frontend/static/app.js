(() => {
  'use strict';
  const BRAND = 'AeroScope';
  const API = '';
  const clientId = sessionStorage.aeroscopeClient || (sessionStorage.aeroscopeClient=crypto.randomUUID());
  const state = { telemetry: null, events: [], samples: [], inspector: [], inspectorRates:{}, profiles: [], analyses: [], activePage: 'overview', paused: false, wsUp: false, followMap: true, mapView:{zoom:1,panX:0,panY:0}, selectedAnalysis: null, replayTime: 0, replayPlaying: false, replayTimer: null, theme: 'dark', timeWindow: 60, profileEdit: null, search: '', inspectorType: '' };
  const $ = (q, root=document) => root.querySelector(q);
  const $$ = (q, root=document) => [...root.querySelectorAll(q)];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const field = (path) => {
    const [section,key] = path.split('.');
    if(section==='connection'){const c=state.telemetry?.connection||{},value=c[key];return {value,available:value!==undefined&&value!==null&&Number(c.messages_total||0)>0,source:'AeroScope',received_at:c.last_update,fresh:c.state==='connected'}}
    return state.telemetry?.[section]?.[key] ?? {available:false,value:null};
  };
  const val = path => { const f=field(path); return f.available && f.fresh !== false ? f.value : null; };
  const nfmt = (v, digits=1) => Number.isFinite(Number(v)) ? Number(v).toFixed(digits) : '—';
  const stamp = (s) => { if(!s) return '—'; const d=new Date(s); return Number.isNaN(+d)?'—':d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}); };
  const ago = s => { if(!s)return 'No samples';const sec=Math.max(0,(Date.now()-new Date(s).getTime())/1000);return sec<1?'just now':`${sec.toFixed(sec<10?1:0)}s ago`; };
  const toast = (message, kind='') => { const el=document.createElement('div');el.className=`toast ${kind}`;el.textContent=message;$('#toast-stack').append(el);setTimeout(()=>el.remove(),4200); };
  async function api(path, options={}) {
    const headers = {'X-AeroScope-Client':clientId,...options.headers};
    if (options.body && !(options.body instanceof FormData)) headers['Content-Type']='application/json';
    const response=await fetch(API+path,{...options,headers});
    let data={};try{data=await response.json()}catch{}
    if(!response.ok) throw new Error(data.error||`Request failed (${response.status})`);
    return data;
  }
  const noData = '—';

  function setPage(page) {
    if(!$('#page-'+page)) page='overview';
    state.activePage=page;
    $$('.page').forEach(el=>el.classList.toggle('active',el.id==='page-'+page));
    $$('.nav-link').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
    const link=$(`.nav-link[data-page="${page}"]`);
    $('#page-crumb').textContent=link?.textContent?.trim().replace(/\d+$/,'')||'Overview';
    if(page==='inspector') loadInspector();
    if(page==='vehicles') loadProfiles();
    if(page==='logs') loadAnalyses();
    if(page==='diagnostics') loadEvents();
    if(page==='telemetry') renderTelemetryCards();
    if(page==='parameters'){loadParameters();loadParameterSnapshots()}
    if(page==='mission') loadMission();
    requestAnimationFrame(renderAllCharts);
  }
  function connectNav() {
    $$('.nav-link').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();const p=a.dataset.page;setPage(p);history.replaceState(null,'','#'+p)}));
    const route=()=>setPage((location.hash||'#overview').slice(1));
    window.addEventListener('hashchange',route);route();
  }

  let recordingRefreshAt=0,armRefreshAt=0;
  function updateConnection() {
    const c=state.telemetry?.connection||{state:'disconnected'};
    const simulated=!!state.telemetry?.vehicle?.simulated;
    const connState=c.state||'disconnected';
    const pill=$('#connection-pill');pill.className=`connection-pill state-${connState}${simulated&&connState==='connected'?' state-simulated':''}`;
    const labels={disconnected:'DISCONNECTED',connecting:'CONNECTING',connected:simulated?'SIMULATED':'CONNECTED',stale:'STALE DATA',reconnecting:'RECONNECTING',error:'CONNECTION ERROR'};
    $('#connection-label').textContent=labels[connState]||connState.toUpperCase();
    $('#vehicle-name').textContent=state.telemetry?.vehicle?.name||'No vehicle';
    const type=state.telemetry?.vehicle?.type?.available?state.telemetry.vehicle.type.value:null;
    const ap=state.telemetry?.vehicle?.autopilot?.available?state.telemetry.vehicle.autopilot.value:null;
    const discrepancy=state.telemetry?.vehicle?.identity_warning;
    $('#vehicle-subtitle').textContent=discrepancy?`⚠ EXPECTED ${discrepancy.expected} · CONNECTED ${discrepancy.actual}`:[state.telemetry?.vehicle?.environment==='sitl'?'AUTOPILOT SITL':null,type,ap].filter(Boolean).join(' · ')||'Waiting for connection';
    $('#vehicle-subtitle').style.color=discrepancy?'#e5b968':'';
    const vehicle=state.telemetry?.vehicle||{},details=vehicle.firmware_details||{},known=f=>f?.available?String(f.value):'Unknown',protocol=vehicle.protocol_capabilities?.available?Number(vehicle.protocol_capabilities.value):null;
    const protocolText=protocol===null?'Unknown':`Mission int ${protocol&4?'yes':'no'} · mission float ${protocol&1?'yes':'no'} · ${protocol&16?'bytewise params':protocol&131072?'C-cast params':protocol&2?'float params':'parameter encoding unknown'} · MAVLink 2 ${protocol&8192?'yes':'no'}`;
    const info=[['SYSTEM / COMPONENT',`${known(vehicle.system_id)} / ${known(vehicle.component_id)}`],['AUTOPILOT',known(vehicle.autopilot)],['VEHICLE TYPE',known(vehicle.type)],['FIRMWARE',known(vehicle.firmware)],['MAVLINK WIRE VERSION',known(vehicle.mavlink_version)],['VENDOR / PRODUCT',details.vendor_id||details.product_id?`${details.vendor_id??'Unknown'} / ${details.product_id??'Unknown'}`:'Unknown'],['PROTOCOL FLAGS',protocolText]];
    const infoGrid=$('#vehicle-info-grid');if(infoGrid){infoGrid.innerHTML=info.map(([label,value])=>`<div><small>${esc(label)}</small><b>${esc(value)}</b></div>`).join('');$('#vehicle-info-label').textContent=vehicle.simulated?'SIMULATED':protocol===null?'HEARTBEAT · VERSION UNKNOWN':'VERSION REPORTED'}
    const banner=$('#disconnect-banner');
    $('#sim-control-panel')?.classList.toggle('hidden',!simulated);
    const controlLive=connState==='connected'&&!simulated&&Number(c.heartbeat_age_s)<=2;
    ['params-download','params-refresh-all'].forEach(id=>{const el=$('#'+id);if(el)el.disabled=!controlLive});
    ['mission-download','mission-upload','mission-clear'].forEach(id=>{const el=$('#'+id),supported=id==='mission-download'||(ap==='ArduPilot'&&['Quadrotor','Hexarotor','Octorotor','Tricopter','Coaxial','Helicopter'].includes(type));if(el){el.disabled=!controlLive||!supported;el.title=supported?'':'Mission writes are qualified only for ArduPilot Copter'}});
    const showBanner=['disconnected','error'].includes(connState);
    banner.classList.toggle('hidden',!showBanner);
    $('#live-tag').textContent=simulated?'SIMULATED TELEMETRY':connState==='connected'?'LIVE TELEMETRY':connState==='stale'?'TELEMETRY STALE':connState==='reconnecting'?'LINK RECOVERING':'NO ACTIVE LINK';
    $('#live-tag').style.color=simulated?'#dcb96a':connState==='connected'?'#69c99a':connState==='stale'||connState==='reconnecting'?'#e6bd68':'';
    const rec=$('#record-toggle');
    if(Date.now()-recordingRefreshAt>1500){recordingRefreshAt=Date.now();api('/api/recording').then(r=>{rec.innerHTML=r.recording?'⏹ <span>Stop recording</span>':'⏺ <span>Record</span>';rec.classList.toggle('button-primary',r.recording)}).catch(()=>{})}
    $('#mavlink-health').textContent=connState==='connected'&&c.transport!=='simulation'?'Connected':connState==='reconnecting'?'Reconnecting':'Idle';
    $('#mavlink-health').className=connState==='connected'?'ok':connState==='error'?'bad':'';
    renderModeControl();
    renderArmControl();
    renderGuidedControl();
  }
  async function renderArmControl(){
    const button=$('#arm-action');if(!button)return;
    const v=state.telemetry?.vehicle||{},c=state.telemetry?.connection||{},status=state.telemetry?.status||{};
    const armed=status.armed?.available?status.armed.value:null,sim=!!v.simulated,live=c.state==='connected'&&!sim&&Number(c.heartbeat_age_s)<=2;
    $('#arm-reported').textContent=armed===null?'Unknown':armed?'ARMED':'DISARMED';
    $('#arm-state-label').textContent=sim?'SIMULATED':armed===null?'UNKNOWN':armed?'ARMED':'DISARMED';
    const checks=[['GPS fix',state.telemetry?.gps?.fix_type],['Battery voltage',state.telemetry?.battery?.voltage_v],['Estimator health',status.estimator]];
    const unknown=checks.filter(([,f])=>!f?.available).map(([n])=>n);
    $('#arm-safety-summary').textContent=unknown.length?`UNKNOWN safety data: ${unknown.join(', ')}`:'GPS, battery and estimator fields are reported; review vehicle warnings before arming.';
    button.disabled=!live||armed===null;
    button.textContent=armed?'Disarm vehicle':'Arm vehicle';
    button.classList.toggle('disarm-button',!!armed);
    if(Date.now()-armRefreshAt<1000)return;armRefreshAt=Date.now();
    try{const result=await api('/api/control/arm');const req=result.request||{};$('#arm-request-state').textContent=req.state==='PENDING'?`Waiting for heartbeat confirmation: ${req.armed?'armed':'disarmed'}.`:req.state==='CONFIRMED'?`Heartbeat confirmed ${req.armed?'armed':'disarmed'}.`:req.state==='UNKNOWN'?req.detail||'Outcome unknown.':req.state==='CANCELLED'?req.detail||'Request cancelled.':'No arm/disarm request pending.'}catch{$('#arm-request-state').textContent='Control status unavailable.';button.disabled=true}
  }
  function renderGuidedControl(){const b=$('#guided-go');if(!b)return;const v=state.telemetry?.vehicle||{},c=state.telemetry?.connection||{},mode=state.telemetry?.status?.flight_mode,enabled=!v.simulated&&c.state==='connected'&&Number(c.heartbeat_age_s)<=2&&v.autopilot?.value==='ArduPilot'&&mode?.available&&mode.value==='GUIDED',armed=state.telemetry?.status?.armed?.value===true;b.disabled=!enabled;$('#guided-takeoff').disabled=!enabled||!armed;$('#guided-land').disabled=!enabled||!armed;$('#guided-speed').disabled=!enabled;$('#guided-status').textContent=enabled?'Commands require per-operation typed confirmation. ACK means accepted; it does not mean the action completed.':'Requires a fresh real ArduPilot connection reporting GUIDED mode.'}
  const paramTypeNames={1:'UINT8',2:'INT8',3:'UINT16',4:'INT16',5:'UINT32',6:'INT32',7:'UINT64',8:'INT64',9:'REAL32',10:'REAL64',11:'CUSTOM'};
  let parameterRows=[];
  async function loadParameters(){
    try{const q=encodeURIComponent($('#params-search')?.value||''),data=await api('/api/parameters?search='+q);parameterRows=data.parameters||[];
      $('#params-progress').textContent=`${data.progress.received} / ${data.progress.count??'?'} received${data.progress.complete?' · complete':''}${data.progress.error?' · '+data.progress.error:''}`;
      if(document.activeElement?.classList?.contains('param-edit'))return;
      const controlLive=state.telemetry?.connection?.state==='connected'&&!state.telemetry?.vehicle?.simulated&&Number(state.telemetry?.connection?.heartbeat_age_s)<=2;
      $('#params-rows').innerHTML=parameterRows.map(p=>{const key=esc(p.name),edited=$(`#param-edit-${CSS.escape(p.name)}`)?.value??String(p.value),pending=data.pending?.[p.name];const changed=edited!==String(p.value),type=Number(p.type),extended=[7,8,10,11].includes(type),integer=[3,4,5,6].includes(type),exact=!integer||Math.abs(Number(edited))<=2**24,writable=exact,status=pending?.state==='PENDING'?'WAITING FOR RESPONSE':pending?.state==='IN_PROGRESS'?'IN PROGRESS':pending?.state==='CONFIRMED'?'CONFIRMED':pending?.state==='FAILED'||pending?.state==='REJECTED'||pending?.state==='UNKNOWN'?pending.state:!exact?'READ ONLY · FLOAT32 LIMIT':changed?'MODIFIED':extended?'PARAM_EXT':'CURRENT';return `<tr data-param="${key}"><td>${key}</td><td>${paramTypeNames[p.type]||`TYPE ${p.type}`}</td><td>${esc(p.value)}</td><td><input class="param-edit" id="param-edit-${key}" data-name="${key}" value="${esc(edited)}" ${!controlLive?'disabled':''}></td><td>${esc(status)}</td><td><button class="button button-quiet button-sm param-refresh" data-name="${key}" ${controlLive?'':'disabled'}>Refresh</button> <button class="button button-outline button-sm param-apply" data-name="${key}" ${changed&&writable&&controlLive?'':'disabled'}>Apply</button></td></tr>`}).join('')||'<tr><td colspan="6">No parameter values received. Connect a vehicle and request download.</td></tr>';
    }catch(e){$('#params-progress').textContent=e.message}
  }
  async function requestParameterDownload(){try{await api('/api/parameters/request',{method:'POST',body:'{}'});toast('Parameter list requested');setTimeout(loadParameters,250)}catch(e){toast(e.message,'error')}}
  async function loadParameterSnapshots(){try{const r=await api('/api/parameters/snapshots'),options=r.snapshots||[];for(const id of ['snapshot-left','snapshot-right']){const el=$('#'+id),prior=el.value;el.innerHTML='<option value="">Choose snapshot</option>'+options.map(x=>`<option value="${esc(x.id)}">${esc(x.name)} · ${stamp(x.created_at)}</option>`).join('');if(options.some(x=>x.id===prior))el.value=prior}}catch{}}
  let missionItems=[],missionExecution={},missionDirty=false;
  async function loadSimulationControl(){
    if(!state.telemetry?.vehicle?.simulated)return;
    try{const data=await api('/api/simulation/control'),rows=data.control?.transactions||[];$('#sim-control-history').innerHTML=rows.slice(0,12).map(t=>`<div class="sim-control-row"><b>${esc(t.kind)}</b><span class="sim-state-${esc(t.state)}">${esc(t.state)}</span><small>${esc(t.created_at||'')} · ${esc(t.detail||t.result||'')}</small></div>`).join('')||'No simulated control operations yet.'}catch(e){$('#sim-control-history').textContent=e.message}
  }
  async function loadMission(force=false){try{const m=await api('/api/mission');let changed=false;if(m.state==='SUCCEEDED'&&(force||!missionDirty)){missionItems=m.items||[];missionDirty=false;changed=true}missionExecution=m.progress||{};$('#mission-progress').textContent=`${m.state} · ${m.progress?.received??missionItems.length}/${m.progress?.total??missionItems.length}`;$('#mission-execution').textContent=missionExecution.current==null?'Current item unknown':`Current ${missionExecution.current} · reached ${missionExecution.reached?.length||0}`;if(changed||!missionDirty)renderMission()}catch(e){$('#mission-progress').textContent=e.message}}
  function renderMission(){
    $('#mission-rows').innerHTML=missionItems.map((m,i)=>`<tr class="${missionExecution.current===i?'mission-current':''}" data-index="${i}"><td>${i}${missionExecution.reached?.includes(i)?' · REACHED':missionExecution.current===i?' · CURRENT':''}<br><button class="button button-quiet button-sm mission-up" ${i===0?'disabled':''}>↑</button><button class="button button-quiet button-sm mission-down" ${i===missionItems.length-1?'disabled':''}>↓</button></td><td><select class="mission-command" data-field="command"><option value="16" ${m.command==16?'selected':''}>Waypoint</option><option value="17" ${m.command==17?'selected':''}>Loiter unlimited</option><option value="18" ${m.command==18?'selected':''}>Loiter turns</option><option value="19" ${m.command==19?'selected':''}>Loiter time</option><option value="21" ${m.command==21?'selected':''}>Land</option><option value="22" ${m.command==22?'selected':''}>Takeoff</option><option value="82" ${m.command==82?'selected':''}>Spline waypoint</option><option value="20" ${m.command==20?'selected':''}>Return to launch</option><option value="178" ${m.command==178?'selected':''}>Change speed</option><option value="206" ${m.command==206?'selected':''}>Camera trigger distance</option></select><select class="mission-value" data-field="frame"><option value="2" ${m.frame==2?'selected':''}>Mission action (no altitude)</option><option value="6" ${m.frame==6?'selected':''}>Global relative</option><option value="5" ${m.frame==5?'selected':''}>Global AMSL</option></select></td><td><input class="mission-value" data-field="latitude" type="number" step="0.0000001" value="${esc(m.latitude??0)}"></td><td><input class="mission-value" data-field="longitude" type="number" step="0.0000001" value="${esc(m.longitude??0)}"></td><td><input class="mission-value" data-field="altitude" type="number" step="0.1" value="${esc(m.altitude??20)}"></td><td><div class="mission-params">${[1,2,3,4].map(n=>`<label>${({16:['Hold s','Radius m','Pass m','Yaw deg'],17:['Unused','Unused','Radius m','Yaw deg'],18:['Turns','Unused','Radius m','Yaw deg'],19:['Time s','Unused','Radius m','Yaw deg'],20:['Unused','Unused','Unused','Unused'],21:['Abort m','Land mode','Unused','Yaw deg'],22:['Pitch deg','Unused','Unused','Yaw deg'],82:['Hold s','Unused','Unused','Yaw deg'],178:['Speed type','Speed m/s','Throttle %','Relative'],206:['Distance m','Unused','Trigger now','Unused']}[m.command]||['P1','P2','P3','P4'])[n-1]}<input class="mission-value" data-field="param${n}" type="number" step="any" value="${esc(m[`param${n}`]??0)}"></label>`).join('')}</div><button class="button button-quiet button-sm mission-remove">Remove</button></td></tr>`).join('')||'<tr><td colspan="6">No mission items. Add waypoints or download the vehicle mission.</td></tr>';
    const errors=[];missionItems.forEach((m,i)=>{if(!Number.isFinite(Number(m.latitude))||Math.abs(Number(m.latitude))>90)errors.push(`Item ${i}: latitude outside -90..90`);if(!Number.isFinite(Number(m.longitude))||Math.abs(Number(m.longitude))>180)errors.push(`Item ${i}: longitude outside -180..180`);if(!Number.isFinite(Number(m.altitude))||Number(m.altitude)<-1000||Number(m.altitude)>100000)errors.push(`Item ${i}: invalid altitude`);if(![2,5,6].includes(Number(m.frame??6)))errors.push(`Item ${i}: unsupported coordinate frame`)});
    $('#mission-validation').textContent=errors.join(' · ')||`${missionItems.length} items · coordinates and altitudes within editor validation limits`;
    window.dispatchEvent(new Event('mission-edited'));
  }
  let controlRefreshAt=0;
  async function renderModeControl(){
    const select=$('#mode-select');if(!select)return;
    const vehicle=state.telemetry?.vehicle||{},conn=state.telemetry?.connection||{};
    const simulated=!!vehicle.simulated,live=conn.state==='connected'&&!simulated&&Number(conn.heartbeat_age_s)<=2;
    const reported=state.telemetry?.status?.flight_mode;
    $('#mode-reported').textContent=reported?.available?String(reported.value):'Unknown';
    $('#mode-connection-state').textContent=simulated?'SIMULATED · CONTROL DISABLED':live?'LIVE VEHICLE':'READ ONLY';
    if(Date.now()-controlRefreshAt<1200)return;controlRefreshAt=Date.now();
    try{
      const [caps,status]=await Promise.all([api('/api/control/capabilities'),api('/api/control/mode')]);
      const names=caps.capabilities?.modes||[],prior=select.value;
      select.innerHTML='<option value="">Select mode…</option>'+names.map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('');
      if(names.includes(prior))select.value=prior;
      select.disabled=!live||!caps.capabilities?.available||!names.length;
      $('#mode-request').disabled=select.disabled||!select.value;
      const req=status.request||{};
      $('#mode-request-state').textContent=req.state==='PENDING'?`Requested ${req.mode}; waiting for heartbeat confirmation.`:req.state==='CONFIRMED'?`Confirmed by heartbeat: ${req.mode}`:req.state==='UNKNOWN'?req.detail||'Outcome unknown':req.state==='CANCELLED'?req.detail||'Request cancelled':'Only modes reported by the connected firmware are selectable.';
    }catch{select.disabled=true;$('#mode-request').disabled=true;$('#mode-request-state').textContent='Control status unavailable.'}
  }
  function metricCard(label, icon, path, unit, digits, foot) {
    const v=val(path), f=field(path), available=v!==null;
    let value=available?nfmt(v,digits):'—';
    if (path==='status.armed') value=v===null?'—':(v?'ARMED':'DISARMED');
    if (path==='status.flight_mode') value=v===null?'—':String(v);
    let footer=available?`${f.source||'Telemetry'} · ${f.fresh===false?'stale':ago(f.received_at)}`:foot;
    return `<article class="metric-card ${available?'':'unavailable'}"><span class="metric-accent"></span><div class="metric-top"><span>${label}</span><span class="metric-icon">${icon}</span></div><div class="metric-value">${esc(value)} <em>${available?esc(unit):''}</em></div><div class="metric-foot">${esc(footer||'Not reported')}</div></article>`;
  }
  function renderMetrics() {
    $('#metric-grid').innerHTML=[
      metricCard('BATTERY VOLTAGE','ϟ','battery.voltage_v','V',2,'Waiting for battery telemetry'),
      metricCard('RELATIVE ALTITUDE','⌖','position.relative_altitude_m','m',1,'Waiting for valid altitude'),
      metricCard('GROUND SPEED','↗','position.ground_speed_m_s','m/s',1,'Waiting for speed data'),
      metricCard('GPS FIX','⌁','gps.fix_type','',0,'Waiting for GPS receiver')
    ].join('');
    const fix=val('gps.fix_type');
    if(fix!==null){const label=['No GPS','No fix','2D','3D','DGPS','RTK float','RTK fixed'][fix]||`Fix ${fix}`;$('.metric-card:nth-child(4) .metric-value').innerHTML=`${esc(label)} <em>${val('gps.satellites')!==null?`${esc(val('gps.satellites'))} sats`:''}</em>`;$('.metric-card:nth-child(4) .metric-foot').textContent=`${field('gps.fix_type').source||'Telemetry'} · ${ago(field('gps.fix_type').received_at)}`;}
    $('#attitude-source').textContent=field('attitude.roll').available?`${field('attitude.roll').source||'TELEMETRY'} · ${ago(field('attitude.roll').received_at).toUpperCase()}`:'NO SIGNAL';
    $('#att-roll').innerHTML=`${nfmt(val('attitude.roll'),1)}<small>°</small>`;
    $('#att-pitch').innerHTML=`${nfmt(val('attitude.pitch'),1)}<small>°</small>`;
    const yaw=val('attitude.yaw')??val('position.heading_deg');
    $('#att-yaw').innerHTML=`${nfmt(yaw,1)}<small>°</small>`;
    const roll=val('attitude.roll'),pitch=val('attitude.pitch');
    $('#roll-meter').style.width=roll===null?'0%':`${Math.min(100,Math.abs(roll)/60*100)}%`;
    $('#pitch-meter').style.width=pitch===null?'0%':`${Math.min(100,Math.abs(pitch)/45*100)}%`;
    $('#yaw-marker').style.left=yaw===null?'50%':`${((yaw%360+360)%360)/360*100}%`;
    drawHorizon();drawMap();
  }
  function drawHorizon(canvas=$('#horizon'),override=null) {
    if(!canvas)return;
    const rect=canvas.getBoundingClientRect();if(!rect.width)return;
    const dpr=Math.min(2,window.devicePixelRatio||1),w=Math.round(rect.width*dpr),h=Math.round(rect.height*dpr);
    if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
    const ctx=canvas.getContext('2d'),cx=w/2,cy=h/2,r=Math.min(w,h)*.47;
    ctx.clearRect(0,0,w,h);ctx.save();ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.clip();
    const roll=override?override.roll:val('attitude.roll'),pitch=override?override.pitch:val('attitude.pitch');
    if(roll===null||pitch===null||!Number.isFinite(Number(roll))||!Number.isFinite(Number(pitch))){ctx.fillStyle='#131e25';ctx.fillRect(0,0,w,h);ctx.strokeStyle='#2c3a44';ctx.lineWidth=dpr;ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.stroke();ctx.fillStyle='#91a0a6';ctx.font=`${9*dpr}px monospace`;ctx.textAlign='center';ctx.fillText('ATTITUDE',cx,cy-3*dpr);ctx.fillStyle='#63747e';ctx.font=`${8*dpr}px monospace`;ctx.fillText('NO DATA',cx,cy+11*dpr);ctx.restore();return;}
    ctx.fillStyle='#5c96ae';ctx.fillRect(0,0,w,h);ctx.save();
    ctx.translate(cx,cy+pitch*dpr*2.05);ctx.rotate(-roll*Math.PI/180);
    ctx.fillStyle='#826c4c';ctx.fillRect(-w*2,0,w*4,h*2);
    ctx.strokeStyle='#ecede5';ctx.lineWidth=1.35*dpr;ctx.beginPath();ctx.moveTo(-w*2,0);ctx.lineTo(w*2,0);ctx.stroke();
    for(let deg=-30;deg<=30;deg+=10){if(deg===0)continue;let y=deg*dpr*2.05;let len=deg%20===0?w*.13:w*.07;ctx.strokeStyle='#ecede5cc';ctx.lineWidth=dpr;ctx.beginPath();ctx.moveTo(-len,y);ctx.lineTo(len,y);ctx.stroke();if(Math.abs(deg)===20){ctx.fillStyle='#fff';ctx.font=`${8*dpr}px monospace`;ctx.textAlign='center';ctx.fillText(`${Math.abs(deg)}°`,0,y-3*dpr)}}
    ctx.restore();
    // Fixed aircraft reference and bank marks remain upright while the horizon rotates.
    ctx.strokeStyle='#f0df78';ctx.lineWidth=2*dpr;ctx.lineCap='round';ctx.beginPath();ctx.moveTo(cx-w*.19,cy);ctx.lineTo(cx-w*.06,cy+2*dpr);ctx.lineTo(cx,cy+4*dpr);ctx.lineTo(cx+w*.06,cy+2*dpr);ctx.lineTo(cx+w*.19,cy);ctx.stroke();
    ctx.fillStyle='#efdb70';ctx.beginPath();ctx.moveTo(cx,cy-r*.78);ctx.lineTo(cx-4*dpr,cy-r*.94);ctx.lineTo(cx+4*dpr,cy-r*.94);ctx.closePath();ctx.fill();
    ctx.restore();
  }
  function drawMap() {
    const canvas=$('#map-draw'),box=$('#map-canvas');if(!canvas||!box)return;
    const rect=box.getBoundingClientRect(),dpr=Math.min(2,window.devicePixelRatio||1);if(!rect.width)return;
    canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);
    const ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height;
    const pts=state.samples.filter(x=>Number.isFinite(x.latitude_deg)&&Number.isFinite(x.longitude_deg)).slice(-300);
    const current=val('position.latitude_deg')!==null&&val('position.longitude_deg')!==null?{latitude_deg:val('position.latitude_deg'),longitude_deg:val('position.longitude_deg')}:pts.at(-1);
    const empty=$('#map-empty');empty.classList.toggle('hidden',!current);
    $('#map-coords').textContent=current?`${current.latitude_deg.toFixed(6)}°, ${current.longitude_deg.toFixed(6)}°`:'—';
    const fix=val('gps.fix_type'),sats=val('gps.satellites');$('#map-fix').textContent=fix!==null?`GPS · FIX ${fix}${sats!==null?` · ${sats} SAT`:''}`:'GPS · UNAVAILABLE';
    ctx.clearRect(0,0,w,h);if(!current||pts.length<1)return;
    const lats=pts.map(p=>p.latitude_deg),lons=pts.map(p=>p.longitude_deg);lats.push(current.latitude_deg);lons.push(current.longitude_deg);
    const minLat=Math.min(...lats),maxLat=Math.max(...lats),minLon=Math.min(...lons),maxLon=Math.max(...lons);
    const centerLat=state.followMap?current.latitude_deg:(minLat+maxLat)/2,centerLon=state.followMap?current.longitude_deg:(minLon+maxLon)/2;
    const spanLat=Math.max(maxLat-minLat,0.00004),spanLon=Math.max((maxLon-minLon)*Math.cos(centerLat*Math.PI/180),0.00004),pad=30*dpr;
    const scale=Math.min((w-2*pad)/spanLon,(h-2*pad)/spanLat)*state.mapView.zoom;
    const panX=state.followMap?0:state.mapView.panX*dpr,panY=state.followMap?0:state.mapView.panY*dpr;
    const x=p=>w/2+(p.longitude_deg-centerLon)*Math.cos(centerLat*Math.PI/180)*scale+panX,y=p=>h/2-(p.latitude_deg-centerLat)*scale+panY;
    if(pts.length>1){ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(x(p),y(p)):ctx.moveTo(x(p),y(p)));ctx.strokeStyle='#5bd1ba';ctx.lineWidth=2*dpr;ctx.globalAlpha=.86;ctx.stroke();ctx.globalAlpha=1;ctx.beginPath();ctx.arc(x(pts[0]),y(pts[0]),3*dpr,0,Math.PI*2);ctx.fillStyle='#cedaa2';ctx.fill();}
    const px=x(current),py=y(current),heading=val('position.heading_deg')??val('attitude.yaw');
    ctx.save();ctx.translate(px,py);if(heading!==null)ctx.rotate(heading*Math.PI/180);
    ctx.fillStyle='#74e1c7';ctx.strokeStyle='#061812';ctx.lineWidth=dpr;ctx.beginPath();ctx.moveTo(0,-8*dpr);ctx.lineTo(5*dpr,5*dpr);ctx.lineTo(0,3*dpr);ctx.lineTo(-5*dpr,5*dpr);ctx.closePath();ctx.fill();ctx.stroke();ctx.restore();
  }

  function visibleSamples() {
    const now=state.samples.length?Date.parse(state.samples.at(-1).timestamp):Date.now();
    const min=now-state.timeWindow*1000;
    return state.samples.filter(s=>!Number.isFinite(Date.parse(s.timestamp))||Date.parse(s.timestamp)>=min);
  }
  function drawChart(canvas, series, opts={}) {
    if(!canvas)return;const rect=canvas.getBoundingClientRect();if(rect.width<2)return;
    const dpr=Math.min(2,window.devicePixelRatio||1),w=Math.floor(rect.width*dpr),h=Math.floor(rect.height*dpr);
    if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
    const ctx=canvas.getContext('2d');ctx.clearRect(0,0,w,h);
    const css=getComputedStyle(document.documentElement),grid=css.getPropertyValue('--border').trim()||'#24313a',muted=css.getPropertyValue('--faint').trim()||'#687984';
    const left=35*dpr,right=11*dpr,top=8*dpr,bottom=15*dpr,iw=w-left-right,ih=h-top-bottom;
    ctx.font=`${8*dpr}px ${getComputedStyle(document.body).fontFamily}`;ctx.fillStyle=muted;ctx.strokeStyle=grid;ctx.lineWidth=dpr;
    for(let i=0;i<=3;i++){const y=top+ih*i/3;ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w-right,y);ctx.stroke();const label=opts.labels?.[i]??'';ctx.fillText(label,left-30*dpr,y+3*dpr)}
    if(!series.length)return;
    const times=series.map((x,i)=>Number.isFinite(x.t)?x.t:i);const lo=Math.min(...times),hi=Math.max(...times),range=Math.max(hi-lo,.001);
    for(const line of opts.lines||[]){const vals=series.map(x=>Number.isFinite(x[line.key])?x[line.key]:null),present=vals.filter(v=>v!==null);if(!present.length)continue;
      let min=opts.min,max=opts.max;if(min===undefined||max===undefined){min=Math.min(...present);max=Math.max(...present);if(max-min<1){min-=.5;max+=.5}else{let pad=(max-min)*.12;min-=pad;max+=pad}}
      ctx.beginPath();let active=false;vals.forEach((v,i)=>{if(v===null){active=false;return}const px=left+(times[i]-lo)/range*iw,py=top+ih-(v-min)/(max-min||1)*ih;if(!active){ctx.moveTo(px,py);active=true}else ctx.lineTo(px,py)});ctx.strokeStyle=line.color;ctx.lineWidth=1.55*dpr;ctx.lineJoin='round';ctx.lineCap='round';ctx.stroke();
    }
  }
  function renderAllCharts() {
    if(state.paused)return;
    const samples=visibleSamples();
    drawChart($('#chart-attitude'),samples,{lines:[{key:'roll_deg',color:'#57c4b8'},{key:'pitch_deg',color:'#d0a261'}]});
    drawChart($('#chart-altitude'),samples,{lines:[{key:'altitude_m',color:'#80b8e0'}]});
    drawChart($('#chart-battery'),samples,{lines:[{key:'voltage_v',color:'#57c4b8'},{key:'current_a',color:'#679ed9'}]});
    drawChart($('#chart-attitude-big'),samples,{lines:[{key:'roll_deg',color:'#57c4b8'},{key:'pitch_deg',color:'#d0a261'}]});
    drawChart($('#chart-flight'),samples,{lines:[{key:'altitude_m',color:'#65cbb5'},{key:'speed_m_s',color:'#668fca'},{key:'climb_m_s',color:'#c79b63'}]});
    drawChart($('#chart-gps'),samples,{lines:[{key:'gps_fix',color:'#57c4b8'},{key:'gps_satellites',color:'#668fca'}]});
  }
  function renderTelemetryCards() {
    const items=[['ARMED','status.armed',x=>x?'ARMED':'DISARMED'],['FLIGHT MODE','status.flight_mode',x=>x],['ROLL','attitude.roll',x=>`${nfmt(x,1)}°`],['PITCH','attitude.pitch',x=>`${nfmt(x,1)}°`],['YAW','attitude.yaw',x=>`${nfmt(x,1)}°`],['ALTITUDE MSL','position.altitude_msl_m',x=>`${nfmt(x,1)} m`],['RELATIVE ALTITUDE','position.relative_altitude_m',x=>`${nfmt(x,1)} m`],['VERTICAL SPEED','position.vertical_speed_m_s',x=>`${nfmt(x,2)} m/s`],['GROUND SPEED','position.ground_speed_m_s',x=>`${nfmt(x,1)} m/s`],['BATTERY VOLTAGE','battery.voltage_v',x=>`${nfmt(x,2)} V`],['BATTERY CURRENT','battery.current_a',x=>`${nfmt(x,1)} A`],['BATTERY REMAINING','battery.remaining_percent',x=>`${nfmt(x,0)}%`],['GPS FIX','gps.fix_type',x=>`${x} · ${val('gps.satellites')??'—'} sats`],['GPS EPH','gps.eph_m',x=>`${nfmt(x,1)} m`],['RC RSSI','rc.rssi_percent',x=>`${x}%`],['VIBRATION X','vibration.x',x=>`${nfmt(x,1)} m/s²`],['MOTOR OUTPUT','actuators.outputs_us',x=>`${x.slice(0,4).join(' · ')} µs`],['MESSAGE RATE','connection.message_rate_hz',x=>`${nfmt(x,1)} Hz`],['ESTIMATOR','status.estimator',x=>x?.message_type||'Reported']];
    $('#all-telemetry').innerHTML=items.map(([label,path,format])=>{const v=val(path),f=field(path);return `<div class="telemetry-item"><span>${esc(label)}</span><b>${v===null?'—':esc(format(v))}</b><small>${v===null?'Not reported':`${esc(f.source||'Telemetry')} · ${esc(ago(f.received_at))}`}</small></div>`}).join('');
    const health=[['Vehicle status',val('status.armed')===null?'Unavailable':val('status.armed')?'Armed':'Disarmed'],['Flight mode',val('status.flight_mode')??'Unavailable'],['GPS receiver',val('gps.fix_type')===null?'No data':`Fix ${val('gps.fix_type')}`],['Estimator',val('status.estimator')===null?'No report':'Reporting'],['Battery',val('battery.voltage_v')===null?'No data':`${nfmt(val('battery.voltage_v'),2)} V`]];
    $('#system-health').innerHTML=health.map(([a,b])=>`<div class="health-row"><span>${esc(a)}</span><b class="${b==='Unavailable'||b==='No data'?'':b==='Armed'?'warn':'ok'}">${esc(b)}</b></div>`).join('');
    const c=state.telemetry?.connection||{};
    $('#link-stats').innerHTML=[['Message rate',`${nfmt(c.message_rate_hz,1)} Hz`],['Messages',c.messages_total??0],['Heartbeat age',c.heartbeat_age_s===null||c.heartbeat_age_s===undefined?'—':`${nfmt(c.heartbeat_age_s,1)} s`],['Last update',ago(c.last_update)],['System / component',`${val('vehicle.system_id')??'—'} / ${val('vehicle.component_id')??'—'}`]].map(([a,b])=>`<div class="health-row"><span>${esc(a)}</span><b>${esc(b)}</b></div>`).join('');
  }
  function renderEvents() {
    const arr=state.events||[];
    $('#alert-count').textContent=arr.filter(e=>!e.acknowledged).length||'';
    $('#recent-events').innerHTML=arr.slice(0,3).map(e=>`<div class="event-mini sev-${esc(e.severity)}"><i></i><div><b>${esc(e.description||e.type)}</b><small>${esc(stamp(e.started_at))} · ${esc(e.source||'AeroScope')}</small></div></div>`).join('')||'<div class="event-empty">No recent events. Vehicle-reported messages and configured screening rules will appear here.</div>';
    $('#diagnostics-summary').innerHTML=[['ACTIVE ALERTS',arr.filter(e=>!e.ended_at).length],['UNACKNOWLEDGED',arr.filter(e=>!e.acknowledged).length],['AUTOPILOT REPORTED',arr.filter(e=>String(e.source).includes('Autopilot')).length],['AEROSCOPE RULES',arr.filter(e=>String(e.source).includes('AeroScope')).length]].map(([l,v])=>`<div class="diag-stat"><b>${v}</b><span>${l}</span></div>`).join('');
    $('#diagnostic-list').innerHTML=arr.map(e=>`<div class="live-event sev-${esc(e.severity)}"><span class="event-status-icon">${e.ended_at?'✓':'!'}</span><div class="live-event-main"><b>${esc(e.description||e.type)}</b><p>${esc(JSON.stringify(e.evidence||{}))}</p><small>${esc(stamp(e.started_at))} · ${esc(e.source||'AeroScope')}${e.ended_at?` · RECOVERED ${esc(stamp(e.ended_at))}`:''}</small></div><div class="live-event-actions">${e.acknowledged?'<span class="subtle-label">ACK</span>':`<button class="ack-btn" data-ack="${esc(e.id)}">ACKNOWLEDGE</button>`}</div></div>`).join('')||'<div class="empty-analysis" style="height:180px"><b>No events recorded</b><span>Start a simulation to exercise the live alert rules.</span></div>';
  }
  async function loadEvents(){try{const r=await api('/api/events?limit=200');state.events=r.events||[];renderEvents()}catch(e){toast(e.message,'error')}}

  function applyState(data) {
    if(!data)return;
    state.telemetry=data;
    updateConnection();renderMetrics();renderTelemetryCards();renderAllCharts();
    $('#ws-health').textContent=state.wsUp?'Connected':'Reconnecting';$('#ws-health').className=state.wsUp?'ok':'warn';
    $('#backend-health').textContent='Reachable';$('#ulog-health').textContent='pyulog optional';
  }
  let telemetrySocket=null,eventsSocket=null,socketRetry=0,wsTimer=null;
  function openSockets() {
    if(location.protocol==='file:')return;
    const scheme=location.protocol==='https:'?'wss:':'ws:';
    const connect=(path,onData)=>{
      const ws=new WebSocket(`${scheme}//${location.host}${path}`);
      ws.onopen=()=>{socketRetry=0;state.wsUp=true;$('#ws-health').textContent='Connected';$('#ws-health').className='ok';};
      ws.onmessage=e=>{try{const packet=JSON.parse(e.data);if(packet.protocol_version!==1)return;onData(packet.data)}catch{}};
      ws.onclose=()=>{if(path==='/ws/telemetry'){state.wsUp=false;$('#ws-health').textContent='Disconnected';$('#ws-health').className='bad';if(wsTimer)clearTimeout(wsTimer);socketRetry=Math.min(socketRetry+1,8);wsTimer=setTimeout(openSockets,Math.min(1000*2**socketRetry,15000));}};
      ws.onerror=()=>ws.close();return ws;
    };
    telemetrySocket=connect('/ws/telemetry',applyState);
    eventsSocket=connect('/ws/events',data=>{state.events=data.events||[];renderEvents()});
  }
  async function loadState(){try{const r=await api('/api/state');applyState(r.data);if(!state.wsUp){$('#ws-health').textContent='Polling fallback';}}catch(e){$('#backend-health').textContent='Unavailable';$('#backend-health').className='bad';}}
  async function loadSeries(){try{const r=await api('/api/series?limit=2000');state.samples=r.samples||[];renderAllCharts();drawMap()}catch{}}
  async function loadInspector(){try{const r=await api('/api/inspector?limit=500');state.inspector=r.messages||[];renderInspector(r.rates||{},r.total||0)}catch(e){toast(e.message,'error')}}
  function renderInspector(rates,total){
    state.inspectorRates=rates;
    const entries=Object.entries(rates).sort((a,b)=>b[1]-a[1]);
    const top=entries[0];
    $('#inspector-stats').innerHTML=[['TOTAL MESSAGES',total],['ACTIVE TYPES',entries.length],['MOST FREQUENT',top?top[0]:'—'],['MESSAGE RATE',`${nfmt(state.telemetry?.connection?.message_rate_hz,1)} Hz`]].map(([a,b])=>`<div class="inspector-stat"><b>${esc(b)}</b><span>${a}</span></div>`).join('');
    const types=[...new Set(state.inspector.map(x=>x.type))].sort(),select=$('#inspector-type-filter'),prev=select.value;
    select.innerHTML='<option value="">All message types</option>'+types.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');if(types.includes(prev))select.value=prev;
    state.inspectorType=select.value;state.search=$('#inspector-search').value.toLowerCase();
    const filtered=state.inspector.filter(m=>(!state.inspectorType||m.type===state.inspectorType)&&(!state.search||`${m.type} ${JSON.stringify(m.fields)}`.toLowerCase().includes(state.search)));
    $('#inspector-total').textContent=`${filtered.length} MESSAGES`;
    $('#inspector-rows').innerHTML=filtered.map(m=>`<tr><td>${esc(stamp(m.timestamp))}</td><td>${esc(m.type)}</td><td>${m.source_system??'—'} / ${m.source_component??'—'}</td><td>${nfmt(state.inspectorRates[m.type]||0,1)} Hz</td><td>${esc(JSON.stringify(m.fields||{}))}</td></tr>`).join('')||'<tr><td colspan="5">No MAVLink messages yet. Start simulation or connect a vehicle.</td></tr>';
  }

  function openModal(title, kicker, html) {
    $('#modal-title').textContent=title;$('#modal-kicker').textContent=kicker;$('#modal-content').innerHTML=html;$('#modal-backdrop').classList.remove('hidden');
    $('#modal-close').focus();
  }
  function closeModal(){ $('#modal-backdrop').classList.add('hidden');$('#modal-content').replaceChildren(); }
  function showConnect(profile=null) {
    openModal('Connect a vehicle','READ-ONLY MAVLINK CONNECTION',`<p class="modal-description">AeroScope listens for telemetry from one active vehicle. Connection setup does not send flight commands or change stream rates.</p><form id="connection-form" class="modal-form"><label>Transport<select name="kind" id="connection-kind"><option value="udp_listener">UDP listener</option><option value="serial">Serial connection</option><option value="tcp">TCP client</option></select></label><div id="connection-fields"></div><div class="notice-box"><b>UDP listener semantics</b><span>ArduPilot or PX4 must send its telemetry stream to this computer's bind address and UDP port. Default: 0.0.0.0:14550.</span></div><div class="modal-actions"><button class="button button-quiet" type="button" id="cancel-modal">Cancel</button><button class="button button-primary">Connect</button></div></form>`);
    const kind=$('#connection-kind');if(profile?.connection?.kind)kind.value=profile.connection.kind;
    const draw=()=>{const k=kind.value,c=profile?.connection||{};$('#connection-fields').innerHTML=k==='udp_listener'?`<div class="form-row"><label>Bind address<input name="bind_address" value="${esc(c.bind_address||'0.0.0.0')}" required></label><label>UDP port<input name="port" type="number" min="1" max="65535" value="${esc(c.port||14550)}" required></label></div>`:k==='serial'?`<div class="form-row"><label>Serial port<input name="port" value="${esc(c.port||'')}" placeholder="COM4 or /dev/ttyUSB0" required></label><label>Baud rate<select name="baud"><option ${Number(c.baud||115200)===115200?'selected':''}>115200</option><option>57600</option><option>921600</option><option>230400</option><option>460800</option><option>38400</option><option>19200</option><option>9600</option></select></label></div>`:`<div class="form-row"><label>Host<input name="host" value="${esc(c.host||'127.0.0.1')}" required></label><label>TCP port<input name="port" type="number" min="1" max="65535" value="${esc(c.port||5760)}" required></label></div><p class="modal-description">The TCP connection is initiated by AeroScope to a MAVLink TCP server.</p>`};kind.addEventListener('change',draw);draw();$('#cancel-modal').onclick=closeModal;
    $('#connection-form').onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.currentTarget),cfg=Object.fromEntries(fd);cfg.kind=kind.value;if(cfg.port)cfg.port=Number(cfg.port);if(cfg.baud)cfg.baud=Number(cfg.baud);if(profile?.expected_autopilot)cfg.expected_autopilot=profile.expected_autopilot;try{await api('/api/connection',{method:'POST',body:JSON.stringify(cfg)});closeModal();toast('MAVLink connection started. Waiting for heartbeat.','success');await loadState()}catch(err){toast(err.message,'error')}};
  }
  function showSimulation() {
    const scenarios=[['normal','Normal flight','Stable telemetry and a steady flight profile.'],['gps_degradation','GPS degradation','Fix quality falls after the scenario begins.'],['battery_sag','Battery voltage sag','Voltage drops during higher simulated current.'],['elevated_vibration','Elevated vibration','VIBE values exceed the configured screening level.'],['heartbeat_loss','Heartbeat loss','Heartbeat stops after eight scenario seconds.'],['ekf_warning','Estimator warning','A simulated estimator status warning is reported.'],['rc_degradation','RC signal degradation','Reported RC signal level falls to zero.']];
    openModal('Start a simulation','SYNTHETIC TELEMETRY · CLEARLY LABELLED',`<p class="modal-description">Scenario values are generated locally by the backend and marked as simulated. They are not aircraft measurements.</p><div class="scenario-grid">${scenarios.map((s,i)=>`<button class="scenario-option ${i===0?'selected':''}" type="button" data-scenario="${s[0]}"><b>${s[1]}</b><small>${s[2]}</small></button>`).join('')}</div><label style="margin-top:12px">Simulation speed<select id="simulation-speed"><option value="1">1× · real time</option><option value="2">2×</option><option value="0.5">0.5×</option></select></label><div class="modal-actions" style="margin-top:15px"><button class="button button-quiet" id="cancel-modal" type="button">Cancel</button><button class="button button-primary" id="start-simulation" type="button">▶ Start scenario</button></div>`);
    let scenario='normal';$$('.scenario-option').forEach(b=>b.onclick=()=>{$$('.scenario-option').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');scenario=b.dataset.scenario});$('#cancel-modal').onclick=closeModal;
    $('#start-simulation').onclick=async()=>{try{await api('/api/simulation',{method:'POST',body:JSON.stringify({action:'start',scenario,speed:Number($('#simulation-speed').value)})});closeModal();toast(`Simulation started · ${scenarios.find(s=>s[0]===scenario)[1]}`,'success');await loadState();await loadSeries()}catch(e){toast(e.message,'error')}};
  }
  function renderConnectionMessage() { const msg=state.telemetry?.connection?.message;if(msg&&state.telemetry.connection.state==='error')toast(msg,'error'); }

  async function loadAnalyses() {
    try{const r=await api('/api/logs');state.analyses=r.analyses||[];$('#log-count').textContent=state.analyses.length||'';$('#analysis-total').textContent=`${state.analyses.length} LOG${state.analyses.length===1?'':'S'}`;
      $('#analysis-list').innerHTML=state.analyses.map(a=>`<button class="analysis-row ${state.selectedAnalysis===a.id?'selected':''}" data-analysis="${esc(a.id)}"><span class="analysis-file-icon">${a.format==='ulog'?'UL':'BIN'}</span><span style="min-width:0"><b>${esc(a.original_name)}</b><small>${esc(new Date(a.created_at).toLocaleString())} · ${(a.size_bytes/1048576).toFixed(1)} MiB</small></span><span class="analysis-status ${esc(a.status)}">${esc(a.status.toUpperCase())}</span></button>`).join('')||'<div class="event-empty">No analyses yet.</div>';
      $$('.analysis-row').forEach(b=>b.onclick=()=>selectAnalysis(b.dataset.analysis));
      const recording=r.recordings||[];if(recording.length){$('#analysis-total').textContent+=` · ${recording.length} RECORDING${recording.length===1?'':'S'}`;}
    }catch(e){toast(e.message,'error')}
  }
  async function selectAnalysis(id) {
    state.selectedAnalysis=id;await loadAnalyses();
    try{const item=await api(`/api/analysis/${encodeURIComponent(id)}`);if(item.status==='queued'){renderAnalysisPending(item);setTimeout(()=>selectAnalysis(id),900);return;}if(item.status==='error'){renderAnalysisError(item);return;}renderAnalysis(item)}catch(e){toast(e.message,'error')}
  }
  function renderAnalysisPending(item){$('#analysis-detail').innerHTML=`<div class="empty-analysis"><div class="progress-track" style="width:160px"><i style="width:50%"></i></div><b>Parsing flight log…</b><span>${esc(item.original_name)} · bounded background analysis</span></div>`}
  function renderAnalysisError(item){$('#analysis-detail').innerHTML=`<div class="empty-analysis"><div class="empty-icon">⚠</div><b>Analysis could not be completed</b><span>${esc(item.error||'The log parser returned an error.')}</span><button class="button button-quiet button-sm" id="delete-analysis">Delete uploaded file</button></div>`;$('#delete-analysis').onclick=()=>deleteAnalysis(item.id)}
  const activeReplay=()=>state.activeAnalysis?.result;
  function renderAnalysis(item) {
    const result=item.result||{},s=result.summary||{},samples=result.series||[],events=result.events||[],findings=result.findings||[];
    state.activeAnalysis=item;state.replayTime=0;state.replayPlaying=false;clearInterval(state.replayTimer);
    const duration=Number(s.duration_s)||0;
    const metric=(label,value,unit,ref)=>`<div class="report-metric"><span>${label}</span><b>${value??'—'} ${value===null||value===undefined?'':unit||''}</b><small>${esc(ref||'')}</small></div>`;
    $('#analysis-detail').innerHTML=`<div class="analysis-report"><div class="report-header"><div><div class="panel-kicker">${esc(item.format==='ulog'?'PX4 ULOG':'ARDUPILOT DATAFLASH BIN')} · ${s.record_count||0} RECORDS</div><h2>${esc(item.original_name)}</h2><p>${duration?`${nfmt(duration,1)} s duration`: 'Duration unavailable'} · ${esc((s.data_fields||[]).join(' · ')||'No recognized fields')}</p></div><div class="heading-actions"><button class="button button-quiet button-sm" id="delete-analysis">Delete log</button></div></div><div class="report-metrics">${metric('MAX ALTITUDE',s.max_relative_altitude_m??s.max_altitude_m,'m',s.max_relative_altitude_reference||s.max_altitude_reference)}${metric('MAX SPEED',s.max_speed_m_s,'m/s',s.max_speed_reference)}${metric('MAX CURRENT',s.max_current_a,'A','recorded')} ${metric('MIN VOLTAGE',s.min_voltage_v,'V','recorded')}</div><section class="panel"><div class="panel-head"><div><div class="panel-kicker">FLIGHT PROFILE</div><h2 id="replay-chart-title">Synchronized replay</h2></div><div class="heading-actions"><select class="select-control" id="replay-series" aria-label="Select replay graph"><option value="attitude">Actual / desired attitude</option><option value="altitude">Altitude</option><option value="speed">Ground / vertical speed</option><option value="voltage">Battery voltage</option><option value="current">Battery current</option><option value="gps_fix">GPS fix</option><option value="gps_satellites">GPS satellites</option><option value="vibration">Vibration axes</option><option value="rc_channel_1">RC input channel 1</option><option value="actuator_1">Actuator output channel 1</option></select><span class="data-source">REPLAY ? READ-ONLY</span></div></div><canvas class="line-chart report-chart" id="replay-chart"></canvas><div class="replay-attitude-row"><div class="panel-kicker">ATTITUDE AT REPLAY CURSOR</div><div class="replay-attitude-content"><div class="horizon-wrap"><canvas id="replay-horizon" width="240" height="240" aria-label="Replayed artificial horizon"></canvas><div class="horizon-center">+</div></div><div><span>ROLL</span><b id="replay-roll">?</b><span>PITCH</span><b id="replay-pitch">?</b><span>YAW</span><b id="replay-yaw">?</b></div></div></div><div class="timeline" id="replay-timeline"><div class="timeline-fill" id="timeline-fill"></div><div class="timeline-marker" id="timeline-marker"></div>${events.filter(e=>Number.isFinite(e.t)&&duration>0).map(e=>`<i class="timeline-event" title="${esc(e.description)}" data-event-time="${e.t}" style="left:${Math.max(0,Math.min(100,e.t/duration*100))}%"></i>`).join('')}</div><div class="replay-controls"><button class="button button-outline button-sm" id="replay-play">▶ Play</button><button class="button button-quiet button-sm" id="replay-step">▸|</button><input type="range" min="0" max="1000" value="0" id="replay-seek" aria-label="Seek replay time"><span class="playback-time" id="replay-time">00:00.0 / ${formatDuration(duration)}</span><select class="select-control" id="replay-speed"><option value="1">1×</option><option value="0.5">0.5×</option><option value="2">2×</option><option value="4">4×</option></select></div><div class="map-footer"><span>GPS TRACE · ${s.gps_path_points||0} VALID POINTS</span><span>${esc(result.parser?.name||'')}</span></div><div class="map-canvas log-map" id="replay-map"><canvas id="replay-map-draw"></canvas><div class="map-empty" id="replay-map-empty"><span class="map-cross">⌖</span><b>GPS path unavailable</b><small>This log has no valid position records.</small></div></div></section><section class="panel"><div class="panel-head"><div><div class="panel-kicker">DETERMINISTIC ANALYSIS</div><h2>Findings · ${findings.length}</h2></div><span class="data-source">RULE BASED</span></div><div>${findings.map(f=>`<article class="finding-card"><div class="finding-head"><span class="severity-pill ${esc(f.severity)}">${esc(f.severity)}</span><b>${esc(f.description)}</b><button class="button button-quiet button-sm jump-finding" data-jump="${Number(f.start_s)||0}">Go to interval →</button></div><p>${esc(JSON.stringify(f.evidence||{}))} · ${esc(f.method||'')}</p><details><summary>Possible explanations, next steps & limits</summary><ul>${(f.possible_explanations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}<li>Recommended: ${esc(f.recommended_investigation||'Review source log')}</li><li>Limit: ${esc(f.data_limitations||'')}</li></ul></details></article>`).join('')||'<div class="event-empty" style="padding:12px">No findings were generated from the fields available in this log.</div>'}</div></section><section class="panel"><div class="panel-head"><div><div class="panel-kicker">RECORDED & ANALYTICAL EVENTS</div><h2>Flight timeline · ${events.length}</h2></div></div><div class="diagnostic-list">${events.map(e=>`<button class="live-event ${e.recorded?'':'sev-medium'} jump-event" data-jump="${Number(e.t)||0}" style="text-align:left;color:inherit;background:transparent"><span class="event-status-icon">${e.recorded?'●':'◇'}</span><span class="live-event-main"><b>${esc(e.description)}</b><p>${esc(e.source==='recorded'?'Recorded in log':'Generated by AeroScope analysis')} · ${formatDuration(Number(e.t)||0)}</p></span><span class="severity-pill ${esc(e.severity)}">${esc(e.severity)}</span></button>`).join('')||'<div class="event-empty">No timeline events were found.</div>'}</div></section><section class="panel"><div class="panel-head"><div><div class="panel-kicker">COVERAGE</div><h2>Data limitations</h2></div></div><ul class="limitations">${(result.data_limitations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></section></div>`;
    $('#delete-analysis').onclick=()=>deleteAnalysis(item.id);
    const slider=$('#replay-seek'),play=$('#replay-play');
    const seek=seconds=>{state.replayTime=Math.max(0,Math.min(duration,seconds));slider.value=duration?String(state.replayTime/duration*1000):'0';$('#timeline-marker').style.left=`${duration?state.replayTime/duration*100:0}%`;$('#timeline-fill').style.width=`${duration?state.replayTime/duration*100:0}%`;$('#replay-time').textContent=`${formatDuration(state.replayTime)} / ${formatDuration(duration)}`;renderReplayFrame(samples,events,state.replayTime)};
    slider.oninput=()=>{pauseReplay();seek(Number(slider.value)/1000*duration)};
    play.onclick=()=>{if(state.replayPlaying){pauseReplay();return}state.replayPlaying=true;play.textContent='Ⅱ Pause';state.replayTimer=setInterval(()=>{const next=state.replayTime+.1*Number($('#replay-speed').value);if(next>=duration){seek(duration);pauseReplay()}else seek(next)},100)};
    $('#replay-step').onclick=()=>{pauseReplay();const next=samples.find(x=>x.t>state.replayTime);seek(next?.t??duration)};
    $('#replay-timeline').onclick=e=>{if(e.target.classList.contains('timeline-event')){e.stopPropagation();seek(Number(e.target.dataset.eventTime));return}const r=e.currentTarget.getBoundingClientRect();seek((e.clientX-r.left)/r.width*duration)};
    $$('.jump-finding,.jump-event').forEach(b=>b.onclick=()=>{pauseReplay();seek(Number(b.dataset.jump)||0)});
    seek(0);renderReplayFrame(samples,events,0);
  }
  function pauseReplay(){state.replayPlaying=false;clearInterval(state.replayTimer);const b=$('#replay-play');if(b)b.textContent='▶ Play'}
  function formatDuration(v){if(!Number.isFinite(v))return'—';const m=Math.floor(v/60),s=v%60;return `${String(m).padStart(2,'0')}:${s.toFixed(1).padStart(4,'0')}`}
  function replayGraphConfig(kind){
    const configs={
      attitude:{title:'Actual / desired attitude · °',lines:[{key:'roll_deg',color:'#57c4b8'},{key:'pitch_deg',color:'#d0a261'},{key:'desired_roll_deg',color:'#8cd9cf'},{key:'desired_pitch_deg',color:'#e2bd82'}]},
      altitude:{title:'Altitude · m',lines:[{key:'relative_altitude_m',color:'#57c4b8'},{key:'altitude_msl_m',color:'#668fca'}]},
      speed:{title:'Ground / vertical speed · m/s',lines:[{key:'speed_m_s',color:'#57c4b8'},{key:'climb_m_s',color:'#d0a261'}]},
      voltage:{title:'Battery voltage · V',lines:[{key:'voltage_v',color:'#57c4b8'}]},
      current:{title:'Battery current · A',lines:[{key:'current_a',color:'#668fca'}]},
      gps_fix:{title:'GPS fix type',lines:[{key:'gps_fix',color:'#57c4b8'}]},
      gps_satellites:{title:'GPS satellites · count',lines:[{key:'satellites',color:'#668fca'}]},
      vibration:{title:'Vibration axes · source units',lines:[{key:'vibe_x',color:'#57c4b8'},{key:'vibe_y',color:'#d0a261'},{key:'vibe_z',color:'#668fca'}]},
      rc_channel_1:{title:'RC input · channel 1 · source units',lines:[{key:'rc_channel_1',color:'#57c4b8'}]},
      actuator_1:{title:'Actuator output · channel 1 · source units',lines:[{key:'actuator_1',color:'#d0a261'}]}
    };
    return configs[kind]||configs.attitude;
  }
  function renderReplayFrame(samples,events,time){
    const before=samples.filter(x=>Number(x.t)<=time);const current=before.at(-1)||samples[0];
    const replayRoll=Number.isFinite(current?.roll_deg)?current.roll_deg:null,replayPitch=Number.isFinite(current?.pitch_deg)?current.pitch_deg:null,replayYaw=Number.isFinite(current?.yaw_deg)?current.yaw_deg:null;
    if($('#replay-horizon')){drawHorizon($('#replay-horizon'),{roll:replayRoll,pitch:replayPitch});$('#replay-roll').textContent=replayRoll===null?'—':`${nfmt(replayRoll,1)}°`;$('#replay-pitch').textContent=replayPitch===null?'—':`${nfmt(replayPitch,1)}°`;$('#replay-yaw').textContent=replayYaw===null?'—':`${nfmt(replayYaw,1)}°`;}
    const chart=$('#replay-chart');if(chart){const config=replayGraphConfig($('#replay-series')?.value);$('#replay-chart-title').textContent=config.title;const chartRows=samples.map(s=>({...s,rc_channel_1:s.rc_inputs?.[0],actuator_1:s.actuator_outputs?.[0]}));drawChart(chart,chartRows,{lines:config.lines});const ctx=chart.getContext('2d'),dpr=Math.min(2,devicePixelRatio||1),w=chart.width,left=35*dpr,right=11*dpr,all=samples.map(x=>x.t),lo=Math.min(...all,0),hi=Math.max(...all,1);const x=left+(time-lo)/(hi-lo)*(w-left-right);ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,chart.height);ctx.strokeStyle='#eee17a';ctx.lineWidth=dpr;ctx.stroke();}
    const map=$('#replay-map-draw'),box=$('#replay-map');if(map&&box){const rect=box.getBoundingClientRect(),dpr=Math.min(2,devicePixelRatio||1);map.width=Math.round(rect.width*dpr);map.height=Math.round(rect.height*dpr);const ctx=map.getContext('2d'),pts=samples.filter(x=>Number.isFinite(x.latitude_deg)&&Number.isFinite(x.longitude_deg)),upto=pts.filter(x=>x.t<=time);$('#replay-map-empty').classList.toggle('hidden',pts.length>0);ctx.clearRect(0,0,map.width,map.height);if(pts.length){const minLat=Math.min(...pts.map(x=>x.latitude_deg)),maxLat=Math.max(...pts.map(x=>x.latitude_deg)),minLon=Math.min(...pts.map(x=>x.longitude_deg)),maxLon=Math.max(...pts.map(x=>x.longitude_deg)),pad=28*dpr;const xx=p=>pad+(p.longitude_deg-minLon)/Math.max(.00004,maxLon-minLon)*(map.width-pad*2),yy=p=>map.height-pad-(p.latitude_deg-minLat)/Math.max(.00004,maxLat-minLat)*(map.height-pad*2);ctx.beginPath();upto.forEach((p,i)=>i?ctx.lineTo(xx(p),yy(p)):ctx.moveTo(xx(p),yy(p)));ctx.strokeStyle='#63d7be';ctx.lineWidth=2*dpr;ctx.stroke();if(current&&Number.isFinite(current.latitude_deg)){ctx.beginPath();ctx.arc(xx(current),yy(current),4*dpr,0,Math.PI*2);ctx.fillStyle='#e6d66d';ctx.fill()}}}
  }
  async function deleteAnalysis(id){if(!confirm('Delete this log file and its analysis metadata?'))return;try{await api(`/api/analysis/${encodeURIComponent(id)}`,{method:'DELETE'});state.selectedAnalysis=null;state.activeAnalysis=null;$('#analysis-detail').innerHTML='<div class="empty-analysis"><b>Select a flight record</b><span>Choose another analysis or upload a log.</span></div>';await loadAnalyses();toast('Flight log deleted','success')}catch(e){toast(e.message,'error')}}
  function uploadFile(file){if(!file)return;const progress=$('#upload-progress'),bar=$('#upload-progress-bar'),label=$('#upload-progress-label');progress.classList.remove('hidden');label.textContent='Uploading…';bar.style.width='0%';const form=new FormData();form.append('file',file,file.name);const xhr=new XMLHttpRequest();xhr.open('POST','/api/logs/upload');xhr.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);bar.style.width=p+'%';label.textContent=`Uploading · ${p}%`}};xhr.onload=async()=>{let data;try{data=JSON.parse(xhr.responseText)}catch{data={error:'Invalid backend response'}}if(xhr.status<200||xhr.status>=300){toast(data.error||'Upload failed','error');progress.classList.add('hidden');return}label.textContent='Parsing in background…';bar.style.width='100%';await loadAnalyses();selectAnalysis(data.id);setTimeout(()=>progress.classList.add('hidden'),700)};xhr.onerror=()=>{toast('Upload failed. Check that the local backend is running.','error');progress.classList.add('hidden')};xhr.send(form)}

  async function loadProfiles(){try{const r=await api('/api/profiles');state.profiles=r.profiles||[];renderProfiles()}catch(e){toast(e.message,'error')}}
  function renderProfiles(){
    const form=$('#profile-form');$('#profile-list').innerHTML=state.profiles.map(p=>`<article class="profile-card"><h3>${esc(p.name)}</h3><p>${esc([p.vehicle_type,p.expected_autopilot].filter(Boolean).join(' · ')||'Vehicle type and autopilot not specified')}</p><div class="profile-tags"><span class="profile-tag">${esc(p.connection?.kind||'UDP listener')}</span><span class="profile-tag">${esc(p.telemetry_profile||'standard')}</span></div><div class="profile-actions"><button class="button button-primary button-sm" data-connect-profile="${esc(p.id)}">Connect</button><button class="button button-outline button-sm" data-edit-profile="${esc(p.id)}">Edit profile</button><button class="button button-quiet button-sm" data-delete-profile="${esc(p.id)}">Delete</button></div></article>`).join('')||'<div class="empty-analysis" style="height:160px"><b>No saved vehicle profiles</b><span>Create a local profile for preferred connection settings and notes.</span></div>';
    $$('[data-connect-profile]').forEach(b=>b.onclick=()=>showConnect(state.profiles.find(p=>p.id===b.dataset.connectProfile)));
    $$('[data-edit-profile]').forEach(b=>b.onclick=()=>editProfile(state.profiles.find(p=>p.id===b.dataset.editProfile)));
    $$('[data-delete-profile]').forEach(b=>b.onclick=async()=>{try{await api(`/api/profiles/${b.dataset.deleteProfile}`,{method:'DELETE'});await loadProfiles()}catch(e){toast(e.message,'error')}});
    if(state.profileEdit)form.classList.remove('hidden');
  }
  function showProfile(profile=null){state.profileEdit=profile?{...profile}:{};const f=$('#profile-form');f.reset();f.classList.remove('hidden');$('#profile-form-title').textContent=profile?'Edit vehicle profile':'New vehicle profile';f.elements.id.value=profile?.id||'';f.elements.name.value=profile?.name||'';f.elements.vehicle_type.value=profile?.vehicle_type||'';f.elements.expected_autopilot.value=profile?.expected_autopilot||'';f.elements.notes.value=profile?.notes||'';const kind=profile?.connection?.kind||'udp_listener';f.elements.kind.value=kind;renderProfileConnectionFields(profile?.connection||{});f.scrollIntoView({behavior:'smooth',block:'center'})}
  function renderProfileConnectionFields(conn={}){let el=$('#profile-connection-fields');if(!el){el=document.createElement('div');el.id='profile-connection-fields';$('#profile-form').elements.kind.closest('label').after(el)}const kind=$('#profile-form').elements.kind.value;el.innerHTML=kind==='udp_listener'?`<div class="form-row"><label>Bind address<input name="bind_address" value="${esc(conn.bind_address||'0.0.0.0')}"></label><label>UDP port<input name="port" type="number" value="${esc(conn.port||14550)}"></label></div>`:kind==='serial'?`<div class="form-row"><label>Serial port<input name="port" value="${esc(conn.port||'')}" placeholder="COM4"></label><label>Baud<input name="baud" type="number" value="${esc(conn.baud||115200)}"></label></div>`:`<div class="form-row"><label>TCP host<input name="host" value="${esc(conn.host||'127.0.0.1')}"></label><label>TCP port<input name="port" type="number" value="${esc(conn.port||5760)}"></label></div>`}
  function editProfile(p){if(p){showProfile(p)}}
  function bindProfileForm(){
    $('#new-profile').onclick=()=>showProfile();$('#close-profile').onclick=$('#cancel-profile').onclick=()=>{state.profileEdit=null;$('#profile-form').classList.add('hidden')};$('#profile-form').elements.kind.onchange=()=>renderProfileConnectionFields();
    $('#profile-form').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,kind=f.elements.kind.value;const connection={kind};if(kind==='udp_listener'){connection.bind_address=f.elements.bind_address.value;connection.port=Number(f.elements.port.value)}else if(kind==='serial'){connection.port=f.elements.port.value;connection.baud=Number(f.elements.baud.value)}else{connection.host=f.elements.host.value;connection.port=Number(f.elements.port.value)}const body={id:f.elements.id.value||undefined,name:f.elements.name.value,vehicle_type:f.elements.vehicle_type.value||null,expected_autopilot:f.elements.expected_autopilot.value||null,connection,telemetry_profile:'standard',notes:f.elements.notes.value};try{await api('/api/profiles',{method:'POST',body:JSON.stringify(body)});state.profileEdit=null;f.classList.add('hidden');await loadProfiles();toast('Vehicle profile saved locally','success')}catch(err){toast(err.message,'error')}};
  }
  async function loadSettings(){try{const r=await api('/api/settings');const s=r.settings||{};state.theme=s.theme||'dark';document.body.classList.toggle('light-theme',state.theme==='light');$('#setting-theme').value=state.theme;$('#setting-telemetry').value=s.telemetry_profile||'standard'}catch{}}

  function bindUI(){
    $('#open-connect').onclick=$('#banner-connect').onclick=showConnect;$('#open-sim').onclick=showSimulation;
    $('#modal-close').onclick=closeModal;$('#modal-backdrop').addEventListener('click',e=>{if(e.target.id==='modal-backdrop')closeModal()});document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});
    $('#theme-toggle').onclick=()=>{state.theme=state.theme==='dark'?'light':'dark';document.body.classList.toggle('light-theme',state.theme==='light');api('/api/settings',{method:'POST',body:JSON.stringify({theme:state.theme})}).catch(()=>{})};
    $('#record-toggle').onclick=async()=>{try{const r=await api('/api/recording');if(r.recording){await api('/api/recording/stop',{method:'POST',body:'{}'});toast('Recording stopped and saved locally','success')}else{await api('/api/recording/start',{method:'POST',body:JSON.stringify({name:'Telemetry recording'})});toast('Local telemetry recording started','success')}updateConnection()}catch(e){toast(e.message,'error')}};
    $('#mode-select').onchange=()=>$('#mode-request').disabled=!$('#mode-select').value||$('#mode-select').disabled;
    $('#mode-request').onclick=async()=>{const mode=$('#mode-select').value;if(!mode||!window.confirm(`Request flight mode ${mode}? The vehicle may change behavior. Confirm only if this is intentional.`))return;try{await api('/api/control/mode',{method:'POST',body:JSON.stringify({mode,confirmed:true})});toast(`Mode ${mode} requested; awaiting heartbeat confirmation`);renderModeControl()}catch(e){toast(e.message,'error')}};
    $('#params-download').onclick=requestParameterDownload;$('#params-refresh-all').onclick=requestParameterDownload;
    $('#params-save-snapshot').onclick=async()=>{const name=window.prompt('Name this parameter snapshot:');if(name===null)return;try{await api('/api/parameters/snapshot',{method:'POST',body:JSON.stringify({name})});toast('Parameter snapshot saved locally','success');loadParameterSnapshots()}catch(e){toast(e.message,'error')}};
    $('#params-export-json').onclick=()=>{location.href='/api/parameters/export?format=json'};$('#params-export-csv').onclick=()=>{location.href='/api/parameters/export?format=csv'};
    $('#snapshot-compare').onclick=async()=>{const left=$('#snapshot-left').value,right=$('#snapshot-right').value;if(!left||!right){toast('Choose both snapshots','error');return}try{const d=await api(`/api/parameters/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);$('#snapshot-diff').textContent=d.changes.length?d.changes.map(x=>`${x.name}: ${x.before??'—'} → ${x.after??'—'} (${x.change})`).join(' · '):'Snapshots have no differences.'}catch(e){toast(e.message,'error')}};
    $('#params-search').oninput=()=>loadParameters();
    $('#params-rows').oninput=e=>{if(e.target.matches('.param-edit')){const row=e.target.closest('tr'),base=parameterRows.find(p=>p.name===e.target.dataset.name),type=Number(base?.type),same=e.target.value===String(base?.value),exact=![3,4,5,6].includes(type)||Math.abs(Number(e.target.value))<=2**24,live=state.telemetry?.connection?.state==='connected'&&!state.telemetry?.vehicle?.simulated&&Number(state.telemetry?.connection?.heartbeat_age_s)<=2;row.querySelector('td:nth-child(5)').textContent=!exact?'READ ONLY · FLOAT32 LIMIT':same?([7,8,10,11].includes(type)?'PARAM_EXT':'CURRENT'):'MODIFIED';row.querySelector('.param-apply').disabled=same||!exact||!live}};
    $('#params-rows').onclick=async e=>{const name=e.target.dataset.name;if(e.target.matches('.param-refresh')){try{await api('/api/parameters/refresh',{method:'POST',body:JSON.stringify({name})});toast(`${name} refresh requested`)}catch(err){toast(err.message,'error')}}if(e.target.matches('.param-apply')){const input=$(`#param-edit-${CSS.escape(name)}`),base=parameterRows.find(p=>p.name===name),type=Number(base?.type),value=type===11||[7,8].includes(type)?input.value:Number(input.value),sensitive=/(FAIL|ARM|BATT|GPS|EKF|RC|MOT|NAV|WPNAV|ATC_)/i.test(name);if(sensitive&&!window.confirm(`Change safety-related parameter ${name} from ${base.value} to ${input.value}?`))return;try{await api('/api/parameters/set',{method:'POST',body:JSON.stringify({name,value,confirmed:sensitive})});toast(`${name} write sent; waiting for typed parameter acknowledgement`);setTimeout(loadParameters,500)}catch(err){toast(err.message,'error')}}};
    $('#mission-add').onclick=()=>{missionItems.push({command:16,frame:6,latitude:0,longitude:0,altitude:20,param1:0,param2:0,param3:0,param4:0});missionDirty=true;renderMission()};
    $('#mission-rows').oninput=e=>{const row=e.target.closest('tr');if(!row)return;const item=missionItems[Number(row.dataset.index)],field=e.target.dataset.field;if(!item||!field)return;item[field]=Number(e.target.value);missionDirty=true;const invalid=!Number.isFinite(item.latitude)||Math.abs(item.latitude)>90||!Number.isFinite(item.longitude)||Math.abs(item.longitude)>180||!Number.isFinite(item.altitude)||item.altitude< -1000||item.altitude>100000;$('#mission-validation').textContent=invalid?`Item ${row.dataset.index}: invalid coordinate or altitude`: `${missionItems.length} items · local edits not uploaded`};
    $('#mission-rows').onchange=e=>{const row=e.target.closest('tr');if(!row)return;const item=missionItems[Number(row.dataset.index)],field=e.target.dataset.field;if(item&&field){item[field]=Number(e.target.value);missionDirty=true;renderMission()}};
    $('#mission-rows').onclick=e=>{const row=e.target.closest('tr');if(!row)return;const index=Number(row.dataset.index);if(e.target.matches('.mission-remove'))missionItems.splice(index,1);else if(e.target.matches('.mission-up')&&index>0)[missionItems[index-1],missionItems[index]]=[missionItems[index],missionItems[index-1]];else if(e.target.matches('.mission-down')&&index<missionItems.length-1)[missionItems[index+1],missionItems[index]]=[missionItems[index],missionItems[index+1]];else return;missionDirty=true;renderMission()};
    $('#mission-download').onclick=async()=>{if(missionDirty&&!window.confirm('Discard local unsaved mission edits and download the vehicle plan?'))return;missionDirty=false;$('#mission-progress').textContent='DOWNLOADING';try{await api('/api/mission/download',{method:'POST',body:'{}'});toast('Mission download requested');setTimeout(()=>loadMission(true),300)}catch(e){toast(e.message,'error')}};
    $('#mission-upload').onclick=async()=>{if(!missionItems.length){toast('Mission is empty','error');return}if($('#mission-validation').textContent.includes('Item ')){toast('Fix mission validation errors before upload','error');return}const summary=missionItems.map((m,i)=>`${i+1}. cmd ${m.command}, ${m.latitude}, ${m.longitude}, alt ${m.altitude} m`).join('\n');if(!window.confirm(`Review complete mission (${missionItems.length} items):\n${summary}\n\nUpload only? This will not start the mission.`)||window.prompt('Type UPLOAD to confirm replacing the onboard mission.')!=='UPLOAD')return;try{$('#mission-progress').textContent='UPLOADING';await api('/api/mission/upload',{method:'POST',body:JSON.stringify({items:missionItems,confirmed:true})});toast('Mission upload started; waiting for vehicle requests/acknowledgement');setTimeout(loadMission,500)}catch(e){toast(e.message,'error')}};
    $('#mission-clear').onclick=async()=>{if(window.prompt('Type CLEAR to confirm deleting the onboard mission.')!=='CLEAR')return;try{await api('/api/mission/clear',{method:'POST',body:JSON.stringify({confirmed:true})});toast('Mission clear requested; waiting for vehicle acknowledgement');setTimeout(loadMission,500)}catch(e){toast(e.message,'error')}};
    $('#sim-control-submit').onclick=async()=>{const operation=$('#sim-control-operation').value;if(!state.telemetry?.vehicle?.simulated){toast('Start a clearly labeled simulation before exercising controls','error');return}if(!window.confirm(`Run ${operation} in SIMULATOR ONLY? This request cannot reach the live MAVLink writer.`))return;let params={};if(operation==='mode')params={mode:$('#sim-control-mode').value};else if(operation==='reposition')params={latitude:Number($('#sim-control-lat').value),longitude:Number($('#sim-control-lon').value),altitude:Number($('#sim-control-alt').value),speed:Number($('#sim-control-speed').value)};else if(operation==='takeoff')params={altitude:Number($('#sim-control-alt').value)};else if(operation==='speed')params={speed:Number($('#sim-control-speed').value)};else if(operation==='parameter_set')params={name:$('#sim-control-param-name').value,value:Number($('#sim-control-param-value').value)};else if(operation==='mission_upload')params={items:missionItems};try{const r=await api('/api/simulation/control',{method:'POST',body:JSON.stringify({operation,behavior:$('#sim-control-behavior').value,params})});toast(`${operation} queued in simulation · ${r.control.state}`,'success');setTimeout(loadSimulationControl,250)}catch(e){toast(e.message,'error')}};
    $('#arm-action').onclick=async()=>{const armed=state.telemetry?.status?.armed?.value===true,operation=armed?'DISARM':'ARM';if(window.prompt(`Type ${operation} to confirm ${operation.toLowerCase()} operation.`)!==operation)return;const safety=['gps.fix_type','battery.voltage_v','status.estimator'].map(k=>{const [s,f]=k.split('.');return state.telemetry?.[s]?.[f]}),unknown=safety.some(f=>!f?.available);if(unknown&&!window.confirm('One or more safety inputs are UNKNOWN. Acknowledge this explicitly to continue?'))return;try{await api('/api/control/arm',{method:'POST',body:JSON.stringify({armed:!armed,confirmation:operation,acknowledge_unknown:unknown})});toast(`${operation} requested; awaiting heartbeat confirmation`);renderArmControl()}catch(e){toast(e.message,'error')}};
    const acknowledgeGuidedUnknown=()=>{const missing=[['GPS fix',state.telemetry?.gps?.fix_type],['battery voltage',state.telemetry?.battery?.voltage_v],['estimator health',state.telemetry?.status?.estimator]].filter(([,f])=>!f?.available).map(([n])=>n);return !missing.length||window.confirm(`Safety data unavailable: ${missing.join(', ')}. Acknowledge these unknowns?`)};
    $('#guided-go').onclick=async()=>{const lat=Number($('#guided-lat').value),lon=Number($('#guided-lon').value),alt=Number($('#guided-alt').value),speed=$('#guided-speed').value?Number($('#guided-speed').value):-1;if(!Number.isFinite(lat)||!Number.isFinite(lon)||!Number.isFinite(alt)){toast('Enter valid latitude, longitude, and altitude','error');return}const unknown=acknowledgeGuidedUnknown();if(unknown===false)return;if(window.prompt(`Type GO to confirm reposition to ${lat}, ${lon}, altitude ${alt} m, speed ${speed===-1?'vehicle default':speed+' m/s'}.`)!=='GO')return;try{const r=await api('/api/guided/reposition',{method:'POST',body:JSON.stringify({latitude:lat,longitude:lon,altitude:alt,speed,confirmation:'GO',acknowledge_unknown:unknown})});$('#guided-status').textContent=`Command transaction ${r.transaction.id} sent/queued. Acceptance is not arrival.`;toast('Guided request queued; awaiting command acknowledgement')}catch(e){toast(e.message,'error')}};
    $('#guided-takeoff').onclick=async()=>{const altitude=Number($('#guided-takeoff-alt').value),unknown=acknowledgeGuidedUnknown();if(unknown===false)return;if(!window.confirm(`Request takeoff to ${altitude} m? Vehicle must already be armed; no mode or arm change will be made.`)||window.prompt('Type TAKEOFF to confirm.')!=='TAKEOFF')return;try{await api('/api/guided/takeoff',{method:'POST',body:JSON.stringify({altitude,confirmation:'TAKEOFF',acknowledge_unknown:unknown})});toast('Takeoff command queued; ACK does not confirm completion')}catch(e){toast(e.message,'error')}};
    $('#guided-land').onclick=async()=>{const unknown=acknowledgeGuidedUnknown();if(unknown===false)return;if(!window.confirm('Request landing now? This changes aircraft behavior.')||window.prompt('Type LAND to confirm.')!=='LAND')return;try{await api('/api/guided/land',{method:'POST',body:JSON.stringify({confirmation:'LAND',acknowledge_unknown:unknown})});toast('Land command queued; ACK does not confirm touchdown')}catch(e){toast(e.message,'error')}};
    $('#guided-speed').onclick=async()=>{const speed=Number($('#guided-speed-input').value),unknown=acknowledgeGuidedUnknown();if(unknown===false)return;if(!window.confirm(`Request speed ${speed} m/s?`)||window.prompt('Type SPEED to confirm.')!=='SPEED')return;try{await api('/api/guided/speed',{method:'POST',body:JSON.stringify({speed,confirmation:'SPEED',acknowledge_unknown:unknown})});toast('Speed change queued; ACK does not confirm achieved speed')}catch(e){toast(e.message,'error')}};
    $('#pause-graphs').onclick=e=>{state.paused=!state.paused;e.currentTarget.innerHTML=state.paused?'▶ <span>Resume graphs</span>':'Ⅱ <span>Pause graphs</span>';if(!state.paused)renderAllCharts()};
    $('#time-window').onchange=e=>{state.timeWindow=Number(e.target.value);renderAllCharts()};
    $('#map-follow').onclick=()=>{state.followMap=true;state.mapView.panX=0;state.mapView.panY=0;toast('Vehicle following enabled');drawMap()};
    const mapBox=$('#map-canvas');let dragging=false,lastPoint=null;
    mapBox.addEventListener('pointerdown',e=>{if(e.target.closest('.map-zoom-controls'))return;dragging=true;lastPoint=[e.clientX,e.clientY];state.followMap=false;mapBox.setPointerCapture(e.pointerId);mapBox.style.cursor='grabbing'});
    mapBox.addEventListener('pointermove',e=>{if(!dragging||!lastPoint)return;state.mapView.panX+=e.clientX-lastPoint[0];state.mapView.panY+=e.clientY-lastPoint[1];lastPoint=[e.clientX,e.clientY];drawMap()});
    mapBox.addEventListener('pointerup',()=>{dragging=false;lastPoint=null;mapBox.style.cursor='grab'});mapBox.addEventListener('pointercancel',()=>{dragging=false;lastPoint=null;mapBox.style.cursor='grab'});
    mapBox.addEventListener('wheel',e=>{e.preventDefault();state.followMap=false;state.mapView.zoom=Math.max(.5,Math.min(20,state.mapView.zoom*(e.deltaY<0?1.2:1/1.2)));drawMap()},{passive:false});
    $$('[data-map-zoom]').forEach(b=>b.onclick=()=>{state.followMap=false;state.mapView.zoom=Math.max(.5,Math.min(20,state.mapView.zoom*(b.dataset.mapZoom==='in'?1.5:1/1.5)));drawMap()});
    $('#choose-log').onclick=()=>$('#log-file').click();$('#log-file').onchange=e=>{uploadFile(e.target.files[0]);e.target.value=''};
    const zone=$('#upload-zone');['dragenter','dragover'].forEach(n=>zone.addEventListener(n,e=>{e.preventDefault();zone.classList.add('dragover')}));['dragleave','drop'].forEach(n=>zone.addEventListener(n,e=>{e.preventDefault();zone.classList.remove('dragover')}));zone.addEventListener('drop',e=>uploadFile(e.dataTransfer.files[0]));
    $('#refresh-events').onclick=loadEvents;$('#diagnostic-list').onclick=async e=>{const b=e.target.closest('[data-ack]');if(!b)return;try{await api('/api/events/acknowledge',{method:'POST',body:JSON.stringify({id:b.dataset.ack})});await loadEvents()}catch(err){toast(err.message,'error')}};
    $('#inspector-search').oninput=()=>renderInspector(state.inspectorRates,state.inspector.length);$('#inspector-type-filter').onchange=()=>renderInspector(state.inspectorRates,state.inspector.length);
    bindProfileForm();
    $('#setting-theme').onchange=e=>{state.theme=e.target.value;document.body.classList.toggle('light-theme',state.theme==='light')};
    $('#save-settings').onclick=async()=>{try{await api('/api/settings',{method:'POST',body:JSON.stringify({theme:$('#setting-theme').value,telemetry_profile:$('#setting-telemetry').value})});$('#settings-saved').textContent='Saved locally';setTimeout(()=>$('#settings-saved').textContent='',2500)}catch(e){toast(e.message,'error')}};
  }
  function refreshEventsAndAnalysis(){setInterval(()=>{if(state.activePage==='diagnostics')loadEvents();if(state.activePage==='inspector')loadInspector();if(state.activePage==='logs')loadAnalyses();if(state.activePage==='parameters'){loadParameters();loadParameterSnapshots()}if(state.activePage==='mission')loadMission();if(state.telemetry?.vehicle?.simulated&&state.activePage==='overview')loadSimulationControl()},2000)}
  async function init(){document.title=`${BRAND} · Flight intelligence`;$('#brand-name').textContent=BRAND;connectNav();bindUI();
    await Promise.all([loadState(),loadSeries(),loadEvents(),loadSettings(),loadAnalyses()]);openSockets();setInterval(()=>{if(!state.wsUp)loadState();loadSeries()},3000);refreshEventsAndAnalysis();
    window.addEventListener('resize',()=>{drawHorizon();drawMap();renderAllCharts();if(state.activeAnalysis)renderReplayFrame(state.activeAnalysis.result.series||[],state.activeAnalysis.result.events||[],state.replayTime)});
  }
  window.AeroScope={api,toast,clientId,get telemetry(){return state.telemetry},get mission(){return missionItems},setMission(items){missionItems=items;missionDirty=true;renderMission()},get missionDirty(){return missionDirty},renderMission,showConnect,showSimulation};
  init();
})();
