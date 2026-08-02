"""Frente 2 — Custo real por categoria (salário visível + indenização invisível).

O quadro de pessoal publica só o salário; diárias/indenizações ficam fora do
contracheque. Aqui somamos, por pessoa, o salário nominal e o que ela recebeu
em indenizações via pagamentos, expondo a "renda invisível".
"""
from __future__ import annotations

import pandas as pd

from .. import config
from ..findings import Finding
from ..utils import fmt_brl
from .common import classificar_beneficiario, df, pagamentos_indenizacao

FRENTE = "Custo real por categoria"


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    quadro = df(staged, "quadro_pessoal")
    ind = pagamentos_indenizacao(staged)

    # -------------------------------------------------- foto do quadro
    if not quadro.empty and "remuneracao" in quadro:
        vinc = quadro.get("VINCULO_NORM", "").fillna("")
        quadro = quadro.assign(
            grupo=vinc.str.contains("COMISS|LIVRE|CONFIAN", regex=True)
            .map({True: "comissionado", False: "efetivo"}))
        foto = (quadro.groupby("grupo")
                .agg(pessoas=("remuneracao", "size"), folha_mensal=("remuneracao", "sum"),
                     media=("remuneracao", "mean")).reset_index())
        achados.append(Finding(
            id="pessoal-foto-quadro", frente=FRENTE, gravidade="info",
            titulo="Foto atual do quadro: headcount e folha por vínculo",
            descricao="Somatório da remuneração nominal publicada no quadro de "
                      "colaboradores. ATENÇÃO: exclui indenizações/diárias.",
            evidencia=foto,
            metricas={"foto": foto.to_dict("records")},
        ))

    # -------------------------------------------------- renda invisível por pessoa
    if not ind.empty and not quadro.empty and "NOME_NORM" in quadro:
        ind = ind.copy()
        ind["categoria"] = classificar_beneficiario(ind["FAVORECIDO_NORM"], staged)
        anos = ind.get("_ano", pd.Series(dtype=float)).dropna()
        n_anos = max(int(anos.nunique()), 1)
        por_pessoa = ind.groupby("FAVORECIDO_NORM")["VALOR"].sum().rename("indenizacoes_periodo")
        base = quadro[["NOME_NORM", "grupo", "remuneracao"]].drop_duplicates("NOME_NORM")
        base = base.join(por_pessoa, on="NOME_NORM").fillna({"indenizacoes_periodo": 0})
        base["indenizacao_media_mensal"] = base["indenizacoes_periodo"] / (n_anos * 12)
        base["renda_invisivel_pct"] = (
            100 * base["indenizacao_media_mensal"] / base["remuneracao"].replace(0, pd.NA)
        ).fillna(0).round(1)
        top = base[base["indenizacoes_periodo"] > 0].sort_values(
            "renda_invisivel_pct", ascending=False)
        relevantes = top[top["renda_invisivel_pct"] > 30]
        if not relevantes.empty:
            achados.append(Finding(
                id="pessoal-renda-invisivel", frente=FRENTE, gravidade="alta",
                titulo=f"{len(relevantes)} pessoa(s) com indenizações médias acima de "
                       "30% do salário nominal",
                descricao="Indenização média mensal (total de diárias/indenizações do "
                          "período ÷ meses) comparada ao salário publicado no quadro. "
                          "Percentuais altos indicam remuneração disfarçada, fora do "
                          "teto e da transparência da folha.",
                evidencia=relevantes.head(50),
            ))
        achados.append(Finding(
            id="pessoal-cruzamento-quadro", frente=FRENTE, gravidade="info",
            titulo="Cruzamento quadro × indenizações (todas as pessoas com match)",
            descricao="Base completa do cruzamento por nome normalizado.",
            evidencia=top.head(200),
        ))

    # -------------------------------------------------- indicador-síntese
    if not ind.empty:
        ind2 = ind.copy()
        if "categoria" not in ind2:
            ind2["categoria"] = classificar_beneficiario(ind2["FAVORECIDO_NORM"], staged)
        gasto_grupo = ind2.groupby("categoria")["VALOR"].sum()
        num = float(gasto_grupo.get("conselheiro", 0) + gasto_grupo.get("comissionado", 0))
        folha_efetivos_ano = None
        if not quadro.empty and "remuneracao" in quadro and "grupo" in quadro:
            folha_efetivos_ano = float(
                quadro.loc[quadro["grupo"] == "efetivo", "remuneracao"].sum()) * 13.3
        if folha_efetivos_ano:
            anos = sorted(set(int(a) for a in ind2.get("_ano", pd.Series()).dropna()))
            num_ano = num / max(len(anos), 1)
            razao = num_ano / folha_efetivos_ano if folha_efetivos_ano else 0
            achados.append(Finding(
                id="pessoal-indicador-sintese", frente=FRENTE,
                gravidade="media" if razao > 0.10 else "info",
                titulo=f"Indenizações de conselheiros+comissionados ≈ {razao:.1%} "
                       "da folha anual de efetivos",
                descricao=f"Numerador: média anual de diárias/indenizações pagas a "
                          f"conselheiros e comissionados ({fmt_brl(num_ano)}). "
                          f"Denominador: folha anual estimada dos efetivos "
                          f"({fmt_brl(folha_efetivos_ano)}, salário × 13,3). ",
                metricas={"razao": round(razao, 4), "numerador_anual": round(num_ano, 2),
                          "folha_efetivos_anual": round(folha_efetivos_ano, 2)},
            ))

    # -------------------------------------------------- série histórica por conta
    comp = df(staged, "comparativo_despesa")
    if not comp.empty and "NOME" in comp:
        comp = comp.copy()
        comp["nome_lower"] = comp["NOME"].fillna("").str.lower()
        grupos = {
            "pessoal_efetivo": r"pessoal|vencimento|sal[aá]rio|remunera",
            "diarias_indenizacoes": config.REGEX_INDENIZACAO,
            "cargos_comissao": r"comiss|confian",
        }
        linhas = []
        valor_col = "REALIZADO_EXERCICIO" if "REALIZADO_EXERCICIO" in comp else "TOTAL_EXERCICIO"
        analitica = comp[comp.get("ANALITICA", True).astype(bool)] if "ANALITICA" in comp else comp
        for grupo, rx in grupos.items():
            m = analitica[analitica["nome_lower"].str.contains(rx, regex=True)]
            if m.empty:
                continue
            s = m.groupby("_ano")[valor_col].sum()
            for ano, v in s.items():
                linhas.append({"ano": int(ano), "grupo": grupo, "realizado": float(v)})
        if linhas:
            serie = pd.DataFrame(linhas)
            piv = serie.pivot_table(index="ano", columns="grupo", values="realizado",
                                    aggfunc="sum").fillna(0).sort_index()
            achados.append(Finding(
                id="pessoal-serie-historica", frente=FRENTE, gravidade="info",
                titulo="Série histórica por conta contábil (pessoal × comissão × diárias)",
                descricao="Despesa realizada por exercício, agregada por regex sobre os "
                          "nomes das contas do Comparativo de Despesa — permite comparar "
                          "a evolução desde o exercício mais antigo disponível na API.",
                evidencia=piv.reset_index(),
                metricas={"anos": [int(a) for a in piv.index],
                          "serie": {c: [round(float(v), 2) for v in piv[c]] for c in piv.columns}},
            ))
    return achados
