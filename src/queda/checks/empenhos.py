"""Frente 3 — Empenhos e pagamentos: concentração, fracionamento, anulações."""
from __future__ import annotations

import pandas as pd

from .. import analytics as A
from .. import config
from ..findings import Finding
from ..utils import fmt_brl
from .common import df, fornecedores_mercado, transferencias_sistema

FRENTE = "Empenhos e pagamentos"


def _hhi(shares: pd.Series) -> float:
    p = shares / shares.sum()
    return float((p ** 2).sum())


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    emp = df(staged, "empenhos")
    pg = df(staged, "pagamentos")

    # -------------------------------------------------- concentração por credor
    if not pg.empty:
        pj = fornecedores_mercado(staged)
        if not pj.empty:
            por_credor = (pj.groupby(["_ano", "FAVORECIDO_NORM"])["VALOR"].sum()
                          .reset_index())
            linhas = []
            for ano, g in por_credor.groupby("_ano"):
                tot = g["VALOR"].sum()
                if tot <= 0:
                    continue
                top10 = g.nlargest(10, "VALOR")
                linhas.append({"ano": int(ano), "total_pj": float(tot),
                               "share_top10": float(top10["VALOR"].sum() / tot),
                               "hhi": round(_hhi(g["VALOR"]), 4)})
            conc = pd.DataFrame(linhas)
            criticos = conc[conc["hhi"] > 0.15]
            achados.append(Finding(
                id="empenhos-concentracao", frente=FRENTE,
                gravidade="media" if not criticos.empty else "info",
                titulo="Concentração de pagamentos por fornecedor (HHI e top-10 por ano)",
                descricao="HHI > 0,15 indica mercado de fornecedores muito concentrado "
                          "para um órgão dessa dimensão — favorece direcionamento.",
                evidencia=conc,
                metricas={"anos_criticos": criticos["ano"].tolist() if not criticos.empty else []},
            ))
            rank = A.ranking_anual(pj, "FAVORECIDO_NORM", "VALOR", top=10)
            serie_top = pd.DataFrame()
            if not rank.empty:
                lideres = (rank.groupby("FAVORECIDO_NORM")["total"].sum()
                           .nlargest(6).index.tolist())
                serie_top = A.serie_anual(
                    pj[pj["FAVORECIDO_NORM"].isin(lideres)], "VALOR",
                    by="FAVORECIDO_NORM")
            achados.append(Finding(
                id="empenhos-top-fornecedores", frente=FRENTE, gravidade="info",
                titulo="Maiores fornecedores por ano",
                descricao="Top 10 de cada exercício. O gráfico acompanha os seis "
                          "maiores do período — mostra quando cada fornecedor "
                          "entrou, cresceu ou saiu.",
                evidencia=rank, tabela_anual=rank,
                serie=serie_top, grafico="bar_stacked",
            ))
            fixos = (rank.groupby("FAVORECIDO_NORM")["_ano"].nunique()
                     .sort_values(ascending=False))
            fixos = fixos[fixos >= max(int(0.7 * pj["_ano"].nunique()), 3)]
            if not fixos.empty:
                achados.append(Finding(
                    id="empenhos-fornecedores-cativos", frente=FRENTE,
                    gravidade="media",
                    titulo=f"{len(fixos)} fornecedor(es) permanecem no top-10 em "
                           "70% ou mais dos exercícios",
                    descricao="Permanência prolongada no topo, num órgão que deve "
                              "licitar periodicamente, sugere renovação sucessiva "
                              "de contrato ou baixa competição real.",
                    evidencia=fixos.to_frame("anos_no_top10").reset_index(),
                ))

    # -------------------------------------------------- fracionamento
    if not pg.empty and {"FAVORECIDO_NORM", "DATA", "VALOR"} <= set(pg.columns):
        pj = fornecedores_mercado(staged).dropna(subset=["DATA"]).copy()
        # Fornecedor com contrato formal registrado executa parcelas mensais —
        # isso não é fracionamento. Sem esse recorte, o achado listava jornais,
        # rádios e prestadores de serviço contínuo (413 na primeira rodada).
        ctr = df(staged, "contratos")
        com_contrato = set(ctr.get("CONTRATADA_NORM", pd.Series(dtype=str)).dropna())
        pj = pj[~pj["FAVORECIDO_NORM"].isin(com_contrato)].sort_values("DATA")

        conta_col = "CONTA_EMPENHO" if "CONTA_EMPENHO" in pj.columns else None
        suspeitas = []
        janela = pd.Timedelta(days=config.JANELA_FRACIONAMENTO_DIAS)
        chaves = ["FAVORECIDO_NORM"] + ([conta_col] if conta_col else [])
        for chave, g in pj.groupby(chaves):
            pequenos = g[g["VALOR"] < config.LIMITE_DISPENSA]
            if len(pequenos) < 3:
                continue
            datas = pequenos["DATA"].reset_index(drop=True)
            vals = pequenos["VALOR"].reset_index(drop=True)
            i = 0
            for j in range(len(datas)):
                while datas[j] - datas[i] > janela:
                    i += 1
                n = j - i + 1
                soma = float(vals[i:j + 1].sum())
                if n >= 3 and soma > config.LIMITE_DISPENSA:
                    dias = (datas[j] - datas[i]).days
                    # cadência mensal regular = execução de contrato, não rajada
                    if n <= 4 and dias >= 60:
                        continue
                    nomes = chave if isinstance(chave, tuple) else (chave,)
                    suspeitas.append({
                        "fornecedor": nomes[0],
                        "conta": nomes[1] if len(nomes) > 1 else "",
                        "inicio": datas[i].date(), "fim": datas[j].date(),
                        "dias": dias, "pagamentos": n,
                        "soma_janela": round(soma, 2)})
                    break
        if suspeitas:
            ev = pd.DataFrame(suspeitas).sort_values("soma_janela", ascending=False)
            achados.append(Finding(
                id="empenhos-fracionamento", frente=FRENTE, gravidade="alta",
                titulo=f"{len(ev)} caso(s) de possível fracionamento de despesa",
                descricao=(
                    f"≥3 pagamentos abaixo de {fmt_brl(config.LIMITE_DISPENSA)} ao "
                    f"mesmo fornecedor, na mesma conta contábil, em até "
                    f"{config.JANELA_FRACIONAMENTO_DIAS} dias, somando acima do "
                    "limite de dispensa. Já excluídos: fornecedores com contrato "
                    "formal registrado e cadências mensais regulares — os dois "
                    "casos legítimos que inflavam este achado."),
                evidencia=ev,
                base_legal="Lei 14.133/2021, art. 29 e art. 75, §1º — vedado "
                           "fracionar despesa para fugir da modalidade licitatória.",
                como_verificar="Pedir os processos de compra dos casos listados e "
                               "verificar se o objeto é o mesmo e se houve "
                               "planejamento anual da contratação.",
            ))

    # -------------------------------------------------- transferências ao sistema
    transf = transferencias_sistema(staged)
    if not transf.empty:
        serie_t = A.serie_anual(transf, "VALOR", by="FAVORECIDO_NORM")
        total_t = float(transf["VALOR"].sum())
        achados.append(Finding(
            id="empenhos-transferencias-sistema", frente=FRENTE, gravidade="info",
            titulo=f"{fmt_brl(total_t)} transferidos ao Confea e à Mútua no período",
            descricao=("Repasses determinados por lei e estatuto, isolados do "
                       "ranking de fornecedores (não disputam licitação). Mostrados "
                       "à parte porque respondem por parcela relevante da receita "
                       "arrecadada dos profissionais e alimentam a rede de "
                       "influência entre os órgãos do sistema."),
            evidencia=A.ranking_anual(transf, "FAVORECIDO_NORM", "VALOR", top=6),
            serie=serie_t, grafico="bar_stacked",
            tabela_anual=A.ranking_anual(transf, "FAVORECIDO_NORM", "VALOR", top=6),
        ))

    # -------------------------------------------------- anulações e saldos
    if not emp.empty and {"VALOR", "VALOR_ANULADO"} <= set(emp.columns):
        por_ano = emp.groupby("_ano").agg(
            empenhado=("VALOR", "sum"), anulado=("VALOR_ANULADO", "sum")).reset_index()
        por_ano["pct_anulado"] = (100 * por_ano["anulado"] /
                                  por_ano["empenhado"].replace(0, pd.NA)).fillna(0).round(1)
        criticos = por_ano[por_ano["pct_anulado"] > 20]
        if not criticos.empty:
            achados.append(Finding(
                id="empenhos-anulacoes", frente=FRENTE, gravidade="media",
                titulo="Exercícios com mais de 20% do valor empenhado anulado",
                descricao="Anulação em massa sugere planejamento orçamentário ruim ou "
                          "manobra de reserva/liberação de dotação.",
                evidencia=por_ano,
            ))
        estouro = emp[emp["VALOR_PAGO"] > emp["VALOR"] * 1.001]
        if not estouro.empty:
            achados.append(Finding(
                id="empenhos-pago-maior-que-empenhado", frente=FRENTE, gravidade="alta",
                titulo=f"{len(estouro)} empenho(s) com valor pago maior que o empenhado",
                descricao="Pagamento acima do valor do empenho viola a execução "
                          "orçamentária (Lei 4.320/64, art. 59).",
                evidencia=estouro.head(100),
            ))
    return achados
