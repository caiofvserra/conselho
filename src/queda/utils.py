"""Utilitários de normalização compartilhados pelos checks."""
from __future__ import annotations

import re
import unicodedata

import pandas as pd


def norm_nome(s) -> str:
    """Normaliza nomes para comparação: maiúsculas, sem acento, espaço único."""
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().upper()


def parse_valor(v) -> float:
    """Converte valores das APIs (float, '1.234,56', 'R$ 1.234,56') em float."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if not s:
        return 0.0
    if "," in s:                       # formato brasileiro
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def so_digitos(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def eh_pf(cpf_cnpj) -> bool:
    """Heurística: 11 dígitos (ou CPF mascarado ***xxx**) = pessoa física."""
    raw = str(cpf_cnpj or "")
    d = so_digitos(raw)
    if len(d) == 14:
        return False
    if len(d) == 11:
        return True
    return "*" in raw and len(d) >= 4


def parse_data(v):
    return pd.to_datetime(v, errors="coerce", dayfirst=True, format="mixed")


def fmt_brl(v: float) -> str:
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"
