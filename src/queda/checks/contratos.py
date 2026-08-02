"""Frente 4 — Contratos e licitações: aditivos, vigência, cross-check PNCP."""
from __future__ import annotations

import pandas as pd

from .. import config
from ..findings import Finding
from ..utils import norm_nome
from .common import df

FRENTE = "Contratos e licitações"


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    ctr = df(staged, "contratos")
    adt = df(staged, "aditivos")
    pg = df(staged, "pagamentos")
    pncp = df(staged, "pncp_contratos")

    # -------------------------------------------------- aditivos acima do limite
    if not ctr.empty and not adt.empty and "NUMERO_CONTRATO" in adt:
        soma_adt = adt.groupby("NUMERO_CONTRATO")["VALOR"].sum().rename("total_aditivos")
        base = ctr.set_index("NUMERO_CONTRATO").join(soma_adt).reset_index()
        base["total_aditivos"] = base["total_aditivos"].fillna(0)
        base["pct_aditivo"] = (100 * base["total_aditivos"] /
                               base["VALOR"].replace(0, pd.NA)).fillna(0).round(1)
        estourados = base[base["pct_aditivo"] > config.ADITIVO_LIMITE_PCT]
        if not estourados.empty:
            achados.append(Finding(
                id="contratos-aditivos-limite", frente=FRENTE, gravidade="alta",
                titulo=f"{len(estourados)} contrato(s) com aditivos acima de "
                       f"{config.ADITIVO_LIMITE_PCT:.0f}% do valor original",
                descricao="Acréscimo acumulado além do limite legal (25% p/ obras e "
                          "serviços — Lei 8.666 art. 65 / Lei 14.133 art. 125).",
                evidencia=estourados[["NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                      "total_aditivos", "pct_aditivo"]]
                .sort_values("pct_aditivo", ascending=False),
            ))

    # -------------------------------------------------- pagamento após vigência
    if not ctr.empty and not pg.empty and "FIM" in ctr and "FAVORECIDO_NORM" in pg:
        fim_por_contratada = ctr.dropna(subset=["FIM"]).groupby("CONTRATADA_NORM")["FIM"].max()
        pj = pg[~pg.get("PF", False).astype(bool)].dropna(subset=["DATA"])
        m = pj.join(fim_por_contratada.rename("FIM_VIGENCIA"), on="FAVORECIDO_NORM")
        tarde = m[m["DATA"] > m["FIM_VIGENCIA"] + pd.Timedelta(days=60)]
        if not tarde.empty:
            resumo = (tarde.groupby("FAVORECIDO_NORM")
                      .agg(pagamentos=("VALOR", "size"), total=("VALOR", "sum"),
                           fim_vigencia=("FIM_VIGENCIA", "max"),
                           ultimo_pagamento=("DATA", "max")).reset_index()
                      .sort_values("total", ascending=False))
            achados.append(Finding(
                id="contratos-pagamento-pos-vigencia", frente=FRENTE, gravidade="media",
                titulo=f"{len(resumo)} contratada(s) recebendo mais de 60 dias após o "
                       "fim da última vigência conhecida",
                descricao="Pagamento sem cobertura contratual vigente (ou contrato não "
                          "publicado no portal). Aproximação por nome da contratada — "
                          "conferir caso a caso.",
                evidencia=resumo.head(50),
            ))

    # -------------------------------------------------- contratos sem licitação
    if not ctr.empty and "NUMERO_LICITACAO" in ctr:
        sem_lic = ctr[ctr["NUMERO_LICITACAO"].isna() |
                      (ctr["NUMERO_LICITACAO"].astype(str).str.strip().isin(["", "None", "nan"]))]
        if not sem_lic.empty:
            tot = float(sem_lic["VALOR"].sum())
            achados.append(Finding(
                id="contratos-sem-licitacao", frente=FRENTE,
                gravidade="media" if tot > 1_000_000 else "baixa",
                titulo=f"{len(sem_lic)} contrato(s) sem número de licitação vinculado",
                descricao="Contratação direta (dispensa/inexigibilidade) ou falha de "
                          "registro. Ranqueado por valor.",
                evidencia=sem_lic[["NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                   "DATA_INICIO_VIGENCIA", "DATA_FINAL_VIGENCIA"]]
                .sort_values("VALOR", ascending=False).head(100),
                metricas={"valor_total": round(tot, 2)},
            ))

    # -------------------------------------------------- cross-check Implanta × PNCP
    if not pncp.empty:
        alvo = pncp[pncp.get("orgao_papel") == "alvo"]
        if not ctr.empty and not alvo.empty:
            nomes_portal = set(ctr["CONTRATADA_NORM"].dropna())
            nomes_pncp = set(alvo["fornecedor_norm"].dropna())
            so_portal = sorted(nomes_portal - nomes_pncp)
            so_pncp = sorted(nomes_pncp - nomes_portal)
            ctr_pos21 = ctr[ctr["INI"].dt.year >= 2021] if "INI" in ctr else ctr
            faltando = ctr_pos21[ctr_pos21["CONTRATADA_NORM"].isin(so_portal)]
            if not faltando.empty:
                achados.append(Finding(
                    id="contratos-ausentes-pncp", frente=FRENTE, gravidade="media",
                    titulo=f"{faltando['CONTRATADA_NORM'].nunique()} contratada(s) do "
                           "portal (pós-2021) sem contrato correspondente no PNCP",
                    descricao="A Lei 14.133 obriga a publicação no PNCP; ausência é "
                              "falha de transparência (comparação por nome do "
                              "fornecedor — aproximada).",
                    evidencia=faltando[["NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                        "DATA_INICIO_VIGENCIA"]].sort_values(
                        "VALOR", ascending=False).head(100),
                ))
            if so_pncp:
                achados.append(Finding(
                    id="contratos-so-no-pncp", frente=FRENTE, gravidade="baixa",
                    titulo=f"{len(so_pncp)} fornecedor(es) com contrato no PNCP e não "
                           "no portal da transparência",
                    descricao="Situação inversa: contrato publicado nacionalmente mas "
                              "invisível no portal próprio do conselho.",
                    evidencia=alvo[alvo["fornecedor_norm"].isin(so_pncp)][
                        ["numeroContrato", "fornecedor_nome", "objeto", "valorGlobal",
                         "vigenciaInicio", "vigenciaFim"]].head(100),
                ))
        # valor acumulado muito acima do global
        infl = alvo[(alvo["valorAcumulado"] > 0) &
                    (alvo["valorAcumulado"] > alvo["valorGlobal"] * (1 + config.ADITIVO_LIMITE_PCT / 100))]
        if not infl.empty:
            achados.append(Finding(
                id="contratos-pncp-acumulado", frente=FRENTE, gravidade="media",
                titulo=f"{len(infl)} contrato(s) no PNCP com valor acumulado "
                       f">{config.ADITIVO_LIMITE_PCT:.0f}% acima do valor global",
                descricao="Indicativo de aditivos sucessivos registrados no PNCP.",
                evidencia=infl[["numeroContrato", "fornecedor_nome", "objeto",
                                "valorGlobal", "valorAcumulado"]],
            ))
    return achados
