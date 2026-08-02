"""Clients das quatro fontes: Implanta, Dados Abertos Crea-SP, PNCP e CNPJ."""
from __future__ import annotations

import logging
import re

from . import config
from .http import get_json, memo

log = logging.getLogger("queda.clients")


# ================================================================ Implanta
def implanta(endpoint: str, params: dict | None = None):
    """GET em um endpoint do Portal da Transparência (Implanta)."""
    data = get_json(f"{config.IMPLANTA_BASE}/{endpoint}", params)
    return data if isinstance(data, list) else (data or [])


def implanta_ano(endpoint: str, ano: int, *, formato: str = "mensal"):
    """Busca um endpoint para o exercício inteiro.

    formato: "mensal" -> referencia MM/AAAA; "vigencia" -> vigenciaInicio/Termino.
    """
    if formato == "vigencia":
        params = {"vigenciaInicio": f"01/{ano}", "vigenciaTermino": f"12/{ano}"}
    else:
        params = {"referenciaInicio": f"01/{ano}", "referenciaTermino": f"12/{ano}"}
    return implanta(endpoint, params)


# Endpoints de diárias que existem nos schemas do swagger mas sem path declarado.
# Sondamos caminhos prováveis; 404 é tolerado.
DIARIAS_PROBE = [
    "DiariasDeslocamentos", "Diarias", "DespesasViagens", "DespesasViagem",
    "Indenizacoes", "PassagensAereas", "PassagensTerrestres",
]


def implanta_probe_diarias(ano: int) -> dict[str, list]:
    found: dict[str, list] = {}
    for ep in DIARIAS_PROBE:
        data = get_json(f"{config.IMPLANTA_BASE}/{ep}",
                        {"referenciaInicio": f"01/{ano}", "referenciaTermino": f"12/{ano}"})
        if data:
            found[ep] = data
    return found


def implanta_datas(endpoint: str, ano: int):
    """Endpoints que usam dataInicio/dataTermino no formato DD/MM/AAAA."""
    return implanta(endpoint, {"dataInicio": f"01/01/{ano}",
                               "dataTermino": f"31/12/{ano}"})


def implanta_exercicio(endpoint: str, ano: int):
    """Endpoints que usam o parâmetro `exercicio`."""
    return implanta(endpoint, {"exercicio": ano})


def achatar_demonstrativo(rows: list, nivel: int = 0, pai: str = "") -> list[dict]:
    """DemonstrativoEmpenhosPagamentos vem em árvore (campo DETALHES). Achata
    preservando o caminho hierárquico, para permitir agregação por conta."""
    out: list[dict] = []
    for r in rows or []:
        filhos = r.get("DETALHES") or []
        linha = {k: v for k, v in r.items() if k != "DETALHES"}
        linha["_nivel"] = nivel
        linha["_pai"] = pai
        linha["_folha"] = not filhos
        out.append(linha)
        if filhos:
            caminho = f"{pai}>{r.get('NOME') or r.get('CODIGO') or ''}".strip(">")
            out.extend(achatar_demonstrativo(filhos, nivel + 1, caminho))
    return out


def achatar_plano_contas(rows: list, nivel: int = 0, pai: str = "") -> list[dict]:
    """PlanoDeContas vem em árvore (campo Analiticas)."""
    out: list[dict] = []
    for r in rows or []:
        filhos = r.get("Analiticas") or []
        out.append({"codigo": r.get("Codigo"), "nome": r.get("Nome"),
                    "nivel": nivel, "pai": pai, "folha": not filhos})
        if filhos:
            out.extend(achatar_plano_contas(filhos, nivel + 1, r.get("Codigo") or ""))
    return out


# ================================================================ Dados Abertos
def _discover_dados_abertos() -> str | None:
    for base in config.DADOS_ABERTOS_CANDIDATES:
        try:
            data = get_json(f"{base}/api/DadosAbertos/mandatarios", use_cache=False)
        except RuntimeError:
            continue
        if isinstance(data, dict) and data.get("result") is not None:
            log.info("Dados Abertos: host descoberto em %s", base)
            return base
    log.warning("Dados Abertos: nenhum host candidato respondeu")
    return None


def dados_abertos_base() -> str | None:
    return memo("dados_abertos_base", _discover_dados_abertos)


def dados_abertos(endpoint: str, params: dict | None = None) -> list:
    base = dados_abertos_base()
    if not base:
        return []
    data = get_json(f"{base}/api/DadosAbertos/{endpoint}", params)
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            return [inner]
    return data if isinstance(data, list) else []


# ================================================================ PNCP
def pncp_paginado(path: str, params: dict, *, max_paginas: int = 200) -> list:
    """Itera as páginas de um endpoint de consulta do PNCP."""
    out: list = []
    pagina = 1
    while pagina <= max_paginas:
        data = get_json(f"{config.PNCP_CONSULTA}{path}",
                        {**params, "pagina": pagina, "tamanhoPagina": params.get("tamanhoPagina", 50)})
        if not data:
            break
        rows = data.get("data") if isinstance(data, dict) else data
        if not rows:
            break
        out.extend(rows)
        if isinstance(data, dict) and not data.get("paginasRestantes"):
            break
        pagina += 1
    return out


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").upper()


