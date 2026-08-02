"""Frente 1 — Diárias, quilometragem e reembolsos.

Fonte primária: Pagamentos/Empenhos filtrados por texto de conta/histórico.
Os endpoints dedicados (indenizações/diárias) são usados como enriquecimento;
se estiverem vazios enquanto os pagamentos mostram gasto, isso vira achado
de transparência.
"""
from __future__ import annotations

import pandas as pd

from .. import config
from ..findings import Finding
from ..utils import fmt_brl
from .common import classificar_beneficiario, df, pagamentos_indenizacao

FRENTE = "Diárias e indenizações"


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    ind = pagamentos_indenizacao(staged)
    if ind.empty:
        achados.append(Finding(
            id="diarias-sem-dados", frente=FRENTE, gravidade="info",
            titulo="Nenhum pagamento classificado como diária/indenização",
            descricao="O filtro textual sobre contas e históricos de pagamentos "
                      "não encontrou registros. Verifique a coleta.",
        ))
        return achados

    ind = ind.copy()
    ind["ano"] = ind.get("_ano", ind["DATA"].dt.year if "DATA" in ind else None)
    ind["categoria"] = classificar_beneficiario(ind.get("FAVORECIDO_NORM", pd.Series(dtype=str)), staged)
    total = float(ind["VALOR"].sum())

    # -------------------------------------------------- série anual por categoria
    serie = (ind.groupby(["ano", "categoria"])["VALOR"].sum()
             .unstack(fill_value=0).sort_index())
    achados.append(Finding(
        id="diarias-serie-anual", frente=FRENTE, gravidade="info",
        titulo="Série anual de diárias/indenizações por categoria de beneficiário",
        descricao="Total pago por exercício, classificando o favorecido como "
                  "conselheiro, comissionado, efetivo ou outro (cruzamento por nome "
                  "com rol de mandatários e quadro de pessoal). Base do comparativo "
                  "histórico pedido (meta: alcançar exercícios pré-2016).",
        evidencia=serie.reset_index(),
        metricas={"total_geral": total,
                  "anos": [int(a) for a in serie.index.dropna()],
                  "serie": {str(c): [round(float(v), 2) for v in serie[c]] for c in serie.columns}},
    ))

    # -------------------------------------------------- top beneficiários
    por_pessoa = (ind.groupby(["FAVORECIDO_NORM", "categoria"])
                  .agg(total=("VALOR", "sum"), pagamentos=("VALOR", "size"))
                  .reset_index().sort_values("total", ascending=False))
    top = por_pessoa.head(30)
    achados.append(Finding(
        id="diarias-top-beneficiarios", frente=FRENTE, gravidade="info",
        titulo="Maiores beneficiários de diárias/indenizações (todo o período)",
        descricao=f"Total do período: {fmt_brl(total)}. Ranking por favorecido.",
        evidencia=top,
        metricas={"top": top.head(15)[["FAVORECIDO_NORM", "categoria", "total"]]
                  .rename(columns=str.lower).to_dict("records")},
    ))

    # -------------------------------------------------- concentração
    if len(por_pessoa) >= 10:
        share_top10 = float(por_pessoa.head(10)["total"].sum()) / total if total else 0
        if share_top10 > 0.4:
            achados.append(Finding(
                id="diarias-concentracao", frente=FRENTE, gravidade="media",
                titulo=f"10 pessoas concentram {share_top10:.0%} das diárias/indenizações",
                descricao="Concentração alta sugere uso recorrente pelos mesmos "
                          "beneficiários — cruzar com agenda/eventos.",
                evidencia=por_pessoa.head(10),
                metricas={"share_top10": round(share_top10, 3)},
            ))

    # -------------------------------------------------- volume anual por pessoa
    anual = (ind.groupby(["FAVORECIDO_NORM", "categoria", "ano"])
             .agg(total=("VALOR", "sum"), pagamentos=("VALOR", "size")).reset_index())
    excesso = anual[anual["pagamentos"] > config.MAX_DIARIAS_ANO]
    if not excesso.empty:
        achados.append(Finding(
            id="diarias-volume-anual", frente=FRENTE, gravidade="alta",
            titulo=f"{excesso['FAVORECIDO_NORM'].nunique()} pessoa(s) com mais de "
                   f"{config.MAX_DIARIAS_ANO} pagamentos de indenização num mesmo ano",
            descricao="Volume incompatível com dias úteis disponíveis; indica diárias "
                      "sistemáticas ou complemento salarial disfarçado.",
            evidencia=excesso.sort_values("pagamentos", ascending=False),
        ))

    # -------------------------------------------------- PJ recebendo indenização
    pj = ind[~ind.get("PF", True).astype(bool)]
    pj_relevante = pj[pj["VALOR"] > 0]
    if not pj_relevante.empty and float(pj_relevante["VALOR"].sum()) > 0.05 * total:
        achados.append(Finding(
            id="diarias-pj", frente=FRENTE, gravidade="media",
            titulo="Pagamentos classificados como indenização feitos a pessoa jurídica",
            descricao="Diária/indenização é verba de pessoa física; valores relevantes "
                      "a PJ nessas contas merecem conferência (pode ser agência de "
                      "viagem — ou triangulação).",
            evidencia=pj_relevante.groupby("FAVORECIDO_NORM")["VALOR"].sum()
                     .sort_values(ascending=False).head(20).reset_index(),
        ))

    # -------------------------------------------------- valor unitário (se houver)
    for nome in ("implanta_probe_diariasdeslocamentos", "implanta_probe_indenizacoes"):
        probe = df(staged, nome)
        if not probe.empty and "VALOR_UNITARIO" in probe:
            caros = probe[probe["VALOR_UNITARIO"].astype(float) > config.TETO_DIARIA_UNITARIA]
            if not caros.empty:
                achados.append(Finding(
                    id="diarias-teto-unitario", frente=FRENTE, gravidade="alta",
                    titulo=f"Diárias com valor unitário acima de {fmt_brl(config.TETO_DIARIA_UNITARIA)}",
                    descricao="Valores unitários acima do teto paramétrico (config).",
                    evidencia=caros,
                ))

    # -------------------------------------------------- transparência dos endpoints
    endpoint = df(staged, "indenizacoes_endpoint")
    if endpoint.empty and total > 0:
        achados.append(Finding(
            id="diarias-endpoint-vazio", frente=FRENTE, gravidade="alta",
            titulo="Endpoints dedicados de diárias/indenizações estão vazios, mas os "
                   "pagamentos registram gasto",
            descricao=f"Os pagamentos somam {fmt_brl(total)} em contas de "
                      "diária/indenização, porém os endpoints específicos (Dados "
                      "Abertos `indenizacoes` e sondagens no Implanta) não retornam "
                      "registros. Detalhes como evento, período de deslocamento e "
                      "valor unitário ficam ocultos — possível descumprimento de "
                      "transparência ativa (LAI e Acórdãos TCU sobre conselhos).",
            metricas={"total_pagamentos": total},
        ))
    return achados
