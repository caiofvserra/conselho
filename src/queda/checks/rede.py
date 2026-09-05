"""Frente 5 — Rede de influência entre órgãos do sistema Confea/Crea.

Constrói um grafo órgãos ↔ fornecedores ↔ pessoas para expor:
- fornecedores que contratam com vários órgãos do sistema (Confea, Mútua, CREAs);
- sócios de fornecedores homônimos de conselheiros/funcionários do Crea-SP;
- sócios/endereços compartilhados entre fornecedores distintos do órgão;
- convênios e repasses a entidades recorrentes.
"""
from __future__ import annotations

import logging

import pandas as pd

from .. import analytics as A
from .. import config
from ..findings import Finding
from ..utils import fmt_brl, norm_nome
from .common import df, fichas_cnpj, qsa_nomes

log = logging.getLogger("queda.checks.rede")

FRENTE = "Rede de influência"

# Acima disso o QSA é de banco/estatal/S-A aberta: sócios em comum não indicam nada.
MAX_SOCIOS_QSA = 12


def _componentes(arestas: list[tuple[str, str]]) -> dict[str, int]:
    """Componentes conexos por union-find — sem dependência externa.

    A componente conexa da projeção fornecedor–sócio é o **grupo econômico de
    fato**: empresas formalmente distintas ligadas por pessoas em comum. É a
    unidade certa para medir concentração, porque duas "concorrentes" do mesmo
    grupo não concorrem.
    """
    pai: dict[str, str] = {}

    def find(x: str) -> str:
        pai.setdefault(x, x)
        while pai[x] != x:
            pai[x] = pai[pai[x]]
            x = pai[x]
        return x

    for a, b in arestas:
        ra, rb = find(a), find(b)
        if ra != rb:
            pai[ra] = rb
    raizes: dict[str, int] = {}
    saida: dict[str, int] = {}
    for no in pai:
        r = find(no)
        saida[no] = raizes.setdefault(r, len(raizes))
    return saida


