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
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import clients, config
from .http import RateLimited
from .utils import (classificar_pessoa, coluna, norm_nome, parse_data,
                    parse_valor, so_digitos)

log = logging.getLogger("queda.pipeline")

# ---------------------------------------------------------------- constantes
# MIGRAR PARA config.py: estas três não estão lá porque config.py pertence a
# outro agente nesta rodada. São parâmetros de operação, não de análise.
CHECKPOINT_SUBDIR = "checkpoints"
# Exercício ainda EM CURSO muda todo dia: um checkpoint dele vale poucas horas.
# Exercício encerrado não muda mais e o checkpoint vale para sempre.
CHECKPOINT_TTL_HORAS = 12.0
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


def _checkpoint_arquivos(etapa: str, janela: str) -> tuple[Path, Path]:
    """(csv com linhas, marcador de janela vazia).

    São dois arquivos porque um CSV de zero linhas é ambíguo: pode ser "o
    servidor respondeu que não há nada neste exercício" ou "a gravação foi
    interrompida". O marcador `.vazio` afirma a primeira coisa e só é escrito
    quando o servidor de fato respondeu.
    """
    base = _checkpoint_dir(etapa) / "".join(
        c if c.isalnum() or c in "-_" else "_" for c in str(janela))
    return base.with_suffix(".csv"), base.with_suffix(".vazio")


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


def _checkpoint_utilizavel(caminho: Path, ano: int | None) -> bool:
    """Checkpoint de exercício encerrado vale sempre; do exercício em curso,
    só por algumas horas — senão a rodada de dezembro continuaria mostrando o
    exercício como ele estava em janeiro."""
    if not caminho.exists():
        return False
    if _exercicio_encerrado(ano):
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
    csv, vazio = _checkpoint_arquivos(etapa, janela)
    if _checkpoint_utilizavel(csv, ano):
        try:
            d = pd.read_csv(csv, dtype=str, keep_default_na=False,
                            low_memory=False)
        except (pd.errors.EmptyDataError, ValueError, OSError) as e:
            log.warning("checkpoint %s ilegível (%s) — será recoletado",
                        csv.name, type(e).__name__)
            return None
        return [{k: _do_texto(v) for k, v in r.items()}
                for r in d.to_dict("records")]
    if _checkpoint_utilizavel(vazio, ano):
        return []
    return None


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
    csv, vazio = _checkpoint_arquivos(etapa, janela)
    try:
        csv.parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            vazio.write_text(f"{datetime.now().isoformat(timespec='seconds')}\n")
            csv.unlink(missing_ok=True)
            return
        if not _plano_para_csv(rows):
            log.debug("%s/%s: payload aninhado — sem checkpoint", etapa, janela)
            return
        # `_ano` é reposto pelo chamador como inteiro a cada leitura; gravá-lo
        # faria o rótulo do exercício voltar como texto e quebrar toda
        # comparação numérica de ano nos checks.
        limpos = [{k: v for k, v in r.items() if k != "_ano"} for r in rows]
        # Gravação atômica: uma queda de sessão no meio do `to_csv` deixaria um
        # CSV truncado que a rodada seguinte leria como se fosse completo.
        parcial = csv.with_suffix(".csv.parcial")
        pd.DataFrame(limpos).to_csv(parcial, index=False)
        os.replace(parcial, csv)
        vazio.unlink(missing_ok=True)
    except OSError as e:
        # Disco cheio ou Drive desmontado não podem derrubar a coleta: o
        # checkpoint é otimização, não requisito.
        log.warning("não foi possível gravar checkpoint %s/%s (%s) — a coleta "
                    "segue, só não haverá retomada fina desta janela",
                    etapa, janela, e)


