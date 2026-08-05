"""Frentes de análise. Cada módulo expõe run(staged) -> list[Finding]."""
from __future__ import annotations

import logging
import re

from ..findings import Finding
import traceback

from . import (atas, casos, contratos, diagnostico, diarias, empenhos,
               finalistica, forense, fornecedores, opacidade, orcamento,
               pessoal, receita, rede, representacao, superintendencias)

log = logging.getLogger("queda.checks")

# ordem = ordem das abas no relatório
MODULOS = [finalistica, superintendencias, receita, orcamento, opacidade,
           diarias, pessoal, representacao, casos, empenhos, contratos,
           rede, atas, forense, fornecedores, diagnostico]


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
    _rebaixar_vazios(achados)
    ordem = {"alta": 0, "media": 1, "baixa": 2, "info": 3}
    achados.sort(key=lambda f: ordem[f.gravidade])
    return achados


# "excederam a dotação em 0 de 13 exercícios" saiu com gravidade média na
# rodada real. Achado que não achou nada não pode ocupar espaço de achado:
# ocupa a atenção do leitor e desgasta a credibilidade do resto.
_RX_ZERO = re.compile(r"\b0 (de|caso|contrato|fornecedor|entidade|pessoa|"
                      r"unidade|exercicio|exercício|empenho|deliberac)", re.I)


def _rebaixar_vazios(achados: list[Finding]) -> None:
    for f in achados:
        if f.gravidade == "info":
            continue
        vazio_titulo = bool(_RX_ZERO.search(f.titulo or ""))
        sem_evidencia = (f.evidencia is None or getattr(f.evidencia, "empty", False))
        sem_serie = (f.serie is None or getattr(f.serie, "empty", True))
        if vazio_titulo or (sem_evidencia and sem_serie and not f.metricas):
            log.info("rebaixando %s para info: nada foi sinalizado", f.id)
            f.gravidade = "info"
