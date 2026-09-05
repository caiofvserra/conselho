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
from ..utils import coluna, fmt_brl, norm_nome
from .common import df, fichas_cnpj, fornecedores_mercado

FRENTE = "Casos concentrados"

# Entidade cujo nome carrega o do próprio conselho: associação de servidores,
# de procuradores, de conselheiros. Contratar a própria "casa" é o vetor
# clássico de autocontratação em conselhos profissionais.
RX_ENTIDADE_CASA = re.compile(
    r"\bCREA\b|\bCONFEA\b|CONSELHO REGIONAL DE ENGENHARIA", re.IGNORECASE)
# Abreviações reais do portal. "ASSOC. DOS PROCURADORES" e "INST. PAULISTA"
# não casavam a forma por extenso, e a entidade de R$ 41,5 mi ficava de fora.
RX_ASSOCIATIVA = re.compile(
    r"ASSOCIACAO|ASSOC\.?\b|SINDICATO|SIND\.?\b|INSTITUTO|INST\.?\b|"
    r"FUNDACAO|FUND\.?\b|COOPERATIVA|CLUBE|CAIXA|SOCIEDADE|ENTIDADE|UNIAO",
    re.IGNORECASE)

# Quem é da "casa" pela função, mesmo sem o nome do conselho no rótulo: são os
# cargos que só existem DENTRO do órgão.
# Grupos não capturantes: `str.contains` com grupo de captura dispara aviso do
# pandas em toda rodada.
RX_QUADRO_PROPRIO = re.compile(
    r"PROCURADOR|ADVOGADO(?:S)? PUBLICO|CONSELHEIR|INSPETOR|SERVIDOR|"
    r"EMPREGADO(?:S)? PUBLICO|FUNCIONARIO", re.IGNORECASE)
# Piso de materialidade para uma entidade contar no TÍTULO: R$ 475,24 pagos
# uma vez em 2019 (rodada 10) não são "entidade da casa", são ruído.
PISO_ENTIDADE_CASA = 10_000.0


