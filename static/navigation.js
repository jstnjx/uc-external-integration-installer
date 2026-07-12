/* Navigation, breadcrumbs, keyboard shortcuts, and connection feedback. */
(() => {
  const s=AppStore.state;
  const banner=document.createElement('div'); banner.id='connectionBanner'; banner.className='connection-banner'; banner.setAttribute('role','status'); banner.setAttribute('aria-live','polite'); banner.innerHTML='<span class="led warn"></span><span id="connectionText">Connection to installer lost. Retrying…</span>';
  document.body.prepend(banner);
  const crumbs=document.createElement('nav'); crumbs.id='breadcrumbs'; crumbs.className='breadcrumbs'; crumbs.setAttribute('aria-label','Breadcrumb');
  const main=document.querySelector('main'); main?.prepend(crumbs);
  function labelFor(route){ if(route.startsWith('logs/')) return ['Installed',decodeURIComponent(route.slice(5)),'Logs']; if(route==='installer-logs')return ['Logs','Installer service']; if(route.startsWith('configure/'))return ['Installed',decodeURIComponent(route.slice(10)),'Configure']; if(route==='update')return ['Settings','Updates']; return [route.replace(/-/g,' ').replace(/\b\w/g,c=>c.toUpperCase())]; }
  function renderBreadcrumbs(){ const route=(location.hash||'#/installed').replace(/^#\/?/,''); const parts=labelFor(route); crumbs.innerHTML=parts.map((p,i)=>'<span'+(i===parts.length-1?' aria-current="page"':'')+'>'+esc(p)+'</span>').join('<b>›</b>'); crumbs.style.display=parts.length>1?'flex':'none'; AppStore.set({route}); }
  window.addEventListener('hashchange',renderBreadcrumbs); renderBreadcrumbs();

  const originalApi=window.api;
  window.api=async function(path,opts){ try{ const out=await originalApi(path,opts); if(s.connection!=='online'){s.connection='online';banner.classList.remove('show');toast('Connection restored','ok');} return out; }catch(e){ if(e.message!=='Unauthorized' && /fetch|network|failed|HTTP 5/i.test(e.message)){s.connection='offline';banner.classList.add('show');} throw e; } };
  setInterval(async()=>{if(s.connection==='offline'){try{await fetch('/api/health',{cache:'no-store'});s.connection='online';banner.classList.remove('show');toast('Connection restored','ok');}catch(e){}}},3000);

  let chord=''; let chordTimer;
  document.addEventListener('keydown',e=>{
    const tag=e.target.tagName; const typing=/INPUT|TEXTAREA|SELECT/.test(tag)||e.target.isContentEditable;
    if(e.key==='Escape'){ closeMenus(); if(window.SELECTED_INSTALLED?.size){SELECTED_INSTALLED.clear();renderInstalled();} return; }
    if(typing) return;
    if(e.key==='/'){e.preventDefault();(($('installedSearch')&&TAB==='installed')?$('installedSearch'):$('searchInput'))?.focus();return;}
    if(e.key.toLowerCase()==='r'){e.preventDefault(); if(TAB==='installed')loadInstalled(); else if(TAB==='browse')loadRegistry(true); else if(location.hash.includes('logs'))refreshLogs();return;}
    if(e.key.toLowerCase()==='g'){chord='g';clearTimeout(chordTimer);chordTimer=setTimeout(()=>chord='',900);return;}
    if(chord==='g'){chord=''; const k=e.key.toLowerCase(); if(k==='b')switchTab('browse'); if(k==='i')switchTab('installed'); if(k==='l')openInstallerLogs(); if(k==='s')openMaint();}
    if(e.key==='?'){window.uiInfo?.({title:'Keyboard shortcuts',message:'/  Focus search\ng b  Browse\ng i  Installed\ng l  Logs\ng s  Settings\nr  Refresh current page\nEsc  Close menus or clear selection'});}
  });
})();
