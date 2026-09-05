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


# O que a ementa de um acórdão MENCIONA. Não é o que o Tribunal decidiu: ementa
# é resumo, e o dispositivo está no acórdão inteiro — por isso cada linha da
# evidência leva o link. O texto vem normalizado (maiúsculas, sem acento) de
# `_texto_do_registro`, então a busca é por substring simples.
#
# A separação entre os dois grupos é a que muda a gravidade do achado: uma
# ementa que fala em multa ou em tomada de contas especial trata de matéria
# condenatória e merece leitura imediata; uma que fala em determinação ou
# recomendação trata de providência. Nenhum dos dois grupos afirma resultado.
_MENCOES_CONDENATORIAS: tuple[tuple[str, str], ...] = (
    ("TOMADA DE CONTAS ESPECIAL", "tomada de contas especial"),
    ("CONTAS IRREGULARES", "contas julgadas irregulares"),
    ("DANO AO ERARIO", "dano ao erário"),
    ("MULTA", "multa"),
    ("DEBITO", "débito"),
    ("INIDONEIDADE", "inidoneidade"),
    ("INABILITACAO", "inabilitação"),
)
_MENCOES_DE_PROVIDENCIA: tuple[tuple[str, str], ...] = (
    ("DESCUMPRIMENTO", "descumprimento de deliberação"),
    ("DETERMINACAO", "determinação"),
    ("RECOMENDACAO", "recomendação"),
    ("AUDIENCIA", "audiência"),
    ("CITACAO", "citação"),
    ("CAUTELAR", "medida cautelar"),
    ("ALERTA", "alerta"),
    ("IRREGULARIDADE", "irregularidade apontada"),
)


# Do que o processo trata. Não muda gravidade nenhuma: existe para que NENHUMA
# linha da evidência saia com a coluna vazia. Célula vazia lê-se como "não há
# nada aqui", e o acórdão 1513/2026 — denúncia sobre a licitação da nova sede —
# saía assim só porque a ementa dele não usa nenhuma das palavras acima.
_MENCOES_DE_NATUREZA: tuple[tuple[str, str], ...] = (
    ("PRESTACAO DE CONTAS", "prestação de contas"),
    ("MONITORAMENTO", "monitoramento"),
    ("REPRESENTACAO", "representação"),
    ("DENUNCIA", "denúncia"),
    ("PEDIDO DE REEXAME", "pedido de reexame"),
    ("EMBARGOS DE DECLARACAO", "embargos de declaração"),
    ("LICITACAO", "licitação"),
    ("CONCORRENCIA", "concorrência"),
    ("PREGAO", "pregão"),
    ("CONTRATACAO", "contratação"),
)


def _mencoes(trecho: str) -> tuple[list[str], bool]:
    """(termos encontrados na ementa, se algum é de matéria condenatória)."""
    texto = str(trecho or "").upper()
    achados = [rotulo for chave, rotulo in _MENCOES_CONDENATORIAS if chave in texto]
    condenatoria = bool(achados)
    for grupo in (_MENCOES_DE_PROVIDENCIA, _MENCOES_DE_NATUREZA):
        achados += [rotulo for chave, rotulo in grupo if chave in texto]
    return achados, condenatoria


# A ressalva que acompanha qualquer leitura da ementa. Ela existe porque a
# diferença entre "a ementa menciona multa" e "o Tribunal aplicou multa" é a
# diferença entre uma pista verificável e uma afirmação que este relatório não
# tem como sustentar — o acórdão de 2026 sobre o conselho, por exemplo, fala em
# apuração de EVENTUAL dano, não em dano apurado.
_RESSALVA_EMENTA = (
    "\n\n**Como ler a coluna `ementa_menciona`:** ela diz que o termo APARECE no "
    "resumo do acórdão, não que o Tribunal tenha decidido naquele sentido. Ementa "
    "é índice; o dispositivo está no acórdão inteiro, e o link de cada linha leva "
    "a ele. Uma ementa que fala em apuração de eventual dano trata de apuração em "
    "curso, não de dano apurado — a distinção é do próprio Tribunal e este "
    "relatório a mantém.")


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
    do_conselho = ac[cita.astype(str).str.lower().isin(("true", "1", "sim"))].copy()
    if not do_conselho.empty:
        # O que cada ementa menciona. Ler isto é o que separa "o TCU tem três
        # acórdãos sobre o conselho" — verdadeiro e inútil — de "a ementa de
        # dois deles fala em multa e em tomada de contas especial", que é o
        # que faz alguém abrir o documento.
        lidos = [_mencoes(t) for t in coluna(do_conselho, "trecho", "")]
        do_conselho["ementa_menciona"] = ["; ".join(m) for m, _ in lidos]
        com_materia = int(sum(1 for _, cond in lidos if cond))
        rotulos_condenatorios = {r for _, r in _MENCOES_CONDENATORIAS}
        destaque = sorted({r for m, cond in lidos if cond for r in m
                           if r in rotulos_condenatorios})

        cols = [c for c in ("numero", "ano", "colegiado", "relator", "data_sessao",
                            "processo", "tipo_processo", "ementa_menciona", "url",
                            "trecho") if c in do_conselho.columns]
        anos = pd.to_numeric(coluna(do_conselho, "ano", None), errors="coerce").dropna()
        periodo = (f"{int(anos.min())}–{int(anos.max())}" if len(anos) > 1
                   else (str(int(anos.iloc[0])) if len(anos) == 1 else "sem ano"))
        titulo = (f"{len(do_conselho)} acórdão(s) do TCU citam o conselho "
                  f"({periodo})")
        if destaque:
            titulo += (f"; a ementa de {com_materia} deles menciona "
                       + ", ".join(destaque))
        achados.append(Finding(
            id="tcu-acordaos-conselho", frente=FRENTE,
            gravidade="alta" if destaque else "media",
            titulo=titulo,
            descricao=(
                "Decisões do Tribunal de Contas da União que mencionam o Crea-SP "
                "pelo nome ou pelo CNPJ. As contas dos conselhos de fiscalização "
                "profissional são julgadas pelo TCU (Lei 8.443/1992, art. 5º, V), "
                "então estes documentos são a apuração feita por quem tem "
                "competência para fazê-la — e não uma leitura nossa dos dados. "
                "Quando um ponto deste relatório e um acórdão tratam do mesmo "
                "assunto, quem vale é o acórdão.\n\n"
                "**Como usar:** cada linha traz o processo, o relator e o link. O "
                "acórdão diz se as contas foram julgadas regulares, regulares com "
                "ressalva ou irregulares, e o que o Tribunal determinou. É o "
                "primeiro lugar a conferir antes de afirmar qualquer coisa que "
                "este relatório levante por conta própria."
                + _RESSALVA_EMENTA + ressalva),
            evidencia=do_conselho[cols].head(200),
            base_legal="Lei 8.443/1992, art. 1º, II e art. 5º, V; CF, art. 71, II.",
            como_verificar=("Abrir cada acórdão pelo número e ano em "
                            "pesquisa.apps.tcu.gov.br — o link da linha vai direto "
                            "— e ler o dispositivo, que é o que decide. A ementa "
                            "só indica o assunto."),
            metricas={"acordaos": int(len(do_conselho)), "periodo": periodo,
                      "ementa_com_materia_condenatoria": com_materia,
                      "termos": ", ".join(destaque)},
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
