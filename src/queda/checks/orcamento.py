"""Frente — Orçamento: o que foi prometido × o que foi gasto.

O orçamento é a peça que o conselho aprova em plenário e publica. Comparar a
dotação com a execução responde a duas perguntas que o gasto sozinho não
responde:

* **O desvio é sistemático?** Uma conta que estoura todo ano não é imprevisto,
  é subestimação deliberada na peça orçamentária — a despesa é aprovada pequena
  e executada grande, contornando o debate do plenário.
* **Quanto da anuidade volta como serviço?** A receita arrecadada dos
  profissionais é o denominador natural: dividir a despesa finalística pela
  receita mede a devolução em serviço, e é comparável entre exercícios.

Fontes: `comparativo_despesa` (VALOR_ORCADO × REALIZADO_EXERCICIO por conta),
`demonstrativo` (orçado → empenhado → liquidado → pago), `balanco_orcamentario`
e `comparativo_receita`.
"""
from __future__ import annotations

import pandas as pd

from .. import analytics as A
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl
from .common import df

FRENTE = "Orçamento"

# Um desvio pequeno é execução normal. Estes são os cortes que separam ruído de
# subestimação: mais da metade acima do orçado, com valor material.
ESTOURO_PCT = 50.0
PISO_CONTA = 200_000.0


