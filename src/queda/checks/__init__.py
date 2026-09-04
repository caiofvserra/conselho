"""Frentes de análise. Cada módulo expõe run(staged) -> list[Finding]."""
from __future__ import annotations

import logging
import re

from ..findings import Finding
import traceback

from . import (atas, benchmark, casos, contratos, diagnostico, diarias, empenhos,
               finalistica, forense, fornecedores, opacidade, orcamento,
               pessoal, plano, receita, rede, representacao, sazonalidade,
               superintendencias)

log = logging.getLogger("queda.checks")

# ordem = ordem das abas no relatório
MODULOS = [finalistica, superintendencias, receita, orcamento, plano,
           opacidade, diarias, pessoal, sazonalidade, representacao, casos,
           empenhos, contratos, rede, atas, forense, fornecedores, benchmark,
           diagnostico]


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
    _declarar_cobertura(achados)
    ordem = {"alta": 0, "media": 1, "baixa": 2, "info": 3}
    achados.sort(key=lambda f: ordem[f.gravidade])
    return achados


# Um achado que enxerga uma fatia pequena do universo pode estar certíssimo —
# e ainda assim a frase que ele publica não é verificável por quem lê, porque
# o denominador não aparece. Estes dois cortes não escondem nada: escrevem no
# próprio achado o que ele cobre, e só rebaixam a gravidade quando a base é
# pequena a ponto de a conclusão depender mais da nossa classificação do que
# do dado do órgão.
COBERTURA_AVISO = 60.0     # abaixo disto, a cobertura entra no texto
COBERTURA_REBAIXA = 25.0   # abaixo disto, deixa de ser achado de gravidade alta


def _declarar_cobertura(achados: list[Finding]) -> None:
    for f in achados:
        pct = f.cobertura_pct
        if pct is None or pct >= COBERTURA_AVISO:
            continue
        o_que = (f.cobertura or {}).get("o_que") or "a base deste achado"
        aviso = (f"\n\n**Cobertura da base: {pct:.1f}%.** O número acima foi "
                 f"medido sobre {o_que}, que é essa fração do universo "
                 "comparável. Isso não invalida a medida, mas significa que "
                 "ela depende de como essa fração foi delimitada — e a "
                 "delimitação é da ferramenta, não do órgão. Confira a "
                 "evidência antes de citar o número.")
        if aviso not in f.descricao:
            f.descricao += aviso
        if pct < COBERTURA_REBAIXA and f.gravidade == "alta":
            log.info("rebaixando %s de alta para media: cobertura %.1f%%",
                     f.id, pct)
            f.gravidade = "media"


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
