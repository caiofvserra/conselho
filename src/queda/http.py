"""Cliente HTTP único: cache em disco, throttle, retry e User-Agent genérico.

Todas as consultas são GETs anônimos a APIs públicas — sem token, cookie ou
qualquer identificador do operador. O cache em disco garante que reexecuções
não repitam chamadas (menos tráfego, menos rastro).

Este módulo também mantém o **ledger da coleta**: uma linha por tentativa de
requisição, com status, número da tentativa, se veio do cache, quantos itens o
JSON trouxe e quanto demorou. Ele existe porque "0 linhas" no relatório é uma
não-resposta: um dataset vazio pode ser bloqueio do WAF (403), endpoint
inexistente (404), formato de parâmetro errado (400), instabilidade (5xx/rede)
ou **200 com lista vazia** — e só o último significa que o órgão não publica o
dado. As causas levam a conclusões opostas (uma é bug nosso, a outra é achado
de transparência) e sem o status registrado nenhuma delas é afirmável.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

from . import config

log = logging.getLogger("queda.http")


class RateLimited(RuntimeError):
    """O host bloqueou por rate-limit (403/429) mesmo após os backoffs."""

_session: requests.Session | None = None
_last_hit: dict[str, float] = {}


def _verify():
    bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if bundle and Path(bundle).exists():
        return bundle
    return True


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "pt-BR,pt;q=0.9",
        })
        s.verify = _verify()
        _session = s
    return _session


def _cache_path(url: str, params: dict | None) -> Path:
    key = url + ("?" + urlencode(sorted(params.items())) if params else "")
    h = hashlib.sha256(key.encode()).hexdigest()[:24]
    host = url.split("/")[2]
    return config.RAW_DIR / host / f"{h}.json"


def _throttle(url: str) -> None:
    host = url.split("/")[2]
    elapsed = time.monotonic() - _last_hit.get(host, 0.0)
    if elapsed < config.THROTTLE_SECONDS:
        time.sleep(config.THROTTLE_SECONDS - elapsed)
    _last_hit[host] = time.monotonic()


# ---------------------------------------------------------------- ledger
# Vocabulário do campo `resultado`. É por ele que a frente de diagnóstico
# separa "o órgão não publica" de "não conseguimos coletar" — o status cru não
# faz essa separação sozinho, porque 200-com-lista-vazia e 403-do-WAF chegam
# ao relatório como o mesmo "0 linhas".
R_OK = "ok"                        # 2xx com pelo menos um registro
R_VAZIO = "vazio"                  # 2xx/204 sem nenhum registro: respondeu, não há dado
R_INEXISTENTE = "inexistente"      # 404: este caminho não existe nesta API
R_RECUSADO = "recusado"            # 4xx determinístico (400/401/422): parâmetro/permissão
R_BLOQUEIO = "bloqueio"            # 403/429: WAF ou rate-limit
R_ERRO_SERVIDOR = "erro-servidor"  # 5xx
R_ERRO_REDE = "erro-rede"          # timeout, DNS, conexão recusada
R_SEM_JSON = "sem-json"            # 2xx cujo corpo não é JSON (página de erro, por ex.)
R_CACHE_DESCARTADO = "cache-descartado"   # entrada de cache não confiável, refeita na rede

COLUNAS_LEDGER = ["ts", "host", "caminho", "params", "status", "resultado",
                  "tentativa", "cache", "itens", "duracao_s"]

# Uma rodada real faz alguns milhares de requisições (o PNCP sozinho é mês a
# mês, paginado). O teto existe só para que um laço acidental não consuma a
# memória da sessão; quando ele é atingido, o descarte é contado e avisado —
# ledger truncado em silêncio seria pior que ledger nenhum.
LEDGER_MAX = 100_000
_LEDGER: list[dict] = []
_LEDGER_DESCARTADAS = 0


def _contar_itens(data) -> int | None:
    """Quantos registros o payload trouxe. None quando não dá para dizer.

    Distinguir 0 de "não sei" importa: é o 0 (200 com lista vazia) que sustenta
    a afirmação de que o órgão não publica o dado.
    """
    if data is None:
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        # envelopes usados pelas três APIs: {"data": [...]} (Dados Abertos e
        # PNCP), {"result": [...]} (Dados Abertos antigo)
        for chave in ("data", "result", "items", "content", "registros"):
            valor = data.get(chave)
            if isinstance(valor, list):
                return len(valor)
        return 1
    return None


def _params_curto(params: dict | None, limite: int = 160) -> str:
    """Parâmetros em uma linha, truncados.

    O ledger é para leitura humana e o que importa nele é o FORMATO do
    parâmetro tentado (`exercicio=2019` × `referenciaInicio=01/2019`), não a
    querystring inteira — a do PNCP repete data, página e tamanho de página em
    cada linha e afogaria a coluna.
    """
    if not params:
        return ""
    txt = "&".join(f"{k}={v}" for k, v in params.items())
    return txt if len(txt) <= limite else txt[:limite - 1] + "…"


def registrar(url: str, params: dict | None, *, resultado: str,
              status: int | str = "", tentativa: int = 1, cache: bool = False,
              itens: int | None = None, duracao: float = 0.0) -> None:
    """Anota UMA tentativa no ledger. Uso interno de `get_json`.

    Cada tentativa vira uma linha, inclusive as repetições de backoff: é a
    contagem de tentativas que separa "bloqueou uma vez" de "bloqueou as cinco".
    """
    global _LEDGER_DESCARTADAS
    if len(_LEDGER) >= LEDGER_MAX:
        _LEDGER_DESCARTADAS += 1
        return
    partes = url.split("/", 3)
    caminho = ("/" + partes[3]) if len(partes) > 3 else "/"
    _LEDGER.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "host": partes[2] if len(partes) > 2 else "",
        "caminho": caminho.split("?")[0],
        "params": _params_curto(params),
        "status": status,
        "resultado": resultado,
        "tentativa": int(tentativa),
        "cache": bool(cache),
        "itens": itens,
        "duracao_s": round(float(duracao), 3),
    })


def ledger() -> list[dict]:
    """Cópia do ledger: uma linha por tentativa de requisição desta execução."""
    return [dict(linha) for linha in _LEDGER]


def salvar_ledger(caminho):
    """Grava o ledger em CSV e devolve o DataFrame equivalente.

    Escreve com o `csv` da biblioteca padrão de propósito: esta é a camada mais
    baixa do projeto e não deveria precisar de pandas para funcionar. O import
    é local e existe só porque quem chama (o pipeline) quer o DataFrame de
    volta para pôr no `staged`.

    O cabeçalho é sempre escrito, mesmo sem nenhuma linha: um CSV totalmente
    vazio faz `pd.read_csv` levantar `EmptyDataError` e derruba o `load_staged`.
    """
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    linhas = ledger()
    with open(destino, "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUNAS_LEDGER)
        escritor.writeheader()
        escritor.writerows(linhas)
    if _LEDGER_DESCARTADAS:
        log.warning("ledger: %d tentativa(s) acima do teto de %d não foram "
                    "registradas", _LEDGER_DESCARTADAS, LEDGER_MAX)
    log.info("staged/%s: %d tentativa(s) de requisição", destino.name, len(linhas))
    import pandas as pd
    return pd.DataFrame(linhas, columns=COLUNAS_LEDGER)


def get_json(url: str, params: dict | None = None, *, use_cache: bool = True,
             ok_empty_status: tuple[int, ...] = (204,),
             rate_limit_retries: int | None = None,
             retries: int | None = None, espera_base: float = 2.0):
    """GET com cache. Retorna o JSON, [] para respostas vazias, None para 404.

    Levanta exceção só depois de esgotar os retries em erros 5xx/rede.

    `rate_limit_retries=0` faz o 403/429 ser tratado como resposta final —
    "este host/caminho não nos serve" — em vez de bloqueio temporário. É o que
    se quer ao SONDAR: um host que recusa não vai passar a aceitar depois de
    três minutos de espera, e insistir custa minutos por candidato.
    """
    limite = (config.RATE_LIMIT_RETRIES if rate_limit_retries is None
              else rate_limit_retries)
    limite_rede = config.RETRIES if retries is None else retries
    cache = _cache_path(url, params)
    if use_cache and cache.exists():
        t0 = time.monotonic()
        try:
            with open(cache) as f:
                payload = json.load(f)
        except (ValueError, OSError):
            payload = None
        if payload is not None:
            # Entradas gravadas por versões antigas não têm "status" e podem ter
            # cacheado um bloqueio (403) como se fosse ausência de dados. Só
            # confiamos no cache quando ele guarda conteúdo, ou quando a ausência
            # foi confirmada por um 404 explícito.
            data = payload.get("data")
            status = payload.get("status")
            confiavel = data not in (None, []) or status == 404
            # A resposta servida do cache também entra no ledger. Sem isso a
            # segunda rodada pareceria não ter feito requisição nenhuma — e o
            # diagnóstico concluiria "sem registro de tentativa" para tudo.
            n = _contar_itens(data)
            if confiavel:
                registrar(url, params, cache=True,
                          status=status if status is not None else "",
                          resultado=(R_INEXISTENTE if status == 404 else
                                     R_OK if (n is None or n > 0) else R_VAZIO),
                          itens=n, duracao=time.monotonic() - t0)
                return data
            registrar(url, params, cache=True,
                      status=status if status is not None else "",
                      resultado=R_CACHE_DESCARTADO, itens=n,
                      duracao=time.monotonic() - t0)
            log.debug("cache descartado (data vazia, status=%s): %s", status, url)

    def _store(data, status):
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w") as f:
            json.dump({"url": url, "params": params, "status": status, "data": data},
                      f, ensure_ascii=False)
        return data

    last_err: Exception | None = None
    net_attempt = 0
    rate_attempt = 0
    max_iter = limite_rede + limite + 2
    for _ in range(max_iter):
        _throttle(url)
        # Ordinal desta tentativa para o mesmo par (url, params): cada volta do
        # laço incrementa exatamente um dos dois contadores.
        tentativa = net_attempt + rate_attempt + 1
        t0 = time.monotonic()
        try:
            r = session().get(url, params=params, timeout=config.TIMEOUT)
        except requests.RequestException as e:
            # O NOME da exceção é o status aqui: ConnectTimeout, SSLError e
            # ConnectionError apontam para causas diferentes (host lento, TLS
            # do proxy, host inexistente) e viram "0 linhas" iguais sem isso.
            registrar(url, params, resultado=R_ERRO_REDE,
                      status=type(e).__name__, tentativa=tentativa,
                      duracao=time.monotonic() - t0)
            last_err = e
            net_attempt += 1
            if net_attempt > limite_rede:
                break
            time.sleep(espera_base * (2 ** (net_attempt - 1)))
            continue

        duracao = time.monotonic() - t0
        if r.status_code >= 500:
            # 5xx é servidor sobrecarregado, não resposta. Vale esperar: o PNCP
            # devolve 503 em janela grande e responde na janela menor.
            registrar(url, params, resultado=R_ERRO_SERVIDOR,
                      status=r.status_code, tentativa=tentativa, duracao=duracao)
            last_err = RuntimeError(f"HTTP {r.status_code}")
            net_attempt += 1
            if net_attempt > limite_rede:
                break
            espera = espera_base * (2 ** (net_attempt - 1))
            log.info("HTTP %s em %s — tentativa %d/%d, aguardando %.0fs",
                     r.status_code, url, net_attempt, limite_rede, espera)
            time.sleep(espera)
            continue
        if r.status_code == 404:
            registrar(url, params, resultado=R_INEXISTENTE, status=404,
                      tentativa=tentativa, itens=0, duracao=duracao)
            return _store(None, 404)                 # ausência genuína: cacheável
        if r.status_code in ok_empty_status:
            registrar(url, params, resultado=R_VAZIO, status=r.status_code,
                      tentativa=tentativa, itens=0, duracao=duracao)
            return _store([], r.status_code)
        if r.status_code in config.RATE_LIMIT_STATUS:
            registrar(url, params, resultado=R_BLOQUEIO, status=r.status_code,
                      tentativa=tentativa, duracao=duracao)
            if limite <= 0:
                log.info("HTTP %s em %s — sondagem: host/caminho tratado como "
                         "indisponível, sem espera", r.status_code, url)
                return None                  # não cacheado: pode ser temporário
            rate_attempt += 1
            if rate_attempt > limite:
                raise RateLimited(
                    f"HTTP {r.status_code} persistente em {url} — o servidor está "
                    "limitando o ritmo. Reexecute a mesma célula: o cache retoma de "
                    "onde parou e só o que faltou é rebaixado.")
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = int(retry_after)
            else:
                wait = min(config.RATE_LIMIT_BACKOFF_BASE * (2 ** (rate_attempt - 1)),
                           config.RATE_LIMIT_BACKOFF_MAX)
            wait += random.uniform(0, 3)             # jitter
            log.warning("HTTP %s (rate-limit) em %s — aguardando %.0fs (tentativa %d/%d)",
                        r.status_code, url, wait, rate_attempt, config.RATE_LIMIT_RETRIES)
            time.sleep(wait)
            continue                                 # NÃO cacheia bloqueio
        if r.status_code >= 500:
            net_attempt += 1
            if net_attempt > config.RETRIES:
                break
            last_err = RuntimeError(f"HTTP {r.status_code} em {url}")
            time.sleep(2 ** net_attempt * 2)
            continue
        if r.status_code >= 400:                     # 400/401/... determinístico
            registrar(url, params, resultado=R_RECUSADO, status=r.status_code,
                      tentativa=tentativa, duracao=duracao)
            log.warning("HTTP %s em %s params=%s", r.status_code, url, params)
            return None                              # não cacheia (permite correção)
        try:
            data = r.json()
        except ValueError:
            registrar(url, params, resultado=R_SEM_JSON, status=r.status_code,
                      tentativa=tentativa, itens=0, duracao=duracao)
            _guardar_corpo_nao_json(url, r)
            return _store(None, r.status_code)
        # A distinção que justifica o ledger inteiro: 200 com lista vazia é o
        # órgão dizendo "não tenho isso publicado", e não uma falha nossa.
        n = _contar_itens(data)
        registrar(url, params, status=r.status_code, tentativa=tentativa,
                  resultado=R_OK if (n is None or n > 0) else R_VAZIO,
                  itens=n, duracao=duracao)
        return _store(data, r.status_code)
    raise RuntimeError(f"Falha após retries: {url}") from last_err


def get_text(url: str, params: dict | None = None):
    """GET sem cache que devolve `(status, texto)` — ou None em erro de rede.

    Para o que NÃO é JSON e nós queremos ler mesmo assim: a página HTML da
    documentação Stoplight, de onde sai a URL do OpenAPI. `get_json` descarta
    corpo não-JSON por desenho; aqui o corpo é o objetivo. Passa pelo mesmo
    throttle e pelo mesmo ledger, para que a sondagem apareça na contabilidade
    da rodada como qualquer outra requisição.
    """
    _throttle(url)
    t0 = time.monotonic()
    try:
        r = session().get(url, params=params, timeout=config.TIMEOUT)
    except requests.RequestException as e:
        registrar(url, params, resultado=R_ERRO_REDE, status=type(e).__name__,
                  tentativa=1, duracao=time.monotonic() - t0)
        return None
    registrar(url, params, resultado=R_OK if r.ok else R_RECUSADO,
              status=r.status_code, tentativa=1, duracao=time.monotonic() - t0)
    return r.status_code, r.text or ""


def _guardar_corpo_nao_json(url: str, r) -> None:
    """Um 2xx que não é JSON é a única resposta que o ledger registrava sem
    dizer O QUE veio — e é justamente a que mais explica.

    Na rodada de 04/09/2026, `servicos.creasp.org.br` foi o ÚNICO host de Dados
    Abertos a responder 200 ao caminho `/DadosAbertos/mandatarios`; os outros
    cinco deram 403, timeout ou conexão recusada. O ledger anotou `sem-json` e
    o corpo foi descartado. Se era uma página de desafio do WAF, um redirect
    para outro caminho ou um HTML embrulhando o JSON, cada caso pede uma
    correção diferente — e sem o corpo não dá para saber qual.

    Guarda o começo do corpo em `data/raw/nao-json/`, um arquivo por URL, e
    loga um trecho legível. Só o começo: uma página inteira de SPA são
    centenas de KB de JavaScript que não dizem mais do que os primeiros 600
    bytes.
    """
    try:
        pasta = config.RAW_DIR / "nao-json"
        pasta.mkdir(parents=True, exist_ok=True)
        nome = hashlib.sha1(url.encode()).hexdigest()[:16]
        corpo = r.text or ""
        (pasta / f"{nome}.txt").write_text(
            f"{url}\ncontent-type: {r.headers.get('Content-Type')}\n"
            f"status: {r.status_code}\n---\n{corpo[:4000]}", encoding="utf-8")
        trecho = re.sub(r"<[^>]+>", " ", corpo[:600])
        trecho = re.sub(r"\s+", " ", trecho).strip()[:160]
        log.warning("%s respondeu %s sem JSON (%s): \"%s\" — corpo em %s",
                    url, r.status_code, r.headers.get("Content-Type", "?"),
                    trecho, pasta / f"{nome}.txt")
    except Exception as e:                     # diagnóstico nunca derruba coleta
        log.debug("não foi possível guardar corpo não-JSON de %s (%s)", url, e)


def memo(name: str, fn):
    """Cache de valores derivados (host descoberto, CNPJs).

    Resultados nulos NÃO são persistidos: uma descoberta que falhou por
    bloqueio temporário não pode envenenar as execuções seguintes.
    """
    path = config.CACHE_DIR / f"{name}.json"
    if path.exists():
        try:
            with open(path) as f:
                value = json.load(f)
            if value is not None:
                return value
        except (ValueError, OSError):
            pass
    value = fn()
    if value is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, ensure_ascii=False)
    return value
