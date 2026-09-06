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

import logging

import pandas as pd

from .. import analytics as A
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl, so_analiticas, topo
from .common import df
from .superintendencias import normalizar_unidade

FRENTE = "Para onde vai o dinheiro"

log = logging.getLogger("queda.checks.finalistica")


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
        d = so_analiticas(d)                       # evita somar totalizadores
        bruta = coluna(d, "NOME_CENTRO_CUSTO").fillna("").astype(str)
        # Fundir "Não utilizar - X" com "X" e dobrar acento ANTES de agrupar.
        # Sem isso "FISCALIZAÇÃO" e "FISCALIZAÇAO" são duas unidades diferentes:
        # na rodada real eram 8 pares, R$ 8,58 mi partidos em duas linhas, e a
        # contagem de "109 rótulos" não era contagem de centro de custo nenhum.
        d["unidade"] = bruta.map(normalizar_unidade)
        # NATUREZA SÓ DA CONTA. A versão anterior passava também a unidade como
        # segundo texto, e classificar_series usa o seguinte quando o primeiro
        # devolve OUTROS — de modo que o resíduo da natureza era decidido pelo
        # MESMO texto que decide a função. Os dois eixos deixavam de ser
        # independentes exatamente onde a independência importa.
        #
        # O efeito era literal: a linha de natureza "Fiscalização (finalística)"
        # da matriz valia R$ 212.502,80 e era composta por "Serviço de
        # Divulgação Institucional" e "Uniformes" — nenhum dos dois casa a regra
        # de fiscalização; entraram porque o centro de custo se chama SUPFIS e
        # UFAT. Ou seja: a ferramenta fazia, por construção, o mesmo
        # enquadramento que ela se propõe a auditar.
        d["categoria"] = T.classificar_series(coluna(d, "NOME_CONTA"))
        d["funcao"] = T.classificar_funcao_series(d["unidade"], d["categoria"])
        d["rotulo"] = d["unidade"] + " / " + coluna(d, "NOME_CONTA").fillna("")
        return d[COLS], "centro de custo"

    comp = df(staged, "comparativo_despesa")
    if not comp.empty:
        valor_col = ("REALIZADO_EXERCICIO" if "REALIZADO_EXERCICIO" in comp
                     else "TOTAL_EXERCICIO")
        if valor_col in comp:
            d = comp.copy()
            d = so_analiticas(d)
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
    # Qual eixo mede a fiscalização — o bloco 4 decide, e o 4a reusa a MESMA
    # escolha: abrir um denominador diferente do publicado seria pior que não
    # abrir nenhum.
    medida_fiscal = "funcao"
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
        medida_fiscal = "funcao" if usa_funcao else "categoria"
        fiscal = fiscal_func.reindex(verbas.index).fillna(0) if usa_funcao else fiscal_cat
        medida = ("despesa alocada a centros de custo de fiscalização" if usa_funcao
                  else "contas contábeis rotuladas como fiscalização")

        # DUPLA CONTAGEM — o defeito que derrubaria a comparação inteira num
        # contraditório. O numerador vem do eixo NATUREZA (a despesa é diária)
        # e o denominador, quando usa_funcao, vem do eixo FUNÇÃO (a unidade é
        # de fiscalização). A diária paga ao agente fiscal em diligência é as
        # DUAS coisas: ela estava sendo somada dos dois lados da mesma conta.
        # E é justamente a diária que financia a atividade-fim — contá-la como
        # despesa-meio é o oposto do que se quer mostrar.
        # A comparação publicada passa a ser disjunta: verba paga a pessoas
        # FORA dos centros de fiscalização × tudo o que a fiscalização custou.
        # O valor retirado vai declarado, nunca escondido.
        if usa_funcao:
            sobreposta = (base[(base["categoria"] == "VERBAS_PESSOAIS") &
                               (base["funcao"] == "FISCALIZACAO")]
                          .groupby("_ano")["valor"].sum()
                          .reindex(verbas.index).fillna(0))
        else:
            sobreposta = pd.Series(0.0, index=verbas.index)
        verbas_fora = (verbas - sobreposta).clip(lower=0)

        rotulo_verba = ("Verbas pessoais fora da fiscalização" if usa_funcao
                        else "Verbas pessoais (diárias, km, jetons)")
        comp = pd.DataFrame({rotulo_verba: verbas_fora, "Fiscalização": fiscal})
        if usa_funcao:
            comp = comp[comp["Fiscalização"] > 0]      # só exercícios com centro de custo
        maior = comp[comp[rotulo_verba] > comp["Fiscalização"]]
        anos_c = [int(a) for a in comp.index]
        periodo = (f"{min(anos_c)}–{max(anos_c)}" if len(anos_c) > 1
                   else (str(anos_c[0]) if anos_c else "—"))
        zerados = [int(a) for a, v in comp["Fiscalização"].items() if v <= 0]
        total_sobreposto = float(sobreposta.reindex(comp.index).fillna(0).sum())
        achados.append(Finding(
            id="finalistica-verbas-vs-fiscalizacao", frente=FRENTE,
            gravidade="alta" if len(maior) >= max(len(comp) // 2, 1) and not zerados else "media",
            titulo=(f"Em {len(maior)} de {len(comp)} exercícios ({periodo}), o gasto com "
                    f"diárias/km/jetons{' fora dos centros de fiscalização' if usa_funcao else ''} "
                    f"superou o gasto com fiscalização — medido por {medida}"),
            descricao=(
                "Comparação direta entre a verba paga a pessoas para se deslocar e "
                "o custo da atividade que justifica a existência do conselho. "
                "Fiscalizar o exercício profissional é a competência legal "
                "primária (Lei 5.194/1966, art. 34); verba indenizatória é meio, "
                f"não fim. Medida de fiscalização: {medida}."
                + (f" **Sem dupla contagem**: {fmt_brl(total_sobreposto)} de verba "
                   "a pessoas foram pagos DENTRO de centros de custo de "
                   "fiscalização e saíram do numerador — essa diária é custo da "
                   "atividade-fim, e somá-la dos dois lados inflaria a "
                   "comparação." if total_sobreposto > 0 else "")
                + (f" Nos exercícios {zerados} a medida devolveu zero — isso é "
                   "limite de classificação da ferramenta (nenhuma conta ou centro "
                   "de custo reconhecido como fiscalização naquele ano), não gasto "
                   "zero do órgão." if zerados else "")),
            evidencia=comp.reset_index(),
            serie=comp, grafico="bar",
            base_legal="Lei 5.194/1966, arts. 24 e 34 (finalidade dos Creas); "
                       "Lei 4.320/1964, art. 63 (liquidação da despesa).",
            metricas={"anos_verba_supera_fiscalizacao": int(len(maior)),
                      "total_verbas": round(float(comp[rotulo_verba].sum()), 2),
                      "total_fiscalizacao": round(float(comp["Fiscalização"].sum()), 2),
                      "verba_dentro_da_fiscalizacao": round(total_sobreposto, 2),
                      "medida": medida, "periodo": periodo,
                      "exercicios_sem_medida": zerados},
        ))

    # ------------------------------------------- 4a. o que É a "fiscalização"
    #
    # A pergunta do operador: "aumentou o valor com fiscalização porque tentam
    # enquadrar tudo na conta contábil de fiscalização, mesmo quando não
    # deveria — um evento de colégio de inspetores, altamente político, foi
    # pago com dinheiro de fiscalização".
    #
    # Não dá para responder isso com um total. Enquanto a fiscalização for um
    # número só, ninguém — nem o conselho, nem quem contesta — consegue ver o
    # que está dentro dele. Este achado ABRE O DENOMINADOR: cada rótulo que
    # compõe a medida, com o valor que carrega, ordenado por peso. É a linha
    # que a pessoa confere na fonte, uma a uma, sem depender da nossa palavra
    # nem da de um modelo de linguagem.
    achados.extend(_abrir_a_fiscalizacao(base, medida_fiscal))

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
                           .sort_values("valor", ascending=False)
                           .pipe(topo, 100, "valor")),
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


