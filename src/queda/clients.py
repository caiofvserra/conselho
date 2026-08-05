"""Clients das quatro fontes: Implanta, Dados Abertos Crea-SP, PNCP e CNPJ."""
from __future__ import annotations

import logging
import re

from . import config
from .http import get_json, memo

log = logging.getLogger("queda.clients")


# ================================================================ Implanta
def implanta(endpoint: str, params: dict | None = None):
    """GET em um endpoint do Portal da Transparência (Implanta).

    Devolve a lista de linhas; `[]` quando o servidor RESPONDEU que não há
    nada; e **None** quando não houve resposta utilizável (404, 4xx
    determinístico, payload nulo).

    Separar `[]` de None não é preciosismo de tipo: é o que impede a sondagem
    retroativa de encerrar a série histórica no ano errado. Antes, um 400 por
    formato de parâmetro chegava ao pipeline como "exercício vazio"; dois anos
    assim em sequência truncavam a série sem uma linha de erro. Pior ainda com
    checkpoint por janela, que gravaria esse engano em disco e o repetiria em
    todas as rodadas seguintes.
    """
    data = get_json(f"{config.IMPLANTA_BASE}/{endpoint}", params)
    if data is None:
        return None
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
    """Sonda caminhos de diárias que o swagger define sem publicar endpoint.

    Caminho que não existe é descartado para SEMPRE nesta execução: sem isso,
    os mesmos caminhos inexistentes eram tentados uma vez por exercício.
    """
    found: dict[str, list] = {}
    for ep in DIARIAS_PROBE:
        if ep in _PROBE_MORTOS:
            continue
        data = get_json(
            f"{config.IMPLANTA_BASE}/{ep}",
            {"referenciaInicio": f"01/{ano}", "referenciaTermino": f"12/{ano}"},
            rate_limit_retries=1)
        if data:
            found[ep] = data
        elif data is None:
            # 404/400/403 na sondagem: o caminho não serve
            _PROBE_MORTOS.add(ep)
    return found


_PROBE_MORTOS: set[str] = set()


def implanta_datas(endpoint: str, ano: int):
    """Endpoints que usam dataInicio/dataTermino no formato DD/MM/AAAA."""
    return implanta(endpoint, {"dataInicio": f"01/01/{ano}",
                               "dataTermino": f"31/12/{ano}"})


def implanta_exercicio(endpoint: str, ano: int):
    """Endpoints que usam o parâmetro `exercicio`."""
    return implanta(endpoint, {"exercicio": ano})


def achatar_grupos(rows) -> list[dict]:
    """Extrai as linhas de um payload agrupado, sem adivinhar o nome do campo.

    Endpoints como QuadrosPessoas e Conselheiros devolvem grupos com uma lista
    aninhada dentro (`[{"TITULO": ..., "PESSOAS": [...]}]`). O código anterior
    procurava a lista por nome — `PESSOAS`, `CONSELHEIROS` — e devolvia vazio
    quando o campo se chamava outra coisa, sem erro nenhum. Foi assim que
    quadro_pessoal e mandatarios vieram com 0 linhas mesmo com o host de pé.

    Aqui a lista é encontrada pela ESTRUTURA (qualquer valor que seja lista de
    dicionários), os campos escalares do grupo descem para o filho com prefixo
    `_grupo_`, e um payload já plano passa direto.
    """
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        aninhadas = {k: v for k, v in r.items()
                     if isinstance(v, list) and v and isinstance(v[0], dict)}
        if not aninhadas:
            out.append(r)                       # payload já plano
            continue
        escalares = {f"_grupo_{k}": v for k, v in r.items()
                     if not isinstance(v, (list, dict))}
        for campo, filhos in aninhadas.items():
            for filho in filhos:
                if isinstance(filho, dict):
                    out.append({**escalares, "_grupo_campo": campo, **filho})
    return out


