"""Frente — Casos concentrados: onde o dinheiro grande está.

Enquanto as outras frentes varrem padrões, esta isola os poucos vetores que
respondem por boa parte do gasto e merecem apuração individual: entidades
criadas em torno do próprio conselho, o maior projeto de investimento em
curso, e os contratos de valor excepcional.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from ..findings import Finding
from ..utils import fmt_brl, norm_nome
from .common import df, fornecedores_mercado

FRENTE = "Casos concentrados"

# Entidade cujo nome carrega o do próprio conselho: associação de servidores,
# de procuradores, de conselheiros. Contratar a própria "casa" é o vetor
# clássico de autocontratação em conselhos profissionais.
RX_ENTIDADE_CASA = re.compile(
    r"\bCREA\b|\bCONFEA\b|CONSELHO REGIONAL DE ENGENHARIA", re.IGNORECASE)
RX_ASSOCIATIVA = re.compile(
    r"ASSOCIACAO|SINDICATO|INSTITUTO|FUNDACAO|COOPERATIVA|CLUBE|CAIXA",
    re.IGNORECASE)

RX_OBRA = re.compile(
    r"\bobra\b|\bconstrucao\b|\bnova sede\b|\bsede\b|\breforma\b|"
    r"\bedificacao\b|\bempreitada\b|gerenciamento de obra|projeto executivo",
    re.IGNORECASE)


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    pg = df(staged, "pagamentos")
    ctr = df(staged, "contratos")

    # ------------------------------------------------ 1. entidade "da casa"
    if not pg.empty and "FAVORECIDO_NORM" in pg:
        nomes = pg["FAVORECIDO_NORM"].fillna("")
        casa = pg[nomes.str.contains(RX_ENTIDADE_CASA, regex=True, na=False)
                  & nomes.str.contains(RX_ASSOCIATIVA, regex=True, na=False)]
        if not casa.empty:
            total = float(casa["VALOR"].sum())
            por_ent = (casa.groupby("FAVORECIDO_NORM")["VALOR"]
                       .agg(total="sum", pagamentos="size").reset_index()
                       .sort_values("total", ascending=False))
            achados.append(Finding(
                id="casos-entidade-da-casa", frente=FRENTE, gravidade="alta",
                titulo=f"{fmt_brl(total)} pagos a entidades associativas que levam "
                       "o nome do próprio conselho",
                descricao=(
                    "Associações, institutos e sindicatos cujo nome contém o do "
                    "próprio órgão — tipicamente constituídos por seus servidores, "
                    "procuradores ou conselheiros. Contratar a entidade formada "
                    "pelo próprio quadro concentra o conflito de interesse: quem "
                    "contrata e quem recebe pertencem ao mesmo grupo, e a "
                    "contratação costuma correr por inexigibilidade."),
                evidencia=por_ent,
                serie=A.serie_anual(casa, "VALOR", by="FAVORECIDO_NORM"),
                grafico="bar_stacked",
                tabela_anual=A.ranking_anual(casa, "FAVORECIDO_NORM", "VALOR", top=6),
                base_legal=("Lei 14.133/2021, art. 9º, III (vedação ao agente que "
                            "atua em favor de interesse próprio) e art. 14; "
                            "Decreto-Lei 200/1967, art. 10; princípio da "
                            "impessoalidade (CF, art. 37)."),
                como_verificar=(
                    "Obter o estatuto e o quadro de associados da entidade e "
                    "confrontar com o quadro de pessoal e o rol de mandatários do "
                    "conselho; obter o processo de contratação e a justificativa "
                    "de inexigibilidade."),
                metricas={"total": round(total, 2),
                          "entidades": int(por_ent.shape[0])},
            ))

    # ------------------------------------------------ 2. obra / nova sede
    frames = []
    if not ctr.empty:
        obj = ctr.get("OBJETO", pd.Series("", index=ctr.index)).fillna("").astype(str)
        nome_c = ctr.get("CONTRATADA_NOME",
                         ctr.get("CONTRATADA", pd.Series("", index=ctr.index)))
        m = obj.str.contains(RX_OBRA, regex=True, na=False)
        if m.any():
            frames.append(ctr[m])
    obras_pg = pd.DataFrame()
    if not pg.empty:
        campos = (pg.get("CONTA_EMPENHO", pd.Series("", index=pg.index)).fillna("") +
                  " " + pg.get("HISTORICO", pd.Series("", index=pg.index)).fillna(""))
        obras_pg = pg[campos.str.contains(RX_OBRA, regex=True, na=False)]
    if not obras_pg.empty:
        total_obra = float(obras_pg["VALOR"].sum())
        serie = A.serie_anual(obras_pg, "VALOR")
        achados.append(Finding(
            id="casos-obras", frente=FRENTE,
            gravidade="media" if total_obra > 20e6 else "info",
            titulo=f"{fmt_brl(total_obra)} em obras, construção e sede no período",
            descricao=(
                "Pagamentos e contas ligados a obra, construção, reforma e sede. "
                "Projeto de investimento imobiliário é o maior risco isolado de um "
                "conselho: valor alto, medição técnica difícil de auditar e "
                "aditivos frequentes. Acompanhe a curva anual — aceleração súbita "
                "costuma preceder aditivos."),
            evidencia=A.ranking_anual(obras_pg, "FAVORECIDO_NORM", "VALOR", top=10),
            serie=serie.rename(columns={"VALOR": "obras e sede"}), grafico="bar",
            tabela_anual=A.ranking_anual(obras_pg, "FAVORECIDO_NORM", "VALOR", top=10),
            como_verificar="Solicitar cronograma físico-financeiro, boletins de "
                           "medição e os termos aditivos da obra.",
        ))
    if frames:
        ctr_obra = pd.concat(frames).drop_duplicates()
        cols = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "VALOR", "OBJETO",
                            "DATA_INICIO_VIGENCIA", "DATA_FINAL_VIGENCIA")
                if c in ctr_obra]
        achados.append(Finding(
            id="casos-contratos-obra", frente=FRENTE, gravidade="info",
            titulo=f"{len(ctr_obra)} contrato(s) com objeto de obra ou sede",
            descricao="Contratos cujo objeto menciona obra, construção, reforma ou "
                      "sede, ordenados por valor.",
            evidencia=ctr_obra[cols].sort_values("VALOR", ascending=False).head(50),
        ))

    # ------------------------------------------------ 3. contratos excepcionais
    if not ctr.empty and "VALOR" in ctr:
        v = pd.to_numeric(ctr["VALOR"], errors="coerce").fillna(0)
        if (v > 0).sum() >= 10:
            z = A.zscore_robusto(v[v > 0])
            outliers = ctr.loc[z[z > 8].index]
            if not outliers.empty:
                cols = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                    "OBJETO", "DATA_INICIO_VIGENCIA",
                                    "DATA_FINAL_VIGENCIA") if c in outliers]
                achados.append(Finding(
                    id="casos-contratos-excepcionais", frente=FRENTE,
                    gravidade="alta",
                    titulo=f"{len(outliers)} contrato(s) com valor muito acima do "
                           "padrão da carteira",
                    descricao=(
                        "Contratos cujo valor está a mais de 8 desvios (medida "
                        "robusta por mediana/MAD) do típico da carteira. Ou são os "
                        "grandes projetos do órgão — que merecem acompanhamento "
                        "individual — ou erro de digitação no portal, que também é "
                        "achado de transparência. Verifique caso a caso."),
                    evidencia=outliers[cols].sort_values("VALOR", ascending=False),
                    como_verificar="Comparar o valor publicado no portal com o "
                                   "extrato do contrato no PNCP e com o total "
                                   "efetivamente pago à contratada.",
                ))

    # ------------------------------------------------ 4. pessoa física fornecedora
    if not pg.empty and "TIPO_PESSOA" in pg:
        pf_forn = pg[(pg["TIPO_PESSOA"] == "PF")]
        if not pf_forn.empty and "CONTA_EMPENHO" in pf_forn:
            servicos = pf_forn[pf_forn["CONTA_EMPENHO"].fillna("").str.contains(
                r"servi[çc]o|consultor|assessor|terceir", case=False, regex=True,
                na=False)]
            if not servicos.empty:
                por_pessoa = (servicos.groupby("FAVORECIDO_NORM")["VALOR"]
                              .agg(total="sum", pagamentos="size").reset_index()
                              .sort_values("total", ascending=False))
                achados.append(Finding(
                    id="casos-pf-prestadora", frente=FRENTE, gravidade="media",
                    titulo=f"{len(por_pessoa)} pessoa(s) física(s) recebendo por "
                           "serviços prestados",
                    descricao=("Prestação de serviço por pessoa física a órgão "
                               "público exige contratação regular e não pode "
                               "encobrir vínculo empregatício nem burlar concurso. "
                               "Cruze estes nomes com o quadro de pessoal e o rol "
                               "de conselheiros."),
                    evidencia=por_pessoa.head(60),
                    serie=A.serie_anual(servicos, "VALOR").rename(
                        columns={"VALOR": "serviços de pessoa física"}),
                    grafico="bar",
                    base_legal="CF, art. 37, II (concurso público); Lei 14.133/2021, "
                               "art. 6º, XVIII.",
                ))
    return achados
