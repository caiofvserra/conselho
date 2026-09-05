"""Clients das quatro fontes: Implanta, Dados Abertos Crea-SP, PNCP e CNPJ."""
from __future__ import annotations

import logging
import os
import re
from datetime import date

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
    algum_recusou = False
    ultimo_erro: Exception | None = None
    for base in _bases_para(endpoint):
        try:
            data = get_json(f"{base}/{endpoint}", params)
        except RuntimeError as e:
            # 5xx esgotado ou rede caída NESTE prefixo. Na rodada de 04/09 um
            # prefixo de reserva respondia 500 para tudo; o RuntimeError subia
            # e derrubava a etapa inteira ("2 etapa(s) falharam com erro") —
            # o prefixo que serve nem chegou a ser tentado. Aqui vira "sem
            # resposta neste prefixo"; só se TODOS falharem assim é indisponi-
            # bilidade de verdade, e o erro sobe.
            log.warning("%s em %s: %s — tentando o próximo prefixo", endpoint,
                        base[len(config.IMPLANTA_HOST):], e)
            ultimo_erro = e
            continue
        if data is None:
            algum_recusou = True           # recusado/inexistente NESTE prefixo
            continue
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
    # Nenhum respondeu: a pergunta não foi aceita em lugar nenhum. Se nenhum
    # respondeu E todos caíram por 5xx/rede, é indisponibilidade — sobe.
    if respondeu_vazio:
        return []
    if ultimo_erro is not None and not algum_recusou:
        raise ultimo_erro                  # TODOS caíram por 5xx/rede: fora do ar
    return None


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


# Mês final que o portal ACEITOU para o exercício em curso, aprendido uma vez
# por execução. Medido no ledger da rodada 10 (05/09/2026): todo endpoint anual
# devolveu HTTP 400 para `referenciaInicio=01/2026&referenciaTermino=12/2026`
# nos dois prefixos, enquanto 2011–2025 (com 12/AAAA) responderam 200. A
# hipótese forte é que o serviço rejeita período cujo mês final ainda não
# fechou; como o corpo do 400 nunca foi guardado, a fronteira exata (mês
# corrente? mês anterior?) só se descobre tentando. Chaves: "mes" = o "MM"
# que funcionou; "esgotado" = já recuamos nesta execução sem sucesso (não
# repetir o custo em cada endpoint).
_FIM_CORRENTE_APRENDIDO: dict[str, str] = {}
_RECUOS_MES_FINAL = 3


def _mes_final(ano: int) -> str:
    """Mês final ("MM") da janela anual pedida ao portal.

    Exercício encerrado: "12". Exercício em curso: o mês corrente — ou o mês
    que o recuo de `implanta_ano` já descobriu funcionar. Exercício futuro:
    "01" (janela mínima; quem chama normalmente nem pede). `date` é
    referenciado pelo módulo para que testes congelem a data sem rede.
    """
    hoje = date.today()
    a = int(ano)
    if a < hoje.year:
        return "12"
    if a > hoje.year:
        return "01"
    return _FIM_CORRENTE_APRENDIDO.get("mes") or f"{hoje.month:02d}"


def _params_ano(ano: int, fim: str, formato: str) -> dict:
    chave = "vigencia" if formato == "vigencia" else "referencia"
    return {f"{chave}Inicio": f"01/{ano}", f"{chave}Termino": f"{fim}/{ano}"}


def implanta_ano(endpoint: str, ano: int, *, formato: str = "mensal"):
    """Busca um endpoint para o exercício inteiro.

    formato: "mensal" -> referencia MM/AAAA; "vigencia" -> vigenciaInicio/Termino.

    Para o exercício em curso a janela vai até `_mes_final(ano)`. Se mesmo
    assim o portal recusar (None nos dois prefixos, que `implanta` já
    percorre), recua um mês por vez, até três vezes, e guarda o mês que
    funcionou em `_FIM_CORRENTE_APRENDIDO` para as etapas seguintes usarem
    direto. O recuo acontece UMA vez por execução: no pior caso são
    3 × 2 requisições extras, não 3 × 2 por endpoint.
    """
    fim = _mes_final(ano)
    linhas = implanta(endpoint, _params_ano(ano, fim, formato))
    if linhas is not None or int(ano) != date.today().year:
        return linhas
    if "mes" in _FIM_CORRENTE_APRENDIDO or "esgotado" in _FIM_CORRENTE_APRENDIDO:
        return None                        # já recuamos nesta execução
    _FIM_CORRENTE_APRENDIDO["esgotado"] = "1"
    mes = int(fim)
    for _ in range(_RECUOS_MES_FINAL):
        mes -= 1
        if mes < 1:
            break
        linhas = implanta(endpoint, _params_ano(ano, f"{mes:02d}", formato))
        if linhas is not None:
            _FIM_CORRENTE_APRENDIDO["mes"] = f"{mes:02d}"
            _FIM_CORRENTE_APRENDIDO.pop("esgotado", None)
            log.info("%s: o portal aceitou %s/%s como mês final do exercício em "
                     "curso (recusou %s/%s) — aprendido para as próximas etapas",
                     endpoint, f"{mes:02d}", ano, fim, ano)
            return linhas
    log.info("%s: o portal recusou o exercício %s com mês final %s e os %d "
             "recuos — janelas de %s seguem como recusadas nesta execução",
             endpoint, ano, fim, _RECUOS_MES_FINAL, ano)
    return None


