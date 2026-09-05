"""Modelo de achado de fiscalização.

Regra do projeto: **todo achado quantitativo carrega sua série anual**.
O relatório renderiza automaticamente um gráfico para cada achado que tiver
`serie` preenchida, e uma tabela ano a ano quando houver `tabela_anual`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

# "erro" é falha da FERRAMENTA (uma frente que levantou exceção), não achado
# sobre o conselho: fica fora da contagem de "gravidade alta" e num bloco
# próprio do relatório. Na rodada 10 o erro do benchmark foi contado como um
# dos 26 achados altos sobre o Crea-SP.
GRAVIDADES = ("alta", "media", "baixa", "info", "erro")
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

    # --- cobertura da base -------------------------------------------------
    # Que fatia do universo o número deste achado enxerga. Preencher SEMPRE que
    # o achado publicar uma RAZÃO, um PERCENTUAL ou um "X para cada Y", porque
    # nesses casos o denominador é metade da afirmação e ele costuma ser
    # invisível para quem lê.
    #
    # Na rodada real, "para cada R$ 1 na atividade-fim o conselho gasta R$ 14,9
    # com a máquina" foi publicado sem dizer que a atividade-fim identificada
    # era 1,35% da despesa e que essa identificação é NOSSA, por nome de centro
    # de custo — não do órgão. O número pode estar certo; a frase, do jeito que
    # saiu, não era verificável. Numa ferramenta de fiscalização isso é pior do
    # que não publicar.
    #
    # Formato: {"base": float, "universo": float, "o_que": str}
    # `pct` é derivado. `o_que` descreve o que está sendo coberto, em uma linha.
    cobertura: dict | None = None
    # Datasets staged de onde o número saiu (ex.: ["pagamentos", "sancoes"]).
    # O relatório monta com isto o rodapé "Fonte: … · exercícios … · coleta …";
    # vazio, ele deduz da frente.
    fontes: list = field(default_factory=list)

    def __post_init__(self):
        assert self.gravidade in GRAVIDADES, self.gravidade
        assert self.grafico in TIPOS_GRAFICO, self.grafico
        self.id = re.sub(r"[^a-z0-9\-]", "-", self.id.lower())
        if self.cobertura and "pct" not in self.cobertura:
            uni = float(self.cobertura.get("universo") or 0)
            base = float(self.cobertura.get("base") or 0)
            self.cobertura["pct"] = round(100 * base / uni, 1) if uni else 0.0

    @property
    def cobertura_pct(self) -> float | None:
        return None if not self.cobertura else float(self.cobertura.get("pct", 0))

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
            "fontes": list(self.fontes or []),
            "linhas_evidencia": self.n_linhas, "metricas": self.metricas,
            "base_legal": self.base_legal, "como_verificar": self.como_verificar,
            "cobertura": self.cobertura,
            "serie": self.serie_json(),
        }
