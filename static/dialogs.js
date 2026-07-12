/* Focus management for dialogs and menus. */
(() => {
  let lastFocus=null;
  document.addEventListener('click',e=>{if(e.target.closest('button'))lastFocus=e.target.closest('button');},true);
  const observer=new MutationObserver(ms=>ms.forEach(m=>{if(m.attributeName!=='class')return;const el=m.target;if((el.classList.contains('show'))&&(el.classList.contains('dialog-back')||el.classList.contains('modal-back'))){const focusables=el.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex="0"]');focusables[0]?.focus();}}));
  document.querySelectorAll('.dialog-back,.modal-back').forEach(el=>observer.observe(el,{attributes:true}));
  document.addEventListener('keydown',e=>{if(e.key!=='Tab')return;const open=document.querySelector('.dialog-back.show,.modal-back.show');if(!open)return;const f=[...open.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex="0"]')];if(!f.length)return;const first=f[0],last=f[f.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}});
  const oldClose=window.closeModal; if(oldClose) window.closeModal=function(id){oldClose(id);setTimeout(()=>lastFocus?.focus(),0);};
})();
