window.msIcon = window.msIcon || function(name, cls='') { return '<span class="material-symbols-outlined '+cls+'" aria-hidden="true">'+name+'</span>'; };
/* Shared state store and persistence helpers. */
window.AppStore = (() => {
  const listeners = new Set();
  const saved = JSON.parse(localStorage.getItem('uc_ui_state_v3') || '{}');
  const state = {
    route: saved.route || 'installed',
    selected: new Set(),
    expanded: new Set(saved.expanded || []),
    filters: saved.filters || {},
    operationsOpen: saved.operationsOpen !== false,
    settingsScroll: Number(saved.settingsScroll || 0),
    log: Object.assign({query:'',level:'',time:'0',regex:false,wrap:true,timestamps:true}, saved.log || {}),
    connection: 'online',
    registrationPending: new Set(),
    remoteTelemetry: {},
    newLogLines: 0,
  };
  function serializable(){ return {route:state.route,expanded:[...state.expanded],filters:state.filters,operationsOpen:state.operationsOpen,settingsScroll:state.settingsScroll,log:state.log}; }
  function persist(){ localStorage.setItem('uc_ui_state_v3', JSON.stringify(serializable())); }
  function set(patch){ Object.assign(state, patch); persist(); listeners.forEach(fn=>fn(state)); }
  function subscribe(fn){ listeners.add(fn); return ()=>listeners.delete(fn); }
  return {state,set,subscribe,persist};
})();
