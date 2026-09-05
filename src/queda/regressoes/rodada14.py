"""Regressões da rodada 14 — TCU como fonte, e a frente de fornecedores sem teto.

Duas mudanças de alcance, não de correção:

* **TCU.** Quem julga as contas de um conselho federal é o Tribunal de Contas
  da União, não a CGU. O CEIS cobre a sanção administrativa do Executivo; a
  declaração de inidoneidade do art. 46 da Lei 8.443/1992 e as contas julgadas
  irregulares (art. 16, III) ficavam inteiramente de fora do relatório.
* **Teto das fichas.** A frente de fornecedores enxergava 149 das ~800
  empresas — 82% do valor, mas quatro grupos econômicos num universo de 149.

Nenhum caminho do TCU foi verificado contra a rede na sessão em que isto foi
escrito (o proxy bloqueia `*.gov.br`). O que estas regressões trancam é o
CONTRATO: sondar candidatos em ordem, aprender o que respondeu, tratar
"nenhum respondeu" como indisponível — e nunca casar pessoa por nome.

Tudo offline: rede substituída por funções locais, disco em pasta temporária.
"""
from __future__ import annotations

import pandas as pd

from .. import clients, config
from ..checks import common as CM
from ..checks import fornecedores as F
from ..http import RateLimited
from .rodada12 import _ambiente, _erro, _trocar

_ORDS = {"items": [{"cpf_cnpj": "12345678000199", "nome": "EMPRESA INIDONEA LTDA",
                    "data_transito_julgado": "2022-01-10", "data_final": "2027-01-10",
                    "processo": "TC 001.234/2021-0", "deliberacao": "Acórdão 1/2022",
                    "uf": "SP"}],
         "hasMore": False}


def _sonda_candidatos(erros: list[str]) -> None:
    """Tenta os caminhos em ordem, aprende o que respondeu, e só devolve None
    quando NENHUM serve — vazio de fonte nunca pode virar "não há inidôneo"."""
    urls: list[str] = []
    candidatos = list(config.TCU_CADASTROS["tcu_inidoneos"])
    if len(candidatos) < 2:
        erros.append("tcu: a lista de candidatos deveria ter alternativas para sondar")
        return

    def _get(url, params=None, **kw):
        urls.append(url)
        # o primeiro candidato responde 404 (None); o segundo serve
        return None if url == candidatos[0] else dict(_ORDS)

    clients._TCU_URL_APRENDIDA.clear()
    with _trocar(clients, get_json=_get):
        linhas, meta = clients.baixar_tcu("tcu_inidoneos")
        _erro(erros, linhas is not None and len(linhas) == 1,
              f"tcu: o segundo candidato respondeu e a leitura devolveu {linhas!r}")
        _erro(erros, meta.get("url") == candidatos[1],
              f"tcu: caminho registrado no meta foi {meta.get('url')!r}")
        _erro(erros, linhas and linhas[0]["documento"] == "12345678000199",
              f"tcu: documento saiu {linhas[0]['documento']!r}" if linhas else "tcu: sem linhas")
        # segunda chamada: o caminho aprendido vem primeiro
        urls.clear()
        clients.baixar_tcu("tcu_inidoneos")
        _erro(erros, urls and urls[0] == candidatos[1],
              f"tcu: não aprendeu o caminho que funcionou (tentou {urls[:1]})")

    clients._TCU_URL_APRENDIDA.clear()
    with _trocar(clients, get_json=lambda url, params=None, **kw: None):
        linhas, _meta = clients.baixar_tcu("tcu_inidoneos")
    _erro(erros, linhas is None,
          f"tcu: nenhum caminho respondeu e a fonte devolveu {linhas!r} — só None "
          "separa 'não conseguimos consultar' de 'não há ninguém na lista'")

    # payload sem lista nenhuma não pode virar zero registros
    clients._TCU_URL_APRENDIDA.clear()
    with _trocar(clients, get_json=lambda url, params=None, **kw: {"erro": "sem rota"}):
        linhas, _meta = clients.baixar_tcu("tcu_inidoneos")
    _erro(erros, linhas is None,
          f"tcu: payload sem lista devolveu {linhas!r}, esperado None")


