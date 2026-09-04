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
    respondeu_vazio = False
    for base in _bases_para(endpoint):
        data = get_json(f"{base}/{endpoint}", params)
        if data is None:
            continue                       # recusado/inexistente NESTE prefixo
        linhas = data if isinstance(data, list) else (data or [])
        if linhas:
            prefixo = base[len(config.IMPLANTA_HOST):]
            if _PREFIXO_APRENDIDO.get(endpoint) != prefixo:
                log.info("%s: responde com dado em %s — prefixo aprendido",
                         endpoint, prefixo)
                _PREFIXO_APRENDIDO[endpoint] = prefixo
            return linhas
        respondeu_vazio = True
    # Vazio em todo prefixo que respondeu: o servidor disse que não há nada.
    # Nenhum respondeu: a pergunta não foi aceita em lugar nenhum.
    return [] if respondeu_vazio else None


# Prefixo que devolveu DADO para cada endpoint. A API existe sob dois prefixos
# no Crea-SP — `/portalTransparencia/api/v1.0` serve Empenhos, Pagamentos e
# Contratos; `/transparencia/api` é o que a documentação pública do produto
# usa. Um endpoint que vem vazio ou recusado no primeiro é tentado nos outros,
# e o que responder com dado passa na frente nas chamadas seguintes. Empenhos
# nunca paga esse custo: responde no primeiro e para ali.
_PREFIXO_APRENDIDO: dict[str, str] = {}


def _bases_para(endpoint: str) -> list[str]:
    prefixos = list(config.IMPLANTA_PREFIXOS)
    aprendido = _PREFIXO_APRENDIDO.get(endpoint)
    if aprendido in prefixos:
        prefixos.remove(aprendido)
        prefixos.insert(0, aprendido)
    return [config.IMPLANTA_HOST + p for p in prefixos]


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
        # `rate_limit_retries=0`: aqui um 403 é RESPOSTA, não bloqueio a esperar.
        # Estes caminhos existem no swagger sem endpoint publicado — sondar é
        # perguntar "este caminho serve?", e o WAF respondendo 403 já respondeu.
        #
        # Com retry, a segunda recusa virava RateLimited, que sobe até
        # build_staged e ABORTA A COLETA INTEIRA. Medido nas rodadas de 04/09:
        # os dois lados morreram no mesmo ponto, `DiariasDeslocamentos`, depois
        # de ~90 requisições bem-sucedidas — e as duas últimas etapas
        # (`indenizacoes_endpoint`, `pncp_contratos`) nunca chegaram a ser
        # executadas em rodada nenhuma. Uma sondagem opcional não pode matar a
        # coleta obrigatória que vem depois dela.
        # A documentação lista cinco endpoints em "Passagens, Diárias e
        # Deslocamentos" — eles EXISTEM no produto. Os 403 que tomamos foram
        # todos no prefixo antigo; o caminho só é dado por morto se falhar em
        # todos os prefixos.
        data = None
        for base in _bases_para(ep):
            data = get_json(
                f"{base}/{ep}",
                {"referenciaInicio": f"01/{ano}", "referenciaTermino": f"12/{ano}"},
                rate_limit_retries=0)
            if data is not None:
                if data:
                    _PREFIXO_APRENDIDO[ep] = base[len(config.IMPLANTA_HOST):]
                break
        if data:
            found[ep] = data
        elif data is None:
            # 404/400/403 em TODOS os prefixos: o caminho não serve
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


_NOME_APRENDIDO: dict[str, str] = {}


