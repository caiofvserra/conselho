"""Regressões da rodada 19 — a palavra "fiscal" que não é fiscalização.

O operador levantou a suspeita certa pelo motivo certo: a conta contábil de
fiscalização estaria inflada com despesa que não é fiscalização. Ao conferir,
parte da inflação era NOSSA — o regex `\\bfiscal(is)?\\b` classificava como
fiscalização do exercício profissional:

    Conselho Fiscal ................ órgão de controle interno
    Nota fiscal / Notas fiscais .... documento de despesa
    Fiscal de contrato ............. gestão contratual
    Fiscalização de processos ...... auditoria interna

E o mesmo regex NÃO casava o plural real: "fiscais" não é `fiscal(is)?`, de
modo que "Fiscais de contrato" caía em OUTROS enquanto "Fiscal de contrato"
caía em FISCALIZACAO. Duas linhas do mesmo relatório, dois destinos.

POR QUE ISSO IMPORTA MAIS DO QUE PARECE: o denominador de
`finalistica-verbas-vs-fiscalizacao` — a frase mais citada do relatório, "a
verba indenizatória superou o gasto com fiscalização em 5 de 5 exercícios" —
sai daí. Denominador inflado empurra a razão para BAIXO, ou seja, o erro
trabalhava CONTRA a tese. Não importa: número errado a favor continua sendo
número errado, e num contraditório é ele que derruba a peça inteira.

A rodada cobre ainda as duas consequências disso:

* o DENOMINADOR ABERTO — a medida de fiscalização deixa de ser um total
  fechado e passa a sair rótulo a rótulo, com o peso de cada um. Sem isso,
  nem quem publica nem quem contesta consegue ver o que está lá dentro;
* a TRIAGEM POR LLM não inventa — resposta sem trecho literal conferível no
  próprio rótulo é descartada, e com o modelo desligado a lista sai vazia.

Tudo offline: não há rede aqui, só a taxonomia.
"""
from __future__ import annotations

from .. import taxonomia as T
from .rodada12 import _erro

# (texto, é fiscalização do exercício profissional?)
_CASOS: tuple[tuple[str, bool], ...] = (
    # a palavra existe e NÃO é fiscalização do exercício profissional
    ("Conselho Fiscal", False),
    ("Conselhos Fiscais", False),
    ("Nota fiscal - conferencia", False),
    ("Notas fiscais", False),
    ("Cupom fiscal", False),
    ("Documento fiscal", False),
    ("Livros fiscais", False),
    ("Fiscal de contrato", False),
    ("Fiscais de contrato", False),
    ("Gestores e fiscais de contrato", False),
    ("Fiscalizacao de contratos", False),
    ("Fiscalizacao de processos internos", False),
    ("Auditoria interna", False),
    ("Fiscais e gestores de contratos", False),
    ("Escrituracao fiscal", False),
    ("Obrigacoes fiscais acessorias", False),
    ("Responsabilidade fiscal", False),
    ("Incentivos fiscais", False),
    ("Beneficio fiscal", False),
    ("Cupons fiscais", False),
    # e o que É — nenhum destes pode ser perdido pela guarda
    ("Departamento de Fiscalizacao", True),
    ("Vistoria de obras", True),
    ("Agentes fiscais - campo", True),
    ("Fiscais em diligencia", True),
    ("Auto de infracao", True),
    ("Autos de infracao - julgamento", True),
    ("Unidade de Fiscalizacao", True),
    ("Coordenadoria de Fiscalizacao", True),
    ("Fiscalizacao do exercicio profissional", True),
    ("Fiscal de obra", True),
    ("Agente fiscal", True),
    ("Gerencia de Fiscalizacao", True),
)


def _a_palavra_fiscal_nao_basta(erros: list[str]) -> None:
    """A guarda vale nos DOIS eixos: natureza da despesa e função da unidade."""
    for texto, e_fiscalizacao in _CASOS:
        natureza = T.classificar_texto(texto)
        funcao = T.classificar_funcao(texto)
        if e_fiscalizacao:
            _erro(erros, natureza == "FISCALIZACAO",
                  f"taxonomia: {texto!r} É fiscalização e saiu como natureza "
                  f"{natureza!r} — a guarda comeu um caso verdadeiro")
            _erro(erros, funcao == "FISCALIZACAO",
                  f"taxonomia: {texto!r} É fiscalização e saiu com função {funcao!r}")
        else:
            _erro(erros, natureza != "FISCALIZACAO",
                  f"taxonomia: {texto!r} NÃO é fiscalização do exercício profissional "
                  f"e entrou como natureza FISCALIZACAO — infla o denominador da "
                  f"comparação verba × fiscalização")
            _erro(erros, funcao != "FISCALIZACAO",
                  f"taxonomia: {texto!r} NÃO é fiscalização e entrou com função "
                  f"FISCALIZACAO — infla o denominador")