def _colunas_tolerantes(erros: list[str]) -> None:
    """O layout do TCU não foi lido contra a rede: nada pode exigir nome exato."""
    casos = [
        (["cpf_cnpj", "nome", "data_transito_julgado", "data_final", "processo"],
         {"documento": "cpf_cnpj", "nome": "nome", "inicio": "data_transito_julgado"}),
        (["CPF/CNPJ do Responsável", "Nome do Responsável", "Data de Início",
          "Data Final", "Número do Processo"],
         {"documento": "CPF/CNPJ do Responsável", "nome": "Nome do Responsável",
          "inicio": "Data de Início", "processo": "Número do Processo"}),
    ]
    for colunas, esperado in casos:
        mapa = clients._resolver_colunas_tcu(colunas)
        for campo, valor in esperado.items():
            _erro(erros, mapa.get(campo) == valor,
                  f"tcu: coluna {campo!r} resolvida como {mapa.get(campo)!r}, "
                  f"esperado {valor!r} em {colunas}")


def _inidoneo_do_tcu_alcanca(erros: list[str]) -> None:
    """A inidoneidade declarada pelo TCU vale para toda a administração federal
    — o Crea-SP é autarquia federal (Lei 5.194/1966; STF, ADI 1.717)."""
    from .. import pipeline
    alcance = clients.alcance_sancao(pipeline._CATEGORIA_TCU_INIDONEO,
                                     "Lei 8.443/1992, art. 46", "FEDERAL")
    _erro(erros, alcance == "nacional",
          f"tcu: inidoneidade do art. 46 saiu com alcance {alcance!r} — se não for "
          "'nacional', o cruzamento a trata como contexto e o achado some")


def _pessoa_so_casa_com_documento(erros: list[str]) -> None:
    """Nome igual com documento diferente é homônimo, e homônimo aqui é
    acusação contra terceiro. O par só sai quando os dois confirmam."""
    fichas = {
        "11111111000191": {"razao_social": "ALFA LTDA",
                           "qsa": [{"nome_socio": "JOAO DA SILVA",
                                    "cnpj_cpf_do_socio": "***456789**"}]},
        "22222222000172": {"razao_social": "BETA LTDA",
                           "qsa": [{"nome_socio": "JOAO DA SILVA",
                                    "cnpj_cpf_do_socio": "***999888**"}]},
        "33333333000153": {"razao_social": "GAMA LTDA",
                           "qsa": [{"nome_socio": "MARIA SOUZA"}]},   # sem documento
    }
    resp = pd.DataFrame([
        {"cadastro": "tcu_contas_irregulares", "documento": "***456789**",
         "nome": "JOAO DA SILVA", "inicio": "2022-01-01", "fim": "2030-01-01",
         "processo": "TC 1/2021", "deliberacao": "Acórdão 1/2022", "uf": "SP"},
        {"cadastro": "tcu_inabilitados", "documento": "***555444**",
         "nome": "MARIA SOUZA", "inicio": "2021-01-01", "fim": "2029-01-01",
         "processo": "TC 2/2020", "deliberacao": "Acórdão 2/2021", "uf": "SP"},
    ])
    pg = pd.DataFrame([
        {"TIPO_PESSOA": "PJ", "CPF_CNPJ": "11.111.111/0001-91", "FAVORECIDO_NORM": "ALFA",
         "VALOR": 500_000.0, "DATA": "2024-01-10"},
        {"TIPO_PESSOA": "PJ", "CPF_CNPJ": "22.222.222/0001-72", "FAVORECIDO_NORM": "BETA",
         "VALOR": 900_000.0, "DATA": "2024-02-10"},
        {"TIPO_PESSOA": "PJ", "CPF_CNPJ": "33.333.333/0001-53", "FAVORECIDO_NORM": "GAMA",
         "VALOR": 700_000.0, "DATA": "2024-03-10"},
    ])
    guardado = CM._FICHAS
    try:
        CM._FICHAS = fichas
        achados = {f.id: f for f in F._achados_tcu_responsaveis(
            {"tcu_responsaveis": resp, "pagamentos": pg})}
    finally:
        CM._FICHAS = guardado
    f = achados.get("fornecedores-socio-condenado-tcu")
    if f is None:
        erros.append("tcu/pessoas: o par com nome E documento batendo não foi publicado")
        return
    _erro(erros, f.metricas.get("fornecedores") == 1 and f.metricas.get("socios") == 1,
          f"tcu/pessoas: saiu {f.metricas} — o homônimo (mesmo nome, outro documento) "
          "e o sócio sem documento não podem entrar")
    cnpjs = set(f.evidencia["cnpj_fornecedor"])
    _erro(erros, cnpjs == {"11111111000191"},
          f"tcu/pessoas: fornecedores publicados {cnpjs}, esperado só o do documento que bate")

    # sem ninguém na lista: diz que conferiu, não fica em silêncio
    try:
        CM._FICHAS = fichas
        vazio = F._achados_tcu_responsaveis(
            {"tcu_responsaveis": resp.iloc[0:0], "pagamentos": pg})
    finally:
        CM._FICHAS = guardado
    _erro(erros, vazio == [],
          "tcu/pessoas: lista vazia do TCU não deveria gerar achado nenhum "
          "(a indisponibilidade é do diagnóstico, não desta frente)")


