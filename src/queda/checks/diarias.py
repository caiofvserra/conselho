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
from ..findings import Finding
from ..utils import fmt_brl
from .common import (classificar_beneficiario, df, pagamentos_indenizacao,
                     pagamentos_viagem_pj)

FRENTE = "Diárias e verbas a pessoas"

BASE_LEGAL = ("Lei 4.320/1964, art. 63 (liquidação: comprovação da despesa); "
              "Resoluções do Confea que fixam diárias e jetons de conselheiros; "
              "Lei 12.527/2011 (LAI), art. 8º, §1º, III — divulgação "
              "individualizada de repasses.")


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    ind = pagamentos_indenizacao(staged)
    if ind.empty:
        achados.append(Finding(
            id="diarias-sem-dados", frente=FRENTE, gravidade="info",
            titulo="Nenhum pagamento de diária/verba identificado por conta contábil",
            descricao="Nenhuma conta contábil dos pagamentos foi classificada como "
                      "verba pessoal. Verifique a coleta ou o plano de contas.",
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
                "IPCA, isolando crescimento verdadeiro de correção monetária."),
            evidencia=serie.reset_index(),
            serie=serie, grafico="bar_stacked",
            base_legal=BASE_LEGAL,
            metricas={"total_periodo": round(total, 2),
                      "cagr_nominal": None if cagr != cagr else round(cagr, 4),
                      "cagr_real": None if cagr_real != cagr_real else round(cagr_real, 4),
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