def _plural_e_singular_no_mesmo_lado(erros: list[str]) -> None:
    """Foi a incoerência que denunciou o defeito: o singular ia para um lado e o
    plural para outro, no mesmo relatório."""
    pares = (("Fiscal de contrato", "Fiscais de contrato"),
             ("Agente fiscal", "Agentes fiscais"),
             ("Conselho Fiscal", "Conselhos Fiscais"))
    for singular, plural in pares:
        a, b = T.classificar_texto(singular), T.classificar_texto(plural)
        _erro(erros, a == b,
              f"taxonomia: {singular!r} deu {a!r} e {plural!r} deu {b!r} — singular e "
              "plural do mesmo termo não podem cair em categorias diferentes")


def _a_guarda_nao_e_generica(erros: list[str]) -> None:
    """A exclusão precisa ser cirúrgica: se ela derrubar qualquer texto com
    "fiscal", mata a medida inteira em vez de corrigi-la."""
    _erro(erros, T._e_fiscalizacao_de_verdade("departamento de fiscalizacao"),
          "a guarda derrubou o caso mais central de todos")
    _erro(erros, not T._e_fiscalizacao_de_verdade("conselho fiscal"),
          "a guarda deixou passar 'conselho fiscal'")


def _o_denominador_abre(erros: list[str]) -> None:
    """A medida de fiscalização tem de sair aberta, e a abertura tem de somar
    exatamente o total — nem rótulo perdido, nem rótulo de fora."""
    import pandas as pd

    from ..checks import finalistica as F

    base = pd.DataFrame({
        "_ano": [2024, 2024, 2024, 2024],
        "valor": [100.0, 50.0, 25.0, 900.0],
        "categoria": ["VERBAS_PESSOAIS", "REMUNERACAO", "FISCALIZACAO",
                      "CUSTEIO_ADMIN"],
        "funcao": ["FISCALIZACAO", "FISCALIZACAO", "FISCALIZACAO",
                   "ADMINISTRACAO"],
        "unidade": ["Depto de Fiscalizacao"] * 3 + ["Superintendencia"],
        "rotulo": ["Depto de Fiscalizacao / Diarias no Pais",
                   "Depto de Fiscalizacao / Vencimentos",
                   "Depto de Fiscalizacao / Acoes de Fiscalizacao",
                   "Superintendencia / Aluguel"],
    })
    achados = F._abrir_a_fiscalizacao(base, "funcao")
    comp = [a for a in achados if a.id == "finalistica-fiscalizacao-composicao"]
    _erro(erros, len(comp) == 1,
          "a composição da fiscalização não foi publicada — o denominador "
          "voltou a ser um total fechado, que ninguém consegue conferir")
    if not comp:
        return
    ev = comp[0].evidencia
    _erro(erros, abs(float(ev["valor"].sum()) - 175.0) < 0.01,
          f"a abertura somou {float(ev['valor'].sum())}, e a medida é 175 — "
          "abertura que não fecha com o total publicado é pior que não abrir")
    _erro(erros, not ev["rótulo"].str.contains("Aluguel").any(),
          "a abertura trouxe um rótulo que não está na medida de fiscalização")
    # e o achado mais citável: metade do que se chama fiscalização é diária
    linha = ev[ev["rótulo"].str.contains("Diarias")]
    _erro(erros, not linha.empty and abs(float(linha["% da fiscalização"].iloc[0]) - 57.1) < 0.2,
          "o percentual por rótulo saiu errado — é ele que mostra o peso de "
          "cada coisa dentro da medida")


def _o_modelo_nao_inventa(erros: list[str]) -> None:
    """A triagem por LLM não pode produzir linha sem lastro no texto auditado, e
    com o modelo desligado não pode produzir linha nenhuma."""
    from .. import llm

    _erro(erros, llm._trecho_confere("Colegio de Inspetores",
                                     "Evento — Colégio de Inspetores 2024"),
          "o trecho literal existe no rótulo e foi rejeitado (a comparação tem "
          "de ser frouxa em acento e caixa)")
    _erro(erros, not llm._trecho_confere("vistoria de obra",
                                         "Evento — Colégio de Inspetores 2024"),
          "trecho que NÃO está no rótulo passou na conferência — é a única "
          "barreira contra o modelo escrever motivo sobre coisa nenhuma")
    _erro(erros, not llm._trecho_confere("", "qualquer coisa"),
          "trecho vazio passou na conferência")
    _erro(erros, llm.auditar_enquadramento({}) == [],
          "a auditoria devolveu algo sem rubrica nenhuma para auditar")


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_a_palavra_fiscal_nao_basta, _plural_e_singular_no_mesmo_lado,
                  _a_guarda_nao_e_generica, _o_denominador_abre,
                  _o_modelo_nao_inventa):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