def implanta_primeiro_nome(candidatos: list[str], ano: int | None = None, *,
                           sem_filtro: bool = True) -> tuple[list | None, str | None]:
    """Tenta NOMES de endpoint até um devolver dado. Devolve (linhas, nome).

    A documentação lista "Rol de Mandatários" como endpoint próprio; nós
    chamávamos `Conselheiros`, que existe e responde vazio — quase certamente
    outro endpoint. Como o nome exato não está visível na captura, os
    prováveis entram em ordem e o que responder com dado fica aprendido.

    Custo contido de propósito: a primeira passada chama cada nome SEM
    parâmetro (a documentação de Quadros de Pessoas não declara nenhum), e só
    os nomes que responderam (mesmo vazio) ganham a segunda passada com os
    formatos de parâmetro. Nome que não responde em prefixo nenhum não merece
    cinco variantes.

    Mesma convenção de `implanta`: `[]` = algum nome respondeu vazio;
    `None` = nenhum respondeu.
    """
    chave = "|".join(candidatos)
    ordem = list(candidatos)
    aprendido = _NOME_APRENDIDO.get(chave)
    if aprendido in ordem:
        ordem.remove(aprendido)
        ordem.insert(0, aprendido)

    responderam: list[str] = []
    for nome in ordem:
        linhas = implanta(nome, None) if sem_filtro else None
        if linhas:
            _NOME_APRENDIDO[chave] = nome
            return linhas, nome
        if linhas is not None:
            responderam.append(nome)
    if not sem_filtro:
        responderam = ordem
    for nome in responderam:
        linhas, _ = implanta_variantes(nome, ano, sem_filtro=False)
        if linhas:
            _NOME_APRENDIDO[chave] = nome
            return linhas, nome
    return ([] if responderam else None), None


def extrair_api_description_url(html: str, base_url: str) -> str | None:
    """URL do OpenAPI declarada numa página Stoplight Elements.

    A documentação pública do produto é uma página `<elements-api
    apiDescriptionUrl="...">` (é o `web-components.min.js` do Stoplight que
    aparece na aba Rede). O atributo aponta para o contrato completo — todos os
    caminhos e parâmetros — e lê-lo é o que substitui adivinhar.
    """
    from urllib.parse import urljoin
    m = re.search(r'api[-_]?description[-_]?url\s*=\s*["\']([^"\']+)["\']', html or "", re.I)
    if not m:
        m = re.search(r'["\']([^"\']+\.(?:json|ya?ml))["\'][^>]*(?:openapi|swagger)', html or "", re.I)
    return urljoin(base_url, m.group(1)) if m else None


def medir_prefixos(ano: int) -> list[dict]:
    """Qual prefixo serve o quê — o experimento decisivo, em ~12 requisições.

    Para cada endpoint suspeito e um de controle, chama cada prefixo uma vez e
    anota status e quantidade. É o que separa "o Crea-SP não publica Quadro de
    Pessoas" de "publica, mas em outro caminho".
    """
    from .http import get_text
    import json as _json
    janela = {"referenciaInicio": f"01/{ano}", "referenciaTermino": f"12/{ano}"}
    alvos = [("Empenhos (controle)", "Empenhos", janela),
             ("QuadrosPessoas", "QuadrosPessoas", None),
             *[(f"mandatários: {n}", n, None) for n in config.ENDPOINT_CANDIDATOS["mandatarios"]],
             ("DiariasDeslocamentos", "DiariasDeslocamentos", janela),
             ("Licitacoes", "Licitacoes", janela)]
    out: list[dict] = []
    for rotulo, ep, params in alvos:
        for prefixo in config.IMPLANTA_PREFIXOS:
            r = get_text(f"{config.IMPLANTA_HOST}{prefixo}/{ep}", params)
            if r is None:
                out.append({"endpoint": rotulo, "prefixo": prefixo, "status": "erro-rede", "itens": ""})
                continue
            status, texto = r
            itens: str
            try:
                data = _json.loads(texto)
                itens = str(len(data)) if isinstance(data, list) else "objeto"
            except ValueError:
                itens = "sem JSON" if status < 400 else ""
            out.append({"endpoint": rotulo, "prefixo": prefixo, "status": status, "itens": itens})
    return out


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
def _variantes_params(ano: int | None, *, sem_filtro: bool = True) -> list[dict]:
    """`sem_filtro=False` retira a variante `{}`, que pede a tabela INTEIRA.

    Ela é indispensável na consulta de tiro único (uma foto do quadro de
    pessoal não tem exercício a filtrar) e é veneno dentro de um laço por
    exercício: se o endpoint responder tudo sem filtro, cada janela recebe o
    MESMO payload e o carimba com um ano diferente. Medido com stub: uma tabela
    de 2 linhas virou 28 linhas na série 2013-2026, todas rotuladas com o mesmo
    exercício, porque `_anos_com_sondagem` faz `r["_ano"] = ano` sobre os
    próprios dicionários devolvidos. Nenhum check por ano sobrevive a isso, e
    nada no relatório denunciaria a duplicação.
    """
    v: list[dict] = [{}] if sem_filtro else []
    if ano:
        v += [{"exercicio": ano},
              {"referenciaInicio": f"01/{ano}", "referenciaTermino": f"12/{ano}"},
              {"dataInicio": f"01/01/{ano}", "dataTermino": f"31/12/{ano}"},
              {"vigenciaInicio": f"01/{ano}", "vigenciaTermino": f"12/{ano}"}]
    return v


