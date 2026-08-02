"""Camada de IA opcional, rodando um modelo LOCAL via Ollama.

Por que local, e não uma API:

* **Nada sai da máquina.** Os dados do conselho — favorecidos, valores,
  deliberações — não trafegam para servidor de terceiro. É a mesma exigência
  de não deixar rastro que rege as consultas às APIs do Crea.
* **Sem chave, sem custo por token.** Roda no Colab, roda no notebook de casa.

Regras do projeto, iguais às de antes:

- **Sempre opcional**: sem Ollama no ar, tudo continua funcionando com as
  regras determinísticas. O modelo só resolve o resíduo que as regras não
  classificaram e lê o texto que regra nenhuma lê.
- **Nunca linha a linha**: classifica os VALORES DISTINTOS (contas, centros de
  custo, objetos de contrato) — poucos milhares, não centenas de milhares.
- **Cache em disco**: cada item classificado é gravado; reexecuções não pagam
  de novo (e num modelo local o custo é tempo de GPU, que dói mais).
- **Não inventa número**: o modelo classifica e resume texto; todos os valores
  monetários vêm do cálculo determinístico.

Modelos pequenos erram mais que os grandes, e o desenho já conta com isso: a
resposta só é aceita se cair numa categoria existente, lotes são curtos, a
temperatura é zero e o esquema JSON é imposto pelo próprio Ollama.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

import requests

from . import config
from .taxonomia import GRUPOS

log = logging.getLogger("queda.llm")

# ---------------------------------------------------------------- configuração
OLLAMA_HOST = os.environ.get("QUEDA_OLLAMA_HOST", "http://localhost:11434")

# Padrão: modelo de ~4,7 GB que roda na GPU gratuita do Colab (T4) e se sai
# bem em português. Alternativas por ordem de peso:
#   llama3.2:3b        ~2,0 GB  — cabe em CPU, qualidade menor
#   qwen2.5:7b-instruct ~4,7 GB — padrão
#   gemma2:9b          ~5,4 GB
#   qwen2.5:14b-instruct ~9 GB  — melhor, exige GPU maior
MODELO_PADRAO = os.environ.get("QUEDA_LLM_MODELO", "qwen2.5:7b-instruct")

# Modelo de 7B perde precisão em lista longa: lote curto vale mais que
# throughput. Na API isso era 60.
LOTE = int(os.environ.get("QUEDA_LLM_LOTE", "20"))
NUM_CTX = 8192
TIMEOUT = 600          # geração local é lenta; melhor esperar que abortar
DESLIGADO = os.environ.get("QUEDA_SEM_LLM", "").lower() in ("1", "true", "sim")

_estado: dict = {}     # memo da verificação de disponibilidade


# ---------------------------------------------------------------- disponibilidade
def _tags() -> list[str] | None:
    """Modelos já baixados no Ollama local, ou None se o servidor não responde."""
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        return None


def _casa(modelo: str, tags: list[str]) -> bool:
    """`qwen2.5:7b-instruct` também casa com a tag `qwen2.5:7b-instruct-q4_K_M`."""
    base = modelo.split(":")[0]
    return any(t == modelo or t.startswith(modelo) or t.split(":")[0] == base
               for t in tags)


def disponivel(*, revalidar: bool = False) -> bool:
    """True quando há um Ollama no ar com o modelo baixado.

    O resultado é memorizado: `disponivel()` é chamado por várias frentes e
    não pode custar uma requisição cada vez.
    """
    if DESLIGADO:
        return False
    if not revalidar and "ok" in _estado:
        return _estado["ok"]

    tags = _tags()
    if tags is None:
        log.info("Ollama não responde em %s — as frentes de IA ficam "
                 "desligadas e o resto do relatório sai completo", OLLAMA_HOST)
        _estado["ok"] = False
    elif not _casa(MODELO_PADRAO, tags):
        log.info("Ollama no ar, mas sem o modelo %s (baixados: %s). "
                 "Rode `ollama pull %s` ou `python -c \"from queda import llm; "
                 "llm.preparar()\"`",
                 MODELO_PADRAO, ", ".join(tags) or "nenhum", MODELO_PADRAO)
        _estado["ok"] = False
    else:
        log.info("Ollama pronto: modelo %s em %s", MODELO_PADRAO, OLLAMA_HOST)
        _estado["ok"] = True
    return _estado["ok"]


LOG_SERVIDOR = "/tmp/ollama-queda.log"


def subir_servidor(espera: int = 90) -> bool:
    """Sobe `ollama serve` em segundo plano e espera até responder.

    Existe porque o modo óbvio falha em silêncio: subir o processo mandando
    a saída para /dev/null e dormir alguns segundos não diz nada quando o
    servidor morre no arranque, e o erro só aparece como "não está rodando".
    Aqui a saída vai para um arquivo e, se não subir, o log é mostrado.
    """
    import shutil
    import subprocess
    import time

    if _tags() is not None:
        return True
    if not shutil.which("ollama"):
        log.error("binário `ollama` não encontrado no PATH. Instale com:\n"
                  "  curl -fsSL https://ollama.com/install.sh | sh")
        return False

    log.info("subindo `ollama serve` (log em %s)...", LOG_SERVIDOR)
    with open(LOG_SERVIDOR, "wb") as saida:
        subprocess.Popen(["ollama", "serve"], stdout=saida, stderr=saida,
                         start_new_session=True)
    for _ in range(espera):
        time.sleep(1)
        if _tags() is not None:
            log.info("Ollama no ar em %s", OLLAMA_HOST)
            _estado.pop("ok", None)
            return True
    try:
        with open(LOG_SERVIDOR) as f:
            cauda = "".join(f.readlines()[-15:]).strip()
    except OSError:
        cauda = "(sem log)"
    log.error("Ollama não subiu em %ds. Fim do log:\n%s", espera, cauda)
    if "OLLAMA_MODELS" in os.environ and "drive" in os.environ["OLLAMA_MODELS"].lower():
        log.error("OLLAMA_MODELS aponta para o Google Drive (%s). O Drive é um "
                  "sistema de arquivos FUSE e não suporta as operações que o "
                  "Ollama usa no diretório de modelos — deixe-o em disco "
                  "local (ex.: /content/ollama).", os.environ["OLLAMA_MODELS"])
    return False


def preparar(modelo: str = MODELO_PADRAO, *, subir: bool = True) -> bool:
    """Sobe o servidor se preciso e baixa o modelo, se ainda não estiver lá.

    Devolve True quando o modelo está disponível ao fim. O download é de
    alguns GB e só acontece uma vez por máquina.
    """
    tags = _tags()
    if tags is None and subir and subir_servidor():
        tags = _tags()
    if tags is None:
        log.error("Ollama não está rodando em %s. Instale e suba o serviço:\n"
                  "  curl -fsSL https://ollama.com/install.sh | sh\n"
                  "  ollama serve &", OLLAMA_HOST)
        return False
    if _casa(modelo, tags):
        _estado["ok"] = True
        return True

    log.info("baixando %s (alguns GB, só desta vez)...", modelo)
    try:
        with requests.post(f"{OLLAMA_HOST}/api/pull", json={"model": modelo},
                           stream=True, timeout=3600) as r:
            r.raise_for_status()
            visto = ""
            for linha in r.iter_lines():
                if not linha:
                    continue
                try:
                    ev = json.loads(linha)
                except ValueError:
                    continue
                if ev.get("error"):
                    log.error("Ollama pull: %s", ev["error"])
                    return False
                if ev.get("status") and ev["status"] != visto:
                    visto = ev["status"]
                    log.info("  %s", visto)
    except Exception as e:
        log.error("falha ao baixar %s: %s", modelo, e)
        return False
    _estado.pop("ok", None)
    return disponivel(revalidar=True)


# ---------------------------------------------------------------- geração
def _extrair_json(texto: str) -> dict | None:
    """Tolera cerca de markdown e prosa em volta do JSON.

    Modelo pequeno às vezes embrulha a resposta em ```json ... ``` mesmo com
    esquema imposto; descartar o lote inteiro por isso seria desperdício.
    """
    t = (texto or "").strip()
    if not t:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    ini, fim = t.find("{"), t.rfind("}")
    if 0 <= ini < fim:
        try:
            return json.loads(t[ini:fim + 1])
        except ValueError:
            return None
    return None


def _gerar(sistema: str, usuario: str, *, schema: dict | None = None,
           modelo: str = MODELO_PADRAO, max_tokens: int = 4096) -> str:
    """Uma requisição ao Ollama. Devolve texto vazio em qualquer falha."""
    corpo = {
        "model": modelo,
        "messages": [{"role": "system", "content": sistema},
                     {"role": "user", "content": usuario}],
        "stream": False,
        # temperatura zero: classificação não é tarefa criativa e o resultado
        # precisa ser reprodutível entre rodadas
        "options": {"temperature": 0, "num_ctx": NUM_CTX,
                    "num_predict": max_tokens},
    }
    if schema:
        corpo["format"] = schema            # structured output nativo do Ollama
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/chat", json=corpo, timeout=TIMEOUT)
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content") or ""
    except Exception as e:                                  # pragma: no cover
        log.warning("Ollama: requisição falhou (%s)", e)
        return ""


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
    "- Responda APENAS o JSON pedido, com um item para CADA rubrica recebida, "
    "repetindo o texto exatamente como veio.\n"
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
            },
        }
    },
    "required": ["classificacoes"],
}


def _casar_texto(devolvido: str, lote: list[str]) -> str | None:
    """O modelo pode devolver o texto com acento ou caixa alterados."""
    if devolvido in lote:
        return devolvido
    alvo = re.sub(r"\W+", "", (devolvido or "").lower())
    if not alvo:
        return None
    for t in lote:
        if re.sub(r"\W+", "", t.lower()) == alvo:
            return t
    return None


def classificar_rubricas(textos: list[str], *,
                         modelo: str = MODELO_PADRAO) -> dict[str, str]:
    """Classifica rubricas por finalidade. Devolve {} se o modelo não estiver ativo.

    Só os itens ainda não cacheados vão ao modelo.
    """
    cache = _carregar("taxonomia")
    unicos = [t for t in dict.fromkeys(x for x in textos
                                       if isinstance(x, str) and x.strip())]
    pendentes = [t for t in unicos if _chave(t) not in cache]
    resultado = {t: cache[_chave(t)] for t in unicos if _chave(t) in cache}

    if not pendentes:
        return resultado
    if not disponivel():
        log.info("LLM indisponível — %d rubricas ficam com a regra determinística",
                 len(pendentes))
        return resultado

    log.info("LLM local (%s): classificando %d rubricas em lotes de %d",
             modelo, len(pendentes), LOTE)
    for i in range(0, len(pendentes), LOTE):
        lote = pendentes[i:i + LOTE]
        texto = _gerar(
            _SISTEMA_TAXONOMIA,
            "Classifique cada rubrica abaixo:\n\n" +
            "\n".join(f"- {t}" for t in lote),
            schema=_SCHEMA_CLASSIFICACAO, modelo=modelo, max_tokens=4096)
        dados = _extrair_json(texto)
        if not dados:
            log.warning("LLM: lote %d sem JSON aproveitável — seguindo com regras",
                        i // LOTE + 1)
            continue
        for item in dados.get("classificacoes", []):
            t = _casar_texto(item.get("texto", ""), lote)
            cat = item.get("categoria")
            # a categoria tem de existir e a confiança tem de ser declarada:
            # palpite de modelo pequeno não entra no relatório
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
            },
        }
    },
    "required": ["decisoes"],
}

_SISTEMA_ATA = (
    "Você lê deliberações de colegiados de um conselho profissional e extrai as "
    "decisões que envolvem dinheiro: convênios, contratos, despesas, verbas "
    "indenizatórias e cargos. Extraia SOMENTE o que estiver escrito no texto — "
    "nunca infira valores ou nomes ausentes; use string vazia quando o dado não "
    "constar. Marque indicio_conflito=true apenas quando o próprio texto sugerir "
    "que quem decidiu tem relação com quem se beneficia. Responda apenas o JSON."
)


def extrair_decisoes_atas(atas: list[dict], *, modelo: str = MODELO_PADRAO,
                          limite: int = 200) -> list[dict]:
    """Extrai decisões estruturadas das deliberações. [] se o modelo estiver off."""
    if not disponivel() or not atas:
        return []
    cache = _carregar("atas")
    saida: list[dict] = []
    lidas = 0
    for ata in atas[:limite]:
        texto = str(ata.get("DELIBERACOES") or "").strip()
        if len(texto) < 80:
            continue
        ck = _chave(texto[:4000])
        if ck in cache:
            saida.extend(cache[ck])
            continue
        bruto = _gerar(
            _SISTEMA_ATA,
            f"Ata {ata.get('NUMERO')} ({ata.get('COLEGIADO')}), "
            f"{ata.get('DATA_INICIO')}:\n\n{texto[:12000]}",
            schema=_SCHEMA_ATA, modelo=modelo, max_tokens=4096)
        dados = _extrair_json(bruto)
        if not dados:
            continue
        decisoes = dados.get("decisoes", [])
        for d in decisoes:
            d["ata"] = ata.get("NUMERO")
            d["colegiado"] = ata.get("COLEGIADO")
            d["data"] = ata.get("DATA_INICIO")
            d["_ano"] = ata.get("_ano")
        cache[ck] = decisoes
        saida.extend(decisoes)
        lidas += 1
        if lidas % 10 == 0:
            log.info("atas lidas: %d", lidas)
            _gravar("atas", cache)          # geração local é lenta: salva parcial
    _gravar("atas", cache)
    return saida


# ---------------------------------------------------------------- narrativa
def resumo_executivo(achados_resumo: list[dict], *,
                     modelo: str = MODELO_PADRAO) -> str:
    """Redige o resumo executivo a partir dos achados JÁ calculados.

    O modelo não recebe dado bruto nem calcula nada — apenas redige sobre os
    números que a análise determinística produziu.
    """
    if not disponivel() or not achados_resumo:
        return ""
    # contexto de 8k tokens: cabe menos que numa API de contexto longo
    payload = json.dumps(achados_resumo, ensure_ascii=False)[:12000]
    texto = _gerar(
        "Você redige o resumo executivo de um relatório de fiscalização de "
        "dados públicos, para leitores leigos. Use SOMENTE os números "
        "fornecidos — nunca invente, arredonde para cima ou extrapole. "
        "Escreva em português do Brasil, tom sóbrio e factual, deixando claro "
        "que os achados são INDÍCIOS que exigem confirmação na fonte. "
        "Máximo de 6 parágrafos curtos. Não use listas nem títulos.",
        "Achados (JSON):\n" + payload + "\n\nEscreva o resumo executivo.",
        modelo=modelo, max_tokens=2048)
    return (texto or "").strip()
