"""Regressões da rodada 17 — a limpeza, e a rede de proteção que faltava.

O serviço legado de Dados Abertos saiu do código: em 13 rodadas nenhum dos seis
hosts devolveu JSON. Tirá-lo quebrou `stage_quadro_pessoal` e
`stage_mandatarios` de um jeito que NENHUM teste pegou — a chamada removida era
quem atribuía `df`, e o `if df.empty:` seguinte passou a ler um nome
indefinido. Só estouraria numa rodada real, no Colab do operador.

A regressão daqui é a que faltava: **executar TODAS as etapas** com a rede
substituída por uma função local, só para provar que nenhuma estoura. Ela não
verifica o conteúdo (isso é do selftest); verifica que o caminho existe.

Tudo offline: rede substituída por funções locais, disco em pasta temporária.
"""
from __future__ import annotations

import pandas as pd

from .. import clients, config, pipeline
from ..checks import common as CM
from .rodada12 import _ambiente, _diagnostico_limpo, _erro, _trocar

# Nomes que a produção NÃO deve mais conhecer. Cada um saiu porque provou-se
# morto; se voltar a aparecer, ou a limpeza foi desfeita ou alguém reintroduziu
# um caminho que não responde há treze rodadas.
_REMOVIDOS = ("dados_abertos", "dados_abertos_base", "_hosts_dados_abertos",
              "_discover_dados_abertos", "efeito_nacional",
              "pncp_instrumentos_cobranca", "DIARIAS_PROBE", "pncp_contratacoes")


def _nada_ressuscitou(erros: list[str]) -> None:
    for nome in _REMOVIDOS:
        if hasattr(clients, nome):
            erros.append(f"clients.{nome} voltou — foi removido por estar morto "
                         "(ver o commit da rodada 17)")
    for nome in ("DADOS_ABERTOS_CANDIDATES", "SWAGGER_CANDIDATOS_DADOS_ABERTOS_SUFIXOS",
                 "IMPLANTA_BASE", "ORGAO_ALVO_CNPJ", "TETO_DIARIA_UNITARIA"):
        if hasattr(config, nome):
            erros.append(f"config.{nome} voltou — foi removido por não ter uso")


def _toda_etapa_roda(erros: list[str]) -> None:
    """Executa TODAS as etapas com a rede desligada: nenhuma pode estourar.

    Não confere conteúdo — confere que o CAMINHO existe. Um `df` indefinido, um
    import que sumiu, uma chave que mudou de nome: tudo isso aparece aqui como
    exceção, e não na rodada do operador.
    """
    def _sem_rede(*a, **kw):
        return None

    locais = dict(
        get_json=_sem_rede,
        implanta=lambda *a, **kw: [],
        implanta_ano=lambda *a, **kw: [],
        implanta_em=lambda *a, **kw: [],
        implanta_variantes=lambda *a, **kw: ([], {}),
        implanta_primeiro_nome=lambda *a, **kw: ([], None),
        implanta_probe_diarias=lambda *a, **kw: {},
        pncp_resolver_orgao=lambda *a, **kw: None,
        pncp_contratos_mes=lambda *a, **kw: [],
        pncp_atas_mes=lambda *a, **kw: [],
        pncp_instrumentos_cobranca_mes=lambda *a, **kw: [],
        pncp_contratacoes_mes=lambda *a, **kw: [],
        pncp_meses=lambda ini, ate: [("20240101", "20240131")],
        baixar_sancoes=lambda *a, **kw: (None, {"cadastro": "x"}),
        baixar_tcu=lambda *a, **kw: (None, {"cadastro": "x"}),
        tcu_tipos_certidoes=lambda *a, **kw: None,
        tcu_acordaos_bloco=lambda *a, **kw: None,
        consultar_cnpj=lambda *a, **kw: None,
    )
    for nome in locais:
        if not hasattr(clients, nome):
            erros.append(f"regressão desatualizada: clients.{nome} não existe mais")
            return

    with _ambiente(checkpoint="0"), _trocar(clients, **locais), _diagnostico_limpo():
        etapas = pipeline._etapas(2024, 2024)
        for nome, executar in etapas.items():
            try:
                executar()
            except Exception as e:                    # noqa: BLE001
                erros.append(f"etapa {nome} estourou com a rede desligada: "
                             f"{type(e).__name__}: {e}")
        # e o inventário, que percorre as mesmas etapas por outro caminho
        try:
            pipeline.inventario(2024, 2024)
        except Exception as e:                        # noqa: BLE001
            erros.append(f"inventario estourou: {type(e).__name__}: {e}")


def _analise_roda_sobre_o_vazio(erros: list[str]) -> None:
    """`main.py all` analisa o que houver mesmo quando a coleta não trouxe nada.

    Toda frente precisa aguentar dataset vazio: é o estado de uma rodada que
    bateu no bloqueio logo no começo, e é quando o relatório mais importa.
    """
    from ..checks import run_all
    guardado = CM._FICHAS
    try:
        CM._FICHAS = {}
        vazio = {nome: pd.DataFrame() for nome in pipeline.DATASETS_ESPERADOS}
        achados = run_all(vazio)
    finally:
        CM._FICHAS = guardado
    quebrou = [a.id for a in achados if a.id.startswith("erro-check-")]
    _erro(erros, not quebrou,
          f"frentes que estouraram com todos os datasets vazios: {quebrou}")


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_nada_ressuscitou, _toda_etapa_roda, _analise_roda_sobre_o_vazio):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
