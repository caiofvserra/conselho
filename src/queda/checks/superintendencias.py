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
import unicodedata

import pandas as pd

from .. import analytics as A
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl, so_analiticas, topo
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
    """Nome canônico do centro de custo, sem a marca de desativação.

    Dobra acento e espaço em torno do hífen porque o portal grava a mesma
    unidade de formas diferentes — "GERENCIA EXECUTIVA - GEX" e "GERENCIA
    EXECUTIVA-GEX", "ADMINISTRAÇAO" e "ADMINISTRACAO". Sem isso R$ 264 mi
    continuavam partidos entre variantes do mesmo nome.
    """
    n = unicodedata.normalize("NFD", str(nome or ""))
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    n = RX_DESATIVADO.sub("", n)
    n = re.sub(r"\s*-\s*", " - ", n)      # padroniza o hífen
    return re.sub(r"\s+", " ", n).strip().upper()


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
    d = so_analiticas(d)                       # não somar totalizadores
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
        # A razão verba/folha só é interpretável se a FOLHA existir em todos os
        # exercícios comparados. Na rodada real, 2021 trazia R$ 0,00 de
        # remuneração contra R$ 3,53 mi de verba no mesmo conjunto: isso não é
        # "a verba supera", é a folha não estar escriturada nesses centros de
        # custo — buraco de alocação, e o achado somava esse buraco ao numerador
        # do período inteiro. Com 2021 dentro a razão dava 1,36; sem ele, 1,20.
        #
        # E a gravidade era fixa em "alta" mesmo quando os exercícios mais
        # recentes contrariavam a tese (2025 deu 0,83 e 2026, 0,92).
        por_ano_nat = fiscal.groupby(["_ano", "natureza"])["valor"].sum().unstack(fill_value=0.0)
        rem_ano = por_ano_nat.get("REMUNERACAO", pd.Series(dtype=float))
        vp_ano = por_ano_nat.get("VERBAS_PESSOAIS", pd.Series(dtype=float))
        anos_sem_folha = [int(a) for a, v in rem_ano.items() if float(v) <= 0]
        anos_completos = [a for a in por_ano_nat.index if a not in anos_sem_folha]
        rem_ok = float(rem_ano.reindex(anos_completos).fillna(0).sum())
        vp_ok = float(vp_ano.reindex(anos_completos).fillna(0).sum())
        recentes = sorted(anos_completos)[-2:]
        contraria = [int(a) for a in recentes
                     if float(vp_ano.get(a, 0)) <= float(rem_ano.get(a, 0))]

        if rem_ok > 0 and vp_ok > rem_ok:
            achados.append(Finding(
                id="super-verba-supera-salario", frente=FRENTE,
                gravidade="media" if contraria else "alta",
                titulo=(f"Nas unidades de fiscalização, a verba indenizatória "
                        f"({fmt_brl(vp_ok)}) supera a remuneração "
                        f"({fmt_brl(rem_ok)}) — {vp_ok / rem_ok:.2f}×"),
                descricao=(
                    "Fiscalizar exige quadro permanente. Quando o pagamento "
                    "eventual — diária, quilometragem, ajuda de custo — supera "
                    "a folha da própria atividade, a fiscalização passou a ser "
                    "remunerada em grande parte por evento, fora da folha — e o "
                    "que é pago por evento não aparece no contracheque publicado."
                    + (f"\n\nExercícios {anos_sem_folha} ficaram FORA da conta: "
                       "não há folha alocada a esses centros de custo neles, o "
                       "que indica buraco de alocação e não ausência de "
                       "salário. Somá-los inflaria a razão."
                       if anos_sem_folha else "")
                    + (f"\n\nNos exercícios mais recentes ({contraria}) a "
                       "relação está INVERTIDA — a folha supera a verba. A "
                       "afirmação vale para o período acumulado, não para a "
                       "tendência." if contraria else "")),
                evidencia=por_ano_nat.reset_index(),
                serie=comp, grafico="bar_stacked",
                base_legal="CF, art. 37, II e XI; Lei 4.320/1964, art. 63.",
                metricas={"verba_sobre_salario": round(vp_ok / rem_ok, 2),
                          "exercicios_sem_folha_alocada": anos_sem_folha,
                          "exercicios_que_contrariam": contraria},
                como_verificar="Pedir em que centros de custo está a folha do "
                               "quadro que fiscaliza, e por que ela não aparece "
                               "nas unidades de fiscalização em todos os "
                               "exercícios.",
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
        evidencia=topo(por_unidade, 120, "total"),
        serie=A.serie_anual(d[d["unidade"].isin(lideres)], "valor", by="unidade"),
        grafico="bar_stacked",
        tabela_anual=A.ranking_anual(d, "unidade", "valor", top=10),
    ))

    # ---- unidade territorial que quase não gasta com a atividade-fim
    #
    # ACHADO QUE SE ACUSAVA SOZINHO. A versão anterior publicava "N unidades
    # territoriais com menos de 5% da despesa em contas de fiscalização" e
    # oferecia duas hipóteses, uma delas sobre o órgão ("a unidade opera como
    # posto administrativo"). Só que o resultado era 0,0% para TODAS elas, em
    # qualquer conselho e em qualquer exercício, por construção das NOSSAS
    # regras: a camada CAMPO casa "UGI", e a regra de função manda "UGI" para
    # ATENDIMENTO antes de qualquer coisa. A interseção CAMPO ∩ FISCALIZAÇÃO é
    # vazia por desenho, e um número que só pode dar um valor não mede nada.
    #
    # A guarda abaixo pergunta isso à própria taxonomia, com os nomes reais das
    # unidades desta rodada: alguma unidade territorial PODERIA ser lida como de
    # fiscalização? Se não, o que se publica é o limite da ferramenta — que é um
    # ponto de transparência legítimo e sobrevive ao contraditório — e nunca uma
    # hipótese sobre a unidade.
    terr = d[d["camada"].isin(TERRITORIAIS)]
    if not terr.empty:
        perfil = terr.pivot_table(index="unidade", columns="funcao",
                                  values="valor", aggfunc="sum", fill_value=0.0)
        perfil["total"] = perfil.sum(axis=1)
        perfil = perfil[perfil["total"] >= 100_000]
        alcanca = any(T.classificar_funcao(u) == "FISCALIZACAO"
                      for u in perfil.index)
        if not perfil.empty:
            perfil["pct_fiscalizacao"] = (
                100 * perfil.get("FISCALIZACAO", 0) / perfil["total"]).round(1)
            secas = perfil[perfil["pct_fiscalizacao"] < 5].sort_values(
                "total", ascending=False)
            if not secas.empty and not alcanca:
                achados.append(Finding(
                    id="super-territorio-nao-separavel", frente=FRENTE,
                    gravidade="info",
                    titulo=(f"A despesa das {len(perfil)} unidades territoriais "
                            f"({fmt_brl(float(perfil['total'].sum()))}) não "
                            "permite separar vistoria de atendimento"),
                    descricao=(
                        "As unidades de campo do conselho aparecem na "
                        "contabilidade publicada identificadas pela CIDADE, não "
                        "pela função. Por isso não é possível, de fora, saber "
                        "quanto do gasto de uma UGI sustenta a vistoria e "
                        "quanto sustenta o atendimento no balcão.\n\n"
                        "**Este achado não diz que essas unidades não "
                        "fiscalizam, e não poderia dizer.** A nossa "
                        "classificação lê a função no NOME do centro de custo e "
                        "trata 'UGI' como atendimento: nenhuma unidade "
                        "territorial poderia ser lida como de fiscalização, em "
                        "conselho nenhum e em exercício nenhum. O que se "
                        "publica aqui é o limite do dado publicado, não uma "
                        "medida sobre a atividade."),
                    evidencia=perfil.reset_index().head(60),
                    base_legal="Lei 4.320/1964, art. 15 (despesa discriminada "
                               "por elemento).",
                    como_verificar="Pedir, por unidade territorial, o número de "
                                   "agentes fiscais lotados e o total de "
                                   "vistorias e autos por exercício — é o dado "
                                   "que fecha esta lacuna.",
                    cobertura={"base": float(perfil["total"].sum()),
                               "universo": total,
                               "o_que": "despesa das unidades territoriais"},
                ))
            elif not secas.empty:
                achados.append(Finding(
                    id="super-unidade-sem-fiscalizacao", frente=FRENTE,
                    gravidade="media",
                    titulo=(f"{len(secas)} de {len(perfil)} unidade(s) "
                            "territorial(is) com menos de 5% da despesa em "
                            "contas de fiscalização"),
                    descricao=(
                        "Unidades regionais e locais existem para levar a "
                        "fiscalização ao interior. Quando a despesa de uma "
                        "delas é quase toda remuneração e custeio de escritório, "
                        "sem rubrica ligada à atividade, ou a classificação "
                        "contábil não distingue a atividade-fim — o que já é "
                        "falha de transparência — ou a unidade opera como posto "
                        "administrativo. As duas hipóteses merecem resposta.\n\n"
                        "A comparação só é publicada porque OUTRAS unidades "
                        "territoriais desta mesma rodada foram lidas como de "
                        "fiscalização: sem esse contraste, o percentual seria "
                        "zero por construção da nossa regra e não mediria nada."),
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