# Formato que já funcionou para cada endpoint, guardado pela ASSINATURA (o
# conjunto de chaves), não pelos valores — o ano muda a cada janela.
_FORMATO_APRENDIDO: dict[str, frozenset] = {}


def implanta_variantes(endpoint: str, ano: int | None = None, *,
                       sem_filtro: bool = True) -> tuple[list | None, dict]:
    """Tenta os formatos de parâmetro até um devolver dado.

    Devolve (linhas, params_vencedores), e as linhas seguem a MESMA convenção
    de `implanta`: `[]` quando o servidor respondeu que não há nada, e **None**
    quando nenhum formato obteve resposta utilizável (400, 403 de sondagem,
    payload nulo). Um 400 por formato errado era indistinguível de "não há
    dado" — esta função separa os dois casos.

    `sem_filtro=False` proíbe a variante que pede a tabela inteira — é o que os
    laços por exercício passam, e o porquê está em `_variantes_params`.

    O `None` não é detalhe de tipo. `_anos_com_sondagem` grava um checkpoint
    `.vazio` para toda janela que devolve lista, e checkpoint `.vazio` de
    exercício encerrado vale para sempre: devolver `[]` numa janela que apenas
    foi RECUSADA congelaria a recusa no Drive do operador, e a rodada seguinte
    leria "vazio" do disco sem tocar na rede — exatamente o que a correção de
    formato veio desfazer. A primeira versão desta função devolvia `[]` nos
    dois casos.

    APRENDE O FORMATO. Chamada por janela numa série de vinte exercícios, a
    sondagem cega custaria até 5 requisições por ano só para redescobrir o
    mesmo formato — caro num portal que responde 403 por rajada. O formato que
    venceu vai para `_FORMATO_APRENDIDO` e passa a ser tentado PRIMEIRO nas
    janelas seguintes; os demais continuam como reserva, porque um endpoint
    pode aceitar formatos diferentes em exercícios diferentes.
    """
    ordem = _variantes_params(ano, sem_filtro=sem_filtro)
    if not ordem:
        log.warning("%s: sem exercício e sem variante filtrada — nada a tentar",
                    endpoint)
        return None, {}
    assinatura = _FORMATO_APRENDIDO.get(endpoint)
    if assinatura is not None:
        preferido = [p for p in ordem if frozenset(p) == assinatura]
        ordem = preferido + [p for p in ordem if frozenset(p) != assinatura]

    # Alguma variante chegou a obter resposta do servidor? É essa a diferença
    # entre "não há dado neste exercício" e "nenhum formato foi aceito".
    respondeu = False
    for params in ordem:
        bruto = implanta(endpoint, params or None)
        if bruto is not None:
            respondeu = True
        linhas = achatar_grupos(bruto)
        if linhas:
            if frozenset(params) != assinatura:
                log.info("%s: %d linha(s) com params=%s", endpoint, len(linhas),
                         params or "sem parâmetros")
            _FORMATO_APRENDIDO[endpoint] = frozenset(params)
            return linhas, params
    if respondeu:
        log.info("%s: o servidor respondeu e não há linhas nesta janela", endpoint)
        return [], {}
    log.warning("%s: nenhum formato de parâmetro obteve resposta utilizável — "
                "janela tratada como NÃO coletada, sem checkpoint", endpoint)
    return None, {}


