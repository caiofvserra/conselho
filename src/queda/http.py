"""Cliente HTTP único: cache em disco, throttle, retry e User-Agent genérico.

Todas as consultas são GETs anônimos a APIs públicas — sem token, cookie ou
qualquer identificador do operador. O cache em disco garante que reexecuções
não repitam chamadas (menos tráfego, menos rastro).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
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


def get_json(url: str, params: dict | None = None, *, use_cache: bool = True,
             ok_empty_status: tuple[int, ...] = (204,)):
    """GET com cache. Retorna o JSON, [] para respostas vazias, None para 404.

    Levanta exceção só depois de esgotar os retries em erros 5xx/rede.
    """
    cache = _cache_path(url, params)
    if use_cache and cache.exists():
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
            if confiavel:
                return data
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
    max_iter = config.RETRIES + config.RATE_LIMIT_RETRIES + 2
    for _ in range(max_iter):
        _throttle(url)
        try:
            r = session().get(url, params=params, timeout=config.TIMEOUT)
        except requests.RequestException as e:
            last_err = e
            net_attempt += 1
            if net_attempt > config.RETRIES:
                break
            time.sleep(2 ** net_attempt)
            continue

        if r.status_code == 404:
            return _store(None, 404)                 # ausência genuína: cacheável
        if r.status_code in ok_empty_status:
            return _store([], r.status_code)
        if r.status_code in config.RATE_LIMIT_STATUS:
            rate_attempt += 1
            if rate_attempt > config.RATE_LIMIT_RETRIES:
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
            log.warning("HTTP %s em %s params=%s", r.status_code, url, params)
            return None                              # não cacheia (permite correção)
        try:
            return _store(r.json(), r.status_code)
        except ValueError:
            return _store(None, r.status_code)
    raise RuntimeError(f"Falha após retries: {url}") from last_err


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