# Formatos de parâmetro aceitos pelo portal — variam por endpoint e não estão
# documentados. Tentados em ordem até um devolver dado.
def _variantes_params(ano: int | None) -> list[dict]:
    v: list[dict] = [{}]
    if ano:
        v += [{"exercicio": ano},
              {"referenciaInicio": f"01/{ano}", "referenciaTermino": f"12/{ano}"},
              {"dataInicio": f"01/01/{ano}", "dataTermino": f"31/12/{ano}"},
              {"vigenciaInicio": f"01/{ano}", "vigenciaTermino": f"12/{ano}"}]
    return v


def implanta_variantes(endpoint: str, ano: int | None = None) -> tuple[list, dict]:
    """Tenta os formatos de parâmetro até um devolver dado.

    Devolve (linhas_achatadas, params_vencedores). Um 400 por formato errado
    era indistinguível de "não há dado" — esta função separa os dois casos.
    """
    for params in _variantes_params(ano):
        bruto = implanta(endpoint, params or None)
        linhas = achatar_grupos(bruto)
        if linhas:
            log.info("%s: %d linha(s) com params=%s", endpoint, len(linhas),
                     params or "sem parâmetros")
            return linhas, params
    log.info("%s: nenhum formato de parâmetro devolveu dado", endpoint)
    return [], {}


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
            # rate_limit_retries=0: sondagem não espera. Um host que devolve
            # 403 em todas as tentativas não está limitando ritmo — está
            # barrando (WAF/bot protection). Insistir custava ~8 min por
            # candidato e não mudava o resultado.
            data = get_json(f"{base}/api/DadosAbertos/mandatarios",
                            use_cache=False, rate_limit_retries=0)
        except RuntimeError:
            continue
        if isinstance(data, dict) and data.get("result") is not None:
            log.info("Dados Abertos: host descoberto em %s", base)
            return base
    log.warning(
        "Dados Abertos: nenhum host candidato respondeu. As frentes que "
        "dependem de quadro de pessoal, mandatários e indenizações ficam "
        "desligadas — e o relatório registra isso como achado de "
        "transparência, porque a indisponibilidade da API é ela própria "
        "descumprimento da LAI.")
    return None


# A descoberta roda UMA vez por execução, inclusive quando FALHA. Sem isto,
# cada chamada a dados_abertos() refazia a varredura inteira dos candidatos —
# e stage_indenizacoes chama uma vez por exercício, o que multiplicava a
# espera por doze.
_BASE_SONDADA: dict[str, str | None] = {}


def dados_abertos_base() -> str | None:
    if "base" not in _BASE_SONDADA:
        _BASE_SONDADA["base"] = memo("dados_abertos_base", _discover_dados_abertos)
    return _BASE_SONDADA["base"]


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
def pncp_paginado(path: str, params: dict, *, max_paginas: int = 200,
                  retries: int | None = None, espera_base: float = 2.0) -> list:
    """Itera as páginas de um endpoint de consulta do PNCP."""
    out: list = []
    pagina = 1
    while pagina <= max_paginas:
        data = get_json(f"{config.PNCP_CONSULTA}{path}",
                        {**params, "pagina": pagina,
                         "tamanhoPagina": params.get("tamanhoPagina", 50)},
                        retries=retries, espera_base=espera_base)
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


def _confere_orgao(cnpj: str, razao_esperada: str, uf: str | None) -> dict | None:
    """Confirma que o CNPJ é MESMO do órgão-alvo, consultando o PNCP.

    Um CNPJ digitado errado no config não pode passar em silêncio: todo o
    bloco PNCP penderia de outro órgão. A razão social devolvida pelo PNCP
    tem de conter os termos que distinguem o alvo dos homônimos do sistema
    (é "SAO PAULO" que separa o Crea-SP dos outros 26), e a UF tem de bater.
    """
    dados = get_json(f"{config.PNCP_API}/v1/orgaos/{cnpj}")
    if not isinstance(dados, dict) or not dados.get("cnpj"):
        return None
    nome = _norm(dados.get("razaoSocial") or "")
    distintivos = _tokens_distintivos(_norm(razao_esperada))
    if distintivos and not distintivos <= set(nome.split()):
        log.warning("PNCP: CNPJ %s pertence a %r, que não casa com %r — "
                    "descartado", cnpj, dados.get("razaoSocial"), razao_esperada)
        return None
    if uf and not _confere_uf(dados.get("cnpj"), uf):
        log.warning("PNCP: CNPJ %s não confere a UF %s — descartado", cnpj, uf)
        return None
    return {"cnpj": dados["cnpj"],
            "razaoSocial": dados.get("razaoSocial") or razao_esperada,
            "_verificado": True}


