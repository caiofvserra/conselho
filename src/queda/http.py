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
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from . import config

log = logging.getLogger("queda.http")

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
        with open(cache) as f:
            payload = json.load(f)
        return payload["data"]

    last_err: Exception | None = None
    for attempt in range(config.RETRIES):
        _throttle(url)
        try:
            r = session().get(url, params=params, timeout=config.TIMEOUT)
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            data = None
        elif r.status_code in ok_empty_status:
            data = []
        elif r.status_code >= 500 or r.status_code == 429:
            last_err = RuntimeError(f"HTTP {r.status_code} em {url}")
            time.sleep(2 ** attempt * 2)
            continue
        elif r.status_code >= 400:
            log.warning("HTTP %s em %s params=%s", r.status_code, url, params)
            data = None
        else:
            try:
                data = r.json()
            except ValueError:
                data = None
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w") as f:
            json.dump({"url": url, "params": params, "data": data}, f, ensure_ascii=False)
        return data
    raise RuntimeError(f"Falha após {config.RETRIES} tentativas: {url}") from last_err


def memo(name: str, fn):
    """Cache simples de valores derivados (ex.: host descoberto, CNPJs)."""
    path = config.CACHE_DIR / f"{name}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    value = fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, ensure_ascii=False)
    return value
