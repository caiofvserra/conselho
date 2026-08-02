"""Frentes de análise. Cada módulo expõe run(staged) -> list[Finding]."""
from __future__ import annotations

import logging

from ..findings import Finding
from . import contratos, diarias, empenhos, fornecedores, pessoal, rede

log = logging.getLogger("queda.checks")

MODULOS = [diarias, pessoal, empenhos, contratos, rede, fornecedores]


def run_all(staged) -> list[Finding]:
    achados: list[Finding] = []
    for mod in MODULOS:
        try:
            found = mod.run(staged)
            log.info("%s: %d achados", mod.__name__.split(".")[-1], len(found))
            achados.extend(found)
        except Exception:
            log.exception("check %s falhou — seguindo com os demais", mod.__name__)
    ordem = {"alta": 0, "media": 1, "baixa": 2, "info": 3}
    achados.sort(key=lambda f: ordem[f.gravidade])
    return achados
