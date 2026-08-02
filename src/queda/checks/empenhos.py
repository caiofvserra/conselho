"""Frente 3 — Empenhos e pagamentos: concentração, fracionamento, anulações."""
from __future__ import annotations

import pandas as pd

from .. import analytics as A
from .. import config
from ..findings import Finding
from ..utils import fmt_brl
from .common import df, fornecedores_mercado, transferencias_sistema

FRENTE = "Empenhos e pagamentos"


def _hhi(shares: pd.Series) -> float:
    p = shares / shares.sum()
    return float((p ** 2).sum())


def _anatomia_anulacoes(emp: pd.DataFrame):
    """Separa a anulação de rotina das três assinaturas suspeitas.

    Devolve (resumo_por_ano, empenhos_fantasma, reempenhos) ou None quando os
    campos necessários não vieram na coleta.
    """
    if "VALOR_ANULADO" not in emp.columns:
        return None
    d = emp.copy()
    for col in ("VALOR", "VALOR_ANULADO", "VALOR_PAGO"):
        d[col] = pd.to_numeric(d.get(col, 0), errors="coerce").fillna(0)

    # fantasma: anulado tudo (ou quase) e nada pago
    integral = d["VALOR_ANULADO"] >= d["VALOR"] * 0.999
    fantasmas = d[integral & (d["VALOR"] > 0) & (d["VALOR_PAGO"] <= 0)]

    # dezembro: anulação carimbada no último mês do exercício
    mes = (pd.to_datetime(d["DATA"], errors="coerce").dt.month
           if "DATA" in d.columns else pd.Series(pd.NA, index=d.index))
    d["_dez"] = mes.eq(12).fillna(False)

    resumo = d.groupby("_ano").apply(
        lambda g: pd.Series({
            "empenhado": float(g["VALOR"].sum()),
            "anulado": float(g["VALOR_ANULADO"].sum()),
            "fantasma": float(g.loc[integral.reindex(g.index, fill_value=False) &
                                    (g["VALOR_PAGO"] <= 0), "VALOR"].sum()),
            "anulado_dezembro": float(g.loc[g["_dez"], "VALOR_ANULADO"].sum()),
        }), include_groups=False) if not d.empty else pd.DataFrame()
    if resumo.empty:
        return None
    resumo["pct_fantasma"] = (100 * resumo["fantasma"] /
                              resumo["empenhado"].replace(0, pd.NA)).fillna(0).round(1)
    resumo["pct_anulado_em_dezembro"] = (
        100 * resumo["anulado_dezembro"] /
        resumo["anulado"].replace(0, pd.NA)).fillna(0).round(1)

    # reempenho: mesmo credor + mesma conta, anulado num ano e empenhado no seguinte
    reempenhos = pd.DataFrame()
    chave = [c for c in ("FAVORECIDO_NORM", "CONTA_EMPENHO") if c in d.columns]
    if chave and not fantasmas.empty:
        vivos = (d[d["VALOR_PAGO"] > 0]
                 .groupby(chave + ["_ano"])["VALOR"].sum().reset_index())
        alvo = fantasmas.groupby(chave + ["_ano"])["VALOR"].sum().reset_index()
        alvo["_ano_seguinte"] = alvo["_ano"] + 1
        reempenhos = alvo.merge(
            vivos, left_on=chave + ["_ano_seguinte"], right_on=chave + ["_ano"],
            suffixes=("_anulado", "_reempenhado"))
    return resumo.round(2), fantasmas, reempenhos


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    emp = df(staged, "empenhos")
    pg = df(staged, "pagamentos")

    # -------------------------------------------------- concentração por credor
    if not pg.empty:
        pj = fornecedores_mercado(staged)
        if not pj.empty:
            por_credor = (pj.groupby(["_ano", "FAVORECIDO_NORM"])["VALOR"].sum()
                          .reset_index())
            linhas = []
            for ano, g in por_credor.groupby("_ano"):
                tot = g["VALOR"].sum()
                if tot <= 0:
                    continue
                top10 = g.nlargest(10, "VALOR")
                linhas.append({"ano": int(ano), "total_pj": float(tot),
                               "share_top10": float(top10["VALOR"].sum() / tot),
                               "hhi": round(_hhi(g["VALOR"]), 4)})
            conc = pd.DataFrame(linhas)
            criticos = conc[conc["hhi"] > 0.15]
            achados.append(Finding(
                id="empenhos-concentracao", frente=FRENTE,
                gravidade="media" if not criticos.empty else "info",
                titulo="Concentração de pagamentos por fornecedor (HHI e top-10 por ano)",
                descricao="HHI > 0,15 indica mercado de fornecedores muito concentrado "
                          "para um órgão dessa dimensão — favorece direcionamento.",
                evidencia=conc,
                metricas={"anos_criticos": criticos["ano"].tolist() if not criticos.empty else []},
            ))
            rank = A.ranking_anual(pj, "FAVORECIDO_NORM", "VALOR", top=10)
            serie_top = pd.DataFrame()
            if not rank.empty:
                lideres = (rank.groupby("FAVORECIDO_NORM")["total"].sum()
                           .nlargest(6).index.tolist())
                serie_top = A.serie_anual(
                    pj[pj["FAVORECIDO_NORM"].isin(lideres)], "VALOR",
                    by="FAVORECIDO_NORM")
            achados.append(Finding(
                id="empenhos-top-fornecedores", frente=FRENTE, gravidade="info",
                titulo="Maiores fornecedores por ano",
                descricao="Top 10 de cada exercício. O gráfico acompanha os seis "
                          "maiores do período — mostra quando cada fornecedor "
                          "entrou, cresceu ou saiu.",
                evidencia=rank, tabela_anual=rank,
                serie=serie_top, grafico="bar_stacked",
            ))
            fixos = (rank.groupby("FAVORECIDO_NORM")["_ano"].nunique()
                     .sort_values(ascending=False))
            fixos = fixos[fixos >= max(int(0.7 * pj["_ano"].nunique()), 3)]
            if not fixos.empty:
                achados.append(Finding(
                    id="empenhos-fornecedores-cativos", frente=FRENTE,
                    gravidade="media",
                    titulo=f"{len(fixos)} fornecedor(es) permanecem no top-10 em "
                           "70% ou mais dos exercícios",
                    descricao="Permanência prolongada no topo, num órgão que deve "
                              "licitar periodicamente, sugere renovação sucessiva "
                              "de contrato ou baixa competição real.",
                    evidencia=fixos.to_frame("anos_no_top10").reset_index(),
                ))

    # -------------------------------------------------- fracionamento
    if not pg.empty and {"FAVORECIDO_NORM", "DATA", "VALOR"} <= set(pg.columns):
        pj = fornecedores_mercado(staged).dropna(subset=["DATA"]).copy()
        # Fornecedor com contrato formal registrado executa parcelas mensais —
        # isso não é fracionamento. Sem esse recorte, o achado listava jornais,
        # rádios e prestadores de serviço contínuo (413 na primeira rodada).
        ctr = df(staged, "contratos")
        com_contrato = set(ctr.get("CONTRATADA_NORM", pd.Series(dtype=str)).dropna())
        pj = pj[~pj["FAVORECIDO_NORM"].isin(com_contrato)].sort_values("DATA")

        conta_col = "CONTA_EMPENHO" if "CONTA_EMPENHO" in pj.columns else None
        suspeitas = []
        janela = pd.Timedelta(days=config.JANELA_FRACIONAMENTO_DIAS)
        chaves = ["FAVORECIDO_NORM"] + ([conta_col] if conta_col else [])
        for chave, g in pj.groupby(chaves):
            if len(g) < 3:
                continue
            datas = g["DATA"].reset_index(drop=True)
            vals = g["VALOR"].reset_index(drop=True)
            anos = g["_ano"].reset_index(drop=True)
            i = 0
            for j in range(len(datas)):
                while datas[j] - datas[i] > janela:
                    i += 1
                # O limite é o do exercício em que a janela se fecha; comparar
                # um pagamento de 2014 com o teto de hoje esconde o achado.
                limite = config.limite_dispensa(anos[j])
                trecho = vals[i:j + 1]
                pequenos = trecho[trecho < limite]
                n = int(len(pequenos))
                soma = float(pequenos.sum())
                if n >= 3 and soma > limite:
                    dias = (datas[j] - datas[i]).days
                    # cadência mensal regular = execução de contrato, não rajada
                    if n <= 4 and dias >= 60:
                        continue
                    nomes = chave if isinstance(chave, tuple) else (chave,)
                    suspeitas.append({
                        "fornecedor": nomes[0],
                        "conta": nomes[1] if len(nomes) > 1 else "",
                        "exercicio": int(anos[j]),
                        "limite_dispensa_no_ano": limite,
                        "inicio": datas[i].date(), "fim": datas[j].date(),
                        "dias": dias, "pagamentos": n,
                        "soma_janela": round(soma, 2),
                        "excedente": round(soma - limite, 2)})
                    break
        if suspeitas:
            ev = (pd.DataFrame(suspeitas)
                  .sort_values("soma_janela", ascending=False))
            por_ano = (ev.groupby("exercicio")
                       .agg(casos=("fornecedor", "size"),
                            soma=("soma_janela", "sum")))
            achados.append(Finding(
                id="empenhos-fracionamento", frente=FRENTE, gravidade="alta",
                titulo=f"{len(ev)} caso(s) de possível fracionamento de despesa",
                descricao=(
                    "≥3 pagamentos ao mesmo fornecedor, na mesma conta contábil, "
                    f"em até {config.JANELA_FRACIONAMENTO_DIAS} dias, cada um "
                    "abaixo do limite de dispensa **vigente naquele exercício** e "
                    "somando acima dele. O limite aplicado aparece em "
                    "`limite_dispensa_no_ano`: R$ 8.000 até 2017 (Lei 8.666, art. "
                    "24, II), R$ 17.600 de 2018 a 2020 (Decreto 9.412/2018), "
                    "R$ 50.000 a partir de 2021 (Lei 14.133, art. 75, II) e "
                    "R$ 59.906,02 de 2023 em diante (Decreto 11.317/2022) — "
                    "confira o valor dos exercícios mais recentes, atualizados "
                    "por decreto anual. Já excluídos: fornecedores com contrato "
                    "formal registrado e cadências mensais regulares."),
                evidencia=ev,
                serie=por_ano[["soma"]].rename(
                    columns={"soma": "valor fracionado"}),
                grafico="bar",
                tabela_anual=por_ano.reset_index(),
                base_legal="Lei 14.133/2021, art. 29 e art. 75, §1º (e, para os "
                           "exercícios anteriores, Lei 8.666/1993, art. 23, §5º) "
                           "— vedado fracionar despesa para fugir da modalidade "
                           "licitatória.",
                como_verificar="Pedir os processos de compra dos casos listados e "
                               "verificar se o objeto é o mesmo e se houve "
                               "planejamento anual da contratação.",
                metricas={"casos": int(len(ev)),
                          "soma_total": round(float(ev["soma_janela"].sum()), 2)},
            ))

    # -------------------------------------------------- transferências ao sistema
    transf = transferencias_sistema(staged)
    if not transf.empty:
        serie_t = A.serie_anual(transf, "VALOR", by="FAVORECIDO_NORM")
        total_t = float(transf["VALOR"].sum())
        achados.append(Finding(
            id="empenhos-transferencias-sistema", frente=FRENTE, gravidade="info",
            titulo=f"{fmt_brl(total_t)} transferidos ao Confea e à Mútua no período",
            descricao=("Repasses determinados por lei e estatuto, isolados do "
                       "ranking de fornecedores (não disputam licitação). Mostrados "
                       "à parte porque respondem por parcela relevante da receita "
                       "arrecadada dos profissionais e alimentam a rede de "
                       "influência entre os órgãos do sistema."),
            evidencia=A.ranking_anual(transf, "FAVORECIDO_NORM", "VALOR", top=6),
            serie=serie_t, grafico="bar_stacked",
            tabela_anual=A.ranking_anual(transf, "FAVORECIDO_NORM", "VALOR", top=6),
        ))

    # -------------------------------------------------- anulações e saldos
    if not emp.empty and {"VALOR", "VALOR_ANULADO"} <= set(emp.columns):
        por_ano = emp.groupby("_ano").agg(
            empenhado=("VALOR", "sum"), anulado=("VALOR_ANULADO", "sum")).reset_index()
        por_ano["pct_anulado"] = (100 * por_ano["anulado"] /
                                  por_ano["empenhado"].replace(0, pd.NA)).fillna(0).round(1)
        criticos = por_ano[por_ano["pct_anulado"] > 20]
        serie_anul = por_ano.set_index("_ano")[["pct_anulado"]].rename(
            columns={"pct_anulado": "% do empenhado anulado"})
        achados.append(Finding(
            id="empenhos-anulacoes", frente=FRENTE,
            gravidade="media" if not criticos.empty else "info",
            titulo=(f"{len(criticos)} exercício(s) com mais de 20% do valor "
                    "empenhado anulado" if not criticos.empty else
                    "Percentual anual de empenho anulado"),
            descricao="Anulação em massa sugere planejamento orçamentário ruim ou "
                      "manobra de reserva/liberação de dotação: empenha-se no "
                      "início do exercício para travar dotação e anula-se depois, "
                      "sem que a despesa jamais existisse.",
            evidencia=por_ano,
            serie=serie_anul, grafico="line", unidade="%",
            tabela_anual=por_ano,
            base_legal="Lei 4.320/1964, arts. 58 a 60 (empenho prévio e limite da "
                       "dotação) e art. 38 (anulação da despesa).",
            metricas={"anos_criticos": criticos["_ano"].astype(int).tolist()},
        ))

        # ---- anatomia: empenho fantasma, dezembro, reempenho
        anat = _anatomia_anulacoes(emp)
        if anat is not None:
            resumo, fantasmas, reempenhos = anat
            achados.append(Finding(
                id="empenhos-anulacao-anatomia", frente=FRENTE,
                gravidade="alta" if float(resumo["fantasma"].sum()) > 0 else "info",
                titulo=("Anatomia das anulações: empenho fantasma, corrida de "
                        "dezembro e reempenho"),
                descricao=(
                    "A anulação tem três assinaturas distintas e só a primeira é "
                    "rotina. **Fantasma**: empenho anulado integralmente e nunca "
                    "pago — a despesa nunca existiu, mas ocupou dotação. "
                    "**Dezembro**: anulação concentrada no fim do exercício, típica "
                    "de liberação de saldo para acomodar outra despesa. "
                    "**Reempenho**: o mesmo credor volta a ser empenhado na mesma "
                    "conta no exercício seguinte — indica que a despesa era real e "
                    "a anulação apenas empurrou o compromisso para o ano seguinte, "
                    "fugindo do controle do exercício."),
                evidencia=resumo.reset_index(),
                serie=resumo[["fantasma", "anulado_dezembro"]].rename(
                    columns={"fantasma": "anulação integral (fantasma)",
                             "anulado_dezembro": "anulado em dezembro"}),
                grafico="bar_stacked",
                tabela_anual=resumo.reset_index(),
                base_legal="Lei 4.320/1964, arts. 35, 36 e 38; princípio da "
                           "anualidade orçamentária.",
                como_verificar="Pedir a nota de anulação de cada empenho fantasma "
                               "com a justificativa do ordenador de despesa.",
                metricas={
                    "empenhos_fantasma": int(len(fantasmas)),
                    "valor_fantasma": round(float(resumo["fantasma"].sum()), 2),
                    "reempenhos_ano_seguinte": int(len(reempenhos)),
                },
            ))
            if not fantasmas.empty:
                cols = [c for c in ("_ano", "NUMERO", "FAVORECIDO_NORM",
                                    "CONTA_EMPENHO", "VALOR", "VALOR_ANULADO",
                                    "VALOR_PAGO", "DATA")
                        if c in fantasmas.columns]
                achados.append(Finding(
                    id="empenhos-fantasma", frente=FRENTE, gravidade="media",
                    titulo=f"{len(fantasmas)} empenho(s) anulados integralmente "
                           "sem qualquer pagamento",
                    descricao="Reserva de dotação que nunca virou despesa. Em "
                              "volume, é indício de empenho feito para travar "
                              "orçamento e não para contratar.",
                    evidencia=fantasmas[cols].sort_values(
                        "VALOR", ascending=False).head(200),
                ))
        estouro = emp[emp["VALOR_PAGO"] > emp["VALOR"] * 1.001]
        if not estouro.empty:
            achados.append(Finding(
                id="empenhos-pago-maior-que-empenhado", frente=FRENTE, gravidade="alta",
                titulo=f"{len(estouro)} empenho(s) com valor pago maior que o empenhado",
                descricao="Pagamento acima do valor do empenho viola a execução "
                          "orçamentária (Lei 4.320/64, art. 59).",
                evidencia=estouro.head(100),
            ))
    return achados
