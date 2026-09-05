"""Regressões da rodada 15 — acórdãos do TCU, com os caminhos reais.

Os caminhos que a rodada 14 usava eram PALPITE meu. Os reais, informados em
05/09/2026, ficam em `dados-abertos.apps.tcu.gov.br` e em
`certidoes-apf.apps.tcu.gov.br`. Continuo sem poder verificá-los daqui (o proxy
da sessão bloqueia `*.gov.br`), então o que estas regressões trancam é de novo
o CONTRATO, não a resposta:

* a varredura de acórdãos é PARCIAL, limitada por rodada e retomável — e o
  progresso tem de sobreviver entre execuções, senão ela recomeça do zero para
  sempre;
* acórdão que cita só um FORNECEDOR não entra na conta do que o Tribunal
  decidiu sobre o conselho: são processos de outro jurisdicionado, e misturar
  os dois publicaria contra o Crea-SP algo que não é dele;
* fim da base e host que não responde são estados diferentes, e nenhum deles
  pode virar "não há acórdão".

Tudo offline: rede substituída por funções locais, disco em pasta temporária.
"""
from __future__ import annotations

import json

import pandas as pd

from .. import clients, config, pipeline
from ..checks import tcu as T
from .rodada12 import _ambiente, _diagnostico_limpo, _erro, _trocar


def _acordao(n: int, texto: str) -> dict:
    return {"numeroAcordao": str(n), "anoAcordao": 2024, "colegiado": "Plenário",
            "relator": "Ministro X", "dataSessao": "2024-01-10",
            "numeroProcesso": f"TC {n:03d}.000/2023-0",
            "tipoProcesso": "Prestação de Contas",
            "urlAcordao": f"https://pesquisa.apps.tcu.gov.br/{n}",
            "sumario": texto}


def _varredura_limitada_e_retomavel(erros: list[str]) -> None:
    """Cada rodada varre um pedaço e a seguinte continua de onde parou."""
    alvo = config.ORGAO_ALVO_CNPJ_CANDIDATOS[0]
    lidos: list[int] = []

    def _bloco(inicio, quantidade):
        lidos.append(inicio)
        if inicio >= 6:                       # a base acaba no sexto registro
            return []
        return [_acordao(inicio + i,
                         f"acordao citando {alvo} e nada mais" if inicio == 0 and i == 0
                         else "acordao sobre outro orgao qualquer")
                for i in range(quantidade)]

    guardado_blocos = config.TCU_ACORDAOS_BLOCOS_POR_RODADA
    guardado_qtd = config.TCU_ACORDAOS_QUANTIDADE
    try:
        config.TCU_ACORDAOS_BLOCOS_POR_RODADA = 2
        config.TCU_ACORDAOS_QUANTIDADE = 2
        with _ambiente(checkpoint="0"), _trocar(clients, tcu_acordaos_bloco=_bloco), \
                _diagnostico_limpo():
            df1 = pipeline.stage_tcu_acordaos(2026)
            _erro(erros, lidos == [0, 2],
                  f"acórdãos: a primeira rodada deveria ler 2 blocos, leu {lidos}")
            _erro(erros, len(df1) == 1,
                  f"acórdãos: só o que cita o alvo é guardado, ficaram {len(df1)}")
            progresso = json.loads(
                (config.RAW_DIR / "tcu-acordaos" / "progresso.json").read_text())
            _erro(erros, progresso.get("proximo_inicio") == 4
                  and progresso.get("varridos") == 4,
                  f"acórdãos: progresso saiu {progresso}")

            # segunda rodada: continua de 4, não recomeça do zero
            lidos.clear()
            df2 = pipeline.stage_tcu_acordaos(2026)
            _erro(erros, lidos and lidos[0] == 4,
                  f"acórdãos: a segunda rodada recomeçou em {lidos[:1]}, esperado 4")
            _erro(erros, len(df2) == 1,
                  f"acórdãos: o que já estava em disco foi perdido ({len(df2)} linha(s))")
            progresso = json.loads(
                (config.RAW_DIR / "tcu-acordaos" / "progresso.json").read_text())
            _erro(erros, progresso.get("fim_da_base") is True
                  and progresso.get("proximo_inicio") == 0,
                  f"acórdãos: fim da base não foi registrado ({progresso})")
    finally:
        config.TCU_ACORDAOS_BLOCOS_POR_RODADA = guardado_blocos
        config.TCU_ACORDAOS_QUANTIDADE = guardado_qtd


