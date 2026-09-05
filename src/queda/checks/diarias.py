"""Frente — Diárias, quilometragem e verbas indenizatórias a pessoas.

Fonte: `Pagamentos`, classificados pela CONTA CONTÁBIL (nunca pelo histórico
nem pelo nome do favorecido — foi o que gerou os falsos positivos da primeira
rodada, quando empresas de frota e telecom entraram como "diárias").

Verba paga a pessoa física fica aqui; viagem comprada de pessoa jurídica
(agência, cia aérea, locadora) é custeio administrativo e sai num achado
próprio, para não inflar o total.
"""
from __future__ import annotations

import pandas as pd

from .. import analytics as A
from .. import config
from .. import taxonomia as T
from ..findings import Finding
from ..utils import fmt_brl
from .common import (classificar_beneficiario, df, pagamentos_indenizacao,
                     pagamentos_viagem_pj)

FRENTE = "Diárias e verbas a pessoas"

BASE_LEGAL = ("Lei 4.320/1964, art. 63 (liquidação: comprovação da despesa); "
              "Resoluções do Confea que fixam diárias e jetons de conselheiros; "
              "Lei 12.527/2011 (LAI), art. 8º, §1º, III — divulgação "
              "individualizada de repasses.")


def _milhar(n: int) -> str:
    """12345 -> "12.345" (separador pt-BR), sem tocar no resto do texto."""
    return f"{int(n):,}".replace(",", ".")


