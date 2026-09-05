"""Regressões da rodada 18 — a rodada em que o TCU respondeu.

Três coisas apareceram na primeira rodada em que os acórdãos passaram (os
cabeçalhos por host resolveram o 499 "Acesso Bloqueado"; 21 mil acórdãos
varridos, 3 sobre o conselho):

* **A ementa vale mais que a contagem.** "3 acórdãos citam o conselho" é
  verdadeiro e não faz ninguém abrir nada. Dois deles falam em multa, tomada de
  contas especial e apuração de eventual dano ao erário — e é isso que o
  relatório tem de dizer, com a ressalva de que ementa é resumo: o que decide é
  o dispositivo, e o link vai nele. A distância entre "a ementa menciona" e "o
  Tribunal decidiu" é a distância entre uma pista e uma afirmação que não temos
  como sustentar.
* **`trecho` cortado em 600 caracteres decapitava o dispositivo.** É no fim da
  ementa que estão multa, débito e tomada de contas especial.
* **`pncp_cobrancas` sumia do diagnóstico.** A etapa `pncp_contratos` é
  reaproveitada do disco desde a rodada 13, e etapa reaproveitada não chama
  `_salvar` — que é quem escreve o diagnóstico. O dataset secundário ficava
  vazio, invisível para `_porque_vazio`, e o relatório em silêncio sobre a
  ausência do cruzamento nota fiscal × contrato. Sete rodadas sem ninguém
  perguntar por quê.

Tudo offline: rede substituída por funções locais, disco em pasta temporária.
"""
from __future__ import annotations

import inspect

import pandas as pd

from .. import clients, config, pipeline
from ..checks import diagnostico as D
from ..checks import tcu as T
from .rodada12 import _ambiente, _diagnostico_limpo, _erro, _trocar

_EMENTA_CONDENATORIA = (
    "ACORDAO 2379/2026 MONITORAMENTO. DETERMINACOES DO TCU. CREA-SP. "
    "LEVANTAMENTO DE QUANTITATIVOS E APURACAO DE EVENTUAL DANO AO ERARIO. "
    "INSTAURACAO DE TOMADA DE CONTAS ESPECIAL. COMPROVACAO PARCIAL DO "
    "CUMPRIMENTO. DESCUMPRIMENTO REITERADO DE DETERMINACAO. NAO ATENDIMENTO "
    "A AUDIENCIA. MULTA. FIXACAO DE NOVO PRAZO. ALERTA.")
_EMENTA_DE_PROCEDIMENTO = (
    "ACORDAO 1513/2026 DENUNCIAS E REPRESENTACAO. CONCORRENCIA ELETRONICA. "
    "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO ESTADO DE SAO PAULO. "
    "CONTRATACAO SEMI-INTEGRADA PARA CONSTRUCAO DE NOVA SEDE.")


def _acordaos(ementas) -> pd.DataFrame:
    return pd.DataFrame([
        {"numero": str(1000 + i), "ano": 2026, "colegiado": "Plenário",
         "relator": "Ministro X", "data_sessao": "10/06/2026",
         "processo": f"TC {i}/2025", "tipo_processo": "ACÓRDÃO",
         "url": f"https://contas.tcu.gov.br/{i}", "cnpjs_citados": "",
         "trecho": e, "cita_o_conselho": True}
        for i, e in enumerate(ementas)])


