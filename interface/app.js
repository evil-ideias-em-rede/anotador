'use strict';
const REVIEW_FORMAT = 'revisao_humana_falas_v1';

function normalizeDebate(raw) {
  if (!raw || typeof raw !== 'object' || typeof raw.tema !== 'string') throw Error('Não é um debate reconhecido.');
  let view = raw;
  if (Array.isArray(raw.falas) && Array.isArray(raw.participantes)) {
    // Compatibilidade de leitura: não altera nem regrava o formato antigo.
    view = {debate_id: raw.debate_id, tema: raw.tema, notas_revisao: raw.notas_revisao || [],
      participantes: raw.participantes.map(p => ({nome:p.nome, falas:raw.falas.filter(f => f.participante === p.nome).map(f => {
        const a = f.anotacao;
        return {id:f.id, ordem_no_debate:f.ordem_inicio, texto:f.texto_titular,
          taxonomia:a ? Object.fromEntries(Object.entries(a.dimensoes || {}).map(([k,v]) => [k,v.indicadores])) : null,
          resumo:a?.resumo ?? null, propostas:a?.propostas?.map(x => x.enunciado) ?? null,
          interrupcoes:(f.interrupcoes || []).map(x => {
            const idx = (f.turnos_titular || []).indexOf(x.apos_turno);
            let offset = null;
            if (idx >= 0 && Array.isArray(raw.turnos)) {
              const turns = f.turnos_titular.slice(0,idx+1).map(id => raw.turnos.find(t => t.id === id));
              if (turns.every(Boolean)) offset = turns.reduce((n,t) => n+t.fim-t.inicio,0)+2*idx;
            }
            return {participante:x.participante,texto:x.texto,apos_caractere_do_texto:offset,
              mudanca_de_opiniao_na_retomada:x.mudanca_de_opiniao,observacao:x.observacao_retomada};
          }), pendencias_revisao:a?.motivo_resumo_pendente ? [a.motivo_resumo_pendente] : []};
      })}))};
  }
  if (!Array.isArray(view.participantes)) throw Error('Faltam participantes.');
  const ids = new Set();
  for (const p of view.participantes) {
    if (typeof p.nome !== 'string' || !Array.isArray(p.falas)) throw Error('Participante inválido.');
    for (const f of p.falas) {
      if (typeof f.id !== 'string' || ids.has(f.id) || typeof f.texto !== 'string') throw Error('ID duplicado ou fala inválida.');
      ids.add(f.id);
      if (f.taxonomia != null && (typeof f.taxonomia !== 'object' || Array.isArray(f.taxonomia) || Object.values(f.taxonomia).some(v => !Array.isArray(v) || v.some(x => typeof x !== 'string')))) throw Error('Taxonomia inválida.');
      if (f.resumo != null && typeof f.resumo !== 'string') throw Error('Resumo inválido.');
      if (f.propostas != null && (!Array.isArray(f.propostas) || f.propostas.some(x => typeof x !== 'string'))) throw Error('Propostas inválidas.');
      if (f.interrupcoes != null && (!Array.isArray(f.interrupcoes) || f.interrupcoes.some(x => !x || typeof x.texto !== 'string'))) throw Error('Interrupções inválidas.');
    }
  }
  if (!ids.size) throw Error('Debate sem falas.');
  return view;
}