# Nomes de endpoint de diárias/passagens a sondar, lidos de
# `QUEDA_DIARIAS_ENDPOINTS` (separados por vírgula). O padrão é VAZIO: os sete
# nomes que a versão anterior chutava — DiariasDeslocamentos, Diarias,
# DespesasViagens, DespesasViagem, Indenizacoes, PassagensAereas,
# PassagensTerrestres — deram 404 no prefixo documentado
# `/transparencia/api/v1.0` (cacheado) e 403 no antigo, em todas as rodadas.
# Sondar nome inexistente custava requisições e ainda fazia o diagnóstico sair
# "bloqueio 403 → mude de IP" quando o honesto é "nome do endpoint
# desconhecido". A lista fica aqui só como registro do que NÃO existe.
def diarias_probe() -> list[str]:
    """Nomes de endpoint de diárias vindos do ambiente (lidos na chamada, não
    na importação, para que uma sessão do Colab possa defini-los depois)."""
    env = getattr(config, "DIARIAS_ENDPOINTS_ENV", "QUEDA_DIARIAS_ENDPOINTS")
    return [n.strip() for n in os.environ.get(env, "").split(",") if n.strip()]


_AVISO_DIARIAS: set[str] = set()


def implanta_probe_diarias(ano: int) -> dict[str, list]:
    """Sonda os nomes de endpoint de diárias definidos no ambiente.

    Sem nome definido devolve {} SEM rede e avisa uma vez. Caminho que não
    existe é descartado para SEMPRE nesta execução: sem isso, os mesmos
    caminhos inexistentes eram tentados uma vez por exercício.
    """
    found: dict[str, list] = {}
    nomes = diarias_probe()
    if not nomes:
        if "sem-nome" not in _AVISO_DIARIAS:
            _AVISO_DIARIAS.add("sem-nome")
            log.info("sem nome de endpoint de diárias; defina %s",
                     getattr(config, "DIARIAS_ENDPOINTS_ENV", "QUEDA_DIARIAS_ENDPOINTS"))
        return found
    for ep in nomes:
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
                _params_ano(ano, _mes_final(ano), "mensal"),
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

    Custo contido de propósito — UMA requisição por nome. A documentação não
    declara parâmetro para QuadrosPessoas nem para Conselheiros, e o ledger da
    rodada 10 mostrou que eles respondem 200 [] sem parâmetro E com todas as
    4 variantes × 2 prefixos (10 requisições cada, 110 s, com um 403 de rajada
    no meio): os parâmetros são ignorados. Por isso a chamada vai ao prefixo
    aprendido (ou ao documentado) sem parâmetro e PARA na primeira resposta
    200, vazia ou não. O outro prefixo e as variantes só rodam quando a
    resposta foi None (recusa/inexistente) ou quando `QUEDA_VARIANTES=1`
    pede a sondagem completa de propósito.

    Mesma convenção de `implanta`: `[]` = algum nome respondeu vazio;
    `None` = nenhum respondeu.
    """
    chave = "|".join(candidatos)
    ordem = list(candidatos)
    aprendido = _NOME_APRENDIDO.get(chave)
    if aprendido in ordem:
        ordem.remove(aprendido)
        ordem.insert(0, aprendido)
    forcar_variantes = os.environ.get("QUEDA_VARIANTES") == "1"

    responderam: list[str] = []
    for nome in ordem:
        linhas = _implanta_tiro_unico(nome) if sem_filtro else None
        if linhas:
            _NOME_APRENDIDO[chave] = nome
            return linhas, nome
        if linhas is not None:
            responderam.append(nome)
            if not forcar_variantes:
                continue                    # 200 vazio: respondeu, e é isso
        linhas, _ = implanta_variantes(nome, ano, sem_filtro=False)
        if linhas:
            _NOME_APRENDIDO[chave] = nome
            return linhas, nome
        if linhas is not None and nome not in responderam:
            responderam.append(nome)
    return ([] if responderam else None), None


def _implanta_tiro_unico(endpoint: str):
    """`implanta(endpoint)` sem parâmetro que para no primeiro 200.

    `implanta` tenta o outro prefixo quando o primeiro vem vazio — certo para
    endpoints anuais, caro aqui: os dois prefixos são espelhos (medido em
    04/09/2026) e "vazio" no primeiro é "vazio" no segundo. Vai ao prefixo
    aprendido ou ao documentado; o outro só entra se este NÃO responder.
    """
    prefixos = list(config.IMPLANTA_PREFIXOS)
    preferido = _PREFIXO_APRENDIDO.get(endpoint)
    if preferido not in prefixos:
        preferido = getattr(config, "IMPLANTA_PREFIXO_DOCUMENTADO", prefixos[-1])
    prefixos.remove(preferido)
    for prefixo in [preferido, *prefixos]:
        try:
            data = get_json(f"{config.IMPLANTA_HOST}{prefixo}/{endpoint}", None)
        except RuntimeError as e:
            log.warning("%s em %s: %s — tentando o próximo prefixo", endpoint, prefixo, e)
            continue
        if data is None:
            continue
        linhas = data if isinstance(data, list) else (data or [])
        if linhas and _PREFIXO_APRENDIDO.get(endpoint) != prefixo:
            log.info("%s: responde com dado em %s — prefixo aprendido", endpoint, prefixo)
            _PREFIXO_APRENDIDO[endpoint] = prefixo
        return linhas
    return None


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
    # Mês final capado ao corrente: 12/AAAA do exercício em curso é o 400 que
    # a rodada 10 tomou em todo endpoint anual, e mediria a recusa, não o prefixo.
    janela = _params_ano(ano, _mes_final(ano), "mensal")
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
        # `referencia*` e `vigencia*` capados ao mês corrente (ver `_mes_final`):
        # 12/AAAA do exercício em curso é recusado com 400. `data*` fica em
        # 31/12 porque AtasColegiados aceitou fim futuro (200 [] na rodada 10).
        fim = _mes_final(ano)
        v += [{"exercicio": ano},
              _params_ano(ano, fim, "mensal"),
              {"dataInicio": f"01/01/{ano}", "dataTermino": f"31/12/{ano}"},
              _params_ano(ano, fim, "vigencia")]
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


def _pasta_swagger():
    pasta = config.RAW_DIR / "swagger"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def _nome_arquivo(url: str) -> str:
    """Nome de arquivo seguro e reconhecível para guardar o que veio de `url`."""
    from urllib.parse import urlparse
    u = urlparse(url)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{u.netloc}{u.path}").strip("_")[:120]


def _guardar_texto(pasta, nome: str, texto: str) -> None:
    try:
        (pasta / nome).write_text(texto, encoding="utf-8")
    except OSError as e:
        log.warning("não consegui guardar %s: %s", nome, e)


def _e_spec(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("paths"), dict)


def _get_json_sondagem(url: str):
    """`get_json` de sondagem: sem cache, sem espera por 403, sem retry de rede.
    Um caminho que não serve não passa a servir depois de minutos de espera."""
    try:
        return get_json(url, use_cache=False, rate_limit_retries=0, retries=0)
    except RuntimeError:
        return None


def _swagger_locais(pasta) -> list[dict]:
    """Contratos que o operador salvou à mão em `data/raw/swagger/`.

    O caminho mais curto até o contrato é o usuário copiar da aba Rede do
    navegador o JSON/YAML que a página Stoplight carrega e soltá-lo nesta
    pasta: lê-se antes de qualquer rede. YAML só se `yaml` estiver instalado
    (não é dependência do projeto); sem ele o arquivo é anunciado e pulado.
    """
    import json as _json
    achados: list[dict] = []
    if not pasta.exists():
        return achados
    for arq in sorted(pasta.iterdir()):
        suf = arq.suffix.lower()
        if suf not in (".json", ".yaml", ".yml"):
            continue
        try:
            texto = arq.read_text(encoding="utf-8", errors="replace")
            if suf == ".json":
                data = _json.loads(texto)
            else:
                try:
                    import yaml
                except ImportError:
                    log.info("%s: YAML sem o pacote `yaml` instalado — pulado", arq.name)
                    continue
                data = yaml.safe_load(texto)
        except (ValueError, OSError) as e:
            log.info("%s: não é um contrato legível (%s)", arq.name, e)
            continue
        if _e_spec(data):
            log.info("swagger local: %s (%d caminho(s))", arq.name, len(data["paths"]))
            achados.append({"fonte": "implanta (local)", "url": str(arq), "spec": data})
    return achados


_RE_SCRIPT_SRC = re.compile(r"<script[^>]+src\s*=\s*[\"']([^\"']+)[\"']", re.I)
_RE_URL_SPEC = re.compile(r"[\"'(]((?:https?:)?//?[^\"'()\s<>]*?\.(?:json|ya?ml))(?:\?[^\"'()\s<>]*)?[\"')]", re.I)
_RE_CAMINHO_API = re.compile(r"/api/v1\.0/([A-Za-z][A-Za-z0-9_]*)")
_MAX_BUNDLES = 4


def _extrair_do_bundle(js: str, base_url: str) -> tuple[list[str], set[str]]:
    """(URLs de contrato, nomes de endpoint) citados num bundle JavaScript.

    A página Stoplight é um app: a lista de endpoints que o usuário vê na tela
    está no JS, não no HTML. Os caminhos `/api/v1.0/<Nome>` que aparecem ali
    são a lista do produto — a que separa "nome errado" de "o órgão não
    publica".
    """
    from urllib.parse import urljoin
    urls: list[str] = []
    for m in _RE_URL_SPEC.finditer(js or ""):
        u = urljoin(base_url, m.group(1))
        if u.startswith("http") and u not in urls:
            urls.append(u)
    nomes = {m.group(1) for m in _RE_CAMINHO_API.finditer(js or "")}
    return urls, nomes


def _sondar_bundles(html: str, url_doc: str, pasta) -> list[dict]:
    """Baixa até 4 bundles .js do mesmo host e lê deles contrato e endpoints."""
    from urllib.parse import urljoin, urlparse
    from .http import get_text
    host = urlparse(url_doc).netloc
    achados: list[dict] = []
    vistos: list[str] = []
    for m in _RE_SCRIPT_SRC.finditer(html or ""):
        u = urljoin(url_doc, m.group(1))
        if urlparse(u).netloc != host or not urlparse(u).path.endswith(".js") or u in vistos:
            continue
        vistos.append(u)
        if len(vistos) > _MAX_BUNDLES:
            break
        r = get_text(u)
        if not r or r[0] >= 400:
            continue
        _guardar_texto(pasta, _nome_arquivo(u), r[1])
        urls_spec, nomes = _extrair_do_bundle(r[1], url_doc)
        for spec_url in urls_spec[:6]:
            data = _get_json_sondagem(spec_url)
            if _e_spec(data):
                log.info("swagger citado no bundle %s: %s (%d caminho(s))",
                         u, spec_url, len(data["paths"]))
                achados.append({"fonte": "implanta", "url": spec_url, "spec": data})
        if len(nomes) >= 5:
            log.info("bundle %s cita %d endpoint(s) /api/v1.0/<Nome>", u, len(nomes))
            achados.append({"fonte": "bundle", "url": u, "endpoints": sorted(nomes)})
    return achados


def _sondar_config_json(url_doc: str, pasta) -> list[dict]:
    """`<base>/config.json` e `<base>/assets/config.json` da página de docs.

    O usuário viu no DevTools uma requisição `config.json` ao abrir a
    documentação: em apps Angular/Stoplight é ali que costuma estar a URL da
    API ou do contrato. Só guardamos quando há chave com 'api', 'swagger' ou
    'url' — o resto é configuração de tela.
    """
    import json as _json
    from urllib.parse import urljoin
    achados: list[dict] = []
    for rel in ("config.json", "assets/config.json"):
        url = urljoin(url_doc, rel)
        data = _get_json_sondagem(url)
        if not isinstance(data, dict):
            continue
        chaves = [k for k in data if any(t in str(k).lower() for t in ("api", "swagger", "url"))]
        if not chaves:
            continue
        log.info("config.json em %s com chave(s) %s", url, chaves)
        _guardar_texto(pasta, _nome_arquivo(url), _json.dumps(data, ensure_ascii=False, indent=1))
        achados.append({"fonte": "config", "url": url, "config": data, "chaves": chaves})
    return achados


def descobrir_swagger() -> list[dict]:
    """Descobre o contrato da API: arquivos locais, página de docs, bundles,
    config.json e os caminhos usuais — nessa ordem, e devolve o que achou.

    Itens com "spec": {"fonte": "implanta"|"implanta (local)", "url", "spec"}.
    Itens sem spec: {"fonte": "bundle", "url", "endpoints"} (a
    lista de nomes de endpoint citada no JavaScript da documentação, quando
    tem 5 ou mais) e {"fonte": "config", "url", "config", "chaves"}. Quem
    consome deve olhar a chave "spec" antes de resumir.

    Sondagem sem cache e sem espera por rate-limit: um 403 aqui é "este caminho
    não serve", e insistir custaria minutos por candidato. O HTML de cada
    página de docs que responder fica em `data/raw/swagger/doc-<host>.html`,
    para que a próxima leitura seja do arquivo e não da memória de quem viu.
    """
    from urllib.parse import urlparse
    from .http import get_text
    pasta = _pasta_swagger()
    # 0) o que o operador já salvou: nada de rede se o contrato está no disco.
    achados = _swagger_locais(pasta)
    if any("spec" in a for a in achados):
        return achados
    # 1) a página de documentação declara a URL do contrato — ler é melhor que
    #    adivinhar, e vale para qualquer versão do produto.
    for url_doc in config.DOC_IMPLANTA_CANDIDATOS:
        r = get_text(url_doc)
        if not r or r[0] >= 400:
            continue
        _guardar_texto(pasta, f"doc-{urlparse(url_doc).netloc}.html", r[1])
        spec_url = extrair_api_description_url(r[1], url_doc)
        if spec_url:
            data = _get_json_sondagem(spec_url)
            if _e_spec(data):
                log.info("swagger encontrado via documentação: %s (%d caminho(s))",
                         spec_url, len(data["paths"]))
                achados.append({"fonte": "implanta", "url": spec_url, "spec": data})
                return achados
        achados.extend(_sondar_bundles(r[1], url_doc, pasta))
        achados.extend(_sondar_config_json(url_doc, pasta))
        if any("spec" in a for a in achados):
            return achados
    # 2) caminhos usuais do produto Implanta.
    for url in config.SWAGGER_CANDIDATOS_IMPLANTA:
        data = _get_json_sondagem(url)
        if _e_spec(data):
            log.info("swagger encontrado: %s (%d caminho(s))", url, len(data["paths"]))
            achados.append({"fonte": "implanta", "url": url, "spec": data})
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
    **{ep: "sondagem: referenciaInicio/referenciaTermino" for ep in diarias_probe()},
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


# Nome do parâmetro do órgão em `/v1/atas`, aprendido na primeira janela que
# responder. A consulta pública de atas NÃO foi verificada desta sessão (rede
# bloqueada para pncp.gov.br): a documentação sugere `cnpj`, mas o endpoint
# irmão `/v1/contratos` usa `cnpjOrgao`. Tentamos os dois e guardamos o que
# não deu 400.
_PNCP_ATAS_PARAM_ORGAO: dict[str, str] = {}


def pncp_atas_mes(cnpj: str, ini: str, fim: str) -> list | None:
    """Atas de registro de preços do órgão vigentes numa janela (AAAAMMDD).

    Devolve a lista (vazia quando o PNCP respondeu que não há ata) ou **None**
    quando a janela não pôde ser coletada: 400 com os dois nomes de parâmetro
    (`cnpj`, depois `cnpjOrgao` — o nome certo não foi verificado, ver
    `_PNCP_ATAS_PARAM_ORGAO`) ou 5xx/rede esgotados. O chamador trata None
    como "indisponível", nunca como "sem atas".

    A primeira página é pedida à parte, porque `pncp_paginado` não distingue
    400 (None) de "sem dados" ([]) — e é esse 400 que diz que o nome do
    parâmetro está errado. Com o nome aceito, `pncp_paginado` refaz a página 1
    do cache (`get_json` guarda o que veio com dado) e segue nas demais.
    """
    path = getattr(config, "PNCP_ATAS_PATH", "/v1/atas")
    base = {"dataInicial": ini, "dataFinal": fim, "tamanhoPagina": 500}
    aprendido = _PNCP_ATAS_PARAM_ORGAO.get("nome")
    nomes = [aprendido] if aprendido else ["cnpj", "cnpjOrgao"]
    for nome in nomes:
        params = {**base, nome: cnpj}
        try:
            primeira = get_json(f"{config.PNCP_CONSULTA}{path}", {**params, "pagina": 1},
                                retries=6, espera_base=4.0)
        except RuntimeError:
            return None
        if primeira is None:
            log.info("PNCP atas: parâmetro %r recusado (400/404) na janela %s–%s",
                     nome, ini, fim)
            continue
        if aprendido != nome:
            _PNCP_ATAS_PARAM_ORGAO["nome"] = nome
        try:
            return pncp_paginado(path, params, retries=6, espera_base=4.0)
        except RuntimeError:
            return None
    return None


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


def pncp_instrumentos_cobranca_mes(cnpj: str, ini: str, fim: str) -> list | None:
    """Notas fiscais vinculadas a contrato do órgão numa janela MENSAL.

    O endpoint aceita no máximo 30 dias por consulta. A janela anual que
    mandávamos era recusada com 422 e a mensagem exata — "Período inicial e
    final maior que 30 dias." —, mas o corpo do 4xx não era guardado, então
    `pncp_cobrancas` saía vazio em toda rodada desde que a etapa existe e
    ninguém sabia por quê. O primeiro corpo guardado (rodada 12) resolveu.

    Devolve a lista (vazia quando não há nota na janela) ou **None** quando a
    janela não pôde ser coletada — o chamador trata None como "indisponível",
    nunca como "sem nota".
    """
    params = {"dataInicial": ini, "dataFinal": fim,
              "cnpjOrgao": cnpj, "tamanhoPagina": 100}
    primeira = get_json(f"{config.PNCP_CONSULTA}/v1/instrumentoscobranca/inclusao",
                        {**params, "pagina": 1})
    if primeira is None:
        return None
    return pncp_paginado("/v1/instrumentoscobranca/inclusao", params)


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


# Nomes canônicos (normalizados) das colunas do CEIS/CNEP/CEPIM. Igualdade
# exata vem antes de "contém os trechos": no layout real do CEIS a 6ª coluna é
# "NOME INFORMADO PELO ÓRGÃO SANCIONADOR" e a 18ª é "ÓRGÃO SANCIONADOR" — casar
# por trecho na ordem do cabeçalho devolvia a 6ª, e a evidência publicada na
# rodada 10 (05/09/2026) trazia a razão social da EMPRESA na coluna do órgão.
_COLUNAS_SANCAO_CANONICAS: dict[str, tuple[str, ...]] = {
    "documento": ("CPF OU CNPJ DO SANCIONADO", "CNPJ ENTIDADE", "CPF OU CNPJ"),
    "nome": ("NOME DO SANCIONADO", "NOME ENTIDADE", "RAZAO SOCIAL - CADASTRO RECEITA"),
    "categoria": ("CATEGORIA DA SANCAO", "TIPO DE SANCAO", "MOTIVO DO IMPEDIMENTO"),
    "inicio": ("DATA INICIO SANCAO",),
    "fim": ("DATA FINAL SANCAO", "DATA FIM SANCAO"),
    "orgao": ("ORGAO SANCIONADOR", "ORGAO CONCEDENTE"),
    "esfera": ("ESFERA ORGAO SANCIONADOR",),
    "uf_orgao": ("UF ORGAO SANCIONADOR",),
    "fundamento": ("FUNDAMENTACAO LEGAL",),
    "abrangencia": ("ABRANGENCIA DEFINIDA EM DECISAO JUDICIAL",),
}
# Fallback por trechos (todos precisam aparecer), com exclusões para o caso em
# que a coluna-mãe e uma derivada compartilham os trechos.
_COLUNAS_SANCAO_TRECHOS: dict[str, tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]] = {
    # chave: ((trechos_exigidos, trechos_proibidos), ...)
    "documento": ((("CPF", "CNPJ"), ()), (("CNPJ",), ("RECEITA",))),
    "nome": ((("NOME", "SANCIONADO"), ("INFORMADO", "ORGAO")), (("NOME", "ENTIDADE"), ()),
             (("RAZAO", "SOCIAL"), ())),
    "categoria": ((("CATEGORIA", "SANCAO"), ()), (("TIPO", "SANCAO"), ()), (("MOTIVO",), ())),
    "inicio": ((("INICIO", "SANCAO"), ()), (("DATA", "INICIO"), ())),
    "fim": ((("FINAL", "SANCAO"), ()), (("FIM", "SANCAO"), ()), (("DATA", "FIM"), ())),
    "orgao": ((("ORGAO", "SANCIONADOR"), ("NOME", "INFORMADO", "UF", "ESFERA")),
              (("ORGAO", "CONCEDENTE"), ("NOME", "UF"))),
    "esfera": ((("ESFERA",), ()),),
    "uf_orgao": ((("UF", "ORGAO"), ()),),
    "fundamento": ((("FUNDAMENTACAO",), ()), (("FUNDAMENTO",), ())),
    "abrangencia": ((("ABRANGENCIA",), ()),),
}


def _resolver_colunas_sancao(colunas: list[str]) -> dict[str, str | None]:
    """Acha as colunas do CEIS/CNEP/CEPIM pelo nome, em três passos.

    1. igualdade exata do nome normalizado com um alvo canônico;
    2. senão, entre as colunas que contêm TODOS os trechos exigidos e NENHUM
       dos proibidos, a de nome mais curto (a coluna-mãe é sempre mais curta
       que a derivada: "ÓRGÃO SANCIONADOR" < "NOME INFORMADO PELO ÓRGÃO
       SANCIONADOR");
    3. senão, None — e o chamador decide.

    Os cabeçalhos têm acento, mudam de caixa entre versões e ganharam palavras
    ao longo dos anos; casar por igualdade crua quebraria na primeira mudança,
    casar só por trecho devolvia a coluna errada (ver rodada 10). O CEPIM não
    tem data de início/fim: essas chaves ficam None e o consumidor trata a
    linha como "consta no cadastro na data do download".
    """
    norm = {c: _norm_coluna(c) for c in colunas}
    inverso: dict[str, str] = {}
    for c, n in norm.items():
        inverso.setdefault(n, c)

    def resolver(chave: str) -> str | None:
        for alvo in _COLUNAS_SANCAO_CANONICAS.get(chave, ()):
            if alvo in inverso:
                return inverso[alvo]
        for exigidos, proibidos in _COLUNAS_SANCAO_TRECHOS.get(chave, ()):
            cands = [c for c, n in norm.items()
                     if all(t in n for t in exigidos) and not any(t in n for t in proibidos)]
            if cands:
                return min(cands, key=lambda c: len(norm[c]))
        return None

    mapa = {chave: resolver(chave) for chave in _COLUNAS_SANCAO_CANONICAS}
    if mapa["orgao"] and "NOME" in norm[mapa["orgao"]]:
        log.warning("coluna do órgão sancionador resolvida para %r — parece nome de "
                    "sancionado, não de órgão; conferir o cabeçalho", mapa["orgao"])
    return mapa


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


def _norm_lei(texto: str | None) -> str:
    """Normaliza um fundamento legal para casar números de lei: sem acento,
    maiúsculo, e sem pontos/espaços DENTRO dos números ("Lei 8.429/92, art. 12"
    → "LEI 8429/92 ART 12"). `_norm_coluna` troca o ponto por espaço e quebrava
    "8.429" em "8 429"."""
    import unicodedata
    t = unicodedata.normalize("NFD", str(texto or ""))
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn").upper()
    t = re.sub(r"(?<=\d)[.\s](?=\d{3}\b)", "", t)        # 8.429 / 14 133 → 8429 / 14133
    return re.sub(r"[^A-Z0-9/]+", " ", t).strip()


_RE_NACIONAL_FUNDAMENTO = re.compile(
    r"\b8429\b|IMPROBIDADE|\b9504\b|ELEITORAL|\b8443\b|\bART 46\b")
# Lei 12.846/13 (anticorrupção) NÃO entra: as sanções que o CNEP registra por
# ela — multa e publicação extraordinária (art. 6º); proibição de receber
# incentivos, subsídios e empréstimos (art. 19, IV) — não restringem contratar.
_RE_NAO_IMPEDE_CATEGORIA = re.compile(
    r"\bMULTA\b|ADVERTENCIA|PUBLICACAO EXTRAORDINARIA|INCENTIVO|SUBSIDIO|SUBVENC|EMPRESTIMO")
_RE_ENTE_FUNDAMENTO = re.compile(
    r"\b10520\b|\bART 7\b|\b156\b.{0,12}\bIII\b|\b87\b.{0,12}\bIII\b|\b12462\b|\bART 47\b")


def alcance_sancao(categoria: str | None, fundamento: str | None = None,
                   esfera: str | None = None) -> str:
    """Quem a sanção alcança: 'nacional', 'federal', 'ente', 'nao_impede' ou
    'indeterminado'.

    * 'nacional' — vale para toda a Administração: declaração de inidoneidade
      (Lei 8.666/93, art. 87, IV; Lei 14.133/21, art. 156, IV; STJ, MS
      13.964/DF), proibição de contratar por improbidade (Lei 8.429/92, art.
      12) ou eleitoral (Lei 9.504/97, art. 81, §3º), inidoneidade do TCU (Lei
      8.443/92, art. 46).
    * 'ente' — vale para o ente federativo que aplicou: impedimento (Lei
      10.520/02, art. 7º; Lei 14.133, art. 156, III e §4º) e suspensão (Lei
      8.666, art. 87, III). Um impedimento estadual ou municipal não alcança o
      Crea-SP, autarquia federal (Lei 5.194/66; STF, ADI 1.717).
    * 'federal' — impedimento/suspensão aplicados por órgão FEDERAL: alcançam
      em tese a administração federal indireta, e portanto o conselho. É
      "verificar", não acusação: a data do contrato decide.
    * 'nao_impede' — sanção que não restringe contratar: multa, advertência,
      publicação extraordinária, proibição de receber incentivos (CNEP, Lei
      12.846/13, arts. 6º e 19). Uma multa da Lei Anticorrupção não é
      inidoneidade — a rodada 11 quase publicou uma como se fosse.
    * 'indeterminado' — sem categoria nem fundamento que permitam decidir.

    A categoria consolidada do CEIS "Impedimento/proibição de contratar com
    prazo determinado" mistura os dois mundos (impedimento do pregão e
    proibição da improbidade). Por isso a decisão passa pelo FUNDAMENTO LEGAL
    quando ele existe; sem fundamento, a palavra "impedimento" na categoria
    prevalece sobre "proibição" — o erro que a rodada 10 cometeu foi o
    inverso, e classificou como nacional um impedimento estadual de R$ 199 mil.
    """
    cat = _norm_coluna(categoria or "")
    fund = _norm_lei(fundamento)
    esf = _norm_coluna(esfera or "")
    if cat and _RE_NAO_IMPEDE_CATEGORIA.search(cat) and "INIDON" not in cat \
            and "IMPEDIMENTO" not in cat and "SUSPENS" not in cat:
        return "nao_impede"
    if "INIDON" in cat or "INIDON" in fund:
        return "nacional"
    if fund and _RE_NACIONAL_FUNDAMENTO.search(fund) and not _RE_ENTE_FUNDAMENTO.search(fund):
        return "nacional"
    if "IMPEDIMENTO" in cat or "SUSPENS" in cat or (fund and _RE_ENTE_FUNDAMENTO.search(fund)):
        return "federal" if "FEDERAL" in esf else ("ente" if esf else "indeterminado")
    if "PROIBI" in cat or "IMPROBIDADE" in cat or "ELEITORAL" in cat:
        return "nacional"
    return "indeterminado"


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
# ------------------------------------------------------------------- TCU
# Colunas do TCU por TRECHO do nome normalizado. O layout não foi verificado
# contra a rede nesta sessão; por isso nada aqui exige um nome exato, e o que
# não for encontrado fica vazio em vez de virar palpite.
_COLUNAS_TCU: dict[str, tuple[tuple[str, ...], ...]] = {
    "documento": (("CPF", "CNPJ"), ("CNPJ",), ("CPF",), ("NI",)),
    "nome": (("NOME",), ("RAZAO", "SOCIAL"), ("INTERESSADO",)),
    "inicio": (("DATA", "INICIO"), ("DATA", "TRANSITO"), ("TRANSITO",), ("DATA", "ACORDAO")),
    "fim": (("DATA", "FINAL"), ("DATA", "FIM"), ("DATA", "TERMINO")),
    "processo": (("PROCESSO",), ("NUMERO", "PROCESSO")),
    "deliberacao": (("DELIBERACAO",), ("ACORDAO",)),
    "uf": (("UF",),),
}


def _resolver_colunas_tcu(colunas: list[str]) -> dict[str, str | None]:
    """Para cada campo, a coluna de nome mais CURTO que contém todos os trechos
    de alguma alternativa — a coluna-mãe é sempre mais curta que a derivada."""
    norm = {c: _norm_coluna(c) for c in colunas}
    out: dict[str, str | None] = {}
    for campo, alternativas in _COLUNAS_TCU.items():
        achada = None
        for trechos in alternativas:
            candidatas = [c for c, n in norm.items()
                          if all(t in n for t in trechos)]
            if candidatas:
                achada = min(candidatas, key=lambda c: len(norm[c]))
                break
        out[campo] = achada
    return out


def _tcu_linhas(payload) -> list[dict] | None:
    """Extrai a lista de registros de um payload do ORDS (ou de uma lista crua).

    O ORDS devolve {"items": [...], "hasMore": bool}; outras rotas devolvem a
    lista direto. Devolve None quando o payload não tem lista nenhuma — o
    chamador trata como "este caminho não serve", não como "não há registro".
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for chave in ("items", "data", "registros", "resultado"):
            v = payload.get(chave)
            if isinstance(v, list):
                return v
    return None