def resumir_openapi(spec: dict) -> list[dict]:
    """[{caminho, metodo, query, obrigatorios}] de um Swagger 2 ou OpenAPI 3.

    Só parâmetros declarados inline (`in: query`); `$ref` para componentes é
    ignorado de propósito — o objetivo é saber o NOME do parâmetro que cada
    endpoint aceita, e o nome está no inline em todos os portais que já vimos.
    """
    out: list[dict] = []
    base = spec.get("basePath", "") or ""
    for caminho, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        comuns = [p for p in ops.get("parameters", []) if isinstance(p, dict)]
        for metodo, op in ops.items():
            if metodo.lower() not in ("get", "post", "put", "delete", "patch"):
                continue
            if not isinstance(op, dict):
                continue
            params = comuns + [p for p in op.get("parameters", []) if isinstance(p, dict)]
            query = [p.get("name") for p in params if p.get("in") == "query" and p.get("name")]
            obrig = [p.get("name") for p in params
                     if p.get("in") == "query" and p.get("required") and p.get("name")]
            out.append({"caminho": base + caminho, "metodo": metodo.upper(),
                        "query": query, "obrigatorios": obrig})
    return out


def descobrir_swagger() -> list[dict]:
    """Tenta os caminhos usuais do contrato da API e devolve os que responderam.

    Cada item: {"fonte": "implanta"|"dados_abertos", "url", "spec"}. Sondagem
    sem cache e sem espera por rate-limit: um 403 aqui é "este caminho não
    serve", e insistir custaria minutos por candidato.
    """
    from .http import get_text
    achados: list[dict] = []
    # 1) a página de documentação declara a URL do contrato — ler é melhor que
    #    adivinhar, e vale para qualquer versão do produto.
    for url_doc in config.DOC_IMPLANTA_CANDIDATOS:
        r = get_text(url_doc)
        if not r or r[0] >= 400:
            continue
        spec_url = extrair_api_description_url(r[1], url_doc)
        if not spec_url:
            continue
        try:
            data = get_json(spec_url, use_cache=False, rate_limit_retries=0, retries=0)
        except RuntimeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("paths"), dict):
            log.info("swagger encontrado via documentação: %s (%d caminho(s))",
                     spec_url, len(data["paths"]))
            achados.append({"fonte": "implanta", "url": spec_url, "spec": data})
            return achados
    # 2) caminhos usuais
    candidatos = [("implanta", u) for u in config.SWAGGER_CANDIDATOS_IMPLANTA]
    for base in config.DADOS_ABERTOS_CANDIDATES:
        for suf in config.SWAGGER_CANDIDATOS_DADOS_ABERTOS_SUFIXOS:
            candidatos.append(("dados_abertos", base + suf))
    for fonte, url in candidatos:
        try:
            data = get_json(url, use_cache=False, rate_limit_retries=0, retries=0)
        except RuntimeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("paths"), dict):
            log.info("swagger encontrado: %s (%d caminho(s))", url, len(data["paths"]))
            achados.append({"fonte": fonte, "url": url, "spec": data})
    return achados


# Endpoint que cada etapa da coleta chama na API Implanta, e o formato de
# parâmetro que mandamos. `main.py sondar` compara isto com o que o swagger
# declara — é onde "adivinhamos errado" vira uma linha de tabela.
ENDPOINTS_IMPLANTA_USADOS: dict[str, str] = {
    "Empenhos": "referenciaInicio/referenciaTermino (MM/AAAA)",
    "Pagamentos": "referenciaInicio/referenciaTermino (MM/AAAA)",
    "Contratos": "vigenciaInicio/vigenciaTermino (MM/AAAA)",
    "ContratosAditivos": "vigenciaInicio/vigenciaTermino (MM/AAAA)",
    "Licitacoes": "sondagem: exercicio | referencia* | data* | vigencia*",
    "Convenios": "referenciaInicio/referenciaTermino (MM/AAAA)",
    "ComparativoDespesa": "referenciaInicio/referenciaTermino (MM/AAAA)",
    "DespesasCentroCusto": "referenciaInicio/referenciaTermino (MM/AAAA)",
    "DemonstrativoEmpenhosPagamentos": "referenciaInicio/referenciaTermino (MM/AAAA)",
    "PlanoDeContas": "exercicio",
    "ProcessosComprasServicos": "referenciaInicio/referenciaTermino (MM/AAAA)",
    "AtasColegiados": "sondagem: exercicio | referencia* | data* | vigencia*",
    "BalancoOrcamentario": "referenciaInicio/referenciaTermino (MM/AAAA)",
    "ComparativoReceita": "referenciaInicio/referenciaTermino (MM/AAAA)",
    "QuadrosPessoas": "sem parâmetro (a documentação não declara nenhum); variantes só se responder vazio",
    **{n: "candidato a 'Rol de Mandatários': sem parâmetro, depois variantes"
       for n in config.ENDPOINT_CANDIDATOS["mandatarios"]},
    **{ep: "sondagem: referenciaInicio/referenciaTermino" for ep in DIARIAS_PROBE},
}


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
    # "FEDERAL" NÃO é genérica: é a única palavra que separa o Confea dos 27
    # Creas. Com ela na lista, o nome do Confea ficava sem nenhum token
    # distintivo — e um conjunto vazio desligava a conferência inteira (ver
    # `_exige_nome`). Medido: um CNPJ de prefeitura passava como Confea.
    genericas = {"CONSELHO", "REGIONAL", "DE", "DO", "DA", "DOS",
                 "DAS", "E", "ENGENHARIA", "AGRONOMIA", "ESTADO", "CAIXA",
                 "ASSISTENCIA", "PROFISSIONAIS", "A"}
    return {t for t in alvo.split() if t not in genericas and len(t) > 2}