def _abrir_a_fiscalizacao(base: pd.DataFrame, eixo: str) -> list[Finding]:
    """Publica a COMPOSIÇÃO do que a ferramenta chamou de fiscalização.

    Um total não é auditável. "R$ 43 mi em fiscalização" pode conter diligência
    de agente fiscal e pode conter evento de colegiado — e quem lê não tem como
    saber, porque o número não abre. Este achado abre: cada rótulo que entrou na
    medida, com o valor que carrega, ordenado por peso.

    A abertura é deterministica e vale por si. Em cima dela, quando há um modelo
    local no ar, roda uma TRIAGEM que sinaliza rótulos cujo enquadramento merece
    conferência — nos dois sentidos, e sem mexer em número nenhum.
    """
    from .. import llm

    if eixo not in base.columns or "rotulo" not in base.columns:
        return []
    dentro = base[base[eixo] == "FISCALIZACAO"]
    if dentro.empty:
        return []

    total = float(dentro["valor"].sum())
    total_geral = float(base["valor"].sum())
    composicao = (dentro.groupby("rotulo")["valor"].sum()
                  .sort_values(ascending=False))
    tabela = composicao.reset_index()
    tabela.columns = ["rótulo", "valor"]
    tabela["% da fiscalização"] = (100 * tabela["valor"] / total).round(1)

    nome_eixo = ("centro de custo (função)" if eixo == "funcao"
                 else "conta contábil (natureza)")
    achados = [Finding(
        id="finalistica-fiscalizacao-composicao", frente=FRENTE,
        gravidade="info",
        titulo=(f"O que está dentro da “fiscalização”: {len(composicao)} rótulos "
                f"somam {fmt_brl(total)}"),
        descricao=(
            "A medida de fiscalização usada no relatório aberta rótulo a rótulo, "
            f"pelo eixo {nome_eixo}. Serve para duas leituras opostas e ambas "
            "legítimas: conferir se algo que não é fiscalização do exercício "
            "profissional entrou na conta, e conferir se a medida está deixando "
            "atividade real de fora.\n\n"
            "**Este achado não acusa nada.** É o denominador aberto para quem "
            "quiser conferir na fonte — a classificação é NOSSA, por nome de "
            "rótulo, não do órgão."),
        evidencia=tabela,
        base_legal="Lei 4.320/1964, art. 63; NBC TSP 11 (segregação por função).",
        como_verificar=("Cada rótulo pode ser conferido no portal da "
                        "transparência do conselho pelo nome do centro de custo "
                        "ou da conta contábil."),
        cobertura={"base": total, "universo": total_geral,
                   "o_que": "despesa reconhecida como fiscalização pela "
                            "classificação do projeto"},
    )]

    # --------------------------------------------------- triagem (opcional)
    fora_todos = (base[base[eixo] != "FISCALIZACAO"].groupby("rotulo")["valor"].sum()
                  .sort_values(ascending=False))
    TETO = 300
    fora = fora_todos.head(TETO)
    classe = base.groupby("rotulo")[eixo].first()
    dentro = set(composicao.index)
    alvo = list(composicao.head(TETO).index) + [r for r in fora.index
                                               if r not in dentro]
    # "OUTROS" não é código de função — um rótulo ausente do mapa entraria com
    # rótulo inválido na tabela publicada.
    rubricas = {r: str(classe.get(r, "NAO_IDENTIFICADA")) for r in alvo}

    # Corte silencioso lê-se como "varremos tudo". Ele vai dito.
    cortados = max(len(composicao) - TETO, 0) + max(len(fora_todos) - TETO, 0)
    if cortados:
        log.info("triagem de enquadramento: %d rótulo(s) de menor valor ficaram "
                 "fora da varredura (teto de %d por sentido)", cortados, TETO)

    divergencias = llm.auditar_enquadramento(rubricas)
    if not divergencias:
        return achados

    # O valor de um rótulo DENTRO da medida é o que ele pesa na medida, não a
    # soma dele em toda a base: a tabela fala em "dentro da fiscalização" e o
    # número tem de ser o mesmo que a composição publica.
    fora_valor = fora_todos.to_dict()
    for d in divergencias:
        r = d["rubrica"]
        d["valor"] = round(float(composicao.get(r, fora_valor.get(r, 0.0))), 2)
    div = pd.DataFrame(divergencias).sort_values("valor", ascending=False)
    dentro_sinalizado = div[div["funcao_da_regra"] == "FISCALIZACAO"]
    valor_dentro = float(dentro_sinalizado["valor"].sum())

    # Quanto do que foi sinalizado DENTRO é folha e verba a pessoa. Na primeira
    # rodada real isso era 85,5% do valor, e a tabela não dizia: o modelo estava
    # respondendo que "Salários" não descreve fiscalizar, o que é verdade sobre
    # a palavra e irrelevante para uma medida que classifica pela UNIDADE.
    pessoal = base[base["categoria"].isin(("REMUNERACAO", "VERBAS_PESSOAIS"))]
    rot_pessoal = set(pessoal["rotulo"].unique())
    valor_dentro_pessoal = float(
        dentro_sinalizado.loc[dentro_sinalizado["rubrica"].isin(rot_pessoal),
                              "valor"].sum())
    pct_pessoal = (100 * valor_dentro_pessoal / valor_dentro) if valor_dentro else 0.0

    achados.append(Finding(
        id="finalistica-enquadramento-conferir", frente=FRENTE,
        gravidade="info",
        titulo=(f"{len(div)} rótulos sinalizados para conferência de "
                f"enquadramento — {len(dentro_sinalizado)} deles dentro da "
                f"fiscalização ({fmt_brl(valor_dentro)})"),
        descricao=(
            "Triagem feita por um modelo de linguagem LOCAL sobre os rótulos "
            "que compõem a despesa, nos DOIS sentidos: unidades contadas como "
            "de fiscalização que o modelo não lê assim, e unidades fora da "
            "medida que ele leria dentro.\n\n"
            "**O que este achado NÃO é.** Não é conclusão, não é imputação e "
            "não mexe em número algum do relatório: nenhuma soma, razão ou "
            "denominador foi alterado por ele. Um modelo pequeno erra, e a "
            "lista só é útil como fila de conferência.\n\n"
            "**O que já foi descartado antes de chegar aqui.** Resposta cujo "
            "trecho não seleciona um pedaço do rótulo (repetir a rubrica "
            "inteira não aponta nada) e resposta cujo motivo repete a "
            "definição dada no enunciado em vez de falar da rubrica.\n\n"
            + (f"**Leia com esta ressalva.** {pct_pessoal:.0f}% do valor "
               "sinalizado dentro da medida é folha e verba a pessoas de "
               "unidades de fiscalização. Pelo eixo que o relatório publica — a "
               "FUNÇÃO, lida do centro de custo — salário e diária de unidade "
               "de fiscalização SÃO custo da atividade-fim: agente fiscal em "
               "campo custa salário e diária. Sinalização nessas linhas é, "
               "quase sempre, o modelo respondendo sobre a palavra da conta e "
               "não sobre a unidade.\n\n" if pct_pessoal >= 25 else "")
            + "**Como usar.** Leia o rótulo, leia o trecho e decida na fonte. "
            "O nome de um rótulo é evidência fraca de finalidade — a forte é o "
            "empenho e o processo que o originou."),
        evidencia=div,
        como_verificar=("Puxar, para os rótulos sinalizados, o empenho e o "
                        "processo administrativo correspondentes: é o documento "
                        "que diz a finalidade, não o nome da conta."),
        metricas={"rubricas_auditadas": len(rubricas),
                  "rotulos_fora_da_varredura": int(cortados),
                  "valor_sinalizado_dentro_que_e_pessoal": round(valor_dentro_pessoal, 2),
                  "sinalizadas": int(len(div)),
                  "sinalizadas_dentro_da_fiscalizacao": int(len(dentro_sinalizado)),
                  "valor_sinalizado_dentro": round(valor_dentro, 2)},
    ))
    return achados