def pncp_resolver_orgao(razao_social: str, *, cnpj_fixo: str | None = None,
                        uf: str | None = None) -> dict | None:
    """Resolve o CNPJ de um órgão no PNCP.

    `cnpj_fixo` aceita um CNPJ ou uma lista de candidatos: cada um é
    CONFERIDO contra o PNCP (razão social + UF) e o primeiro que passar
    vence. Nenhum candidato é aceito no escuro — um CNPJ errado no config
    faria toda a frente de contratos falar de outro conselho. Se nenhum
    confere, cai na busca textual, que também exige os termos distintivos.
    """
    candidatos = ([cnpj_fixo] if isinstance(cnpj_fixo, str)
                  else list(cnpj_fixo or []))
    for bruto in candidatos:
        cnpj = re.sub(r"\D", "", str(bruto))
        if len(cnpj) != 14:
            continue
        ok = _confere_orgao(cnpj, razao_social, uf)
        if ok:
            return ok
    if candidatos:
        log.warning("PNCP: nenhum dos CNPJs configurados para %r foi confirmado "
                    "— tentando busca por razão social", razao_social)

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


def _meses(ano_ini: int, ano_fim: int):
    """Janelas MENSAIS (AAAAMMDD, AAAAMMDD).

    O PNCP devolve 503 em janela anual — foi o que derrubou a etapa na rodada
    real. Janela de um mês responde. O custo é mais requisições; o benefício é
    a coleta terminar.
    """
    import calendar
    for ano in range(ano_ini, ano_fim + 1):
        for mes in range(1, 13):
            ultimo = calendar.monthrange(ano, mes)[1]
            yield f"{ano}{mes:02d}01", f"{ano}{mes:02d}{ultimo:02d}"


def pncp_meses(ano_ini: int, ano_fim: int):
    """Janelas mensais do PNCP, para quem precisa iterar mês a mês por fora
    (o pipeline, que grava um checkpoint por janela)."""
    return _meses(ano_ini, ano_fim)


def pncp_contratos_mes(cnpj: str, ini: str, fim: str) -> list:
    """Contratos do órgão em UMA janela mensal (AAAAMMDD, AAAAMMDD).

    Exposta separadamente para que o chamador possa gravar checkpoint por mês:
    são ~66 janelas paginadas entre 2021 e 2026, e refazer todas depois de uma
    queda de sessão é a parte mais cara da etapa do PNCP.
    """
    return pncp_paginado("/v1/contratos", {
        "dataInicial": ini, "dataFinal": fim,
        "cnpjOrgao": cnpj, "tamanhoPagina": 500,
    }, retries=6, espera_base=4.0)


def pncp_contratos(cnpj: str, ano_ini: int, ano_fim: int) -> list:
    """Contratos do órgão no PNCP, mês a mês.

    Um mês que falha não derruba a coleta: registra e segue. Perder um mês é
    perder um mês; perder a etapa inteira por causa dele custa todo o resto.
    """
    out: list = []
    falhas: list[str] = []
    for ini, fim in _meses(ano_ini, ano_fim):
        try:
            rows = pncp_contratos_mes(cnpj, ini, fim)
        except Exception as e:
            falhas.append(f"{ini[:6]} ({type(e).__name__})")
            continue
        out.extend(rows)
    if falhas:
        log.warning("PNCP contratos: %d mês(es) sem resposta — %s",
                    len(falhas), ", ".join(falhas[:12]))
    log.info("PNCP contratos: %d registro(s) para o CNPJ %s", len(out), cnpj)
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
