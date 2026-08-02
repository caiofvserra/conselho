"""Frente — Forense estatística sobre os pagamentos.

Testes que não dependem de conhecer o caso: procuram a assinatura numérica que
manipulação costuma deixar. Nenhum deles prova irregularidade sozinho — servem
para priorizar onde olhar.
"""
from __future__ import annotations

import pandas as pd

from .. import analytics as A
from .. import config
from ..findings import Finding
from ..utils import fmt_brl
from .common import df

FRENTE = "Forense estatística"


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    pg = df(staged, "pagamentos")
    if pg.empty or "VALOR" not in pg:
        return achados
    pg = pg.copy()
    if "_ano" not in pg and "DATA" in pg:
        pg["_ano"] = pd.to_datetime(pg["DATA"], errors="coerce").dt.year

    # ------------------------------------------------ 1. Lei de Benford
    geral = A.benford_primeiro_digito(pg["VALOR"])
    if geral.get("aplicavel"):
        linhas = []
        for ano, g in pg.groupby("_ano"):
            r = A.benford_primeiro_digito(g["VALOR"])
            if r.get("aplicavel"):
                linhas.append({"_ano": int(ano), "n": r["n"], "mad": r["mad"],
                               "conformidade": r["conformidade"],
                               "digito_mais_desviante": r["maior_desvio_digito"]})
        por_ano = pd.DataFrame(linhas)
        desviantes = por_ano[por_ano["conformidade"].isin(
            ["marginal", "nao_conforme"])] if not por_ano.empty else pd.DataFrame()
        comparativo = pd.DataFrame({
            "observado": geral["observado"], "esperado": geral["esperado"]})
        comparativo.index.name = "digito"
        achados.append(Finding(
            id="forense-benford", frente=FRENTE,
            gravidade="media" if not desviantes.empty else "info",
            titulo=(f"Distribuição do primeiro dígito fora do padrão de Benford em "
                    f"{len(desviantes)} exercício(s)"
                    if not desviantes.empty else
                    "Distribuição do primeiro dígito compatível com a Lei de Benford"),
            descricao=(
                "Em conjuntos financeiros grandes e não manipulados, o primeiro "
                "dígito segue a Lei de Benford (1 aparece ~30% das vezes, 9 ~4,6%). "
                "Desvio forte pode indicar valores fabricados, represados abaixo de "
                "um limite ou concentrados em faixas de preço. Métrica: MAD de "
                "Nigrini (<0,006 conforme; >0,015 não conforme). Atenção: contratos "
                "de valor fixo repetido e folha de pagamento distorcem o teste "
                "legitimamente."),
            evidencia=por_ano if not por_ano.empty else comparativo.reset_index(),
            serie=(por_ano.set_index("_ano")[["mad"]]
                   .rename(columns={"mad": "desvio (MAD)"})
                   if not por_ano.empty else None),
            grafico="line", unidade="MAD",
            metricas={"geral": geral},
            como_verificar="Nos anos apontados, isolar as contas com maior volume "
                           "e repetir o teste por conta — o desvio costuma vir de "
                           "poucas rubricas.",
        ))

    # ------------------------------------------------ 2. bunching no limite
    pj = pg[~pg.get("PF", False).astype(bool)] if "PF" in pg else pg
    b = A.bunching(pj["VALOR"], config.LIMITE_DISPENSA)
    if b["n_abaixo"] >= 20 and b["razao"] >= 2:
        faixa = pj[(pj["VALOR"] >= config.LIMITE_DISPENSA * 0.9) &
                   (pj["VALOR"] < config.LIMITE_DISPENSA)]
        achados.append(Finding(
            id="forense-bunching-dispensa", frente=FRENTE, gravidade="alta",
            titulo=f"Pagamentos represados logo abaixo do limite de dispensa "
                   f"({b['razao']:.1f}× mais frequentes que logo acima)",
            descricao=(
                f"Há {b['n_abaixo']} pagamentos na faixa de 10% imediatamente "
                f"ABAIXO de {fmt_brl(config.LIMITE_DISPENSA)} contra "
                f"{b['n_acima']} na faixa simétrica acima. Numa distribuição "
                "natural de preços as duas faixas seriam parecidas; a assimetria "
                "indica valores ajustados para não cruzar o limite que obrigaria "
                "licitação."),
            evidencia=A.ranking_anual(faixa, "FAVORECIDO_NORM", "VALOR", top=10),
            serie=A.serie_anual(faixa, "VALOR").rename(
                columns={"VALOR": "valor logo abaixo do limite"}),
            grafico="bar",
            metricas=b,
            base_legal="Lei 14.133/2021, art. 29 e art. 75 — vedado o "
                       "fracionamento para fugir da modalidade licitatória.",
        ))

    # ------------------------------------------------ 3. valores redondos
    v = pg["VALOR"]
    redondos = pg[(v > 1000) & (v % 1000 == 0)]
    if not redondos.empty:
        share = 100 * len(redondos) / len(pg[v > 1000])
        if share > 5:
            achados.append(Finding(
                id="forense-valores-redondos", frente=FRENTE, gravidade="media",
                titulo=f"{share:.1f}% dos pagamentos acima de R$ 1.000 têm valor "
                       "exatamente redondo (múltiplo de mil)",
                descricao=("Preço de mercado raramente fecha em milhar exato. "
                           "Concentração de valores redondos aponta para valores "
                           "arbitrados (rateio, teto pactuado, repasse combinado) "
                           "em vez de precificação por item ou medição."),
                evidencia=A.ranking_anual(redondos, "FAVORECIDO_NORM", "VALOR", top=10),
                serie=A.serie_anual(redondos, "VALOR").rename(
                    columns={"VALOR": "pago em valores redondos"}),
                grafico="bar",
                metricas={"share_pct": round(share, 2),
                          "n": int(len(redondos)),
                          "total": round(float(redondos["VALOR"].sum()), 2)},
            ))

    # ------------------------------------------------ 4. duplicidade aparente
    if {"FAVORECIDO_NORM", "DATA"} <= set(pg.columns):
        d = pg.dropna(subset=["DATA"]).copy()
        chave = ["FAVORECIDO_NORM", "VALOR"]
        grupos = d.groupby(chave).agg(
            ocorrencias=("VALOR", "size"),
            primeira=("DATA", "min"), ultima=("DATA", "max")).reset_index()
        rep = grupos[(grupos["ocorrencias"] >= 3) & (grupos["VALOR"] > 5000)]
        rep = rep.assign(
            janela_dias=(rep["ultima"] - rep["primeira"]).dt.days)
        curtos = rep[rep["janela_dias"] <= 45].sort_values(
            "ocorrencias", ascending=False)
        if not curtos.empty:
            achados.append(Finding(
                id="forense-valores-repetidos", frente=FRENTE, gravidade="media",
                titulo=f"{len(curtos)} combinação(ões) de favorecido + valor "
                       "idêntico repetidas 3+ vezes em até 45 dias",
                descricao=("Mesmo valor, mesmo favorecido, intervalo curto. Pode "
                           "ser parcela contratual legítima — ou pagamento em "
                           "duplicidade, ou despesa fatiada em parcelas iguais "
                           "para ficar abaixo de um limite."),
                evidencia=curtos.head(80),
            ))

    # ------------------------------------------------ 5. sazonalidade de dezembro
    dez = A.concentracao_dezembro(pg, "DATA", "VALOR")
    if not dez.empty:
        media = float(dez["share_dezembro"].mean())
        altos = dez[dez["share_dezembro"] > 0.20]
        achados.append(Finding(
            id="forense-dezembro", frente=FRENTE,
            gravidade="media" if not altos.empty else "info",
            titulo=(f"Dezembro concentra {media:.0%} do pagamento anual em média"
                    + (f" — acima de 20% em {len(altos)} exercício(s)"
                       if not altos.empty else "")),
            descricao=("Um mês vale 8,3% do ano. Concentração muito acima disso em "
                       "dezembro é o padrão clássico de corrida orçamentária: "
                       "empenhar e pagar às pressas para não devolver dotação, com "
                       "menos tempo de análise e mais risco de contratação "
                       "apressada."),
            evidencia=dez,
            serie=dez.set_index("_ano")[["share_dezembro"]].mul(100).round(1)
                  .rename(columns={"share_dezembro": "% pago em dezembro"}),
            grafico="line", unidade="%",
            base_legal="Lei 4.320/1964, arts. 58–60 (empenho prévio) e art. 38 "
                       "(anulação de restos).",
        ))

    # ------------------------------------------------ 6. estornos e negativos
    neg = pg[pg["VALOR"] < 0]
    if not neg.empty:
        achados.append(Finding(
            id="forense-estornos", frente=FRENTE, gravidade="media",
            titulo=f"{len(neg)} lançamento(s) de valor negativo "
                   f"({fmt_brl(abs(float(neg['VALOR'].sum())))} em estornos)",
            descricao=("Valores negativos compensam silenciosamente os totais de "
                       "todos os demais indicadores. Isolá-los mostra o volume de "
                       "pagamento revertido — e quem concentra reversões."),
            evidencia=A.ranking_anual(
                neg.assign(VALOR_ABS=neg["VALOR"].abs()),
                "FAVORECIDO_NORM", "VALOR_ABS", top=10),
            serie=A.serie_anual(neg.assign(VALOR_ABS=neg["VALOR"].abs()),
                                "VALOR_ABS").rename(
                columns={"VALOR_ABS": "estornado"}),
            grafico="bar",
        ))
    return achados