_ARTIGOS = {"DE", "DO", "DA", "DOS", "DAS", "E", "A", "O", "AS", "OS", "EM", "NO", "NA"}


def _exige_nome(nome_pncp: str, razao_esperada: str) -> bool:
    """O nome devolvido pelo PNCP é mesmo o do órgão que esperamos?

    Regra normal: TODOS os termos distintivos do esperado aparecem no nome.
    Quando o esperado não tem termo distintivo nenhum (todas as palavras são
    da lista genérica), a versão anterior fazia `if distintivos and ...` — e
    um conjunto vazio pulava a checagem, aceitando QUALQUER órgão. Aqui o
    vazio cai para a regra estrita: todas as palavras de conteúdo do esperado
    (tudo menos artigo e preposição) têm de estar no nome. Nunca "sem termos,
    sem conferência".
    """
    nome = set(_norm(nome_pncp).split())
    alvo = _norm(razao_esperada)
    distintivos = _tokens_distintivos(alvo)
    if distintivos:
        return distintivos <= nome
    conteudo = {t for t in alvo.split() if t not in _ARTIGOS}
    return bool(conteudo) and conteudo <= nome


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
    if not _exige_nome(dados.get("razaoSocial") or "", razao_esperada):
        log.warning("PNCP: CNPJ %s pertence a %r, que não casa com %r — "
                    "descartado", cnpj, dados.get("razaoSocial"), razao_esperada)
        return None
    if uf and not _confere_uf(dados.get("cnpj"), uf):
        log.warning("PNCP: CNPJ %s não confere a UF %s — descartado", cnpj, uf)
        return None
    return {"cnpj": dados["cnpj"],
            "razaoSocial": dados.get("razaoSocial") or razao_esperada,
            "_verificado": True}


def pncp_resolver_orgao(razao_social: str, *, cnpj_fixo=None,
                        uf: str | None = None,
                        termo_busca: str | None = None) -> dict | None:
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
        # `termo_busca` existe porque a busca do PNCP é por PREFIXO da razão
        # social, e o nome registrado lá nem sempre começa como o nosso: a
        # Mútua devolveu zero resultados para "mutua caixa de assistencia
        # dos profissionais" na rodada de 04/09/2026. Um termo curto traz
        # mais candidatos, e os termos distintivos continuam filtrando.
        termo = (termo_busca or " ".join(alvo.split()[:6])).lower()
        data = get_json(f"{config.PNCP_API}/v1/orgaos/",
                        {"razaoSocial": termo, "pagina": 1})
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        for o in rows:
            # exige TODOS os termos distintivos — sem isso o Crea-PE respondia
            # pela busca do Crea-SP. E sem termos distintivos, a regra estrita
            # de `_exige_nome`: conjunto vazio nunca vale por "aceita tudo".
            if _exige_nome(o.get("razaoSocial", ""), razao_social):
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


