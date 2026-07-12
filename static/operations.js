/* Operation accessibility and actionable toasts. */
(() => {
  const drawer=$('operationDrawer');if(drawer){drawer.setAttribute('aria-live','polite');if(!AppStore.state.operationsOpen)drawer.classList.remove('open');}
  const oldToggle=window.toggleOperationDrawer;window.toggleOperationDrawer=function(){oldToggle();AppStore.state.operationsOpen=$('operationDrawer')?.classList.contains('open');AppStore.persist();};
  const oldUpdate=window.updateOperation;window.updateOperation=function(id,patch){oldUpdate(id,patch);if(patch.status==='success'||patch.status==='failed'){const live=document.createElement('span');live.className='sr-only';live.setAttribute('role','status');live.textContent=(OPERATIONS.get(id)?.title||id)+' '+patch.status;document.body.appendChild(live);setTimeout(()=>live.remove(),1500);}};
  window.toastAction=function(msg,kind,label,action){toast(msg,kind);const t=$('toasts')?.lastElementChild;if(t&&label){const b=document.createElement('button');b.className='toast-action';b.textContent=label;b.onclick=()=>{action?.();t.remove();};t.appendChild(b);}};
})();

/* Loading skeleton hooks and active-remote persistence. */
(() => {
  function rows(n=3){return '<div class="skeleton-list">'+Array(n).fill('<div class="skeleton-row"></div>').join('')+'</div>';}
  function cards(n=6){return '<div class="grid">'+Array(n).fill('<div class="skeleton-card"></div>').join('')+'</div>';}
  const lr=window.loadRegistry;window.loadRegistry=async function(refresh){if($('browseGrid'))$('browseGrid').innerHTML=cards();return lr(refresh);};
  const li=window.loadInstalled;window.loadInstalled=async function(){if($('installedRows')&&!INSTALLED.length)$('installedRows').innerHTML=rows();return li();};
  const la=window.loadActivity;window.loadActivity=async function(){if($('actList'))$('actList').innerHTML=rows(4);return la();};
  const lm=window.loadMainSettings;window.loadMainSettings=async function(){document.querySelectorAll('#maintBack .settings-section').forEach(s=>s.classList.add('loading-section'));try{return await lm();}finally{document.querySelectorAll('#maintBack .settings-section').forEach(s=>s.classList.remove('loading-section'));}};
  const cu=window.checkUpdate;window.checkUpdate=async function(){if($('updBodyContent'))$('updBodyContent').innerHTML=rows(2);return cu();};
})();

/* Route all transient feedback through Operations instead of toast popups. */
(() => {
  let noticeSeq=0;
  window.toast=function(message,kind){
    const id='notice-'+Date.now()+'-'+(++noticeSeq);
    const failed=kind==='bad';
    addOperation(id, failed?'Action failed':'Notification');
    updateOperation(id,{status:failed?'failed':'success',lines:[String(message)],progress:100,title:String(message)});
    const drawer=$('operationDrawer');
    if(drawer){drawer.classList.remove('hidden');requestAnimationFrame(updateFloatingOffsets);}
    return id;
  };
  window.toastAction=function(message,kind,label,action){
    const id=toast(message,kind);
    const op=OPERATIONS.get(id);
    if(op&&label){op.actionLabel=label;op.action=action;renderOperations();}
  };
  const baseRender=window.renderOperations;
  window.renderOperations=function(){
    baseRender();
    const list=$('operationList');if(!list)return;
    const ops=Array.from(OPERATIONS.values()).slice(-8).reverse();
    [...list.children].forEach((el,i)=>{const op=ops[i];if(op?.actionLabel){const b=document.createElement('button');b.className='btn btn-line btn-sm';b.textContent=op.actionLabel;b.onclick=()=>op.action?.();el.appendChild(b);}});
  };
})();
