"""Frente 4 — Contratos e licitações: aditivos, vigência, cross-check PNCP."""
from __future__ import annotations

import pandas as pd

from .. import analytics as A
from .. import config
from ..findings import Finding
from ..utils import fmt_brl
from .common import df

FRENTE = "Contratos e licitações"


def _valor_implausivel(ctr: pd.DataFrame) -> pd.DataFrame:
    """Contratos cujo VALOR não sobrevive a um teste de ordem de grandeza.

    Um único registro errado contamina toda soma de carteira. Na rodada real,
    C-010/2025 publicou R$ 345.011.843,00 para 180 dias — 4,9× o segundo maior
    contrato e R$ 1,9 milhão POR DIA de vigência, contra R$ 96 mil do segundo
    colocado nesse critério. Não afirmamos que é erro: afirmamos que o valor
    não é comparável aos demais e não pode entrar nas somas sem ressalva.
    """
    if ctr.empty or "VALOR" not in ctr:
        return pd.DataFrame()
    v = pd.to_numeric(ctr["VALOR"], errors="coerce").fillna(0)
    d = ctr[v > 0].copy()
    if len(d) < 20:
        return pd.DataFrame()
    d["_valor"] = pd.to_numeric(d["VALOR"], errors="coerce")
    ordenado = d["_valor"].sort_values(ascending=False)
    segundo = float(ordenado.iloc[1])
    d["_dias"] = 1.0
    if {"INI", "FIM"} <= set(d.columns):
        dias = (pd.to_datetime(d["FIM"], errors="coerce") -
                pd.to_datetime(d["INI"], errors="coerce")).dt.days
        d["_dias"] = dias.where(dias > 0, 1.0)
    d["valor_por_dia"] = (d["_valor"] / d["_dias"]).round(2)
    mediana_dia = float(d["valor_por_dia"].median())
    suspeitos = d[(d["_valor"] > 4 * segundo) |
                  (d["valor_por_dia"] > 10 * mediana_dia * 20)]
    return suspeitos


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    ctr = df(staged, "contratos")
    adt = df(staged, "aditivos")
    pg = df(staged, "pagamentos")
    pncp = df(staged, "pncp_contratos")

    # ---------------------------------------- sanidade de valor, ANTES de somar
    implausiveis = _valor_implausivel(ctr)
    if not implausiveis.empty:
        cols = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                            "valor_por_dia", "DATA_INICIO_VIGENCIA",
                            "DATA_FINAL_VIGENCIA", "OBJETO")
                if c in implausiveis]
        excluido = float(implausiveis["VALOR"].sum())
        carteira = float(pd.to_numeric(ctr["VALOR"], errors="coerce").fillna(0).sum())
        achados.append(Finding(
            id="contratos-valor-implausivel", frente=FRENTE, gravidade="alta",
            titulo=(f"{len(implausiveis)} contrato(s) com valor fora de ordem de "
                    f"grandeza — {fmt_brl(excluido)} de "
                    f"{fmt_brl(carteira)} da carteira"),
            descricao=(
                "Contratos cujo valor publicado é várias vezes maior que o "
                "segundo maior da carteira, ou cujo valor por dia de vigência "
                "destoa por ordens de grandeza. **Não estamos dizendo que é "
                "erro de digitação** — estamos dizendo que o número não é "
                "comparável aos demais e que somá-lo à carteira sem ressalva "
                "distorce todos os totais. A divergência é, ela própria, o "
                "pedido: o extrato do contrato e os empenhos correspondentes "
                "resolvem em uma consulta."),
            evidencia=implausiveis[cols].sort_values("VALOR", ascending=False),
            metricas={"valor_excluido": round(excluido, 2),
                      "carteira_bruta": round(carteira, 2),
                      "carteira_sem_implausiveis": round(carteira - excluido, 2)},
            como_verificar="Pedir o extrato do contrato e a relação de empenhos "
                           "emitidos contra ele; comparar com o total "
                           "efetivamente pago à contratada no período.",
        ))
        ctr = ctr[~ctr.index.isin(implausiveis.index)]

    # -------------------------------------------------- aditivos acima do limite
    if not ctr.empty and not adt.empty and "NUMERO_CONTRATO" in adt:
        # Aditivo de valor zero é prorrogação de prazo, não acréscimo — misturar
        # os dois inflava o achado na primeira rodada.
        adt_valor = adt[adt["VALOR"] > 0]
        adt_prazo = adt[adt["VALOR"] <= 0]
        soma_adt = (adt_valor.groupby("NUMERO_CONTRATO")["VALOR"]
                    .agg(total_aditivos="sum", n_aditivos="size"))
        base = ctr.set_index("NUMERO_CONTRATO").join(soma_adt).reset_index()
        base["total_aditivos"] = base["total_aditivos"].fillna(0)
        base["n_aditivos"] = base["n_aditivos"].fillna(0).astype(int)
        base["pct_aditivo"] = (100 * base["total_aditivos"] /
                               base["VALOR"].replace(0, pd.NA)).fillna(0).round(1)

        # SEMÂNTICA INDETERMINADA: quando o único aditivo registrado tem valor
        # IGUAL ao valor do contrato, ao centavo, o campo quase certamente
        # carrega o valor GLOBAL revisado e não o incremento. Na rodada real
        # eram 59 de 79 linhas e R$ 417,8 mi de R$ 464,6 mi — 90% do achado.
        # Tratar isso como acréscimo de 100% é acusar com base em ambiguidade
        # de cadastro.
        base["_indeterminado"] = ((base["n_aditivos"] == 1) &
                                  ((base["total_aditivos"] - base["VALOR"]).abs()
                                   < 0.005) & (base["VALOR"] > 0))

        # Contratos de valor irrisório fazem o percentual explodir sem que isso
        # signifique irregularidade relevante — separados como ruído de base.
        relevantes = base[(base["VALOR"] >= config.PISO_CONTRATO_ADITIVO) &
                          (~base["_indeterminado"])]
        indeterminados = base[base["_indeterminado"]]
        if not indeterminados.empty:
            cols_i = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                  "total_aditivos", "DATA_INICIO_VIGENCIA")
                      if c in indeterminados]
            achados.append(Finding(
                id="contratos-aditivo-indeterminado", frente=FRENTE,
                gravidade="media",
                titulo=(f"{len(indeterminados)} contrato(s) cujo aditivo tem "
                        "valor idêntico ao do contrato — o campo é ambíguo"),
                descricao=(
                    "O portal publica o aditivo com valor exatamente igual ao "
                    "do contrato, ao centavo, e um único registro. Isso admite "
                    "duas leituras opostas: acréscimo de 100% do valor "
                    "original, ou simples repetição do valor global revisado. "
                    "Não dá para escolher pelos dados, e a diferença entre as "
                    "duas leituras é de "
                    f"{fmt_brl(float(indeterminados['total_aditivos'].sum()))}. "
                    "Estes contratos ficam FORA do achado de acréscimo acima "
                    "do limite, e a ambiguidade vira um pedido: basta ler um "
                    "termo aditivo para resolver todos."),
                evidencia=indeterminados[cols_i].sort_values(
                    "VALOR", ascending=False),
                metricas={"contratos": int(len(indeterminados)),
                          "valor_em_disputa":
                              round(float(indeterminados["total_aditivos"].sum()), 2)},
                como_verificar="Pedir UM termo aditivo desta lista e verificar "
                               "se ele diz 'acréscimo de R$ X' ou 'valor "
                               "global revisado para R$ X'.",
            ))
        estourados = relevantes[relevantes["pct_aditivo"] > config.ADITIVO_LIMITE_PCT]
        if not estourados.empty:
            acrescimo = float(estourados["total_aditivos"].sum())
            cols = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                "total_aditivos", "n_aditivos", "pct_aditivo",
                                "DATA_INICIO_VIGENCIA") if c in estourados]
            serie = pd.DataFrame()
            if "INI" in estourados:
                serie = A.serie_anual(
                    estourados.assign(_ano=estourados["INI"].dt.year),
                    "total_aditivos").rename(
                        columns={"total_aditivos": "acréscimo por aditivo"})
            achados.append(Finding(
                id="contratos-aditivos-limite", frente=FRENTE, gravidade="alta",
                titulo=f"{len(estourados)} contrato(s) com acréscimo por aditivo "
                       f"acima de {config.ADITIVO_LIMITE_PCT:.0f}% — "
                       f"{fmt_brl(acrescimo)} somados",
                descricao=(
                    "Acréscimo acumulado de VALOR (aditivos de prazo puro foram "
                    "excluídos) sobre contratos de pelo menos "
                    f"{fmt_brl(config.PISO_CONTRATO_ADITIVO)}. Percentuais muito "
                    "acima de 100% podem indicar também erro de cadastro no "
                    "portal (aditivo lançado com o valor total do contrato em vez "
                    "do incremento) — conferir o termo aditivo antes de concluir."),
                evidencia=estourados[cols].sort_values("pct_aditivo", ascending=False),
                serie=serie, grafico="bar",
                base_legal="Lei 8.666/1993, art. 65, §1º e Lei 14.133/2021, "
                           "art. 125 — acréscimo limitado a 25% (50% para reforma "
                           "de edifício ou equipamento).",
                como_verificar="Solicitar os termos aditivos dos contratos listados "
                               "e verificar se o valor lançado é incremento ou "
                               "valor global revisado, e qual a justificativa técnica.",
                metricas={"acrescimo_total": round(acrescimo, 2),
                          "contratos_ignorados_valor_irrisorio":
                              int(len(base) - len(relevantes))},
            ))
        if not adt_prazo.empty:
            achados.append(Finding(
                id="contratos-prorrogacoes", frente=FRENTE, gravidade="info",
                titulo=f"{len(adt_prazo)} aditivo(s) sem acréscimo de valor "
                       "(prorrogação de prazo)",
                descricao="Separados do achado anterior. Prorrogações sucessivas do "
                          "mesmo contrato indicam serviço contínuo renovado em vez "
                          "de nova licitação — legítimo até o limite legal de "
                          "prazo, mas vale acompanhar a contagem.",
                evidencia=(adt_prazo.groupby("NUMERO_CONTRATO").size()
                           .sort_values(ascending=False)
                           .to_frame("prorrogacoes").reset_index().head(50)),
            ))

    # -------------------------------------------------- pagamento após vigência
    if not ctr.empty and not pg.empty and "FIM" in ctr and "FAVORECIDO_NORM" in pg:
        fim_por_contratada = ctr.dropna(subset=["FIM"]).groupby("CONTRATADA_NORM")["FIM"].max()
        pj = pg[~pg.get("PF", False).astype(bool)].dropna(subset=["DATA"])
        m = pj.join(fim_por_contratada.rename("FIM_VIGENCIA"), on="FAVORECIDO_NORM")
        tarde = m[m["DATA"] > m["FIM_VIGENCIA"] + pd.Timedelta(days=60)]
        if not tarde.empty:
            resumo = (tarde.groupby("FAVORECIDO_NORM")
                      .agg(pagamentos=("VALOR", "size"), total=("VALOR", "sum"),
                           fim_vigencia=("FIM_VIGENCIA", "max"),
                           ultimo_pagamento=("DATA", "max")).reset_index()
                      .sort_values("total", ascending=False))
            achados.append(Finding(
                id="contratos-pagamento-pos-vigencia", frente=FRENTE, gravidade="media",
                titulo=f"{len(resumo)} contratada(s) recebendo mais de 60 dias após o "
                       "fim da última vigência conhecida",
                descricao="Pagamento sem cobertura contratual vigente (ou contrato não "
                          "publicado no portal). Aproximação por nome da contratada — "
                          "conferir caso a caso.",
                evidencia=resumo.head(50),
            ))

    # -------------------------------------------------- contratos sem licitação
    if not ctr.empty and "NUMERO_LICITACAO" in ctr:
        sem_lic = ctr[ctr["NUMERO_LICITACAO"].isna() |
                      (ctr["NUMERO_LICITACAO"].astype(str).str.strip().isin(["", "None", "nan"]))]
        if not sem_lic.empty:
            tot = float(sem_lic["VALOR"].sum())
            taxa = 100 * len(sem_lic) / len(ctr)
            lic = df(staged, "licitacoes")
            # Um check que aciona quase todo o universo mudou de natureza: não
            # está apontando contratações diretas, está apontando que o campo
            # nunca é preenchido. Na rodada real foram 965 de 965 — e o
            # dataset de licitações veio com zero linhas.
            if taxa >= 95:
                achados.append(Finding(
                    id="contratos-vinculo-certame-ausente", frente=FRENTE,
                    gravidade="alta",
                    titulo=(f"{taxa:.1f}% dos contratos ({len(sem_lic)} de "
                            f"{len(ctr)}) não informam a licitação de origem"
                            + (" e o rol de licitações não é publicado"
                               if lic.empty else "")),
                    descricao=(
                        "Quando o campo falta em praticamente todos os "
                        "registros, ele não distingue contratação direta de "
                        "licitada — simplesmente não é preenchido. Isso não é "
                        "uma lista de 965 irregularidades: é **um** achado de "
                        "transparência, e mais grave que a lista, porque torna "
                        "impossível saber, a partir dos dados abertos, qual "
                        "contratação foi licitada e qual foi direta. "
                        + ("O dataset de licitações também voltou vazio, o que "
                           "fecha as duas portas de verificação."
                           if lic.empty else "")
                        + f" A carteira afetada soma {fmt_brl(tot)} — que "
                        "**não** deve ser lida como 'contratos sem licitação', "
                        "e sim como 'contratos cuja forma de contratação o "
                        "portal não informa'."),
                    evidencia=sem_lic[["NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                       "DATA_INICIO_VIGENCIA"]]
                    .sort_values("VALOR", ascending=False).head(50),
                    metricas={"taxa_pct": round(taxa, 1),
                              "contratos": int(len(sem_lic)),
                              "universo": int(len(ctr)),
                              "valor_total": round(tot, 2),
                              "licitacoes_coletadas": int(len(lic))},
                    base_legal="Lei 14.133/2021, arts. 94 e 174 (divulgação no "
                               "PNCP como condição de eficácia do contrato); "
                               "Lei 12.527/2011, art. 8º, §1º, IV.",
                    como_verificar="Pedir a relação de licitações do período e "
                                   "o vínculo contrato ↔ certame.",
                ))
            else:
                achados.append(Finding(
                    id="contratos-sem-licitacao", frente=FRENTE,
                    gravidade="media" if tot > 1_000_000 else "baixa",
                    titulo=f"{len(sem_lic)} contrato(s) sem número de licitação "
                           f"vinculado ({taxa:.0f}% da carteira)",
                    descricao="Contratação direta (dispensa/inexigibilidade) ou "
                              "falha de registro. Ranqueado por valor.",
                    evidencia=sem_lic[["NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                       "DATA_INICIO_VIGENCIA", "DATA_FINAL_VIGENCIA"]]
                    .sort_values("VALOR", ascending=False).head(100),
                    metricas={"valor_total": round(tot, 2),
                              "taxa_pct": round(taxa, 1)},
                ))

    # -------------------------------------------------- cross-check Implanta × PNCP
    if not pncp.empty:
        alvo = pncp[pncp.get("orgao_papel") == "alvo"]
        if not ctr.empty and not alvo.empty:
            nomes_portal = set(ctr["CONTRATADA_NORM"].dropna())
            nomes_pncp = set(alvo["fornecedor_norm"].dropna())
            so_portal = sorted(nomes_portal - nomes_pncp)
            so_pncp = sorted(nomes_pncp - nomes_portal)
            ctr_pos21 = ctr[ctr["INI"].dt.year >= 2021] if "INI" in ctr else ctr
            faltando = ctr_pos21[ctr_pos21["CONTRATADA_NORM"].isin(so_portal)]
            if not faltando.empty:
                achados.append(Finding(
                    id="contratos-ausentes-pncp", frente=FRENTE, gravidade="media",
                    titulo=f"{faltando['CONTRATADA_NORM'].nunique()} contratada(s) do "
                           "portal (pós-2021) sem contrato correspondente no PNCP",
                    descricao="A Lei 14.133 obriga a publicação no PNCP; ausência é "
                              "falha de transparência (comparação por nome do "
                              "fornecedor — aproximada).",
                    evidencia=faltando[["NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                        "DATA_INICIO_VIGENCIA"]].sort_values(
                        "VALOR", ascending=False).head(100),
                ))
            if so_pncp:
                achados.append(Finding(
                    id="contratos-so-no-pncp", frente=FRENTE, gravidade="baixa",
                    titulo=f"{len(so_pncp)} fornecedor(es) com contrato no PNCP e não "
                           "no portal da transparência",
                    descricao="Situação inversa: contrato publicado nacionalmente mas "
                              "invisível no portal próprio do conselho.",
                    evidencia=alvo[alvo["fornecedor_norm"].isin(so_pncp)][
                        ["numeroContrato", "fornecedor_nome", "objeto", "valorGlobal",
                         "vigenciaInicio", "vigenciaFim"]].head(100),
                ))
        # valor acumulado muito acima do global
        infl = alvo[(alvo["valorAcumulado"] > 0) &
                    (alvo["valorAcumulado"] > alvo["valorGlobal"] * (1 + config.ADITIVO_LIMITE_PCT / 100))]
        if not infl.empty:
            achados.append(Finding(
                id="contratos-pncp-acumulado", frente=FRENTE, gravidade="media",
                titulo=f"{len(infl)} contrato(s) no PNCP com valor acumulado "
                       f">{config.ADITIVO_LIMITE_PCT:.0f}% acima do valor global",
                descricao="Indicativo de aditivos sucessivos registrados no PNCP.",
                evidencia=infl[["numeroContrato", "fornecedor_nome", "objeto",
                                "valorGlobal", "valorAcumulado"]],
            ))
    return achados
