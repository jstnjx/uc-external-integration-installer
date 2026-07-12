/* Log state, new-line indicator, source context, and error navigation. */
(() => {
  const st=AppStore.state;
  const controls=document.querySelector('#logBack .body');
  if(controls){
    const indicator=document.createElement('button'); indicator.id='newLogLines'; indicator.className='new-log-lines'; indicator.onclick=()=>{st.newLogLines=0;indicator.classList.remove('show');jumpToLatestLogs();indicator.textContent='';}; controls.appendChild(indicator);
    const nav=document.createElement('div'); nav.className='log-error-nav'; nav.innerHTML='<button class="btn btn-line btn-sm" id="prevLogError">↑ Previous error</button><span id="logErrorCounts">0 errors · 0 warnings</span><button class="btn btn-line btn-sm" id="nextLogError">Next error ↓</button>'; const consoleEl=$('logConsole'); consoleEl?.before(nav);
    let errorIndex=-1;
    function jumpError(dir){const rows=[...consoleEl.querySelectorAll('.log-line[data-level="error"]:not(.filtered-out)')];if(!rows.length)return;errorIndex=(errorIndex+dir+rows.length)%rows.length;rows[errorIndex].scrollIntoView({block:'center',behavior:'smooth'});rows[errorIndex].focus?.();}
    $('prevLogError').onclick=()=>jumpError(-1); $('nextLogError').onclick=()=>jumpError(1);
  }
  function syncInputs(){ if($('logSearch'))$('logSearch').value=st.log.query; if($('logLevelFilter'))$('logLevelFilter').value=st.log.level; if($('logTimeFilter'))$('logTimeFilter').value=st.log.time; if($('logRegex'))$('logRegex').checked=st.log.regex; if($('logWrap'))$('logWrap').checked=st.log.wrap; if($('logTimestamps'))$('logTimestamps').checked=st.log.timestamps; toggleLogWrap();toggleLogTimestamps(); }
  ['logSearch','logLevelFilter','logTimeFilter','logRegex','logWrap','logTimestamps'].forEach(id=>$(id)?.addEventListener('change',()=>{st.log={query:$('logSearch')?.value||'',level:$('logLevelFilter')?.value||'',time:$('logTimeFilter')?.value||'0',regex:!!$('logRegex')?.checked,wrap:!!$('logWrap')?.checked,timestamps:!!$('logTimestamps')?.checked};AppStore.persist();}));
  $('logSearch')?.addEventListener('input',()=>{st.log.query=$('logSearch').value;AppStore.persist();});
  const oldOpenLogs=window.openLogs; window.openLogs=async function(id){ await oldOpenLogs(id); syncInputs(); updateLogSource(); };
  const oldOpenInstaller=window.openInstallerLogs; window.openInstallerLogs=async function(){ await oldOpenInstaller(); syncInputs(); updateLogSource(); };
  function updateLogSource(){ const it=INSTALLED.find(x=>x.id===logTarget); const h=$('logBack')?.querySelector('header h2'); const sub=$('logSub'); if(logInstaller){if(h)h.textContent='Installer service logs';if(sub)sub.textContent='systemd · '+($('settingsService')?.value||'uc-external-integration-installer');}else if(it){if(h)h.textContent=(it.label||it.name)+' logs';if(sub)sub.textContent='container · '+it.status+' · '+it.id;} }
  const oldApply=window.applyLogFilters; window.applyLogFilters=function(){oldApply();const rows=[...($('logConsole')?.querySelectorAll('.log-line')||[])];const errors=rows.filter(r=>r.dataset.level==='error').length,warns=rows.filter(r=>r.dataset.level==='warn').length;if($('logErrorCounts'))$('logErrorCounts').textContent=errors+' errors · '+warns+' warnings';};
  const oldAppend=window.appendLogLine; window.appendLogLine=function(line){const c=$('logConsole');const wasLatest=LOG_AT_LATEST;oldAppend(line);if(!wasLatest){st.newLogLines++;const b=$('newLogLines');if(b){b.textContent=st.newLogLines+' new line'+(st.newLogLines===1?'':'s');b.classList.add('show');}}};
  $('logConsole')?.addEventListener('scroll',()=>{if(LOG_AT_LATEST){st.newLogLines=0;$('newLogLines')?.classList.remove('show');}});
  syncInputs();
})();
