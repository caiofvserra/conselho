"""Regressão do cliente HTTP: corpo dos 4xx guardado e ritmo adaptativo.

Por que existe: em dez rodadas o portal respondeu HTTP 400 a todo exercício de
2026 e a exercícios anteriores ao início de cada série, e `get_json` descartava
o corpo — nunca deu para saber se a recusa era do período, do formato do
parâmetro ou de outra coisa. E o ritmo fixo de 2,5 s levou a 6 bloqueios por
rate-limit na mesma rodada (374 s de espera). Se alguém voltar a descartar o
corpo, tirar o `import re` (o NameError era engolido e o aviso sumia em
silêncio) ou desligar o ritmo lento após 403, este teste pega.

Sem rede: `http.session()` é trocado por um stub; `time.sleep` é interceptado
para não dormir de verdade; `config.RAW_DIR` aponta para um tempdir e é
restaurado no fim.
"""
from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

from .. import config, http


class _Resp:
    """Response falso com o mínimo que `get_json`/`get_text` consomem."""

    def __init__(self, status: int, texto: str, ctype: str, payload=None,
                 headers: dict | None = None):
        self.status_code = status
        self.text = texto
        self.headers = {"Content-Type": ctype, **(headers or {})}
        self._payload = payload
        self.ok = status < 400

    def json(self):
        if self._payload is None:
            raise ValueError("não é JSON")
        return self._payload


class _Sess:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas: list[tuple[str, dict | None]] = []

    def get(self, url, params=None, timeout=None):
        self.chamadas.append((url, params))
        return self.respostas.pop(0) if len(self.respostas) > 1 else self.respostas[0]