def _num(d: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(coluna(d, col, 0), errors="coerce").fillna(0)


def _receita_anual(staged) -> pd.Series:
    """Receita realizada por exercício, de qualquer fonte que a traga."""
    rec = df(staged, "comparativo_receita")
    if not rec.empty:
        col = ("REALIZADO_EXERCICIO" if "REALIZADO_EXERCICIO" in rec
               else "TOTAL_EXERCICIO")
        if col in rec:
            d = rec.copy()
            if "ANALITICA" in d:
                d = d[d["ANALITICA"].astype(bool)]
            s = d.groupby("_ano")[col].sum()
            if float(s.sum()) > 0:
                return s
    bal = df(staged, "balanco_orcamentario")
    if not bal.empty and "RECEITAS_REALIZADAS" in bal:
        s = bal.groupby("_ano")["RECEITAS_REALIZADAS"].sum()
        if float(s.sum()) > 0:
            return s
    return pd.Series(dtype=float)


def _execucao_anual(staged) -> pd.DataFrame:
    """orçado → empenhado → liquidado → pago por exercício."""
    dem = df(staged, "demonstrativo")
    if not dem.empty and "ORCADO" in dem:
        return pd.DataFrame({
            "orçado": _num(dem, "ORCADO").groupby(dem["_ano"]).sum(),
            "empenhado": _num(dem, "EMPENHADOEXERCICIO").groupby(dem["_ano"]).sum(),
            "liquidado": _num(dem, "LIQUIDADO_EXERCICIO").groupby(dem["_ano"]).sum(),
            "pago": _num(dem, "PAGO_EXERCICIO").groupby(dem["_ano"]).sum(),
        })
    bal = df(staged, "balanco_orcamentario")
    if not bal.empty and "DOTACOES" in bal:
        base = _num(bal, "DOTACOES_ATUALIZADAS")
        if float(base.sum()) <= 0:
            base = _num(bal, "DOTACOES")
        return pd.DataFrame({
            "orçado": base.groupby(bal["_ano"]).sum(),
            "empenhado": _num(bal, "DESPESAS_EMPENHADAS").groupby(bal["_ano"]).sum(),
            "liquidado": _num(bal, "DESPESAS_LIQUIDADAS").groupby(bal["_ano"]).sum(),
            "pago": _num(bal, "DESPESAS_PAGAS").groupby(bal["_ano"]).sum(),
        })
    return pd.DataFrame()


def run(staged) -> list[Finding]:
    achados: list[Finding] = []

    # ------------------------------------------------ 1. execução do orçamento
    exec_ = _execucao_anual(staged)
    if not exec_.empty and float(exec_["orçado"].sum()) > 0:
        exec_ = exec_[exec_["orçado"] > 0].round(2)
        exec_["% executado"] = (100 * exec_["pago"] / exec_["orçado"]).round(1)
        # empenhado mas não liquidado = compromisso que atravessa o exercício
        exec_["a liquidar"] = (exec_["empenhado"] - exec_["liquidado"]).round(2)
        achados.append(Finding(
            id="orcamento-execucao", frente=FRENTE, gravidade="info",
            titulo="Execução do orçamento por exercício: orçado, empenhado, "
                   "liquidado e pago",
            descricao=(
                "As quatro etapas da despesa pública, ano a ano. A diferença "
                "entre empenhado e liquidado é o compromisso que atravessa o "
                "exercício (restos a pagar); quando cresce de forma persistente, "
                "o orçamento do ano seguinte já nasce comprometido."),
            evidencia=exec_.reset_index(),
            serie=exec_[["orçado", "empenhado", "liquidado", "pago"]],
            grafico="bar", tabela_anual=exec_.reset_index(),
            base_legal="Lei 4.320/1964, arts. 58 (empenho), 63 (liquidação) e "
                       "36 (restos a pagar).",
        ))

    # ------------------------------------------------ 2. desvio por conta
    comp = df(staged, "comparativo_despesa")
    if not comp.empty and "VALOR_ORCADO" in comp and "REALIZADO_EXERCICIO" in comp:
        d = comp.copy()
        if "ANALITICA" in d:
            d = d[d["ANALITICA"].astype(bool)]
        d["orcado"] = _num(d, "VALOR_ORCADO")
        d["realizado"] = _num(d, "REALIZADO_EXERCICIO")
        d["conta"] = coluna(d, "NOME").fillna("").astype(str)
        d = d[(d["orcado"] > 0) & (d["realizado"] > 0)]
        if not d.empty:
            d["desvio"] = d["realizado"] - d["orcado"]
            d["pct_desvio"] = (100 * d["desvio"] / d["orcado"]).round(1)
            estouros = d[(d["pct_desvio"] > ESTOURO_PCT) &
                         (d["realizado"] >= PISO_CONTA)]
            if not estouros.empty:
                # a subestimação sistemática é a que se repete
                recorrentes = (estouros.groupby("conta")
                               .agg(exercicios=("_ano", "nunique"),
                                    excedente_total=("desvio", "sum"),
                                    pct_medio=("pct_desvio", "mean"))
                               .reset_index()
                               .sort_values("excedente_total", ascending=False))
                cronicas = recorrentes[recorrentes["exercicios"] >= 3]
                serie = A.serie_anual(estouros, "desvio")
                achados.append(Finding(
                    id="orcamento-estouro-conta", frente=FRENTE,
                    gravidade="alta" if not cronicas.empty else "media",
                    titulo=(f"{len(cronicas)} conta(s) estouraram o orçado em mais "
                            f"de {ESTOURO_PCT:.0f}% em 3 ou mais exercícios"
                            if not cronicas.empty else
                            f"{len(estouros)} conta-exercício com realizado mais de "
                            f"{ESTOURO_PCT:.0f}% acima do orçado"),
                    descricao=(
                        "Estourar o orçado uma vez é imprevisto. Estourar a mesma "
                        "conta ano após ano é subestimação na peça orçamentária: a "
                        "despesa é aprovada pequena no plenário e executada grande "
                        "depois, sem o debate que a dotação real exigiria. A coluna "
                        "`exercicios` conta em quantos anos a conta repetiu o "
                        "padrão."),
                    evidencia=recorrentes.round(2).head(60),
                    serie=serie.rename(columns={"desvio": "excedente sobre o orçado"}),
                    grafico="bar",
                    tabela_anual=(estouros[["_ano", "conta", "orcado", "realizado",
                                            "pct_desvio"]]
                                  .sort_values(["_ano", "pct_desvio"],
                                               ascending=[True, False])
                                  .round(2).head(200)),
                    base_legal="Lei 4.320/1964, arts. 2º e 48 (o orçamento é "
                               "limite, não estimativa); art. 167, II, da CF "
                               "(vedada despesa que exceda os créditos).",
                    como_verificar="Pedir os créditos adicionais (suplementares e "
                                   "especiais) abertos em cada exercício e a "
                                   "deliberação do plenário que os autorizou.",
                    metricas={"contas_cronicas": int(len(cronicas)),
                              "excedente_total": round(
                                  float(estouros["desvio"].sum()), 2)},
                ))

            # o mesmo teste restrito ao que interessa: verbas a pessoas
            d["categoria"] = T.classificar_series(d["conta"])
            verbas = d[d["categoria"] == "VERBAS_PESSOAIS"]
            if not verbas.empty:
                por_ano = verbas.groupby("_ano").agg(
                    orcado=("orcado", "sum"), realizado=("realizado", "sum"))
                por_ano["pct_do_orcado"] = (
                    100 * por_ano["realizado"] / por_ano["orcado"]).round(1)
                acima = por_ano[por_ano["pct_do_orcado"] > 100]
                achados.append(Finding(
                    id="orcamento-verbas-vs-dotacao", frente=FRENTE,
                    gravidade="alta" if len(acima) >= 3 else "media",
                    titulo=(f"Diárias, km e jetons excederam a dotação aprovada em "
                            f"{len(acima)} de {len(por_ano)} exercícios"),
                    descricao=(
                        "Verba indenizatória é a rubrica em que o orçamento mais "
                        "costuma ser figurativo: aprova-se um valor modesto e "
                        "executa-se muito acima, porque a concessão é discricionária "
                        "e não passa por licitação. Comparar dotação e execução ano "
                        "a ano mostra se o excesso é exceção ou regra."),
                    evidencia=por_ano.reset_index(),
                    serie=por_ano[["orcado", "realizado"]].rename(
                        columns={"orcado": "dotação aprovada",
                                 "realizado": "executado"}),
                    grafico="bar", tabela_anual=por_ano.reset_index(),
                    base_legal="Lei 4.320/1964, art. 59 (o empenho não pode exceder "
                               "o limite dos créditos concedidos).",
                    metricas={"anos_acima": acima.index.astype(int).tolist()},
                ))

    # ------------------------------------------------ 3. receita × atividade-fim
    receita = _receita_anual(staged)
    if not receita.empty:
        from .finalistica import _base_despesa            # evita import circular
        base, _fonte = _base_despesa(staged)
        if not base.empty:
            base = base[base["valor"] > 0]
            # O salário do agente fiscal É gasto com fiscalização, ainda que sua
            # NATUREZA seja remuneração. Quando o centro de custo foi coletado,
            # a FUNÇÃO é a medida certa; sem ele, cai-se na natureza da conta.
            total = float(base["valor"].sum())
            id_funcao = float(base.loc[base["funcao"] != "NAO_IDENTIFICADA",
                                       "valor"].sum())
            if total > 0 and id_funcao / total >= 0.30:
                eixo, alvo_fim = "funcao", ["FISCALIZACAO", "ATENDIMENTO", "POLITICO"]
                medida = "centro de custo (função da unidade)"
            else:
                eixo, alvo_fim = "categoria", ["FISCALIZACAO", "ATENDIMENTO",
                                               "INSTITUCIONAL"]
                medida = "conta contábil (natureza da despesa)"
            fim = (base[base[eixo].isin(alvo_fim)]
                   .groupby("_ano")["valor"].sum())
            fiscal = (base[base[eixo] == "FISCALIZACAO"]
                      .groupby("_ano")["valor"].sum())
            comp2 = pd.DataFrame({"receita": receita}).join(
                pd.DataFrame({"finalistica": fim, "fiscalizacao": fiscal}),
                how="left").fillna(0)
            comp2 = comp2[comp2["receita"] > 0]
            if not comp2.empty:
                comp2["% da receita na atividade-fim"] = (
                    100 * comp2["finalistica"] / comp2["receita"]).round(1)
                comp2["% da receita em fiscalização"] = (
                    100 * comp2["fiscalizacao"] / comp2["receita"]).round(1)
                medio = float(comp2["% da receita em fiscalização"].mean())
                achados.append(Finding(
                    id="orcamento-retorno-da-anuidade", frente=FRENTE,
                    gravidade="alta" if medio < 10 else "media",
                    titulo=(f"Da receita arrecadada dos profissionais, "
                            f"{medio:.1f}% em média voltou como fiscalização"),
                    descricao=(
                        "A anuidade é compulsória e existe para custear a "
                        "fiscalização do exercício profissional. Este é o "
                        "indicador de devolução: quanto de cada real arrecadado "
                        "financiou a atividade-fim (fiscalização, atendimento e "
                        f"ações institucionais) e quanto ficou na máquina. O "
                        f"enquadramento foi medido por {medida}. Para "
                        "usar como parâmetro externo, compare com o percentual "
                        "apurado pelo TCU no relatório consolidado dos conselhos "
                        "de fiscalização profissional do mesmo exercício — a "
                        "ferramenta não o presume, apenas publica o número "
                        "calculado a partir dos dados do portal."),
                    evidencia=comp2.round(2).reset_index(),
                    serie=comp2[["% da receita na atividade-fim",
                                 "% da receita em fiscalização"]],
                    grafico="line", unidade="%",
                    tabela_anual=comp2.round(2).reset_index(),
                    base_legal="Lei 5.194/1966, arts. 24 e 34 (finalidade dos "
                               "Creas); Lei 12.514/2011 (anuidades); CF, art. 70 "
                               "(economicidade).",
                    metricas={"pct_fiscalizacao_medio": round(medio, 2),
                              "receita_total": round(float(comp2["receita"].sum()), 2)},
                ))

                # e o contraponto direto: verba a pessoas sobre a mesma receita
                verbas_ano = (base[base["categoria"] == "VERBAS_PESSOAIS"]
                              .groupby("_ano")["valor"].sum())
                if float(verbas_ano.sum()) > 0:
                    v = pd.DataFrame({"receita": receita,
                                      "verbas": verbas_ano}).dropna()
                    v = v[v["receita"] > 0]
                    if not v.empty:
                        v["% da receita em diárias/km/jetons"] = (
                            100 * v["verbas"] / v["receita"]).round(2)
                        achados.append(Finding(
                            id="orcamento-verbas-sobre-receita", frente=FRENTE,
                            gravidade="info",
                            titulo=(f"{fmt_brl(float(v['verbas'].sum()))} em verbas "
                                    "a pessoas, medidos contra a receita do mesmo ano"),
                            descricao=(
                                "Normalizar pela receita neutraliza inflação e "
                                "crescimento do órgão: a série mostra se a verba "
                                "indenizatória cresceu porque o conselho cresceu ou "
                                "porque passou a consumir fatia maior da anuidade."),
                            evidencia=v.round(2).reset_index(),
                            serie=v[["% da receita em diárias/km/jetons"]],
                            grafico="line", unidade="%",
                            tabela_anual=v.round(2).reset_index(),
                        ))
    return achados