_TCU_URL_APRENDIDA: dict[str, str] = {}


def baixar_tcu(cadastro: str) -> tuple[list[dict] | None, dict]:
    """Uma lista de condenações do TCU → linhas normalizadas. (linhas, meta).

    `linhas` é None quando NENHUM caminho candidato respondeu com uma lista: a
    fonte fica declarada indisponível, nunca "não há ninguém na lista". O
    caminho que funcionar fica aprendido para as páginas seguintes e para as
    próximas chamadas da mesma execução.

    Nenhum destes caminhos foi verificado contra a rede na sessão em que isto
    foi escrito. O corpo de cada recusa fica em `data/raw/http-4xx/`, que é o
    que permitiu descobrir, na rodada 12, que o PNCP só aceita janela de 30
    dias — o mesmo mecanismo resolve este aqui na primeira rodada com rede.
    """
    candidatos = list(config.TCU_CADASTROS.get(cadastro, ()))
    aprendida = _TCU_URL_APRENDIDA.get(cadastro)
    if aprendida in candidatos:
        candidatos.remove(aprendida)
        candidatos.insert(0, aprendida)
    meta: dict = {"cadastro": cadastro, "tentativas": []}
    for url in candidatos:
        meta["tentativas"].append(url)
        linhas: list[dict] = []
        offset, pagina = 0, 0
        primeira = None
        while pagina < config.TCU_MAX_PAGINAS:
            try:
                payload = get_json(url, {"limit": config.TCU_PAGINA, "offset": offset})
            except RuntimeError as e:
                log.warning("tcu/%s: %s", cadastro, e)
                payload = None
            if payload is None:
                break
            rows = _tcu_linhas(payload)
            if rows is None:
                log.info("tcu/%s: %s respondeu sem lista de registros — próximo caminho",
                         cadastro, url)
                break
            if primeira is None:
                primeira = rows
            linhas.extend(rows)
            tem_mais = bool(payload.get("hasMore")) if isinstance(payload, dict) else False
            if not tem_mais or not rows:
                break
            offset += len(rows)
            pagina += 1
        if primeira is None:
            continue
        _TCU_URL_APRENDIDA[cadastro] = url
        colunas = sorted({k for r in linhas[:50] for k in r}) if linhas else []
        mapa = _resolver_colunas_tcu(colunas)
        meta.update({"url": url, "registros": len(linhas), "colunas": colunas,
                     "mapa": mapa})
        if linhas and not mapa["documento"]:
            log.warning("tcu/%s: nenhuma coluna de CPF/CNPJ em %s",
                        cadastro, colunas[:8])
            return [], meta
        out = []
        for r in linhas:
            doc = re.sub(r"\D", "", str(r.get(mapa["documento"]) or ""))
            out.append({
                "cadastro": cadastro,
                "documento": doc,
                "nome": (r.get(mapa["nome"]) or "") if mapa["nome"] else "",
                "inicio": (r.get(mapa["inicio"]) or "") if mapa["inicio"] else "",
                "fim": (r.get(mapa["fim"]) or "") if mapa["fim"] else "",
                "processo": (r.get(mapa["processo"]) or "") if mapa["processo"] else "",
                "deliberacao": (r.get(mapa["deliberacao"]) or "") if mapa["deliberacao"] else "",
                "uf": (r.get(mapa["uf"]) or "") if mapa["uf"] else "",
            })
        return out, meta
    return None, meta


