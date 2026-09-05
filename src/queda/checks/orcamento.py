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


def _execucao_anual(staged) -> tuple[pd.DataFrame, str]:
    """orçado → empenhado → liquidado → pago por exercício.

    O `demonstrativo` vem em ÁRVORE: cada conta sintética repete a soma das
    filhas. Somar tudo conta o mesmo real três ou quatro vezes — foi o que
    aconteceu na primeira rodada (empenhado 3,2× e pago 4,6× acima do que os
    próprios datasets de empenhos e pagamentos registram). Só as FOLHAS entram.
    """
    dem = df(staged, "demonstrativo")
    if not dem.empty and "ORCADO" in dem:
        folhas = dem
        if "_folha" in dem.columns:
            folhas = dem[dem["_folha"].astype(bool)]
        elif "_nivel" in dem.columns:          # sem marca de folha: nível máximo
            folhas = dem[dem["_nivel"] == dem["_nivel"].max()]
        if folhas.empty:
            folhas = dem
        return pd.DataFrame({
            "orçado": _num(folhas, "ORCADO").groupby(folhas["_ano"]).sum(),
            "empenhado": _num(folhas, "EMPENHADOEXERCICIO").groupby(folhas["_ano"]).sum(),
            "liquidado": _num(folhas, "LIQUIDADO_EXERCICIO").groupby(folhas["_ano"]).sum(),
            "pago": _num(folhas, "PAGO_EXERCICIO").groupby(folhas["_ano"]).sum(),
        }), "demonstrativo"
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
        }), "balanco"
    return pd.DataFrame(), "indisponivel"