function sourceAnnotation(f, guide) {
  return {taxonomia:Object.fromEntries(guide.map(d => [d.dimension, [...(f.taxonomia?.[d.dimension] || [])]])),
    resumo:f.resumo || '', propostas:[...(f.propostas || [])]};
}
function annotationChanged(a,b,guide) {
  return a.resumo !== b.resumo || JSON.stringify(a.propostas)!==JSON.stringify(b.propostas) ||
    guide.some(d => JSON.stringify([...a.taxonomia[d.dimension]].sort())!==JSON.stringify([...b.taxonomia[d.dimension]].sort()));
}
function validateEntry(e, row, guide) {
  if (!e || e.fala_id !== row.f.id || e.participante !== row.person || !['rascunho','revisado'].includes(e.status)) throw Error('Revisão não corresponde à fala.');
  if (!['','confirmado','corrigido','inconclusivo'].includes(e.decisao) || !['nao_avaliado','adequado','problema','incerto'].includes(e.agrupamento)) throw Error('Decisão inválida.');
  for (const key of ['resumo','observacoes','validador','atualizado_em']) if (typeof e[key] !== 'string') throw Error('Campo textual inválido na revisão.');
  if (!Array.isArray(e.propostas) || e.propostas.some(x => typeof x !== 'string')) throw Error('Propostas inválidas na revisão.');
  if (!e.taxonomia || Object.keys(e.taxonomia).length !== guide.length) throw Error('Dimensões inválidas na revisão.');
  for (const d of guide) {
    const labels=e.taxonomia[d.dimension];
    if (!Array.isArray(labels) || new Set(labels).size!==labels.length || labels.some(x => !d.indicators.includes(x)) || ([1,4].includes(d.id) && labels.length>1)) throw Error('Indicadores inválidos na revisão.');
  }
  if (e.status==='revisado') {
    if (!e.validador.trim() || !e.decisao) throw Error('Informe o validador e a decisão.');
    if ((e.decisao==='inconclusivo' || ['problema','incerto'].includes(e.agrupamento)) && !e.observacoes.trim()) throw Error('Descreva a dúvida ou o problema nas observações.');
    if (e.decisao==='confirmado' && row.f.taxonomia==null) throw Error('Esta fala não possui anotação do modelo para confirmar. Registre sua anotação como correção.');
    if (e.decisao==='confirmado' && annotationChanged(e, sourceAnnotation(row.f,guide),guide)) throw Error('Os campos foram alterados. Escolha “Registrar correções” ou restaure o original.');
    if (e.decisao==='confirmado' && e.agrupamento==='problema') throw Error('Há problema de agrupamento. Registre correções ou uma decisão inconclusiva.');
    if (e.decisao==='corrigido' && !annotationChanged(e,sourceAnnotation(row.f,guide),guide) && !e.observacoes.trim()) throw Error('Altere a anotação ou descreva a correção nas observações.');
  }
  return e;
}
function speechParts(f) {
  const located=[],unlocated=[];
  for (const x of f.interrupcoes || []) {
    const at=x.apos_caractere_do_texto;
    (Number.isInteger(at) && at>=0 && at<=Array.from(f.texto).length ? located : unlocated).push(x);
  }
  located.sort((a,b) => a.apos_caractere_do_texto-b.apos_caractere_do_texto);
  return {located,unlocated};
}
// Exportações permitem testar regras de integridade sem navegador ou API.
if (typeof module !== 'undefined') module.exports={normalizeDebate,sourceAnnotation,annotationChanged,validateEntry,speechParts};
if (typeof document !== 'undefined') boot().catch(e => {document.getElementById('notice').textContent='Falha ao iniciar: '+e.message;});