def _ementa_condenatoria_muda_a_gravidade(erros: list[str]) -> None:
    """Ementa que fala em multa ou tomada de contas especial sobe o achado para
    `alta` e diz QUAIS termos apareceram — senão o leitor não abre o acórdão."""
    a = T.run({"tcu_acordaos": _acordaos([_EMENTA_CONDENATORIA,
                                          _EMENTA_DE_PROCEDIMENTO])})
    achados = {f.id: f for f in a}
    f = achados.get("tcu-acordaos-conselho")
    if f is None:
        erros.append("tcu: o achado sobre o conselho não foi publicado")
        return
    _erro(erros, f.gravidade == "alta",
          f"tcu: ementa com multa e tomada de contas especial saiu com gravidade "
          f"{f.gravidade!r} — matéria condenatória do Tribunal é o achado mais "
          "verificável do relatório e não pode ficar abaixo de alta")
    _erro(erros, f.metricas.get("ementa_com_materia_condenatoria") == 1,
          f"tcu: só uma das duas ementas é condenatória ({f.metricas})")
    for termo in ("multa", "tomada de contas especial", "dano ao erário"):
        _erro(erros, termo in f.titulo,
              f"tcu: o título não cita {termo!r} ({f.titulo!r})")

    # a ressalva que impede a leitura errada
    _erro(erros, "não que o Tribunal tenha decidido" in f.descricao,
          "tcu: falta a ressalva de que a ementa indica o assunto e não o "
          "resultado — sem ela 'menciona dano ao erário' vira 'houve dano'")

    # nenhuma célula vazia: vazio lê-se como 'não há nada aqui'
    col = f.evidencia["ementa_menciona"].astype(str).str.strip()
    _erro(erros, (col != "").all(),
          f"tcu: linha com `ementa_menciona` vazia ({f.evidencia[col == ''].to_dict('records')})")
    procedimento = f.evidencia[f.evidencia["trecho"] == _EMENTA_DE_PROCEDIMENTO]
    _erro(erros, not procedimento.empty
          and "denúncia" in str(procedimento.iloc[0]["ementa_menciona"]),
          "tcu: a ementa de denúncia/representação não foi classificada")

    # sem matéria condenatória o achado NÃO pode subir de gravidade
    so_procedimento = T.run({"tcu_acordaos": _acordaos([_EMENTA_DE_PROCEDIMENTO])})
    f2 = {x.id: x for x in so_procedimento}.get("tcu-acordaos-conselho")
    _erro(erros, f2 is not None and f2.gravidade == "media",
          f"tcu: ementa sem matéria condenatória saiu como "
          f"{None if f2 is None else f2.gravidade!r} — 'alta' aqui gastaria a "
          "gravidade que o achado de verdade precisa")
    _erro(erros, f2 is not None and "menciona" not in f2.titulo,
          f"tcu: título anuncia termos que não existem ({None if f2 is None else f2.titulo!r})")


def _trecho_alcanca_o_dispositivo(erros: list[str]) -> None:
    """600 caracteres cortavam a ementa antes de multa, débito e tomada de
    contas especial — que ficam no fim."""
    fonte = inspect.getsource(pipeline._acordao_linha)
    _erro(erros, "texto[:600]" not in fonte,
          "acórdãos: o corte de 600 caracteres voltou — o dispositivo fica no "
          "fim da ementa e é ele que a classificação lê")
    longo = ("X " * 400) + "MULTA. TOMADA DE CONTAS ESPECIAL."
    linha = pipeline._acordao_linha({"numeroAcordao": "1", "sumario": longo},
                                    pipeline._texto_do_registro(
                                        {"numeroAcordao": "1", "sumario": longo}),
                                    set())
    m, cond = T._mencoes(linha["trecho"])
    _erro(erros, cond and "multa" in m and "tomada de contas especial" in m,
          f"acórdãos: ementa de {len(longo)} caracteres perdeu o dispositivo no "
          f"corte ({m})")


