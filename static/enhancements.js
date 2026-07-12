/* UC Installer advanced UX and resilience enhancements. */
(() => {
  'use strict';
  const $id = id => document.getElementById(id);
  const escHtml = window.esc || (s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
  const pending = new Map();
  let lastSelectedIndex = null;
  let completedCollapsed = true;
  const savedViewsKey = 'uc_saved_log_views_v1';
  const restartKey = 'uc_restart_required_v1';

  // ---- resilient API: timeout, dedupe GETs, normalized errors, retries ----
  const rawApi = window.api;
  function normalizeError(error, path='') {
    const raw = String(error?.message || error || 'Unknown error');
    const map = [
      [/container.*not found|no such container/i, ['Container no longer exists', 'Rebuild the integration.']],
      [/authentication|401/i, ['Authentication failed', 'Check the access token, remote PIN, or API key.']],
      [/port .*in use|409/i, ['Port conflict', 'Choose another port or use automatic assignment.']],
      [/remote.*unreachable|could not reach remote/i, ['Remote is unreachable', 'Test the remote connection and verify its address.']],
      [/network|fetch|timeout|timed out/i, ['Connection failed', 'The installer may be restarting. The request will be retried where safe.']],
    ];
    for (const [rx,[title,suggestion]] of map) if (rx.test(raw)) return {title, detail:raw, suggestion, path};
    return {title:'Request failed', detail:raw.replace(/^HTTP \d+\s*/i,''), suggestion:'Review the related logs and retry.', path};
  }
  window.api = async function(path, opts={}) {
    const method=(opts.method||'GET').toUpperCase();
    const key=method+':'+path+':'+(opts.body||'');
    if(method==='GET' && pending.has(key)) return pending.get(key);
    const run=async()=>{
      let attempt=0, last;
      const max=method==='GET'?3:1;
      while(attempt<max){
        try {
          const ctrl=new AbortController();
          const timer=setTimeout(()=>ctrl.abort(), Number(opts.timeout||20000));
          const result=await rawApi(path,{...opts,signal:opts.signal||ctrl.signal});
          clearTimeout(timer); return result;
        } catch(e){ last=e; attempt++; if(attempt<max) await new Promise(r=>setTimeout(r,300*Math.pow(2,attempt-1))); }
      }
      const n=normalizeError(last,path); const err=new Error(n.detail); err.normalized=n; throw err;
    };
    const promise=run().finally(()=>pending.delete(key)); if(method==='GET')pending.set(key,promise); return promise;
  };

  // ---- persistent operations with Active / Failed / Completed ----
  const historyKey='uc_operation_history_v1';
  function saveOps(){
    const rows=[...OPERATIONS.values()].map(o=>({...o,action:null,retry:null})).slice(-100);
    localStorage.setItem(historyKey,JSON.stringify(rows));
  }
  try { JSON.parse(localStorage.getItem(historyKey)||'[]').forEach(o=>OPERATIONS.set(o.id,o)); } catch {}
  window.scheduleOperationCleanup=()=>{};
  window.addOperation=function(id,title){const existing=OPERATIONS.get(id)||{};OPERATIONS.set(id,{id,title,status:'running',lines:[],progress:5,createdAt:Date.now(),...existing,title});saveOps();renderOperations();};
  window.updateOperation=function(id,patch){const o=OPERATIONS.get(id)||{id,title:id,status:'running',lines:[],progress:5,createdAt:Date.now()};Object.assign(o,patch);if(o.status==='error')o.status='failed';o.updatedAt=Date.now();OPERATIONS.set(id,o);saveOps();renderOperations();};
  window.dismissOperation=id=>{OPERATIONS.delete(id);saveOps();renderOperations();};
  window.clearCompletedOperations=()=>{[...OPERATIONS].forEach(([id,o])=>{if(o.status==='success'||o.status==='completed')OPERATIONS.delete(id)});saveOps();renderOperations();};
  window.retryOperation=async id=>{const o=OPERATIONS.get(id);if(!o?.retry)return;try{updateOperation(id,{status:'running',lines:['Retrying…']});await o.retry();}catch(e){const n=e.normalized||normalizeError(e);updateOperation(id,{status:'failed',lines:[n.detail],suggestion:n.suggestion});}};
  window.renderOperations=function(){
    const drawer=$id('operationDrawer'), list=$id('operationList'); if(!drawer||!list)return;
    const all=[...OPERATIONS.values()].sort((a,b)=>(b.updatedAt||b.createdAt||0)-(a.updatedAt||a.createdAt||0));
    const groups={active:all.filter(o=>o.status==='running'),failed:all.filter(o=>o.status==='failed'||o.status==='error'),completed:all.filter(o=>o.status==='success'||o.status==='completed')};
    drawer.classList.toggle('hidden',!all.length);
    $id('operationSummary').textContent=groups.active.length?groups.active.length+' active':groups.failed.length?groups.failed.length+' failed':groups.completed.length+' completed';
    const item=o=>`<article class="operation-item operation-${escHtml(o.status)}"><div class="operation-row"><strong>${escHtml(o.title)}</strong><span>${escHtml(o.status)}</span></div><div class="operation-meta">${escHtml((o.lines||[]).slice(-1)[0]||o.detail||'')}</div>${o.suggestion?`<div class="operation-suggestion">Suggested: ${escHtml(o.suggestion)}</div>`:''}<div class="operation-actions">${o.retry?`<button class="btn btn-line btn-sm" onclick="retryOperation('${escHtml(o.id)}')">Retry</button>`:''}${o.logTarget?`<button class="btn btn-line btn-sm" onclick="${o.logTarget==='installer'?'openInstallerLogs()':`openLogs('${escHtml(o.logTarget)}')`}">Open logs</button>`:''}${o.actionLabel?`<button class="btn btn-line btn-sm" id="op-action-${escHtml(o.id)}">${escHtml(o.actionLabel)}</button>`:''}<button class="btn btn-line btn-sm" onclick="dismissOperation('${escHtml(o.id)}')">Dismiss</button></div></article>`;
    const section=(name,title,rows,collapsed=false)=>`<section class="operation-group ${collapsed?'collapsed':''}"><button class="operation-group-head" onclick="this.parentElement.classList.toggle('collapsed')"><span>${title}</span><b>${rows.length}</b><i>${msIcon('expand_more')}</i></button><div>${rows.map(item).join('')||'<p class="operation-empty">None</p>'}</div></section>`;
    list.innerHTML=section('active','Active',groups.active)+section('failed','Failed',groups.failed)+section('completed','Completed',groups.completed,completedCollapsed)+`<div class="operation-footer"><button class="btn btn-line btn-sm" onclick="clearCompletedOperations()" ${groups.completed.length?'':'disabled'}>Clear completed</button></div>`;
    all.forEach(o=>{const b=$id('op-action-'+o.id);if(b)b.onclick=()=>o.action?.();});
    requestAnimationFrame(window.updateFloatingOffsets||(()=>{}));
  };
  renderOperations();

  function operationNotice(title,message,status='success',options={}){
    const id='notice-'+Date.now()+'-'+Math.random().toString(36).slice(2,7);
    addOperation(id,title); const n=options.error?.normalized|| (options.error?normalizeError(options.error):null);
    const op=OPERATIONS.get(id); if(op){op.retry=options.retry;op.logTarget=options.logTarget;op.action=options.action;op.actionLabel=options.actionLabel;}
    updateOperation(id,{status:status==='bad'?'failed':status,lines:[n?.detail||message],suggestion:n?.suggestion}); return id;
  }
  window.toast=(msg,kind)=>operationNotice(kind==='bad'?'Action failed':'Notification',msg,kind==='bad'?'failed':'success');
  window.toastAction=(msg,kind,label,action)=>operationNotice(kind==='bad'?'Action failed':'Notification',msg,kind==='bad'?'failed':'success',{actionLabel:label,action});

  // ---- selection interaction: strict click, double click, range and modifier ----
  window.handleInstalledRowClick=function(event,id){
    if(event.target.closest('button,a,input,select,label,.menu,.menu-wrap,.integration-row-chevron'))return;
    const visible=filteredInstalled(), idx=visible.findIndex(x=>x.id===id);
    if(event.shiftKey && lastSelectedIndex!=null){const [a,b]=[lastSelectedIndex,idx].sort((x,y)=>x-y);visible.slice(a,b+1).forEach(x=>SELECTED_INSTALLED.add(x.id));}
    else if(event.ctrlKey||event.metaKey){SELECTED_INSTALLED.has(id)?SELECTED_INSTALLED.delete(id):SELECTED_INSTALLED.add(id);lastSelectedIndex=idx;}
    else {SELECTED_INSTALLED.has(id)?SELECTED_INSTALLED.delete(id):SELECTED_INSTALLED.add(id);lastSelectedIndex=idx;}
    renderInstalled();
  };
  document.addEventListener('dblclick',e=>{const row=e.target.closest('.integration-row');if(!row||e.target.closest('button,a,input,select,label,.menu'))return;const id=row.querySelector('.integration-row-sub')?.textContent.split(' · ')[0];if(id)toggleDetails(id);});
  if(!localStorage.getItem('uc_row_hint_seen')){setTimeout(()=>{operationNotice('Installed row controls','Click a row to select it. Double-click or use the chevron for details. Shift-click selects a range.','success');localStorage.setItem('uc_row_hint_seen','1');},1200);}

  // ---- optimistic lifecycle and auto-update safeguards ----
  const busy=new Set();
  const rawLifecycle=window.lifecycle;
  window.lifecycle=async function(id,action){if(busy.has(id))return;busy.add(id);renderInstalled();const op='lifecycle-'+id+'-'+action+'-'+Date.now();addOperation(op,action+' '+id);try{await rawLifecycle(id,action);updateOperation(op,{status:'success',lines:[action+' requested'],logTarget:id});}catch(e){const n=e.normalized||normalizeError(e);updateOperation(op,{status:'failed',lines:[n.detail],suggestion:n.suggestion,logTarget:id,retry:()=>window.lifecycle(id,action)});await loadInstalled();}finally{busy.delete(id);renderInstalled();}};
  const rawSetPolicy=window.setAutoUpdate;
  window.setAutoUpdate=async function(id,enabled){if(busy.has('policy:'+id))return;busy.add('policy:'+id);const it=INSTALLED.find(x=>x.id===id),prev=it?.auto_update;if(it)it.auto_update=enabled;renderInstalled();try{await rawSetPolicy(id,enabled);}catch(e){if(it)it.auto_update=prev;renderInstalled();throw e;}finally{busy.delete('policy:'+id);}};

  // ---- registration preflight and bulk remote registration ----
  async function preflight(remoteId,id){return api(`/api/remotes/${encodeURIComponent(remoteId)}/registration-preflight/${encodeURIComponent(id)}`);}
  window.bulkRegister=async function(){
    const ids=selectedInstalledIds();if(!ids.length)return;
    const remotes=REMOTES.remotes||[];if(!remotes.length){operationNotice('No remotes configured','Add a remote before registering integrations.','failed');return;}
    const choice=await uiPrompt({title:'Register selected integrations',message:'Enter the remote name or ID.',label:'Remote',value:remotes[0].name||remotes[0].id});if(!choice.confirmed)return;
    const remote=remotes.find(r=>r.id===choice.value||r.name===choice.value);if(!remote){operationNotice('Unknown remote','No configured remote matches that value.','failed');return;}
    const results=await Promise.all(ids.map(async id=>{try{return{id,pre:await preflight(remote.id,id)}}catch(e){return{id,error:e}}}));
    const lines=results.map(r=>r.error?`${r.id}: preflight failed`:r.pre.issues.length?`${r.id}: ${r.pre.issues.map(i=>i.message).join('; ')}`:`${r.id}: will register`);
    const eligible=results.filter(r=>r.pre?.ok&&!r.pre.issues.some(i=>i.code==='driver_id_exists'));
    const d=await uiConfirm({title:`Register ${ids.length} integrations on ${remote.name}`,message:`${eligible.length} will register. ${ids.length-eligible.length} require attention.`,detail:lines.join('\n'),confirmText:'Register eligible'});if(!d.confirmed)return;
    for(const r of eligible){try{await registerIntegration(r.id,remote.id)}catch{}}
  };
  const rawRegister=window.registerIntegration;
  window.registerIntegration=async function(id,remoteId){
    remoteId=remoteId||(REMOTES.remotes||[])[0]?.id; if(!remoteId)return rawRegister(id,remoteId);
    const pf=await preflight(remoteId,id); const blocking=pf.issues.filter(i=>i.severity==='error');
    if(blocking.length){operationNotice('Registration blocked',blocking.map(i=>i.message).join('; '),'failed');return;}
    if(pf.issues.length){const d=await uiConfirm({title:'Registration preflight',message:'Potential conflicts were detected.',detail:pf.issues.map(i=>i.message).join('\n'),confirmText:'Continue'});if(!d.confirmed)return;}
    return rawRegister(id,remoteId);
  };

  // ---- actionable remote driver management ----
  const rawViewDrivers=window.viewDrivers;
  window.viewDrivers=async function(rid){
    const host=$id('drivers-'+rid);if(host)host.innerHTML='<div class="skeleton-list"><div class="skeleton-row"></div><div class="skeleton-row"></div></div>';
    let drivers;try{drivers=await api(`/api/remotes/${rid}/drivers`);}catch(e){operationNotice('Could not load remote drivers','', 'failed',{error:e,retry:()=>viewDrivers(rid)});return;}
    const remote=(REMOTES.remotes||[]).find(r=>r.id===rid);const installedByDriver=new Map(INSTALLED.map(i=>[i.driver_id||i.id,i]));
    host.innerHTML=`<div class="driver-toolbar"><input id="driverSearch-${rid}" placeholder="Search driver name or ID"><select id="driverType-${rid}"><option value="">All types</option><option value="LOCAL">Bundled</option><option value="EXTERNAL">External</option><option value="CUSTOM">Custom</option></select></div><div id="driverRows-${rid}"></div>`;
    const render=()=>{const q=$id('driverSearch-'+rid).value.toLowerCase(),type=$id('driverType-'+rid).value;const filtered=drivers.filter(d=>{const t=String(d.driver_type||d.type||'').toUpperCase();return(!type||t===type)&&(!q||`${d.name?.en||d.name||''} ${d.driver_id||''}`.toLowerCase().includes(q));});$id('driverRows-'+rid).innerHTML=filtered.map(d=>{const type=String(d.driver_type||d.type||'EXTERNAL').toUpperCase(), local=installedByDriver.get(d.driver_id), removable=type!=='LOCAL';return `<div class="driver-manage-row"><div><strong>${escHtml(d.name?.en||d.name||d.driver_id)}</strong><span>${escHtml(d.driver_id||'')} · ${escHtml(type)} · ${escHtml(d.driver_state||d.state||'unknown')}</span></div><div>${local?`<button class="btn btn-line btn-sm" onclick="switchTab('installed');setTimeout(()=>document.querySelector('[data-instance-id=\\'${escHtml(local.id)}\\']')?.scrollIntoView({behavior:'smooth'}),100)">Open local</button>`:''}<button class="btn btn-line btn-sm" onclick="copyText('${escHtml(d.driver_id||'')}')">Copy ID</button>${removable?`<button class="btn btn-danger btn-sm" onclick="unregisterDriver('${rid}','${escHtml(d.driver_id)}')">Unregister</button>`:''}</div></div>`}).join('')||'<div class="empty compact">No matching drivers</div>';};
    $id('driverSearch-'+rid).oninput=render;$id('driverType-'+rid).onchange=render;render();
    operationNotice('Remote drivers loaded',`${drivers.length} drivers on ${remote?.name||rid}`,'success');
  };

  // ---- logs: source selector and saved filter views ----
  function ensureLogControls(){
    const bar=$id('logBack')?.querySelector('.control-bar');if(!bar||$id('logSourceSelect'))return;
    const source=document.createElement('select');source.id='logSourceSelect';source.className='filter';source.setAttribute('aria-label','Log source');source.onchange=()=>{const v=source.value;v==='installer'?openInstallerLogs():openLogs(v);};bar.prepend(source);
    const views=document.createElement('select');views.id='savedLogViews';views.className='filter';views.onchange=()=>applySavedLogView(views.value);bar.appendChild(views);
    const save=document.createElement('button');save.className='btn btn-line btn-sm';save.textContent='Save view';save.onclick=saveCurrentLogView;bar.appendChild(save);refreshLogSourceOptions();refreshSavedLogViews();
  }
  function refreshLogSourceOptions(){const s=$id('logSourceSelect');if(!s)return;s.innerHTML='<option value="installer">Installer service</option>'+INSTALLED.map(i=>`<option value="${escHtml(i.id)}">${escHtml(i.label||i.name)}</option>`).join('');s.value=logInstaller?'installer':logTarget||'installer';}
  function getSavedViews(){try{return JSON.parse(localStorage.getItem(savedViewsKey)||'[]')}catch{return[]}}
  function refreshSavedLogViews(){const s=$id('savedLogViews');if(!s)return;s.innerHTML='<option value="">Saved views</option>'+getSavedViews().map((v,i)=>`<option value="${i}">${escHtml(v.name)}</option>`).join('');}
  window.saveCurrentLogView=async()=>{const r=await uiPrompt({title:'Save log view',message:'Save the current filter combination.',label:'View name',value:''});if(!r.confirmed||!r.value.trim())return;const views=getSavedViews();views.push({name:r.value.trim(),query:$id('logSearch')?.value||'',level:$id('logLevelFilter')?.value||'',time:$id('logTimeFilter')?.value||'0',regex:!!$id('logRegex')?.checked,wrap:!!$id('logWrap')?.checked,timestamps:!!$id('logTimestamps')?.checked});localStorage.setItem(savedViewsKey,JSON.stringify(views));refreshSavedLogViews();operationNotice('Log view saved',r.value.trim(),'success');};
  window.applySavedLogView=i=>{if(i==='')return;const v=getSavedViews()[Number(i)];if(!v)return;['logSearch','logLevelFilter','logTimeFilter'].forEach(id=>{if($id(id))$id(id).value=id==='logSearch'?v.query:id==='logLevelFilter'?v.level:v.time});if($id('logRegex'))$id('logRegex').checked=v.regex;if($id('logWrap'))$id('logWrap').checked=v.wrap;if($id('logTimestamps'))$id('logTimestamps').checked=v.timestamps;toggleLogWrap();toggleLogTimestamps();applyLogFilters();};
  const ol=window.openLogs;window.openLogs=async id=>{ensureLogControls();const r=await ol(id);refreshLogSourceOptions();return r};
  const oil=window.openInstallerLogs;window.openInstallerLogs=async()=>{ensureLogControls();const r=await oil();refreshLogSourceOptions();return r};
  ensureLogControls();

  // ---- settings changes, restart handling, import/export ----
  function ensureRestartBanner(){if($id('restartRequiredBanner'))return;const p=$id('maintBack')?.querySelector('.body');if(!p)return;const b=document.createElement('div');b.id='restartRequiredBanner';b.className='restart-banner';b.innerHTML='<div><strong>Installer restart required</strong><span>Saved settings are not fully active yet.</span></div><button class="btn btn-primary btn-sm" onclick="restartInstallerService()">Restart now</button>';p.prepend(b);b.classList.toggle('show',localStorage.getItem(restartKey)==='1');}
  window.restartInstallerService=async()=>{const d=await uiConfirm({title:'Restart installer service',message:'The web interface will disconnect briefly.',confirmText:'Restart'});if(!d.confirmed)return;const id=operationNotice('Restarting installer','Connection will return automatically.','running');try{await api('/api/service/restart',{method:'POST'});localStorage.removeItem(restartKey);$id('restartRequiredBanner')?.classList.remove('show');updateOperation(id,{status:'success',lines:['Restart requested']});}catch(e){const n=e.normalized||normalizeError(e);updateOperation(id,{status:'failed',lines:[n.detail],suggestion:n.suggestion,retry:restartInstallerService});}};
  window.exportSettings=async()=>{const data=await api('/api/settings/export');const blob=new Blob([JSON.stringify({...data,ui_preferences:AppStore.serializable?.()||AppStore.state,saved_log_views:getSavedViews()},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='uc-installer-settings.json';a.click();URL.revokeObjectURL(a.href);operationNotice('Settings exported','Secrets were excluded.','success');};
  window.importSettingsFile=async file=>{if(!file)return;let payload;try{payload=JSON.parse(await file.text())}catch{operationNotice('Invalid settings file','The selected file is not valid JSON.','failed');return;}const d=await uiConfirm({title:'Import settings',message:'Import general settings, notification events, UI preferences, and saved log views?',detail:'Secrets and remote credentials are never imported.',confirmText:'Import'});if(!d.confirmed)return;await api('/api/settings/import',{method:'POST',body:JSON.stringify({payload})});if(payload.ui_preferences)localStorage.setItem('uc_ui_state_v3',JSON.stringify(payload.ui_preferences));if(payload.saved_log_views)localStorage.setItem(savedViewsKey,JSON.stringify(payload.saved_log_views));operationNotice('Settings imported','Reload to apply all UI preferences.','success',{actionLabel:'Reload',action:()=>location.reload()});};
  function enhanceSettings(){ensureRestartBanner();const sec=$id('maintBack')?.querySelector('[data-settings-tab="backup"]');if(sec&&!$id('settingsExportBtn')){sec.insertAdjacentHTML('beforeend','<div class="field"><label>Export & Import</label><div class="hint">Exports settings and UI preferences without secrets.</div><div class="det-actions"><button class="btn btn-line" id="settingsExportBtn" onclick="exportSettings()">Export settings</button><button class="btn btn-line" onclick="document.getElementById(\'settingsImportFile\').click()">Import settings</button><input id="settingsImportFile" type="file" accept="application/json,.json" hidden onchange="importSettingsFile(this.files[0]);this.value=\'\'"></div></div>');}}
  const osm=window.openMaint;window.openMaint=async()=>{const r=await osm();enhanceSettings();return r};enhanceSettings();
  const oldSaveAll=window.saveAllSettings;
  window.saveAllSettings=async function(){
    const baseline=window.__settingsBaselineSnapshot; // optional future hook
    const changed=[];document.querySelectorAll('#maintBack input,#maintBack select,#maintBack textarea').forEach(el=>{if(el.matches(':disabled'))return;if(el.dataset.dirty==='true'||el.classList.contains('dirty'))changed.push(el.closest('.field')?.querySelector('label')?.textContent.trim()||el.id)});
    const restartIds=new Set(['settingsRepo','settingsBranch','settingsService','settingsToken']);const restartNeeded=[...document.querySelectorAll('#maintBack input,#maintBack select')].some(el=>restartIds.has(el.id)&&el.value!==el.defaultValue);
    const d=await uiConfirm({title:'Save settings',message:`Save ${changed.length||'the'} changed setting${changed.length===1?'':'s'}?`,detail:(changed.length?changed.join('\n'):'Changes will be applied according to each setting’s effect label.'),confirmText:restartNeeded?'Save':'Save changes',checkboxLabel:restartNeeded?'Restart installer after saving':null});if(!d.confirmed)return;
    await oldSaveAll();
    if(restartNeeded){localStorage.setItem(restartKey,'1');$id('restartRequiredBanner')?.classList.add('show');operationNotice('Settings saved','Some changes require an installer restart.','success',{actionLabel:'Restart now',action:restartInstallerService});if(d.checked)restartInstallerService();}
    else operationNotice('Settings saved','Changes applied immediately or will affect future integrations.','success');
  };

  // ---- update policies in expanded management ----
  function updatePolicyLabel(mode){return ({off:'Manual updates only',notify:'Notify when updates are available',stable:'Automatically install stable releases',prerelease:'Automatically install all releases',scheduled:'Install stable releases on a schedule'})[mode]||'Manual updates only';}
  function ensureUpdatePolicyDialog(){if($id('updatePolicyBack'))return;document.body.insertAdjacentHTML('beforeend',`<div class="dialog-back" id="updatePolicyBack"><div class="dialog update-policy-dialog" role="dialog" aria-modal="true" aria-labelledby="updatePolicyTitle"><div class="dialog-head"><div class="dialog-icon">${msIcon('restart_alt')}</div><div><h2 class="dialog-title" id="updatePolicyTitle">Update Settings</h2><p class="dialog-detail" id="updatePolicySub"></p></div></div><div class="dialog-body"><div class="update-policy-options" id="updatePolicyOptions"></div><div class="update-policy-schedule" id="updatePolicySchedule"><div class="field"><label>Delay after release</label><div class="input-suffix"><input id="updatePolicyDelay" type="number" min="0" max="365" value="0"><span>days</span></div><div class="hint">Wait this many days after a release before installing it.</div></div><div class="field"><label>Maintenance window</label><input id="updatePolicyWindow" type="text" placeholder="02:00-04:00"><div class="hint">Use 24-hour time, for example 02:00-04:00.</div></div></div></div><div class="dialog-foot"><button class="btn btn-line" id="updatePolicyCancel">Cancel</button><button class="btn btn-primary" id="updatePolicySave">Save update settings</button></div></div></div>`);}
  window.openUpdatePolicy=async id=>{ensureUpdatePolicyDialog();const it=INSTALLED.find(x=>x.id===id);if(!it)return;const cur=it.update_policy||{mode:it.auto_update?'stable':'off',delay_days:0,maintenance_window:''};const options=[['off','Manual updates only','Updates are installed only when you choose to rebuild or change version.'],['notify','Notify me only','Show and send notifications when a newer release is available, but never install it automatically.'],['stable','Install stable releases automatically','Automatically update to normal stable releases. Pre-releases are ignored.'],['prerelease','Install every release automatically','Automatically install stable releases and pre-releases such as beta or release-candidate builds.'],['scheduled','Install stable releases on a schedule','Automatically install stable releases only after the configured delay and during the maintenance window.']];const back=$id('updatePolicyBack'),box=$id('updatePolicyOptions');$id('updatePolicySub').textContent=it.label||it.name||id;box.innerHTML=options.map(([value,title,desc])=>`<label class="update-policy-option"><input type="radio" name="updatePolicyMode" value="${value}" ${cur.mode===value?'checked':''}><span><strong>${title}</strong><small>${desc}</small></span></label>`).join('');$id('updatePolicyDelay').value=cur.delay_days||0;$id('updatePolicyWindow').value=cur.maintenance_window||'';const sync=()=>{$id('updatePolicySchedule').classList.toggle('show',box.querySelector('input:checked')?.value==='scheduled')};box.onchange=sync;sync();back.classList.add('show');const close=()=>back.classList.remove('show');$id('updatePolicyCancel').onclick=close;$id('updatePolicySave').onclick=async()=>{const mode=box.querySelector('input:checked')?.value||'off',delay=Math.max(0,Number($id('updatePolicyDelay').value)||0),windowText=$id('updatePolicyWindow').value.trim();if(mode==='scheduled'&&!/^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$/.test(windowText)){operationNotice('Invalid maintenance window','Use the format HH:MM-HH:MM, for example 02:00-04:00.','failed');return;}const save=$id('updatePolicySave');save.disabled=true;save.innerHTML=msIcon('save')+' Saving';try{await api(`/api/instances/${id}/update-policy`,{method:'PUT',body:JSON.stringify({mode,delay_days:mode==='scheduled'?delay:0,maintenance_window:mode==='scheduled'?windowText:''})});close();operationNotice('Update settings changed',`${it.label||id}: ${updatePolicyLabel(mode)}`,'success');await loadInstalled();}catch(e){operationNotice('Could not change update settings','', 'failed',{error:e,retry:()=>openUpdatePolicy(id)});}finally{save.disabled=false;save.textContent='Save update settings';}};requestAnimationFrame(()=>box.querySelector('input:checked')?.focus());};
  const oldDetails=window.detailsHtml;window.detailsHtml=function(it){let h=oldDetails(it);h=h.replace(/<label class="auto-control"[\s\S]*?<\/label>/,`<button class="btn btn-line" onclick="openUpdatePolicy('${it.id}')">Update Settings</button>`);return h;};

  // ---- diagnostics page ----
  function ensureDiagnostics(){if($id('diagBack'))return;const main=document.querySelector('main');const panel=document.createElement('section');panel.id='diagBack';panel.className='workspace-panel';panel.style.display='none';panel.innerHTML='<header class="workspace-panel-head"><div><div class="eyebrow">Support</div><h2>Diagnostics</h2><p>System, Docker, registry, remote, storage, and recent error information.</p></div><button aria-label="Back" class="x" onclick="closeModal(\'diagBack\')" title="Back"><svg class="ms-icon" aria-hidden="true"><use href="/static/material-symbols.svg#arrow_back"></use></svg></button></header><div class="body"><div id="diagContent" class="skeleton-list"><div class="skeleton-row"></div></div><div class="det-actions"><button class="btn btn-line" onclick="copyDiagnostics()">Copy diagnostics</button><button class="btn btn-primary" onclick="downloadDiagnostics()">Download diagnostics</button></div></div>';main.appendChild(panel);const settings=$id('maintBack')?.querySelector('[data-settings-tab="runtime"]');settings?.insertAdjacentHTML('beforeend','<div class="field"><label>Support diagnostics</label><button class="btn btn-line" onclick="openDiagnostics()">Open diagnostics</button></div>');}
  let diagData=null;
  window.openDiagnostics=async()=>{ensureDiagnostics();showWorkspacePanel('diagBack');setHash?.('diagnostics');$id('diagContent').innerHTML='<div class="skeleton-list"><div class="skeleton-row"></div><div class="skeleton-row"></div></div>';try{diagData=await api('/api/diagnostics');const rows=[['Installer version',diagData.installer_version],['Python',diagData.python_version],['Docker',diagData.docker_version||'unavailable'],['Data directory',diagData.data_dir],['Service unit',diagData.service_unit],['Registry commit',diagData.registry_commit||'unknown'],['Active jobs',diagData.active_jobs],['Installed integrations',diagData.installed_integrations],['Disk used',fmtBytes(diagData.disk.used)+' / '+fmtBytes(diagData.disk.total)],['Remotes',diagData.remotes.map(r=>`${r.name}: ${r.reachable?'reachable':'unreachable'}`).join(', ')||'none']];$id('diagContent').innerHTML='<div class="det-grid">'+rows.map(([k,v])=>`<div class="kv"><span class="k2">${escHtml(k)}</span><span class="v2">${escHtml(v)}</span></div>`).join('')+'</div><h3>Recent backend errors</h3><div class="console">'+escHtml((diagData.recent_errors||[]).map(e=>`${e.ts} ${e.message}`).join('\n')||'No recent errors')+'</div>';}catch(e){operationNotice('Diagnostics failed','', 'failed',{error:e,retry:openDiagnostics});}};
  window.copyDiagnostics=async()=>{if(diagData)await navigator.clipboard.writeText(JSON.stringify(diagData,null,2));operationNotice('Diagnostics copied','Sensitive credentials are not included.','success');};
  window.downloadDiagnostics=()=>{if(!diagData)return;const b=new Blob([JSON.stringify(diagData,null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='uc-installer-diagnostics.json';a.click();URL.revokeObjectURL(a.href);};
  ensureDiagnostics();

  // ---- destructive context improvements and mobile polish hooks ----
  const rawBulkRemove=window.bulkRemove;window.bulkRemove=async()=>{const ids=selectedInstalledIds(),items=ids.map(id=>INSTALLED.find(x=>x.id===id)).filter(Boolean);if(!items.length)return;const d=await uiConfirm({title:`Remove ${items.length} integrations`,message:'Review the integrations that will be removed.',detail:items.map(i=>`${i.label||i.name} (${i.id})`).join('\n'),confirmText:'Remove',danger:true,checkboxLabel:'Also delete saved configuration and cloned source'});if(!d.confirmed)return;for(const i of items)await api(`/api/instances/${i.id}?purge=${d.checked}`,{method:'DELETE'});SELECTED_INSTALLED.clear();operationNotice('Integrations removed',items.map(i=>i.label||i.name).join(', '),'success');loadInstalled();};

  // asset/frontend version mismatch hint
  fetch('/api/health').then(r=>r.json()).then(h=>{const client='4.0.0';if(h.ui_version&&h.ui_version!==client)operationNotice('Frontend/backend version mismatch',`UI ${client}, backend ${h.ui_version}. Reload after updating.`,'failed');}).catch(()=>{});
})();
