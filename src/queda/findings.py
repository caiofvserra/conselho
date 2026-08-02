"""Modelo de achado de fiscalização."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

GRAVIDADES = ("alta", "media", "baixa", "info")


@dataclass
class Finding:
    id: str                      # ex.: "diarias-teto-unitario"
    frente: str                  # ex.: "Diárias e indenizações"
    gravidade: str               # alta | media | baixa | info
    titulo: str
    descricao: str               # metodologia + o que foi encontrado
    evidencia: pd.DataFrame | None = None
    metricas: dict = field(default_factory=dict)

    def __post_init__(self):
        assert self.gravidade in GRAVIDADES, self.gravidade
        self.id = re.sub(r"[^a-z0-9\-]", "-", self.id.lower())

    @property
    def n_linhas(self) -> int:
        return 0 if self.evidencia is None else len(self.evidencia)

    def resumo(self) -> dict:
        return {
            "id": self.id, "frente": self.frente, "gravidade": self.gravidade,
            "titulo": self.titulo, "descricao": self.descricao,
            "linhas_evidencia": self.n_linhas, "metricas": self.metricas,
        }
