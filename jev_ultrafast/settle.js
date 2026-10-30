// Read-only verification after one logged mutation. Never dispatch input here.
(async action => {
  const started=performance.now(), deadline=started+3000;
  let stableSince=started, lastSignature=null, accepted=false;
  const valueOf=e=>String('value' in e ? e.value : e.innerText||'').replace(/\r\n/g,'\n');
  const comparable=s=>String(s||'').normalize('NFKC').toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu,' ').trim();
  const result=(status,reason,observed=null)=>({status,reason,expected:action.expected,
    observed,field_node:action.field_node,wait_ms:Math.round(performance.now()-started)});
  if (action.kind==='scroll') {
    let lastY=scrollY, since=performance.now();
    while (performance.now()<deadline) {
      if (performance.timeOrigin!==action.document) return result('unknown','document_changed');
      if (scrollY!==lastY) {lastY=scrollY;since=performance.now();}
      if ((scrollY-action.before_y)*action.expected>0 && performance.now()-since>=100)
        return result('confirmed','scroll_moved',scrollY);
      await new Promise(resolve=>setTimeout(resolve,25));
    }
    return result('failed','scroll_timeout',scrollY);
  }
  while (true) {
    const cache=window.__jevFast, field=cache?.nodes.get(action.field_node);
    if (!cache || performance.timeOrigin!==action.document || !field?.isConnected)
      return result('unknown','field_replaced_or_document_changed');
    const observed=valueOf(field), options=cache.optionsFor(field);
    const r=field.getBoundingClientRect();
    const signature=JSON.stringify([observed,cache.optionSignature(options),field.getAttribute('aria-expanded'),
      scrollY,Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]);
    if (signature!==lastSignature) { lastSignature=signature; stableSince=performance.now(); }
    const stable=performance.now()-stableSince>=100;
    if (action.kind==='fill') {
      accepted=observed===String(action.expected).replace(/\r\n/g,'\n');
      if (accepted && stable && !action.combobox) return result('confirmed','text_accepted',observed);
      const refreshed=JSON.stringify(cache.optionSignature(options))!==action.before_options;
      if (accepted && stable && options.length && refreshed && field.getAttribute('aria-busy')!=='true')
        return result('confirmed','owned_suggestions_ready',observed);
    } else {
      const clicked=cache.nodes.get(action.node), selected=clicked?.getAttribute('aria-selected')==='true';
      const closed=field.getAttribute('aria-expanded')==='false' || !options.length;
      if (stable && observed && (selected || closed) &&
          (observed!==action.before_value || selected || comparable(observed)===comparable(action.expected)))
        return result('confirmed','suggestion_accepted',observed);
    }
    if (performance.now()>=deadline)
      return result('failed',action.kind==='fill' && !accepted ? 'input_rejected' :
        action.kind==='fill' ? 'autocomplete_timeout' : 'selection_unconfirmed',observed);
    await new Promise(resolve=>setTimeout(resolve,25));
  }
})
