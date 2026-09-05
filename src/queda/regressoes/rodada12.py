"""Regressões da rodada 12 — o que a rodada 11 mostrou quebrado.

* CNPJ que passou por float: `_ja_coletado` (o caminho de `main.py all` para
  toda etapa pronta) relia o staged sem `dtype`, CONTRATADA_CNPJ virava
  float64 e "72843212000141.0" perdia o ponto → 15 dígitos. A frente de
  fornecedores ficou com 0,4% do valor "legível" (era 82%) e 44 contratos
  saíram como ausentes do PNCP. Leitor único + normalizador tolerante.
* `stage_pncp` abortava inteira com 503 na consulta de um órgão RELACIONADO.
* `stage_pncp_atas` devolvia a mesma ata uma vez por mês de vigência (138
  linhas para 8 atas na rodada 11).
* recusa do exercício em curso gravada com "12/2026" segurava a janela por
  12 h depois de a pergunta já ter mudado para o mês corrente.
* diagnóstico dizia "sem registro de tentativa" para fonte cujos checkpoints
  `.vazio` de rodadas anteriores eram a própria evidência.

Tudo offline: rede substituída por funções locais, disco em pasta temporária.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

from .. import clients, config, pipeline
from ..http import RateLimited
from ..utils import cnpj14, so_digitos


def _erro(erros: list[str], ok: bool, msg: str) -> None:
    if not ok:
        erros.append(msg)


@contextmanager
def _ambiente(checkpoint: str = "1"):
    """Disco em pasta temporária e checkpoint ligado/desligado; tudo restaurado."""
    guardado = (config.DATA_DIR, config.RAW_DIR, config.STAGED_DIR, config.CACHE_DIR,
                os.environ.get("QUEDA_CHECKPOINT"))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            base = Path(tmp)
            config.DATA_DIR = base
            config.RAW_DIR = base / "raw"
            config.STAGED_DIR = base / "staged"
            config.CACHE_DIR = base / "cache"
            for p in (config.RAW_DIR, config.STAGED_DIR, config.CACHE_DIR):
                p.mkdir(parents=True, exist_ok=True)
            os.environ["QUEDA_CHECKPOINT"] = checkpoint
            yield base
        finally:
            (config.DATA_DIR, config.RAW_DIR, config.STAGED_DIR, config.CACHE_DIR,
             antes) = guardado
            if antes is None:
                os.environ.pop("QUEDA_CHECKPOINT", None)
            else:
                os.environ["QUEDA_CHECKPOINT"] = antes


@contextmanager
def _trocar(modulo, **atributos):
    """Substitui atributos de um módulo e devolve os originais no fim."""
    originais = {k: getattr(modulo, k) for k in atributos}
    try:
        for k, v in atributos.items():
            setattr(modulo, k, v)
        yield
    finally:
        for k, v in originais.items():
            setattr(modulo, k, v)


@contextmanager
def _diagnostico_limpo():
    """`pipeline._DIAGNOSTICO` é global: uma asserção que estoure no meio do
    teste deixaria linhas falsas para o selftest seguinte."""
    pipeline._DIAGNOSTICO.clear()
    try:
        yield
    finally:
        pipeline._DIAGNOSTICO.clear()


# ------------------------------------------------------------------ casos
def _normalizador(erros: list[str]) -> None:
    casos = {
        "1816098000112.0": "1816098000112",          # float de CNPJ com zero à esquerda
        "72843212000141.0": "72843212000141",
        "6.0985017000177e+13": "60985017000177",
        "00.000.424/0001-56": "00000424000156",
        "123.456.789-01": "12345678901",
        1816098000112.0: "1816098000112",
        float("nan"): "",
        None: "",
    }
    for entrada, esperado in casos.items():
        got = so_digitos(entrada)
        _erro(erros, got == esperado, f"so_digitos({entrada!r}) = {got!r}, esperado {esperado!r}")
    got = cnpj14(pd.Series(["1816098000112.0", "72843212000141.0", "00.000.424/0001-56",
                            "424000156.0", "123.456.789-01", "", None])).tolist()
    esperado = ["01816098000112", "72843212000141", "00000424000156",
                "424000156", "12345678901", "", ""]
    _erro(erros, got == esperado, f"cnpj14 devolveu {got}, esperado {esperado}")
    got = cnpj14(pd.Series(["424000156.0", "191", "123.456.789-01", "123456"]),
                 presumir_cnpj=True).tolist()
    esperado = ["00000424000156", "00000000000191", "12345678901", "00000000123456"]
    _erro(erros, got == esperado, f"cnpj14(presumir_cnpj) devolveu {got}, esperado {esperado}")


def _leitor_staged(erros: list[str]) -> None:
    """CSV staged gravado por versão anterior, com CNPJ que passou por float."""
    with _ambiente():
        pd.DataFrame([
            {"NUMERO_CONTRATO": "C-025/2024",
             "CONTRATADA": "72.843.212/0001-41 - CIRION TECHNOLOGIES DO BRASIL LTDA",
             "CONTRATADA_CNPJ": "72843212000141.0", "VALOR": 36268493.66},
            {"NUMERO_CONTRATO": "C-019/2024",
             "CONTRATADA": "01.816.098/0001-12 - NC SERVICOS LTDA",
             "CONTRATADA_CNPJ": "01816098000112", "VALOR": 18197476.0},
            {"NUMERO_CONTRATO": "C-007/2024", "CONTRATADA": "JOSE DA SILVA",
             "CONTRATADA_CNPJ": "", "VALOR": 1000.0},
        ]).to_csv(config.STAGED_DIR / "contratos.csv", index=False)
        pd.DataFrame([
            {"CPF_CNPJ": "72.843.212/0001-41", "NOME_RAZAO_SOCIAL": "CIRION",
             "FAVORECIDO_CNPJ": "72843212000141.0", "VALOR": 10.0, "TIPO_PESSOA": "PJ"},
            {"CPF_CNPJ": "00.360.305/0689-22", "NOME_RAZAO_SOCIAL": "CAIXA",
             "FAVORECIDO_CNPJ": "360305068922.0", "VALOR": 20.0, "TIPO_PESSOA": "PJ"},
            {"CPF_CNPJ": "", "NOME_RAZAO_SOCIAL": "DIVERSOS FUNCIONARIOS",
             "FAVORECIDO_CNPJ": "", "VALOR": 30.0, "TIPO_PESSOA": "AGREGADO"},
        ]).to_csv(config.STAGED_DIR / "pagamentos.csv", index=False)

        ctr = pipeline._ja_coletado("contratos", exigir_esquema=False)
        _erro(erros, ctr is not None and (pd.api.types.is_object_dtype(ctr["CONTRATADA_CNPJ"])
                                          or pd.api.types.is_string_dtype(ctr["CONTRATADA_CNPJ"])),
              "_ja_coletado: CONTRATADA_CNPJ não veio como texto")
        if ctr is not None:
            got = ctr["CONTRATADA_CNPJ"].fillna("").tolist()
            _erro(erros, got == ["72843212000141", "01816098000112", ""],
                  f"_ja_coletado: CONTRATADA_CNPJ saiu {got} — o float de rodada anterior "
                  "não foi reparado ou o zero à esquerda sumiu")
            _erro(erros, float(ctr["VALOR"].sum()) > 54_000_000,
                  "_ja_coletado: VALOR deixou de ser numérico (dtype=str no arquivo inteiro?)")
        pg = pipeline._ja_coletado("pagamentos", exigir_esquema=False)
        if pg is None:
            erros.append("_ja_coletado: pagamentos não carregou")
        else:
            got = pg["FAVORECIDO_CNPJ"].fillna("").tolist()
            _erro(erros, got == ["72843212000141", "00360305068922", ""],
                  f"_ja_coletado: FAVORECIDO_CNPJ saiu {got} — deveria ser recomposto de CPF_CNPJ")
            _erro(erros, pg["CPF_CNPJ"].iloc[1] == "00.360.305/0689-22",
                  "_ja_coletado: CPF_CNPJ perdeu o formato/zero à esquerda")
        tudo = pipeline.load_staged()
        _erro(erros, "contratos" in tudo and tudo["contratos"]["CONTRATADA_CNPJ"].tolist()[0] == "72843212000141",
              "load_staged: não passou pelo mesmo leitor que _ja_coletado")


def _pagamentos_pj(erros: list[str]) -> None:
    """Coluna derivada podre em quase toda linha + coluna-fonte boa: linha a linha."""
    from ..checks import fornecedores
    pg = pd.DataFrame([
        {"TIPO_PESSOA": "PJ", "CPF_CNPJ": "72.843.212/0001-41", "FAVORECIDO_NORM": "CIRION",
         "FAVORECIDO_CNPJ": "72843212000141.0", "VALOR": 1000.0, "DATA": "2024-01-10"},
        {"TIPO_PESSOA": "PJ", "CPF_CNPJ": "01.816.098/0001-12", "FAVORECIDO_NORM": "NC",
         "FAVORECIDO_CNPJ": "1816098000112.0", "VALOR": 2000.0, "DATA": "2024-02-10"},
        {"TIPO_PESSOA": "PJ", "CPF_CNPJ": "00.360.305/0689-22", "FAVORECIDO_NORM": "CAIXA",
         "FAVORECIDO_CNPJ": "360305068922.0", "VALOR": 3000.0, "DATA": "2024-03-10"},
        {"TIPO_PESSOA": "PF", "CPF_CNPJ": "123.456.789-01", "FAVORECIDO_NORM": "FULANO",
         "FAVORECIDO_CNPJ": "12345678901", "VALOR": 400.0, "DATA": "2024-03-11"},
        {"TIPO_PESSOA": "PJ", "CPF_CNPJ": "33.665.647/0001-91", "FAVORECIDO_NORM": "CONFEA",
         "FAVORECIDO_CNPJ": "33665647000191", "VALOR": 5000.0, "DATA": "2024-04-10"},
    ])
    d = fornecedores._pagamentos_pj({"pagamentos": pg})
    got = sorted(d["cnpj"].tolist()) if not d.empty else []
    esperado = ["00360305068922", "01816098000112", "72843212000141"]
    _erro(erros, got == esperado,
          f"_pagamentos_pj: cnpj saiu {got}, esperado {esperado} (PF fora, Confea fora, "
          "os três PJ de mercado com 14 dígitos recompostos da coluna-fonte)")
    _erro(erros, d.empty or float(d["valor"].sum()) == 6000.0,
          f"_pagamentos_pj: valor {d['valor'].sum() if not d.empty else 0} ≠ 6000")


def _pncp_tolerante(erros: list[str]) -> None:
    """503 na consulta do Confea não pode derrubar os contratos do alvo."""
    alvo = {"cnpj": "60985017000177", "razaoSocial": "CREA-SP", "_verificado": True}

    def resolver(razao, **kw):
        if "SAO PAULO" in razao:
            return dict(alvo)
        raise RuntimeError("Falha após retries: https://pncp.gov.br/api/pncp/v1/orgaos/33665647000191")

    def contratos_mes(cnpj, ini, fim):
        return [{"numeroControlePNCP": "60985017000177-2-000001/2025",
                 "numeroControlePncpCompra": "60985017000177-1-000010/2025",
                 "niFornecedor": "72843212000141", "nomeRazaoSocialFornecedor": "CIRION",
                 "valorGlobal": 1000.0, "valorInicial": 1000.0, "anoContrato": 2025,
                 "numeroContratoEmpenho": "1/2025", "tipoPessoa": "PJ",
                 "dataAssinatura": "2025-01-10", "dataVigenciaInicio": "2025-01-10",
                 "dataVigenciaFim": "2025-12-31",
                 "tipoContrato": {"id": 1, "nome": "Contrato (termo inicial)"},
                 "unidadeOrgao": {"codigoUnidade": "1"}}]

    def cobranca(cnpj, ini, ate):
        raise RuntimeError("Falha após retries: instrumentoscobranca")

    with _ambiente(checkpoint="0"), _trocar(
            clients, pncp_resolver_orgao=resolver, pncp_contratos_mes=contratos_mes,
            pncp_instrumentos_cobranca=cobranca,
            pncp_meses=lambda ini, ate: [("20250101", "20250131")]), _diagnostico_limpo():
        try:
            df = pipeline.stage_pncp(2025, 2025)
        except Exception as e:                        # noqa: BLE001
            erros.append(f"stage_pncp estourou com órgão relacionado indisponível: "
                         f"{type(e).__name__}: {e}")
            return
        _erro(erros, len(df) == 1 and df["fornecedor_ni"].iloc[0] == "72843212000141",
              f"stage_pncp: contratos do alvo não sobreviveram ({len(df)} linha(s))")
        diag = {d["dataset"]: d for d in pipeline._DIAGNOSTICO}
        nota = str(diag.get("pncp_contratos", {}).get("indisponivel", ""))
        _erro(erros, "CONSELHO FEDERAL" in nota.upper() and "cobran" in nota.lower(),
              f"stage_pncp: o diagnóstico não registrou os órgãos/etapas não consultados ({nota!r})")
        orgaos = pd.read_csv(config.STAGED_DIR / "pncp_orgaos.csv", dtype=str)
        _erro(erros, len(orgaos) == 1 and orgaos["papel"].iloc[0] == "alvo",
              "stage_pncp: pncp_orgaos deveria ter só o alvo")

    # Bloqueio por ritmo é outra coisa: tem de SUBIR e parar a rodada, senão
    # a coleta segue batendo nos outros quatro órgãos (~11 min de backoff cada)
    # contra um host que já mandou parar. `RateLimited` herda de RuntimeError,
    # e era exatamente por isso que o `except RuntimeError` a engolia.
    def bloqueado(razao, **kw):
        if "SAO PAULO" in razao:
            return dict(alvo)
        raise RateLimited("HTTP 429 persistente")

    with _ambiente(checkpoint="0"), _trocar(
            clients, pncp_resolver_orgao=bloqueado, pncp_contratos_mes=contratos_mes,
            pncp_instrumentos_cobranca=cobranca,
            pncp_meses=lambda ini, ate: [("20250101", "20250131")]), _diagnostico_limpo():
        try:
            pipeline.stage_pncp(2025, 2025)
            erros.append("stage_pncp: RateLimited de um órgão relacionado foi engolida "
                         "— o bloqueio por ritmo tem de parar a rodada")
        except RateLimited:
            pass
        except Exception as e:                        # noqa: BLE001
            erros.append(f"stage_pncp: RateLimited virou {type(e).__name__}: {e}")


def _atas_sem_duplicata(erros: list[str]) -> None:
    alvo = {"cnpj": "60985017000177", "razaoSocial": "CREA-SP", "_verificado": True}
    ata = {"numeroControlePNCPAta": "60985017000177-1-000001/2024-000001",
           "numeroAtaRegistroPreco": "00002", "anoAta": 2024,
           "numeroControlePncpCompra": "60985017000177-1-000001/2024",
           "dataAssinatura": "2024-08-08", "vigenciaInicio": "2024-08-21",
           "vigenciaFim": "2026-08-20", "dataPublicacaoPncp": "2024-08-09",
           "objetoContratacao": "Registro de preços", "cancelado": False}
    meses = [("20240801", "20240831"), ("20240901", "20240930"), ("20241001", "20241031")]
    with _ambiente(checkpoint="0"), _trocar(
            clients, pncp_resolver_orgao=lambda razao, **kw: dict(alvo),
            pncp_atas_mes=lambda cnpj, ini, fim: [dict(ata)],
            pncp_meses=lambda ini, ate: meses), _diagnostico_limpo():
        df = pipeline.stage_pncp_atas(2024, 2024)
        _erro(erros, len(df) == 1,
              f"stage_pncp_atas: a mesma ata vigente em 3 meses saiu {len(df)} vez(es)")
        _erro(erros, not df.empty and int(df["_ano"].iloc[0]) == 2024,
              "stage_pncp_atas: _ano deveria vir de anoAta")


def _assinatura_do_exercicio_corrente(erros: list[str]) -> None:
    ano = datetime.now().year
    nova = pipeline._assinatura_etapa("empenhos", ano)
    _erro(erros, ";fim=" in nova, f"assinatura do exercício em curso sem o mês final: {nova!r}")
    _erro(erros, "fim=" not in pipeline._assinatura_etapa("empenhos", 2020),
          "assinatura de exercício encerrado não deveria ter mês final")
    # e estável entre PROCESSOS: se assinasse com o mês que `implanta_ano`
    # aprendeu no meio da rodada, a rodada seguinte leria outra pergunta e o
    # TTL de 12 h cairia sozinho.
    guardado = dict(clients._FIM_CORRENTE_APRENDIDO)
    try:
        clients._FIM_CORRENTE_APRENDIDO["mes"] = "06"
        _erro(erros, pipeline._assinatura_etapa("empenhos", ano) == nova,
              "assinatura mudou porque o mês final foi aprendido no meio da rodada — "
              "a recusa gravada agora pareceria de outra pergunta na rodada seguinte")
    finally:
        clients._FIM_CORRENTE_APRENDIDO.clear()
        clients._FIM_CORRENTE_APRENDIDO.update(guardado)
    with _ambiente(checkpoint="1"):
        pipeline._marcar_recusa("empenhos", str(ano), "referenciaInicio,referenciaTermino")
        _erro(erros, not pipeline._recusa_fresca("empenhos", str(ano), ano, nova),
              "recusa gravada com a pergunta antiga (12/AAAA) segurou a janela do exercício em curso")
        pipeline._marcar_recusa("empenhos", str(ano), nova)
        _erro(erros, pipeline._recusa_fresca("empenhos", str(ano), ano, nova),
              "recusa gravada com a MESMA pergunta deveria valer 12 h")


def _inventario_coerente(erros: list[str]) -> None:
    """`faltando` e o laço de coleta têm de dizer a MESMA coisa do mesmo ano.

    Um `.recusado` gravado com a pergunta antiga ("12/2026") é pergunta
    diferente: o laço vai à rede, e o inventário — que não passava assinatura —
    contava a mesma janela como recusada pelo portal. A rodada 11 deixou esse
    marcador em toda etapa anual."""
    ano = datetime.now().year
    with _ambiente(checkpoint="1"):
        pipeline._marcar_recusa("empenhos", str(ano), "referenciaInicio,referenciaTermino")
        pendentes = pipeline.janelas_pendentes("empenhos", ano, ano)
        estado = pipeline._janela_estado("empenhos", str(ano), ano,
                                         pipeline._assinatura_etapa("empenhos", ano))
        _erro(erros, ano in pendentes and estado is None,
              f"assinatura antiga: o laço deveria ir à rede ({pendentes=}, {estado=})")
        inv = pipeline.inventario(ano, ano)          # DataFrame
        alvo = inv[inv["etapa"] == "empenhos"]        # a coluna se chama `etapa`
        linha = None if alvo.empty else alvo.iloc[0]
        if linha is None:
            erros.append("inventario: etapa empenhos sumiu do inventário")
        else:
            _erro(erros, not linha["janelas_recusadas"],
                  f"inventario: contou como recusada uma janela que o laço vai buscar "
                  f"({linha['janelas_recusadas']} recusada(s), "
                  f"vai_a_rede={linha['vai_a_rede']}, "
                  f"recusados_pelo_portal={linha['recusados_pelo_portal']!r})")


def _diagnostico_com_memoria(erros: list[str]) -> None:
    from ..checks import diagnostico
    with _ambiente(checkpoint="1"):
        pasta = config.RAW_DIR / pipeline.CHECKPOINT_SUBDIR / "licitacoes"
        pasta.mkdir(parents=True)
        for ano in (2024, 2025):
            (pasta / f"{ano}.vazio").write_text("2026-09-04T22:00:00\n")
        (pasta / "2026.recusado").write_text("2026-09-04T22:21:24|1|referenciaInicio,referenciaTermino\n")
        causa = diagnostico._causa_checkpoint("licitacoes")
        _erro(erros, causa is not None and causa[0].startswith("janelas respondidas vazias"),
              f"diagnóstico: com .vazio no disco a causa saiu {causa!r}")
        if causa:
            _erro(erros, "2 janela(s) 200 []" in causa[1] and "2024–2025" in causa[1]
                  and "1 recusada(s)" in causa[1],
                  f"diagnóstico: resumo dos marcadores incompleto: {causa[1]!r}")
        _erro(erros, diagnostico._causa_checkpoint("atas") is None,
              "diagnóstico: fonte sem pasta de checkpoint deveria devolver None")

        # só recusa, nenhuma vazia: é falha nossa, não falta de publicação
        pasta2 = config.RAW_DIR / pipeline.CHECKPOINT_SUBDIR / "convenios"
        pasta2.mkdir(parents=True)
        (pasta2 / "2026.recusado").write_text("2026-09-04T22:21:24|1|x\n")
        causa2 = diagnostico._causa_checkpoint("convenios")
        _erro(erros, causa2 is not None and causa2[0].startswith("janelas recusadas"),
              f"diagnóstico: só .recusado deveria dar 'janelas recusadas', saiu {causa2!r}")

        # dado no checkpoint e dataset vazio: o parser é que perdeu — nunca
        # pode sair como "o órgão não publica"
        pasta3 = config.RAW_DIR / pipeline.CHECKPOINT_SUBDIR / "contratos"
        pasta3.mkdir(parents=True)
        (pasta3 / "2024.csv").write_text("NUMERO_CONTRATO\nC-001/2024\n")
        (pasta3 / "2025.vazio").write_text("2026-09-04T22:00:00\n")
        causa3 = diagnostico._causa_checkpoint("contratos")
        _erro(erros, causa3 is not None
              and causa3[0] == "resposta recebida e descartada no staging",
              f"diagnóstico: checkpoint COM dado e staged vazio é falha nossa, saiu {causa3!r}")
        _erro(erros, causa3 is None
              or diagnostico.CAUSAS[causa3[0]][0] == diagnostico.NAO_COLETAMOS,
              "diagnóstico: dado perdido no staging não pode virar veredito sobre o órgão")

        # integração: o achado publica veredito de falta de publicação, não "indeterminado"
        ledger = pd.DataFrame([{
            "ts": "2026-09-05T06:58:35", "host": "crea-sp.implanta.net.br",
            "caminho": "/transparencia/api/v1.0/QuadrosPessoas", "params": "",
            "status": 200, "resultado": "vazio", "tentativa": 1, "cache": False,
            "itens": 0, "duracao_s": 0.4}])
        diag = pd.DataFrame([{"dataset": "licitacoes", "linhas": 0, "retomado": ""}])
        achados = diagnostico._porque_vazio(
            {"_coleta_http": ledger, "licitacoes": pd.DataFrame()}, diag, ["licitacoes"])
        f = next((a for a in achados if a.id == "diagnostico-porque-vazio"), None)
        _erro(erros, f is not None and f.metricas.get("sem_publicacao") == 1
              and f.metricas.get("indeterminadas") == 0,
              "diagnóstico: licitações com 13 exercícios de 200 [] no checkpoint saiu "
              f"{None if f is None else f.metricas}")
        if f is not None and f.evidencia is not None and not f.evidencia.empty:
            linha = f.evidencia.iloc[0]
            _erro(erros, str(linha["causa_provavel"]).startswith("janelas respondidas vazias")
                  and "recusada" in str(linha["causa_provavel"]),
                  "diagnóstico: com janelas vazias E recusadas, a causa tem de dizer as duas "
                  f"coisas — saiu {linha['causa_provavel']!r}")
            _erro(erros, str(linha["status_observados"]).startswith("checkpoint:"),
                  "diagnóstico: a evidência tem de dizer que a causa veio do checkpoint")


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_normalizador, _leitor_staged, _pagamentos_pj, _pncp_tolerante,
                  _atas_sem_duplicata, _assinatura_do_exercicio_corrente,
                  _inventario_coerente, _diagnostico_com_memoria):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