def _secundario_aparece_no_diagnostico(erros: list[str]) -> None:
    """Etapa reaproveitada do disco tem de registrar os secundários dela.

    Sem isto `pncp_cobrancas` não existe para o diagnóstico: fica vazio, ninguém
    pergunta por quê, e o relatório cala sobre a falta do cruzamento nota fiscal
    × contrato. Foi assim por sete rodadas.
    """
    with _ambiente(checkpoint="1"), _diagnostico_limpo():
        # o principal cheio em disco, o secundário vazio mas já respondido
        pd.DataFrame([{"orgao_papel": "alvo", "fornecedor_ni": "55666777000188",
                       "numeroControlePNCP": "x-2-000007/2023", "valorGlobal": 10.0,
                       "tipoContrato_nome": "Contrato (termo inicial)", "_ano": 2024}
                      ]).to_csv(config.STAGED_DIR / "pncp_contratos.csv", index=False)
        (config.STAGED_DIR / "pncp_cobrancas.csv").write_text("_vazio\n")
        alvo = config.ORGAO_ALVO_CNPJ_CANDIDATOS[0]
        pasta = (config.RAW_DIR / pipeline.CHECKPOINT_SUBDIR / "pncp_cobrancas" / alvo)
        pasta.mkdir(parents=True)
        (pasta / "202401.vazio").write_text("2026-09-05T09:00:00\n")

        pronto, pendentes = pipeline.etapa_pronta("pncp_contratos", 2024, 2024)
        _erro(erros, pronto is not None,
              f"regressão desatualizada: a etapa deveria sair pronta ({pendentes=})")
        pipeline._diagnostico_dos_secundarios("pncp_contratos")
        diag = {d["dataset"]: d for d in pipeline._DIAGNOSTICO}
        linha = diag.get("pncp_cobrancas")
        if linha is None:
            erros.append("diagnóstico: `pncp_cobrancas` não entrou no diagnóstico da "
                         "etapa reaproveitada — sem a linha, `_porque_vazio` nunca "
                         "pergunta por que o dataset está vazio")
            return
        _erro(erros, linha.get("linhas") == 0,
              f"diagnóstico: `pncp_cobrancas` vazio saiu com {linha.get('linhas')} linha(s)")
        _erro(erros, linha.get("secundario_de") == "pncp_contratos",
              f"diagnóstico: falta dizer de qual etapa o secundário veio ({linha})")

    # e o caminho inteiro: `build_staged --retomar` tem de deixar a linha lá
    with _ambiente(checkpoint="1"), _diagnostico_limpo(), _trocar(
            pipeline, _etapas=lambda desde, ate: {
                "pncp_contratos": lambda: pd.DataFrame()}):
        pd.DataFrame([{"orgao_papel": "alvo", "fornecedor_ni": "55666777000188",
                       "numeroControlePNCP": "x-2-000007/2023", "valorGlobal": 10.0,
                       "tipoContrato_nome": "Contrato (termo inicial)", "_ano": 2024}
                      ]).to_csv(config.STAGED_DIR / "pncp_contratos.csv", index=False)
        (config.STAGED_DIR / "pncp_cobrancas.csv").write_text("_vazio\n")
        alvo = config.ORGAO_ALVO_CNPJ_CANDIDATOS[0]
        pasta = (config.RAW_DIR / pipeline.CHECKPOINT_SUBDIR / "pncp_cobrancas" / alvo)
        pasta.mkdir(parents=True)
        (pasta / "202401.vazio").write_text("2026-09-05T09:00:00\n")
        try:
            pipeline.build_staged(2024, 2024, retomar=True)
        except Exception as e:                        # noqa: BLE001
            erros.append(f"build_staged estourou: {type(e).__name__}: {e}")
            return
        diag = pd.read_csv(config.STAGED_DIR / "_coleta_diagnostico.csv")
        _erro(erros, "pncp_cobrancas" in set(diag["dataset"].astype(str)),
              f"build_staged --retomar: `pncp_cobrancas` fora do "
              f"`_coleta_diagnostico.csv` ({sorted(diag['dataset'].astype(str))})")


def _vazio_invisivel_ganha_veredito(erros: list[str]) -> None:
    """Com a linha no diagnóstico, a frente de diagnóstico passa a dizer por que
    `pncp_cobrancas` está vazio — que é a pergunta que ficou sete rodadas sem
    ser feita."""
    _erro(erros, "pncp_cobrancas" in D.ENDPOINTS,
          "diagnóstico: `pncp_cobrancas` precisa de endpoint conhecido para "
          "receber veredito")
    with _ambiente():
        diag = pd.DataFrame([
            {"dataset": "pncp_cobrancas", "linhas": 0, "colunas": 0,
             "retomado": True, "secundario_de": "pncp_contratos"}])
        ledger = pd.DataFrame([
            {"caminho": "/api/consulta/v1/instrumentoscobranca/inclusao",
             "status": 200, "resultado": "vazio", "itens": 0,
             "params": "dataInicial=20240101&dataFinal=20240131",
             "ts": "2026-09-05T09:00:00"}])
        achados = D._porque_vazio({"_coleta_http": ledger}, diag, ["pncp_cobrancas"])
    alvo = [a for a in achados
            if any("pncp_cobrancas" in str(v)
                   for v in (a.evidencia["fonte_vazia"] if a.evidencia is not None
                             and "fonte_vazia" in a.evidencia.columns else []))]
    _erro(erros, bool(alvo),
          "diagnóstico: `pncp_cobrancas` vazio não recebeu veredito — a linha no "
          "diagnóstico existe justamente para que a pergunta seja feita")


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_ementa_condenatoria_muda_a_gravidade, _trecho_alcanca_o_dispositivo,
                  _secundario_aparece_no_diagnostico, _vazio_invisivel_ganha_veredito):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
