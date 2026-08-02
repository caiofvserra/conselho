"""Frentes de análise. Cada módulo expõe run(staged) -> list[Finding]."""
from __future__ import annotations

import logging

from ..findings import Finding
import traceback

from . import (casos, contratos, diagnostico, diarias, empenhos, finalistica,
               forense, fornecedores, opacidade, orcamento, pessoal, rede)

log = logging.getLogger("queda.checks")

# ordem = ordem das abas no relatório
MODULOS = [finalistica, orcamento, opacidade, diarias, pessoal, casos,
           empenhos, contratos, rede, forense, fornecedores, diagnostico]


def run_all(staged) -> list[Finding]:
    achados: list[Finding] = []
    for mod in MODULOS:
        nome = mod.__name__.split(".")[-1]
        try:
            found = mod.run(staged)
            log.info("%s: %d achados", nome, len(found))
            achados.extend(found)
        except Exception as exc:
            # Uma frente que estoura sumia deixando só uma linha de log: o
            # relatório saía com menos achados e nada dizia que faltava algo.
            # O erro passa a ser um achado visível no dashboard.
            log.exception("check %s falhou — seguindo com os demais", nome)
            achados.append(Finding(
                id=f"erro-check-{nome}", frente="Diagnóstico da coleta",
                gravidade="alta",
                titulo=f"A frente '{nome}' falhou e não produziu achados",
                descricao=(
                    f"A execução desta frente levantou `{type(exc).__name__}: "
                    f"{exc}`. As demais frentes seguiram normalmente, mas **tudo "
                    "o que esta frente mediria está ausente deste relatório** — "
                    "não interprete a ausência de achados dela como "
                    "regularidade."),
                metricas={"traceback": traceback.format_exc()[-2000:]},
            ))
    ordem = {"alta": 0, "media": 1, "baixa": 2, "info": 3}
    achados.sort(key=lambda f: ordem[f.gravidade])
    return achados
