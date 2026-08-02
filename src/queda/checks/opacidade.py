"""Frente — Opacidade: dinheiro público pago a rótulos que escondem o beneficiário.

Motivação: nos dados reais do Crea-SP, o maior "favorecido" de todos é
"DIVERSOS FUNCIONARIOS", com R$ 1,33 bilhão. Um rótulo agregado impede saber
quem recebeu o quê — o oposto do que a LAI exige. Este módulo mede a opacidade,
e principalmente **decompõe o agregado pela conta contábil do empenho**, que é
a informação que sobra quando o nome do beneficiário é suprimido.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from ..findings import Finding
from ..utils import fmt_brl
from .common import df

FRENTE = "Opacidade e transparência"

# Rótulos que agregam múltiplos beneficiários num único "favorecido".
RX_AGREGADOR = re.compile(
    r"\bdiversos?\b|\bvarios?\b|\bdemais\b|\boutros?\b|"
    r"\bfuncionarios\b|\bempregados\b|\bservidores\b|\bcolaboradores\b|"
    r"\bfolha de pagamento\b|\bcredores?\b|\bbeneficiarios?\b|"
    r"\bconselheiros\b|\bterceiros\b|\bnao informado\b|\bsem identificacao\b",
    re.IGNORECASE)


def _agregadores(pg: pd.DataFrame) -> pd.Series:
    """Máscara dos pagamentos feitos a rótulos agregadores."""
    nomes = pg.get("FAVORECIDO_NORM", pd.Series(dtype=str)).fillna("")
    por_nome = nomes.str.contains(RX_AGREGADOR, regex=True, na=False)
    # documento ausente também impede identificar o beneficiário
    doc = pg.get("CPF_CNPJ", pd.Series(dtype=str)).fillna("").astype(str)
    sem_doc = doc.str.replace(r"\D", "", regex=True).str.len() < 11
    return por_nome | (sem_doc & (nomes.str.len() > 0))


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    pg = df(staged, "pagamentos")
    if pg.empty or "VALOR" not in pg:
        return achados

    pg = pg.copy()
    if "_ano" not in pg and "DATA" in pg:
        pg["_ano"] = pd.to_datetime(pg["DATA"], errors="coerce").dt.year
    pg["_agregado"] = _agregadores(pg)
    total_ano = pg.groupby("_ano")["VALOR"].sum()
    agr = pg[pg["_agregado"]]

    if agr.empty:
        return achados

    # ---------------------------------------------------- 1. dimensão do problema
    por_ano = agr.groupby("_ano")["VALOR"].agg(valor="sum", pagamentos="size")
    por_ano["total_pago_ano"] = total_ano
    por_ano["share"] = (100 * por_ano["valor"] /
                        por_ano["total_pago_ano"].replace(0, pd.NA)).round(1)
    por_ano["ticket_medio"] = (por_ano["valor"] / por_ano["pagamentos"]).round(2)
    total_agr = float(agr["VALOR"].sum())
    share_medio = float(por_ano["share"].mean())

    achados.append(Finding(
        id="opacidade-agregadores", frente=FRENTE, gravidade="alta",
        titulo=f"{fmt_brl(total_agr)} pagos a rótulos que não identificam o "
               f"beneficiário ({share_medio:.0f}% do total, média anual)",
        descricao=(
            "Pagamentos cujo favorecido é um rótulo coletivo (ex.: 'DIVERSOS "
            "FUNCIONARIOS') ou que não trazem CPF/CNPJ. Nesses casos é impossível "
            "saber quem recebeu, quanto e por quê — inclusive para verificar teto "
            "remuneratório, acúmulo de diárias e conflito de interesse. O ticket "
            "médio elevado mostra que são lotes, não pagamentos individuais."),
        evidencia=por_ano.reset_index(),
        serie=por_ano[["valor"]].rename(columns={"valor": "pago a rótulo agregador"}),
        grafico="bar",
        tabela_anual=por_ano.reset_index(),
        metricas={"total": round(total_agr, 2),
                  "share_medio_pct": round(share_medio, 2),
                  "ticket_medio_geral": round(total_agr / max(len(agr), 1), 2)},
        base_legal=(
            "Lei 12.527/2011 (LAI), arts. 3º, III e 8º, §1º, III — divulgação de "
            "informações sobre repasses e despesas de forma individualizada; "
            "Decreto 8.777/2016 (dados abertos); jurisprudência do TCU sobre "
            "transparência de conselhos de fiscalização profissional."),
        como_verificar=(
            "Protocolar pedido de acesso à informação (e-SIC do Crea-SP) exigindo "
            "a relação individualizada dos beneficiários dos empenhos pagos a "
            "'DIVERSOS FUNCIONARIOS', por ano, com CPF parcial, cargo e rubrica."),
    ))

    # ------------------------------------- 2. decomposição pela conta contábil
    conta_col = next((c for c in ("CONTA_EMPENHO", "NOME_CONTA", "CONTA_PASSIVO")
                      if c in agr.columns), None)
    if conta_col:
        decomp = (agr.groupby([conta_col])["VALOR"]
                  .agg(total="sum", pagamentos="size").reset_index()
                  .sort_values("total", ascending=False))
        decomp["share_pct"] = (100 * decomp["total"] / total_agr).round(2)
        serie_conta = A.serie_anual(agr, "VALOR", by=conta_col)
        if not serie_conta.empty and serie_conta.shape[1] > 8:
            principais = decomp.head(8)[conta_col].tolist()
            outras = [c for c in serie_conta.columns if c not in principais]
            serie_conta["(demais contas)"] = serie_conta[outras].sum(axis=1)
            serie_conta = serie_conta[principais + ["(demais contas)"]]
        achados.append(Finding(
            id="opacidade-decomposicao-conta", frente=FRENTE, gravidade="alta",
            titulo="O que está escondido dentro dos rótulos agregados "
                   "(decomposição pela conta contábil)",
            descricao=(
                "O portal suprime o nome do beneficiário, mas mantém a conta "
                "contábil do empenho. Agrupando por essa conta é possível revelar "
                "a NATUREZA do gasto oculto — quanto é folha, quanto é diária, "
                "quanto é benefício, quanto é verba indenizatória. Esta é a melhor "
                "aproximação possível sem o dado individualizado."),
            evidencia=decomp,
            serie=serie_conta, grafico="bar_stacked",
            metricas={"contas": decomp.head(12).to_dict("records")},
            como_verificar=(
                "Confrontar cada conta aqui listada com o Comparativo da Despesa "
                "e com o Demonstrativo de Empenhos do mesmo exercício."),
        ))

    # ------------------------------------- 3. tendência da opacidade
    if len(por_ano) >= 4:
        tend = A.cagr(por_ano["share"].astype(float))
        piora = por_ano["share"].iloc[-1] > por_ano["share"].iloc[0]
        achados.append(Finding(
            id="opacidade-tendencia", frente=FRENTE,
            gravidade="media" if piora else "info",
            titulo=("A opacidade aumentou ao longo do período"
                    if piora else "Evolução da opacidade no período"),
            descricao=(
                f"Participação dos pagamentos sem beneficiário identificável no "
                f"total pago, ano a ano. Primeiro ano: "
                f"{por_ano['share'].iloc[0]:.1f}%; último: "
                f"{por_ano['share'].iloc[-1]:.1f}%."),
            evidencia=por_ano.reset_index()[["_ano", "share", "valor", "pagamentos"]],
            serie=por_ano[["share"]].rename(columns={"share": "% do total pago"}),
            grafico="line", unidade="%",
            metricas={"cagr_share": None if pd.isna(tend) else round(tend, 4)},
        ))

    # ------------------------------------- 4. quem são os rótulos
    rotulos = (agr.groupby("FAVORECIDO_NORM")["VALOR"]
               .agg(total="sum", pagamentos="size").reset_index()
               .sort_values("total", ascending=False).head(30))
    achados.append(Finding(
        id="opacidade-rotulos", frente=FRENTE, gravidade="info",
        titulo=f"Rótulos agregadores identificados ({len(rotulos)} principais)",
        descricao="Nomes usados no lugar do beneficiário real, por valor total.",
        evidencia=rotulos,
        tabela_anual=A.ranking_anual(agr, "FAVORECIDO_NORM", "VALOR", top=5),
    ))

    # ------------------------------------- 5. conciliação com a contabilidade
    comp = df(staged, "comparativo_despesa")
    if not comp.empty and "NOME" in comp and conta_col:
        valor_col = ("REALIZADO_EXERCICIO" if "REALIZADO_EXERCICIO" in comp
                     else "TOTAL_EXERCICIO")
        if valor_col in comp:
            analitica = (comp[comp["ANALITICA"].astype(bool)]
                         if "ANALITICA" in comp else comp)
            rx_pessoal = (r"pessoal|vencimento|salario|remunera|encargo|"
                          r"beneficio|vale|plano de saude")
            pess = analitica[analitica["NOME"].fillna("").str.lower()
                             .str.contains(rx_pessoal, regex=True)]
            cont_ano = pess.groupby("_ano")[valor_col].sum()
            comparo = pd.DataFrame({
                "pago_a_agregadores": por_ano["valor"],
                "despesa_pessoal_contabil": cont_ano,
            }).dropna(how="all").fillna(0)
            comparo["excedente"] = (comparo["pago_a_agregadores"] -
                                    comparo["despesa_pessoal_contabil"]).round(2)
            excede = comparo[comparo["excedente"] > 0]
            achados.append(Finding(
                id="opacidade-conciliacao-pessoal", frente=FRENTE,
                gravidade="media" if len(excede) >= 3 else "info",
                titulo="Pagamentos a rótulos agregados × despesa de pessoal "
                       "registrada na contabilidade",
                descricao=(
                    "Se o rótulo agregado fosse apenas folha de pagamento, o valor "
                    "pago deveria ficar em linha com a despesa de pessoal "
                    "contabilizada. O excedente indica que há, dentro do agregado, "
                    "verbas que não são salário — tipicamente diárias, indenizações "
                    "e benefícios pagos em lote."),
                evidencia=comparo.reset_index(),
                serie=comparo[["pago_a_agregadores", "despesa_pessoal_contabil"]],
                grafico="bar",
                metricas={"anos_com_excedente": int(len(excede)),
                          "excedente_total": round(float(excede["excedente"].sum()), 2)
                          if not excede.empty else 0.0},
            ))

    return achados