def _host_mudo_nao_e_ausencia(erros: list[str]) -> None:
    """Nenhum caminho respondeu ≠ não há acórdão. A diferença tem de aparecer
    no diagnóstico, senão o relatório afirma cobertura que não teve."""
    with _ambiente(checkpoint="0"), _trocar(
            clients, tcu_acordaos_bloco=lambda inicio, quantidade: None), \
            _diagnostico_limpo():
        df = pipeline.stage_tcu_acordaos(2026)
        _erro(erros, df.empty, f"acórdãos: host mudo deveria dar vazio, deu {len(df)}")
        diag = {d["dataset"]: d for d in pipeline._DIAGNOSTICO}
        nota = str(diag.get("tcu_acordaos", {}).get("indisponivel", ""))
        _erro(erros, "respondeu" in nota,
              f"acórdãos: host mudo não foi registrado como indisponível ({nota!r})")


def _fornecedor_nao_vira_achado_do_conselho(erros: list[str]) -> None:
    """Citação de fornecedor em processo de OUTRO órgão não é decisão sobre o
    Crea-SP. Misturar as duas publicaria contra o conselho algo alheio a ele."""
    ac = pd.DataFrame([
        {"numero": "1", "ano": 2022, "processo": "TC 1/2021", "url": "u1",
         "cnpjs_citados": "00000000000191", "trecho": "contas do conselho",
         "cita_o_conselho": True},
        {"numero": "2", "ano": 2024, "processo": "TC 2/2023", "url": "u2",
         "cnpjs_citados": "55666777000188", "trecho": "representacao de outro orgao",
         "cita_o_conselho": False},
        {"numero": "3", "ano": 2024, "processo": "TC 3/2023", "url": "u3",
         "cnpjs_citados": "55666777000188, 66777888000199", "trecho": "outro caso",
         "cita_o_conselho": False},
    ])
    achados = {f.id: f for f in T.run({"tcu_acordaos": ac})}
    c = achados.get("tcu-acordaos-conselho")
    _erro(erros, c is not None and c.metricas.get("acordaos") == 1,
          f"tcu: acórdãos sobre o conselho saiu {None if c is None else c.metricas}")
    f = achados.get("tcu-acordaos-fornecedores")
    _erro(erros, f is not None and f.metricas.get("acordaos") == 2
          and f.metricas.get("fornecedores_citados") == 2,
          f"tcu: acórdãos de fornecedor saiu {None if f is None else f.metricas}")
    _erro(erros, f is not None and f.gravidade == "info",
          "tcu: citação de fornecedor em processo alheio não pode ter gravidade "
          "acima de info — não é achado contra o conselho nem contra a empresa")
    # a ressalva de cobertura parcial tem de estar nos DOIS textos
    for a in (c, f):
        if a is not None:
            _erro(erros, "NÃO é" in a.descricao and "varredura" in a.descricao,
                  f"tcu/{a.id}: falta a ressalva de que a varredura é parcial")
    _erro(erros, T.run({"tcu_acordaos": pd.DataFrame()}) == [],
          "tcu: sem acórdão coletado a frente não pode publicar nada")


def _cnpj_no_texto(erros: list[str]) -> None:
    """O filtro acha CNPJ formatado e cru, e não inventa um de 13 ou 15 dígitos."""
    texto = pipeline._texto_do_registro(
        {"a": "cita 60.985.017/0001-77", "b": {"c": ["e 12345678000199", 123]},
         "d": "1234567890123456 nao e cnpj"})
    achados = pipeline._cnpjs_no_texto(texto)
    _erro(erros, achados == {"60985017000177", "12345678000199"},
          f"cnpjs no texto: {achados}")


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_varredura_limitada_e_retomavel, _host_mudo_nao_e_ausencia,
                  _fornecedor_nao_vira_achado_do_conselho, _cnpj_no_texto):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
