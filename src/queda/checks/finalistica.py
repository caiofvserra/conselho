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
    # o LLM (se houver chave) resolve o resíduo que o regex não enquadrou
    base["categoria"], info_llm = T.refinar_residuo(
        base["rotulo"], base["categoria"], base["valor"])
    base["funcao"] = T.classificar_funcao_series(base["unidade"], base["categoria"])
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
            # Esta é a frase mais citável do relatório inteiro, e por isso a
            # que precisa do denominador mais explícito. Na rodada real ela
            # saiu como "R$ 14,9 com a máquina para cada R$ 1 na atividade-fim"
            # — verdadeira dentro da classificação, mas construída sobre uma
            # atividade-fim de 1,35% da despesa, identificada POR NÓS a partir
            # do nome do centro de custo. Um denominador dessa ordem torna a
            # razão extremamente sensível: reclassificar um único centro de
            # custo grande muda o número inteiro.
            #
            # A razão continua publicada, porque a assimetria é real e é o
            # ponto. O que muda é que ela passa a carregar o tamanho do próprio
            # denominador — e deixa de ser achado de gravidade média quando o
            # denominador é pequeno demais para sustentar a frase.
            fim_ult = float(serie_macro["Finalística"].iloc[-1])
            tot_ult = float(serie_macro.iloc[-1].sum())
            pct_fim = 100 * fim_ult / tot_ult if tot_ult else 0.0
            frágil = pct_fim < 5.0
            achados.append(Finding(
                id="finalistica-razao-meio-fim", frente=FRENTE,
                gravidade=("info" if frágil else
                           "media" if float(razao.mean()) > 4 else "info"),
                titulo=(f"Para cada R$ 1 na atividade-fim, o conselho gasta "
                        f"R$ {razao.iloc[-1]:.1f} nas demais funções "
                        f"({int(razao.index[-1])}) — atividade-fim identificada: "
                        f"{pct_fim:.1f}% da despesa"),
                cobertura={"base": fim_ult, "universo": tot_ult,
                           "o_que": "a despesa que a ferramenta identificou "
                                    "como atividade-fim no último exercício, "
                                    "que é o denominador desta razão"},
                descricao=(
                    "Razão entre despesa-meio (pessoal, verbas pessoais e custeio "
                    "administrativo) e despesa finalística (fiscalização, "
                    "atendimento, ações institucionais). Quanto maior, menor a "
                    "parcela da anuidade do profissional que retorna como serviço. "
                    + ("A razão piorou ao longo do período. " if piorou else "")
                    + f"\n\nO denominador desta razão é pequeno: a atividade-fim "
                    f"identificada foi {pct_fim:.1f}% da despesa do exercício. "
                    "Isso importa porque a razão é aritmeticamente sensível a "
                    "ele — reclassificar um único centro de custo relevante "
                    "muda o resultado por inteiro. A identificação é da "
                    "ferramenta, pelo nome do centro de custo, não uma "
                    "declaração do conselho."
                    + ("\n\n**Publicado como informação, não como achado.** Com "
                       "a atividade-fim abaixo de 5% da despesa, o número não "
                       "tem estabilidade suficiente para ser citado isolado: "
                       "use-o junto da tabela de centros de custo, que mostra "
                       "onde o dinheiro efetivamente está."
                       if frágil else "")),
                evidencia=razao.to_frame("razao_meio_fim").round(2).reset_index(),
                serie=razao.to_frame("R$ de meio por R$ de fim").round(2),
                grafico="line", unidade="x",
                como_verificar=("Pedir a classificação funcional da despesa que "
                                "o próprio conselho usa no seu plano de ação e "
                                "no relatório de gestão, e refazer a razão com "
                                "ela — aí o denominador deixa de ser nosso."),
                metricas={"razao_media": round(float(razao.mean()), 2),
                          "razao_ultimo": round(float(razao.iloc[-1]), 2),
                          "pct_atividade_fim": round(pct_fim, 2),
                          "denominador_fragil": bool(frágil)},
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
        fiscal_cat = serie_cat.get("FISCALIZACAO", pd.Series(0, index=serie_cat.index))
        # A MESMA medida de fiscalização dos demais achados (função do centro
        # de custo) quando ela existe: pela conta contábil, "fiscalização"
        # dava R$ 0 em três exercícios (rodada 10) enquanto o centro de custo
        # media R$ 43 mi no mesmo período — 163× de diferença dentro do mesmo
        # relatório, e "R$ 0" induz a leitura vetada de que não se fiscaliza.
        fiscal_func = (base[base["funcao"] == "FISCALIZACAO"].groupby("_ano")["valor"].sum()
                       if "funcao" in base.columns else pd.Series(dtype=float))
        usa_funcao = not fiscal_func.empty and float(fiscal_func.sum()) > float(fiscal_cat.sum())
        fiscal = fiscal_func.reindex(verbas.index).fillna(0) if usa_funcao else fiscal_cat
        medida = ("despesa alocada a centros de custo de fiscalização" if usa_funcao
                  else "contas contábeis rotuladas como fiscalização")
        comp = pd.DataFrame({"Verbas pessoais (diárias, km, jetons)": verbas,
                             "Fiscalização": fiscal})
        if usa_funcao:
            comp = comp[comp["Fiscalização"] > 0]      # só exercícios com centro de custo
        maior = comp[comp["Verbas pessoais (diárias, km, jetons)"] >
                     comp["Fiscalização"]]
        anos_c = [int(a) for a in comp.index]
        periodo = (f"{min(anos_c)}–{max(anos_c)}" if len(anos_c) > 1
                   else (str(anos_c[0]) if anos_c else "—"))
        zerados = [int(a) for a, v in comp["Fiscalização"].items() if v <= 0]
        achados.append(Finding(
            id="finalistica-verbas-vs-fiscalizacao", frente=FRENTE,
            gravidade="alta" if len(maior) >= max(len(comp) // 2, 1) and not zerados else "media",
            titulo=(f"Em {len(maior)} de {len(comp)} exercícios ({periodo}), o gasto com "
                    f"diárias/km/jetons superou o gasto com fiscalização — medido por {medida}"),
            descricao=(
                "Comparação direta entre a verba paga a pessoas para se deslocar e "
                "o custo da atividade que justifica a existência do conselho. "
                "Fiscalizar o exercício profissional é a competência legal "
                "primária (Lei 5.194/1966, art. 34); verba indenizatória é meio, "
                f"não fim. Medida de fiscalização: {medida}."
                + (f" Nos exercícios {zerados} a medida devolveu zero — isso é "
                   "limite de classificação da ferramenta (nenhuma conta ou centro "
                   "de custo reconhecido como fiscalização naquele ano), não gasto "
                   "zero do órgão." if zerados else "")),
            evidencia=comp.reset_index(),
            serie=comp, grafico="bar",
            base_legal="Lei 5.194/1966, arts. 24 e 34 (finalidade dos Creas); "
                       "Lei 4.320/1964, art. 63 (liquidação da despesa).",
            metricas={"anos_verba_supera_fiscalizacao": int(len(maior)),
                      "total_verbas": round(float(comp["Verbas pessoais (diárias, km, jetons)"].sum()), 2),
                      "total_fiscalizacao": round(float(comp["Fiscalização"].sum()), 2),
                      "medida": medida, "periodo": periodo,
                      "exercicios_sem_medida": zerados},
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
                    "interseção mais sensível dos dois eixos: os valores são "
                    "fixados por resolução do Confea, pagos pelo próprio conselho "
                    "e não aparecem no contracheque publicado."),
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
                "Contas cujo nome não permitiu enquadramento automático — a "
                "medida de confiabilidade de toda esta frente. "
                + (f"O modelo local resolveu {info_llm.get('rubricas', 0)} "
                   f"rubrica(s) deste resíduo "
                   f"({fmt_brl(info_llm['valor_reclassificado'])}); o que "
                   "restou está abaixo."
                   if info_llm.get("reclassificadas") else
                   "Rodar `python main.py llm` prepara o modelo local (Ollama) "
                   "e reduz este resíduo.")
                + " A lista também serve para refinar as regras determinísticas."),
            evidencia=T.maiores_nao_classificados(
                base["rotulo"], base["categoria"], base["valor"]),
            metricas={**cob, "llm": info_llm},
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
