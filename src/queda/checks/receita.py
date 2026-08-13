"""Frente — De onde vem o dinheiro, e o que ele deveria comprar.

Toda análise de despesa responde "quanto se gastou". Nenhuma responde "isso é
muito?". A receita fecha essa lacuna por dois caminhos:

* **Composição** — quanto vem de anuidade compulsória, quanto de ART, quanto
  de multa. Um conselho que arrecada cada vez mais de multa e cada vez menos
  de anuidade mudou de natureza sem que ninguém decidisse isso.
* **Denominador de atividade** — a receita de **ART** é a melhor proxy pública
  do volume de engenharia que o conselho deveria estar fiscalizando: cada ART
  é uma obra ou serviço técnico registrado. Se a ART cresce e o gasto com
  fiscalização não acompanha, a cobertura por obra caiu — e isso é mensurável
  sem pedir nada a ninguém.

A receita de **multa** merece leitura própria e cuidadosa: ela é ao mesmo
tempo produto da fiscalização (autuar gera multa) e incentivo perverso
(arrecadar por autuação). Crescer sozinha, sem o gasto de fiscalização
crescer, é sinal de que a autuação virou receita e não método.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from ..findings import Finding
from ..utils import coluna, fmt_brl
from .common import df

FRENTE = "Receita e cobertura"

# Fontes de receita de um conselho profissional. Ordem importa: "multa de
# anuidade" é acessório da anuidade, não produto de fiscalização.
FONTES: list[tuple[str, str]] = [
    ("DIVIDA_ATIVA", r"divida ativa|execucao fiscal"),
    # `juros` solto capturava "juros de aplicações financeiras" antes de
    # FINANCEIRA chegar a ser testada: na rodada real a receita financeira
    # ficou com 4 contas e R$ 26 mi enquanto "multa e juros de anuidade" ficou
    # com 25. Juros só é acessório de anuidade quando o nome diz isso.
    ("MULTA_ANUIDADE",
     r"multa.*(anuidade|mora|atraso)|juros.*(anuidade|mora|atraso)|"
     r"(anuidade|mora|atraso).*juros"),
    ("MULTA_FISCALIZACAO",
     r"multa.*(infrac|fiscaliza|exercicio ilegal)|auto de infrac"),
    # ANUIDADE vem ANTES de ART, e a razão é concreta: nome de conta contábil
    # brasileiro cita artigo de lei o tempo todo ("Repasse Anuidade Técnicos
    # Industriais - Lei 13.639/2018", que existe neste plano). Com ART na
    # frente, `\bart\b` casava a citação e a receita de anuidade era
    # contabilizada como ART — foi o que produziu, na rodada real, ART de
    # R$ 1,55 bi contra anuidade de R$ 452 mi, proporção impossível num
    # conselho profissional, onde a anuidade é a fonte principal por lei.
    ("ANUIDADE", r"anuidade"),
    # E mesmo depois disso, `\bart\b` não pode casar "art. 5º" nem "Lei ...,
    # art": a ART é o documento, não a citação.
    ("ART", r"(?<!lei )\bart\b(?!\s*\.?\s*\d)|anotacao de responsabilidade"),
    ("SERVICOS", r"certidao|certidoes|carteira|registro|acervo|\bcat\b|"
                 r"servico(s)? prestado|emolumento|taxa de expediente"),
    ("FINANCEIRA", r"financeir|aplicac|rendiment|juros"),
    ("TRANSFERENCIA", r"\bconfea\b|cota[- ]parte|transferenc|convenio|subvenc"),
]
_FONTES = [(k, re.compile(rx)) for k, rx in FONTES]

ROTULO = {
    "ANUIDADE": "Anuidades (compulsória)",
    "ART": "ART e acervo técnico",
    "MULTA_FISCALIZACAO": "Multas de fiscalização",
    "MULTA_ANUIDADE": "Multa e juros de anuidade",
    "DIVIDA_ATIVA": "Dívida ativa",
    "SERVICOS": "Serviços e certidões",
    "FINANCEIRA": "Receita financeira",
    "TRANSFERENCIA": "Transferências recebidas",
    "OUTRA": "Outras receitas",
}


def fonte(nome: str) -> str:
    from .. import taxonomia as T
    t = T.normalizar(nome)
    if not t:
        return "OUTRA"
    for cat, rx in _FONTES:
        if rx.search(t):
            return cat
    return "OUTRA"


def _receita(staged) -> pd.DataFrame:
    rec = df(staged, "comparativo_receita")
    if rec.empty:
        return pd.DataFrame()
    col = ("REALIZADO_EXERCICIO" if "REALIZADO_EXERCICIO" in rec
           else "TOTAL_EXERCICIO")
    if col not in rec or "_ano" not in rec:
        return pd.DataFrame()
    d = rec.copy()
    if "ANALITICA" in d:                       # não somar totalizadores
        d = d[d["ANALITICA"].astype(bool)]
    d["valor"] = pd.to_numeric(d[col], errors="coerce").fillna(0)
    d = d[d["valor"] > 0]
    if d.empty:
        return d
    d["conta"] = coluna(d, "NOME").fillna("").astype(str)
    d["fonte"] = d["conta"].map({c: fonte(c) for c in d["conta"].unique()})
    d["rotulo"] = d["fonte"].map(ROTULO)
    return d


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    d = _receita(staged)
    if d.empty:
        return achados
    total = float(d["valor"].sum())

    # ------------------------------------------------ 1. composição
    serie = A.serie_anual(d, "valor", by="rotulo")
    por_fonte = (d.groupby(["fonte", "rotulo"])["valor"]
                 .agg(total="sum", contas="size").reset_index()
                 .sort_values("total", ascending=False))
    por_fonte["share_pct"] = (100 * por_fonte["total"] / total).round(2)
    pct = serie.div(serie.sum(axis=1).replace(0, pd.NA), axis=0) * 100

    # Quanto da receita a classificação NÃO alcançou. Na rodada real foram
    # R$ 1,74 bi em 432 contas caindo em "outras" — 40% do total. Uma
    # composição com 40% em "outras" não sustenta frase nenhuma sobre a
    # natureza do órgão, e o leitor precisa saber disso antes de citar, não
    # depois. As maiores contas não classificadas saem nomeadas, para que a
    # correção seja possível — é o mesmo tratamento que a frente do plano de
    # contas dá ao buraco da taxonomia de despesa, e lá ele funcionou.
    outras = d[d["fonte"] == "OUTRA"]
    val_outras = float(outras["valor"].sum())
    top_outras = pd.DataFrame()
    if not outras.empty:
        top_outras = (outras.groupby("conta")["valor"].sum()
                      .sort_values(ascending=False).head(25).reset_index()
                      .rename(columns={"conta": "conta não classificada",
                                       "valor": "total"}))
    achados.append(Finding(
        id="receita-composicao", frente=FRENTE, gravidade="info",
        titulo=(f"De onde vieram os {fmt_brl(total)} arrecadados no período"
                + (f" — {100 * val_outras / total:.0f}% ainda sem classificação"
                   if total and val_outras / total > 0.15 else "")),
        cobertura={"base": total - val_outras, "universo": total,
                   "o_que": "as contas de receita que a ferramenta conseguiu "
                            "atribuir a uma fonte conhecida"},
        descricao=(
            "A anuidade é compulsória e existe para custear a fiscalização do "
            "exercício profissional (Lei 12.514/2011). As demais fontes dizem "
            "o que mais o conselho faz: ART mede o volume de engenharia "
            "registrada, multa mede autuação, dívida ativa mede inadimplência "
            "cobrada judicialmente. A composição mudando ao longo do tempo é "
            "mudança de natureza do órgão — e costuma acontecer sem decisão "
            "explícita.\n\n"
            + (f"**{fmt_brl(val_outras)} em {int(outras['conta'].nunique())} "
               "contas não foram atribuídos a nenhuma fonte conhecida.** A "
               "classificação é por nome de conta e é nossa, não do órgão: "
               "conta com nome que não bate com nenhum padrão cai aqui. As "
               "maiores estão nomeadas na tabela ao lado para que a regra "
               "possa ser corrigida — enquanto esse resíduo for grande, os "
               "percentuais desta composição não devem ser citados como a "
               "estrutura de receita do conselho.\n\n"
               if val_outras > 0 else "")
            + "Uma conferência externa fecha a questão em minutos: o balanço "
            "orçamentário publicado pelo próprio conselho traz a receita por "
            "categoria econômica, e os totais têm de bater."),
        evidencia=por_fonte,
        serie=serie, grafico="area_stacked",
        tabela_anual=(top_outras if not top_outras.empty
                      else pct.round(1).reset_index()),
        base_legal="Lei 12.514/2011 (anuidades); Lei 6.994/1982 (ART); Lei "
                   "5.194/1966, arts. 24 e 34.",
        como_verificar=("Comparar cada linha com o Balanço Orçamentário do "
                        "exercício e com a Demonstração das Variações "
                        "Patrimoniais publicados pelo conselho."),
        metricas={"total": round(total, 2),
                  "nao_classificado": round(val_outras, 2),
                  "pct_nao_classificado": (round(100 * val_outras / total, 1)
                                           if total else 0.0),
                  "contas_nao_classificadas": int(outras["conta"].nunique())
                  if not outras.empty else 0,
                  "por_fonte": {r["rotulo"]: round(float(r["total"]), 2)
                                for _, r in por_fonte.iterrows()}},
    ))

    # ------------------------------------------------ 2. ART como denominador
    #
    # Cada ART é uma obra ou serviço técnico registrado — é a melhor proxy
    # pública do volume que o conselho deveria fiscalizar. O gasto de
    # fiscalização dividido por ela mede COBERTURA, não esforço absoluto.
    art = d[d["fonte"] == "ART"].groupby("_ano")["valor"].sum()
    if len(art) >= 4:
        from .finalistica import _base_despesa
        base, _fonte_desp = _base_despesa(staged)
        if not base.empty:
            base = base[base["valor"] > 0]
            eixo = ("funcao" if (base["funcao"] != "NAO_IDENTIFICADA").mean() > 0.3
                    else "categoria")
            fisc = (base[base[eixo] == "FISCALIZACAO"]
                    .groupby("_ano")["valor"].sum())
            comp = pd.DataFrame({"receita de ART": art,
                                 "gasto com fiscalização": fisc}).dropna()
            if len(comp) >= 4 and float(comp["receita de ART"].sum()) > 0:
                comp["centavos de fiscalização por R$ de ART"] = (
                    100 * comp["gasto com fiscalização"] /
                    comp["receita de ART"]).round(1)
                cagr_art = A.cagr(comp["receita de ART"])
                cagr_fisc = A.cagr(comp["gasto com fiscalização"])
                caiu = (comp["centavos de fiscalização por R$ de ART"].iloc[-1] <
                        comp["centavos de fiscalização por R$ de ART"].iloc[0])
                achados.append(Finding(
                    id="receita-cobertura-por-art", frente=FRENTE,
                    gravidade="alta" if caiu else "media",
                    # Cobertura NÃO é o share da ART. ART ser 24% da receita
                    # é um fato sobre o conselho; o que limita esta medida é
                    # quanto da receita a ferramenta conseguiu classificar —
                    # se metade das contas cai em "outras", parte da ART pode
                    # estar lá dentro e o denominador estaria subestimado.
                    cobertura={"base": float(d.loc[d["fonte"] != "OUTRA",
                                                   "valor"].sum()),
                               "universo": float(d["valor"].sum()),
                               "o_que": "a receita atribuída a alguma fonte "
                                        "conhecida — ART não classificada "
                                        "subestimaria o denominador desta razão"},
                    titulo=(f"A receita de ART cresce {cagr_art:.1%} ao ano e o "
                            f"gasto com fiscalização, {cagr_fisc:.1%}"),
                    descricao=(
                        "Cada ART registrada é uma obra ou serviço técnico que "
                        "passa a existir para o conselho fiscalizar. A razão "
                        "entre o que se gasta fiscalizando e o que se arrecada "
                        "de ART mede **cobertura**: quantos centavos de "
                        "fiscalização o conselho aplica por real de atividade "
                        "registrada.\n\n"
                        + ("A razão CAIU no período: entrou mais engenharia "
                           "para fiscalizar do que fiscalização para "
                           "acompanhá-la."
                           if caiu else
                           "A razão se manteve ou subiu no período.")
                        + " Não existe patamar legalmente correto — o que "
                        "importa é a direção, e ela é medida com dados que o "
                        "próprio conselho publica."),
                    evidencia=comp.round(2).reset_index(),
                    serie=comp[["centavos de fiscalização por R$ de ART"]],
                    grafico="line", unidade="centavos",
                    tabela_anual=comp.round(2).reset_index(),
                    base_legal="Lei 6.496/1977 e Lei 6.994/1982 (ART); Lei "
                               "5.194/1966, art. 34; Lei 12.514/2011.",
                    como_verificar=("Pedir o número de ARTs registradas por ano "
                                    "e o número de fiscalizações realizadas — "
                                    "com as duas contagens, a razão deixa de "
                                    "ser proxy e vira medida direta."),
                    metricas={"cagr_art": None if cagr_art != cagr_art else round(cagr_art, 4),
                              "cagr_fiscalizacao": (None if cagr_fisc != cagr_fisc
                                                    else round(cagr_fisc, 4)),
                              "cobertura_inicial":
                                  float(comp["centavos de fiscalização por R$ de ART"].iloc[0]),
                              "cobertura_final":
                                  float(comp["centavos de fiscalização por R$ de ART"].iloc[-1])},
                ))

    # ------------------------------------------------ 3. multa como receita
    multa = d[d["fonte"] == "MULTA_FISCALIZACAO"].groupby("_ano")["valor"].sum()
    if len(multa) >= 4 and float(multa.sum()) > 0:
        anuid = d[d["fonte"] == "ANUIDADE"].groupby("_ano")["valor"].sum()
        comp = pd.DataFrame({"multa de fiscalização": multa,
                             "anuidade": anuid}).fillna(0)
        comp = comp[comp["anuidade"] > 0]
        if not comp.empty:
            comp["% da anuidade"] = (100 * comp["multa de fiscalização"] /
                                     comp["anuidade"]).round(2)
            cagr_m = A.cagr(comp["multa de fiscalização"])
            cagr_a = A.cagr(comp["anuidade"])
            achados.append(Finding(
                id="receita-multa-vs-anuidade", frente=FRENTE,
                gravidade="media" if (cagr_m == cagr_m and cagr_a == cagr_a
                                      and cagr_m > cagr_a) else "info",
                titulo=(f"Multa de fiscalização cresce {cagr_m:.1%} ao ano; "
                        f"anuidade, {cagr_a:.1%}"),
                descricao=(
                    "A multa tem natureza dupla e por isso precisa de leitura "
                    "cuidadosa: é **produto** da fiscalização (autuar gera "
                    "multa) e é **incentivo** (arrecadar por autuação). Crescer "
                    "mais rápido que a anuidade não é irregularidade — pode ser "
                    "fiscalização funcionando. Vira pergunta quando cresce "
                    "**sem** que o gasto com fiscalização cresça junto: aí a "
                    "autuação virou fonte de receita sem o método que a "
                    "justificaria."),
                evidencia=comp.round(2).reset_index(),
                serie=comp[["% da anuidade"]], grafico="line", unidade="%",
                tabela_anual=comp.round(2).reset_index(),
                base_legal="Lei 5.194/1966, arts. 71 a 73 (infrações e multas); "
                           "Lei 12.514/2011.",
                metricas={"cagr_multa": None if cagr_m != cagr_m else round(cagr_m, 4),
                          "cagr_anuidade": None if cagr_a != cagr_a else round(cagr_a, 4)},
            ))

    # ------------------------------------------------ 4. quem sustenta o órgão
    anuid_t = float(d.loc[d["fonte"] == "ANUIDADE", "valor"].sum())
    if total > 0 and anuid_t > 0:
        pct_anuid = 100 * anuid_t / total
        div = float(d.loc[d["fonte"] == "DIVIDA_ATIVA", "valor"].sum())
        achados.append(Finding(
            id="receita-dependencia-anuidade", frente=FRENTE, gravidade="info",
            titulo=(f"{pct_anuid:.0f}% da receita vem da anuidade compulsória "
                    f"paga pelos profissionais"),
            cobertura={"base": total - float(d.loc[d["fonte"] == "OUTRA",
                                                   "valor"].sum()),
                       "universo": total,
                       "o_que": "as contas de receita atribuídas a uma fonte "
                                "conhecida — o percentual acima muda se o "
                                "resíduo for reclassificado"},
            descricao=(
                "A anuidade é cobrada por lei de quem precisa do registro para "
                "exercer a profissão — não é preço de serviço escolhido. Isso "
                "define a quem o conselho deve prestação de contas: ao "
                "profissional que paga, não ao mercado. Quanto maior a "
                "dependência, mais forte o dever de demonstrar que o dinheiro "
                "volta como fiscalização."
                + (f" A dívida ativa somou {fmt_brl(div)} no período, o que "
                   "mede inadimplência cobrada judicialmente." if div else "")),
            evidencia=por_fonte,
            metricas={"pct_anuidade": round(pct_anuid, 1),
                      "anuidade": round(anuid_t, 2),
                      "divida_ativa": round(div, 2)},
        ))
    return achados