def _nomes_resolvidos(pg, staged) -> "pd.Series":
    """Razão social da Receita quando houver, senão o rótulo do portal.

    O portal trunca o favorecido em ~60 caracteres, e a truncagem come
    justamente o sufixo que identifica o conselho: "ASSOCIACAO DOS
    PROCURADORES, ADVOGADOS E CONSULTORES JURIDICOS" perde o "DO CREA-SP".
    Testar o regex contra o nome truncado escondia R$ 41,5 mi.
    """
    rotulo = coluna(pg, "NOME_RAZAO_SOCIAL").fillna("").astype(str)
    fichas = fichas_cnpj(staged)
    if not fichas or "FAVORECIDO_CNPJ" not in pg.columns:
        return rotulo
    razoes = {c: (f.get("razao_social") or "") for c, f in fichas.items()}
    receita = pg["FAVORECIDO_CNPJ"].map(razoes).fillna("")
    return receita.where(receita.str.len() > 0, rotulo)

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
        # nome do portal OU razão social da Receita — o que for mais completo
        nomes = (pg["FAVORECIDO_NORM"].fillna("").astype(str) + " | " +
                 _nomes_resolvidos(pg, staged).map(norm_nome))
        associativa = nomes.str.contains(RX_ASSOCIATIVA, regex=True, na=False)
        # "da casa" por duas vias: o nome do conselho aparece, OU a entidade é
        # formada por quem só existe dentro dele (procuradores, conselheiros)
        da_casa = (nomes.str.contains(RX_ENTIDADE_CASA, regex=True, na=False) |
                   nomes.str.contains(RX_QUADRO_PROPRIO, regex=True, na=False))
        casa = pg[associativa & da_casa]
        if not casa.empty:
            por_ent = (casa.groupby("FAVORECIDO_NORM")["VALOR"]
                       .agg(total="sum", pagamentos="size").reset_index()
                       .sort_values("total", ascending=False))
            relevantes = por_ent[por_ent["total"] >= PISO_ENTIDADE_CASA]
            total = float(relevantes["total"].sum())
            anos = pd.to_datetime(coluna(casa, "DATA", None), errors="coerce").dt.year.dropna()
            periodo = (f"{int(anos.min())}–{int(anos.max())}" if not anos.empty
                       and anos.min() != anos.max() else
                       (str(int(anos.min())) if not anos.empty else "período não datado"))
        if not casa.empty and not relevantes.empty:
            achados.append(Finding(
                id="casos-entidade-da-casa", frente=FRENTE, gravidade="media",
                titulo=(f"{fmt_brl(total)} pagos ({periodo}) a {len(relevantes)} "
                        "entidade(s) associativa(s) formada(s) pelo próprio quadro ou "
                        "com o nome do conselho"),
                descricao=(
                    "Associações, institutos e sindicatos cujo nome contém o do "
                    "próprio órgão — tipicamente constituídos por seus servidores, "
                    "procuradores ou conselheiros. Contratar a entidade formada "
                    "pelo próprio quadro é a situação em que a conferência do "
                    "objeto e da forma de contratação mais importa. Hipótese "
                    "alternativa a testar antes de qualquer leitura: repasse de "
                    "honorários de sucumbência à associação de procuradores — a "
                    "receita 'Honorários da cobrança judicial' do mesmo relatório "
                    "tem ordem de grandeza compatível. O instrumento (contrato, "
                    "convênio ou repasse) e a hipótese legal decidem."),
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
        # O contrato de valor implausível (ordem de grandeza fora da carteira)
        # já tem achado próprio em contratos.py; repeti-lo aqui publicava DOIS
        # achados altos sobre o mesmo C-010/2025 e uma carteira com ele.
        try:
            from .contratos import contratos_implausiveis
            implausiveis = contratos_implausiveis(ctr)
        except ImportError:                # módulo ausente: não exclui nada
            implausiveis = pd.Index([])
        if (v > 0).sum() >= 10:
            # Carteira de contratos é log-normal: o z-robusto sobre o valor
            # BRUTO sinalizava os 18% maiores (170 de 965), o que não
            # seleciona nada. Em escala log a distribuição fica simétrica e o
            # corte volta a significar "fora do padrão".
            import numpy as np
            z = A.zscore_robusto(pd.Series(np.log10(v[v > 0].values),
                                           index=v[v > 0].index))
            outliers = ctr.loc[z[z > 3.5].index].drop(index=implausiveis, errors="ignore")
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
                        "Contratos a mais de 3,5 desvios robustos do típico da "
                        "carteira, medidos em escala logarítmica — que é a "
                        "escala natural de valor de contrato. São os grandes "
                        "projetos do órgão, que merecem acompanhamento "
                        "individual, ou erro de cadastro no portal, que também "
                        "é achado de transparência. Verifique caso a caso."),
                    evidencia=outliers[cols].sort_values("VALOR", ascending=False),
                    como_verificar="Comparar o valor publicado no portal com o "
                                   "extrato do contrato no PNCP e com o total "
                                   "efetivamente pago à contratada.",
                ))

        # ---- concentração da carteira: o que outlier estatístico não mostra
        # Mesma carteira do achado de implausíveis: sem o C-010/2025 (23,6% da
        # carteira com ele), senão "metade da carteira" mede um erro de cadastro.
        vv = v.drop(index=implausiveis, errors="ignore")
        vv = vv[vv > 0].sort_values(ascending=False)
        if len(vv) >= 20:
            total_c = float(vv.sum())
            acum = vv.cumsum() / total_c
            n_metade = int((acum <= 0.5).sum()) + 1
            maiores = ctr.loc[vv.head(n_metade).index]
            cols_c = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                  "OBJETO", "DATA_INICIO_VIGENCIA")
                      if c in maiores]
            ev = maiores[cols_c].copy()
            ev["share_da_carteira_pct"] = (100 * ev["VALOR"] / total_c).round(2)
            achados.append(Finding(
                id="casos-concentracao-carteira", frente=FRENTE,
                gravidade="media" if n_metade <= len(vv) * 0.05 else "info",
                titulo=(f"{n_metade} de {len(vv)} contratos concentram metade "
                        f"da carteira ({fmt_brl(total_c)})"),
                descricao=(
                    "Quantos contratos bastam para chegar a 50% do valor "
                    "contratado. É a pergunta que o teste de outlier não "
                    "responde: não interessa se um contrato é estatisticamente "
                    "atípico, interessa onde está o dinheiro. Poucos contratos "
                    "concentrando a carteira significa que auditar meia dúzia "
                    "de processos cobre metade do risco."),
                evidencia=ev.sort_values("VALOR", ascending=False),
                metricas={"contratos_ate_50pct": n_metade,
                          "universo": int(len(vv)),
                          "carteira": round(total_c, 2)},
                como_verificar="Pedir o processo completo dos contratos "
                               "listados — é o menor conjunto que cobre a "
                               "maior parte do valor.",
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
