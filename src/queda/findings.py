"""Modelo de achado de fiscalização.

Regra do projeto: **todo achado quantitativo carrega sua série anual**.
O relatório renderiza automaticamente um gráfico para cada achado que tiver
`serie` preenchida, e uma tabela ano a ano quando houver `tabela_anual`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

GRAVIDADES = ("alta", "media", "baixa", "info")
TIPOS_GRAFICO = ("bar", "bar_stacked", "line", "area_stacked", "barh")


@dataclass
class Finding:
    id: str                      # ex.: "diarias-teto-unitario"
    frente: str                  # ex.: "Diárias e indenizações"
    gravidade: str               # alta | media | baixa | info
    titulo: str
    descricao: str               # metodologia + o que foi encontrado
    evidencia: pd.DataFrame | None = None
    metricas: dict = field(default_factory=dict)

    # --- série anual (renderizada como gráfico no relatório) ---
    serie: pd.DataFrame | None = None      # índice = ano, colunas = categorias
    grafico: str = "bar"                   # ver TIPOS_GRAFICO
    unidade: str = "R$"                    # "R$", "%", "un"

    # --- ranking ano a ano (substitui "todo o período") ---
    tabela_anual: pd.DataFrame | None = None

    # --- fundamentação ---
    base_legal: str = ""                   # norma/acórdão que sustenta o achado
    como_verificar: str = ""               # próximo passo concreto de apuração

    def __post_init__(self):
        assert self.gravidade in GRAVIDADES, self.gravidade
        assert self.grafico in TIPOS_GRAFICO, self.grafico
        self.id = re.sub(r"[^a-z0-9\-]", "-", self.id.lower())

    @property
    def n_linhas(self) -> int:
        return 0 if self.evidencia is None else len(self.evidencia)

    def serie_json(self) -> dict | None:
        """Série anual no formato consumido pelo dashboard."""
        if self.serie is None or self.serie.empty:
            return None
        s = self.serie.copy()
        s.index = [int(i) for i in s.index]
        s = s.sort_index()
        return {
            "tipo": self.grafico,
            "unidade": self.unidade,
            "anos": list(s.index),
            "series": {str(c): [round(float(v), 2) for v in s[c].fillna(0)]
                       for c in s.columns},
        }

    def resumo(self) -> dict:
        return {
            "id": self.id, "frente": self.frente, "gravidade": self.gravidade,
            "titulo": self.titulo, "descricao": self.descricao,
            "linhas_evidencia": self.n_linhas, "metricas": self.metricas,
            "base_legal": self.base_legal, "como_verificar": self.como_verificar,
            "serie": self.serie_json(),
        }
