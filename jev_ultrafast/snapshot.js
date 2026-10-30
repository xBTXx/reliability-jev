(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  const clip = (s,n=500) => String(s||'').replace(/\s+/g,' ').trim().slice(0,n);
  const refs = (e,attribute) => (e.getAttribute(attribute)||'').split(/\s+/)
    .map(id=>document.getElementById(id)).filter(Boolean);
  const readable = root => {
    if (!root) return '';
    const texts=[], walk=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    let n, length=0;
    while ((n=walk.nextNode()) && length<500) {
      const p=n.parentElement;
      if (!p || p.closest('script,style,noscript,template,input,textarea,select,[contenteditable]') ||
          !onScreen(p)) continue;
      const t=clip(n.textContent); if (t) { texts.push(t); length+=t.length; }
    }
    return clip(texts.join(' '));
  };
  const formOf = e => e.form || e.closest('form,[role="form"],[role="dialog"],[role="group"],fieldset');
  const context = e => {
    if (!safe(e)) return null;
    const form=formOf(e), group=e.closest('fieldset,[role="group"]');
    return {
      id:clip(e.id,160), name:clip(e.getAttribute('name'),160), type:clip(e.type,40),
      placeholder:clip(e.getAttribute('placeholder'),200), required:!!e.required,
      labels:[...(e.labels||[]),...refs(e,'aria-labelledby')].map(l=>clip(l.textContent,200)).slice(0,6),
      description:clip(refs(e,'aria-describedby').map(n=>n.textContent).join(' ')),
      errors:clip([...refs(e,'aria-errormessage'),...refs(e,'aria-describedby')]
        .filter(visible).map(n=>n.textContent).join(' ')),
      invalid:e.getAttribute('aria-invalid') || (e.validity?.valid===false ? 'true' : 'false'),
      nearby_text:readable(e.parentElement),
      group:clip(group?.getAttribute('aria-label') || group?.querySelector('legend')?.textContent,200),
      form:form ? identity(form) : null,
    };
  };
  const onScreen = e => {
    const r=e.getBoundingClientRect();
    return visible(e) && r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight &&
      r.right>0 && r.left<innerWidth;
  };
  const comboboxes = () => [...document.querySelectorAll('[role="combobox"]')].filter(e=>safe(e)&&onScreen(e));
  const owner = option => {
    const fields=comboboxes();
    const explicit=fields.filter(e=>[...refs(e,'aria-controls'),...refs(e,'aria-owns')]
      .some(root=>root===option || root.contains(option)));
    if (explicit.length===1) return {node:identity(explicit[0]),source:'aria'};
    if (explicit.length) return null;
    const lists=[...document.querySelectorAll('[role="listbox"]')].filter(onScreen);
    const active=document.activeElement;
    if (lists.length===1 && lists[0].contains(option) && fields.includes(active) &&
        active.getAttribute('aria-expanded')!=='false' &&
        !fields.some(e=>e!==active && (e.getAttribute('aria-expanded')==='true' ||
          [...refs(e,'aria-controls'),...refs(e,'aria-owns')].some(r=>r===lists[0] || r.contains(lists[0])))))
      return {node:identity(active),source:'focus'};
    return null;
  };
  const optionsFor = field => [...document.querySelectorAll('[role="option"]')].filter(onScreen)
    .filter(o=>owner(o)?.node===identity(field));
  const optionSignature = options => options.map(o=>[identity(o),clip(o.textContent),o.getAttribute('aria-selected')]);
  cache.context=context; cache.owner=owner; cache.optionsFor=optionsFor; cache.optionSignature=optionSignature;
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||'',context(e),
      e.closest('[role="option"]') ? owner(e.closest('[role="option"]')) : null];
  };
  const actions=[];
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (!e.contains(document.elementFromPoint(x,y))) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    const editableContext=['textbox','searchbox','spinbutton','combobox'].includes(rname);
    if (editableContext) base.context=context(e);
    const option=e.closest('[role="option"]');
    if (option) {
      const owned=owner(option);
      if (owned) { base.owner_node=owned.node; base.owner_source=owned.source; }
    }
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  // Observed form hints aid scrolling, but never become executable offscreen targets.
  const fields_outside_view=[...document.querySelectorAll('input,textarea,select')].filter(e=>{
    const r=e.getBoundingClientRect();
    return safe(e) && visible(e) && r.width>0 && r.height>0 && (r.bottom<=0 || r.top>=innerHeight);
  }).slice(0,24).map(e=>({node:identity(e),label:name(e),role:role(e),value:String(e.value||''),
    context:context(e),position:e.getBoundingClientRect().bottom<=0 ? 'above' : 'below'}));
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6],fields_outside_view];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,fields_outside_view};
})()