def _verbas_por_categoria(staged) -> list[Finding]:
    """Quem recebe a verba, lido na CONTA — e não no nome do favorecido.

    Esta é a resposta ao problema que motivou a ferramenta: o portal agrega o
    favorecido sob rótulos como "DIVERSOS FUNCIONÁRIOS", e todas as verbas
    ficam sem dono identificável. Só que a conta contábil continua dizendo a
    QUEM a verba se destina — "Diarias com Conselheiros", "Locomoção de
    Inspetores" —, e isso basta para separar a estrutura política do quadro
    contratado, que é a comparação que interessa.

    Note que este recorte NÃO exige favorecido pessoa física: é justamente o
    pagamento agregado, invisível para as outras análises, que aparece aqui.
    """
    achados: list[Finding] = []
    todos = pagamentos_indenizacao(staged, apenas_pessoa_fisica=False)
    if todos.empty or "_conta_match" not in todos:
        return achados
    d = todos.copy()
    # verba é despesa com pessoa: viagem comprada de empresa é custeio e sai
    if "PJ" in d.columns:
        d = d[~d["PJ"].astype(bool)]
    if d.empty:
        return achados
    mapa = {c: T.categoria_beneficiario(c) for c in d["_conta_match"].unique()}
    d["categoria_conta"] = d["_conta_match"].map(mapa)
    d["rotulo"] = d["categoria_conta"].map(T.CATEGORIAS_BENEFICIARIO)

    resumo = (d.groupby(["categoria_conta", "rotulo"])
              .agg(total=("VALOR", "sum"), pagamentos=("VALOR", "size"))
              .reset_index().sort_values("total", ascending=False))
    resumo["ticket_medio"] = (resumo["total"] / resumo["pagamentos"]).round(2)
    politico = float(d.loc[d["categoria_conta"].isin(T.POLITICOS), "VALOR"].sum())
    quadro = float(d.loc[d["categoria_conta"].isin(T.QUADRO), "VALOR"].sum())
    n_pol = int((d["categoria_conta"].isin(T.POLITICOS)).sum())
    n_quad = int((d["categoria_conta"].isin(T.QUADRO)).sum())
    if politico <= 0 and quadro <= 0:
        return achados
    razao = politico / quadro if quadro else float("inf")

    serie = A.serie_anual(d, "VALOR", by="rotulo")
    achados.append(Finding(
        id="diarias-politico-vs-quadro", frente=FRENTE,
        gravidade="alta" if razao > 1 else "media",
        titulo=(f"A estrutura política recebeu {fmt_brl(politico)} em verbas "
                f"contra {fmt_brl(quadro)} do quadro contratado"
                + (f" — {razao:.1f}× mais" if razao != float("inf") else "")),
        descricao=(
            "A conta contábil declara a QUEM a verba se destina, mesmo quando o "
            "portal esconde o favorecido sob um rótulo agregado. Separando por "
            "essa declaração: de um lado conselheiros, inspetores e direção — "
            "eleitos ou indicados, cujos valores são fixados por resolução do "
            "Confea e pagos pelo próprio conselho; do outro, funcionários e "
            "colaboradores, que são contratados.\n\n"
            f"São {_milhar(n_pol)} pagamentos à estrutura política contra "
            f"{_milhar(n_quad)} ao quadro. "
            "Nenhum deles aparece no contracheque publicado: a "
            "remuneração divulgada no quadro de pessoal traz só o salário, e "
            "verba indenizatória não é salário — não entra em teto, não compõe "
            "base de contribuição e não aparece na transparência da folha. É "
            "exatamente por isso que comparar salários entre categorias não "
            "revela nada, e comparar verbas revela."),
        evidencia=resumo,
        serie=serie, grafico="bar_stacked",
        tabela_anual=A.ranking_anual(d, "rotulo", "VALOR", top=8),
        base_legal=BASE_LEGAL,
        como_verificar=(
            "Pedir via LAI, por exercício e por CPF, a relação nominal de "
            "beneficiários de cada uma dessas contas, com evento, destino, "
            "período e valor unitário — é o que o portal deixa de publicar ao "
            "agregar o favorecido."),
        metricas={"estrutura_politica": round(politico, 2),
                  "quadro_contratado": round(quadro, 2),
                  "razao": None if razao == float("inf") else round(razao, 2),
                  "pagamentos_politica": n_pol, "pagamentos_quadro": n_quad,
                  "por_categoria": {r["rotulo"]: round(float(r["total"]), 2)
                                    for _, r in resumo.iterrows()}},
    ))

    # ---- o que o agregado esconde: contagem de pagamentos por categoria
    oculto = d[~d.get("PF", pd.Series(False, index=d.index)).astype(bool)]
    if not oculto.empty:
        por_cat = (oculto.groupby("rotulo")
                   .agg(total=("VALOR", "sum"), pagamentos=("VALOR", "size"))
                   .reset_index().sort_values("total", ascending=False))
        tot_oculto = float(oculto["VALOR"].sum())
        achados.append(Finding(
            id="diarias-sem-beneficiario", frente=FRENTE, gravidade="alta",
            titulo=(f"{fmt_brl(tot_oculto)} em verbas pagas sem beneficiário "
                    f"identificável ({_milhar(len(oculto))} pagamentos)"),
            descricao=(
                "Verbas cujo favorecido veio agregado — sem CPF e sem nome de "
                "pessoa. A conta diz a categoria, mas não a pessoa: é "
                "impossível saber quantas diárias uma mesma pessoa recebeu, se "
                "houve acumulação no mesmo dia, ou se o total anual de alguém "
                "ultrapassa o razoável. A LAI exige divulgação individualizada "
                "de repasses; agregar o beneficiário de verba indenizatória "
                "esvazia o controle justamente onde ele mais importa."),
            evidencia=por_cat,
            serie=A.serie_anual(oculto, "VALOR", by="rotulo"),
            grafico="bar_stacked",
            tabela_anual=A.ranking_anual(oculto, "rotulo", "VALOR", top=8),
            base_legal="Lei 12.527/2011, art. 8º, §1º, II e III; Decreto "
                       "7.724/2012, art. 7º, §3º, VI (divulgação de repasses "
                       "com identificação do beneficiário).",
            metricas={"total": round(tot_oculto, 2),
                      "pagamentos": int(len(oculto))},
        ))
    return achados


