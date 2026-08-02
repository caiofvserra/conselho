"""Frente 5 — Rede de influência entre órgãos do sistema Confea/Crea.

Constrói um grafo órgãos ↔ fornecedores ↔ pessoas para expor:
- fornecedores que contratam com vários órgãos do sistema (Confea, Mútua, CREAs);
- sócios de fornecedores homônimos de conselheiros/funcionários do Crea-SP;
- sócios/endereços compartilhados entre fornecedores distintos do órgão;
- convênios e repasses a entidades recorrentes.
"""
from __future__ import annotations

import pandas as pd

from ..findings import Finding
from ..utils import norm_nome
from .common import df, fichas_cnpj, qsa_nomes

FRENTE = "Rede de influência"

# Acima disso o QSA é de banco/estatal/S-A aberta: sócios em comum não indicam nada.
MAX_SOCIOS_QSA = 12


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
            descricao=("Empresas formalmente 'concorrentes' com sócio em comum minam "
                       "a competição na licitação (indício de conluio ou de empresa "
                       "de fachada). Já descontados dois falsos positivos: filiais "
                       "do mesmo CNPJ-raiz e quadros societários com mais de "
                       f"{MAX_SOCIOS_QSA} sócios (bancos e S/A de capital aberto)."),
            evidencia=ev,
            como_verificar="Conferir no Portal da Transparência e na Jucesp se os "
                           "homônimos são a mesma pessoa (mesmo CPF) e se as "
                           "empresas disputaram a mesma licitação.",
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
        achados.append(Finding(
            id="rede-convenios", frente=FRENTE,
            gravidade="media" if not recorrentes.empty else "info",
            titulo="Repasses por convênio — beneficiários e recorrência",
            descricao="Entidades que recebem convênios repetidamente formam o braço "
                      "'institucional' da rede (entidades de classe, associações).",
            evidencia=rec.head(50),
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
