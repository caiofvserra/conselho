"""Frente — Para onde vai o dinheiro: despesa-meio × despesa-fim.

Responde à pergunta central do usuário: quanto do orçamento sustenta a máquina
(remuneração, diárias, quilometragem, custeio administrativo) e quanto chega na
atividade-fim de um conselho (fiscalização do exercício profissional,
atendimento ao profissional, ações institucionais).

Fontes, em ordem de preferência:
1. `despesas_centro_custo` — traz o CENTRO DE CUSTO, que é a melhor evidência de
   finalidade (ex.: "Fiscalização", "Câmaras", "Superintendência").
2. `comparativo_despesa` — despesa realizada por conta contábil, por exercício.
3. `pagamentos` — usado como reforço quando as fontes contábeis faltarem.
"""
from __future__ import annotations

import pandas as pd

from .. import analytics as A
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl
from .common import df

FRENTE = "Para onde vai o dinheiro"


COLS = ["_ano", "valor", "categoria", "funcao", "unidade", "rotulo"]


def _base_despesa(staged) -> tuple[pd.DataFrame, str]:
    """Devolve (DataFrame com _ano, valor, categoria, funcao, unidade, rotulo), fonte.

    `categoria` é o eixo NATUREZA (o que se comprou) e `funcao` é o eixo FUNÇÃO
    (para que unidade se comprou) — medidos separadamente, ver taxonomia.py.
    """
    cc = df(staged, "despesas_centro_custo")
    if not cc.empty and "VALOR_TOTAL" in cc:
        d = cc.copy()
        d["valor"] = pd.to_numeric(d["VALOR_TOTAL"], errors="coerce").fillna(0)
        if "ANALITICO" in d:                       # evita somar totalizadores
            d = d[d["ANALITICO"].astype(bool)]
        d["unidade"] = coluna(d, "NOME_CENTRO_CUSTO").fillna("").astype(str)
        d["categoria"] = T.classificar_series(coluna(d, "NOME_CONTA"), d["unidade"])
        d["funcao"] = T.classificar_funcao_series(d["unidade"], d["categoria"])
        d["rotulo"] = d["unidade"] + " / " + coluna(d, "NOME_CONTA").fillna("")
        return d[COLS], "centro de custo"

    comp = df(staged, "comparativo_despesa")
    if not comp.empty:
        valor_col = ("REALIZADO_EXERCICIO" if "REALIZADO_EXERCICIO" in comp
                     else "TOTAL_EXERCICIO")
        if valor_col in comp:
            d = comp.copy()
            if "ANALITICA" in d:
                d = d[d["ANALITICA"].astype(bool)]
            d["valor"] = pd.to_numeric(d[valor_col], errors="coerce").fillna(0)
            d["categoria"] = T.classificar_series(coluna(d, "NOME"))
            # sem centro de custo esta fonte não mede função: só o sinal fraco
            d["unidade"] = ""
            d["funcao"] = T.classificar_funcao_series(d["unidade"], d["categoria"])
            d["rotulo"] = coluna(d, "NOME")
            return d[COLS], "conta contábil"

    pg = df(staged, "pagamentos")
    if not pg.empty and "VALOR" in pg:
        d = pg.copy()
        d["valor"] = d["VALOR"]
        d["categoria"] = T.classificar_series(coluna(d, "CONTA_EMPENHO"))
        d["unidade"] = coluna(d, "CENTRO_CUSTO").fillna("").astype(str)
        d["funcao"] = T.classificar_funcao_series(d["unidade"], d["categoria"])
        d["rotulo"] = coluna(d, "CONTA_EMPENHO")
        return d[COLS], "pagamentos"

    return pd.DataFrame(), "indisponível"


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    base, fonte = _base_despesa(staged)
    if base.empty:
        return achados
    base = base[base["valor"] > 0]
    base["macro"] = T.macro(base["categoria"])

    # ------------------------------------------------ 1. panorama anual macro
    serie_macro = A.serie_anual(base, "valor", by="macro")
    if serie_macro.empty:
        return achados
    pct = serie_macro.div(serie_macro.sum(axis=1).replace(0, pd.NA), axis=0) * 100

    ult = int(serie_macro.index[-1])
    pess = float(pct.get("Pessoal e verbas", pd.Series(dtype=float)).get(ult, 0))
    fim = float(pct.get("Finalística", pd.Series(dtype=float)).get(ult, 0))
    achados.append(Finding(
        id="finalistica-panorama", frente=FRENTE, gravidade="info",
        titulo=f"Composição anual da despesa por finalidade (fonte: {fonte})",
        descricao=(
            f"Toda a despesa realizada foi classificada em grupos de finalidade a "
            f"partir do nome da conta e do centro de custo. Em {ult}, "
            f"{pess:.1f}% foi para pessoal e verbas pessoais e {fim:.1f}% para a "
            f"atividade-fim (fiscalização, atendimento e ações institucionais)."),
        evidencia=serie_macro.reset_index(),
        serie=serie_macro, grafico="area_stacked",
        tabela_anual=pct.round(1).reset_index(),
        metricas={"ultimo_ano": ult, "pct_pessoal": round(pess, 1),
                  "pct_finalistica": round(fim, 1),
                  "cobertura": T.cobertura(base["categoria"], base["valor"])},
    ))

    # ------------------------------------------------ 2. razão meio/fim
    if "Finalística" in serie_macro.columns:
        meio = (serie_macro.get("Pessoal e verbas", 0) +
                serie_macro.get("Administrativo (meio)", 0))
        fim_s = serie_macro["Finalística"].replace(0, pd.NA)
        razao = (meio / fim_s).dropna()
        if not razao.empty:
            piorou = len(razao) > 1 and razao.iloc[-1] > razao.iloc[0]
            achados.append(Finding(
                id="finalistica-razao-meio-fim", frente=FRENTE,
                gravidade="media" if float(razao.mean()) > 4 else "info",
                titulo=f"Para cada R$ 1 na atividade-fim, o conselho gasta "
                       f"R$ {razao.iloc[-1]:.1f} com a máquina ({int(razao.index[-1])})",
                descricao=(
                    "Razão entre despesa-meio (pessoal, verbas pessoais e custeio "
                    "administrativo) e despesa finalística (fiscalização, "
                    "atendimento, ações institucionais). Quanto maior, menor a "
                    "parcela da anuidade do profissional que retorna como serviço. "
                    + ("A razão piorou ao longo do período." if piorou else "")),
                evidencia=razao.to_frame("razao_meio_fim").round(2).reset_index(),
                serie=razao.to_frame("R$ de meio por R$ de fim").round(2),
                grafico="line", unidade="x",
                metricas={"razao_media": round(float(razao.mean()), 2),
                          "razao_ultimo": round(float(razao.iloc[-1]), 2)},
            ))

    # ------------------------------------------------ 3. detalhe por categoria
    serie_cat = A.serie_anual(base, "valor", by="categoria")
    achados.append(Finding(
        id="finalistica-categorias", frente=FRENTE, gravidade="info",
        titulo="Despesa anual por categoria de finalidade (detalhe)",
        descricao="Abertura das 12 categorias: remuneração, verbas pessoais, "
                  "custeio, serviços profissionais, publicidade, fiscalização, "
                  "atendimento, institucional, transferências, investimento, "
                  "financeiras/judiciais e não classificado.",
        evidencia=serie_cat.reset_index(),
        serie=serie_cat, grafico="bar_stacked",
    ))

    # ------------------------------------------------ 4. verbas pessoais x fim
    if {"VERBAS_PESSOAIS"} <= set(serie_cat.columns):
        verbas = serie_cat["VERBAS_PESSOAIS"]
        fiscal = serie_cat.get("FISCALIZACAO", pd.Series(0, index=serie_cat.index))
        comp = pd.DataFrame({"Verbas pessoais (diárias, km, jetons)": verbas,
                             "Fiscalização": fiscal})
        maior = comp[comp["Verbas pessoais (diárias, km, jetons)"] >
                     comp["Fiscalização"]]
        achados.append(Finding(
            id="finalistica-verbas-vs-fiscalizacao", frente=FRENTE,
            gravidade="alta" if len(maior) >= max(len(comp) // 2, 1) else "media",
            titulo=(f"Em {len(maior)} de {len(comp)} exercícios, o gasto com "
                    "diárias/km/jetons superou o gasto com fiscalização"),
            descricao=(
                "Comparação direta entre a verba paga a pessoas para se deslocar e "
                "o custo da atividade que justifica a existência do conselho. "
                "Fiscalizar o exercício profissional é a competência legal "
                "primária (Lei 5.194/1966, art. 34); verba indenizatória é meio, "
                "não fim."),
            evidencia=comp.reset_index(),
            serie=comp, grafico="bar",
            base_legal="Lei 5.194/1966, arts. 24 e 34 (finalidade dos Creas); "
                       "Lei 4.320/1964, art. 63 (liquidação da despesa).",
            metricas={"anos_verba_supera_fiscalizacao": int(len(maior)),
                      "total_verbas": round(float(verbas.sum()), 2),
                      "total_fiscalizacao": round(float(fiscal.sum()), 2)},
        ))

    # ------------------------------------------------ 4b. matriz natureza × função
    mede_funcao = float(base.loc[base["funcao"] != "NAO_IDENTIFICADA",
                                 "valor"].sum())
    if mede_funcao > 0:
        m = T.matriz(base["categoria"], base["funcao"], base["valor"])
        serie_funcao = A.serie_anual(base, "valor", by="funcao")
        serie_funcao = serie_funcao.rename(columns=T.FUNCOES)
        total_f = float(base["valor"].sum())
        pct_fiscal = 100 * float(
            base.loc[base["funcao"] == "FISCALIZACAO", "valor"].sum()) / total_f
        pct_direcao = 100 * float(
            base.loc[base["funcao"] == "DIRECAO_SUPERIOR", "valor"].sum()) / total_f
        achados.append(Finding(
            id="finalistica-matriz", frente=FRENTE,
            gravidade="alta" if pct_direcao > pct_fiscal else "info",
            titulo=(f"Fiscalização levou {pct_fiscal:.1f}% da despesa; direção "
                    f"superior levou {pct_direcao:.1f}%"),
            descricao=(
                "Matriz de dois eixos: **natureza** (o que se comprou — salário, "
                "diária, aluguel, obra) nas linhas e **função** (para que unidade "
                "— fiscalização, atendimento, direção superior, administração) nas "
                "colunas. Um salário de agente fiscal e um salário de assessor de "
                "gabinete têm a mesma natureza e funções opostas; só o cruzamento "
                "distingue 'gasta muito com pessoal' de 'gasta muito com pessoal "
                "que não fiscaliza'. A coluna de função vem do centro de custo — "
                "quando ele falta, a função é inferida da conta e o resíduo cai "
                "em 'não identificada'."),
            evidencia=m.reset_index().rename(columns={"index": "natureza"}),
            serie=serie_funcao, grafico="area_stacked",
            tabela_anual=serie_funcao.reset_index(),
            base_legal="Lei 5.194/1966, art. 34 (fiscalizar o exercício "
                       "profissional é a competência primária do Crea); Lei "
                       "4.320/1964, art. 15 (despesa por elemento).",
            metricas={"pct_fiscalizacao": round(pct_fiscal, 2),
                      "pct_direcao_superior": round(pct_direcao, 2),
                      "pct_funcao_identificada": round(100 * mede_funcao / total_f, 1)},
        ))

        # verbas pessoais que saíram de unidades políticas/direção
        verbas_pol = base[(base["categoria"] == "VERBAS_PESSOAIS") &
                          (base["funcao"].isin(["POLITICO", "DIRECAO_SUPERIOR"]))]
        if not verbas_pol.empty:
            s = A.serie_anual(verbas_pol, "valor", by="funcao").rename(
                columns=T.FUNCOES)
            total_vp = float(verbas_pol["valor"].sum())
            achados.append(Finding(
                id="finalistica-verbas-politicas", frente=FRENTE, gravidade="alta",
                titulo=f"{fmt_brl(total_vp)} em diárias, km e jetons alocados a "
                       "órgãos políticos e à direção superior",
                descricao=(
                    "Verba indenizatória paga dentro das unidades de decisão — "
                    "plenário, câmaras, inspetorias, presidência e diretoria. É a "
                    "interseção mais sensível dos dois eixos: quem autoriza a "
                    "despesa é do mesmo colegiado que a recebe, e a verba não "
                    "aparece no contracheque publicado."),
                evidencia=(verbas_pol.groupby(["_ano", "unidade"])["valor"]
                           .sum().reset_index()
                           .sort_values("valor", ascending=False).head(100)),
                serie=s, grafico="bar_stacked", tabela_anual=s.reset_index(),
                base_legal="CF, art. 37, caput (impessoalidade e moralidade); "
                           "Lei 4.320/1964, art. 63 (liquidação: comprovação do "
                           "serviço prestado).",
                como_verificar="Pedir as portarias de concessão e os relatórios de "
                               "viagem das unidades listadas.",
            ))

    # ------------------------------------------------ 5. qualidade da classificação
    cob = T.cobertura(base["categoria"], base["valor"])
    if cob.get("pct_nao_classificado", 0) > 15:
        achados.append(Finding(
            id="finalistica-cobertura", frente=FRENTE, gravidade="info",
            titulo=f"{cob['pct_nao_classificado']:.0f}% da despesa não pôde ser "
                   "classificada por finalidade",
            descricao=(
                "Contas cujo nome não permitiu enquadramento automático. Rodar com "
                "`QUEDA_ANTHROPIC_API_KEY` definida aciona a classificação por LLM "
                "e reduz este resíduo; a lista abaixo também serve para refinar as "
                "regras determinísticas."),
            evidencia=T.maiores_nao_classificados(
                base["rotulo"], base["categoria"], base["valor"]),
            metricas=cob,
        ))

    # ------------------------------------------------ 6. centros de custo
    if fonte == "centro de custo":
        cc = df(staged, "despesas_centro_custo")
        if not cc.empty and "NOME_CENTRO_CUSTO" in cc:
            d = cc.copy()
            d["valor"] = pd.to_numeric(d.get("VALOR_TOTAL"), errors="coerce").fillna(0)
            achados.append(Finding(
                id="finalistica-centros-custo", frente=FRENTE, gravidade="info",
                titulo="Maiores centros de custo por ano",
                descricao="Onde a despesa foi alocada dentro da estrutura do "
                          "conselho, ano a ano.",
                evidencia=A.ranking_anual(d, "NOME_CENTRO_CUSTO", "valor", top=15),
                tabela_anual=A.ranking_anual(d, "NOME_CENTRO_CUSTO", "valor", top=10),
            ))

    return achados
