"""Coleta e normalização: APIs -> data/raw (JSON) -> data/staged (CSV).

A sondagem retroativa tenta anos cada vez mais antigos (meta do usuário:
alcançar exercícios pré-2016) e para após N anos vazios consecutivos.

Retomada em dois níveis. O grosso (`--retomar`) pula ETAPAS que já têm CSV em
`data/staged`. O fino, implementado aqui, pula JANELAS dentro da etapa: cada
exercício coletado vira um checkpoint em `data/raw/checkpoints/<etapa>/`, e a
etapa só vai à rede pelas janelas que faltam. É o que separa "a sessão do Colab
caiu em 2019 e voltamos de 2019" de "caiu em 2019 e a etapa inteira recomeça de
2026" — que é o que acontecia antes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import clients, config
from .http import RateLimited
from .utils import (RX_CNPJ_FORMATADO, classificar_pessoa, coluna, norm_nome,
                    parse_data, parse_valor, so_digitos)

log = logging.getLogger("queda.pipeline")

# ---------------------------------------------------------------- constantes
# MIGRAR PARA config.py: estas três não estão lá porque config.py pertence a
# outro agente nesta rodada. São parâmetros de operação, não de análise.
CHECKPOINT_SUBDIR = "checkpoints"
# Exercício ainda EM CURSO muda todo dia: um checkpoint dele vale poucas horas.
# Exercício encerrado não muda mais e o checkpoint vale para sempre.
CHECKPOINT_TTL_HORAS = 12.0
# Janela que o portal RECUSA (400) não pode virar checkpoint permanente — a
# recusa pode ser do nosso formato de parâmetro, e congelá-la desfaria a própria
# correção. Mas também não pode ser retentada em toda rodada: medido na rodada
# real de 04/09, o portal devolve 400 para o exercício 2026 em ONZE endpoints e
# para 2010-2020 no DespesasCentroCusto — ~25 requisições por rodada que nunca
# vão dar certo, num host que responde 403 por rajada (e que abortou aquela
# rodada). A recusa é anotada com validade CURTA: dentro dela a rodada seguinte
# pula a janela e gasta o ritmo no que ainda pode vir; passada, tenta de novo,
# porque a correção pode ter sido nossa ou o exercício pode ter sido publicado.
CHECKPOINT_RECUSA_TTL_HORAS = 12.0
# Recusa de exercício ENCERRADO, confirmada em duas rodadas distintas e com a
# mesma pergunta (mesmos nomes de parâmetro): o portal não vai mudar de ideia
# em 12 h. Medido na rodada 10 (05/09/2026): 16 janelas de exercícios
# encerrados (2010 em quatro endpoints, 2010-2011 no balanço, 2010-2020 no
# centro de custo) voltavam à rede duas vezes por dia, 32 requisições que
# nunca deram certo e ainda disparavam o 403 por rajada. Passam a valer 30
# dias — e a validade cai na hora se a nossa pergunta mudar, porque o marcador
# guarda a assinatura dos parâmetros.
CHECKPOINT_RECUSA_TTL_HORAS_ENCERRADO = 720.0
# Escotilha de emergência: QUEDA_CHECKPOINT=0 desliga leitura e gravação, para
# o caso de um checkpoint ruim precisar ser contornado sem apagar nada à mão.
_ENV_CHECKPOINT = "QUEDA_CHECKPOINT"


# ---------------------------------------------------------------- helpers
# Registro do que cada dataset trouxe. Um "0 linhas" no log some no scroll e
# não chega ao relatório; sem ele, meia ferramenta pode estar desligada sem
# que ninguém perceba (foi o caso de quadro_pessoal e mandatarios na primeira
# rodada real). Vira uma frente própria no dashboard.
_DIAGNOSTICO: list[dict] = []

# Contabilidade das janelas por etapa, consumida por `_salvar` para que o
# relatório de diagnóstico diga QUANTAS janelas ficaram sem coleta — não só
# quantas linhas vieram. Uma etapa com 40.000 linhas e três exercícios
# faltando parece saudável no total e não é.
_JANELAS: dict[str, dict] = {}


def _salvar(nome: str, df: pd.DataFrame) -> pd.DataFrame:
    config.STAGED_DIR.mkdir(parents=True, exist_ok=True)
    caminho = config.STAGED_DIR / f"{nome}.csv"
    anos: list[int] = []
    if df is not None and not df.empty and "_ano" in df.columns:
        anos = sorted({int(a) for a in pd.to_numeric(df["_ano"], errors="coerce")
                       .dropna().unique()})
    _DIAGNOSTICO.append({
        "dataset": nome,
        "linhas": 0 if df is None or df.empty else int(len(df)),
        "colunas": 0 if df is None or df.empty else int(df.shape[1]),
        "ano_min": anos[0] if anos else None,
        "ano_max": anos[-1] if anos else None,
        "anos_cobertos": len(anos),
        "anos_faltando": (", ".join(str(a) for a in
                                    range(anos[0], anos[-1] + 1)
                                    if a not in set(anos)) if anos else ""),
        # Quantos conselhos responderam (benchmark): sem isto o diagnóstico
        # mostrava 15.938 linhas e ninguém sabia de quantas UFs eram.
        **({"hosts": int(df["_uf"].nunique())}
           if df is not None and not df.empty and "_uf" in df.columns else {}),
        **_JANELAS.pop(nome, {}),
    })
    if df is None or df.empty:
        # DataFrame sem colunas grava um CSV vazio que pd.read_csv rejeita com
        # EmptyDataError, quebrando `analyze` e `report` depois da coleta.
        caminho.write_text("_vazio\n")
        log.info("staged/%s.csv: 0 linhas", nome)
        return pd.DataFrame() if df is None else df
    df.to_csv(caminho, index=False)
    log.info("staged/%s.csv: %d linhas", nome, len(df))
    return df


def salvar_diagnostico() -> pd.DataFrame:
    """Grava o inventário da coleta. Chamado ao fim de `fetch`."""
    d = pd.DataFrame(_DIAGNOSTICO)
    if d.empty:
        return d
    config.STAGED_DIR.mkdir(parents=True, exist_ok=True)
    d.to_csv(config.STAGED_DIR / "_coleta_diagnostico.csv", index=False)
    return d


def salvar_ledger_http() -> pd.DataFrame:
    """Grava em `staged/_coleta_http.csv` o registro das requisições da rodada.

    Quantas chamadas foram feitas, a que host, com que status: é o que separa
    "o conselho não publica este dado" de "a coleta não conseguiu perguntar".
    Sem esse registro, um 403 em rajada e uma ausência real de publicação
    chegam ao relatório com a mesma cara — zero linhas.

    O ledger é OPCIONAL. `queda.http` é a camada mais baixa do projeto e pode
    não expô-lo (numa versão anterior, ou numa instalação parcial); nesse caso
    a coleta segue sem ele, porque perder o registro de rede é um prejuízo
    pequeno perto de perder a coleta. Quando `http.salvar_ledger` existe, é ele
    que grava — a formatação do ledger é assunto de quem o mantém, não daqui.
    """
    destino = config.STAGED_DIR / "_coleta_http.csv"
    try:
        from . import http
        try:
            return http.salvar_ledger(destino)
        except AttributeError:
            bruto = http.ledger() if callable(http.ledger) else http.ledger
    except (ImportError, AttributeError):
        log.debug("queda.http não expõe ledger — sem _coleta_http.csv")
        return pd.DataFrame()
    except Exception as e:                 # forma inesperada não derruba nada
        log.warning("ledger HTTP ilegível (%s: %s) — seguindo sem ele",
                    type(e).__name__, e)
        return pd.DataFrame()
    d = (bruto if isinstance(bruto, pd.DataFrame)
         else pd.DataFrame(list(bruto.values()) if isinstance(bruto, dict)
                           else list(bruto or [])))
    if d.empty:
        return d                           # nada a dizer: não cria arquivo vazio
    config.STAGED_DIR.mkdir(parents=True, exist_ok=True)
    d.to_csv(destino, index=False)
    log.info("staged/_coleta_http.csv: %d requisição(ões) registrada(s)", len(d))
    return d


# ------------------------------------------------- checkpoint por janela
# Por que CSV e não parquet: parquet só existe com pyarrow ou fastparquet
# instalados, e nenhum dos dois está em requirements.txt (o Colab traz pyarrow,
# uma máquina limpa não). Um checkpoint que às vezes existe e às vezes não é
# pior do que não ter checkpoint. CSV também é auditável no meio de uma queda:
# o operador abre o arquivo e vê o que já veio.
#
# O preço do CSV é perder o tipo. Ele é pago na LEITURA, não na gravação: tudo
# volta como texto (`dtype=str`), e é isso que se quer, porque assim CPF/CNPJ
# com zero à esquerda e número de contrato zero-preenchido não são convertidos
# em inteiro pelo inferidor do pandas. `parse_valor` e `parse_data` já recebem
# texto no caminho normal, então nada muda para eles.
#
# A ÚNICA exceção é o booleano: "False" lido como texto é VERDADEIRO num
# `astype(bool)`, e é exatamente por `ANALITICA`/`_folha` que a ferramenta
# separa conta analítica de totalizador. Um checkpoint que devolvesse "False"
# reintroduziria, em silêncio, a soma da árvore contábil inteira — o bug que
# multiplicava a despesa por 3-4x. Por isso `_do_texto` restaura o booleano.
def _checkpoint_ligado() -> bool:
    return str(os.environ.get(_ENV_CHECKPOINT, "1")).strip().lower() not in (
        "0", "nao", "não", "false", "off")


def _checkpoint_dir(etapa: str) -> Path:
    """Pasta da etapa. `etapa` pode conter barra e virar subpasta — é como o
    PNCP separa as janelas de cada órgão (`pncp_contratos/<cnpj>/202401.csv`),
    já que o mesmo mês é buscado uma vez por CNPJ."""
    return config.RAW_DIR / CHECKPOINT_SUBDIR / etapa


def _checkpoint_arquivos(etapa: str, janela: str) -> tuple[Path, Path, Path, Path]:
    """(csv, json, marcador de vazia, marcador de recusada).

    Três arquivos para três afirmações distintas, e a distinção é o que impede
    a ferramenta de mentir sobre o fiscalizado:

    * `.csv`      — o servidor respondeu e há dado, e a linha é PLANA;
    * `.json`     — idem, mas a linha tem lista ou dicionário dentro. Um CSV
                    transformaria o aninhado em texto (`"[{'ITEM': ...}]"`) e o
                    devolveria como texto; JSON devolve o que a API mandou.
                    Sem ele, `contratos` re-coletava seus 15 exercícios em TODA
                    rodada (medido nas duas de 04/09: "0 janela(s) do
                    checkpoint, 15 coletada(s) agora", as duas vezes), e
                    `faltando` anunciava 17 exercícios pendentes numa etapa
                    que já tinha vindo inteira;
    * `.vazio`    — o servidor respondeu que NÃO HÁ nada neste exercício. Vale
                    para sempre num exercício encerrado, e é o único dos três
                    que pode sustentar "o órgão não publica";
    * `.recusado` — o servidor não aceitou a pergunta (400). Não diz nada sobre
                    o dado: diz que ainda não sabemos. Validade curta.

    Um CSV de zero linhas seria ambíguo entre os três, e foi por confundir os
    dois últimos que um 400 por formato de parâmetro já entrou no relatório
    como "exercício sem despesa".
    """
    base = _checkpoint_dir(etapa) / "".join(
        c if c.isalnum() or c in "-_" else "_" for c in str(janela))
    return (base.with_suffix(".csv"), base.with_suffix(".json"),
            base.with_suffix(".vazio"), base.with_suffix(".recusado"))


# Formato de parâmetro que cada etapa anual do Implanta manda — é a
# "assinatura" gravada no marcador `.recusado`. Se a pergunta mudar (outro
# nome de parâmetro), a recusa antiga deixa de valer sozinha, sem `limpar`.
_ETAPAS_VIGENCIA = {"contratos", "aditivos"}


def _assinatura_etapa(etapa: str, ano: int | None = None) -> str:
    """A PERGUNTA feita ao portal, para a recusa só valer contra a mesma pergunta.

    No exercício em curso entra o mês final pedido: "12/2026" deu 400 em todo
    endpoint anual da rodada 10 e "09/2026" é outra pergunta — sem isso a
    recusa gravada com o mês errado segurava o exercício corrente por 12 h
    depois de a correção já estar no código (rodada 11: nenhum endpoint
    anual foi à rede por 2026).
    """
    base = ("vigenciaInicio,vigenciaTermino" if etapa in _ETAPAS_VIGENCIA
            else "referenciaInicio,referenciaTermino")
    if ano is not None and not _exercicio_encerrado(ano):
        # Mês do CALENDÁRIO, não o que `implanta_ano` aprendeu no meio da
        # rodada: o mês aprendido vive no processo, e uma recusa assinada com
        # ele pareceria de outra pergunta na rodada seguinte — o TTL de 12 h
        # cairia sozinho. O recuo do mês já é tratado na hora do fetch.
        return f"{base};fim={datetime.now().month:02d}"
    return base


def _ler_recusa(recusado: Path) -> tuple[int, str]:
    """(quantas rodadas recusaram, assinatura) gravados no marcador.

    Marcador antigo (só o timestamp) vale como uma recusa sem assinatura."""
    try:
        partes = recusado.read_text(encoding="utf-8").strip().split("|")
    except OSError:
        return 1, ""
    n = 1
    if len(partes) >= 2:
        try:
            n = max(1, int(partes[1]))
        except ValueError:
            n = 1
    return n, (partes[2] if len(partes) >= 3 else "")


def _recusa_fresca(etapa: str, janela: str, ano: int | None = None,
                   assinatura: str | None = None) -> bool:
    """A janela foi recusada há pouco? Então não vale gastar ritmo com ela agora.

    12 h por padrão. Exercício encerrado, recusado em duas rodadas distintas
    com a MESMA pergunta: `CHECKPOINT_RECUSA_TTL_HORAS_ENCERRADO`. Pergunta
    diferente da gravada: a recusa não vale (a correção pode ter sido nossa).
    Sem assinatura (laços do benchmark e do PNCP, onde None também vem de
    403/5xx) nunca se aplica o prazo longo.
    """
    if not _checkpoint_ligado():
        return False
    *_, recusado = _checkpoint_arquivos(etapa, janela)
    if not recusado.exists():
        return False
    n, gravada = _ler_recusa(recusado)
    if assinatura and gravada and assinatura != gravada:
        return False
    idade_h = (time.time() - recusado.stat().st_mtime) / 3600.0
    ttl = CHECKPOINT_RECUSA_TTL_HORAS
    if assinatura and n >= 2 and _exercicio_encerrado(ano):
        ttl = CHECKPOINT_RECUSA_TTL_HORAS_ENCERRADO
    return idade_h <= ttl


def _marcar_recusa(etapa: str, janela: str, assinatura: str = "") -> None:
    """Anota `timestamp|n|assinatura`; `n` conta rodadas que recusaram a mesma
    pergunta (zera se a assinatura mudou)."""
    if not _checkpoint_ligado():
        return
    *_, recusado = _checkpoint_arquivos(etapa, janela)
    try:
        n = 1
        if recusado.exists():
            n_ant, gravada = _ler_recusa(recusado)
            if not assinatura or not gravada or gravada == assinatura:
                n = n_ant + 1
        recusado.parent.mkdir(parents=True, exist_ok=True)
        recusado.write_text(
            f"{datetime.now().isoformat(timespec='seconds')}|{n}|{assinatura}\n")
    except OSError as e:
        log.debug("não foi possível anotar a recusa %s/%s (%s)", etapa, janela, e)


def _exercicio_encerrado(ano: int | None) -> bool:
    """Exercício já fechado — o dado dele não muda mais.

    Usa o relógio, não `config.ANO_ATUAL`: quando a virada de ano chegar sem
    que ninguém edite o config, o exercício novo continua sendo tratado como
    aberto (e portanto recoletado), que é o comportamento seguro.
    """
    try:
        return int(ano) < datetime.now().year
    except (TypeError, ValueError):
        return False


def _janela_encerrada(janela: str | None, ano: int | None) -> bool:
    """Janela que não muda mais: exercício encerrado, ou mês (rótulo AAAAMM)
    anterior ao mês corrente. Sem isso, todo mês já fechado de 2026 no PNCP
    cairia no TTL de 12 h do "exercício em curso" e voltaria à rede duas vezes
    por dia."""
    if janela and re.fullmatch(r"\d{6}", str(janela)):
        agora = datetime.now()
        return int(janela) < agora.year * 100 + agora.month
    return _exercicio_encerrado(ano)


def _checkpoint_utilizavel(caminho: Path, ano: int | None,
                           janela: str | None = None) -> bool:
    """Checkpoint de exercício (ou mês) encerrado vale sempre; do período em
    curso, só por algumas horas — senão a rodada de dezembro continuaria
    mostrando o exercício como ele estava em janeiro."""
    if not caminho.exists():
        return False
    if _janela_encerrada(janela, ano):
        return True
    idade_h = (time.time() - caminho.stat().st_mtime) / 3600.0
    if idade_h <= CHECKPOINT_TTL_HORAS:
        return True
    log.info("checkpoint %s tem %.1fh e o exercício ainda está em curso — "
             "será recoletado", caminho.name, idade_h)
    return False


def _do_texto(v: str):
    """Desfaz o que o CSV fez com os tipos que importam (ver bloco acima)."""
    if v == "":
        return None                       # ausência: era None/NaN antes do CSV
    if v == "True":
        return True
    if v == "False":
        return False
    return v


def _checkpoint_ler(etapa: str, janela: str, ano: int | None = None):
    """Linhas já coletadas desta janela.

    Devolve `None` para "ainda não tentamos" e `[]` para "tentamos, e o
    servidor respondeu que está vazio". A distinção não é cosmética: é ela que
    impede um checkpoint de encerrar a sondagem retroativa cedo demais.
    """
    if not _checkpoint_ligado():
        return None
    csv, jsn, vazio, _recusa = _checkpoint_arquivos(etapa, janela)
    if _checkpoint_utilizavel(csv, ano, janela):
        try:
            d = pd.read_csv(csv, dtype=str, keep_default_na=False,
                            low_memory=False)
        except (pd.errors.EmptyDataError, ValueError, OSError) as e:
            log.warning("checkpoint %s ilegível (%s) — será recoletado",
                        csv.name, type(e).__name__)
            return None
        return [{k: _do_texto(v) for k, v in r.items()}
                for r in d.to_dict("records")]
    if _checkpoint_utilizavel(jsn, ano, janela):
        try:
            linhas = json.loads(jsn.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            log.warning("checkpoint %s ilegível (%s) — será recoletado",
                        jsn.name, type(e).__name__)
            return None
        return linhas if isinstance(linhas, list) else None
    if _checkpoint_utilizavel(vazio, ano, janela):
        return []
    return None


def _janela_estado(etapa: str, janela: str, ano: int | None = None,
                   assinatura: str | None = None) -> str | None:
    """`"linhas"`, `"vazio"`, `"vazio_expirado"`, `"recusado"` ou `None` — sem
    abrir o arquivo.

    `_checkpoint_ler` responde a mesma pergunta, mas lendo o arquivo inteiro:
    para o inventário são 17 etapas × ~14 exercícios, e ler centenas de
    milhares de linhas só para saber se o arquivo existe transformaria um
    comando de conferência num comando demorado.
    """
    if not _checkpoint_ligado():
        return None
    csv, jsn, vazio, _recusa = _checkpoint_arquivos(etapa, janela)
    if (_checkpoint_utilizavel(csv, ano, janela)
            or _checkpoint_utilizavel(jsn, ano, janela)):
        return "linhas"
    if _checkpoint_utilizavel(vazio, ano, janela):
        return "vazio"
    if _recusa_fresca(etapa, janela, ano, assinatura):
        return "recusado"
    # Marcador de vazio EXPIRADO — só acontece no exercício em curso, cujo
    # checkpoint vale 12h. A janela volta à rede, mas o que ela devolveu da
    # última vez foi vazio, e é isso que decide se a sondagem retroativa para
    # aqui. Sem o estado próprio, o inventário anunciava 16 exercícios
    # pendentes em `atas` numa rodada em que a coleta fez UMA requisição.
    if vazio.exists():
        return "vazio_expirado"
    return None


def janelas_pendentes(etapa: str, ate: int, desde: int | None) -> list[int]:
    """Exercícios que esta etapa iria buscar NA REDE, sem tocar na rede.

    É um ensaio de `_anos_com_sondagem`: mesma ordem (de `ate` para trás),
    mesmo piso, mesmo critério de parada da sondagem retroativa. Escrito como
    espelho de propósito — uma reimplementação com regra própria passaria a
    divergir do laço real na primeira vez que ele mudasse, e um inventário que
    mente sobre o que falta é pior que nenhum inventário.

    Janela sem checkpoint zera o contador de vazios pelo mesmo motivo que no
    laço real: com um exercício desconhecido no meio, não dá para afirmar que
    dois vazios foram consecutivos.

    Consequência do TTL que vale declarar: o checkpoint do exercício EM CURSO
    expira em 12h, então passadas 12h o ano corrente reaparece como pendente e
    a etapa é refeita. Isso é o comportamento desejado — o exercício aberto
    muda todo dia —, e o custo é uma requisição por etapa, porque as demais
    janelas continuam saindo do disco. O que mudou é o significado de rodar
    duas vezes seguidas: dentro das 12h, quase de graça; depois delas, a rodada
    atualiza o exercício em curso em vez de não fazer nada.
    """
    pendentes: list[int] = []
    vazios = 0
    ano = ate
    piso = desde or config.ANO_PISO
    while ano >= piso:
        estado = _janela_estado(etapa, str(ano), ano, _assinatura_etapa(etapa, ano))
        if estado is None:
            pendentes.append(ano)
            vazios = 0
        elif estado == "vazio_expirado":
            # Vai à rede (o exercício em curso muda todo dia), mas da última vez
            # veio vazio — e é isso que decide se a sondagem retroativa para
            # aqui. Sem contar como vazio, o inventário anunciava 16 exercícios
            # pendentes em `atas` numa rodada em que a coleta fez UMA
            # requisição e parou nos dois vazios seguidos (medido em 04/09).
            pendentes.append(ano)
            vazios += 1
            if desde is None and vazios >= config.SONDAGEM_VAZIOS:
                break
        elif estado == "recusado":
            # Não entra em "pendente" porque a rodada NÃO vai buscá-la agora —
            # o inventário tem de prever o que a coleta faz, não o que seria
            # bom. Sai numa coluna própria, para não passar por coletada.
            vazios = 0
        elif estado == "vazio":
            vazios += 1
            if desde is None and vazios >= config.SONDAGEM_VAZIOS:
                break
        else:
            vazios = 0
        ano -= 1
    return pendentes


def _plano_para_csv(rows: list) -> bool:
    """Só vira checkpoint o payload PLANO.

    Uma lista ou um dicionário aninhado dentro da linha viram texto no CSV
    (`"[{'NOME_ITEM': ...}]"`) e voltam como esse texto — é o formato de
    ProcessosComprasServicos, que carrega os ITENS dentro de cada processo.
    Melhor ficar sem checkpoint do que ter um que corrompe em silêncio.

    A checagem é por amostra (payload de API é homogêneo) para não varrer
    100.000 linhas a cada exercício.
    """
    amostra = rows[:100] + rows[-100:]
    for r in amostra:
        if not isinstance(r, dict):
            return False
        if any(isinstance(v, (list, dict, tuple, set)) for v in r.values()):
            return False
    return True


def _checkpoint_gravar(etapa: str, janela: str, rows: list) -> None:
    """Grava a janela. NUNCA é chamada para janela que falhou — checkpoint de
    falha viraria falha permanente."""
    if not _checkpoint_ligado():
        return
    csv, jsn, vazio, _recusa = _checkpoint_arquivos(etapa, janela)
    try:
        csv.parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            vazio.write_text(f"{datetime.now().isoformat(timespec='seconds')}\n")
            csv.unlink(missing_ok=True)
            _recusa.unlink(missing_ok=True)
            return
        # `_ano` é reposto pelo chamador como inteiro a cada leitura; gravá-lo
        # faria o rótulo do exercício voltar como texto e quebrar toda
        # comparação numérica de ano nos checks.
        limpos = [{k: v for k, v in r.items() if k != "_ano"} for r in rows]
        # Gravação atômica nos dois formatos: uma queda de sessão no meio da
        # escrita deixaria um arquivo truncado que a rodada seguinte leria como
        # se fosse completo.
        if _plano_para_csv(rows):
            parcial = csv.with_suffix(".csv.parcial")
            pd.DataFrame(limpos).to_csv(parcial, index=False)
            os.replace(parcial, csv)
            jsn.unlink(missing_ok=True)
        else:
            # Aninhado. Antes esta janela simplesmente NÃO virava checkpoint —
            # num `log.debug` que ninguém vê —, e a etapa inteira voltava à
            # rede em toda rodada. JSON devolve exatamente o que a API mandou,
            # que é o que o CSV não consegue fazer com lista dentro de linha.
            parcial = jsn.with_suffix(".json.parcial")
            parcial.write_text(json.dumps(limpos, ensure_ascii=False),
                               encoding="utf-8")
            os.replace(parcial, jsn)
            csv.unlink(missing_ok=True)
        vazio.unlink(missing_ok=True)
        _recusa.unlink(missing_ok=True)
    except OSError as e:
        # Disco cheio ou Drive desmontado não podem derrubar a coleta: o
        # checkpoint é otimização, não requisito.
        log.warning("não foi possível gravar checkpoint %s/%s (%s) — a coleta "
                    "segue, só não haverá retomada fina desta janela",
                    etapa, janela, e)


def _registrar_janelas(dataset: str, falhas: list, do_disco: int,
                       coletadas: int, recusadas: list | None = None) -> None:
    """Fecha a contabilidade de janelas de uma etapa: log + diagnóstico.

    Acumula em vez de sobrescrever porque uma etapa pode ter mais de um laço de
    janelas (o PNCP tem um por órgão) e o que interessa ao operador é o total
    que ficou de fora do dataset.
    """
    log.info("%s: %d janela(s) do checkpoint, %d coletada(s) agora, "
             "%d sem coleta", dataset, do_disco, coletadas, len(falhas))
    recusadas = list(recusadas or [])
    # Recusa e falha ficam separadas na mensagem porque a instrução ao operador
    # é OPOSTA: falha de rede pede "rode de novo"; recusa do portal (400) não
    # melhora rodando de novo, e dizer que melhora manda o operador queimar
    # ritmo num host que responde 403 por rajada.
    so_falharam = [f for f in falhas if f not in set(recusadas)]
    if so_falharam:
        log.warning("%s: %d janela(s) ficaram FORA por falha de rede (%s). "
                    "Rode de novo: as janelas já gravadas em checkpoint não "
                    "são refeitas e só estas voltam à rede.", dataset,
                    len(so_falharam),
                    ", ".join(str(f) for f in so_falharam[:12]))
    if recusadas:
        # Sem afirmar o status: aqui chega tanto o 400 (pergunta não aceita)
        # quanto host que não existe ou não respondeu de forma utilizável — o
        # ledger `_coleta_http.csv` tem o código exato de cada um.
        log.warning("%s: %d janela(s) sem resposta utilizável (%s) — recusa ou "
                    "caminho que não serve. Anotado por %.0fh: a rodada seguinte "
                    "não insiste nelas e gasta o ritmo no que ainda pode vir. "
                    "Isso NÃO afirma que o exercício está vazio.", dataset,
                    len(recusadas), ", ".join(str(f) for f in recusadas[:12]),
                    CHECKPOINT_RECUSA_TTL_HORAS)
    d = _JANELAS.setdefault(dataset, {"janelas_sem_coleta": 0})
    d["janelas_sem_coleta"] += len(falhas)
    if recusadas:
        texto = ", ".join(str(r) for r in recusadas)
        d["janelas_recusadas"] = ", ".join(
            x for x in (d.get("janelas_recusadas"), texto) if x)
    if falhas:
        texto = ", ".join(str(f) for f in falhas)
        d["janelas_falhas"] = ", ".join(x for x in (d.get("janelas_falhas"),
                                                    texto) if x)


def _janelas_nomeadas(etapa: str, janelas, buscar, *, dataset: str | None = None,
                      assinatura: str = ""):
    """Percorre janelas arbitrárias com checkpoint, tolerando falhas.

    `janelas` é um iterável de `(rótulo, ano, *args)` e `buscar(*args)` devolve
    as linhas da janela (ou None, quando o host não respondeu). Serve aos laços
    que não são "um exercício por vez" — os meses do PNCP e os exercícios de
    cada Crea no benchmark.

    Toda linha sai carimbada com `_ano` (o `ano` da tupla), venha da rede ou do
    checkpoint — o checkpoint não grava `_ano` (ver `_checkpoint_gravar`), e o
    benchmark ficou 15.938 linhas sem exercício por isso: a frente quebrava
    antes de produzir um achado (rodadas 9 e 10). Quem precisa de outro ano
    (o PNCP usa `anoContrato`) sobrescreve depois.

    `assinatura` vazia: recusa vale 12 h e nunca o prazo longo — nestes laços
    None também vem de 403/5xx do host, não só de 400 do portal.
    """
    out: list[dict] = []
    falhas: list[str] = []
    do_disco = coletadas = 0
    recusadas: list[str] = []
    for rotulo, ano, *args in janelas:
        rows = _checkpoint_ler(etapa, rotulo, ano)
        if rows is not None:
            do_disco += 1
        elif _recusa_fresca(etapa, rotulo, ano, assinatura):
            recusadas.append(rotulo)
            continue
        else:
            try:
                rows = buscar(*args)
            except RateLimited:
                raise
            except Exception as e:
                log.warning("%s/%s: janela falhou (%s: %s) — sem checkpoint",
                            etapa, rotulo, type(e).__name__, e)
                falhas.append(rotulo)
                continue
            if not isinstance(rows, list):
                log.warning("%s/%s: sem resposta utilizável", etapa, rotulo)
                _marcar_recusa(etapa, rotulo, assinatura)
                falhas.append(rotulo)
                recusadas.append(rotulo)
                continue
            _checkpoint_gravar(etapa, rotulo, rows)
            coletadas += 1
        if ano is not None:
            for r in rows:
                if isinstance(r, dict):
                    r["_ano"] = ano
        out.extend(rows)
    _registrar_janelas(dataset or etapa, falhas, do_disco, coletadas,
                       recusadas=recusadas)
    return out


# ------------------------------------------------- ingestão manual
# Seis datasets voltam vazios da coleta automática — não porque o órgão não os
# publique, mas porque o WAF barra o cliente. O portal serve os mesmos dados a
# um navegador logado, e o operador consegue baixá-los à mão.
#
# Este é o caminho para trazê-los. Basta salvar o arquivo como
# `<QUEDA_DATA_DIR>/manual/<dataset>.json` (ou `.csv`) e rodar de novo: a etapa
# usa o arquivo e NÃO vai à rede. Os nomes válidos são os das etapas em
# `build_staged` — `mandatarios`, `quadro_pessoal`, `atas`, `licitacoes`,
# `processos_itens`, `indenizacoes_endpoint`.
#
# O JSON pode vir cru (lista) ou dentro do envelope que o portal usa
# (`{"result": true, "data": [...]}`) — as duas formas são aceitas, porque o
# operador salva o que o navegador mostrou, não o que seria conveniente para
# nós.
#
# A procedência é registrada no diagnóstico: um número obtido à mão não pode
# se confundir com um número que a ferramenta coletou sozinha, porque ele não
# é reproduzível por quem só rodar o código.
MANUAL_SUBDIR = "manual"


def manual_rows(nome: str) -> list[dict] | None:
    """Registros de `data/manual/<nome>.json|csv`, ou None se não houver."""
    pasta = config.DATA_DIR / MANUAL_SUBDIR
    alvo_json, alvo_csv = pasta / f"{nome}.json", pasta / f"{nome}.csv"
    try:
        if alvo_json.exists():
            bruto = json.loads(alvo_json.read_text(encoding="utf-8"))
            rows = bruto
            if isinstance(bruto, dict):
                for chave in ("data", "result", "items", "content", "registros"):
                    if isinstance(bruto.get(chave), list):
                        rows = bruto[chave]
                        break
                else:
                    rows = [bruto]
            if not isinstance(rows, list):
                log.warning("manual/%s.json: formato não reconhecido", nome)
                return None
            log.info("manual/%s.json: %d registro(s) — a etapa não vai à rede",
                     nome, len(rows))
            return rows
        if alvo_csv.exists():
            d = pd.read_csv(alvo_csv, dtype=str, low_memory=False)
            log.info("manual/%s.csv: %d registro(s) — a etapa não vai à rede",
                     nome, len(d))
            return d.to_dict("records")
    except Exception as e:
        # Arquivo manual ilegível não pode derrubar a coleta: ela segue pela
        # rede, que é o comportamento de antes.
        log.warning("manual/%s ilegível (%s: %s) — seguindo pela rede",
                    nome, type(e).__name__, e)
    return None


def _marcar_manual(nome: str, n: int) -> None:
    """Anota no diagnóstico que este dataset veio à mão, não pela coleta."""
    _JANELAS[nome] = {**_JANELAS.get(nome, {}), "fonte": "manual",
                      "registros_manuais": int(n)}


def _anos_com_sondagem(fetch_ano, ate: int, desde: int | None, *, etapa: str):
    """Itera anos de `ate` para trás. Se desde=None, sonda até esvaziar.

    Cada exercício é uma janela com checkpoint próprio: o que já veio não é
    buscado de novo. Três estados, e a diferença entre eles é o que sustenta a
    sondagem retroativa:

    * checkpoint com linhas   -> usa o disco, não vai à rede;
    * checkpoint `.vazio`     -> o servidor JÁ respondeu que não há nada neste
                                 exercício; conta para encerrar a sondagem;
    * sem checkpoint          -> ainda não sabemos; vai à rede.

    Janela que falha (exceção, 4xx, payload não-lista) não vira checkpoint, não
    conta como exercício vazio e ainda ZERA o contador de vazios — porque com
    uma janela desconhecida no meio não dá para afirmar que dois exercícios
    vazios foram consecutivos. O custo de errar para este lado é sondar até
    `ANO_PISO` (no máximo ~17 requisições, quase todas cacheadas); o custo de
    errar para o outro é truncar a série histórica por causa de uma oscilação
    de rede e nunca mais tentar aqueles anos.
    """
    frames: list[dict] = []
    vazios = 0
    falhas: list[int] = []
    recusadas: list[int] = []
    do_disco = coletadas = 0
    ano = ate
    piso = desde or config.ANO_PISO
    while ano >= piso:
        rows = _checkpoint_ler(etapa, str(ano), ano)
        if rows is not None:
            do_disco += 1
        elif _recusa_fresca(etapa, str(ano), ano, _assinatura_etapa(etapa, ano)):
            # O portal já recusou esta janela há pouco. Insistir agora gasta o
            # ritmo — e é o ritmo que decide até onde a rodada chega antes do
            # 403. Recusa NÃO é exercício vazio: o contador de vazios zera,
            # senão a sondagem retroativa encerraria a série no ano errado.
            recusadas.append(ano)
            vazios = 0
            ano -= 1
            continue
        else:
            try:
                rows = fetch_ano(ano)
            except RateLimited:
                raise                     # bloqueio por ritmo: parar tudo
            except Exception as e:
                log.warning("%s/%d: janela falhou (%s: %s) — sem checkpoint, "
                            "seguindo para o exercício anterior",
                            etapa, ano, type(e).__name__, e)
                falhas.append(ano)
                vazios = 0
                ano -= 1
                continue
            if not isinstance(rows, list):
                # None = o host não devolveu dado utilizável (404, 4xx, payload
                # nulo). Isso NÃO é "o exercício está vazio" — tratar como
                # vazio congelaria um erro de parâmetro num `.vazio` e
                # encerraria a série histórica pelo motivo errado.
                log.warning("%s/%d: sem resposta utilizável (%s) — não conta "
                            "como exercício vazio", etapa, ano,
                            type(rows).__name__)
                _marcar_recusa(etapa, str(ano), _assinatura_etapa(etapa, ano))
                falhas.append(ano)
                recusadas.append(ano)
                vazios = 0
                ano -= 1
                continue
            _checkpoint_gravar(etapa, str(ano), rows)
            coletadas += 1
        if rows:
            vazios = 0
            for r in rows:
                r["_ano"] = ano
            frames.extend(rows)
        else:
            vazios += 1
            if desde is None and vazios >= config.SONDAGEM_VAZIOS:
                break
        ano -= 1

    _registrar_janelas(etapa, sorted(falhas, reverse=True), do_disco, coletadas,
                       recusadas=sorted(set(recusadas), reverse=True))
    return frames


# ---------------------------------------------------------------- datasets
def stage_empenhos(desde, ate):
    rows = _anos_com_sondagem(lambda a: clients.implanta_ano("Empenhos", a), ate,
                              desde, etapa="empenhos")
    df = pd.DataFrame(rows)
    if not df.empty:
        df["VALOR"] = coluna(df, "VALOR", 0).map(parse_valor)
        df["VALOR_PAGO"] = coluna(df, "VALOR_PAGO", 0).map(parse_valor)
        df["VALOR_ANULADO"] = coluna(df, "VALOR_ANULADO", 0).map(parse_valor)
        # A data do EMPENHO é a que importa: é ela que situa o ato no
        # exercício e sustenta os testes de dezembro, anulação e restos a
        # pagar. A data da nota fiscal é do fornecedor e pode ser de outro
        # ano — usá-la como data do empenho deslocava a série inteira.
        df["DATA"] = parse_data(coluna(df, "DATA_EMPENHO", None))
        df["_origem_data"] = "DATA_EMPENHO"
        falta = df["DATA"].isna()
        for alternativa in ("DATA", "DATA_LIQUIDACAO", "DATA_NOTA_FISCAL"):
            if not falta.any() or alternativa not in df.columns:
                continue
            df.loc[falta, "DATA"] = parse_data(df.loc[falta, alternativa])
            df.loc[falta & df["DATA"].notna(), "_origem_data"] = alternativa
            falta = df["DATA"].isna()
    return _salvar("empenhos", df)


def stage_pagamentos(desde, ate):
    rows = _anos_com_sondagem(lambda a: clients.implanta_ano("Pagamentos", a), ate,
                              desde, etapa="pagamentos")
    df = pd.DataFrame(rows)
    if not df.empty:
        df["VALOR"] = coluna(df, "VALOR", 0).map(parse_valor)
        df["VALOR_LIQUIDO"] = coluna(df, "VALOR_LIQUIDO", 0).map(parse_valor)
        df["DATA"] = parse_data(df["DATA_PAGAMENTO"]) if "DATA_PAGAMENTO" in df else pd.NaT
        doc = df["CPF_CNPJ"] if "CPF_CNPJ" in df else pd.Series("", index=df.index)
        nome = df["NOME_RAZAO_SOCIAL"] if "NOME_RAZAO_SOCIAL" in df else pd.Series("", index=df.index)
        df["TIPO_PESSOA"] = [classificar_pessoa(d, n) for d, n in zip(doc, nome)]
        df["PF"] = df["TIPO_PESSOA"] == "PF"
        df["PJ"] = df["TIPO_PESSOA"] == "PJ"
        df["FAVORECIDO_NORM"] = nome.map(norm_nome)
        df["FAVORECIDO_CNPJ"] = doc.map(so_digitos)
    return _salvar("pagamentos", df)


def stage_contratos(desde, ate):
    """Contratos vigentes. Um contrato plurianual aparece em vários exercícios;
    mantemos a ocorrência do PRIMEIRO ano de vigência, que traz o valor
    original — manter a mais recente fazia o percentual de aditivo explodir."""
    rows = _anos_com_sondagem(
        lambda a: clients.implanta_ano("Contratos", a, formato="vigencia"),
        ate, desde, etapa="contratos")
    df = pd.DataFrame(rows)
    if not df.empty:
        df["VALOR"] = df["VALOR"].map(parse_valor) if "VALOR" in df else 0.0
        df["INI"] = parse_data(df["DATA_INICIO_VIGENCIA"]) if "DATA_INICIO_VIGENCIA" in df else pd.NaT
        df["FIM"] = parse_data(df["DATA_FINAL_VIGENCIA"]) if "DATA_FINAL_VIGENCIA" in df else pd.NaT
        contratada = df["CONTRATADA"].fillna("").astype(str) if "CONTRATADA" in df else pd.Series("", index=df.index)
        # o portal grava "12.345.678/0001-90 - RAZÃO SOCIAL": separar os dois
        df["CONTRATADA_CNPJ"] = contratada.str.extract(
            RX_CNPJ_FORMATADO, expand=False).map(so_digitos)
        df["CONTRATADA_NOME"] = contratada.str.replace(
            r"^\s*[\d./-]{14,20}\s*-\s*", "", regex=True)
        df["CONTRATADA_NORM"] = df["CONTRATADA_NOME"].map(norm_nome)
        if "NUMERO_CONTRATO" in df:
            df = (df.sort_values("_ano", ascending=True)
                    .drop_duplicates(subset=["NUMERO_CONTRATO"], keep="first"))
    return _salvar("contratos", df)


def stage_aditivos(desde, ate):
    rows = _anos_com_sondagem(
        lambda a: clients.implanta_ano("ContratosAditivos", a, formato="vigencia"),
        ate, desde, etapa="aditivos")
    df = pd.DataFrame(rows)
    if not df.empty:
        df["VALOR"] = coluna(df, "VALOR", 0).map(parse_valor)
        df = df.drop_duplicates(subset=[c for c in ("NUMERO_CONTRATO", "VALOR", "DATA_PUBLICACAO") if c in df])
    return _salvar("aditivos", df)


def stage_licitacoes(desde, ate):
    manual = manual_rows("licitacoes")
    if manual is not None:
        _marcar_manual("licitacoes", len(manual))
        return _salvar("licitacoes", pd.DataFrame(manual))
    # Antes: `implanta_ano`, que tenta SÓ referenciaInicio/Termino — e o
    # endpoint devolveu 400 para esse formato, o que a sondagem não sabia
    # distinguir de "não há licitação neste ano". `implanta_variantes` percorre
    # os formatos conhecidos e aprende o que funciona (ver clients.py).
    rows = _anos_com_sondagem(
        lambda a: clients.implanta_variantes("Licitacoes", a, sem_filtro=False)[0], ate,
        desde, etapa="licitacoes")
    return _salvar("licitacoes", pd.DataFrame(rows))


def stage_convenios(desde, ate):
    rows = _anos_com_sondagem(lambda a: clients.implanta_ano("Convenios", a), ate,
                              desde, etapa="convenios")
    df = pd.DataFrame(rows)
    if not df.empty:
        df["VALOR_TOTAL"] = coluna(df, "VALOR_TOTAL", 0).map(parse_valor)
    return _salvar("convenios", df)


def stage_comparativo_despesa(desde, ate):
    rows = _anos_com_sondagem(
        lambda a: clients.implanta_ano("ComparativoDespesa", a), ate, desde,
        etapa="comparativo_despesa")
    df = pd.DataFrame(rows)
    if not df.empty:
        for c in ("VALOR_ORCADO", "REALIZADO_EXERCICIO", "TOTAL_EXERCICIO"):
            if c in df:
                df[c] = df[c].map(parse_valor)
    return _salvar("comparativo_despesa", df)


def stage_despesas_centro_custo(desde, ate):
    """Despesa por CENTRO DE CUSTO — base para separar despesa-meio de
    despesa-fim (fiscalização, câmaras, atendimento vs administração)."""
    rows = _anos_com_sondagem(
        lambda a: clients.implanta_ano("DespesasCentroCusto", a), ate, desde,
        etapa="despesas_centro_custo")
    df = pd.DataFrame(rows)
    if not df.empty:
        meses = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
                 "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
        for m in meses:
            c = f"VALOR_{m}"
            if c in df:
                df[c] = df[c].map(parse_valor)
        for c in ("VALOR_TOTAL", "ORCAMENTO", "SALDO"):
            if c in df:
                df[c] = df[c].map(parse_valor)
    return _salvar("despesas_centro_custo", df)


def _fetch_demonstrativo(ano: int):
    """Um exercício do demonstrativo, já achatado.

    O achatamento acontece ANTES do checkpoint de propósito: o payload original
    é uma árvore (campo DETALHES) e árvore não cabe em CSV. Devolve None — e
    não lista vazia — quando o host não respondeu, para que a janela conte como
    falha e não como exercício sem despesa.
    """
    rows = clients.implanta_ano("DemonstrativoEmpenhosPagamentos", ano)
    if not isinstance(rows, list):
        return None
    return clients.achatar_demonstrativo(rows)


def stage_demonstrativo(desde, ate):
    """Orçado × empenhado × liquidado × pago por conta/centro de custo."""
    frames = _anos_com_sondagem(_fetch_demonstrativo, ate, desde,
                                etapa="demonstrativo")
    df = pd.DataFrame(frames)
    if not df.empty:
        for c in ("ORCADO", "EMPENHADOEXERCICIO", "LIQUIDADO_EXERCICIO",
                  "PAGO_EXERCICIO", "SALDO_ORCAMENTARIO", "EMPENHA_PERIODO"):
            if c in df:
                df[c] = df[c].map(parse_valor)
        # Marcas da árvore, restauradas explicitamente porque é por elas que o
        # check de orçamento soma SÓ as folhas. Vindas de um checkpoint em CSV,
        # `_nivel` volta como texto ("10" < "9" na comparação de string) e a
        # soma da execução passaria a incluir a sintética junto com as filhas —
        # o mesmo real contado duas vezes.
        if "_folha" in df:
            df["_folha"] = df["_folha"].map(
                lambda v: v if isinstance(v, bool) else str(v).strip().lower()
                in ("true", "1", "sim"))
        if "_nivel" in df:
            df["_nivel"] = (pd.to_numeric(df["_nivel"], errors="coerce")
                            .fillna(0).astype(int))
    return _salvar("demonstrativo", df)


def stage_plano_contas(ate):
    """Hierarquia oficial do plano de contas (para taxonomia correta)."""
    frames = []
    for ano in (ate, ate - 1, ate - 2):
        rows = clients.implanta_exercicio("PlanoDeContas", ano)
        achatado = clients.achatar_plano_contas(rows)
        if achatado:
            for r in achatado:
                r["_ano"] = ano
            frames.extend(achatado)
            break
    return _salvar("plano_contas", pd.DataFrame(frames))


def stage_processos_compras(desde, ate):
    """Processos de compra com ITENS (quantidade, valor unitário, melhor cotação)
    — permite detectar sobrepreço e manipulação de cotação."""
    # Sem checkpoint por janela, e de propósito: cada processo carrega a lista
    # ITENS aninhada dentro de si, e o que um CSV faz com uma lista aninhada é
    # transformá-la em texto irreversível. Aqui a retomada continua sendo a do
    # cache HTTP (uma requisição por exercício, já gravada em data/raw).
    manual = manual_rows("processos_itens")
    if manual is not None:
        _marcar_manual("processos_itens", len(manual))
        return _salvar("processos_itens", pd.DataFrame(manual))
    processos, itens = [], []
    piso = desde or config.ANO_PISO
    vazios = 0
    for ano in range(ate, piso - 1, -1):
        rows = clients.implanta_ano("ProcessosComprasServicos", ano)
        if rows is None:
            # sem resposta ≠ exercício vazio: não encerra a sondagem
            log.warning("processos_compras/%d: sem resposta utilizável", ano)
            continue
        if not rows:
            vazios += 1
            if desde is None and vazios >= config.SONDAGEM_VAZIOS:
                break
            continue
        vazios = 0
        for p in rows:
            num = p.get("NUMERO_PROCESSO")
            processos.append({
                "_ano": ano, "NUMERO_PROCESSO": num,
                "SITUACAO_ATUAL": p.get("SITUACAO_ATUAL"),
                "DATA_CADASTRO": p.get("DATA_CADASTRO"),
                "UNIDADE_REQUISITANTE": p.get("UNIDADE_REQUISITANTE"),
                "VALOR_TOTAL_PROCESSO": parse_valor(p.get("VALOR_TOTAL_PROCESSO")),
                "OBJETO": p.get("OBJETO"), "JUSTIFICATIVA": p.get("JUSTIFICATIVA"),
                "N_ITENS": len(p.get("ITENS") or []),
            })
            for it in p.get("ITENS") or []:
                itens.append({
                    "_ano": ano, "NUMERO_PROCESSO": num,
                    "NUMERO_LOTE": it.get("NUMERO_LOTE"),
                    "NOME_ITEM": it.get("NOME_ITEM"),
                    "QUANTIDADE": parse_valor(it.get("QUANTIDADE")),
                    "VALOR_UNITARIO": parse_valor(it.get("VALOR_UNITARIO")),
                    "EMPRESA_MELHOR_COTACAO": it.get("EMPRESA_MELHOR_COTACAO"),
                    "VALOR_MELHOR_COTACAO": parse_valor(it.get("VALOR_MELHOR_COTACAO")),
                })
    _salvar("processos_compras", pd.DataFrame(processos))
    return _salvar("processos_itens", pd.DataFrame(itens))


def stage_atas(desde, ate):
    """Atas de colegiado: deliberações (texto) + participantes."""
    manual = manual_rows("atas")
    if manual is not None:
        _marcar_manual("atas", len(manual))
        return _salvar("atas", pd.DataFrame(manual))
    # Mesmo motivo de `licitacoes`: `implanta_datas` fixa dataInicio/Termino.
    rows = _anos_com_sondagem(
        lambda a: clients.implanta_variantes("AtasColegiados", a, sem_filtro=False)[0], ate, desde,
        etapa="atas")
    return _salvar("atas", pd.DataFrame(rows))


def stage_orcamento(desde, ate):
    """Balanço orçamentário e comparativo de receita (denominadores)."""
    orc = _anos_com_sondagem(
        lambda a: clients.implanta_ano("BalancoOrcamentario", a), ate, desde,
        etapa="balanco_orcamentario")
    dfo = pd.DataFrame(orc)
    if not dfo.empty:
        for c in ("DOTACOES", "DOTACOES_ATUALIZADAS", "RECEITAS_REALIZADAS",
                  "DESPESAS_EMPENHADAS", "DESPESAS_LIQUIDADAS", "DESPESAS_PAGAS"):
            if c in dfo:
                dfo[c] = dfo[c].map(parse_valor)
    _salvar("balanco_orcamentario", dfo)

    rec = _anos_com_sondagem(
        lambda a: clients.implanta_ano("ComparativoReceita", a), ate, desde,
        etapa="comparativo_receita")
    dfr = pd.DataFrame(rec)
    if not dfr.empty:
        for c in ("VALOR_ORCADO", "REALIZADO_EXERCICIO", "TOTAL_EXERCICIO"):
            if c in dfr:
                dfr[c] = dfr[c].map(parse_valor)
    return _salvar("comparativo_receita", dfr)


def _primeira_coluna(df: pd.DataFrame, *nomes: str) -> pd.Series:
    """Primeira coluna existente entre as candidatas — os nomes de campo mudam
    entre Dados Abertos e Implanta, e entre versões do portal."""
    for n in nomes:
        if n in df.columns:
            return df[n].fillna("")
    return pd.Series([""] * len(df), index=df.index)


def stage_quadro_pessoal(ate=None):
    """Quadro de pessoal: Dados Abertos e, se falhar, Implanta QuadrosPessoas.

    O Implanta responde mesmo quando o host de Dados Abertos está barrado — é
    o caminho que mantém viva a frente de pessoal.
    """
    manual = manual_rows("quadro_pessoal")
    if manual is not None:
        df = pd.DataFrame(manual)
        _marcar_manual("quadro_pessoal", len(df))
    else:
        df = pd.DataFrame(clients.dados_abertos("quadro-colaboradores"))
        if df.empty:
            # A documentação de "Quadros de Pessoas" não declara parâmetro de
            # consulta, e o endpoint pode morar sob outro prefixo — as duas
            # coisas ficam com `implanta_primeiro_nome`/`implanta`.
            linhas, nome = clients.implanta_primeiro_nome(
                config.ENDPOINT_CANDIDATOS["quadro_pessoal"], ate)
            if nome:
                log.info("quadro_pessoal: veio de %s", nome)
            df = pd.DataFrame(linhas or [])
    if not df.empty:
        df["nome"] = _primeira_coluna(df, "nome", "NOME", "NOME_PESSOA")
        df["vinculo"] = _primeira_coluna(df, "vinculo", "VINCULO", "TIPO_VINCULO",
                                         "_grupo_TITULO")
        df["cargo"] = _primeira_coluna(df, "nomeCargo", "NOME_CARGO", "CARGO")
        df["lotacao"] = _primeira_coluna(df, "nomeLotacao", "NOME_LOTACAO",
                                         "LOTACAO", "UNIDADE")
        df["remuneracao"] = _primeira_coluna(
            df, "renumeracaoMensal", "remuneracaoMensal", "REMUNERACAO_MENSAL",
            "REMUNERACAO").map(parse_valor)
        df["NOME_NORM"] = df["nome"].map(norm_nome)
        df["VINCULO_NORM"] = df["vinculo"].map(norm_nome)
        df["LOTACAO_NORM"] = df["lotacao"].map(norm_nome)
    return _salvar("quadro_pessoal", df)


def stage_mandatarios(ate=None):
    """Conselheiros/mandatários — quem decide e por qual entidade tem assento.

    A ENTIDADE que indicou o conselheiro é o elo entre o assento e o interesse
    representado; sem ela não há como ligar composição do plenário a
    beneficiário de repasse.
    """
    manual = manual_rows("mandatarios")
    if manual is not None:
        df = pd.DataFrame(manual)
        _marcar_manual("mandatarios", len(df))
    else:
        df = pd.DataFrame(clients.dados_abertos("mandatarios"))
        if df.empty:
            # "Rol de Mandatários" é endpoint próprio na documentação;
            # `Conselheiros` (o que chamávamos) existe e responde vazio.
            linhas, nome = clients.implanta_primeiro_nome(
                config.ENDPOINT_CANDIDATOS["mandatarios"], ate)
            if nome:
                log.info("mandatarios: veio de %s", nome)
            df = pd.DataFrame(linhas or [])
    if not df.empty:
        df["nome"] = _primeira_coluna(df, "nomeMandatario", "NOME_MANDATARIO",
                                      "NOME_CONSELHEIRO", "NOME")
        df["cargo"] = _primeira_coluna(df, "cargo", "CARGO", "_grupo_TITULO")
        df["entidade"] = _primeira_coluna(
            df, "entidade", "ENTIDADE", "NOME_ENTIDADE", "ENTIDADE_CLASSE",
            "INSTITUICAO", "NOME_INSTITUICAO", "REPRESENTACAO", "_grupo_TITULO")
        df["NOME_NORM"] = df["nome"].map(norm_nome)
        df["ENTIDADE_NORM"] = df["entidade"].map(norm_nome)
        # Datas do mandato: são elas que dizem se o rol é uma FOTO do momento
        # ou uma série histórica — e o portal devolve a foto. Sem isso, cruzar
        # o mandato de hoje com uma diária de 2015 seria anacronismo.
        for col, orig in (("POSSE", ("dataPosse", "DATA_POSSE")),
                          ("FIM_PREVISTO", ("dataPrevistaFim", "DATA_FIM")),
                          ("EXONERACAO", ("dataExoneracao", "DATA_EXONERACAO"))):
            df[col] = pd.to_datetime(_primeira_coluna(df, *orig),
                                     dayfirst=True, errors="coerce")
        # O DOMÍNIO do e-mail, nunca o e-mail. O endereço é dado pessoal e não
        # entra em relatório nenhum; o domínio é o que carrega informação
        # pública — `unesp.br` diz representante de instituição de ensino,
        # `construtora*.com.br` diz vínculo com empresa do setor fiscalizado.
        # Este é o único proxy da entidade representada quando o portal não
        # publica o campo de entidade, que é exatamente o caso.
        email = _primeira_coluna(df, "enderecoEletronico", "EMAIL", "E_MAIL")
        df["EMAIL_DOMINIO"] = (email.astype(str).str.strip().str.lower()
                               .str.rsplit("@", n=1).str[-1]
                               .where(email.astype(str).str.contains("@"), ""))
        # O endereço completo é descartado aqui, depois de extraído o domínio.
        # O relatório e os CSVs de evidência são feitos para serem
        # compartilhados; e-mail pessoal de 2.455 pessoas não tem por que
        # viajar junto. O arquivo original continua em `data/manual/` para
        # quem precisar conferir.
        df = df.drop(columns=[c for c in ("enderecoEletronico", "EMAIL",
                                          "E_MAIL") if c in df.columns])
        # O CPF vem MASCARADO pela fonte (431.xxx.xxx-00): seis dígitos
        # visíveis. Não serve de chave — no rol real há mais nomes distintos
        # que CPFs mascarados distintos, ou seja, máscaras diferentes colidem.
        # Fica só como coluna de conferência manual.
        df["CPF_MASCARA"] = _primeira_coluna(df, "cpfMandatario", "CPF")
    return _salvar("mandatarios", df)


def stage_indenizacoes(desde, ate):
    """Endpoints dedicados de indenizações/diárias (podem estar vazios —
    o próprio vazio vira achado de transparência no check)."""
    manual = manual_rows("indenizacoes_endpoint")
    if manual is not None:
        df = pd.DataFrame(manual)
        _marcar_manual("indenizacoes_endpoint", len(df))
        if not df.empty:
            df["valor"] = coluna(df, "valorBrutoPagamento", 0).map(parse_valor)
            df["FAVORECIDO_NORM"] = coluna(df,
                                           "nomeFavorecidoPagamento").map(norm_nome)
        return _salvar("indenizacoes_endpoint", df)
    frames = []
    for ano in range((desde or 2015), ate + 1):
        for r in clients.dados_abertos("indenizacoes", {"exercicio": str(ano)}):
            r["_ano"] = ano
            frames.append(r)
    probe = {}
    for ano in range((desde or 2015), ate + 1):
        for ep, rows in clients.implanta_probe_diarias(ano).items():
            probe.setdefault(ep, []).extend({**r, "_ano": ano} for r in rows)
    df = pd.DataFrame(frames)
    if not df.empty:
        df["valor"] = coluna(df, "valorBrutoPagamento", 0).map(parse_valor)
        df["FAVORECIDO_NORM"] = coluna(df, "nomeFavorecidoPagamento").map(norm_nome)
    _salvar("indenizacoes_endpoint", df)
    for ep, rows in probe.items():
        _salvar(f"implanta_probe_{ep.lower()}", pd.DataFrame(rows))
    return df


def _chave_ci(r: dict, nome: str, padrao=None):
    """`r[nome]` ignorando a caixa das letras.

    O DTO real de `/v1/contratos` grafa `numeroControlePncpCompra` (lido do
    cache bruto em 05/09/2026), e a projeção esperava `numeroControlePNCPCompra`:
    145 contratos ficaram seis rodadas com a compra de origem vazia, e os dois
    achados que dependem dela nunca dispararam. Tolerar a caixa custa nada e
    fecha essa classe de erro para qualquer outra chave.
    """
    if not isinstance(r, dict):
        return padrao
    if nome in r:
        return r[nome]
    alvo = nome.lower()
    for k, v in r.items():
        if str(k).lower() == alvo:
            return v
    return padrao


def _pncp_contrato_linha(o: dict, r: dict) -> dict:
    """Projeta um contrato do PNCP nas colunas que a análise usa.

    Nomes das colunas staged são estáveis (os checks os usam); a leitura do
    DTO é tolerante à caixa (`_chave_ci`)."""
    tipo = _chave_ci(r, "tipoContrato") or {}
    unidade = _chave_ci(r, "unidadeOrgao") or {}
    return {
        "orgao_cnpj": o["cnpj"], "orgao_papel": o["papel"],
        "orgao_nome": o.get("razaoSocial"),
        "numeroControlePNCP": _chave_ci(r, "numeroControlePNCP"),
        "fornecedor_ni": so_digitos(_chave_ci(r, "niFornecedor")),
        "fornecedor_nome": _chave_ci(r, "nomeRazaoSocialFornecedor"),
        "tipoPessoa": _chave_ci(r, "tipoPessoa"),
        "objeto": _chave_ci(r, "objetoContrato"),
        "valorGlobal": parse_valor(_chave_ci(r, "valorGlobal")),
        "valorAcumulado": parse_valor(_chave_ci(r, "valorAcumulado")),
        "valorInicial": parse_valor(_chave_ci(r, "valorInicial")),
        "dataAssinatura": _chave_ci(r, "dataAssinatura"),
        "vigenciaInicio": _chave_ci(r, "dataVigenciaInicio"),
        "vigenciaFim": _chave_ci(r, "dataVigenciaFim"),
        "dataPublicacaoPncp": _chave_ci(r, "dataPublicacaoPncp"),
        "anoContrato": _chave_ci(r, "anoContrato"),
        "numeroContrato": _chave_ci(r, "numeroContratoEmpenho"),
        # "Empenho" ou "Contrato (termo inicial)": o PNCP publica também o
        # que o portal próprio publica como nota de empenho, e sem esta coluna
        # o cruzamento acusava o portal de omitir o que ele publica em outro
        # dataset.
        "tipoContrato_nome": tipo.get("nome") if isinstance(tipo, dict) else tipo,
        "unidade_codigo": (unidade.get("codigoUnidade")
                           if isinstance(unidade, dict) else None),
        # O contrato não expõe edital nem termo de referência; a COMPRA de
        # origem sim. É por este número que se chega aos documentos do certame
        # — e o portal do próprio Crea não publica nenhum número de licitação
        # nos seus contratos.
        "numeroControlePNCPCompra": _chave_ci(r, "numeroControlePncpCompra"),
        "numeroControlePncpAta": _chave_ci(r, "numeroControlePncpAta"),
        "processo": _chave_ci(r, "processo"),
    }


def _pncp_ata_linha(o: dict, r: dict) -> dict:
    """Projeta uma ata de registro de preços do PNCP (`/v1/atas`).

    As chaves do DTO NÃO foram verificadas contra a rede (bloqueada na sessão
    em que isto foi escrito); por isso cada uma é lida com `_chave_ci` e com
    sinônimos prováveis. Coluna vazia é sinal para conferir o DTO no cache
    bruto (`data/raw/pncp.gov.br/`), não para inventar valor.
    """
    def ci(*nomes):
        for n in nomes:
            v = _chave_ci(r, n)
            if v not in (None, ""):
                return v
        return None
    return {
        "orgao_cnpj": o["cnpj"], "orgao_papel": o["papel"],
        "numeroControlePNCPAta": ci("numeroControlePNCPAta", "numeroControlePncpAta"),
        "numeroAtaRegistroPreco": ci("numeroAtaRegistroPreco", "numeroAta"),
        "anoAta": ci("anoAta"),
        "numeroControlePNCPCompra": ci("numeroControlePNCPCompra", "numeroControlePncpCompra"),
        "dataAssinatura": ci("dataAssinatura"),
        "vigenciaInicio": ci("vigenciaInicio", "dataVigenciaInicio"),
        "vigenciaFim": ci("vigenciaFim", "dataVigenciaFim"),
        "dataPublicacaoPncp": ci("dataPublicacaoPncp"),
        "objetoContratacao": ci("objetoContratacao", "objeto"),
        "cancelado": ci("cancelado"),
    }


_PNCP_ORGAOS_FALHOS: list[str] = []


def _resolver_orgao_tolerante(razao: str, **kw) -> dict | None:
    """`clients.pncp_resolver_orgao` que não derruba a etapa quando o PNCP
    responde 5xx na consulta do órgão. Na rodada 11 quatro 503 em
    `/v1/orgaos/<cnpj do Confea>` abortaram `stage_pncp` inteira — os 145
    contratos do alvo só sobreviveram porque `build_staged` preserva o CSV
    anterior. Falha de um órgão RELACIONADO vira aviso no diagnóstico."""
    try:
        return clients.pncp_resolver_orgao(razao, **kw)
    except RateLimited:
        # `RateLimited` herda de RuntimeError: sem esta cláusula, o bloqueio
        # por ritmo virava "órgão indisponível" e a rodada seguia batendo nos
        # outros quatro órgãos (~11 min de backoff cada) contra um host que
        # já mandou parar. Bloqueio para tudo, como em toda a pipeline.
        raise
    except RuntimeError as e:
        log.warning("PNCP: órgão %r não pôde ser consultado (%s) — a rodada "
                    "segue sem ele", razao, e)
        _PNCP_ORGAOS_FALHOS.append(f"{razao}: {e}")
        return None


def stage_pncp(desde, ate):
    """Contratos PNCP do Crea-SP e dos órgãos relacionados (rede de influência)."""
    ini = max(desde or 2021, 2021)   # PNCP só existe desde ago/2021
    orgaos = []
    _PNCP_ORGAOS_FALHOS.clear()
    alvo = clients.pncp_resolver_orgao(config.ORGAO_ALVO_RAZAO,
                                       cnpj_fixo=config.ORGAO_ALVO_CNPJ_CANDIDATOS,
                                       uf=config.ORGAO_ALVO_UF)
    if alvo and alvo.get("cnpj"):
        log.info("PNCP: órgão-alvo resolvido em %s (%s)%s", alvo["cnpj"],
                 alvo.get("razaoSocial"),
                 "" if alvo.get("_verificado") else " — NÃO verificado")
        orgaos.append({**alvo, "papel": "alvo"})
    else:
        log.warning("PNCP: órgão-alvo não resolvido — o bloco PNCP virá vazio "
                    "em vez de trazer contratos de outro conselho")
    for rel in config.ORGAOS_RELACIONADOS:
        o = _resolver_orgao_tolerante(
            rel["razao"], cnpj_fixo=rel.get("cnpj_candidatos"),
            termo_busca=rel.get("termo_busca"))
        if o and o.get("cnpj") and o["cnpj"] != (alvo or {}).get("cnpj"):
            orgaos.append({**o, "papel": "relacionado"})
        else:
            log.warning("PNCP: órgão relacionado %r não resolvido — a rede "
                        "entre órgãos do sistema fica sem ele", rel["razao"])
    _salvar("pncp_orgaos", pd.DataFrame(orgaos))

    contratos, cobrancas = [], []
    for o in orgaos:
        # Mês a mês com checkpoint: o PNCP devolve 503 em janela anual, então
        # são ~66 janelas paginadas por órgão entre 2021 e hoje. Sem
        # checkpoint, uma queda no último órgão obrigava a refazer as janelas
        # de todos eles.
        def buscar_mes(jini, jfim, orgao=o):
            linhas = clients.pncp_contratos_mes(orgao["cnpj"], jini, jfim)
            if not isinstance(linhas, list):
                return None
            # A projeção vem ANTES do checkpoint porque o DTO do PNCP traz
            # objetos aninhados (órgão, unidade, tipo de contrato) e aninhado
            # não sobrevive a um CSV. Projetado, o registro é plano.
            proj = [_pncp_contrato_linha(orgao, r) for r in linhas]
            if proj and not any(l.get("numeroControlePNCPCompra") for l in proj):
                # Foi exatamente este silêncio que escondeu a grafia errada da
                # chave por seis rodadas.
                log.warning("pncp_contratos/%s: %d contrato(s) sem compra de "
                            "origem no DTO — conferir a grafia da chave no cache "
                            "bruto", jini[:6], len(proj))
            return proj

        contratos.extend(_janelas_nomeadas(
            f"pncp_contratos/{o['cnpj']}",
            ((jini[:6], int(jini[:4]), jini, jfim)
             for jini, jfim in clients.pncp_meses(ini, ate)),
            buscar_mes, dataset="pncp_contratos"))
        if o["papel"] == "alvo":
            try:
                instrumentos = clients.pncp_instrumentos_cobranca(o["cnpj"], ini, ate)
            except RateLimited:
                raise                     # bloqueio por ritmo: parar tudo
            except RuntimeError as e:
                log.warning("PNCP: instrumentos de cobrança indisponíveis (%s) — "
                            "os contratos já coletados ficam", e)
                _PNCP_ORGAOS_FALHOS.append(f"instrumentos de cobrança: {e}")
                instrumentos = []
            for r in instrumentos:
                nfe = r.get("notaFiscalEletronica") or {}
                contrato = r.get("recuperarContratoDTO") or {}
                cobrancas.append({
                    "sequencialContrato": r.get("sequencialContrato"),
                    "numeroNota": nfe.get("numero"),
                    "emitente_ni": so_digitos(nfe.get("niEmitente")),
                    "emitente_nome": nfe.get("nomeEmitente"),
                    "valorNota": parse_valor(nfe.get("valorNotaFiscal")),
                    "dataEmissao": nfe.get("dataEmissao"),
                    "objetoContrato": contrato.get("objetoContrato"),
                })
    df_c = pd.DataFrame(contratos)
    if not df_c.empty:
        df_c["fornecedor_norm"] = coluna(df_c, "fornecedor_nome").map(norm_nome)
        df_c["_ano"] = pd.to_numeric(coluna(df_c, "anoContrato"), errors="coerce")
        # Janela vinda do checkpoint devolve texto. Sem reconverter, `sum()`
        # sobre valorGlobal CONCATENARIA em vez de somar, e o ranking de
        # fornecedores do PNCP sairia sem sentido. `parse_valor` aceita as duas
        # formas, então a coerção é idempotente para a janela vinda da rede.
        for c in ("valorGlobal", "valorAcumulado"):
            df_c[c] = coluna(df_c, c, 0).map(parse_valor)
    _salvar("pncp_contratos", df_c)
    if _PNCP_ORGAOS_FALHOS:
        _DIAGNOSTICO[-1]["indisponivel"] = ("PNCP não respondeu à consulta de: "
                                            + "; ".join(_PNCP_ORGAOS_FALHOS))
    _salvar("pncp_cobrancas", pd.DataFrame(cobrancas))
    return df_c


# ---------------------------------------------------------------- entrada
# Colunas que um staged precisa ter para ser reaproveitado pelo `--retomar`.
# Um CSV com linhas mas sem elas é de uma versão anterior do código e vale
# como NÃO coletado — senão a correção nunca chega ao relatório: a rodada 10
# pulou benchmark_creas ("já em disco, 15.938 linhas") e o check quebrou no
# mesmo `_ano` que a coleta velha não gravava.
COLUNAS_OBRIGATORIAS: dict[str, tuple[str, ...]] = {
    "benchmark_creas": ("_ano",),
    "pncp_contratos": ("tipoContrato_nome",),
    "sancoes": ("alcance",),
}
# Etapas cujo checkpoint guarda a PROJEÇÃO (não a resposta bruta): esquema
# novo exige refazer a projeção, e refazer é barato porque o cache HTTP em
# data/raw/<host>/ serve as respostas sem rede.
_CHECKPOINT_E_PROJECAO = {"pncp_contratos"}


def _esquema_faltante(nome: str, df: pd.DataFrame) -> list[str]:
    return [c for c in COLUNAS_OBRIGATORIAS.get(nome, ()) if c not in df.columns]


def _invalidar_checkpoints(etapa: str) -> None:
    """Apaga os checkpoints de uma etapa cujo esquema mudou. Só para etapas
    em `_CHECKPOINT_E_PROJECAO`; nas demais o checkpoint continua válido e a
    coluna nova é reposta na leitura ou na etapa."""
    import shutil
    raiz = _checkpoint_dir(etapa)
    if raiz.is_dir():
        shutil.rmtree(raiz, ignore_errors=True)
        log.info("%s: checkpoints apagados — guardavam a projeção antiga; a "
                 "recoleta sai do cache HTTP em data/raw/", etapa)


def _ja_coletado(nome: str, exigir_esquema: bool = True) -> pd.DataFrame | None:
    """DataFrame já staged com linhas, ou None se falta, veio vazio ou é de um
    esquema anterior (ver `COLUNAS_OBRIGATORIAS`).

    Vazio conta como NÃO coletado de propósito: dataset zerado é quase sempre
    resultado de 403 por rajada, e 403 nunca é cacheado — repetir a etapa é
    exatamente o que se quer. `exigir_esquema=False` serve a quem só quer
    preservar o que já está em disco (o caminho de falha de `build_staged`).
    """
    caminho = config.STAGED_DIR / f"{nome}.csv"
    if not caminho.exists():
        return None
    try:
        df = _ler_csv_staged(caminho)
    except (pd.errors.EmptyDataError, ValueError, OSError):
        return None
    if df.empty:
        return None
    if exigir_esquema:
        faltam = _esquema_faltante(nome, df)
        if faltam:
            log.info("%s: staged sem a(s) coluna(s) %s — esquema anterior; a "
                     "etapa será refeita", nome, ", ".join(faltam))
            if nome in _CHECKPOINT_E_PROJECAO:
                _invalidar_checkpoints(nome)
            return None
    for col in ("DATA", "INI", "FIM"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


# Etapa -> chave(s) de checkpoint por janela ANUAL. A chave é o argumento
# `etapa=` que cada stage_* passa a `_anos_com_sondagem`, e ela NEM SEMPRE é o
# nome da etapa: `comparativo_receita` grava duas janelas (a sua e a do
# balanço orçamentário), e é por isso que este mapa é explícito em vez de
# derivado do nome. Etapa fora do mapa não tem retomada fina por exercício:
#
#   processos_itens        cada processo carrega os ITENS aninhados, e uma
#                          lista dentro de um CSV vira texto irreversível — a
#                          retomada dela é o cache HTTP;
#   indenizacoes_endpoint  varre exercícios em endpoints de sondagem, sem
#                          janela própria;
#   plano_contas,          consultas de tiro único (foto do momento), sem
#   quadro_pessoal,        exercício a percorrer;
#   mandatarios
#   pncp_contratos         janela MENSAL e uma subpasta por CNPJ
#                          (`pncp_contratos/<cnpj>/AAAAMM`), coberta pelo mapa
#                          abaixo.
ETAPAS_JANELA_ANUAL: dict[str, tuple[str, ...]] = {
    "empenhos": ("empenhos",),
    "pagamentos": ("pagamentos",),
    "contratos": ("contratos",),
    "aditivos": ("aditivos",),
    "licitacoes": ("licitacoes",),
    "convenios": ("convenios",),
    "comparativo_despesa": ("comparativo_despesa",),
    "despesas_centro_custo": ("despesas_centro_custo",),
    "demonstrativo": ("demonstrativo",),
    "atas": ("atas",),
    "comparativo_receita": ("comparativo_receita", "balanco_orcamentario"),
}


def stage_licitacoes_pncp(desde, ate):
    """Contratações (editais) do órgão-alvo no PNCP, por modalidade × mês.

    É a fonte alternativa para `licitacoes`, que o portal próprio não publica:
    sob a Lei 14.133 (obrigatória desde 30/12/2023) toda contratação tem de
    estar no PNCP. E é o que permite ligar contrato → compra de origem
    (`numeroControlePNCPCompra`) → modalidade: o portal não diz se um contrato
    veio de pregão ou de dispensa; o PNCP diz.
    """
    manual = manual_rows("licitacoes_pncp")
    if manual is not None:
        _marcar_manual("licitacoes_pncp", len(manual))
        return _salvar("licitacoes_pncp", pd.DataFrame(manual))
    alvo = clients.pncp_resolver_orgao(config.ORGAO_ALVO_RAZAO,
                                       cnpj_fixo=config.ORGAO_ALVO_CNPJ_CANDIDATOS,
                                       uf=config.ORGAO_ALVO_UF)
    if not alvo or not alvo.get("cnpj"):
        log.warning("licitacoes_pncp: órgão-alvo não resolvido — etapa vazia")
        return _salvar("licitacoes_pncp", pd.DataFrame())
    ini = max(desde or config.LICITACOES_PNCP_DESDE, config.LICITACOES_PNCP_DESDE)
    linhas: list[dict] = []
    for mod, nome_mod in config.PNCP_MODALIDADES_CONSELHO.items():
        def buscar(jini, jfim, mod=mod):
            rows = clients.pncp_contratacoes_mes(alvo["cnpj"], jini, jfim, mod)
            if rows is None:
                return None
            return [clients._projetar_contratacao(r) for r in rows]
        linhas.extend(_janelas_nomeadas(
            f"licitacoes_pncp/{mod}",
            ((jini[:6], int(jini[:4]), jini, jfim)
             for jini, jfim in clients.pncp_meses(ini, ate)),
            buscar, dataset="licitacoes_pncp"))
    df = pd.DataFrame(linhas)
    if not df.empty:
        df["_ano"] = pd.to_numeric(df.get("anoCompra"), errors="coerce")
        for c in ("valorTotalEstimado", "valorTotalHomologado"):
            if c in df:
                df[c] = df[c].map(parse_valor)
    return _salvar("licitacoes_pncp", df)


def stage_pncp_atas(desde, ate):
    """Atas de registro de preços do órgão-alvo no PNCP (`/v1/atas`), mês a mês.

    Por que existe: 6 dos 11 "contratos ausentes do PNCP" da rodada 10 eram
    ATAs do portal próprio (ATA 004/2024 etc.), e o cruzamento só consultava
    `/v1/contratos` — ata nunca ia casar, por construção. O parâmetro do órgão
    é `cnpj`, CONFIRMADO na rodada 11 (o ledger registra 200/204 com ele em
    todas as janelas); `clients.pncp_atas_mes` ainda tenta `cnpjOrgao` como
    alternativa, e uma janela sem resposta vira recusa (12 h), não "sem atas".
    Falha total grava vazio e anota `indisponivel` no diagnóstico, sem
    derrubar a coleta.
    """
    buscar_atas = getattr(clients, "pncp_atas_mes", None)
    alvo = clients.pncp_resolver_orgao(config.ORGAO_ALVO_RAZAO,
                                       cnpj_fixo=config.ORGAO_ALVO_CNPJ_CANDIDATOS,
                                       uf=config.ORGAO_ALVO_UF)
    if buscar_atas is None or not alvo or not alvo.get("cnpj"):
        out = _salvar("pncp_atas", pd.DataFrame())
        _DIAGNOSTICO[-1]["indisponivel"] = ("cliente de atas ausente" if buscar_atas is None
                                            else "órgão-alvo não resolvido")
        return out
    orgao = {**alvo, "papel": "alvo"}
    ini = max(desde or 2021, 2021)

    def buscar(jini, jfim):
        rows = buscar_atas(orgao["cnpj"], jini, jfim)
        if rows is None:
            return None
        return [_pncp_ata_linha(orgao, r) for r in rows]

    linhas = _janelas_nomeadas(
        f"pncp_atas/{orgao['cnpj']}",
        ((jini[:6], int(jini[:4]), jini, jfim)
         for jini, jfim in clients.pncp_meses(ini, ate)),
        buscar, dataset="pncp_atas")
    df = pd.DataFrame(linhas)
    if not df.empty:
        df["_ano"] = pd.to_numeric(coluna(df, "anoAta", None), errors="coerce")
        # O endpoint filtra por VIGÊNCIA: uma ata vigente por 24 meses volta em
        # 24 janelas mensais. Na rodada 11 foram 138 linhas para 8 atas.
        antes = len(df)
        chave = coluna(df, "numeroControlePNCPAta", None)
        if chave.notna().all() and chave.astype(str).str.strip().ne("").all():
            df = df.drop_duplicates(subset=["numeroControlePNCPAta"])
        else:
            df = df.drop_duplicates()
        if len(df) != antes:
            log.info("pncp_atas: %d linha(s) de janelas mensais → %d ata(s) distinta(s)",
                     antes, len(df))
    out = _salvar("pncp_atas", df)
    janelas = _DIAGNOSTICO[-1]
    if df.empty and janelas.get("janelas_recusadas") and not janelas.get("janelas_com_dado"):
        janelas["indisponivel"] = ("PNCP /v1/atas não respondeu a janela nenhuma — "
                                   "nome do parâmetro do órgão a confirmar")
    return out


def stage_sancoes(ate=None):
    """CEIS + CNEP (CGU), a foto de hoje. Tiro único; a retomada é o cache."""
    manual = manual_rows("sancoes")
    if manual is not None:
        _marcar_manual("sancoes", len(manual))
        return _salvar("sancoes", pd.DataFrame(manual))
    todas: list[dict] = []
    metas: list[dict] = []
    for cadastro in config.SANCOES_CADASTROS:
        linhas, meta = clients.baixar_sancoes(cadastro)
        metas.append(meta)
        if linhas is None:
            log.warning("sancoes: %s indisponível (nenhuma data respondeu)", cadastro)
            continue
        todas.extend(linhas)
    df = pd.DataFrame(todas)
    if not df.empty:
        df["inicio"] = parse_data(df["inicio"])
        df["fim"] = parse_data(df["fim"])
        df["efeito_nacional"] = [clients.efeito_nacional(c, f)
                                 for c, f in zip(df["categoria"], df["fundamento"])]
        # Alcance por fundamento legal e esfera (nacional / federal / ente /
        # indeterminado): é o que separa "vale contra o Crea-SP" de contexto.
        # A rodada 10 publicou como efeito nacional um impedimento ESTADUAL de
        # R$ 199 mil porque só olhava a palavra "proibição" na categoria.
        df["alcance"] = [clients.alcance_sancao(c, f, e) for c, f, e in zip(
            df["categoria"], coluna(df, "fundamento", None),
            coluna(df, "esfera", None))]
    out = _salvar("sancoes", df)
    _DIAGNOSTICO[-1]["fontes"] = "; ".join(
        f"{m['cadastro']}: {m.get('data') or 'sem resposta'}" for m in metas)
    return out


def stage_benchmark_creas(ate):
    """ComparativoDespesa dos outros conselhos, para comparar o alvo com os pares.

    OPT-IN (`QUEDA_COMPARAR_CREAS=1` / `--comparar-creas`): são até 28 hosts ×
    exercícios. Desligado, grava vazio e diz por quê — não conta como etapa
    não alcançada.
    """
    if os.environ.get(config.BENCHMARK_LIGADO_ENV, "").strip().lower() not in ("1", "true", "sim"):
        out = _salvar("benchmark_creas", pd.DataFrame())
        _DIAGNOSTICO[-1]["desligado"] = (f"opt-in: {config.BENCHMARK_LIGADO_ENV}=1 "
                                         "ou --comparar-creas")
        return out
    anos = list(range(ate - config.BENCHMARK_EXERCICIOS, ate))   # encerrados
    linhas: list[dict] = []
    for uf, host in config.CREAS_IMPLANTA.items():
        def buscar(ano, host=host, uf=uf):
            rows = clients.implanta_em(host, "ComparativoDespesa", {
                "referenciaInicio": f"01/{ano}", "referenciaTermino": f"12/{ano}"})
            if rows is None:
                return None
            return [{**r, "_uf": uf, "_host": host} for r in rows]
        linhas.extend(_janelas_nomeadas(
            f"benchmark/{host}", ((str(a), a, a) for a in anos), buscar,
            dataset="benchmark_creas"))
    df = pd.DataFrame(linhas)
    if not df.empty:
        for c in ("VALOR", "VALOR_EMPENHADO", "VALOR_PAGO", "VALOR_LIQUIDADO",
                  "VALOR_ORCADO", "REALIZADO_EXERCICIO", "TOTAL_EXERCICIO",
                  "REALIZADO_PERIODO", "TOTAL_PERIODO"):
            if c in df:
                df[c] = df[c].map(parse_valor)
    return _salvar("benchmark_creas", df)


def _etapas(desde: int | None, ate: int) -> dict:
    """Etapa -> como executá-la. ORDEM É A DA COLETA.

    Fonte única do universo esperado: `DATASETS_ESPERADOS` sai daqui, e o check
    de cobertura compara o que veio contra ela. Enquanto a lista morava dentro
    de `build_staged`, o diagnóstico só conseguia falar das etapas que a rodada
    chegou a executar — e uma rodada que abortou na terceira etapa publicava
    "Coleta completa: 3 de 3 datasets com dados", medido. Etapa nunca alcançada
    não é uma etapa vazia: é uma etapa invisível, e invisível se parece com
    regular.
    """
    return {
        "empenhos": lambda: stage_empenhos(desde, ate),
        "pagamentos": lambda: stage_pagamentos(desde, ate),
        "contratos": lambda: stage_contratos(desde, ate),
        "aditivos": lambda: stage_aditivos(desde, ate),
        "licitacoes": lambda: stage_licitacoes(desde, ate),
        "convenios": lambda: stage_convenios(desde, ate),
        "comparativo_despesa": lambda: stage_comparativo_despesa(desde, ate),
        "despesas_centro_custo": lambda: stage_despesas_centro_custo(desde, ate),
        "demonstrativo": lambda: stage_demonstrativo(desde, ate),
        "plano_contas": lambda: stage_plano_contas(ate),
        "processos_itens": lambda: stage_processos_compras(desde, ate),
        "atas": lambda: stage_atas(desde, ate),
        "comparativo_receita": lambda: stage_orcamento(desde, ate),
        "quadro_pessoal": lambda: stage_quadro_pessoal(ate),
        "mandatarios": lambda: stage_mandatarios(ate),
        "indenizacoes_endpoint": lambda: stage_indenizacoes(desde, ate),
        "pncp_contratos": lambda: stage_pncp(desde, ate),
        "licitacoes_pncp": lambda: stage_licitacoes_pncp(desde, ate),
        "pncp_atas": lambda: stage_pncp_atas(desde, ate),
        "sancoes": lambda: stage_sancoes(ate),
        "benchmark_creas": lambda: stage_benchmark_creas(ate),
    }


# As lambdas nunca são chamadas aqui: só as CHAVES interessam, e o dict do
# Python preserva a ordem de inserção, que é a ordem da coleta.
DATASETS_ESPERADOS: tuple[str, ...] = tuple(_etapas(None, 0))


# Etapas com checkpoint MENSAL numa subpasta por CNPJ/modalidade
# (`pncp_contratos/<cnpj>/AAAAMM`). Fora de ETAPAS_JANELA_ANUAL, elas eram
# "prontas" para sempre assim que o staged tinha linhas — o mês corrente nunca
# era atualizado em `--retomar`, que é o modo que o caderno usa sempre.
ETAPAS_JANELA_MENSAL: dict[str, str] = {
    "pncp_contratos": "pncp_contratos",
    "licitacoes_pncp": "licitacoes_pncp",
    "pncp_atas": "pncp_atas",
}


def janelas_pendentes_mensais(chave: str, ate: int, desde: int | None) -> list[str]:
    """Meses (AAAAMM) sem checkpoint utilizável em alguma subpasta da etapa.

    Mês anterior ao corrente vale para sempre (`_janela_encerrada`); o mês
    corrente expira em 12 h como o exercício em curso. Sem subpasta nenhuma a
    etapa nunca rodou — e aí é `_ja_coletado` quem decide, não esta função.
    """
    raiz = _checkpoint_dir(chave)
    if not raiz.is_dir():
        return []
    subpastas = [p for p in raiz.iterdir() if p.is_dir()]
    if not subpastas:
        return []
    piso = config.LICITACOES_PNCP_DESDE if chave == "licitacoes_pncp" else 2021
    ini = max(desde or piso, piso)
    agora = datetime.now()
    limite = agora.year * 100 + agora.month
    pendentes: set[str] = set()
    for jini, _jfim in clients.pncp_meses(ini, min(ate, agora.year)):
        rotulo = jini[:6]
        if int(rotulo) > limite:
            break
        for sub in subpastas:
            if _janela_estado(f"{chave}/{sub.name}", rotulo, int(rotulo[:4])) in (
                    None, "vazio_expirado"):
                pendentes.add(rotulo)
    return sorted(pendentes, reverse=True)


def etapa_pronta(nome: str, desde: int | None,
                 ate: int) -> tuple[pd.DataFrame | None, list]:
    """(DataFrame pronto ou None, exercícios pendentes).

    A regra do `--retomar`, num lugar só: a etapa está pronta quando tem linhas
    em `data/staged` **e** nenhuma janela ficou faltando. `inventario` e
    `build_staged` chamam esta função em vez de cada um ter a sua versão —
    duas versões da mesma regra divergem, e a que diverge em silêncio é a que
    decide ir ou não à rede.
    """
    pronto = _ja_coletado(nome)
    pendentes: list = []
    for chave in ETAPAS_JANELA_ANUAL.get(nome, ()):
        pendentes += janelas_pendentes(chave, ate, desde)
    pendentes = sorted(set(pendentes), reverse=True)
    if nome in ETAPAS_JANELA_MENSAL:
        pendentes += janelas_pendentes_mensais(ETAPAS_JANELA_MENSAL[nome], ate, desde)
    return (pronto if pronto is not None and not pendentes else None), pendentes


def _inventario_pncp(staged, manual: bool, ate: int) -> dict:
    """Linha do PNCP: janela MENSAL, uma subpasta por CNPJ.

    Os checkpoints moram em `pncp_contratos/<cnpj>/AAAAMM.csv` porque
    `_checkpoint_dir` não sanitiza a barra do nome da etapa — só a da janela.
    São ~66 janelas por órgão entre 2021 e hoje, a varredura mais longa da
    coleta depois dos exercícios; deixá-la fora do inventário escondia
    justamente o maior bloco do que falta.

    O ano sai de `stem[:4]`, NÃO de `int(stem)`: `_exercicio_encerrado(202401)`
    é False (202401 não é menor que o ano corrente), e o mês inteiro cairia no
    TTL de 12h para sempre — o PNCP apareceria como pendente em toda rodada.
    """
    raiz = _checkpoint_dir("pncp_contratos")
    com_dado = vazias = recusadas = 0
    if raiz.is_dir():
        for arq in raiz.rglob("*"):
            if not arq.is_file() or arq.suffix not in (".csv", ".vazio",
                                                       ".recusado"):
                continue
            try:
                ano = int(arq.stem[:4])
            except ValueError:
                continue
            if arq.suffix == ".recusado":
                recusadas += _recusa_fresca(
                    f"pncp_contratos/{arq.parent.name}", arq.stem)
                continue
            if not _checkpoint_utilizavel(arq, ano):
                continue
            com_dado += arq.suffix == ".csv"
            vazias += arq.suffix == ".vazio"
    tem_janela = com_dado + vazias > 0
    return {
        "etapa": "pncp_contratos",
        "linhas_staged": 0 if staged is None else int(len(staged)),
        "janelas_com_dado": com_dado,
        "janelas_vazias": vazias,
        "janelas_recusadas": recusadas,
        "recusados_pelo_portal": "",
        # Quantas FALTAM não é calculável sem resolver os CNPJs dos órgãos, o
        # que é uma consulta de rede — e este comando promete não fazer nenhuma.
        # Declarar o que se sabe e o que não se sabe, em vez de estimar.
        "janelas_faltando": "",
        "exercicios_faltando": (f"arquivo manual em data/{MANUAL_SUBDIR}/" if manual
                                else "janela mensal por CNPJ; quantas faltam só "
                                     "se sabe resolvendo os órgãos na rede"),
        "vai_a_rede": not manual and (staged is None or not tem_janela),
    }


def inventario(desde: int | None, ate: int) -> pd.DataFrame:
    """O que já está em disco e o que falta — sem uma requisição sequer.

    Uma coluna por pergunta que o operador faz antes de decidir se vale
    reconectar: quantas linhas já foram normalizadas, quantos exercícios estão
    em checkpoint, e QUAIS exercícios a próxima rodada ainda iria buscar.
    """
    linhas = []
    for nome, chaves in ETAPAS_JANELA_ANUAL.items():
        staged = _ja_coletado(nome)
        _, pend = etapa_pronta(nome, desde, ate)
        com_dado = vazias = recusadas = 0
        anos_recusados: list[int] = []
        for chave in chaves:
            for ano in range(desde or config.ANO_PISO, ate + 1):
                # Com a assinatura, como no laço real (`_anos_com_sondagem`):
                # sem ela um `.recusado` de pergunta antiga contava como
                # "recusado" aqui e como "pendente" lá, e `faltando` se
                # contradizia sobre o mesmo exercício.
                estado = _janela_estado(chave, str(ano), ano,
                                        _assinatura_etapa(chave, ano))
                com_dado += estado == "linhas"
                vazias += estado in ("vazio", "vazio_expirado")
                if estado == "recusado":
                    recusadas += 1
                    anos_recusados.append(ano)
        # A ingestão manual curto-circuita a etapa ANTES de qualquer checkpoint
        # (`stage_licitacoes` e `stage_atas` chamam `manual_rows` na primeira
        # linha). Sem consultar isso, o inventário anunciava 14 exercícios a
        # buscar para uma etapa que não faz uma requisição sequer.
        manual = manual_rows(nome) is not None
        linhas.append({
            "etapa": nome,
            "linhas_staged": 0 if staged is None else int(len(staged)),
            "janelas_com_dado": com_dado,
            "janelas_vazias": vazias,
            # O portal recusou a pergunta (400). Não é dado, não é ausência de
            # dado: é pergunta não aceita, e fica visível para não passar por
            # nenhum dos dois.
            "janelas_recusadas": recusadas,
            "janelas_faltando": 0 if manual else len(pend),
            "exercicios_faltando": (f"arquivo manual em data/{MANUAL_SUBDIR}/"
                                    if manual else
                                    ", ".join(str(a) for a in pend)),
            "recusados_pelo_portal": ", ".join(
                str(a) for a in sorted(set(anos_recusados), reverse=True)),
            "vai_a_rede": bool(pend) and not manual,
        })
    for nome in ("processos_itens", "indenizacoes_endpoint", "plano_contas",
                 "quadro_pessoal", "mandatarios", "pncp_contratos",
                 "licitacoes_pncp", "pncp_atas", "sancoes", "benchmark_creas"):
        staged = _ja_coletado(nome)
        manual = manual_rows(nome) is not None
        pronto, pend_m = etapa_pronta(nome, desde, ate)
        if nome == "pncp_contratos":
            inv = _inventario_pncp(staged, manual, ate)
            inv["janelas_faltando"] = len(pend_m)
            inv["vai_a_rede"] = pronto is None and not manual
            linhas.append(inv)
            continue
        linhas.append({
            "etapa": nome,
            "linhas_staged": 0 if staged is None else int(len(staged)),
            "janelas_com_dado": "", "janelas_vazias": "",
            "janelas_recusadas": "", "janelas_faltando": "",
            "recusados_pelo_portal": "",
            # Sem janela anual própria: o critério é o antigo — vazio se
            # recoleta, cheio se pula. Declarado, para o inventário não dar a
            # entender que conferiu exercício por exercício aqui.
            "exercicios_faltando": (f"arquivo manual em data/{MANUAL_SUBDIR}/"
                                    if manual else
                                    (", ".join(map(str, pend_m)) if pend_m else
                                     "sem janela anual (ver ETAPAS_JANELA_ANUAL)")),
            # `etapa_pronta` também reprova staged de esquema anterior
            # (COLUNAS_OBRIGATORIAS) e mês corrente vencido no PNCP.
            "vai_a_rede": pronto is None and not manual,
        })
    return pd.DataFrame(linhas)


def build_staged(desde: int | None, ate: int, *,
                 retomar: bool = False) -> dict[str, pd.DataFrame]:
    """Executa toda a coleta. desde=None ativa sondagem retroativa.

    `retomar=True` pula as etapas que já têm dado em disco — é o que torna
    barata a volta depois de uma queda de sessão. Com os diretórios apontados
    para o Google Drive, a coleta continua de onde parou em vez de recomeçar.

    Abaixo disso há a retomada FINA, que não depende de `--retomar`: cada
    exercício (e cada mês, no PNCP) já coletado ficou gravado como checkpoint
    em `data/raw/checkpoints/`, e a etapa refeita só vai à rede pelas janelas
    que faltam. É a diferença entre reprocessar dezessete exercícios e
    reprocessar o exercício em que a sessão caiu.
    """
    etapas = list(_etapas(desde, ate).items())

    # Lista de MÓDULO: duas coletas no mesmo processo (o Colab reaproveita o
    # kernel) empilhavam duas linhas por dataset em `_coleta_diagnostico.csv`, e
    # o check de cobertura passava a contar cada fonte duas vezes.
    _DIAGNOSTICO.clear()
    _JANELAS.clear()

    staged: dict[str, pd.DataFrame] = {}
    try:
        for nome, executar in etapas:
            if retomar:
                # CSV cheio NÃO é etapa completa. Uma etapa que trouxe
                # 2016-2025 e falhou em 2013-2015 tem linhas de sobra em
                # `staged` e três exercícios faltando — e a versão anterior a
                # pulava para sempre, porque olhava só se havia linhas. O
                # buraco não se fechava em rodada nenhuma, e `--retomar` é o
                # modo que o notebook usa SEMPRE. Refazer é barato: as janelas
                # que já estão em checkpoint saem do disco, sem rede.
                pronto, pendentes = etapa_pronta(nome, desde, ate)
                if pronto is None and pendentes and _ja_coletado(nome) is not None:
                    log.info("retomando: %s tem linhas mas faltam os exercícios "
                             "%s — refazendo a etapa (as janelas já coletadas "
                             "saem do disco, sem rede)",
                             nome, ", ".join(map(str, pendentes)))
                if pronto is not None:
                    log.info("retomando: %s já em disco (%d linhas) — pulando",
                             nome, len(pronto))
                    staged[nome] = pronto
                    _DIAGNOSTICO.append({
                        "dataset": nome, "linhas": int(len(pronto)),
                        "colunas": int(pronto.shape[1]),
                        "ano_min": None, "ano_max": None, "anos_cobertos": 0,
                        "anos_faltando": "", "retomado": True})
                    continue
            try:
                staged[nome] = executar()
            except RateLimited:
                # bloqueio por ritmo é sinal de parar tudo: insistir nas
                # etapas seguintes só piora
                raise
            except Exception as e:
                # Uma fonte fora do ar não pode derrubar a coleta inteira. O
                # PNCP devolveu 503 na última etapa e matou uma rodada em que
                # todo o resto já tinha vindo.
                log.warning("etapa %s falhou (%s: %s) — seguindo com as demais",
                            nome, type(e).__name__, e)
                # `_salvar` com DataFrame vazio grava `_vazio` POR CIMA do CSV
                # existente: um KeyError passageiro na normalização apagava
                # 40 mil linhas boas da rodada anterior. O dado que já está em
                # disco é preservado, e a falha vai para o diagnóstico — que é
                # onde ela tem de aparecer, não no lugar do dado.
                anterior = _ja_coletado(nome, exigir_esquema=False)
                if anterior is not None:
                    log.warning("etapa %s: mantendo as %d linhas da rodada "
                                "anterior em disco", nome, len(anterior))
                    staged[nome] = anterior
                    _DIAGNOSTICO.append({
                        "dataset": nome, "linhas": int(len(anterior)),
                        "colunas": int(anterior.shape[1]), "ano_min": None,
                        "ano_max": None, "anos_cobertos": 0,
                        "anos_faltando": "", "preservado_de_rodada_anterior": True})
                else:
                    staged[nome] = _salvar(nome, pd.DataFrame())
                _DIAGNOSTICO[-1]["erro"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        # Grava o inventário mesmo se a coleta abortar no meio: sem isso, uma
        # queda no meio do caminho deixaria o operador sem saber o que já veio.
        # O ledger HTTP entra aqui pelo mesmo motivo: é justamente na rodada
        # que abortou por bloqueio que ele tem mais a explicar.
        config.STAGED_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.STAGED_DIR / "_resumo.json", "w") as f:
            json.dump({k: len(v) for k, v in staged.items()}, f,
                      indent=2, ensure_ascii=False)
        salvar_diagnostico()
        salvar_ledger_http()

    # Datasets que uma etapa grava mas NÃO devolve — `stage_orcamento` salva
    # `balanco_orcamentario` e devolve `comparativo_receita`; `stage_pncp` salva
    # `pncp_orgaos`. Eles existiam em disco e não no dicionário, então
    # `main.py analyze` encontrava dois achados de orçamento que
    # `main.py all` perdia, sobre exatamente o mesmo dado. Reler o que sobrou em
    # disco fecha o buraco sem exigir que cada stage_* declare seus datasets.
    for nome, df_disco in load_staged().items():
        if nome not in staged and not nome.startswith("_"):
            log.info("staged/%s.csv existe em disco e não foi devolvido por "
                     "etapa nenhuma (%d linhas) — entrando na análise",
                     nome, len(df_disco))
            staged[nome] = df_disco

    staged["_coleta_diagnostico"] = salvar_diagnostico()
    staged["_coleta_http"] = salvar_ledger_http()
    return staged


# Colunas cujo conteúdo é uma STRING DE DÍGITOS, não um número. Relidas com
# inferência de tipo, `01234567000189` vira `1234567000189` e o zero à esquerda
# some — e `checks/common.py` filtra CNPJ por `str.len() == 14`, então o
# fornecedor simplesmente desaparece da análise, sem erro nenhum. É a mesma
# classe de defeito que `_checkpoint_ler` já evita com `dtype=str`.
COLUNAS_TEXTO_STAGED = ("CPF_CNPJ", "FAVORECIDO_CNPJ", "CONTRATADA_CNPJ",
                        "CNPJ", "CPF", "NUMERO_EMPENHO", "NUMERO_PAGAMENTO",
                        "NUMERO", "NUMERO_CONTRATO", "NUMERO_LICITACAO",
                        # PNCP e sanções — as que ficaram de fora e custaram caro
                        "fornecedor_ni", "orgao_cnpj", "cnpjOrgao", "niFornecedor",
                        "documento", "numeroControlePNCP", "numeroControlePNCPCompra")
# Lista explícita NÃO basta: `fornecedor_ni` (CNPJ do fornecedor no PNCP) não
# estava nela, foi relida como int64, perdeu o zero à esquerda, e o cruzamento
# portal × PNCP deixou de casar todo fornecedor cujo CNPJ começa com 0. Na
# rodada de 04/09/2026, 25 dos 39 contratos publicados como "ausentes do PNCP"
# (R$ 66,5 mi, 61% do valor) ESTAVAM no PNCP — em 15 casos era o mesmo
# contrato, mesmo número e valor. Por isso a regra também é por PADRÃO de nome:
# qualquer coluna que fale de CNPJ, CPF, NI (número de inscrição), documento ou
# número de controle é texto, sempre.
_PADRAO_COLUNA_TEXTO = re.compile(r"(cnpj|cpf|(^|_)ni($|_)|documento|numerocontrole|"
                                  r"^numero(_|$)|_numero$)", re.I)


def _colunas_texto(cabecalho) -> dict:
    return {c: str for c in cabecalho
            if c in COLUNAS_TEXTO_STAGED or _PADRAO_COLUNA_TEXTO.search(str(c))}


_RX_DOCUMENTO_SUSPEITO = re.compile(r"^\d+\.0+$|^\d(?:\.\d+)?[eE]\+\d+$")


def _sanear_documentos(df: pd.DataFrame, nome: str) -> pd.DataFrame:
    """Repara colunas de documento gravadas por versões anteriores que as
    deixaram passar por float ("…141.0") e recompõe as derivadas a partir da
    coluna-fonte quando ela existe: CPF_CNPJ → FAVORECIDO_CNPJ nos pagamentos,
    o CNPJ formatado dentro de CONTRATADA → CONTRATADA_CNPJ nos contratos.
    Idempotente: sobre CSV bom não muda nada."""
    if df is None or df.empty:
        return df
    for c in df.columns:
        if not (pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c])):
            continue
        if not (c in COLUNAS_TEXTO_STAGED or _PADRAO_COLUNA_TEXTO.search(str(c))):
            continue
        s = df[c]
        suspeitas = s.astype(str).str.match(_RX_DOCUMENTO_SUSPEITO, na=False)
        if suspeitas.any():
            log.info("%s: %d valor(es) de %s vieram como float de uma rodada "
                     "anterior — reparados", nome, int(suspeitas.sum()), c)
            df[c] = s.where(~suspeitas, s.map(so_digitos))
    if nome == "pagamentos" and "CPF_CNPJ" in df.columns:
        df["FAVORECIDO_CNPJ"] = df["CPF_CNPJ"].map(so_digitos)
    if nome == "contratos" and "CONTRATADA" in df.columns:
        extraido = (df["CONTRATADA"].fillna("").astype(str)
                    .str.extract(RX_CNPJ_FORMATADO, expand=False).map(so_digitos))
        atual = coluna(df, "CONTRATADA_CNPJ", "").fillna("").astype(str)
        df["CONTRATADA_CNPJ"] = extraido.where(extraido.str.len().eq(14), atual)
    return df


def _ler_csv_staged(caminho: Path) -> pd.DataFrame:
    """Leitor ÚNICO dos CSVs staged: colunas de documento como texto.

    `load_staged` já lia assim; `_ja_coletado` — o caminho que `main.py all`
    percorre para toda etapa pronta, ou seja, quase todas em toda rodada —
    lia com `read_csv` puro, e CONTRATADA_CNPJ / FAVORECIDO_CNPJ viravam
    float64. Na rodada 11 isso cegou a frente de fornecedores inteira (0,4%
    do valor pago com CNPJ "legível", contra 82% na rodada 10) e publicou 44
    contratos como ausentes do PNCP por CNPJ com um dígito a mais.

    `dtype` só nas colunas de dígitos: aplicar `dtype=str` no arquivo
    inteiro transformaria VALOR em texto e derrubaria toda soma.
    """
    cabecalho = pd.read_csv(caminho, nrows=0).columns
    forcar = _colunas_texto(cabecalho)
    df = pd.read_csv(caminho, low_memory=False, dtype=forcar,
                     keep_default_na=False if forcar else True,
                     na_values=[""] if forcar else None)
    if list(df.columns) == ["_vazio"]:
        return pd.DataFrame()
    df = _sanear_documentos(df, caminho.stem)
    for col in ("DATA", "INI", "FIM"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def load_staged() -> dict[str, pd.DataFrame]:
    """Recarrega os CSVs staged sem refazer coleta."""
    out = {}
    for path in sorted(config.STAGED_DIR.glob("*.csv")):
        try:
            out[path.stem] = _ler_csv_staged(path)
        except (pd.errors.EmptyDataError, ValueError):
            out[path.stem] = pd.DataFrame()
    return out
