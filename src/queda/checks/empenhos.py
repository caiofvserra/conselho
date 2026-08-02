"""Frente 3 — Empenhos e pagamentos: concentração, fracionamento, anulações."""
from __future__ import annotations

import pandas as pd

from .. import config
from ..findings import Finding
from ..utils import fmt_brl
from .common import df

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
        pj = pg[~pg.get("PF", False).astype(bool)]
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
            top_geral = (pj.groupby("FAVORECIDO_NORM")["VALOR"].sum()
                         .sort_values(ascending=False).head(25).reset_index())
            achados.append(Finding(
                id="empenhos-top-fornecedores", frente=FRENTE, gravidade="info",
                titulo="Maiores fornecedores por valor pago (todo o período)",
                descricao="Ranking de pessoas jurídicas por total recebido.",
                evidencia=top_geral,
                metricas={"top": top_geral.head(12).to_dict("records")},
            ))

    # -------------------------------------------------- fracionamento
    if not pg.empty and {"FAVORECIDO_NORM", "DATA", "VALOR"} <= set(pg.columns):
        pj = pg[~pg.get("PF", False).astype(bool)].dropna(subset=["DATA"]).copy()
        pj = pj.sort_values("DATA")
        suspeitas = []
        janela = pd.Timedelta(days=config.JANELA_FRACIONAMENTO_DIAS)
        for credor, g in pj.groupby("FAVORECIDO_NORM"):
            if len(g) < 3:
                continue
            pequenos = g[g["VALOR"] < config.LIMITE_DISPENSA]
            if len(pequenos) < 3:
                continue
            datas = pequenos["DATA"].reset_index(drop=True)
            vals = pequenos["VALOR"].reset_index(drop=True)
            i = 0
            for j in range(len(datas)):
                while datas[j] - datas[i] > janela:
                    i += 1
                soma = float(vals[i:j + 1].sum())
                if j - i + 1 >= 3 and soma > config.LIMITE_DISPENSA:
                    suspeitas.append({
                        "fornecedor": credor, "inicio": datas[i].date(),
                        "fim": datas[j].date(), "pagamentos": j - i + 1,
                        "soma_janela": round(soma, 2)})
                    break
        if suspeitas:
            ev = pd.DataFrame(suspeitas).sort_values("soma_janela", ascending=False)
            achados.append(Finding(
                id="empenhos-fracionamento", frente=FRENTE, gravidade="alta",
                titulo=f"{len(ev)} fornecedor(es) com padrão de possível fracionamento "
                       "de despesa",
                descricao=f"≥3 pagamentos individuais abaixo de "
                          f"{fmt_brl(config.LIMITE_DISPENSA)} ao mesmo fornecedor em "
                          f"até {config.JANELA_FRACIONAMENTO_DIAS} dias, somando acima "
                          "do limite de dispensa — padrão clássico para escapar de "
                          "licitação (vedado pela Lei 14.133/2021, art. 29).",
                evidencia=ev,
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
