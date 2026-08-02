"""Gera output/report.html (autocontido) + output/csv/<finding>.csv."""
from __future__ import annotations

import json
import logging
from datetime import date

import pandas as pd

from . import config
from .findings import Finding

log = logging.getLogger("queda.report")


def _records(df: pd.DataFrame | None, limit: int = 25) -> list[dict]:
    if df is None or df.empty:
        return []
    head = df.head(limit).copy()
    for c in head.columns:
        if pd.api.types.is_datetime64_any_dtype(head[c]):
            head[c] = head[c].dt.strftime("%Y-%m-%d")
    return json.loads(head.to_json(orient="records", force_ascii=False))


def gerar(achados: list[Finding], staged: dict) -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_dir = config.OUTPUT_DIR / "csv"
    csv_dir.mkdir(exist_ok=True)

    payload_findings = []
    for f in achados:
        if f.evidencia is not None and not f.evidencia.empty:
            f.evidencia.to_csv(csv_dir / f"{f.id}.csv", index=False)
        payload_findings.append({
            **f.resumo(),
            "amostra": _records(f.evidencia),
            "csv": f"csv/{f.id}.csv" if f.n_linhas else None,
        })

    resumo_staged = {k: int(len(v)) for k, v in staged.items()
                     if isinstance(v, pd.DataFrame)}
    payload = {
        "gerado_em": date.today().isoformat(),
        "staged": resumo_staged,
        "findings": payload_findings,
        "frentes": sorted({f.frente for f in achados}),
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
:root{--bg:#0f1419;--panel:#1a2129;--txt:#e6e6e6;--mut:#8b98a5;--alta:#e5484d;
--media:#f5a524;--baixa:#4d9de0;--info:#6b7785;--acc:#38b26f}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
header{padding:24px 32px;border-bottom:1px solid #2a333d}
h1{font-size:22px}h1 small{color:var(--mut);font-weight:400;font-size:14px;margin-left:8px}
nav{display:flex;gap:4px;flex-wrap:wrap;padding:12px 32px;border-bottom:1px solid #2a333d}
nav button{background:none;border:1px solid #2a333d;color:var(--txt);padding:8px 14px;
border-radius:8px;cursor:pointer;font-size:14px}
nav button.on{background:var(--acc);border-color:var(--acc);color:#06130c;font-weight:600}
main{padding:24px 32px;max-width:1280px;margin:auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.card{background:var(--panel);border-radius:12px;padding:16px}
.card .n{font-size:28px;font-weight:700}.card .l{color:var(--mut);font-size:13px}
.finding{background:var(--panel);border-radius:12px;padding:18px;margin-bottom:16px;
border-left:4px solid var(--info)}
.finding.alta{border-color:var(--alta)}.finding.media{border-color:var(--media)}
.finding.baixa{border-color:var(--baixa)}
.badge{display:inline-block;font-size:11px;font-weight:700;text-transform:uppercase;
padding:2px 8px;border-radius:99px;margin-right:8px}
.badge.alta{background:var(--alta)}.badge.media{background:var(--media);color:#221a00}
.badge.baixa{background:var(--baixa)}.badge.info{background:var(--info)}
.finding h3{font-size:16px;margin:6px 0}.finding p{color:var(--mut);font-size:13.5px}
details{margin-top:10px}summary{cursor:pointer;color:var(--acc);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:8px;display:block;
overflow-x:auto;white-space:nowrap}
th,td{border:1px solid #2a333d;padding:4px 8px;text-align:left}
th{background:#232c36;position:sticky;top:0}
a{color:var(--acc)}.chartbox{background:var(--panel);border-radius:12px;padding:16px;
margin-bottom:20px}.chartbox h3{font-size:15px;margin-bottom:8px}
canvas.net{width:100%;height:520px;background:#0b0f14;border-radius:8px}
.aviso{background:#1f2933;border-radius:8px;padding:10px 14px;font-size:13px;
color:var(--mut);margin-bottom:20px}
</style></head><body>
<header><h1>QUEDA <small>fiscalização de dados abertos · Crea-SP · gerado em
<span id="gdate"></span></small></h1></header>
<nav id="tabs"></nav><main id="main"></main>
<script>/*__CHARTJS__*/</script>
<script>
const P = __PAYLOAD__;
document.getElementById('gdate').textContent = P.gerado_em;
const GRAV = ['alta','media','baixa','info'];
const tabs = ['Visão geral', ...P.frentes];
const nav = document.getElementById('tabs'), main = document.getElementById('main');
let charts = [];
tabs.forEach((t,i)=>{const b=document.createElement('button');b.textContent=t;
 b.onclick=()=>show(t,b);nav.appendChild(b);if(i===0)setTimeout(()=>show(t,b));});
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function tbl(rows){if(!rows||!rows.length)return'';const cols=Object.keys(rows[0]);
 return '<table><tr>'+cols.map(c=>`<th>${esc(c)}</th>`).join('')+'</tr>'+
 rows.map(r=>'<tr>'+cols.map(c=>`<td>${esc(r[c])}</td>`).join('')+'</tr>').join('')+'</table>'}
function findingCard(f){return `<div class="finding ${f.gravidade}">
 <span class="badge ${f.gravidade}">${f.gravidade}</span>
 <span style="color:var(--mut);font-size:12px">${esc(f.frente)}</span>
 <h3>${esc(f.titulo)}</h3><p>${esc(f.descricao)}</p>
 ${f.amostra.length?`<details><summary>evidência (${f.linhas_evidencia} linhas${f.csv?` — <a href="${f.csv}">CSV completo</a>`:''})</summary>${tbl(f.amostra)}</details>`:''}</div>`}
function show(tab,btn){charts.forEach(c=>c.destroy());charts=[];
 [...nav.children].forEach(b=>b.classList.toggle('on',b===btn));
 if(tab==='Visão geral')return overview();
 const fs=P.findings.filter(f=>f.frente===tab);
 let html='';
 if(tab.startsWith('Diárias')) html+='<div class="chartbox"><h3>Diárias/indenizações por ano e categoria</h3><canvas id="c1" height="110"></canvas></div>';
 if(tab.startsWith('Custo')) html+='<div class="chartbox"><h3>Série histórica por conta contábil</h3><canvas id="c2" height="110"></canvas></div>';
 if(tab.startsWith('Empenhos')) html+='<div class="chartbox"><h3>Top fornecedores (valor pago)</h3><canvas id="c3" height="130"></canvas></div>';
 if(tab.startsWith('Rede')) html+='<div class="chartbox"><h3>Grafo da rede</h3><canvas id="net" class="net"></canvas><p style="color:var(--mut);font-size:12px">verde=órgão · azul=fornecedor · laranja=pessoa (sócio) · roxo=convênio — arraste para reorganizar</p></div>';
 html+=fs.map(findingCard).join('');
 main.innerHTML=html||'<p>Nenhum achado nesta frente.</p>';
 if(tab.startsWith('Diárias')) chartSerie('c1','diarias-serie-anual',true);
 if(tab.startsWith('Custo')) chartSerie('c2','pessoal-serie-historica',false);
 if(tab.startsWith('Empenhos')) chartTop('c3');
 if(tab.startsWith('Rede')) netGraph();
}
function overview(){const by=Object.fromEntries(GRAV.map(g=>[g,P.findings.filter(f=>f.gravidade===g).length]));
 main.innerHTML=`<div class="cards">${GRAV.map(g=>`<div class="card"><div class="n" style="color:var(--${g})">${by[g]}</div><div class="l">achados · gravidade ${g}</div></div>`).join('')}
 <div class="card"><div class="n">${Object.values(P.staged).reduce((a,b)=>a+b,0).toLocaleString('pt-BR')}</div><div class="l">registros coletados</div></div></div>
 <div class="aviso">Ferramenta de triagem sobre dados públicos. Todo achado é <b>indício</b>, não prova:
 homonímias, falhas de cadastro e limitações das APIs produzem falsos positivos. Confirme cada caso
 na fonte antes de qualquer denúncia. Coleta anônima: GETs públicos, sem autenticação.</div>
 <div class="chartbox"><h3>Registros por conjunto de dados</h3>${tbl(Object.entries(P.staged).map(([k,v])=>({conjunto:k,linhas:v})))}</div>
 ${P.findings.filter(f=>f.gravidade==='alta').map(findingCard).join('')}`}
function met(id){const f=P.findings.find(f=>f.id===id);return f&&f.metricas}
const PAL=['#38b26f','#f5a524','#4d9de0','#e5484d','#9b8afb','#8b98a5'];
function chartSerie(cv,id,stack){const m=met(id);if(!m||!m.serie)return;
 charts.push(new Chart(document.getElementById(cv),{type:'bar',
 data:{labels:m.anos,datasets:Object.entries(m.serie).map(([k,v],i)=>({label:k,data:v,backgroundColor:PAL[i%6]}))},
 options:{scales:{x:{stacked:stack},y:{stacked:stack}},plugins:{legend:{labels:{color:'#e6e6e6'}}}}}))}
function chartTop(cv){const m=met('empenhos-top-fornecedores');if(!m||!m.top)return;
 charts.push(new Chart(document.getElementById(cv),{type:'bar',
 data:{labels:m.top.map(t=>t.FAVORECIDO_NORM),datasets:[{label:'valor pago',data:m.top.map(t=>t.VALOR),backgroundColor:PAL[2]}]},
 options:{indexAxis:'y',plugins:{legend:{display:false}}}}))}
function netGraph(){const m=met('rede-grafo');if(!m||!m.nodes)return;
 const cv=document.getElementById('net'),ctx=cv.getContext('2d');
 const W=cv.width=cv.clientWidth,H=cv.height=520;
 const COLOR={orgao:'#38b26f',fornecedor:'#4d9de0',pessoa:'#f5a524',convenio:'#9b8afb'};
 const nodes=m.nodes.map((n,i)=>({...n,x:W/2+Math.cos(i*2.4)*(80+i%7*40),y:H/2+Math.sin(i*2.4)*(60+i%5*40),vx:0,vy:0}));
 const idx=Object.fromEntries(nodes.map((n,i)=>[n.id,i]));
 const edges=m.edges.filter(e=>idx[e.from]!=null&&idx[e.to]!=null);
 let drag=null;
 cv.onmousedown=e=>{const r=cv.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;
  drag=nodes.find(n=>(n.x-x)**2+(n.y-y)**2<120)};
 cv.onmousemove=e=>{if(!drag)return;const r=cv.getBoundingClientRect();drag.x=e.clientX-r.left;drag.y=e.clientY-r.top};
 cv.onmouseup=()=>drag=null;
 function step(){for(const a of nodes){a.vx*=.85;a.vy*=.85;
  for(const b of nodes){if(a===b)continue;let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy+0.01,f=900/d2;
   a.vx+=dx*f/Math.sqrt(d2);a.vy+=dy*f/Math.sqrt(d2)}
  a.vx+=(W/2-a.x)*0.0015;a.vy+=(H/2-a.y)*0.0015}
  for(const e of edges){const s=nodes[idx[e.from]],t=nodes[idx[e.to]];
   let dx=t.x-s.x,dy=t.y-s.y,d=Math.sqrt(dx*dx+dy*dy)+0.01,f=(d-90)*0.004;
   s.vx+=dx/d*f*d;s.vy+=dy/d*f*d;t.vx-=dx/d*f*d;t.vy-=dy/d*f*d}
  for(const n of nodes){if(n!==drag){n.x+=n.vx;n.y+=n.vy}
   n.x=Math.max(15,Math.min(W-15,n.x));n.y=Math.max(15,Math.min(H-15,n.y))}
  ctx.clearRect(0,0,W,H);ctx.strokeStyle='#2a333d';
  for(const e of edges){const s=nodes[idx[e.from]],t=nodes[idx[e.to]];
   ctx.beginPath();ctx.moveTo(s.x,s.y);ctx.lineTo(t.x,t.y);ctx.stroke()}
  for(const n of nodes){ctx.fillStyle=COLOR[n.tipo]||'#8b98a5';ctx.beginPath();
   ctx.arc(n.x,n.y,n.tipo==='orgao'?9:5,0,7);ctx.fill();
   if(n.tipo==='orgao'||nodes.length<80){ctx.fillStyle='#8b98a5';ctx.font='10px sans-serif';
    ctx.fillText(n.label,n.x+8,n.y+3)}}
  requestAnimationFrame(step)}
 step()}
</script></body></html>
"""