def _fichas_sem_teto(erros: list[str]) -> None:
    """Sem teto, todo fornecedor acima do piso é consultado — e um bloqueio no
    meio para a consulta em vez de custar a rodada inteira em espera."""
    _erro(erros, config.MAX_CONSULTAS_CNPJ is None,
          f"fichas: o teto voltou ({config.MAX_CONSULTAS_CNPJ}) — a rede de sócios "
          "volta a enxergar só os maiores")
    pg = pd.DataFrame([
        {"TIPO_PESSOA": "PJ", "CPF_CNPJ": f"{i:010d}0191", "PF": False,   # 14 dígitos
         "FAVORECIDO_NORM": f"FORN {i}", "VALOR": 1_000_000.0 - i, "DATA": "2024-01-10"}
        for i in range(1, 301)])
    consultados: list[str] = []

    def _consulta(cnpj):
        consultados.append(cnpj)
        return {"razao_social": f"FORN {cnpj}"}

    guardado = CM._FICHAS
    try:
        CM._FICHAS = None
        with _trocar(clients, consultar_cnpj=_consulta):
            fichas = CM.fichas_cnpj({"pagamentos": pg})
        _erro(erros, len(fichas) == 300,
              f"fichas: com 300 fornecedores acima do piso vieram {len(fichas)} — "
              "o teto de 150 ainda está cortando")

        # bloqueio no meio: guarda o que veio e segue
        consultados.clear()
        CM._FICHAS = None

        def _bloqueia(cnpj):
            consultados.append(cnpj)
            if len(consultados) > 10:
                raise RateLimited("HTTP 429 persistente")
            return {"razao_social": "X"}

        with _trocar(clients, consultar_cnpj=_bloqueia):
            fichas = CM.fichas_cnpj({"pagamentos": pg})
        _erro(erros, len(fichas) == 10,
              f"fichas: bloqueio no meio deveria manter as 10 já obtidas, ficou com "
              f"{len(fichas)}")
    finally:
        CM._FICHAS = guardado


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_sonda_candidatos, _colunas_tolerantes, _inidoneo_do_tcu_alcanca,
                  _pessoa_so_casa_com_documento, _fichas_sem_teto):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