async function boot() {
  const $=id => document.getElementById(id);
  const response=await fetch('/guia.json');
  if (!response.ok) throw Error('Não foi possível carregar os critérios. Inicie com visualizar.py.');
  const guide=await response.json();
  const guideHash=await hash(new TextEncoder().encode(JSON.stringify(guide)));
  const state={debates:[],current:null,selected:null,dirty:false,loading:false};
  const el=(tag,text,cls) => {const n=document.createElement(tag);if(text!=null)n.textContent=String(text);if(cls)n.className=cls;return n;};
  const notice=(message,error=false) => {$('notice').textContent=message;$('notice').classList.toggle('error',error);};
  const row=() => state.current?.rows.find(r => r.f.id===state.selected);
  const key=d => 'revisao-falas-v1:'+d.hash;
  function bundle(d) {return {arquivo:d.name,sha256:d.hash,debate_id:d.view.debate_id,tema:d.view.tema,revisoes:Object.values(d.entries)};}
  function persist() {
    try {localStorage.setItem(key(state.current),JSON.stringify(bundle(state.current)));return true;}
    catch {notice('Armazenamento do navegador indisponível ou cheio. Exporte a revisão antes de sair.',true);return false;}
  }
  async function hash(bytes) {return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');}
  function checkedEntries(b,d) {
    if (b.sha256!==d.hash || b.debate_id!==d.view.debate_id) throw Error('A revisão pertence a outra versão do debate.');
    if (!Array.isArray(b.revisoes)) throw Error('Arquivo de revisão inválido.');
    const entries=Object.create(null);
    for (const e of b.revisoes) {
      const r=d.rows.find(x=>x.f.id===e.fala_id);
      if (!r || Object.hasOwn(entries,e.fala_id)) throw Error('Fala ausente ou repetida na revisão.');
      entries[e.fala_id]=validateEntry(e,r,guide);
    }
    return entries;
  }
  async function loadFiles(files) {
    if (state.loading) return;
    state.loading=true;
    try {
      const all=[...files],byPath=new Map(all.map(f=>[f.webkitRelativePath||f.name,f]));
      let added=0,ignored=0,unavailable=0;const errors=[];
      for (const file of all) {
        const path=file.webkitRelativePath||file.name;
        if (!path.endsWith('.json') || path.split('/').some(p => /^(auditoria|andamento|pendentes|cache.*)$/.test(p))) {ignored++;continue;}
        notice('Abrindo '+path+'…');
        try {
          const bytes=await file.arrayBuffer(),raw=JSON.parse(new TextDecoder().decode(bytes));
          if (!raw || !Array.isArray(raw.participantes)) {ignored++;continue;}
          const view=normalizeDebate(raw),sha=await hash(bytes);
          if (state.debates.some(d=>d.hash===sha)) {ignored++;continue;}
          const audit=Array.isArray(raw.falas)?raw:null;
          const d={name:path,hash:sha,view,audit,entries:Object.create(null),rows:view.participantes.flatMap(p=>p.falas.map(f=>({person:p.nome,f})))};
          d.rows.sort((a,b)=>(a.f.ordem_no_debate||0)-(b.f.ordem_no_debate||0));
          if (!audit && typeof raw.arquivo_auditoria==='string' && !raw.arquivo_auditoria.startsWith('/') && !raw.arquivo_auditoria.split('/').includes('..')) {
            const base=path.includes('/')?path.slice(0,path.lastIndexOf('/')+1):'';
            const companion=byPath.get(base+raw.arquivo_auditoria);
            if (companion) {
              try {
                const data=JSON.parse(await companion.text()),av=normalizeDebate(data);
                const flat=av.participantes.flatMap(p=>p.falas.map(f=>({person:p.nome,f})));
                if (data.tema!==view.tema || data.debate_id!==view.debate_id || flat.length!==d.rows.length || !d.rows.every(r=>flat.some(x=>x.f.id===r.f.id && x.person===r.person && x.f.texto===r.f.texto && !annotationChanged(sourceAnnotation(x.f,guide),sourceAnnotation(r.f,guide),guide)))) throw Error('Auditoria divergente do resultado.');
                d.audit=data;
              } catch {unavailable++;}
            }
          }
          try {
            const saved=localStorage.getItem(key(d));
            if (saved) d.entries=checkedEntries(JSON.parse(saved),d);
          } catch {errors.push(file.name+': rascunho local incompatível; não foi restaurado.');}
          state.debates.push(d);added++;
        } catch(e) {errors.push(file.name+': '+e.message);}
      }
      $('debate').replaceChildren(...state.debates.map((d,i)=>{const o=el('option',d.view.tema+' · '+d.name);o.value=i;return o;}));
      if (state.debates.length) {
        $('welcome').hidden=true;$('workspace').hidden=false;$('export').disabled=false;
        const idx=state.current ? state.debates.indexOf(state.current):0;
        $('debate').value=idx;selectDebate(idx);
      }
      notice(`${added} debate(s) adicionado(s). ${ignored} arquivo(s) auxiliar(es), repetido(s) ou não compatível(is) ignorado(s).`+(unavailable?` ${unavailable} auditoria(s) incompatível(is) não exibida(s).`:'')+(errors.length?' '+errors.join(' | '):''),errors.length>0||unavailable>0);
    } finally {state.loading=false;}
  }
  function selectDebate(i) {
    state.current=state.debates[i];state.selected=null;
    $('theme').textContent=state.current.view.tema;
    $('debateNotes').textContent=[state.current.name,...(state.current.view.notas_revisao||[])].join('\n');
    $('search').value='';renderList(true);
  }
  function filtered() {
    if(!state.current)return [];
    const q=$('search').value.toLocaleLowerCase('pt-BR'),filter=$('filter').value;
    return state.current.rows.filter(r => {
      const e=state.current.entries[r.f.id];
      return (!q||(r.person+' '+r.f.texto).toLocaleLowerCase('pt-BR').includes(q)) &&
        (filter==='all'||filter==='pending'&&e?.status!=='revisado'||filter==='reviewed'&&e?.status==='revisado'||filter==='flags'&&(r.f.pendencias_revisao||[]).length||filter==='uncertain'&&e?.status==='revisado'&&e.decisao==='inconclusivo');
    });
  }
  function renderList(select=false,keepCurrent=false) {
    const d=state.current,rows=filtered(),entries=Object.values(d.entries);
    if(keepCurrent && row() && !rows.some(r=>r.f.id===state.selected)) rows.unshift(row());
    const completed=entries.filter(e=>e.status==='revisado');
    $('coverage').textContent=`${completed.length} / ${d.rows.length} falas com decisão humana · ${completed.filter(e=>e.decisao==='inconclusivo').length} inconclusiva(s). Cobertura: ${Math.round(100*completed.length/d.rows.length)}%.`;
    $('speeches').replaceChildren();
    const groups=new Map();for(const r of rows){if(!groups.has(r.person))groups.set(r.person,[]);groups.get(r.person).push(r);}
    for (const [person,items] of groups) {
      $('speeches').append(el('div',person,'person'));
      for (const r of items) {
        const e=d.entries[r.f.id],b=el('button',null,'speech-link');b.dataset.id=r.f.id;
        b.append(el('span',`Fala ${r.f.ordem_no_debate ?? r.f.id}`),el('span',e?.status==='revisado'?({confirmado:'Confirmada',corrigido:'Corrigida',inconclusivo:'Inconclusiva'}[e.decisao]):e?'Rascunho':'Não revisada','status-tag'));
        b.classList.toggle('active',state.selected===r.f.id);b.onclick=()=>selectSpeech(r.f.id);$('speeches').append(b);
      }
    }
    if(select || !rows.some(r=>r.f.id===state.selected)) selectSpeech(rows[0]?.f.id??null);
  }
  function interruption(x,unlocated=false) {
    const n=el('details',null,'interruption');n.open=true;
    n.append(el('summary',`Interrupção · ${x.participante||'Participante não identificado'}${unlocated?' · posição indisponível':''}`),el('p',x.texto));
    n.append(el('p','Mudança de opinião na retomada: '+(x.mudanca_de_opiniao_na_retomada===true?'indicada pelo modelo':x.mudanca_de_opiniao_na_retomada===false?'não indicada':'não determinada')+'.','muted'));
    if(x.observacao)n.append(el('p',x.observacao));return n;
  }
  function renderText(f) {
    const n=$('transcript');n.replaceChildren();let pos=0;const parts=speechParts(f),chars=Array.from(f.texto);
    for(const x of parts.located){n.append(el('span',chars.slice(pos,x.apos_caractere_do_texto).join(''),'speech-segment'),interruption(x));pos=x.apos_caractere_do_texto;}
    n.append(el('span',chars.slice(pos).join(''),'speech-segment'));
    for(const x of parts.unlocated)n.append(interruption(x,true));
  }
  function renderAudit(f) {
    const n=$('audit');n.replaceChildren();
    const a=state.current.audit?.falas?.find(x=>x.id===f.id)?.anotacao;
    if(!a){n.append(el('p','Justificativas indisponíveis. Abra a pasta que contém o resultado e sua subpasta auditoria. A revisão continua disponível sem esses detalhes.','muted'));return;}
    for(const [name,data] of Object.entries(a.dimensoes||{})) {
      const section=el('section',null,'audit-card');section.append(el('h3',name),el('p',data.justificativa||'Sem justificativa.'));
      for(const e of data.evidencias||[]) {
        section.append(el('div',`${e.indicador||'Evidência'} · ${e.status==='correspondencia_textual_verificada'?'correspondência textual verificada; classificação não validada':e.status||'sem verificação informada'}`,'muted'));
        if(e.trecho)section.append(el('blockquote',e.trecho));
        if(e.motivo)section.append(el('p',e.motivo,'flag'));
        if(e.recebida){const detail=el('details');detail.append(el('summary','Evidência recebida'),el('pre',JSON.stringify(e.recebida,null,2)));section.append(detail);}
      }
      for(const p of data.pendencias||[])section.append(el('p',p,'flag'));
      n.append(section);
    }
    const extra=el('details');extra.append(el('summary','Opinião e propostas registradas na auditoria'),el('pre',JSON.stringify({opiniao:a.opiniao,propostas:a.propostas},null,2)));n.append(extra);
  }
  function selectSpeech(id) {
    state.selected=id;
    $('empty').hidden=!!id;$('speechContent').hidden=!id;$('annotation').hidden=!id;
    for(const b of $('speeches').querySelectorAll('button'))b.classList.toggle('active',b.dataset.id===id);
    if(!id)return;
    const r=row(),f=r.f,e=state.current.entries[id],value=e||sourceAnnotation(f,guide);
    const rows=filtered(),idx=rows.findIndex(x=>x.f.id===id);
    $('previous').disabled=idx<=0;$('next').disabled=idx===rows.length-1;
    $('speaker').textContent=r.person;$('speechOrder').textContent=`FALA ${f.ordem_no_debate ?? f.id} · ${f.id} · ${f.texto.length.toLocaleString('pt-BR')} caracteres`;
    $('flags').replaceChildren(...(f.pendencias_revisao||[]).map(p=>el('p',p,'flag')));
    if(!f.taxonomia)$('flags').append(el('p','Esta fala ainda não contém classificação do modelo.','flag'));
    renderText(f);renderAudit(f);setTab(false);
    $('taxonomy').replaceChildren();
    for(const d of guide) {
      const section=el('section',null,'dimension');section.append(el('h3',d.dimension));
      const details=el('details');details.append(el('summary','Consultar critérios atuais'),el('p',d.classification_object),el('p',d.orientacao));section.append(details);
      section.append(el('p',`Modelo: ${f.taxonomia===null||f.taxonomia===undefined?'não anotado':(f.taxonomia[d.dimension]||[]).join(', ')||'sem indicador'}`,'original'));
      const choices=el('div',null,'choices');
      for(const label of d.indicators) {
        const wrap=el('label',null,'choice'),input=el('input');input.type='checkbox';input.value=label;input.dataset.dimension=d.dimension;input.dataset.dimensionId=d.id;input.checked=value.taxonomia[d.dimension].includes(label);
        input.onchange=()=>{if(input.checked&&[1,4].includes(d.id))for(const other of choices.querySelectorAll('input'))if(other!==input)other.checked=false;draft();};
        wrap.append(input,el('span',label));choices.append(wrap);
      }
      section.append(choices,el('p',[1,4].includes(d.id)?'Até um indicador. Desmarque para deixar sem rótulo.':'Múltiplos indicadores permitidos; nenhum é obrigatório.','muted'));$('taxonomy').append(section);
    }
    $('summary').value=value.resumo;$('proposals').value=value.propostas.join('\n');
    $('originalSummary').textContent=f.resumo??'Resumo não gerado.';$('originalProposals').textContent=f.propostas===null||f.propostas===undefined?'Extração ainda não realizada.':f.propostas.join('\n')||'Nenhuma proposta extraída.';
    $('proposalHint').textContent='Lista vazia significa nenhuma proposta. Corrija aqui somente as medidas efetivamente defendidas.';
    $('segmentation').value=e?.agrupamento||'nao_avaliado';$('notes').value=e?.observacoes||'';$('decision').value=e?.decisao||'';
    $('saveStatus').textContent=e?.status==='revisado'?`Decisão registrada por ${e.validador}. Exporte para guardar uma cópia.`:e?'Rascunho restaurado; ainda sem decisão registrada.':'Fala ainda não revisada.';
  }
  function capture() {
    const r=row(),baseline=state.current.entries[state.selected]||sourceAnnotation(r.f,guide);return {criterios_sha256:guideHash,fala_id:r.f.id,participante:r.person,status:'rascunho',decisao:$('decision').value,
      taxonomia:Object.fromEntries(guide.map(d=>[d.dimension,[...$('taxonomy').querySelectorAll('input:checked')].filter(x=>x.dataset.dimension===d.dimension).map(x=>x.value)])),
      resumo:$('summary').value,propostas:$('proposals').value===baseline.propostas.join('\n')?[...baseline.propostas]:$('proposals').value.split('\n').map(s=>s.trim()).filter(Boolean),
      agrupamento:$('segmentation').value,observacoes:$('notes').value,validador:$('reviewer').value.trim(),atualizado_em:new Date().toISOString()};
  }
  function draft() {
    if(!row())return;
    state.current.entries[state.selected]=capture();state.dirty=true;
    const stored=persist();$('saveStatus').textContent=stored?'Rascunho salvo neste navegador. Registre a decisão quando terminar.':'Rascunho apenas nesta sessão. Exporte antes de sair.';
    renderList(false,true);
  }
  function setTab(audit){$('transcript').hidden=audit;$('audit').hidden=!audit;$('textTab').classList.toggle('selected',!audit);$('auditTab').classList.toggle('selected',audit);}
  $('textTab').onclick=()=>setTab(false);$('auditTab').onclick=()=>setTab(true);
  $('files').onchange=e=>loadFiles(e.target.files).catch(e=>notice(e.message,true));$('folder').onchange=$('files').onchange;
  $('debate').onchange=()=>selectDebate(Number($('debate').value));$('search').oninput=()=>renderList();$('filter').onchange=()=>renderList();
  for(const id of ['summary','proposals','notes'])$(id).oninput=draft;
  for(const id of ['segmentation','decision'])$(id).onchange=draft;
  $('reviewer').onchange=()=>{if(row()&&state.current.entries[state.selected]?.status==='rascunho')draft();};
  for(const [id,step] of [['previous',-1],['next',1]])$(id).onclick=()=>{const rows=filtered(),idx=rows.findIndex(r=>r.f.id===state.selected);if(rows[idx+step])selectSpeech(rows[idx+step].f.id);};
  $('save').onclick=()=>{
    try {const e=capture();e.status='revisado';validateEntry(e,row(),guide);state.current.entries[state.selected]=e;state.dirty=true;const stored=persist();renderList(false,true);$('saveStatus').textContent=stored?'Decisão registrada neste navegador. Exporte a revisão para guardar uma cópia.':'Decisão registrada somente nesta sessão. Exporte agora.';}
    catch(e){$('saveStatus').textContent=e.message;}
  };
  $('export').onclick=()=>{
    const result={formato:REVIEW_FORMAT,criterios_atuais:{sha256:guideHash,dimensoes:guide},exportado_em:new Date().toISOString(),unidade:'fala_integral',
      observacao:'Somente falas com status revisado têm decisão humana registrada; decisão inconclusiva não confirma a anotação. Falas ausentes não foram revisadas. Rascunhos não são validações. Este arquivo não altera os originais nem estima a qualidade do corpus.',
      debates:state.debates.map(bundle)};
    const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'})),a=el('a');a.href=url;a.download='revisao_humana_'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);state.dirty=false;notice('Download da revisão solicitado. Guarde esse JSON para retomar ou analisar as decisões.');
  };
  $('import').onchange=async event=>{
    try {
      const file=event.target.files[0];if(!file)return;
      if(!state.debates.length)throw Error('Abra primeiro os debates originais correspondentes à revisão.');
      const data=JSON.parse(await file.text());if(data.formato!==REVIEW_FORMAT||!Array.isArray(data.debates))throw Error('Formato de revisão não reconhecido.');
      const staged=[],seen=new Set();let conflicts=0;
      for(const b of data.debates){if(seen.has(b.sha256))throw Error('Debate repetido na revisão.');seen.add(b.sha256);const d=state.debates.find(x=>x.hash===b.sha256);if(!d)throw Error('Abra o arquivo original correspondente a '+b.arquivo+'. Nenhuma revisão foi importada.');const entries=checkedEntries(b,d);conflicts+=Object.keys(entries).filter(id=>d.entries[id]&&JSON.stringify(d.entries[id])!==JSON.stringify(entries[id])).length;staged.push({d,entries});}
      if(conflicts&&!window.confirm(`Há ${conflicts} registro(s) local(is) diferente(s). Substituir pelas versões do arquivo importado?`))return;
      const current=state.current;
      for(const {d,entries} of staged){Object.assign(d.entries,entries);state.current=d;persist();}state.current=current;state.dirty=true;renderList();if(state.selected)selectSpeech(state.selected);notice('Revisão importada e vinculada aos arquivos originais por SHA-256.');
    } catch(e){notice(e.message,true);}finally{event.target.value='';}
  };
  window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue='';}});
}