def pncp_contratacoes_mes(cnpj: str, ini: str, fim: str, modalidade: int):
    """Contratações (editais/avisos) do órgão numa janela mensal e UMA modalidade.

    O endpoint exige `codigoModalidadeContratacao`; por isso a varredura é
    modalidade × mês, cada par com checkpoint próprio no chamador. Devolve
    `None` quando o servidor não respondeu de forma utilizável (o chamador
    trata como janela não coletada, não como vazia).
    """
    try:
        rows = pncp_paginado("/v1/contratacoes/publicacao", {
            "dataInicial": ini, "dataFinal": fim, "cnpj": cnpj,
            "codigoModalidadeContratacao": modalidade, "tamanhoPagina": 50,
        }, retries=4, espera_base=4.0)
    except RuntimeError:
        return None
    return rows if isinstance(rows, list) else None


def _projetar_contratacao(r: dict) -> dict:
    return {
        "numeroControlePNCP": r.get("numeroControlePNCP"),
        "anoCompra": r.get("anoCompra"),
        "sequencialCompra": r.get("sequencialCompra"),
        "modalidadeId": r.get("modalidadeId"),
        "modalidadeNome": r.get("modalidadeNome"),
        "modoDisputaNome": r.get("modoDisputaNome"),
        "situacaoCompraNome": r.get("situacaoCompraNome"),
        "objeto": r.get("objetoCompra"),
        "valorTotalEstimado": r.get("valorTotalEstimado"),
        "valorTotalHomologado": r.get("valorTotalHomologado"),
        "dataPublicacaoPncp": r.get("dataPublicacaoPncp"),
        "dataAberturaProposta": r.get("dataAberturaProposta"),
        "processo": r.get("processo"),
        "amparoLegalNome": (r.get("amparoLegal") or {}).get("nome") if isinstance(r.get("amparoLegal"), dict) else r.get("amparoLegalNome"),
        "srp": r.get("srp"),
    }


# ============================================================ sanções (CGU)
def _norm_coluna(c: str) -> str:
    import unicodedata
    c = unicodedata.normalize("NFD", str(c or ""))
    c = "".join(ch for ch in c if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^A-Z0-9]+", " ", c.upper()).strip()


def _resolver_colunas_sancao(colunas: list[str]) -> dict[str, str | None]:
    """Acha as colunas por trecho do nome, não por nome exato.

    Os cabeçalhos do CEIS/CNEP têm acento, mudam de caixa entre versões e já
    ganharam palavras ao longo dos anos. Casar por igualdade quebraria em
    silêncio na primeira mudança; casar por trecho normalizado declara o que
    achou e o que não achou.
    """
    norm = {c: _norm_coluna(c) for c in colunas}

    def acha(*trechos):
        for c, n in norm.items():
            if all(t in n for t in trechos):
                return c
        return None

    return {
        "documento": acha("CPF", "CNPJ") or acha("CNPJ"),
        "nome": acha("NOME", "SANCIONADO") or acha("RAZAO", "SOCIAL"),
        "categoria": acha("CATEGORIA", "SANCAO") or acha("TIPO", "SANCAO"),
        "inicio": acha("INICIO", "SANCAO") or acha("DATA", "INICIO"),
        "fim": acha("FINAL", "SANCAO") or acha("FIM", "SANCAO") or acha("DATA", "FIM"),
        "orgao": acha("ORGAO", "SANCIONADOR"),
        "esfera": acha("ESFERA"),
        "uf_orgao": acha("UF", "ORGAO"),
        "fundamento": acha("FUNDAMENTACAO") or acha("FUNDAMENTO"),
    }


