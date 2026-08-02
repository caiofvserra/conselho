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


def so_digitos(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


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


def eh_pf(cpf_cnpj, nome=None) -> bool:
    return classificar_pessoa(cpf_cnpj, nome) == "PF"


def eh_pj(cpf_cnpj, nome=None) -> bool:
    """Só devolve True com evidência positiva de pessoa jurídica."""
    return classificar_pessoa(cpf_cnpj, nome) == "PJ"


def parse_data(v):
    return pd.to_datetime(v, errors="coerce", dayfirst=True, format="mixed")


def fmt_brl(v: float) -> str:
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"
