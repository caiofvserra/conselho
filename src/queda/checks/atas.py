"""Frente — Deliberações dos colegiados (leitura por LLM).

As atas são a única fonte que liga o dinheiro a uma DECISÃO: quem propôs, quem
aprovou, com que justificativa. Mas vêm como texto corrido, e é por isso que
ninguém as cruza com a despesa.

Aqui o LLM faz o único trabalho que uma regra não faz — ler prosa e extrair
estrutura — e nada além disso: não calcula valor, não julga legalidade, não
decide gravidade. O que ele devolve (tipo da decisão, beneficiário, valor
mencionado) volta para o mundo determinístico e é cruzado com os pagamentos
por nome normalizado. Sem `QUEDA_ANTHROPIC_API_KEY` a frente sai um aviso e
nada mais — nenhum número deste relatório depende dela.
"""
from __future__ import annotations

import pandas as pd

from .. import analytics as A
from .. import llm
from ..findings import Finding
from ..utils import fmt_brl, norm_nome
from .common import df

FRENTE = "Deliberações dos colegiados"

MAX_ATAS = 200


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    atas = df(staged, "atas")
    if atas.empty:
        return achados

    if not llm.disponivel():
        achados.append(Finding(
            id="atas-llm-desligado", frente=FRENTE, gravidade="info",
            titulo=f"{len(atas)} ata(s) coletadas, mas a leitura por LLM está "
                   "desligada",
            descricao=(
                "As deliberações são texto corrido: sem um leitor de linguagem "
                "não há como extrair quem decidiu o quê e cruzar com a despesa. "
                "Defina `QUEDA_ANTHROPIC_API_KEY` (e instale `anthropic`) para "
                "ligar esta frente. O restante do relatório não depende dela."),
            evidencia=(atas.groupby("_ano").size().to_frame("atas").reset_index()
                       if "_ano" in atas else pd.DataFrame()),
        ))
        return achados

    decisoes = llm.extrair_decisoes_atas(
        atas.to_dict("records"), limite=MAX_ATAS)
    if not decisoes:
        return achados

    d = pd.DataFrame(decisoes)
    d["beneficiario_norm"] = d.get(
        "beneficiario", pd.Series("", index=d.index)).fillna("").map(norm_nome)

    por_tipo = (d.groupby(["_ano", "tipo"]).size().reset_index(name="decisoes")
                if "_ano" in d else pd.DataFrame())
    serie = (A.serie_anual(d.assign(_um=1), "_um", by="tipo")
             if "_ano" in d else pd.DataFrame())
    achados.append(Finding(
        id="atas-decisoes", frente=FRENTE, gravidade="info",
        titulo=f"{len(d)} decisão(ões) com efeito financeiro extraídas de "
               f"{min(len(atas), MAX_ATAS)} ata(s)",
        descricao=(
            "Deliberações que envolvem dinheiro — convênios, contratos, "
            "despesas, verbas e cargos — estruturadas a partir do texto das "
            "atas. A extração é literal: o modelo foi instruído a não inferir "
            "valor nem nome que não estejam escritos. Trate como índice para "
            "leitura das atas originais, não como fato apurado."),
        evidencia=d.head(300),
        serie=serie, grafico="bar_stacked", unidade="un",
        tabela_anual=por_tipo,
    ))

    # ---- decisão que a própria ata sinaliza como conflituosa
    conflito = d[d.get("indicio_conflito", False).astype(bool)] if \
        "indicio_conflito" in d else pd.DataFrame()
    if not conflito.empty:
        achados.append(Finding(
            id="atas-indicio-conflito", frente=FRENTE, gravidade="alta",
            titulo=f"{len(conflito)} deliberação(ões) em que o próprio texto da "
                   "ata sugere relação entre quem decide e quem se beneficia",
            descricao=(
                "Casos em que a leitura da ata identificou, **no texto**, "
                "vínculo entre o colegiado e o beneficiário da decisão — "
                "declaração de impedimento, menção a entidade de que o "
                "conselheiro participa, aprovação de repasse à própria "
                "categoria. Confirme lendo a ata citada antes de qualquer "
                "afirmação: a extração é automática."),
            evidencia=conflito.head(100),
            base_legal="Lei 9.784/1999, arts. 18 a 21 (impedimento e suspeição); "
                       "Lei 14.133/2021, art. 9º, III; CF, art. 37 "
                       "(impessoalidade e moralidade).",
            como_verificar="Abrir a ata pelo número indicado e conferir o "
                           "registro de declaração de impedimento e o quórum de "
                           "votação.",
        ))

    # ---- cruzamento com o dinheiro efetivamente pago
    pg = df(staged, "pagamentos")
    if not pg.empty and "FAVORECIDO_NORM" in pg:
        nomes = set(d.loc[d["beneficiario_norm"].str.len() > 6,
                          "beneficiario_norm"])
        if nomes:
            casados = pg[pg["FAVORECIDO_NORM"].isin(nomes)]
            if not casados.empty:
                resumo = (casados.groupby(["_ano", "FAVORECIDO_NORM"])["VALOR"]
                          .sum().reset_index()
                          .sort_values("VALOR", ascending=False))
                total = float(casados["VALOR"].sum())
                achados.append(Finding(
                    id="atas-decisao-x-pagamento", frente=FRENTE,
                    gravidade="media",
                    titulo=f"{fmt_brl(total)} pagos a beneficiários nomeados em "
                           "deliberações de colegiado",
                    descricao=(
                        "Ligação direta entre a decisão e o desembolso: nomes "
                        "que aparecem nas atas como beneficiários e que "
                        "efetivamente receberam pagamento. É o par "
                        "decisão↔dinheiro que permite pedir o processo "
                        "administrativo completo de cada caso."),
                    evidencia=resumo.head(200),
                    serie=A.serie_anual(casados, "VALOR"),
                    grafico="bar",
                    tabela_anual=A.ranking_anual(casados, "FAVORECIDO_NORM",
                                                 "VALOR", top=10),
                    como_verificar="Pedir o processo administrativo referente à "
                                   "ata e ao empenho correspondente.",
                ))
    return achados