_TCU_ACORDAOS_URL: dict[str, str] = {}


def tcu_acordaos_bloco(inicio: int, quantidade: int) -> list | None:
    """Um bloco de acórdãos do TCU (`recupera-acordaos?inicio&quantidade`).

    Devolve a lista (vazia quando o bloco não tem registro — fim da base) ou
    **None** quando nenhum caminho respondeu. A assinatura documentada é só de
    paginação: não há busca por texto, então quem filtra somos nós.
    """
    candidatos = list(config.TCU_ACORDAOS_URLS)
    aprendida = _TCU_ACORDAOS_URL.get("url")
    if aprendida in candidatos:
        candidatos.remove(aprendida)
        candidatos.insert(0, aprendida)
    for url in candidatos:
        try:
            data = get_json(url, {"inicio": inicio, "quantidade": quantidade})
        except RuntimeError as e:
            log.warning("tcu/acordaos: %s", e)
            return None
        if data is None:
            continue
        rows = _tcu_linhas(data)
        if rows is None:
            log.info("tcu/acordaos: %s respondeu sem lista — próximo caminho", url)
            continue
        _TCU_ACORDAOS_URL["url"] = url
        return rows
    return None


def tcu_tipos_certidoes() -> list | None:
    """Catálogo das certidões da Administração Pública Federal (uma requisição).

    Não cruza com nada sozinho: serve para DESCOBRIR quais certidões o TCU
    publica e com que nome, que é como se acha o caminho da consulta por
    CPF/CNPJ sem adivinhar. O resultado é gravado em `data/raw/sondar/` para a
    rodada seguinte — o mesmo caminho que resolveu o contrato da API do portal.
    """
    try:
        data = get_json(config.TCU_TIPOS_CERTIDOES)
    except RuntimeError as e:
        log.warning("tcu/certidoes: %s", e)
        return None
    return _tcu_linhas(data)


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