def _registrar_janelas(dataset: str, falhas: list, do_disco: int,
                       coletadas: int) -> None:
    """Fecha a contabilidade de janelas de uma etapa: log + diagnóstico.

    Acumula em vez de sobrescrever porque uma etapa pode ter mais de um laço de
    janelas (o PNCP tem um por órgão) e o que interessa ao operador é o total
    que ficou de fora do dataset.
    """
    log.info("%s: %d janela(s) do checkpoint, %d coletada(s) agora, "
             "%d sem coleta", dataset, do_disco, coletadas, len(falhas))
    if falhas:
        log.warning("%s: %d janela(s) ficaram FORA desta rodada (%s). Rode de "
                    "novo: as janelas já gravadas em checkpoint não são "
                    "refeitas e só estas voltam à rede.",
                    dataset, len(falhas), ", ".join(str(f) for f in falhas[:12]))
    d = _JANELAS.setdefault(dataset, {"janelas_sem_coleta": 0})
    d["janelas_sem_coleta"] += len(falhas)
    if falhas:
        texto = ", ".join(str(f) for f in falhas)
        d["janelas_falhas"] = ", ".join(x for x in (d.get("janelas_falhas"),
                                                    texto) if x)


def _janelas_nomeadas(etapa: str, janelas, buscar, *, dataset: str | None = None):
    """Percorre janelas arbitrárias com checkpoint, tolerando falhas.

    `janelas` é um iterável de `(rótulo, ano, *args)` e `buscar(*args)` devolve
    as linhas da janela (ou None, quando o host não respondeu). Serve aos laços
    que não são "um exercício por vez" — hoje, os meses do PNCP, que são a
    outra varredura longa da coleta (66 janelas paginadas para 2021-2026).
    """
    out: list[dict] = []
    falhas: list[str] = []
    do_disco = coletadas = 0
    for rotulo, ano, *args in janelas:
        rows = _checkpoint_ler(etapa, rotulo, ano)
        if rows is not None:
            do_disco += 1
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
                falhas.append(rotulo)
                continue
            _checkpoint_gravar(etapa, rotulo, rows)
            coletadas += 1
        out.extend(rows)
    _registrar_janelas(dataset or etapa, falhas, do_disco, coletadas)
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
    do_disco = coletadas = 0
    ano = ate
    piso = desde or config.ANO_PISO
    while ano >= piso:
        rows = _checkpoint_ler(etapa, str(ano), ano)
        if rows is not None:
            do_disco += 1
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
                falhas.append(ano)
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

    _registrar_janelas(etapa, sorted(falhas, reverse=True), do_disco, coletadas)
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
            r"(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})", expand=False).map(so_digitos)
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
    rows = _anos_com_sondagem(lambda a: clients.implanta_ano("Licitacoes", a), ate,
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
    rows = _anos_com_sondagem(
        lambda a: clients.implanta_datas("AtasColegiados", a), ate, desde,
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
            linhas, _ = clients.implanta_variantes("QuadrosPessoas", ate)
            df = pd.DataFrame(linhas)
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
            linhas, _ = clients.implanta_variantes("Conselheiros", ate)
            df = pd.DataFrame(linhas)
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


def _pncp_contrato_linha(o: dict, r: dict) -> dict:
    """Projeta um contrato do PNCP nas colunas que a análise usa."""
    return {
        "orgao_cnpj": o["cnpj"], "orgao_papel": o["papel"],
        "orgao_nome": o.get("razaoSocial"),
        "numeroControlePNCP": r.get("numeroControlePNCP"),
        "fornecedor_ni": so_digitos(r.get("niFornecedor")),
        "fornecedor_nome": r.get("nomeRazaoSocialFornecedor"),
        "tipoPessoa": r.get("tipoPessoa"),
        "objeto": r.get("objetoContrato"),
        "valorGlobal": parse_valor(r.get("valorGlobal")),
        "valorAcumulado": parse_valor(r.get("valorAcumulado")),
        "dataAssinatura": r.get("dataAssinatura"),
        "vigenciaInicio": r.get("dataVigenciaInicio"),
        "vigenciaFim": r.get("dataVigenciaFim"),
        "anoContrato": r.get("anoContrato"),
        "numeroContrato": r.get("numeroContratoEmpenho"),
        # O contrato não expõe edital nem termo de referência; a COMPRA de
        # origem sim. É por este número que se chega aos documentos do certame
        # — e o portal do próprio Crea não publica nenhum número de licitação
        # em 965 contratos.
        "numeroControlePNCPCompra": r.get("numeroControlePNCPCompra"),
        "processo": r.get("processo"),
    }


def stage_pncp(desde, ate):
    """Contratos PNCP do Crea-SP e dos órgãos relacionados (rede de influência)."""
    ini = max(desde or 2021, 2021)   # PNCP só existe desde ago/2021
    orgaos = []
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
    for razao in config.ORGAOS_RELACIONADOS_RAZAO:
        o = clients.pncp_resolver_orgao(razao)
        if o and o.get("cnpj") and o["cnpj"] != (alvo or {}).get("cnpj"):
            orgaos.append({**o, "papel": "relacionado"})
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
            return [_pncp_contrato_linha(orgao, r) for r in linhas]

        contratos.extend(_janelas_nomeadas(
            f"pncp_contratos/{o['cnpj']}",
            ((jini[:6], int(jini[:4]), jini, jfim)
             for jini, jfim in clients.pncp_meses(ini, ate)),
            buscar_mes, dataset="pncp_contratos"))
        if o["papel"] == "alvo":
            for r in clients.pncp_instrumentos_cobranca(o["cnpj"], ini, ate):
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
    _salvar("pncp_cobrancas", pd.DataFrame(cobrancas))
    return df_c


# ---------------------------------------------------------------- entrada
def _ja_coletado(nome: str) -> pd.DataFrame | None:
    """DataFrame já staged com linhas, ou None se falta ou veio vazio.

    Vazio conta como NÃO coletado de propósito: dataset zerado é quase sempre
    resultado de 403 por rajada, e 403 nunca é cacheado — repetir a etapa é
    exatamente o que se quer.
    """
    caminho = config.STAGED_DIR / f"{nome}.csv"
    if not caminho.exists():
        return None
    try:
        df = pd.read_csv(caminho, low_memory=False)
    except (pd.errors.EmptyDataError, ValueError, OSError):
        return None
    if df.empty or list(df.columns) == ["_vazio"]:
        return None
    for col in ("DATA", "INI", "FIM"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


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
    etapas = [
        ("empenhos", lambda: stage_empenhos(desde, ate)),
        ("pagamentos", lambda: stage_pagamentos(desde, ate)),
        ("contratos", lambda: stage_contratos(desde, ate)),
        ("aditivos", lambda: stage_aditivos(desde, ate)),
        ("licitacoes", lambda: stage_licitacoes(desde, ate)),
        ("convenios", lambda: stage_convenios(desde, ate)),
        ("comparativo_despesa", lambda: stage_comparativo_despesa(desde, ate)),
        ("despesas_centro_custo", lambda: stage_despesas_centro_custo(desde, ate)),
        ("demonstrativo", lambda: stage_demonstrativo(desde, ate)),
        ("plano_contas", lambda: stage_plano_contas(ate)),
        ("processos_itens", lambda: stage_processos_compras(desde, ate)),
        ("atas", lambda: stage_atas(desde, ate)),
        ("comparativo_receita", lambda: stage_orcamento(desde, ate)),
        ("quadro_pessoal", lambda: stage_quadro_pessoal(ate)),
        ("mandatarios", lambda: stage_mandatarios(ate)),
        ("indenizacoes_endpoint", lambda: stage_indenizacoes(desde, ate)),
        ("pncp_contratos", lambda: stage_pncp(desde, ate)),
    ]

    staged: dict[str, pd.DataFrame] = {}
    try:
        for nome, executar in etapas:
            if retomar:
                pronto = _ja_coletado(nome)
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

    staged["_coleta_diagnostico"] = salvar_diagnostico()
    staged["_coleta_http"] = salvar_ledger_http()
    return staged


def load_staged() -> dict[str, pd.DataFrame]:
    """Recarrega os CSVs staged sem refazer coleta."""
    out = {}
    for path in sorted(config.STAGED_DIR.glob("*.csv")):
        try:
            df = pd.read_csv(path, low_memory=False)
        except (pd.errors.EmptyDataError, ValueError):
            out[path.stem] = pd.DataFrame()
            continue
        if list(df.columns) == ["_vazio"]:
            out[path.stem] = pd.DataFrame()
            continue
        for col in ("DATA", "INI", "FIM"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        out[path.stem] = df
    return out