class _Captura(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.linhas: list[str] = []

    def emit(self, record):
        self.linhas.append(record.getMessage())


def _com_ambiente(fn) -> list[str]:
    """Roda `fn(erros, captura, dormidas)` com o módulo http isolado e restaurado."""
    erros: list[str] = []
    guardado = (config.RAW_DIR, http.session, dict(http._ritmo_host),
                dict(http._last_hit), list(http._LEDGER), time.sleep,
                http.log.level, http.log.propagate)
    captura = _Captura()
    dormidas: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            config.RAW_DIR = Path(tmp) / "raw"
            http._ritmo_host.clear()
            http._last_hit.clear()
            http.log.addHandler(captura)
            http.log.setLevel(logging.DEBUG)
            http.log.propagate = False
            # `http` faz `import time` e chama `time.sleep`: trocar o atributo do
            # módulo `time` alcança a chamada sem tocar no código de produção.
            time.sleep = lambda s: dormidas.append(float(s))
            fn(erros, captura, dormidas)
        except Exception as e:                       # noqa: BLE001 — é o teste
            erros.append(f"http_config: exceção inesperada {type(e).__name__}: {e}")
        finally:
            (config.RAW_DIR, http.session, ritmo, ultimo, ledger, time.sleep,
             nivel, propaga) = guardado
            http._ritmo_host.clear()
            http._ritmo_host.update(ritmo)
            http._last_hit.clear()
            http._last_hit.update(ultimo)
            http._LEDGER[:] = ledger
            http.log.removeHandler(captura)
            http.log.setLevel(nivel)
            http.log.propagate = propaga
    return erros


URL = "https://crea-sp.implanta.net.br/portalTransparencia/api/v1.0/Empenhos"
PARAMS = {"referenciaInicio": "01/2026", "referenciaTermino": "12/2026"}


def _caso_400_json(erros, captura, dormidas):
    """(a) 400 com JSON {"Message": ...}: None, arquivo e log com a mensagem."""
    corpo = '{"Message":"Referência inválida"}'
    http.session = lambda: _Sess([_Resp(400, corpo, "application/json; charset=utf-8",
                                        payload={"Message": "Referência inválida"})])
    r = http.get_json(URL, PARAMS, use_cache=False)
    if r is not None:
        erros.append("http_config: 400 tem de devolver None (devolveu %r)" % (r,))
    if not dormidas == []:
        erros.append("http_config: 400 determinístico não pode esperar/repetir")
    arquivos = list((config.RAW_DIR / "http-4xx").glob("*.txt"))
    if len(arquivos) != 1:
        erros.append(f"http_config: esperava 1 arquivo em raw/http-4xx, há {len(arquivos)}")
    else:
        texto = arquivos[0].read_text(encoding="utf-8")
        for trecho in ("Referência inválida", "status: 400", URL, "referenciaInicio",
                       "content-type: application/json"):
            if trecho not in texto:
                erros.append(f"http_config: arquivo do 4xx sem {trecho!r}")
    avisos = [l for l in captura.linhas if "HTTP 400" in l]
    if not avisos:
        erros.append("http_config: nenhum WARNING de HTTP 400 no log")
    elif "Referência inválida" not in avisos[0] or URL not in avisos[0] \
            or "referenciaInicio" not in avisos[0]:
        erros.append("http_config: o WARNING do 400 tem de trazer URL, params e a "
                     f"mensagem do corpo na mesma linha: {avisos[0]!r}")
    if not any(l.get("resultado") == http.R_RECUSADO and l.get("status") == 400
               for l in http.ledger()):
        erros.append("http_config: ledger sem linha 'recusado' status 400")


def _caso_400_html(erros, captura, dormidas):
    """(b) 400 com HTML do IIS: arquivo gravado e log SEM tags."""
    corpo = ("<!DOCTYPE html><html><head><title>Bad Request</title>"
             "<style>body{font:12px}</style></head><body>\n<h1>Bad   Request</h1>"
             "<p>Período  de referência\ninválido</p></body></html>")
    http.session = lambda: _Sess([_Resp(400, corpo, "text/html")])
    r = http.get_json(URL, PARAMS, use_cache=False)
    if r is not None:
        erros.append("http_config: 400 HTML tem de devolver None")
    arquivos = list((config.RAW_DIR / "http-4xx").glob("*.txt"))
    if not arquivos or "<h1>Bad" not in arquivos[0].read_text(encoding="utf-8"):
        erros.append("http_config: arquivo do 400 HTML não guarda o corpo cru")
    avisos = [l for l in captura.linhas if "HTTP 400" in l]
    if not avisos:
        erros.append("http_config: nenhum WARNING para 400 HTML")
    else:
        aviso = avisos[0]
        if "<" in aviso.split("corpo:")[-1].split("(guardado")[0]:
            erros.append(f"http_config: log do 400 HTML ainda tem tags: {aviso!r}")
        if "Bad Request Período de referência inválido" not in aviso:
            erros.append("http_config: espaços não colapsados no trecho do log: "
                         f"{aviso!r}")
        if "font:12px" in aviso:
            erros.append("http_config: conteúdo de <style> vazou para o log")

    # get_text também grava: é por ele que `medir_prefixos` sonda os prefixos.
    captura.linhas.clear()
    antes = len(list((config.RAW_DIR / "http-4xx").glob("*.txt")))
    outra = URL.replace("Empenhos", "Licitacoes")
    r = http.get_text(outra, {"exercicio": "2026"})
    if r != (400, corpo):
        erros.append(f"http_config: get_text tem de devolver (status, texto): {r!r}")
    if len(list((config.RAW_DIR / "http-4xx").glob("*.txt"))) != antes + 1:
        erros.append("http_config: get_text não gravou o corpo do 400")
    if not any("HTTP 400" in l and "Período de referência" in l for l in captura.linhas):
        erros.append("http_config: get_text não logou o trecho do 400")


def _caso_ritmo_apos_403(erros, captura, dormidas):
    """(c) Um 403 basta para o host passar ao ritmo THROTTLE_APOS_403."""
    host = URL.split("/")[2]
    if http._intervalo_minimo(host) != config.THROTTLE_SECONDS:
        erros.append("http_config: host sem bloqueio tem de começar em THROTTLE_SECONDS")
    # 403 seguido de 200: o rate-limit é retentado (uma espera falsa) e vence.
    sess = _Sess([
        _Resp(403, "<html>Forbidden</html>", "text/html"),
        _Resp(200, "[]", "application/json", payload=[{"id": 1}]),
    ])
    http.session = lambda: sess           # a MESMA sessão: a fila avança entre tentativas
    r = http.get_json(URL, PARAMS, use_cache=False)
    if r != [{"id": 1}]:
        erros.append(f"http_config: 403 retentado devia terminar em 200: {r!r}")
    esperas_rl = [d for d in dormidas if d >= config.RATE_LIMIT_BACKOFF_BASE]
    if not esperas_rl or esperas_rl[0] < config.RATE_LIMIT_BACKOFF_BASE:
        erros.append("http_config: a 1ª espera de rate-limit tem de partir de "
                     f"RATE_LIMIT_BACKOFF_BASE={config.RATE_LIMIT_BACKOFF_BASE}: {dormidas}")
    if config.RATE_LIMIT_BACKOFF_BASE < 45:
        erros.append("http_config: RATE_LIMIT_BACKOFF_BASE voltou abaixo de 45 s — a "
                     "1ª espera de 20 s nunca bastou na rodada de 05/09/2026")
    if http._ritmo_host.get(host) != config.THROTTLE_APOS_403:
        erros.append("http_config: após um 403 o host tem de ficar em THROTTLE_APOS_403 "
                     f"(estado: {http._ritmo_host})")
    if http._intervalo_minimo("pncp.gov.br") != config.THROTTLE_SECONDS:
        erros.append("http_config: o ritmo lento vazou para um host que nunca bloqueou")
    # E `_throttle` de fato dorme pelo intervalo novo, não pelo padrão.
    dormidas.clear()
    http._last_hit[host] = time.monotonic()
    http._throttle(URL)
    if not dormidas or not (config.THROTTLE_SECONDS < dormidas[0] <= config.THROTTLE_APOS_403):
        erros.append("http_config: _throttle não usou o intervalo lento após o 403: "
                     f"{dormidas}")
    if config.THROTTLE_APOS_403 <= config.THROTTLE_SECONDS:
        erros.append("http_config: THROTTLE_APOS_403 tem de ser maior que THROTTLE_SECONDS")
    # Um 403 na sondagem (rate_limit_retries=0) também rebaixa o ritmo.
    http._ritmo_host.clear()
    http.session = lambda: _Sess([_Resp(403, "", "text/html")])
    http.get_json(URL, PARAMS, use_cache=False, rate_limit_retries=0)
    if host not in http._ritmo_host:
        erros.append("http_config: 403 em sondagem (sem retentativa) não rebaixou o ritmo")
    if list((config.RAW_DIR / "http-4xx").glob("*.txt")):
        erros.append("http_config: 403/429 é bloqueio, não recusa — não deve ir para http-4xx")


def _caso_import_re(erros, captura, dormidas):
    """(d) `_guardar_corpo_nao_json` loga o trecho: exige `import re` no módulo."""
    if getattr(http, "re", None) is None:
        erros.append("http_config: `import re` sumiu de http.py")
    resp = _Resp(200, "<html><body><p>Verificando seu   navegador</p></body></html>",
                 "text/html")
    http._guardar_corpo_nao_json("https://servicos.creasp.org.br/service-api/x", resp)
    if not any("Verificando seu navegador" in l for l in captura.linhas):
        erros.append("http_config: _guardar_corpo_nao_json não logou o trecho legível "
                     "(NameError engolido? confira `import re`)")
    if not list((config.RAW_DIR / "nao-json").glob("*.txt")):
        erros.append("http_config: corpo não-JSON não foi guardado")


def run() -> list[str]:
    erros: list[str] = []
    for caso in (_caso_400_json, _caso_400_html, _caso_ritmo_apos_403, _caso_import_re):
        erros.extend(_com_ambiente(caso))
    return erros


if __name__ == "__main__":
    falhas = run()
    print("\n".join(falhas) if falhas else "http_config: ok")