def run(staged) -> list[Finding]:
    achados: list[Finding] = []

    # ------------------------------------------------ 1. execução do orçamento
    exec_, fonte_exec = _execucao_anual(staged)
    # A conferência só faz sentido contra o DEMONSTRATIVO, que vem em árvore.
    # O balanço orçamentário já é consolidado e não tem risco de dupla soma.
    if (not exec_.empty and float(exec_["orçado"].sum()) > 0
            and fonte_exec == "demonstrativo"):
        exec_ = exec_[exec_["orçado"] > 0].round(2)
        # Teste de consistência DENTRO da própria árvore: a soma das contas
        # sintéticas de nível 0 tem de bater com a soma das folhas, porque são
        # o mesmo dinheiro visto em dois níveis. Se não bate, o achatamento
        # perdeu a marca de folha e a série está contando o mesmo real duas ou
        # três vezes — foi o que inflou empenhado em 3,2x e pago em 4,6x.
        dem = df(staged, "demonstrativo")
        conferencia = []
        if {"_folha", "_nivel"} <= set(dem.columns):
            folhas = dem[dem["_folha"].astype(bool)]
            raizes = dem[dem["_nivel"] == dem["_nivel"].min()]
            for etapa, col in (("orçado", "ORCADO"),
                               ("empenhado", "EMPENHADOEXERCICIO"),
                               ("pago", "PAGO_EXERCICIO")):
                sf = float(_num(folhas, col).sum())
                sr = float(_num(raizes, col).sum())
                if sr <= 0:
                    continue
                conferencia.append({"etapa": etapa, "soma_folhas": round(sf, 2),
                                    "soma_sinteticas_raiz": round(sr, 2),
                                    "razao": round(sf / sr, 3)})
        divergente = [c for c in conferencia
                      if c["razao"] > 1.15 or c["razao"] < 0.85]
        if divergente:
            achados.append(Finding(
                id="orcamento-execucao-inconsistente", frente=FRENTE,
                gravidade="alta",
                titulo=("A árvore do demonstrativo não fecha entre folhas e "
                        "sintéticas — série de execução suprimida"),
                descricao=(
                    "O demonstrativo vem em árvore: cada conta sintética "
                    "repete a soma das filhas, e as duas somas têm de bater. "
                    "Quando não batem, ou o achatamento perdeu a marca de "
                    "folha — e então o mesmo real é contado duas ou três "
                    "vezes —, ou o portal publica níveis incompletos. Nos dois "
                    "casos a série **não foi publicada**: preferimos não "
                    "mostrar número a mostrar número errado."),
                evidencia=pd.DataFrame(conferencia),
                metricas={"conferencia": conferencia},
                como_verificar="Conferir se o endpoint devolve as contas "
                               "sintéticas junto com as analíticas e se o "
                               "achatamento preserva `_folha`.",
            ))
            exec_ = pd.DataFrame()

    if not exec_.empty:
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
                        "Verba indenizatória é concedida caso a caso e não passa "
                        "por licitação, então a execução pode se afastar da dotação "
                        "aprovada. Comparar dotação e execução ano a ano mostra se "
                        "o excesso é exceção ou regra — e em quantos exercícios "
                        "houve reformulação orçamentária para acomodá-lo."),
                    evidencia=por_ano.reset_index(),
                    serie=por_ano[["orcado", "realizado"]].rename(
                        columns={"orcado": "dotação aprovada",
                                 "realizado": "executado"}),
                    grafico="bar", tabela_anual=por_ano.reset_index(),
                    base_legal="Lei 4.320/1964, art. 59 (o empenho não pode exceder "
                               "o limite dos créditos concedidos).",
                    metricas={"anos_acima": acima.index.astype(int).tolist()},
                ))

    # ------------------------------------------------ 2b. conciliação da verba
    #
    # Duas fontes independentes medem a mesma verba indenizatória: o que foi
    # PAGO (fluxo, conta a conta) e o que foi REALIZADO na peça orçamentária.
    # As duas deveriam convergir. Divergência grande não é irregularidade por
    # si — regime de caixa e de competência diferem —, mas é a pergunta que
    # ninguém fez ainda, e o tamanho dela define se cabe explicação contábil.
    pg = df(staged, "pagamentos")
    # `d` só existe se o bloco de desvio por conta rodou (comparativo com
    # orçado e realizado). Sem ele não há o lado contábil da conciliação.
    if not pg.empty and isinstance(locals().get("d"), pd.DataFrame) \
            and "categoria" in locals()["d"].columns:
        conta_pg = coluna(pg, "CONTA_EMPENHO").fillna("").astype(str)
        nat_pg = conta_pg.map({c: T.classificar_texto(c)
                               for c in conta_pg.unique()})
        pago = (pg[nat_pg == "VERBAS_PESSOAIS"]
                .groupby("_ano")["VALOR"].sum())
        realizado = (d[d["categoria"] == "VERBAS_PESSOAIS"]
                     .groupby("_ano")["realizado"].sum())
        conc = pd.DataFrame({"pago (fluxo)": pago,
                             "realizado (contabilidade)": realizado}).dropna()
        if len(conc) >= 3 and float(conc.sum().sum()) > 0:
            conc["diferenca"] = (conc["pago (fluxo)"] -
                                 conc["realizado (contabilidade)"]).round(2)
            conc["divergencia_pct"] = (
                100 * conc["diferenca"] /
                conc["realizado (contabilidade)"].replace(0, pd.NA)
            ).fillna(0).round(1)
            tot_pago = float(conc["pago (fluxo)"].sum())
            tot_real = float(conc["realizado (contabilidade)"].sum())
            div = tot_pago - tot_real
            pct = 100 * div / tot_real if tot_real else 0.0
            achados.append(Finding(
                id="orcamento-conciliacao-verba", frente=FRENTE,
                gravidade="alta" if abs(pct) > 25 else "media",
                titulo=(f"Verba indenizatória: {fmt_brl(tot_pago)} pagos contra "
                        f"{fmt_brl(tot_real)} registrados na contabilidade "
                        f"({pct:+.0f}%)"),
                descricao=(
                    "Duas fontes independentes do próprio portal medem a mesma "
                    "coisa: o que foi **pago** (fluxo, somado conta a conta nos "
                    "pagamentos) e o que foi **realizado** (peça orçamentária). "
                    "Deveriam convergir.\n\n"
                    "Divergência não é irregularidade por si — regime de caixa "
                    "e de competência diferem, e restos a pagar deslocam valor "
                    "entre exercícios. Mas uma diferença desta ordem precisa de "
                    "explicação contábil, e a pergunta ainda não foi feita: ou "
                    "há verba paga fora da dotação, ou a peça orçamentária não "
                    "reflete o desembolso. A tabela mostra a diferença ano a "
                    "ano, que é o que permite distinguir defasagem temporal "
                    "(oscila de sinal) de divergência estrutural (mesmo sinal "
                    "todo ano)."),
                evidencia=conc.reset_index(),
                serie=conc[["pago (fluxo)", "realizado (contabilidade)"]],
                grafico="bar",
                tabela_anual=conc.reset_index(),
                base_legal="Lei 4.320/1964, arts. 35 (regime), 60 (empenho "
                           "prévio) e 63 (liquidação); art. 85 (escrituração "
                           "sintética e analítica).",
                como_verificar="Pedir a conciliação entre o razão das contas de "
                               "verba e o relatório de pagamentos do mesmo "
                               "exercício, com os restos a pagar destacados.",
                metricas={"pago": round(tot_pago, 2),
                          "realizado": round(tot_real, 2),
                          "diferenca": round(div, 2),
                          "divergencia_pct": round(pct, 1),
                          "sinal_constante": bool(
                              (conc["diferenca"] > 0).all() or
                              (conc["diferenca"] < 0).all())},
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
                # Média só dos exercícios em que a fiscalização foi MEDIDA:
                # com centro de custo só a partir de 2021, os anos anteriores
                # entram com zero e a média de 13 anos (0,7%) era média de 8
                # zeros de ausência de dado com 5 anos reais (1,8%).
                medidos = comp2[comp2["fiscalizacao"] > 0]
                if medidos.empty:
                    medidos = comp2
                medio = float(medidos["% da receita em fiscalização"].mean())
                anos_med = [int(a) for a in medidos.index]
                periodo = (f"{min(anos_med)}–{max(anos_med)}" if len(anos_med) > 1
                           else str(anos_med[0]))
                achados.append(Finding(
                    id="orcamento-retorno-da-anuidade", frente=FRENTE,
                    gravidade="alta" if medio < 10 else "media",
                    # "receita arrecadada dos profissionais" não é a receita
                    # TOTAL: entram aqui aluguel de imóvel próprio, rendimento
                    # de aplicação e recuperação de despesa, que ninguém pagou
                    # como contribuição compulsória. O denominador é o que é —
                    # receita total — e o título passa a dizer isso, em vez de
                    # sugerir uma base mais estreita e mais favorável ao número.
                    titulo=(f"Da receita total do conselho, {medio:.1f}% em "
                            f"média ({periodo}) foi alocado a fiscalização — "
                            f"medido por {medida}"),
                    cobertura={"base": float(comp2["fiscalizacao"].sum()),
                               "universo": float(comp2["receita"].sum()),
                               "o_que": "o gasto que a ferramenta identificou "
                                        "como fiscalização, medido contra toda "
                                        "a receita do exercício"},
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
