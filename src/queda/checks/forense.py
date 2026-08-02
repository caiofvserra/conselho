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

    # ------------------------------------------------ 1b. Benford 2 dígitos
    d2 = A.benford_dois_digitos(pg["VALOR"])
    if d2.get("aplicavel"):
        excessos = pd.DataFrame({
            "excesso_sobre_o_esperado": d2["maiores_excessos"]}).reset_index(
            names="dois_primeiros_digitos")
        excessos["observado"] = excessos["dois_primeiros_digitos"].map(
            d2["observado"])
        excessos["esperado"] = excessos["dois_primeiros_digitos"].map(
            d2["esperado"])
        desviante = d2["conformidade"] in ("marginal", "nao_conforme")
        achados.append(Finding(
            id="forense-benford-2d", frente=FRENTE,
            gravidade="media" if desviante else "info",
            titulo=(f"Dois primeiros dígitos {'fora do' if desviante else 'dentro do'} "
                    f"padrão de Benford (MAD {d2['mad']:.5f})"),
            descricao=(
                "O teste de dois dígitos tem 90 categorias em vez de 9 e é onde "
                "aparece a assinatura de valor 'arredondado para caber': excesso "
                "em 10, 15, 20, 25, 50. A tabela lista os pares de dígitos com "
                "maior excesso sobre o esperado — cada um é uma faixa de valor "
                "que aparece mais do que deveria. Os limiares são os próprios do "
                "teste de 2 dígitos (conforme <0,0012; não conforme >0,0022), "
                "bem mais apertados que os de 1 dígito."),
            evidencia=excessos,
            metricas={"mad": d2["mad"], "n": d2["n"],
                      "conformidade": d2["conformidade"]},
            como_verificar="Filtrar os pagamentos cujo valor começa pelos dígitos "
                           "em excesso e verificar se se concentram num punhado "
                           "de fornecedores ou contas.",
        ))

    # ------------------------------------------------ 2. bunching no limite
    # O limite de dispensa muda por exercício: testar tudo contra o teto de
    # hoje procura represamento na fronteira errada para 2/3 da série.
    pj = pg[~pg.get("PF", False).astype(bool)] if "PF" in pg else pg
    por_ano_b, faixas = [], []
    for ano, g in pj.groupby("_ano"):
        limite = config.limite_dispensa(ano)
        r = A.bunching(g["VALOR"], limite)
        r["_ano"] = int(ano)
        por_ano_b.append(r)
        faixas.append(g[(g["VALOR"] >= limite * 0.9) & (g["VALOR"] < limite)])
    tabela = pd.DataFrame(por_ano_b)
    faixa = pd.concat(faixas) if faixas else pd.DataFrame()
    agudos = (tabela[(tabela["n_abaixo"] >= 20) & (tabela["razao"] >= 2)]
              if not tabela.empty else pd.DataFrame())
    if not agudos.empty:
        n_abaixo = int(agudos["n_abaixo"].sum())
        pior = agudos.loc[agudos["razao"].idxmax()]
        achados.append(Finding(
            id="forense-bunching-dispensa", frente=FRENTE, gravidade="alta",
            titulo=(f"Pagamentos represados logo abaixo do limite de dispensa em "
                    f"{len(agudos)} exercício(s) — pico de {pior['razao']:.1f}× "
                    f"em {int(pior['_ano'])}"),
            descricao=(
                f"São {n_abaixo} pagamentos na faixa de 10% imediatamente ABAIXO "
                "do limite de dispensa **vigente em cada exercício** contra "
                f"{int(agudos['n_acima'].sum())} na faixa simétrica acima. Numa "
                "distribuição natural de preços as duas faixas seriam parecidas; "
                "a assimetria indica valores ajustados para não cruzar o limite "
                "que obrigaria licitação. A coluna `limite` mostra o teto usado "
                f"em cada ano (em {int(pior['_ano'])}: "
                f"{fmt_brl(float(pior['limite']))})."),
            evidencia=tabela[["_ano", "limite", "n_abaixo", "n_acima", "razao",
                              "valor_abaixo"]],
            serie=(A.serie_anual(faixa, "VALOR").rename(
                columns={"VALOR": "valor logo abaixo do limite"})
                if not faixa.empty else None),
            grafico="bar",
            tabela_anual=(A.ranking_anual(faixa, "FAVORECIDO_NORM", "VALOR",
                                          top=10) if not faixa.empty
                          else pd.DataFrame()),
            metricas={"exercicios_agudos": agudos["_ano"].astype(int).tolist(),
                      "n_abaixo_total": n_abaixo},
            base_legal="Lei 14.133/2021, art. 29 e art. 75 (e Lei 8.666/1993, "
                       "art. 23, §5º, nos exercícios anteriores) — vedado o "
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
