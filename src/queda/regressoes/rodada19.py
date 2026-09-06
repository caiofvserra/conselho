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


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_a_palavra_fiscal_nao_basta, _plural_e_singular_no_mesmo_lado,
                  _a_guarda_nao_e_generica):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