def _intermediacao(arestas: list[tuple[str, str]]) -> pd.Series:
    """Betweenness dos nós — quem está no caminho entre os demais.

    Usa networkx quando disponível (presente no Colab); sem ele, cai para o
    grau de ligação, que responde à mesma pergunta de forma mais grosseira.
    """
    try:
        import networkx as nx
    except ImportError:
        log.info("networkx ausente — intermediação medida pelo grau de ligação")
        graus: dict[str, int] = {}
        for a, b in arestas:
            graus[a] = graus.get(a, 0) + 1
            graus[b] = graus.get(b, 0) + 1
        return pd.Series(graus, dtype=float).sort_values(ascending=False)
    g = nx.Graph()
    g.add_edges_from(arestas)
    if g.number_of_nodes() < 3:
        return pd.Series(dtype=float)
    bt = nx.betweenness_centrality(g, normalized=True)
    return pd.Series(bt, dtype=float).sort_values(ascending=False)


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    pncp = df(staged, "pncp_contratos")
    nodes: list[dict] = []
    edges: list[dict] = []

    # -------------------------------------------------- fornecedores compartilhados
    if not pncp.empty and "fornecedor_ni" in pncp:
        pncp = pncp[pncp["fornecedor_ni"].astype(str).str.len() == 14]
        grp = (pncp.groupby(["fornecedor_ni", "fornecedor_nome", "orgao_nome", "orgao_papel"])
               .agg(contratos=("valorGlobal", "size"), valor=("valorGlobal", "sum"))
               .reset_index())
        por_forn = grp.groupby(["fornecedor_ni", "fornecedor_nome"]).agg(
            orgaos=("orgao_nome", "nunique"), valor_total=("valor", "sum")).reset_index()
        compartilhados = por_forn[por_forn["orgaos"] > 1]
        tem_alvo = set(grp.loc[grp["orgao_papel"] == "alvo", "fornecedor_ni"])
        compartilhados = compartilhados[compartilhados["fornecedor_ni"].isin(tem_alvo)]
        if not compartilhados.empty:
            detalhe = grp[grp["fornecedor_ni"].isin(compartilhados["fornecedor_ni"])]
            achados.append(Finding(
                id="rede-fornecedores-compartilhados", frente=FRENTE, gravidade="media",
                titulo=f"{len(compartilhados)} fornecedor(es) do Crea-SP também contratam "
                       "com outros órgãos do sistema Confea/Crea",
                descricao="Mesma empresa vendendo para Crea-SP e Confea/Mútua/outros "
                          "CREAs (via PNCP). Não é ilícito por si, mas empresas que "
                          "circulam pelo sistema são o ponto de partida clássico de "
                          "esquemas em rede — conferir sócios e objetos.",
                evidencia=detalhe.sort_values("valor", ascending=False),
                metricas={"n": int(len(compartilhados))},
            ))
        # grafo: órgão -> fornecedor
        for org in grp["orgao_nome"].dropna().unique():
            nodes.append({"id": f"org:{org}", "label": org[:40], "tipo": "orgao"})
        for _, r in grp.iterrows():
            fid = f"forn:{r['fornecedor_ni']}"
            if not any(n["id"] == fid for n in nodes):
                nodes.append({"id": fid, "label": str(r["fornecedor_nome"])[:40],
                              "tipo": "fornecedor"})
            edges.append({"from": f"org:{r['orgao_nome']}", "to": fid,
                          "valor": float(r["valor"]), "tipo": "contrato"})

    # -------------------------------------------------- QSA × pessoas do conselho
    fichas = fichas_cnpj(staged)
    mand = df(staged, "mandatarios")
    quadro = df(staged, "quadro_pessoal")
    pessoas_conselho = set(mand.get("NOME_NORM", pd.Series(dtype=str)).dropna()) | \
        set(quadro.get("NOME_NORM", pd.Series(dtype=str)).dropna())
    socio_map: dict[str, list[str]] = {}
    homonimos = []
    for cnpj, ficha in fichas.items():
        razao = ficha.get("razao_social") or ficha.get("nome_fantasia") or cnpj
        socios = qsa_nomes(ficha)
        # Bancos, estatais e S/A de capital aberto têm dezenas de diretores no
        # QSA; incluí-los produzia falso positivo em massa na primeira rodada
        # (o quadro do Banco do Brasil "compartilhava" sócios com tudo).
        if len(socios) > MAX_SOCIOS_QSA:
            continue
        for socio in socios:
            socio_map.setdefault(socio, []).append(cnpj)
            if socio in pessoas_conselho:
                homonimos.append({"socio": socio, "cnpj_fornecedor": cnpj,
                                  "razao_social": razao,
                                  "vinculo_no_conselho":
                                      "conselheiro" if socio in set(mand.get("NOME_NORM", pd.Series(dtype=str)))
                                      else "quadro de pessoal"})
            edges.append({"from": f"forn:{cnpj}", "to": f"pessoa:{socio}",
                          "valor": 0, "tipo": "socio"})
            if not any(n["id"] == f"pessoa:{socio}" for n in nodes):
                nodes.append({"id": f"pessoa:{socio}", "label": socio.title()[:40],
                              "tipo": "pessoa"})
    if homonimos:
        achados.append(Finding(
            id="rede-socio-homonimo", frente=FRENTE, gravidade="alta",
            titulo=f"{len(homonimos)} sócio(s) de fornecedores homônimos de pessoas "
                   "ligadas ao Crea-SP",
            descricao="Sócio de empresa fornecedora com nome idêntico ao de "
                      "conselheiro/funcionário (comparação por nome normalizado — "
                      "homonímia precisa ser confirmada por CPF antes de qualquer "
                      "acusação). Potencial conflito de interesse / contratação vedada.",
            evidencia=pd.DataFrame(homonimos),
        ))

    # Filiais da mesma empresa compartilham CNPJ-raiz (8 primeiros dígitos) e
    # obviamente têm os mesmos sócios — não são "duas empresas".
    def _raizes(cnpjs) -> set[str]:
        return {c[:8] for c in cnpjs}

    socios_repetidos = {s: cs for s, cs in socio_map.items()
                        if len(_raizes(cs)) > 1}
    if socios_repetidos:
        ev = pd.DataFrame([{"socio": s,
                            "cnpjs": ", ".join(sorted(set(cs))),
                            "n_grupos_economicos": len(_raizes(cs))}
                           for s, cs in socios_repetidos.items()]).sort_values(
            "n_grupos_economicos", ascending=False)
        achados.append(Finding(
            id="rede-socios-compartilhados", frente=FRENTE, gravidade="media",
            titulo=f"{len(ev)} sócio(s) presentes em grupos econômicos distintos "
                   "que fornecem ao órgão",
            descricao=("Empresas formalmente 'concorrentes' com sócio em comum "
                       "reduzem a competição efetiva quando disputam o mesmo "
                       "certame — conferir por CPF e por certame antes de qualquer "
                       "leitura. Já descontados dois falsos positivos: filiais "
                       "do mesmo CNPJ-raiz e quadros societários com mais de "
                       f"{MAX_SOCIOS_QSA} sócios (bancos e S/A de capital aberto)."),
            evidencia=ev,
            como_verificar="Conferir no Portal da Transparência e na Jucesp se os "
                           "homônimos são a mesma pessoa (mesmo CPF) e se as "
                           "empresas disputaram a mesma licitação.",
        ))

    # -------------------------------------------------- grupos econômicos de fato
    arestas_soc: list[tuple[str, str]] = [
        (e["from"], e["to"]) for e in edges if e.get("tipo") == "socio"]
    if arestas_soc:
        comp = _componentes(arestas_soc)
        d = pd.DataFrame([{"no": n, "grupo": g} for n, g in comp.items()])
        d["tipo"] = d["no"].str.split(":").str[0]
        # Matriz e filial (mesma raiz de CNPJ) não são "empresas formalmente
        # distintas": 2 dos 6 grupos da rodada 10 eram só isso.
        d["raiz"] = d["no"].str.split(":").str[1].str[:8].where(d["tipo"] == "forn")
        tam = d.groupby("grupo").agg(
            empresas=("tipo", lambda s: int((s == "forn").sum())),
            raizes=("raiz", lambda s: int(s.dropna().nunique())),
            pessoas=("tipo", lambda s: int((s == "pessoa").sum())))
        multi = tam[(tam["empresas"] > 1) & (tam["raizes"] > 1)].sort_values(
            "empresas", ascending=False)
        if not multi.empty:
            membros = (d[d["grupo"].isin(multi.index)]
                       .assign(nome=lambda x: x["no"].str.split(":").str[1])
                       .groupby(["grupo", "tipo"])["nome"]
                       .apply(lambda s: ", ".join(sorted(s)[:12])).unstack(fill_value=""))
            ev = multi.join(membros).reset_index()
            achados.append(Finding(
                id="rede-grupos-economicos", frente=FRENTE, gravidade="media",
                titulo=(f"{len(multi)} grupo(s) de empresas de CNPJ-raiz distinto "
                        "ligadas por sócio homônimo (matriz e filial já excluídas)"),
                descricao=(
                    "Componente conexa da rede fornecedor–sócio. Duas empresas "
                    "ligadas por uma pessoa não são concorrentes: se disputam a "
                    "mesma licitação, a competição é aparente. Este é o "
                    "agrupamento correto para medir concentração — o ranking por "
                    "CNPJ isolado subestima o poder de cada grupo. Confirmar por "
                    "CPF antes de qualquer afirmação: a ligação aqui é por nome "
                    "normalizado."),
                evidencia=ev,
                base_legal="Lei 14.133/2021, art. 9º, §1º e art. 14 (impedimentos); "
                           "Lei 12.529/2011, arts. 36, §3º, I e II (conluio em "
                           "licitação).",
                como_verificar="Cruzar com as atas de julgamento das licitações: "
                               "empresas do mesmo grupo que apresentaram propostas "
                               "no mesmo certame caracterizam cotação de cobertura.",
                metricas={"grupos": int(len(multi)),
                          "maior_grupo": int(multi["empresas"].max())},
            ))

        bt = _intermediacao(arestas_soc)
        pontes = bt[bt > 0].head(20)
        if not pontes.empty:
            ev = pontes.to_frame("intermediacao").reset_index(names="no")
            ev["tipo"] = ev["no"].str.split(":").str[0].map(
                {"forn": "fornecedor", "pessoa": "pessoa"}).fillna("outro")
            ev["nome"] = ev["no"].str.split(":").str[1]
            achados.append(Finding(
                id="rede-intermediadores", frente=FRENTE, gravidade="media",
                titulo="Quem está no meio da rede: nós de maior intermediação",
                descricao=(
                    "Centralidade de intermediação: mede quantos caminhos entre "
                    "os demais nós passam por cada um. É uma medida de posição na "
                    "rede de pagamentos e sócios, não de conduta: um sócio que "
                    "aparece em duas empresas fornecedoras tem intermediação alta "
                    "por construção. Serve para ordenar a leitura do grafo."),
                evidencia=ev[["nome", "tipo", "intermediacao"]].round(4),
            ))

    # -------------------------------------------------- convênios recorrentes
    conv = df(staged, "convenios")
    if not conv.empty and "BENEFICIARIO" in conv:
        conv = conv.copy()
        conv["beneficiario_norm"] = conv["BENEFICIARIO"].map(norm_nome)
        rec = (conv.groupby("beneficiario_norm")
               .agg(convenios=("VALOR_TOTAL", "size"), total=("VALOR_TOTAL", "sum"))
               .reset_index().sort_values("total", ascending=False))
        recorrentes = rec[rec["convenios"] >= 3]
        serie_conv = (A.serie_anual(conv, "VALOR_TOTAL")
                      if "_ano" in conv else pd.DataFrame())
        achados.append(Finding(
            id="rede-convenios", frente=FRENTE,
            gravidade="media" if not recorrentes.empty else "info",
            titulo="Repasses por convênio — beneficiários e recorrência",
            descricao="Entidades que recebem convênios repetidamente formam o braço "
                      "'institucional' da rede (entidades de classe, associações). "
                      "Nos conselhos, as entidades de classe indicam os delegados "
                      "que elegem a diretoria: repassar recurso a quem vota é "
                      "conflito de interesse estrutural, não circunstancial.",
            evidencia=rec.head(50),
            serie=serie_conv.rename(columns={"VALOR_TOTAL": "convênios"}),
            grafico="bar",
            tabela_anual=(A.ranking_anual(conv, "beneficiario_norm", "VALOR_TOTAL",
                                          top=10) if "_ano" in conv
                          else pd.DataFrame()),
            base_legal="Lei 14.133/2021, art. 9º, III; Decreto 6.170/2007 e "
                       "Portaria Interministerial 424/2016 (convênios); CF, art. 37.",
        ))

        # ---- ciclo eleitoral: repasse concentrado em ano de eleição?
        if "_ano" in conv and conv["_ano"].nunique() >= 4:
            por_ano = conv.groupby("_ano")["VALOR_TOTAL"].sum()
            eleitoral = por_ano.index.isin(config.ANOS_ELEITORAIS_CREA)
            if eleitoral.any() and (~eleitoral).any():
                m_el = float(por_ano[eleitoral].mean())
                m_nao = float(por_ano[~eleitoral].mean())
                razao = m_el / m_nao if m_nao > 0 else 0.0
                tab = por_ano.to_frame("convenios")
                tab["ano_eleitoral"] = eleitoral
                achados.append(Finding(
                    id="rede-convenios-ciclo-eleitoral", frente=FRENTE,
                    gravidade="alta" if razao >= 1.5 else "info",
                    titulo=(f"Convênios em ano de eleição do Crea: "
                            f"{razao:.2f}× a média dos demais anos"),
                    descricao=(
                        "Média anual de repasse por convênio nos anos de eleição "
                        f"({', '.join(str(a) for a in config.ANOS_ELEITORAIS_CREA)}) "
                        "contra a média dos demais exercícios. As beneficiárias "
                        "típicas são entidades de classe, que indicam os delegados "
                        "do colégio eleitoral. Razão bem acima de 1 é indício de "
                        "uso eleitoral do recurso do conselho; abaixo ou perto de "
                        "1 afasta a hipótese. **Confira o calendário eleitoral "
                        "antes de usar como prova** — o teste é sensível à lista "
                        "de anos configurada."),
                    evidencia=tab.reset_index(),
                    serie=tab[["convenios"]], grafico="bar",
                    tabela_anual=tab.reset_index(),
                    metricas={"media_ano_eleitoral": round(m_el, 2),
                              "media_demais_anos": round(m_nao, 2),
                              "razao": round(razao, 2)},
                    como_verificar="Confrontar as datas de assinatura dos convênios "
                                   "com o calendário eleitoral do Confea/Crea e com "
                                   "a lista de entidades com delegado no colégio.",
                ))

        # ---- valores redondos e idênticos: assinatura de rateio, não de plano
        if "VALOR_TOTAL" in conv:
            v = pd.to_numeric(conv["VALOR_TOTAL"], errors="coerce").fillna(0)
            v = v[v > 0]
            if len(v) >= 10:
                redondos = v[(v % 1000 == 0)]
                repetidos = v.value_counts()
                repetidos = repetidos[repetidos >= 3]
                pct_red = 100 * len(redondos) / len(v)
                if pct_red > 40 or not repetidos.empty:
                    achados.append(Finding(
                        id="rede-convenios-valores", frente=FRENTE,
                        gravidade="media",
                        titulo=(f"{pct_red:.0f}% dos convênios têm valor redondo e "
                                f"{len(repetidos)} valor(es) se repetem 3+ vezes"),
                        descricao=(
                            "Convênio decorre de plano de trabalho: o valor sai de "
                            "um orçamento de itens e raramente termina em três "
                            "zeros. Valor redondo repetido entre beneficiárias "
                            "diferentes é assinatura de rateio — a cota foi "
                            "decidida antes e o plano de trabalho foi escrito "
                            "para caber nela."),
                        evidencia=(repetidos.to_frame("ocorrencias")
                                   .reset_index(names="valor")
                                   if not repetidos.empty
                                   else redondos.to_frame("valor")),
                        metricas={"pct_redondos": round(pct_red, 1)},
                        base_legal="Decreto 6.170/2007, art. 2º e Portaria "
                                   "Interministerial 424/2016 (plano de trabalho "
                                   "com detalhamento de metas e custos).",
                    ))
        for _, r in rec.head(20).iterrows():
            bid = f"conv:{r['beneficiario_norm']}"
            nodes.append({"id": bid, "label": str(r["beneficiario_norm"]).title()[:40],
                          "tipo": "convenio"})
            edges.append({"from": "org:CREA-SP", "to": bid,
                          "valor": float(r["total"]), "tipo": "convenio"})
        if not any(n["id"] == "org:CREA-SP" for n in nodes):
            nodes.append({"id": "org:CREA-SP", "label": "CREA-SP", "tipo": "orgao"})

    # -------------------------------------------------- pagamentos a entidades do sistema
    pg = df(staged, "pagamentos")
    if not pg.empty and "FAVORECIDO_NORM" in pg:
        rx = r"CONFEA|MUTUA|CREA[ -]|SINDICATO|ASSOCIACAO|FEDERACAO"
        sist = pg[pg["FAVORECIDO_NORM"].str.contains(rx, regex=True, na=False)]
        if not sist.empty:
            resumo = (sist.groupby("FAVORECIDO_NORM")["VALOR"].agg(["size", "sum"])
                      .rename(columns={"size": "pagamentos", "sum": "total"})
                      .sort_values("total", ascending=False).reset_index())
            achados.append(Finding(
                id="rede-pagamentos-entidades", frente=FRENTE, gravidade="baixa",
                titulo="Pagamentos diretos a entidades do sistema/classe",
                descricao="Transferências a Confea, Mútua, sindicatos, associações e "
                          "federações fora de convênio formal — mapear o fluxo "
                          "institucional de dinheiro.",
                evidencia=resumo.head(50),
            ))

    # -------------------------------------------------- grafo para o report
    if nodes:
        achados.append(Finding(
            id="rede-grafo", frente=FRENTE, gravidade="info",
            titulo="Grafo da rede (órgãos, fornecedores, sócios, convênios)",
            descricao="Dados do grafo interativo exibido no dashboard.",
            evidencia=pd.DataFrame(edges),
            metricas={"nodes": nodes[:400], "edges": edges[:800]},
        ))
    return achados
