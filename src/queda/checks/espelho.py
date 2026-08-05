"""Frente — O conselho medido pela régua que ele aplica nos outros.

O Crea existe para exigir ART e responsável técnico de quem contrata obra e
serviço de engenharia (Lei 6.496/1977, art. 1º — **sem dispensa por valor ou
porte**). A pergunta espelhada é a mais legítima que se pode fazer a um órgão
de fiscalização: *quando ele próprio contrata engenharia, cumpre o rito que
cobra?*

O que torna a pergunta respondível é um campo que o portal do próprio Crea não
publica e o PNCP sim: `categoriaProcesso`. Ao publicar no PNCP, o órgão
classifica cada contratação na origem — 7 obras, 8 serviços, 9 serviços de
engenharia. As categorias 7 e 9 são exatamente aquelas em que a ART é
obrigatória.

O segundo campo é `numeroControlePNCPCompra`: o contrato não expõe edital nem
termo de referência, mas a COMPRA de origem sim. É por ele que se chega aos
documentos do certame — e o portal do Crea não publica um único número de
licitação nos 965 contratos que registra.

Limite honesto desta frente: o PNCP só cobre contratações a partir de agosto
de 2021 (Lei 14.133/2021), e a categoria é auto-declarada pelo órgão. Um
contrato de engenharia classificado como "serviços" pelo próprio órgão é, ele
mesmo, o achado.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from ..findings import Finding
from ..utils import coluna, fmt_brl
from .common import df

FRENTE = "O conselho pela própria régua"

# Objeto com cara de engenharia, para confrontar com a categoria declarada.
# Escrito a partir das atividades privativas (Lei 5.194/1966, art. 7º e
# Resolução Confea 1.073/2016) e das modalidades da Resolução 218/1973.
RX_ENGENHARIA = re.compile(
    r"\bobra\b|\bobras\b|constru|reforma|amplia|demoli|pavimenta|recapea|"
    r"drenagem|terraplan|urbaniza|impermeabiliza|pintura predial|"
    r"telhado|cobertura|alvenaria|estrutur|funda[çc][ãa]o|muro de arrimo|"
    r"instala[çc][ãa]o el[ée]trica|instala[çc][ãa]o hidr|subesta[çc][ãa]o|"
    r"ar[- ]condicionado|climatiza|refrigera[çc][ãa]o|eleva[dt]or|"
    r"projeto (b[áa]sico|executivo|el[ée]trico|hidr[áa]ulico|estrutural)|"
    r"laudo t[ée]cnico|memorial descritivo|sondagem|topografi|"
    r"georreferencia|manuten[çc][ãa]o predial|manuten[çc][ãa]o civil|"
    r"combate a inc[êe]ndio|spda|para[- ]raios|manejo arb[óo]reo",
    re.IGNORECASE)


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    pncp = df(staged, "pncp_contratos")
    if pncp.empty:
        return achados
    alvo = pncp[coluna(pncp, "orgao_papel") == "alvo"].copy()
    if alvo.empty:
        return achados
    if "categoria" not in alvo.columns:
        return achados

    alvo["valor"] = pd.to_numeric(coluna(alvo, "valorGlobal", 0),
                                  errors="coerce").fillna(0)
    total = float(alvo["valor"].sum())

    # ------------------------------------------------ 1. o que o órgão declara
    por_cat = (alvo.groupby("categoria")
               .agg(contratos=("valor", "size"), valor=("valor", "sum"))
               .reset_index().sort_values("valor", ascending=False))
    por_cat["share_pct"] = (100 * por_cat["valor"] / total).round(2) if total else 0
    eng = alvo[alvo["exige_art"].astype(bool)] if "exige_art" in alvo else pd.DataFrame()
    val_eng = float(eng["valor"].sum()) if not eng.empty else 0.0
    achados.append(Finding(
        id="espelho-categoria-declarada", frente=FRENTE,
        gravidade="alta" if not eng.empty else "info",
        titulo=(f"{len(eng)} contrato(s) que o próprio conselho classificou como "
                f"obra ou serviço de engenharia — {fmt_brl(val_eng)}"),
        descricao=(
            "Ao publicar no PNCP, o órgão classifica cada contratação na "
            "origem. As categorias **obras** e **serviços de engenharia** são "
            "exatamente aquelas em que a ART é obrigatória, sem dispensa por "
            "valor ou porte — a mesma exigência que o Crea fiscaliza nos "
            "outros. Cada um destes contratos deveria ter responsável técnico "
            "e ART registrada no próprio conselho.\n\n"
            "Esta é a lista de partida para a pergunta espelhada: o órgão que "
            "autua quem contrata engenharia sem ART cumpriu o rito nas suas "
            "próprias contratações?"),
        evidencia=por_cat,
        serie=(A.serie_anual(alvo, "valor", by="categoria")
               if "_ano" in alvo else pd.DataFrame()),
        grafico="bar_stacked",
        tabela_anual=(A.ranking_anual(alvo, "categoria", "valor", top=5)
                      if "_ano" in alvo else pd.DataFrame()),
        base_legal=("Lei 6.496/1977, art. 1º (ART obrigatória em todo contrato "
                    "de obra ou serviço de engenharia); Lei 5.194/1966, art. "
                    "7º; Lei 14.133/2021, art. 6º, XII e XXI; Resolução Confea "
                    "1.025/2009."),
        como_verificar=("Consultar no próprio sistema do Crea-SP se existe ART "
                        "registrada para cada um destes contratos, com o "
                        "responsável técnico indicado."),
        metricas={"contratos_engenharia": int(len(eng)),
                  "valor_engenharia": round(val_eng, 2),
                  "valor_total": round(total, 2),
                  "share_engenharia_pct": round(100 * val_eng / total, 2) if total else 0},
    ))

    # ------------------------------------------------ 2. objeto × categoria
    obj = coluna(alvo, "objeto").fillna("").astype(str)
    parece_eng = obj.str.contains(RX_ENGENHARIA, regex=True, na=False)
    declarada_eng = alvo["exige_art"].astype(bool) if "exige_art" in alvo else False
    subenquadrado = alvo[parece_eng & ~declarada_eng]
    if not subenquadrado.empty:
        val_sub = float(subenquadrado["valor"].sum())
        cols = [c for c in ("numeroContrato", "fornecedor_nome", "valor",
                            "categoria", "objeto", "dataAssinatura")
                if c in subenquadrado]
        achados.append(Finding(
            id="espelho-subenquadramento", frente=FRENTE, gravidade="alta",
            titulo=(f"{len(subenquadrado)} contrato(s) com objeto de engenharia "
                    f"classificados como serviço comum — {fmt_brl(val_sub)}"),
            descricao=(
                "O objeto descreve obra, reforma, instalação elétrica, projeto "
                "ou laudo técnico, mas o órgão classificou a contratação fora "
                "das categorias que exigem ART. Subenquadrar serviço de "
                "engenharia como serviço comum é a forma mais frequente de "
                "escapar do rito — e é precisamente a irregularidade que o "
                "Crea autua nos entes que fiscaliza.\n\n"
                "A classificação de origem é auto-declarada pelo próprio "
                "órgão: a divergência entre o que o objeto descreve e o que a "
                "categoria afirma é o achado, e ela não depende de nenhuma "
                "interpretação nossa sobre valor ou porte."),
            evidencia=subenquadrado[cols].sort_values("valor", ascending=False),
            serie=(A.serie_anual(subenquadrado, "valor")
                   if "_ano" in subenquadrado else pd.DataFrame()),
            grafico="bar",
            base_legal=("Lei 6.496/1977, art. 1º; Lei 14.133/2021, art. 6º, XXI "
                        "(serviço COMUM de engenharia continua sendo engenharia "
                        "e exige rito); Resolução Confea 1.073/2016."),
            como_verificar=("Pedir o termo de referência de cada contrato e "
                            "verificar se há exigência de responsável técnico, "
                            "ART e acervo técnico. Confrontar com o auto de "
                            "infração que o próprio Crea lavraria no caso."),
            metricas={"contratos": int(len(subenquadrado)),
                      "valor": round(val_sub, 2)},
        ))

    # ------------------------------------------------ 3. o certame que falta
    ctr_portal = df(staged, "contratos")
    if not ctr_portal.empty and "numeroControlePNCPCompra" in alvo.columns:
        com_compra = alvo[coluna(alvo, "numeroControlePNCPCompra")
                          .fillna("").astype(str).str.len() > 5]
        if not com_compra.empty:
            achados.append(Finding(
                id="espelho-certame-no-pncp", frente=FRENTE, gravidade="media",
                titulo=(f"{len(com_compra)} contrato(s) têm o certame de origem "
                        "identificado no PNCP — o portal do Crea não publica "
                        "nenhum"),
                descricao=(
                    "O portal de transparência do Crea-SP registra 965 "
                    "contratos e **nenhum** com número de licitação vinculado; "
                    "o rol de licitações também não é publicado. O PNCP, para "
                    "os contratos posteriores a agosto de 2021, traz o número "
                    "de controle da COMPRA de origem — e por ele se chega ao "
                    "edital, ao termo de referência e à modalidade.\n\n"
                    "Isso muda o enquadramento do achado de transparência: não "
                    "é que a informação não exista, é que o portal próprio não "
                    "a publica enquanto o órgão a declara em outro sistema. A "
                    "coluna `numeroControlePNCPCompra` é o caminho para os "
                    "documentos de cada certame."),
                evidencia=(com_compra[[c for c in
                                       ("numeroContrato", "fornecedor_nome",
                                        "valor", "categoria",
                                        "numeroControlePNCPCompra", "processo")
                                       if c in com_compra]]
                           .sort_values("valor", ascending=False).head(200)),
                base_legal=("Lei 14.133/2021, arts. 94 e 174; Lei 12.527/2011, "
                            "art. 8º, §1º, IV."),
                como_verificar=(
                    "Para cada número de controle, consultar "
                    "`pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/"
                    "{sequencial}/arquivos` e baixar edital e termo de "
                    "referência; verificar se exigem responsável técnico e ART."),
                metricas={"com_certame_identificado": int(len(com_compra)),
                          "contratos_no_portal": int(len(ctr_portal))},
            ))

    # ------------------------------------------------ 4. portal × PNCP
    if not ctr_portal.empty and "_ano" in alvo.columns:
        pos21 = ctr_portal[ctr_portal["INI"].dt.year >= 2021] \
            if "INI" in ctr_portal else ctr_portal
        n_portal, n_pncp = len(pos21), len(alvo)
        if n_portal and n_pncp:
            razao = n_pncp / n_portal
            if razao < 0.5 or razao > 2:
                achados.append(Finding(
                    id="espelho-divergencia-portal-pncp", frente=FRENTE,
                    gravidade="media",
                    titulo=(f"O portal registra {n_portal} contratos desde 2021 e "
                            f"o PNCP {n_pncp} — os dois sistemas não fecham"),
                    descricao=(
                        "A publicação no PNCP é condição de eficácia do "
                        "contrato (Lei 14.133/2021, art. 94). Divergência "
                        "grande entre o que o órgão publica no portal próprio e "
                        "o que declara no sistema nacional é falha de "
                        "transparência mensurável, e aponta qual dos dois "
                        "conjuntos está incompleto."),
                    evidencia=pd.DataFrame([
                        {"fonte": "portal do Crea-SP (desde 2021)", "contratos": n_portal},
                        {"fonte": "PNCP", "contratos": n_pncp},
                        {"fonte": "razão PNCP/portal", "contratos": round(razao, 2)}]),
                    base_legal="Lei 14.133/2021, arts. 94 e 174.",
                ))
    return achados