def baixar_sancoes(cadastro: str, hoje=None) -> tuple[list[dict] | None, dict]:
    """ZIP diário do CEIS/CNEP → linhas normalizadas. (linhas, meta).

    `linhas` é None quando nenhuma data recente respondeu (rede) — o chamador
    declara a fonte indisponível. Meta traz a data usada, a URL e o mapa de
    colunas resolvido, para o relatório dizer exatamente o que leu.
    """
    import csv
    import io as _io
    import zipfile
    from datetime import date, timedelta
    from .http import get_bytes

    tpl = config.SANCOES_CADASTROS[cadastro]
    hoje = hoje or date.today()
    meta: dict = {"cadastro": cadastro, "tentativas": []}
    for recuo in range(config.SANCOES_RECUO_DIAS + 1):
        d = hoje - timedelta(days=recuo)
        url = tpl.format(data=d.strftime("%Y%m%d"))
        meta["tentativas"].append(url)
        try:
            corpo = get_bytes(url)
        except RuntimeError as e:
            log.warning("%s: %s", cadastro, e)
            return None, meta
        if not corpo:
            continue                                   # 404: sem publicação no dia
        try:
            zf = zipfile.ZipFile(_io.BytesIO(corpo))
            nomes = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not nomes:
                log.warning("%s: ZIP sem CSV (%s)", cadastro, zf.namelist()[:3])
                continue
            texto = zf.read(nomes[0]).decode("latin-1", "replace")
        except zipfile.BadZipFile:
            log.warning("%s: resposta não é ZIP (%d bytes)", cadastro, len(corpo))
            continue
        leitor = csv.DictReader(_io.StringIO(texto), delimiter=";")
        colunas = list(leitor.fieldnames or [])
        mapa = _resolver_colunas_sancao(colunas)
        meta.update({"data": d.isoformat(), "url": url, "arquivo": nomes[0],
                     "colunas": colunas, "mapa": mapa})
        if not mapa["documento"]:
            log.warning("%s: não achei a coluna de CPF/CNPJ em %s", cadastro, colunas[:8])
            return [], meta
        linhas = []
        for r in leitor:
            doc = re.sub(r"\D", "", r.get(mapa["documento"]) or "")
            if not doc:
                continue
            linhas.append({
                "cadastro": cadastro, "documento": doc,
                "nome": r.get(mapa["nome"]) if mapa["nome"] else None,
                "categoria": r.get(mapa["categoria"]) if mapa["categoria"] else None,
                "inicio": r.get(mapa["inicio"]) if mapa["inicio"] else None,
                "fim": r.get(mapa["fim"]) if mapa["fim"] else None,
                "orgao_sancionador": r.get(mapa["orgao"]) if mapa["orgao"] else None,
                "esfera": r.get(mapa["esfera"]) if mapa["esfera"] else None,
                "uf_orgao": r.get(mapa["uf_orgao"]) if mapa["uf_orgao"] else None,
                "fundamento": r.get(mapa["fundamento"]) if mapa["fundamento"] else None,
            })
        log.info("%s: %d sanção(ões) em %s (%s)", cadastro, len(linhas), d, nomes[0])
        return linhas, meta
    return None, meta


def efeito_nacional(categoria: str | None, fundamento: str | None = None) -> bool:
    """A sanção vale para TODA a administração ou só para quem a aplicou?

    Impedimento e suspensão (8.666 art. 87 III; 14.133 art. 156 III) valem
    para o órgão/ente sancionador — um impedimento aplicado por um município
    não proíbe o Crea-SP de contratar. Declaração de inidoneidade (8.666 art.
    87 IV; 14.133 art. 156 IV) e proibição de contratar (12.846) valem para
    toda a administração. Cruzar pagamento com sanção sem esta distinção
    acusaria o conselho de contratar impedido onde não há impedimento.
    """
    t = _norm_coluna(f"{categoria or ''} {fundamento or ''}")
    return ("INIDON" in t) or ("PROIBICAO DE CONTRATAR" in t) or ("PROIBIDA DE CONTRATAR" in t)


# ====================================================== outros conselhos
def implanta_em(host: str, endpoint: str, params: dict | None = None):
    """`implanta()` apontado para OUTRO conselho da mesma API (comparação).

    Mesma convenção de retorno ([] / None), mesmos prefixos; sem aprendizado
    de prefixo por host para não misturar com o do alvo.
    """
    for prefixo in config.IMPLANTA_PREFIXOS:
        data = get_json(f"https://{host}.implanta.net.br{prefixo}/{endpoint}", params,
                        rate_limit_retries=0)
        if data is None:
            continue
        return data if isinstance(data, list) else (data or [])
    return None


def sondar_creas(ano: int) -> list[dict]:
    """Quais hosts `crea-XX.implanta.net.br` existem e respondem? Uma requisição
    cada (ComparativoDespesa de um exercício), sem espera por rate-limit."""
    out = []
    for uf, host in config.CREAS_IMPLANTA.items():
        params = {"referenciaInicio": f"01/{ano}", "referenciaTermino": f"12/{ano}"}
        try:
            data = implanta_em(host, "ComparativoDespesa", params)
        except RuntimeError:
            data = None
        out.append({"uf": uf, "host": f"{host}.implanta.net.br",
                    "responde": data is not None, "linhas": len(data) if data else 0})
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
