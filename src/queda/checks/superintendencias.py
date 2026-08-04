"""Frente — Fiscalizar o próprio Crea: onde o dinheiro fica e onde ele chega.

Um Crea existe para fiscalizar o exercício profissional em todo o estado. São
Paulo tem 645 municípios, e a fiscalização acontece na rua: agente que se
desloca, vistoria obra, autua. A pergunta desta frente é territorial e direta:
**quanto do dinheiro fica na estrutura central e quanto chega às unidades que
efetivamente fiscalizam?**

A fonte é `despesas_centro_custo`, que traz o NOME DO CENTRO DE CUSTO — a
única evidência de qual unidade gastou. Cada unidade é classificada em três
camadas territoriais e cruzada com os dois eixos da taxonomia (natureza ×
função), o que permite responder à segunda pergunta: dentro do que foi
alocado à fiscalização, quanto é salário de fiscal, quanto é deslocamento e
quanto é custeio da atividade.

Cuidado deliberado com a palavra "efetivamente": diária de agente fiscal que
viaja para vistoriar **é** custo de fiscalização, não desvio. O que esta
frente mede é a composição — e o achado aparece quando a verba supera o
salário, ou quando a unidade dita de fiscalização não gasta nada com a
atividade.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl, norm_nome
from .common import df

FRENTE = "Superintendências e fiscalização"

# --------------------------------------------------------------- territórios
# Escrito sobre os nomes REAIS que a coleta trouxe. A primeira versão usava
# `\bunidade\b` para achar unidade de campo e classificava "UNIDADE DE
# FINANÇAS - UF" (R$ 330 mi) como território — invertendo o achado inteiro.
# No Crea-SP a unidade de campo se chama UGI (Unidade de Gestão Integrada) e
# vem sempre com o nome da cidade; tudo que é "GERENCIA DE …", "UNIDADE DE …"
# e "EQUIPE DE …" é estrutura central.
CAMADAS: list[tuple[str, str]] = [
    # campo primeiro: é o único padrão inequívoco
    ("CAMPO",
     r"^ugi\b|\bugi\s|unidade de gestao integrada|\binspetoria\b|"
     r"posto(s)? de (atendimento|servico)|escritorio(s)? (regional|local)"),

    ("POLITICO",
     r"\bplenari|\bcamara(s)?\b|\bcolegio\b|\bconselho diretor\b|"
     r"\bcorpo de inspetores\b|\bcomissao\b|\bjunta\b"),

    ("DIRECAO",
     r"\bpresidencia\b|\bgabinete\b|\bdiretoria\b|\bsuperintendencia\b|"
     r"\bchefia\b|\bvice-?presidencia\b"),

    # tudo o mais que a coleta mostrou: gerências, unidades e equipes centrais
    ("ADMIN_CENTRAL",
     r"\bgerencia\b|\bunidade\b|\bequipe\b|\bsecretaria\b|\bassessoria\b|"
     r"\bouvidoria\b|\bauditoria\b|\bdepartamento\b|\bdepto\b|"
     r"\bcontroladoria\b|\bcorregedoria\b|\bcoordenadoria\b|\bdivisao\b|"
     r"\bsetor\b|\bnucleo\b"),
]
_CAMADAS = [(c, re.compile(rx)) for c, rx in CAMADAS]

ROTULO_CAMADA = {
    "CAMPO": "Unidades de campo (UGI, inspetorias)",
    "POLITICO": "Órgãos políticos (plenária, câmaras)",
    "DIRECAO": "Direção superior",
    "ADMIN_CENTRAL": "Administração central",
    "NAO_IDENTIFICADA": "Camada não identificada",
}
CENTRAIS = ("DIRECAO", "ADMIN_CENTRAL", "POLITICO")
TERRITORIAIS = ("CAMPO",)

# O portal renomeia centros de custo prefixando "Não utilizar - " e cria um
# novo com o mesmo nome. Sem fundir os dois, "UGI CAMPINAS" aparece duas vezes
# com metade do valor cada, e o ranking mente. Foram 73 rótulos assim em 120.
RX_DESATIVADO = re.compile(r"^\s*n[ãa]o\s+utilizar\s*[-–]\s*", re.IGNORECASE)


def normalizar_unidade(nome) -> str:
    """Nome canônico do centro de custo, sem a marca de desativação."""
    n = re.sub(r"\s+", " ", str(nome or "")).strip()
    return RX_DESATIVADO.sub("", n).strip()


def camada(nome_centro: str) -> str:
    t = T.normalizar(normalizar_unidade(nome_centro))
    if not t:
        return "NAO_IDENTIFICADA"
    for cat, rx in _CAMADAS:
        if rx.search(t):
            return cat
    return "NAO_IDENTIFICADA"


def _base(staged) -> pd.DataFrame:
    cc = df(staged, "despesas_centro_custo")
    if cc.empty or "VALOR_TOTAL" not in cc:
        return pd.DataFrame()
    d = cc.copy()
    d["valor"] = pd.to_numeric(d["VALOR_TOTAL"], errors="coerce").fillna(0)
    if "ANALITICO" in d:                       # não somar totalizadores
        d = d[d["ANALITICO"].astype(bool)]
    d = d[d["valor"] > 0]
    if d.empty:
        return d
    bruto = coluna(d, "NOME_CENTRO_CUSTO").fillna("").astype(str)
    d["unidade_rotulo"] = bruto
    d["unidade"] = bruto.map(normalizar_unidade)      # funde renomeações
    d["desativado"] = bruto.str.match(RX_DESATIVADO)
    d["conta"] = coluna(d, "NOME_CONTA").fillna("").astype(str)
    unicos = {u: camada(u) for u in d["unidade"].unique()}
    d["camada"] = d["unidade"].map(unicos)
    d["natureza"] = T.classificar_series(d["conta"], d["unidade"])
    d["funcao"] = T.classificar_funcao_series(d["unidade"], d["natureza"])
    return d


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    d = _base(staged)
    if d.empty:
        return achados

    total = float(d["valor"].sum())
    d["camada_rotulo"] = d["camada"].map(ROTULO_CAMADA)

    # ------------------------------------------------ 1. central × território
    serie_camada = A.serie_anual(d, "valor", by="camada_rotulo")
    central = float(d.loc[d["camada"].isin(CENTRAIS), "valor"].sum())
    territorio = float(d.loc[d["camada"].isin(TERRITORIAIS), "valor"].sum())
    identificado = central + territorio
    pct_central = 100 * central / identificado if identificado else 0.0
    razao = central / territorio if territorio else float("inf")
    n_desativados = int(d.loc[d["desativado"], "unidade"].nunique())
    achados.append(Finding(
        id="super-central-vs-territorio", frente=FRENTE,
        gravidade="alta" if pct_central > 70 else "media",
        titulo=(f"{pct_central:.0f}% da despesa identificada por unidade ficou na "
                f"estrutura central; {100 - pct_central:.0f}% chegou às unidades "
                "de campo (UGIs)"),
        descricao=(
            "O Crea-SP fiscaliza o exercício profissional em 645 municípios, e "
            "a fiscalização acontece na ponta: agente que se desloca, vistoria "
            "e autua. Esta é a divisão territorial do gasto — quanto ficou em "
            "presidência, diretoria, câmaras, gerências e unidades da sede, e "
            "quanto foi alocado às UGIs (Unidades de Gestão Integrada) e "
            "inspetorias, que são as unidades de campo. Não existe proporção "
            "legalmente correta, mas a série mostra a direção: estrutura "
            "central crescendo mais rápido que a ponta é o movimento a "
            "explicar."
            + (f" Nota metodológica: {n_desativados} centro(s) de custo "
               "aparecem no portal com o prefixo 'Não utilizar' e um homônimo "
               "ativo — são a mesma unidade renomeada, e foram fundidos aqui; "
               "sem isso o valor de cada uma apareceria pela metade."
               if n_desativados else "")),
        evidencia=(d.groupby(["_ano", "camada_rotulo"])["valor"].sum()
                   .reset_index().sort_values(["_ano", "valor"],
                                              ascending=[True, False])),
        serie=serie_camada, grafico="area_stacked",
        tabela_anual=(serie_camada.div(serie_camada.sum(axis=1)
                                       .replace(0, pd.NA), axis=0) * 100
                      ).round(1).reset_index(),
        base_legal="Lei 5.194/1966, arts. 24 e 34 (fiscalizar o exercício "
                   "profissional é a competência primária); CF, art. 37, caput "
                   "(eficiência).",
        metricas={"central": round(central, 2), "territorio": round(territorio, 2),
                  "pct_central": round(pct_central, 1),
                  "razao_central_territorio": (None if razao == float("inf")
                                               else round(razao, 2))},
        como_verificar="Pedir o organograma com a lotação de cada centro de "
                       "custo e o número de agentes fiscais por unidade.",
    ))

    # ------------------------------------------------ 2. fiscalização efetiva
    fiscal = d[d["funcao"] == "FISCALIZACAO"]
    if not fiscal.empty:
        comp = A.serie_anual(fiscal, "valor", by="natureza")
        comp = comp.rename(columns=T.GRUPOS)
        tot_ano = A.serie_anual(d, "valor")["valor"]
        fis_ano = comp.sum(axis=1)
        pct_ano = (100 * fis_ano / tot_ano.reindex(fis_ano.index)).round(1)
        pct_medio = float(pct_ano.mean())

        verbas = float(fiscal.loc[fiscal["natureza"] == "VERBAS_PESSOAIS", "valor"].sum())
        salarios = float(fiscal.loc[fiscal["natureza"] == "REMUNERACAO", "valor"].sum())
        operacional = float(fiscal.loc[~fiscal["natureza"].isin(
            ["VERBAS_PESSOAIS", "REMUNERACAO"]), "valor"].sum())
        # A UGI faz atendimento E fiscalização no mesmo centro de custo — o
        # portal não separa. Cravar um número só seria escolher entre
        # subestimar (só contas explicitamente rotuladas) e superestimar (toda
        # a UGI). Publicamos o INTERVALO, que é o que o dado sustenta.
        campo = d[d["camada"] == "CAMPO"]
        teto_ano = ((fis_ano.add(A.serie_anual(campo, "valor")["valor"], fill_value=0)
                     if not campo.empty else fis_ano))
        pct_teto = (100 * teto_ano / tot_ano.reindex(teto_ano.index)).round(1)
        pct_teto_medio = float(pct_teto.mean())
        achados.append(Finding(
            id="super-fiscalizacao-efetiva", frente=FRENTE,
            gravidade="alta" if pct_teto_medio < 15 else "media",
            titulo=(f"Entre {pct_medio:.1f}% e {pct_teto_medio:.1f}% da despesa "
                    "por centro de custo sustenta a fiscalização"),
            descricao=(
                "Quanto do orçamento chega à atividade que justifica a "
                "existência do conselho, e **de que é feito esse valor**.\n\n"
                f"O número é um intervalo, não um ponto: o piso ({pct_medio:.1f}%) "
                "conta só o que está explicitamente rotulado como fiscalização; "
                f"o teto ({pct_teto_medio:.1f}%) soma todas as UGIs — as "
                "Unidades de Gestão Integrada, que são as unidades de campo e "
                "onde os agentes fiscais estão lotados, mas que fazem "
                "atendimento e análise de processo no MESMO centro de custo. "
                "O portal não separa as duas coisas, e cravar um número só "
                "seria escolher entre subestimar e superestimar. Mesmo o teto "
                "é baixo para um órgão cuja competência primária é fiscalizar."
                "\n\nA decomposição importa mais que o total: diária de agente "
                "que viaja para vistoriar é custo legítimo de fiscalização, "
                "não desvio. O que merece explicação é a proporção — verba "
                "indenizatória superando o salário do quadro que fiscaliza "
                "indica atividade sustentada por pagamento eventual e não por "
                f"estrutura permanente. No núcleo explícito: {fmt_brl(salarios)} "
                f"em remuneração, {fmt_brl(verbas)} em verbas a pessoas e "
                f"{fmt_brl(operacional)} em custeio operacional."),
            evidencia=comp.reset_index(),
            serie=pd.DataFrame({"piso (rótulo explícito)": pct_ano,
                                "teto (incluindo UGIs)": pct_teto}),
            grafico="line", unidade="%",
            tabela_anual=pd.DataFrame(
                {"% piso": pct_ano, "% teto": pct_teto}).reset_index(),
            base_legal="Lei 5.194/1966, art. 34, alíneas 'a' e 'b'; Lei "
                       "12.514/2011 (a anuidade custeia a fiscalização).",
            metricas={"pct_piso": round(pct_medio, 2),
                      "pct_teto": round(pct_teto_medio, 2),
                      "remuneracao": round(salarios, 2),
                      "verbas_pessoais": round(verbas, 2),
                      "custeio_operacional": round(operacional, 2),
                      "verba_sobre_salario": (round(verbas / salarios, 2)
                                              if salarios else None),
                      "pct_por_ano": {int(a): float(v) for a, v in pct_ano.items()}},
            como_verificar="Pedir, por UGI, quantos agentes fiscais estão "
                           "lotados e quantas vistorias e autos foram feitos — "
                           "é o que fecha o intervalo num número só. Pedir "
                           "também o relatório anual de fiscalização.",
        ))
        if salarios > 0 and verbas > salarios:
            achados.append(Finding(
                id="super-verba-supera-salario", frente=FRENTE, gravidade="alta",
                titulo=(f"Nas unidades de fiscalização, a verba indenizatória "
                        f"({fmt_brl(verbas)}) supera a remuneração "
                        f"({fmt_brl(salarios)})"),
                descricao=(
                    "Fiscalizar exige quadro permanente. Quando o pagamento "
                    "eventual — diária, quilometragem, ajuda de custo — supera "
                    "a folha da própria atividade, a fiscalização deixou de ser "
                    "função de servidor e virou tarefa remunerada por evento, "
                    "fora do contracheque, do teto e da transparência da folha."),
                evidencia=(fiscal.groupby(["_ano", "natureza"])["valor"].sum()
                           .reset_index()),
                serie=comp, grafico="bar_stacked",
                base_legal="CF, art. 37, II e XI; Lei 4.320/1964, art. 63.",
            ))

    # ------------------------------------------------ 3. ranking por unidade
    por_unidade = (d.groupby("unidade")
                   .agg(total=("valor", "sum"), exercicios=("_ano", "nunique"),
                        camada=("camada_rotulo", "first")).reset_index()
                   .sort_values("total", ascending=False))
    por_unidade["pct_do_total"] = (100 * por_unidade["total"] / total).round(2)
    lideres = por_unidade.head(8)["unidade"].tolist()
    achados.append(Finding(
        id="super-ranking-unidades", frente=FRENTE, gravidade="info",
        titulo=f"{len(por_unidade)} centros de custo — quanto cada um gastou, "
               "por exercício",
        descricao="Ranking completo das unidades, com a camada territorial de "
                  "cada uma. O gráfico acompanha as oito maiores ao longo do "
                  "período: mostra qual unidade cresceu, qual encolheu e quando.",
        evidencia=por_unidade.head(120),
        serie=A.serie_anual(d[d["unidade"].isin(lideres)], "valor", by="unidade"),
        grafico="bar_stacked",
        tabela_anual=A.ranking_anual(d, "unidade", "valor", top=10),
    ))

    # ---- unidade territorial que quase não gasta com a atividade-fim
    terr = d[d["camada"].isin(TERRITORIAIS)]
    if not terr.empty:
        perfil = terr.pivot_table(index="unidade", columns="funcao",
                                  values="valor", aggfunc="sum", fill_value=0.0)
        perfil["total"] = perfil.sum(axis=1)
        perfil = perfil[perfil["total"] >= 100_000]
        if not perfil.empty:
            perfil["pct_fiscalizacao"] = (
                100 * perfil.get("FISCALIZACAO", 0) / perfil["total"]).round(1)
            secas = perfil[perfil["pct_fiscalizacao"] < 5].sort_values(
                "total", ascending=False)
            if not secas.empty:
                achados.append(Finding(
                    id="super-unidade-sem-fiscalizacao", frente=FRENTE,
                    gravidade="media",
                    titulo=(f"{len(secas)} unidade(s) territorial(is) com menos de "
                            "5% da despesa em contas de fiscalização"),
                    descricao=(
                        "Unidades regionais e locais existem para levar a "
                        "fiscalização ao interior. Quando a despesa de uma "
                        "delas é quase toda remuneração e custeio de escritório, "
                        "sem rubrica ligada à atividade, ou a classificação "
                        "contábil não distingue a atividade-fim — o que já é "
                        "falha de transparência — ou a unidade opera como posto "
                        "administrativo. As duas hipóteses merecem resposta."),
                    evidencia=secas.reset_index().head(60),
                    base_legal="Lei 5.194/1966, art. 34; Lei 4.320/1964, art. 15 "
                               "(despesa discriminada por elemento).",
                    como_verificar="Pedir, para as unidades listadas, o número "
                                   "de agentes fiscais lotados e o total de "
                                   "vistorias realizadas no período.",
                ))

    # ---- tendência divergente: unidade cresce, fiscalização encolhe
    if not fiscal.empty and d["_ano"].nunique() >= 4:
        tot_ano = A.serie_anual(d, "valor")["valor"]
        fis_ano = A.serie_anual(fiscal, "valor")["valor"].reindex(
            tot_ano.index).fillna(0)
        cagr_tot, cagr_fis = A.cagr(tot_ano), A.cagr(fis_ano)
        if cagr_tot == cagr_tot and cagr_fis == cagr_fis and cagr_tot > cagr_fis:
            comp2 = pd.DataFrame({"despesa total": tot_ano,
                                  "fiscalização": fis_ano})
            base_idx = comp2 / comp2.iloc[0] * 100
            achados.append(Finding(
                id="super-tendencia-divergente", frente=FRENTE,
                gravidade="alta" if cagr_fis < 0 else "media",
                titulo=(f"A despesa total cresce {cagr_tot:.1%} ao ano e a "
                        f"fiscalização, {cagr_fis:.1%}"),
                descricao=(
                    "As duas curvas partem de 100 no primeiro exercício, o que "
                    "torna comparável o ritmo e não o tamanho. Quando a "
                    "despesa total sobe mais rápido que a atividade-fim, a "
                    "diferença foi absorvida pela estrutura: cada real a mais "
                    "arrecadado do profissional financiou proporcionalmente "
                    "menos fiscalização."),
                evidencia=comp2.round(2).reset_index(),
                serie=base_idx.round(1), grafico="line", unidade="índice",
                tabela_anual=comp2.round(2).reset_index(),
                metricas={"cagr_total": round(cagr_tot, 4),
                          "cagr_fiscalizacao": round(cagr_fis, 4)},
            ))

    # ------------------------------------------------ 4. quadro por lotação
    quadro = df(staged, "quadro_pessoal")
    if not quadro.empty and "LOTACAO_NORM" in quadro:
        q = quadro[quadro["LOTACAO_NORM"].fillna("").str.len() > 2].copy()
        if not q.empty:
            q["camada"] = q["LOTACAO_NORM"].map(camada).map(ROTULO_CAMADA)
            resumo = (q.groupby("camada")
                      .agg(pessoas=("NOME_NORM", "nunique"),
                           folha_mensal=("remuneracao", "sum")).reset_index()
                      .sort_values("folha_mensal", ascending=False))
            pessoas_fim = int(q[q["LOTACAO_NORM"].map(
                lambda s: T.classificar_funcao(s) == "FISCALIZACAO")]
                ["NOME_NORM"].nunique())
            achados.append(Finding(
                id="super-quadro-por-camada", frente=FRENTE,
                gravidade="media" if pessoas_fim * 3 < len(q) else "info",
                titulo=(f"{pessoas_fim} de {q['NOME_NORM'].nunique()} pessoas do "
                        "quadro estão lotadas em unidades de fiscalização"),
                descricao=(
                    "Onde está a gente, e não só o dinheiro. Fiscalização é "
                    "atividade de pessoal: a distribuição do quadro entre "
                    "estrutura central e unidades de campo diz, melhor que "
                    "qualquer rubrica, qual é a prioridade real do conselho."),
                evidencia=resumo,
                metricas={"pessoas_fiscalizacao": pessoas_fim,
                          "pessoas_total": int(q["NOME_NORM"].nunique())},
                como_verificar="Confrontar com o quadro de cargos aprovado e "
                               "com o número de agentes fiscais concursados.",
            ))

    # ------------------------------------------------ 5. cobertura
    nao_id = float(d.loc[d["camada"] == "NAO_IDENTIFICADA", "valor"].sum())
    if total > 0 and 100 * nao_id / total > 20:
        maiores = (d[d["camada"] == "NAO_IDENTIFICADA"]
                   .groupby("unidade")["valor"].sum()
                   .sort_values(ascending=False).head(40).reset_index())
        achados.append(Finding(
            id="super-cobertura", frente=FRENTE, gravidade="info",
            titulo=f"{100 * nao_id / total:.0f}% da despesa está em centros de "
                   "custo que não foi possível situar no território",
            descricao="Unidades cujo nome não permite dizer se são estrutura "
                      "central ou de campo. A lista abaixo, ordenada por valor, "
                      "é o que precisa ser nomeado para fechar a conta — por "
                      "regra ou pelo organograma oficial.",
            evidencia=maiores,
            metricas={"pct_nao_identificado": round(100 * nao_id / total, 1)},
        ))
    return achados