def _tokens_distintivos(alvo: str) -> set[str]:
    """Palavras que diferenciam um órgão dos homônimos do mesmo sistema.

    Todos os 27 CREAs começam com "CONSELHO REGIONAL DE ENGENHARIA E
    AGRONOMIA"; o que distingue é o final ("SAO PAULO", "PARANA"...).
    """
    genericas = {"CONSELHO", "REGIONAL", "FEDERAL", "DE", "DO", "DA", "DOS",
                 "DAS", "E", "ENGENHARIA", "AGRONOMIA", "ESTADO", "CAIXA",
                 "ASSISTENCIA", "PROFISSIONAIS", "A"}
    return {t for t in alvo.split() if t not in genericas and len(t) > 2}


def pncp_resolver_orgao(razao_social: str, *, cnpj_fixo: str | None = None,
                        uf: str | None = None) -> dict | None:
    """Resolve o CNPJ de um órgão no PNCP.

    `cnpj_fixo` curto-circuita a busca — usar sempre que o CNPJ for conhecido,
    porque a busca textual do PNCP casa qualquer conselho do mesmo sistema.
    """
    if cnpj_fixo:
        cnpj = re.sub(r"\D", "", cnpj_fixo)
        dados = get_json(f"{config.PNCP_API}/v1/orgaos/{cnpj}")
        if isinstance(dados, dict) and dados.get("cnpj"):
            return {"cnpj": dados["cnpj"],
                    "razaoSocial": dados.get("razaoSocial") or razao_social}
        return {"cnpj": cnpj, "razaoSocial": razao_social}

    def resolve():
        alvo = _norm(razao_social)
        distintivos = _tokens_distintivos(alvo)
        termo = " ".join(alvo.split()[:6]).lower()
        data = get_json(f"{config.PNCP_API}/v1/orgaos/",
                        {"razaoSocial": termo, "pagina": 1})
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        for o in rows:
            nome = _norm(o.get("razaoSocial", ""))
            # exige TODOS os termos distintivos — sem isso o Crea-PE respondia
            # pela busca do Crea-SP
            if distintivos and distintivos <= set(nome.split()):
                if uf and not _confere_uf(o.get("cnpj"), uf):
                    continue
                return {"cnpj": o.get("cnpj"), "razaoSocial": o.get("razaoSocial")}
        log.warning("PNCP: nenhum órgão casou com %r (%d candidatos) — ignorado",
                    razao_social, len(rows))
        return None

    key = "orgao_" + re.sub(r"\W+", "_", razao_social.lower())[:60]
    return memo(key, resolve)


def _confere_uf(cnpj: str | None, uf: str) -> bool:
    """Confirma a UF pelas unidades administrativas do órgão no PNCP."""
    if not cnpj:
        return False
    unidades = get_json(f"{config.PNCP_API}/v1/orgaos/{cnpj}/unidades")
    if not isinstance(unidades, list) or not unidades:
        return True                      # sem dado para desmentir: aceita
    return any((u.get("municipio") or {}).get("uf", {}).get("siglaUF") == uf
               or u.get("ufSigla") == uf for u in unidades)


def pncp_contratos(cnpj: str, ano_ini: int, ano_fim: int) -> list:
    out = []
    for ano in range(ano_ini, ano_fim + 1):
        out.extend(pncp_paginado("/v1/contratos", {
            "dataInicial": f"{ano}0101", "dataFinal": f"{ano}1231",
            "cnpjOrgao": cnpj, "tamanhoPagina": 500,
        }))
    return out


def pncp_instrumentos_cobranca(cnpj: str, ano_ini: int, ano_fim: int) -> list:
    out = []
    for ano in range(ano_ini, ano_fim + 1):
        out.extend(pncp_paginado("/v1/instrumentoscobranca/inclusao", {
            "dataInicial": f"{ano}0101", "dataFinal": f"{ano}1231",
            "cnpjOrgao": cnpj, "tamanhoPagina": 100,
        }))
    return out


def pncp_contratacoes(cnpj: str, ano_ini: int, ano_fim: int,
                      modalidades: range = range(1, 15)) -> list:
    out = []
    for ano in range(ano_ini, ano_fim + 1):
        for mod in modalidades:
            out.extend(pncp_paginado("/v1/contratacoes/publicacao", {
                "dataInicial": f"{ano}0101", "dataFinal": f"{ano}1231",
                "codigoModalidadeContratacao": mod, "cnpj": cnpj,
                "tamanhoPagina": 50,
            }))
    return out


# ================================================================ CNPJ
def consultar_cnpj(cnpj: str) -> dict | None:
    """Ficha cadastral de um CNPJ (fornecedores PJ apenas — nunca CPFs)."""
    cnpj = re.sub(r"\D", "", cnpj or "")
    if len(cnpj) != 14:
        return None
    for tpl in config.CNPJ_APIS:
        try:
            data = get_json(tpl.format(cnpj=cnpj))
        except RuntimeError:
            continue
        if isinstance(data, dict) and (data.get("cnpj") or data.get("razao_social")):
            return data
    return None
