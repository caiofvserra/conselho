"""Utilitários de normalização compartilhados pelos checks."""
from __future__ import annotations

import re
import unicodedata

import pandas as pd


def coluna(df, nome: str, default="") -> "pd.Series":
    """Devolve sempre uma Series alinhada ao índice do DataFrame.

    `DataFrame.get(nome, default)` devolve o ESCALAR quando a coluna não
    existe; encadear `.map()` nele levanta AttributeError e derruba a frente
    inteira em silêncio (o `except` do runner engole). Este helper elimina a
    classe de bug de uma vez.
    """
    if nome in df.columns:
        return df[nome]
    return pd.Series([default] * len(df), index=df.index)


def norm_nome(s) -> str:
    """Normaliza nomes para comparação: maiúsculas, sem acento, espaço único."""
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().upper()


_MILHAR = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def parse_valor(v) -> float:
    """Converte valores das APIs em float.

    Trata os três formatos que aparecem: número JSON, decimal com ponto
    ('1234.56') e formato brasileiro ('1.234,56', '345.011.843', 'R$ 2.760').

    O ponto só é tratado como separador de milhar quando o texto tem o padrão
    exato de milhares (grupos de 3 dígitos) — sem isso, '345.011.843' virava
    0.0 e '2.760' virava 2,76, distorcendo valores de contrato por 1000x.
    """
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v)).strip()
    if not s or s in {"-", ".", ","}:
        return 0.0
    if "," in s:                          # pt-BR com decimais: 1.234,56
        s = s.replace(".", "").replace(",", ".")
    elif _MILHAR.match(s):                # pt-BR sem decimais: 345.011.843
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


# CNPJ como o portal grava dentro de CONTRATADA: "12.345.678/0001-90 - RAZÃO"
RX_CNPJ_FORMATADO = r"(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})"
_RX_FLOAT_ARTEFATO = re.compile(r"^\s*(\d+)\.0+\s*$")
_RX_FLOAT_CIENTIFICO = re.compile(r"^\s*\d(?:\.\d+)?[eE]\+\d+\s*$")


def so_digitos(s) -> str:
    """Só os dígitos de um documento — reconhecendo documento que passou por float.

    CSV relido sem `dtype` transforma "01816098000112" em 1816098000112.0; o
    `str()` disso é "1816098000112.0", e tirar só o que não é dígito dava
    "18160980001120": 14 dígitos, CNPJ ERRADO, e o cruzamento com o PNCP
    publicou 44 contratos como "ausentes" na rodada 11 por causa disso. O
    ".0" sai antes; notação científica ("6.0985017000177e+13") volta pelo int.
    """
    if s is None:
        return ""
    if isinstance(s, float):
        if s != s:                       # NaN
            return ""
        s = int(s) if s.is_integer() else s
    texto = str(s)
    m = _RX_FLOAT_ARTEFATO.match(texto)
    if m:
        return m.group(1)
    if _RX_FLOAT_CIENTIFICO.match(texto):
        # "6.0985017000177e+13" tem os 14 dígitos e volta inteiro; "6.0985e+13"
        # perdeu-os na impressão e voltaria "60985000000000" — um CNPJ que não
        # existe. Fabricar documento é pior que não ter documento.
        significativos = len(re.sub(r"\D", "", texto.split("e")[0].split("E")[0]))
        expoente = int(texto.split("e")[-1].split("E")[-1])
        if significativos < expoente + 1:
            return ""
        try:
            return str(int(float(texto)))
        except (ValueError, OverflowError):
            return ""
    return re.sub(r"\D", "", texto)


def cnpj14(serie, *, presumir_cnpj: bool = False) -> "pd.Series":
    """CNPJs com 14 dígitos, tolerantes a float e a zero à esquerda perdido.

    12 ou 13 dígitos = CNPJ que perdeu um ou dois zeros à esquerda ao passar
    por número: zfill(14). 11 dígitos é CPF e fica como está. Menos que isso
    não dá para afirmar o que é — fica como veio e o chamador filtra por
    `str.len() == 14`. (O `_cnpj14` anterior dava zfill em qualquer coisa e
    transformou "424000156.0" em "00004240001560".)

    `presumir_cnpj=True` é para coluna que SÓ tem CNPJ (CONTRATADA_CNPJ do
    portal, `fornecedor_ni` de PJ no PNCP): aí qualquer tamanho até 13 —
    menos 11, que é CPF — recebe zfill, porque "191" ali é o Banco do Brasil
    (00.000.000/0001-91) que passou por int. Nunca usar em coluna que mistura
    CPF mascarado ("***.123.456-**" → "123456"), como o CEIS: o zfill fabricaria
    um CNPJ de raiz 00000000 — e essa raiz existe, é a do Banco do Brasil.
    """
    s = pd.Series(serie).map(so_digitos).astype(str)
    n = s.str.len()
    if presumir_cnpj:
        completar = n.between(1, 13) & n.ne(11)
    else:
        completar = n.between(12, 13)
    return s.where(~completar, s.str.zfill(14))


RX_PJ_NOME = re.compile(
    r"\b(ltda|s\.?a\.?|eireli|epp|\bme\b|mei|s/a|s/s|cia|companhia|"
    r"associacao|instituto|fundacao|sindicato|conselho|federacao|"
    r"cooperativa|banco|prefeitura|municipio|estado|uniao|secretaria)\b",
    re.IGNORECASE)


def classificar_pessoa(cpf_cnpj, nome=None) -> str:
    """'PF' | 'PJ' | 'INDEFINIDO'.

    Documento ausente NÃO é PJ. Rótulos agregados do portal (ex.: "DIVERSOS
    FUNCIONARIOS", R$ 1,33 bi) vêm sem documento; classificá-los como PJ
    contaminava o ranking de fornecedores e o índice de concentração com o
    que na verdade é folha de pagamento.
    """
    raw = str(cpf_cnpj or "")
    d = so_digitos(raw)
    if len(d) == 14:
        return "PJ"
    if len(d) == 11:
        return "PF"
    if "*" in raw and len(d) >= 4:        # CPF mascarado pelo portal
        return "PF"
    if nome and RX_PJ_NOME.search(str(nome)):
        return "PJ"
    return "INDEFINIDO"


def eh_pj(cpf_cnpj, nome=None) -> bool:
    """Só devolve True com evidência positiva de pessoa jurídica."""
    return classificar_pessoa(cpf_cnpj, nome) == "PJ"


def parse_data(v):
    return pd.to_datetime(v, errors="coerce", dayfirst=True, format="mixed")


def fmt_brl(v: float) -> str:
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"
