"""Controle externo: o que o TCU já decidiu sobre o conselho e seus fornecedores.

Por que esta frente existe separada das sanções: CEIS e CNEP registram sanção
ADMINISTRATIVA do Executivo. Acórdão é decisão do Tribunal que julga as contas
— e as contas de um conselho de fiscalização profissional são julgadas pelo TCU
(Lei 8.443/1992, art. 5º, V; STF, MS 21.797). É a diferença entre "esta empresa
foi punida por algum órgão" e "o Tribunal examinou esta despesa".

A varredura é PARCIAL por construção: a API do TCU só pagina, sem busca por
texto, então percorremos blocos por rodada e filtramos localmente. Todo achado
daqui publica quantos acórdãos foram lidos — ausência de citação não é prova de
que não exista.
"""
from __future__ import annotations

import pandas as pd

from ..findings import Finding
from ..utils import coluna
from .common import df

FRENTE = "Controle externo (TCU)"


def _cobertura(diag: pd.DataFrame) -> str:
    """A frase de cobertura, lida do diagnóstico da coleta."""
    if diag is None or diag.empty or "dataset" not in diag.columns:
        return ""
    linha = diag[diag["dataset"].astype(str) == "tcu_acordaos"]
    if linha.empty or "fontes" not in linha.columns:
        return ""
    return str(linha.iloc[0].get("fontes") or "")


def run(staged) -> list[Finding]:
    ac = df(staged, "tcu_acordaos")
    if ac.empty:
        return []
    achados: list[Finding] = []
    cobertura = _cobertura(df(staged, "_coleta_diagnostico"))
    ressalva = (
        "\n\n**O que esta lista NÃO é:** o conjunto de tudo que o TCU decidiu. A "
        "API de acórdãos só pagina, sem busca por texto, então a varredura é "
        "sequencial e feita por partes a cada rodada"
        + (f" — {cobertura}. " if cobertura else ". ")
        + "Um acórdão fora do trecho já lido não aparece aqui, e isso não diz "
        "nada sobre ele existir ou não.")

    cita = coluna(ac, "cita_o_conselho", False)
    do_conselho = ac[cita.astype(str).str.lower().isin(("true", "1", "sim"))]
    if not do_conselho.empty:
        cols = [c for c in ("numero", "ano", "colegiado", "relator", "data_sessao",
                            "processo", "tipo_processo", "url", "trecho")
                if c in do_conselho.columns]
        anos = pd.to_numeric(coluna(do_conselho, "ano", None), errors="coerce").dropna()
        periodo = (f"{int(anos.min())}–{int(anos.max())}" if len(anos) > 1
                   else (str(int(anos.iloc[0])) if len(anos) == 1 else "sem ano"))
        achados.append(Finding(
            id="tcu-acordaos-conselho", frente=FRENTE, gravidade="media",
            titulo=(f"{len(do_conselho)} acórdão(s) do TCU citam o conselho "
                    f"({periodo})"),
            descricao=(
                "Decisões do Tribunal de Contas da União que mencionam o Crea-SP "
                "pelo nome ou pelo CNPJ. As contas dos conselhos de fiscalização "
                "profissional são julgadas pelo TCU (Lei 8.443/1992, art. 5º, V), "
                "então estes documentos são a apuração feita por quem tem "
                "competência para fazê-la — e não uma leitura nossa dos dados.\n\n"
                "**Como usar:** cada linha traz o processo e o link. O acórdão diz "
                "se as contas foram julgadas regulares, regulares com ressalva ou "
                "irregulares, e o que o Tribunal determinou. É o primeiro lugar a "
                "conferir antes de afirmar qualquer coisa que este relatório "
                "levante por conta própria."
                + ressalva),
            evidencia=do_conselho[cols].head(200),
            base_legal="Lei 8.443/1992, art. 1º, II e art. 5º, V; CF, art. 71, II.",
            como_verificar=("Abrir cada acórdão pelo número e ano em "
                            "pesquisa.apps.tcu.gov.br e ler o dispositivo."),
            metricas={"acordaos": int(len(do_conselho)), "periodo": periodo},
            fontes=["tcu_acordaos"],
        ))

    fornecedores = ac[~ac.index.isin(do_conselho.index)]
    fornecedores = fornecedores[coluna(fornecedores, "cnpjs_citados", "")
                                .astype(str).str.strip().ne("")]
    if not fornecedores.empty:
        cnpjs = set()
        for lista in fornecedores["cnpjs_citados"].astype(str):
            cnpjs |= {c.strip() for c in lista.split(",") if c.strip()}
        cols = [c for c in ("numero", "ano", "processo", "cnpjs_citados", "url",
                            "trecho") if c in fornecedores.columns]
        achados.append(Finding(
            id="tcu-acordaos-fornecedores", frente=FRENTE, gravidade="info",
            titulo=(f"{len(fornecedores)} acórdão(s) do TCU citam {len(cnpjs)} "
                    "fornecedor(es) do conselho, em processos de outros órgãos"),
            descricao=(
                "O CNPJ de uma empresa que fornece ao Crea-SP aparece num acórdão "
                "do TCU sobre outro jurisdicionado. **Isto não é achado contra o "
                "conselho nem contra a empresa:** citação em acórdão pode ser de "
                "qualquer natureza — a empresa pode ser a interessada, a "
                "representante, ou apenas mencionada. É contexto sobre com quem o "
                "conselho contrata, e um ponto de partida para quem for conferir."
                + ressalva),
            evidencia=fornecedores[cols].head(200),
            como_verificar=("Abrir o acórdão e ler em que qualidade a empresa "
                            "aparece antes de tirar qualquer conclusão."),
            metricas={"acordaos": int(len(fornecedores)),
                      "fornecedores_citados": len(cnpjs)},
            fontes=["tcu_acordaos", "contratos", "pncp_contratos"],
        ))
    return achados
