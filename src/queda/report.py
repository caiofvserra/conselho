"""Gera output/report.html (autocontido) + output/csv/<finding>.csv.

Cada achado com série anual vira um gráfico próprio no dashboard — o eixo
temporal é obrigatório em toda métrica do projeto.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import date, datetime

import pandas as pd

from . import config
from .findings import Finding

log = logging.getLogger("queda.report")


def _formatar_datas(d: pd.DataFrame) -> pd.DataFrame:
    """Converte qualquer data para texto ISO.

    Colunas `object` contendo `datetime.date` eram serializadas por `to_json`
    como epoch em milissegundos — no dashboard e no CSV a data aparecia como
    "1413504000000". Só checar o dtype datetime64 não bastava.
    """
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%Y-%m-%d")
        elif d[c].dtype == object:
            amostra = d[c].dropna()
            if not amostra.empty and isinstance(amostra.iloc[0], (date, datetime)):
                d[c] = d[c].map(lambda v: v.isoformat()[:10]
                                if isinstance(v, (date, datetime)) else v)
    return d


def _records(df: pd.DataFrame | None, limit: int = 30) -> list[dict]:
    if df is None or df.empty:
        return []
    head = _formatar_datas(df.head(limit).copy())
    head.columns = [str(c) for c in head.columns]
    return json.loads(head.to_json(orient="records", force_ascii=False))


def gerar(achados: list[Finding], staged: dict, *, resumo_llm: str = "") -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_dir = config.OUTPUT_DIR / "csv"
    # limpa a evidência da execução anterior: sem isso, CSVs de uma rodada de
    # teste continuavam ao lado da evidência real, com data e conteúdo antigos
    if csv_dir.exists():
        shutil.rmtree(csv_dir)
    csv_dir.mkdir(parents=True)

    payload_findings = []
    for f in achados:
        if f.evidencia is not None and not f.evidencia.empty:
            _formatar_datas(f.evidencia.copy()).to_csv(
                csv_dir / f"{f.id}.csv", index=False)
        anual = None
        if f.tabela_anual is not None and not f.tabela_anual.empty:
            _formatar_datas(f.tabela_anual.copy()).to_csv(
                csv_dir / f"{f.id}--anual.csv", index=False)
            anual = _records(f.tabela_anual, 40)
        payload_findings.append({
            **f.resumo(),
            "amostra": _records(f.evidencia),
            "tabela_anual": anual,
            "csv": f"csv/{f.id}.csv" if f.n_linhas else None,
        })

    resumo_staged = {k: int(len(v)) for k, v in staged.items()
                     if isinstance(v, pd.DataFrame)}
    payload = {
        "gerado_em": date.today().isoformat(),
        "staged": resumo_staged,
        "findings": payload_findings,
        "frentes": sorted({f.frente for f in achados}),
        "resumo_llm": resumo_llm,
    }

    chartjs = (config.ASSETS_DIR / "chart.umd.js").read_text()
    html = _TEMPLATE.replace("/*__CHARTJS__*/", chartjs) \
                    .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    (config.OUTPUT_DIR / "report.html").write_text(html)
    log.info("output/report.html gerado com %d achados", len(achados))


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QUEDA — Fiscalização Crea-SP</title>
<style>
:root{--bg:#0f1419;--panel:#1a2129;--line:#2a333d;--txt:#e6e6e6;--mut:#8b98a5;
--alta:#e5484d;--media:#f5a524;--baixa:#4d9de0;--info:#6b7785;--acc:#38b26f}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--txt);font:15px/1.55 system-ui,Segoe UI,Roboto,sans-serif}
header{padding:22px 32px;border-bottom:1px solid var(--line)}
h1{font-size:21px}h1 small{color:var(--mut);font-weight:400;font-size:13px;margin-left:8px}
nav{display:flex;gap:6px;flex-wrap:wrap;padding:12px 32px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:var(--bg);z-index:5}
nav button{background:none;border:1px solid var(--line);color:var(--txt);padding:7px 13px;
border-radius:8px;cursor:pointer;font-size:13.5px}
nav button:hover{border-color:var(--acc)}
nav button.on{background:var(--acc);border-color:var(--acc);color:#06130c;font-weight:600}
main{padding:24px 32px 60px;max-width:1240px;margin:auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border-radius:12px;padding:15px}
.card .n{font-size:26px;font-weight:700}.card .l{color:var(--mut);font-size:12.5px}
.finding{background:var(--panel);border-radius:12px;padding:18px;margin-bottom:16px;
border-left:4px solid var(--info)}
.finding.alta{border-color:var(--alta)}.finding.media{border-color:var(--media)}
.finding.baixa{border-color:var(--baixa)}
.badge{display:inline-block;font-size:10.5px;font-weight:700;text-transform:uppercase;
padding:2px 8px;border-radius:99px;margin-right:8px;color:#fff}
.badge.alta{background:var(--alta)}.badge.media{background:var(--media);color:#221a00}
.badge.baixa{background:var(--baixa)}.badge.info{background:var(--info)}
.finding h3{font-size:16.5px;margin:7px 0}
.finding p.desc{color:#c3ccd4;font-size:13.5px}
.meta{margin-top:10px;font-size:12.5px;color:var(--mut);border-top:1px solid var(--line);padding-top:8px}
.meta b{color:#a9b4bf}
details{margin-top:10px}summary{cursor:pointer;color:var(--acc);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:12px;margin-top:8px;display:block;
overflow-x:auto;white-space:nowrap;max-height:420px}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left}
th{background:#232c36;position:sticky;top:0}
a{color:var(--acc)}
.chartwrap{margin:12px 0 4px;background:#131920;border-radius:8px;padding:10px}
canvas.net{width:100%;height:520px;background:#0b0f14;border-radius:8px}
.aviso{background:#1f2933;border-left:3px solid var(--media);border-radius:8px;
padding:11px 14px;font-size:13px;color:#c3ccd4;margin-bottom:20px}
.resumo{background:var(--panel);border-radius:12px;padding:18px;margin-bottom:20px;
font-size:14px;line-height:1.65}
.resumo h3{font-size:15px;margin-bottom:8px;color:var(--acc)}
.resumo p{margin-bottom:10px}
</style></head><body>
<header><h1>QUEDA <small>fiscalização de dados abertos · Crea-SP · gerado em
<span id="gdate"></span></small></h1></header>
<nav id="tabs"></nav><main id="main"></main>
<script>/*__CHARTJS__*/</script>
<script>
const P = __PAYLOAD__;
document.getElementById('gdate').textContent = P.gerado_em;
const GRAV=['alta','media','baixa','info'], PAL=['#38b26f','#f5a524','#4d9de0','#e5484d','#9b8afb','#e07a5f','#6ec7c1','#c0ca33','#8b98a5'];
const nav=document.getElementById('tabs'), main=document.getElementById('main');
let charts=[];
['Visão geral',...P.frentes].forEach((t,i)=>{const b=document.createElement('button');
 b.textContent=t;b.onclick=()=>show(t,b);nav.appendChild(b);if(!i)setTimeout(()=>show(t,b));});
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function brl(v){return typeof v==='number'?v.toLocaleString('pt-BR',{style:'currency',currency:'BRL',maximumFractionDigits:0}):esc(v)}
function tbl(rows){if(!rows||!rows.length)return'';const c=Object.keys(rows[0]);
 return '<table><tr>'+c.map(x=>`<th>${esc(x)}</th>`).join('')+'</tr>'+
 rows.map(r=>'<tr>'+c.map(x=>`<td>${esc(r[x])}</td>`).join('')+'</tr>').join('')+'</table>'}
function card(f,idx){
 const cid='ch_'+f.id.replace(/[^a-z0-9]/g,'')+'_'+idx;
 return `<div class="finding ${f.gravidade}">
 <span class="badge ${f.gravidade}">${f.gravidade}</span>
 <span style="color:var(--mut);font-size:11.5px">${esc(f.frente)}</span>
 <h3>${esc(f.titulo)}</h3><p class="desc">${esc(f.descricao)}</p>
 ${f.serie?`<div class="chartwrap"><canvas id="${cid}" height="95"></canvas></div>`:''}
 ${(f.base_legal||f.como_verificar)?`<div class="meta">
   ${f.base_legal?`<div><b>Base legal:</b> ${esc(f.base_legal)}</div>`:''}
   ${f.como_verificar?`<div style="margin-top:4px"><b>Como apurar:</b> ${esc(f.como_verificar)}</div>`:''}
  </div>`:''}
 ${f.tabela_anual&&f.tabela_anual.length?`<details><summary>tabela ano a ano</summary>${tbl(f.tabela_anual)}</details>`:''}
 ${f.amostra.length?`<details><summary>evidência (${f.linhas_evidencia} linhas${f.csv?` — <a href="${f.csv}">CSV completo</a>`:''})</summary>${tbl(f.amostra)}</details>`:''}
 </div>`}
function drawSerie(f,idx){
 if(!f.serie)return; const cid='ch_'+f.id.replace(/[^a-z0-9]/g,'')+'_'+idx;
 const el=document.getElementById(cid); if(!el)return;
 const s=f.serie, stacked=s.tipo.includes('stacked'), area=s.tipo==='area_stacked';
 const kind=(s.tipo==='line'||area)?'line':'bar';
 charts.push(new Chart(el,{type:kind,
  data:{labels:s.anos,datasets:Object.entries(s.series).map(([k,v],i)=>({
   label:k,data:v,backgroundColor:PAL[i%PAL.length],borderColor:PAL[i%PAL.length],
   fill:area,tension:.25,borderWidth:kind==='line'?2:0}))},
  options:{indexAxis:s.tipo==='barh'?'y':'x',responsive:true,
   scales:{x:{stacked,ticks:{color:'#8b98a5'},grid:{color:'#232c36'}},
           y:{stacked,ticks:{color:'#8b98a5',callback:v=>s.unidade==='R$'?(v/1e6).toFixed(0)+'M':v},
              grid:{color:'#232c36'}}},
   plugins:{legend:{labels:{color:'#c3ccd4',boxWidth:12,font:{size:11}}},
    tooltip:{callbacks:{label:c=>c.dataset.label+': '+(s.unidade==='R$'?brl(c.parsed.y??c.parsed.x):(c.parsed.y??c.parsed.x)+' '+s.unidade)}}}}}))}
function show(tab,btn){
 charts.forEach(c=>c.destroy());charts=[];
 [...nav.children].forEach(b=>b.classList.toggle('on',b===btn));
 const fs = tab==='Visão geral'
   ? P.findings.filter(f=>f.gravidade==='alta')
   : P.findings.filter(f=>f.frente===tab);
 let html='';
 if(tab==='Visão geral'){
  const by=Object.fromEntries(GRAV.map(g=>[g,P.findings.filter(f=>f.gravidade===g).length]));
  html+=`<div class="cards">${GRAV.map(g=>`<div class="card"><div class="n" style="color:var(--${g})">${by[g]}</div><div class="l">achados · ${g}</div></div>`).join('')}
   <div class="card"><div class="n">${Object.values(P.staged).reduce((a,b)=>a+b,0).toLocaleString('pt-BR')}</div><div class="l">registros analisados</div></div></div>
  <div class="aviso">Ferramenta de <b>triagem</b> sobre dados públicos. Todo achado é <b>indício</b>, não prova:
  homonímia, falha de cadastro e limitação das APIs geram falsos positivos. Confirme cada caso na fonte
  antes de qualquer denúncia. Coleta anônima, sem autenticação.</div>`;
  if(P.resumo_llm) html+=`<div class="resumo"><h3>Resumo executivo</h3>${
    P.resumo_llm.split(/\n\n+/).map(p=>`<p>${esc(p)}</p>`).join('')}</div>`;
  const pan=P.findings.find(f=>f.id==='finalistica-panorama');
  if(pan) html+=card(pan,900);
  html+=`<h3 style="margin:18px 0 10px;font-size:15px">Achados de gravidade alta</h3>`;
 }
 if(tab.startsWith('Rede')) html+='<div class="chartwrap"><canvas id="net" class="net"></canvas><p style="color:var(--mut);font-size:11.5px;margin-top:6px">verde = órgão · azul = fornecedor · laranja = pessoa (sócio) · roxo = convênio — arraste os nós</p></div>';
 html+=fs.map((f,i)=>card(f,i)).join('')||'<p style="color:var(--mut)">Nenhum achado nesta frente.</p>';
 main.innerHTML=html;
 if(tab==='Visão geral'&&P.findings.find(f=>f.id==='finalistica-panorama'))
   drawSerie(P.findings.find(f=>f.id==='finalistica-panorama'),900);
 fs.forEach((f,i)=>drawSerie(f,i));
 if(tab.startsWith('Rede')) netGraph();
}
function netGraph(){
 const f=P.findings.find(x=>x.id==='rede-grafo'); const m=f&&f.metricas;
 if(!m||!m.nodes)return;
 const cv=document.getElementById('net'),ctx=cv.getContext('2d');
 const W=cv.width=cv.clientWidth,H=cv.height=520;
 const COLOR={orgao:'#38b26f',fornecedor:'#4d9de0',pessoa:'#f5a524',convenio:'#9b8afb'};
 const nodes=m.nodes.map((n,i)=>({...n,x:W/2+Math.cos(i*2.4)*(80+i%7*40),y:H/2+Math.sin(i*2.4)*(60+i%5*40),vx:0,vy:0}));
 const idx=Object.fromEntries(nodes.map((n,i)=>[n.id,i]));
 const edges=(m.edges||[]).filter(e=>idx[e.from]!=null&&idx[e.to]!=null);
 let drag=null;
 cv.onmousedown=e=>{const r=cv.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;
  drag=nodes.find(n=>(n.x-x)**2+(n.y-y)**2<140)};
 cv.onmousemove=e=>{if(!drag)return;const r=cv.getBoundingClientRect();drag.x=e.clientX-r.left;drag.y=e.clientY-r.top};
 cv.onmouseup=()=>drag=null; cv.onmouseleave=()=>drag=null;
 function step(){
  for(const a of nodes){a.vx*=.85;a.vy*=.85;
   for(const b of nodes){if(a===b)continue;let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy+.01,ff=900/d2;
    a.vx+=dx*ff/Math.sqrt(d2);a.vy+=dy*ff/Math.sqrt(d2)}
   a.vx+=(W/2-a.x)*.0015;a.vy+=(H/2-a.y)*.0015}
  for(const e of edges){const s=nodes[idx[e.from]],t=nodes[idx[e.to]];
   let dx=t.x-s.x,dy=t.y-s.y,d=Math.sqrt(dx*dx+dy*dy)+.01,ff=(d-95)*.004;
   s.vx+=dx/d*ff*d;s.vy+=dy/d*ff*d;t.vx-=dx/d*ff*d;t.vy-=dy/d*ff*d}
  for(const n of nodes){if(n!==drag){n.x+=n.vx;n.y+=n.vy}
   n.x=Math.max(14,Math.min(W-14,n.x));n.y=Math.max(14,Math.min(H-14,n.y))}
  ctx.clearRect(0,0,W,H);ctx.strokeStyle='#232c36';
  for(const e of edges){const s=nodes[idx[e.from]],t=nodes[idx[e.to]];
   ctx.beginPath();ctx.moveTo(s.x,s.y);ctx.lineTo(t.x,t.y);ctx.stroke()}
  for(const n of nodes){ctx.fillStyle=COLOR[n.tipo]||'#8b98a5';ctx.beginPath();
   ctx.arc(n.x,n.y,n.tipo==='orgao'?9:5,0,7);ctx.fill();
   if(n.tipo==='orgao'||nodes.length<70){ctx.fillStyle='#8b98a5';ctx.font='10px sans-serif';
    ctx.fillText(n.label,n.x+8,n.y+3)}}
  requestAnimationFrame(step)}
 step()}
</script></body></html>
"""
