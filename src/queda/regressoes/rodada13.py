"""Regressões da rodada 13 — o que o corpo do 4xx da rodada 12 revelou.

A rodada 12 foi a primeira a guardar o corpo das respostas 4xx, e o primeiro
corpo guardado resolveu um vazio de seis rodadas:

    {"message":"Período inicial e final maior que 30 dias.","status":"422"}

`pncp_cobrancas` nunca teve uma linha porque mandávamos janela ANUAL a um
endpoint que aceita 30 dias. Ninguém sabia: o dataset saía vazio, e vazio sem
corpo de erro é indistinguível de "o órgão não registrou nota nenhuma".

O segundo defeito é o que impedia a correção de valer: a etapa `pncp_contratos`
se dava por pronta pelos 145 contratos em disco e nunca voltaria para as notas,
por mais correto que o cliente ficasse.

Tudo offline: rede substituída por funções locais, disco em pasta temporária.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from .. import clients, config, pipeline
from ..checks import contratos as C
from .rodada12 import _ambiente, _diagnostico_limpo, _erro, _trocar


def _janela_de_trinta_dias(erros: list[str]) -> None:
    """O cliente das notas fiscais nunca pode pedir mais de 30 dias."""
    pedidos: list[dict] = []

    def _get_json(url, params=None, **kw):
        pedidos.append(dict(params or {}))
        return {"data": [{"sequencialContrato": 1,
                          "notaFiscalEletronica": {"numero": "1",
                                                   "niEmitente": "55666777000188",
                                                   "valorNotaFiscal": 10.0},
                          "recuperarContratoDTO": {
                              "numeroControlePNCP": "x-2-000007/2023"}}],
                "paginasRestantes": 0}

    with _trocar(clients, get_json=_get_json):
        rows = clients.pncp_instrumentos_cobranca("60985017000177", 2024, 2024)
    _erro(erros, len(rows) == 12,
          f"cobranças: 12 meses de 2024 deveriam dar 12 janelas, deram {len(rows)}")
    for p in pedidos:
        ini, fim = str(p.get("dataInicial", "")), str(p.get("dataFinal", ""))
        if not (len(ini) == 8 and len(fim) == 8):
            erros.append(f"cobranças: janela mal formada {ini!r}–{fim!r}")
            continue
        dias = (date(int(fim[:4]), int(fim[4:6]), int(fim[6:]))
                - date(int(ini[:4]), int(ini[4:6]), int(ini[6:]))).days
        if dias > 30:
            erros.append(f"cobranças: janela de {dias} dias ({ini}–{fim}) — o PNCP "
                         "recusa qualquer janela maior que 30 dias (422)")
            break

    # 4xx na primeira página é "não coletamos", nunca "não há nota"
    with _trocar(clients, get_json=lambda url, params=None, **kw: None):
        um_mes = clients.pncp_instrumentos_cobranca_mes(
            "60985017000177", "20240101", "20240131")
    _erro(erros, um_mes is None,
          f"cobranças: janela recusada devolveu {um_mes!r} — None é o que separa "
          "'não coletamos' de 'não há nota'")


def _secundario_puxa_a_etapa(erros: list[str]) -> None:
    """Etapa com o dataset principal em disco e o secundário nunca coletado
    não pode se dar por pronta: foi assim que as notas fiscais ficaram seis
    rodadas fora, mesmo depois de a etapa passar a saber buscá-las."""
    with _ambiente(checkpoint="1"):
        pd.DataFrame([{"orgao_papel": "alvo", "fornecedor_ni": "55666777000188",
                       "numeroControlePNCP": "x-2-000007/2023", "valorGlobal": 10.0,
                       "tipoContrato_nome": "Contrato (termo inicial)", "_ano": 2024}
                      ]).to_csv(config.STAGED_DIR / "pncp_contratos.csv", index=False)
        pronto, pend = pipeline.etapa_pronta("pncp_contratos", 2024, 2024)
        _erro(erros, pronto is None and any("pncp_cobrancas" in str(x) for x in pend),
              f"etapa_pronta: com pncp_cobrancas nunca coletado a etapa saiu pronta "
              f"({pend=})")

        # secundário que o servidor respondeu (mesmo vazio) deixa checkpoint e
        # não pode segurar a etapa para sempre
        (config.RAW_DIR / pipeline.CHECKPOINT_SUBDIR / "pncp_cobrancas"
         / "60985017000177").mkdir(parents=True)
        (config.RAW_DIR / pipeline.CHECKPOINT_SUBDIR / "pncp_cobrancas"
         / "60985017000177" / "202401.vazio").write_text("2026-09-05T09:00:00\n")
        pronto, pend = pipeline.etapa_pronta("pncp_contratos", 2024, 2024)
        _erro(erros, not any("pncp_cobrancas" in str(x) for x in pend),
              f"etapa_pronta: secundário com checkpoint ainda conta como pendente ({pend=})")


def _atas_repetidas_no_disco(erros: list[str]) -> None:
    """CSV gravado por versão anterior traz a mesma ata uma vez por mês de
    vigência (138 linhas para 8 atas na rodada 11). A etapa não volta a rodar —
    os meses fechados são checkpoint permanente —, então o reparo é na leitura."""
    with _ambiente():
        linhas = []
        for mes in range(1, 13):
            for ata in ("00002", "00003"):
                linhas.append({"numeroControlePNCPAta": f"60985017000177-1-{ata}/2024",
                               "numeroAtaRegistroPreco": ata, "anoAta": 2024,
                               "vigenciaInicio": f"2024-{mes:02d}-01", "_ano": 2024})
        pd.DataFrame(linhas).to_csv(config.STAGED_DIR / "pncp_atas.csv", index=False)
        df = pipeline._ja_coletado("pncp_atas", exigir_esquema=False)
        _erro(erros, df is not None and len(df) == 2,
              f"pncp_atas: 24 linhas de 2 atas deveriam virar 2, viraram "
              f"{None if df is None else len(df)}")


def _veredito_404(erros: list[str]) -> None:
    """404 é "não sabemos", nunca "o órgão não publica".

    Era a fixture do selftest que trancava isto, através de `pncp_cobrancas`
    vazio; a rodada 13 fez a etapa funcionar, e o caso passou a ser testado
    aqui, direto sobre a regra."""
    from ..checks import diagnostico as D
    ledger = pd.DataFrame([
        {"caminho": "/api/dadosabertos/indenizacoes", "status": 404,
         "resultado": "inexistente", "itens": 0, "params": "", "ts": "2026-09-05T09:00:00"}
        for _ in range(2)])
    causa = D._causa(ledger, retomado=False)
    _erro(erros, causa == "endpoint inexistente (404)",
          f"404 deu a causa {causa!r}")
    _erro(erros, D.CAUSAS[causa][0] == D.INDETERMINADO,
          f"404 saiu com veredito {D.CAUSAS[causa][0]!r} — caminho que não existe "
          "não autoriza dizer nada sobre o que o órgão publica")


def _faturamento(erros: list[str]) -> None:
    """A nota fiscal cruzada com o contrato — e a regra que separa filial de
    terceiro. Sem ela, toda empresa com mais de um CNPJ vira acusação."""
    pncp = pd.DataFrame([
        {"orgao_papel": "alvo", "fornecedor_ni": "55666777000188",
         "fornecedor_nome": "ELETRICA PREDIAL LTDA", "numeroContrato": "7/2023",
         "numeroControlePNCP": "x-2-000007/2023", "valorGlobal": 2_400_000.0},
    ])
    cob = pd.DataFrame([
        {"numeroControlePNCP": "x-2-000007/2023", "emitente_ni": "55666777000188",
         "valorNota": 900_000.0, "numeroNota": "1"},
        # filial: mesma raiz, não é terceiro
        {"numeroControlePNCP": "x-2-000007/2023", "emitente_ni": "55666777000269",
         "valorNota": 600_000.0, "numeroNota": "2"},
        # outra raiz: é terceiro
        {"numeroControlePNCP": "x-2-000007/2023", "emitente_ni": "11222333000144",
         "valorNota": 750_000.0, "numeroNota": "3"},
    ])
    achados = {f.id: f for f in C._faturamento_pncp(
        {"pncp_cobrancas": cob, "pncp_contratos": pncp})}
    f = achados.get("contratos-faturamento-pncp")
    _erro(erros, f is not None and f.metricas.get("contratos_com_nota") == 1
          and f.metricas.get("faturado_desses") == 2_250_000.0,
          f"faturamento: cruzamento saiu {None if f is None else f.metricas}")
    t = achados.get("contratos-nota-de-terceiro")
    _erro(erros, t is not None and t.metricas.get("notas") == 1
          and t.metricas.get("valor") == 750_000.0,
          f"terceiro: deveria ser 1 nota de 750 mil (a filial NÃO conta), saiu "
          f"{None if t is None else t.metricas}")

    # chave de ligação vazia: diz que não deu para cruzar, não inventa cruzamento
    sem = C._faturamento_pncp({"pncp_cobrancas": cob.assign(numeroControlePNCP=""),
                               "pncp_contratos": pncp})
    _erro(erros, len(sem) == 1 and sem[0].id == "contratos-faturamento-sem-chave"
          and sem[0].gravidade == "erro",
          f"faturamento sem chave: saiu {[(a.id, a.gravidade) for a in sem]}")


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_janela_de_trinta_dias, _secundario_puxa_a_etapa,
                  _atas_repetidas_no_disco, _veredito_404, _faturamento):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