def run(staged) -> list[Finding]:
    achados: list[Finding] = _verbas_por_categoria(staged)
    ind = pagamentos_indenizacao(staged)
    if ind.empty:
        # Contradição encontrada na rodada real: este achado dizia "nenhum
        # pagamento de verba identificado" no mesmo relatório em que
        # `diarias-politico-vs-quadro` publicava R$ 178,6 milhões. Os dois
        # medem coisas diferentes e a mensagem não dizia isso.
        #
        # `pagamentos_indenizacao` devolve verba **de pessoa física
        # identificável**; `_verbas_por_categoria` lê a categoria do
        # beneficiário na CONTA, e por isso enxerga a verba mesmo quando o
        # favorecido é um rótulo agregado. Quando a primeira volta vazia e a
        # segunda não, o que aconteceu não é ausência de verba — é ausência de
        # NOME, que é um achado de transparência e não uma falha de coleta.
        todas = pagamentos_indenizacao(staged, apenas_pessoa_fisica=False)
        tem_verba = bool(achados) or not todas.empty
        achados.append(Finding(
            id="diarias-sem-dados", frente=FRENTE, gravidade="info",
            titulo=("Nenhuma verba pôde ser atribuída a uma pessoa física "
                    "identificada" if tem_verba else
                    "Nenhum pagamento de diária/verba identificado por conta "
                    "contábil"),
            descricao=(
                "As análises por pessoa desta frente — teto por diária, volume "
                "anual, acumulação no mesmo dia, ranking de beneficiários — "
                "exigem saber **quem** recebeu, e nenhum pagamento de verba "
                "traz favorecido pessoa física identificável nesta coleta.\n\n"
                "Isso não significa que não houve verba: os achados desta "
                "mesma frente que leem a categoria do beneficiário na conta "
                "contábil continuam medindo o gasto. O que falta é o nome. "
                "Enquanto ele faltar, o valor total é auditável e a conduta "
                "individual não é."
                if tem_verba else
                "Nenhuma conta contábil dos pagamentos foi classificada como "
                "verba pessoal. Verifique a coleta ou o plano de contas — este "
                "caso é falha de dado, não achado sobre o órgão."),
            base_legal=("Lei 12.527/2011, art. 8º, §1º, III; Decreto "
                        "7.724/2012, art. 7º, §3º, VI." if tem_verba else ""),
            como_verificar=("Pedir a relação nominal de beneficiários de "
                            "diárias, jetons e auxílio-locomoção por exercício, "
                            "com CPF parcialmente mascarado."
                            if tem_verba else ""),
        ))
        return achados

    ind = ind.copy()
    if "_ano" not in ind and "DATA" in ind:
        ind["_ano"] = pd.to_datetime(ind["DATA"], errors="coerce").dt.year
    ind["categoria"] = classificar_beneficiario(
        ind.get("FAVORECIDO_NORM", pd.Series(dtype=str)), staged)
    total = float(ind["VALOR"].sum())

    # ------------------------------------------------ 1. série anual
    serie = A.serie_anual(ind, "VALOR", by="categoria")
    if not serie.empty:
        tot_ano = serie.sum(axis=1)
        cagr = A.cagr(tot_ano)
        real = A.variacao_real(tot_ano)
        cagr_real = A.cagr(real)
        nominais = A.anos_sem_deflator(tot_ano.index)
        achados.append(Finding(
            id="diarias-serie-anual", frente=FRENTE,
            gravidade="alta" if (cagr == cagr and cagr > 0.10) else "info",
            titulo=(f"Verbas a pessoas cresceram {cagr:.1%} ao ano "
                    f"({cagr_real:+.1%} já descontada a inflação)"
                    if cagr == cagr else
                    "Série anual de verbas pagas a pessoas"),
            descricao=(
                "Total pago por exercício em contas de diária, quilometragem, "
                "jeton e ajuda de custo, com o favorecido classificado por "
                "cruzamento de nome com o rol de mandatários e o quadro de "
                "pessoal. Valores nominais no gráfico; o CAGR real desconta o "
                "IPCA pelo índice MÉDIO do ano (despesa é fluxo distribuído ao "
                "longo do exercício — deflacionar pelo acumulado dez/dez "
                "superestima a inflação sofrida). "
                + (f"Sem IPCA publicado para {', '.join(str(a) for a in nominais)}"
                   ", esse(s) exercício(s) ficam fora do cálculo real."
                   if nominais else "")),
            evidencia=serie.reset_index(),
            serie=serie, grafico="bar_stacked",
            base_legal=BASE_LEGAL,
            metricas={"total_periodo": round(total, 2),
                      "cagr_nominal": None if cagr != cagr else round(cagr, 4),
                      "cagr_real": None if cagr_real != cagr_real else round(cagr_real, 4),
                      "anos_sem_deflator": nominais,
                      "por_ano": {int(a): round(float(v), 2) for a, v in tot_ano.items()}},
        ))

    # ------------------------------------------------ 2. ranking ANUAL
    rank = A.ranking_anual(ind, "FAVORECIDO_NORM", "VALOR", top=10)
    if not rank.empty:
        achados.append(Finding(
            id="diarias-top-beneficiarios-ano", frente=FRENTE, gravidade="info",
            titulo="Maiores beneficiários de verbas, ano a ano",
            descricao="Top 10 de cada exercício — permite ver quem entra e sai do "
                      "topo ao longo do tempo, e não apenas o acumulado.",
            evidencia=rank, tabela_anual=rank,
        ))
        recorrentes = (rank.groupby("FAVORECIDO_NORM")
                       .agg(anos_no_top=("_ano", "nunique"),
                            total=("total", "sum")).reset_index()
                       .sort_values(["anos_no_top", "total"], ascending=False))
        persistentes = recorrentes[recorrentes["anos_no_top"] >= 5]
        if not persistentes.empty:
            achados.append(Finding(
                id="diarias-beneficiarios-persistentes", frente=FRENTE,
                gravidade="media",
                titulo=f"{len(persistentes)} pessoa(s) aparecem no top-10 de verbas "
                       "em 5 ou mais exercícios",
                descricao="Presença contínua no topo sugere verba estrutural — "
                          "complemento de renda recorrente, não ressarcimento "
                          "eventual de deslocamento.",
                evidencia=persistentes,
            ))

    # ------------------------------------------------ 3. volume anual por pessoa
    anual = (ind.groupby(["FAVORECIDO_NORM", "categoria", "_ano"])
             .agg(total=("VALOR", "sum"), pagamentos=("VALOR", "size")).reset_index())
    excesso = anual[anual["pagamentos"] > config.MAX_DIARIAS_ANO]
    if not excesso.empty:
        serie_exc = (excesso.groupby("_ano")["FAVORECIDO_NORM"].nunique()
                     .to_frame("pessoas acima do limite"))
        achados.append(Finding(
            id="diarias-volume-anual", frente=FRENTE, gravidade="alta",
            titulo=f"{excesso['FAVORECIDO_NORM'].nunique()} pessoa(s) com mais de "
                   f"{config.MAX_DIARIAS_ANO} pagamentos de verba num mesmo ano",
            descricao=("Um ano tem ~250 dias úteis. Acima de "
                       f"{config.MAX_DIARIAS_ANO} pagamentos anuais o padrão deixa "
                       "de ser ressarcimento eventual e passa a ser rotina — "
                       "indício de remuneração disfarçada de verba indenizatória, "
                       "que escapa de teto, imposto e transparência da folha."),
            evidencia=excesso.sort_values("pagamentos", ascending=False),
            serie=serie_exc, grafico="bar", unidade="un",
            tabela_anual=excesso.sort_values(["_ano", "pagamentos"],
                                             ascending=[True, False]),
            base_legal=BASE_LEGAL,
            como_verificar="Pedir via LAI as prestações de contas de viagem "
                           "(evento, destino, período) das pessoas listadas, nos "
                           "anos indicados, e confrontar com as atas de presença.",
        ))

    # ------------------------------------------------ 3b. cadência: mês e dia
    if "DATA" in ind:
        d = ind.dropna(subset=["DATA"]).copy()
        d["_mes"] = pd.to_datetime(d["DATA"], errors="coerce").dt.to_period("M")
        d["_dia"] = pd.to_datetime(d["DATA"], errors="coerce").dt.date

        por_mes = (d.groupby(["FAVORECIDO_NORM", "_mes"])
                   .agg(pagamentos=("VALOR", "size"), total=("VALOR", "sum"))
                   .reset_index())
        acima_mes = por_mes[por_mes["pagamentos"] > config.MAX_JETONS_MES]
        if not acima_mes.empty:
            acima_mes = acima_mes.copy()
            acima_mes["_ano"] = acima_mes["_mes"].dt.year
            acima_mes["_mes"] = acima_mes["_mes"].astype(str)
            s = (acima_mes.groupby("_ano")["FAVORECIDO_NORM"].nunique()
                 .to_frame("pessoas acima do limite mensal"))
            achados.append(Finding(
                id="diarias-limite-mensal", frente=FRENTE, gravidade="alta",
                titulo=(f"{acima_mes['FAVORECIDO_NORM'].nunique()} pessoa(s) "
                        f"receberam mais de {config.MAX_JETONS_MES} verbas num "
                        "único mês"),
                descricao=(
                    "O jeton remunera a presença em sessão e a resolução do Confea "
                    f"limita as sessões remuneráveis por mês (parâmetro em uso: "
                    f"{config.MAX_JETONS_MES}). Passar disso significa uma de duas "
                    "coisas: houve mais sessões do que o normativo permite "
                    "remunerar, ou a verba está pagando outra coisa com o nome de "
                    "jeton. Nos dois casos o pagamento precisa de justificativa "
                    "individual. **Confirme o limite na resolução vigente em cada "
                    "exercício** antes de tratar como irregularidade."),
                evidencia=acima_mes.sort_values("pagamentos", ascending=False).head(300),
                serie=s, grafico="bar", unidade="un",
                tabela_anual=acima_mes.sort_values(["_ano", "pagamentos"],
                                                   ascending=[True, False]).head(300),
                base_legal=BASE_LEGAL,
                como_verificar="Confrontar com as atas das sessões do mês: cada "
                               "jeton deve corresponder a uma presença registrada.",
            ))

        por_dia = (d.groupby(["FAVORECIDO_NORM", "_dia"])
                   .agg(pagamentos=("VALOR", "size"), total=("VALOR", "sum"),
                        contas=("CONTA_EMPENHO", "nunique")
                        if "CONTA_EMPENHO" in d else ("VALOR", "size"))
                   .reset_index())
        acumulo = por_dia[por_dia["pagamentos"] > config.MAX_JETONS_DIA]
        if not acumulo.empty:
            acumulo = acumulo.copy()
            acumulo["_ano"] = pd.to_datetime(acumulo["_dia"]).dt.year
            s = acumulo.groupby("_ano")["total"].sum().to_frame(
                "valor acumulado no mesmo dia")
            achados.append(Finding(
                id="diarias-acumulacao-diaria", frente=FRENTE, gravidade="alta",
                titulo=(f"{len(acumulo)} caso(s) de mais de uma verba paga à mesma "
                        "pessoa no mesmo dia"),
                descricao=(
                    "Diária, jeton, quilometragem e verba de representação "
                    "indenizam fatos distintos, mas acumulá-los no mesmo dia "
                    "costuma ser vedado: quem recebe diária de viagem não pode "
                    "receber, pelo mesmo dia, jeton de sessão presencial — as duas "
                    "coisas não acontecem ao mesmo tempo. A coluna `contas` mostra "
                    "quantas rubricas diferentes foram acionadas no dia; mais de "
                    "uma é o caso a apurar primeiro."),
                evidencia=acumulo.sort_values("total", ascending=False).head(300),
                serie=s, grafico="bar",
                tabela_anual=acumulo.sort_values(["_ano", "total"],
                                                 ascending=[True, False]).head(300),
                base_legal=BASE_LEGAL,
                como_verificar="Pedir, para as datas listadas, o relatório de "
                               "viagem e a ata da sessão: se a pessoa estava em "
                               "deslocamento, não presidiu sessão presencial.",
                metricas={"casos": int(len(acumulo)),
                          "valor": round(float(acumulo["total"].sum()), 2)},
            ))

    # ------------------------------------------------ 4. concentração anual
    if not rank.empty:
        conc = []
        for ano, g in ind.groupby("_ano"):
            tot = float(g["VALOR"].sum())
            if tot <= 0:
                continue
            top10 = g.groupby("FAVORECIDO_NORM")["VALOR"].sum().nlargest(10).sum()
            conc.append({"_ano": int(ano), "share_top10": round(100 * top10 / tot, 1),
                         "beneficiarios": int(g["FAVORECIDO_NORM"].nunique())})
        dfc = pd.DataFrame(conc)
        if not dfc.empty:
            achados.append(Finding(
                id="diarias-concentracao", frente=FRENTE,
                gravidade="media" if dfc["share_top10"].mean() > 35 else "info",
                titulo=f"10 pessoas concentram em média "
                       f"{dfc['share_top10'].mean():.0f}% das verbas por ano",
                descricao="Participação do top-10 no total de cada exercício e "
                          "número de beneficiários distintos — mede se a verba "
                          "atende muitos ou poucos.",
                evidencia=dfc,
                serie=dfc.set_index("_ano")[["share_top10"]]
                      .rename(columns={"share_top10": "% do total no top-10"}),
                grafico="line", unidade="%",
            ))

    # ------------------------------------------------ 5. viagem via PJ (custeio)
    pj = pagamentos_viagem_pj(staged)
    if not pj.empty:
        serie_pj = A.serie_anual(pj, "VALOR")
        achados.append(Finding(
            id="diarias-viagem-pj", frente=FRENTE, gravidade="info",
            titulo=f"Gasto com viagens contratado de empresas: "
                   f"{fmt_brl(float(pj['VALOR'].sum()))} no período",
            descricao=("Passagens, hospedagem e locação pagos a pessoa jurídica "
                       "(agências, cias aéreas, locadoras). É custeio "
                       "administrativo, não verba indenizatória — está separado "
                       "para não inflar o total de 'diárias'. Somado às verbas a "
                       "pessoas, mostra o custo real de deslocamento do conselho."),
            evidencia=A.ranking_anual(pj, "FAVORECIDO_NORM", "VALOR", top=10),
            serie=serie_pj.rename(columns={"VALOR": "viagens via PJ"}),
            grafico="bar",
            tabela_anual=A.ranking_anual(pj, "FAVORECIDO_NORM", "VALOR", top=10),
        ))

    # ------------------------------------------------ 6. transparência
    endpoint = df(staged, "indenizacoes_endpoint")
    if endpoint.empty and total > 0:
        achados.append(Finding(
            id="diarias-endpoint-vazio", frente=FRENTE, gravidade="alta",
            titulo="Os endpoints dedicados de diárias/indenizações não devolvem "
                   "dados, embora os pagamentos registrem o gasto",
            descricao=(
                f"Os pagamentos somam {fmt_brl(total)} em contas de verba pessoal, "
                "mas as APIs específicas (Dados Abertos `indenizacoes` e as "
                "sondagens no portal Implanta) retornam vazio. Sem elas, evento, "
                "destino, período de deslocamento e valor unitário ficam ocultos "
                "— é impossível verificar se a diária corresponde a viagem real."),
            metricas={"total_verbas": round(total, 2)},
            base_legal="Lei 12.527/2011, arts. 8º e 5º; Decreto 8.777/2016 "
                       "(Política de Dados Abertos).",
            como_verificar="Pedido de acesso à informação exigindo a base de "
                           "indenizações por exercício, com favorecido, evento, "
                           "trecho, datas, quantidade e valor unitário.",
        ))
    return achados
