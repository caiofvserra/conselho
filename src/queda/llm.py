"""Camada de IA opcional (Claude API).

Regras do projeto:
- **Sempre opcional**: sem chave de API, tudo continua funcionando com as regras
  determinísticas. O LLM só resolve o resíduo que as regras não classificaram.
- **Nunca linha a linha**: classifica os VALORES DISTINTOS (contas, centros de
  custo, objetos de contrato) — poucos milhares, não centenas de milhares.
- **Cache em disco**: cada item classificado é gravado; reexecuções não pagam
  de novo.
- **Não inventa número**: o LLM classifica e resume texto; todos os valores
  monetários vêm do cálculo determinístico.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

from . import config
from .taxonomia import GRUPOS

log = logging.getLogger("queda.llm")

MODELO_PADRAO = "claude-opus-5"
LOTE = 60                      # itens distintos por requisição
MAX_TOKENS = 16000


def _api_key() -> str | None:
    return (os.environ.get("QUEDA_ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY"))


def disponivel() -> bool:
    if not _api_key():
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        log.info("pacote `anthropic` não instalado — LLM desativado")
        return False
    return True


def _cliente():
    import anthropic
    return anthropic.Anthropic(api_key=_api_key())


# ---------------------------------------------------------------- cache
def _cache_path(nome: str):
    return config.CACHE_DIR / f"llm_{nome}.json"


def _carregar(nome: str) -> dict:
    p = _cache_path(nome)
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except (ValueError, OSError):
            return {}
    return {}


def _gravar(nome: str, dados: dict) -> None:
    p = _cache_path(nome)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)


def _chave(texto: str) -> str:
    return hashlib.sha256(texto.strip().lower().encode()).hexdigest()[:20]


# ---------------------------------------------------------------- classificação
_SISTEMA_TAXONOMIA = (
    "Você classifica rubricas de despesa de um conselho de fiscalização "
    "profissional brasileiro (Crea-SP, autarquia federal) segundo a FINALIDADE "
    "do gasto. Receberá nomes de contas contábeis, centros de custo ou objetos "
    "de contrato e devolverá a categoria de cada um.\n\n"
    "Categorias possíveis (use exatamente estes códigos):\n"
    + "\n".join(f"- {k}: {v}" for k, v in GRUPOS.items()) +
    "\n\nCritérios:\n"
    "- VERBAS_PESSOAIS cobre valores pagos a PESSOAS por deslocamento ou "
    "representação (diária, jeton, quilometragem, ajuda de custo, reembolso de "
    "viagem). Passagem comprada de agência de turismo para o conselho é "
    "CUSTEIO_ADMIN, não VERBAS_PESSOAIS.\n"
    "- FISCALIZACAO, ATENDIMENTO e INSTITUCIONAL são atividade-fim: só use "
    "quando a rubrica for claramente ligada a fiscalizar o exercício "
    "profissional, atender o profissional registrado, ou ações/convênios "
    "institucionais.\n"
    "- Locação e aluguel são CUSTEIO_ADMIN; aquisição de bem é INVESTIMENTO.\n"
    "- Se o texto for ambíguo ou genérico demais, responda OUTROS. Não invente "
    "enquadramento.\n"
)

_SCHEMA_CLASSIFICACAO = {
    "type": "object",
    "properties": {
        "classificacoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string"},
                    "categoria": {"type": "string", "enum": list(GRUPOS)},
                    "confianca": {"type": "string", "enum": ["alta", "media", "baixa"]},
                },
                "required": ["texto", "categoria", "confianca"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classificacoes"],
    "additionalProperties": False,
}


def classificar_rubricas(textos: list[str], *, modelo: str = MODELO_PADRAO) -> dict[str, str]:
    """Classifica rubricas por finalidade. Devolve {} se o LLM não estiver ativo.

    Só os itens ainda não cacheados vão para a API.
    """
    cache = _carregar("taxonomia")
    unicos = [t for t in dict.fromkeys(x for x in textos if isinstance(x, str) and x.strip())]
    pendentes = [t for t in unicos if _chave(t) not in cache]
    resultado = {t: cache[_chave(t)] for t in unicos if _chave(t) in cache}

    if not pendentes:
        return resultado
    if not disponivel():
        log.info("LLM indisponível — %d rubricas ficam com a regra determinística",
                 len(pendentes))
        return resultado

    cliente = _cliente()
    log.info("LLM: classificando %d rubricas em lotes de %d", len(pendentes), LOTE)
    for i in range(0, len(pendentes), LOTE):
        lote = pendentes[i:i + LOTE]
        try:
            resposta = cliente.messages.create(
                model=modelo,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": _SISTEMA_TAXONOMIA,
                         "cache_control": {"type": "ephemeral"}}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": _SCHEMA_CLASSIFICACAO},
                },
                messages=[{"role": "user", "content":
                           "Classifique cada rubrica abaixo:\n\n" +
                           "\n".join(f"- {t}" for t in lote)}],
            )
        except Exception as e:                       # pragma: no cover
            log.warning("LLM: lote falhou (%s) — seguindo com regras", e)
            break
        if getattr(resposta, "stop_reason", None) == "refusal":
            log.warning("LLM: requisição recusada — seguindo com regras")
            break
        texto = next((b.text for b in resposta.content if b.type == "text"), "")
        try:
            dados = json.loads(texto)
        except ValueError:
            log.warning("LLM: resposta não-JSON — lote ignorado")
            continue
        for item in dados.get("classificacoes", []):
            t, cat = item.get("texto"), item.get("categoria")
            if t and cat in GRUPOS and item.get("confianca") != "baixa":
                cache[_chave(t)] = cat
                resultado[t] = cat
    _gravar("taxonomia", cache)
    return resultado


# ---------------------------------------------------------------- atas
_SCHEMA_ATA = {
    "type": "object",
    "properties": {
        "decisoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": [
                        "convenio", "contrato", "despesa", "diaria_verba",
                        "pessoal", "normativo", "outro"]},
                    "resumo": {"type": "string"},
                    "beneficiario": {"type": "string"},
                    "valor_mencionado": {"type": "string"},
                    "indicio_conflito": {"type": "boolean"},
                    "justificativa_indicio": {"type": "string"},
                },
                "required": ["tipo", "resumo", "beneficiario",
                             "valor_mencionado", "indicio_conflito",
                             "justificativa_indicio"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisoes"],
    "additionalProperties": False,
}

_SISTEMA_ATA = (
    "Você lê deliberações de colegiados de um conselho profissional e extrai as "
    "decisões que envolvem dinheiro: convênios, contratos, despesas, verbas "
    "indenizatórias e cargos. Extraia SOMENTE o que estiver escrito no texto — "
    "nunca infira valores ou nomes ausentes; use string vazia quando o dado não "
    "constar. Marque indicio_conflito=true apenas quando o próprio texto sugerir "
    "que quem decidiu tem relação com quem se beneficia."
)


def extrair_decisoes_atas(atas: list[dict], *, modelo: str = MODELO_PADRAO,
                          limite: int = 200) -> list[dict]:
    """Extrai decisões estruturadas das deliberações. [] se LLM inativo."""
    if not disponivel() or not atas:
        return []
    cache = _carregar("atas")
    cliente = _cliente()
    saida: list[dict] = []
    for ata in atas[:limite]:
        texto = str(ata.get("DELIBERACOES") or "").strip()
        if len(texto) < 80:
            continue
        ck = _chave(texto[:4000])
        if ck in cache:
            saida.extend(cache[ck])
            continue
        try:
            resposta = cliente.messages.create(
                model=modelo, max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": _SISTEMA_ATA,
                         "cache_control": {"type": "ephemeral"}}],
                output_config={"effort": "low",
                               "format": {"type": "json_schema",
                                          "schema": _SCHEMA_ATA}},
                messages=[{"role": "user", "content":
                           f"Ata {ata.get('NUMERO')} ({ata.get('COLEGIADO')}), "
                           f"{ata.get('DATA_INICIO')}:\n\n{texto[:12000]}"}],
            )
        except Exception as e:                       # pragma: no cover
            log.warning("LLM atas: %s", e)
            break
        if getattr(resposta, "stop_reason", None) == "refusal":
            continue
        try:
            bruto = next((b.text for b in resposta.content if b.type == "text"), "")
            decisoes = json.loads(bruto).get("decisoes", [])
        except ValueError:
            continue
        for d in decisoes:
            d["ata"] = ata.get("NUMERO")
            d["colegiado"] = ata.get("COLEGIADO")
            d["data"] = ata.get("DATA_INICIO")
            d["_ano"] = ata.get("_ano")
        cache[ck] = decisoes
        saida.extend(decisoes)
    _gravar("atas", cache)
    return saida


# ---------------------------------------------------------------- narrativa
def resumo_executivo(achados_resumo: list[dict], *,
                     modelo: str = MODELO_PADRAO) -> str:
    """Redige o resumo executivo a partir dos achados JÁ calculados.

    O LLM não recebe dado bruto nem calcula nada — apenas redige sobre os
    números que a análise determinística produziu.
    """
    if not disponivel() or not achados_resumo:
        return ""
    cliente = _cliente()
    payload = json.dumps(achados_resumo, ensure_ascii=False)[:60000]
    try:
        resposta = cliente.messages.create(
            model=modelo, max_tokens=4000,
            system=[{"type": "text", "text":
                     "Você redige o resumo executivo de um relatório de "
                     "fiscalização de dados públicos, para leitores leigos. "
                     "Use SOMENTE os números fornecidos — nunca invente, "
                     "arredonde para cima ou extrapole. Escreva em português "
                     "do Brasil, tom sóbrio e factual, deixando claro que os "
                     "achados são INDÍCIOS que exigem confirmação na fonte. "
                     "Máximo de 6 parágrafos curtos."}],
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content":
                       "Achados (JSON):\n" + payload +
                       "\n\nEscreva o resumo executivo."}],
        )
    except Exception as e:                           # pragma: no cover
        log.warning("LLM resumo: %s", e)
        return ""
    if getattr(resposta, "stop_reason", None) == "refusal":
        return ""
    return "".join(b.text for b in resposta.content if b.type == "text").strip()
