"""Coleta e normalização: APIs -> data/raw (JSON) -> data/staged (CSV).

A sondagem retroativa tenta anos cada vez mais antigos (meta do usuário:
alcançar exercícios pré-2016) e para após N anos vazios consecutivos.
"""
from __future__ import annotations

import json
import logging

import pandas as pd

from . import clients, config
from .utils import (classificar_pessoa, coluna, norm_nome, parse_data,
                    parse_valor, so_digitos)

log = logging.getLogger("queda.pipeline")


# ---------------------------------------------------------------- helpers
def _salvar(nome: str, df: pd.DataFrame) -> pd.DataFrame:
    config.STAGED_DIR.mkdir(parents=True, exist_ok=True)
    caminho = config.STAGED_DIR / f"{nome}.csv"
    if df is None or df.empty:
        # DataFrame sem colunas grava um CSV vazio que pd.read_csv rejeita com
        # EmptyDataError, quebrando `analyze` e `report` depois da coleta.
        caminho.write_text("_vazio\n")
        log.info("staged/%s.csv: 0 linhas", nome)
        return pd.DataFrame() if df is None else df
    df.to_csv(caminho, index=False)
    log.info("staged/%s.csv: %d linhas", nome, len(df))
    return df


def _anos_com_sondagem(fetch_ano, ate: int, desde: int | None):
    """Itera anos de `ate` para trás. Se desde=None, sonda até esvaziar."""
    frames = []
    vazios = 0
    ano = ate
    piso = desde or config.ANO_PISO
    while ano >= piso:
        rows = fetch_ano(ano)
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
    return frames


# ---------------------------------------------------------------- datasets
def stage_empenhos(desde, ate):
    rows = _anos_com_sondagem(lambda a: clients.implanta_ano("Empenhos", a), ate, desde)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["VALOR"] = coluna(df, "VALOR", 0).map(parse_valor)
        df["VALOR_PAGO"] = coluna(df, "VALOR_PAGO", 0).map(parse_valor)
        df["VALOR_ANULADO"] = coluna(df, "VALOR_ANULADO", 0).map(parse_valor)
        df["DATA"] = parse_data(coluna(df, "DATA_NOTA_FISCAL", None))
    return _salvar("empenhos", df)


def stage_pagamentos(desde, ate):
    rows = _anos_com_sondagem(lambda a: clients.implanta_ano("Pagamentos", a), ate, desde)
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
        lambda a: clients.implanta_ano("Contratos", a, formato="vigencia"), ate, desde)
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
        lambda a: clients.implanta_ano("ContratosAditivos", a, formato="vigencia"), ate, desde)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["VALOR"] = coluna(df, "VALOR", 0).map(parse_valor)
        df = df.drop_duplicates(subset=[c for c in ("NUMERO_CONTRATO", "VALOR", "DATA_PUBLICACAO") if c in df])
    return _salvar("aditivos", df)


def stage_licitacoes(desde, ate):
    rows = _anos_com_sondagem(lambda a: clients.implanta_ano("Licitacoes", a), ate, desde)
    return _salvar("licitacoes", pd.DataFrame(rows))


def stage_convenios(desde, ate):
    rows = _anos_com_sondagem(lambda a: clients.implanta_ano("Convenios", a), ate, desde)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["VALOR_TOTAL"] = coluna(df, "VALOR_TOTAL", 0).map(parse_valor)
    return _salvar("convenios", df)


def stage_comparativo_despesa(desde, ate):
    rows = _anos_com_sondagem(lambda a: clients.implanta_ano("ComparativoDespesa", a), ate, desde)
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
        lambda a: clients.implanta_ano("DespesasCentroCusto", a), ate, desde)
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


def stage_demonstrativo(desde, ate):
    """Orçado × empenhado × liquidado × pago por conta/centro de custo."""
    frames = []
    piso = desde or config.ANO_PISO
    vazios = 0
    for ano in range(ate, piso - 1, -1):
        rows = clients.implanta_ano("DemonstrativoEmpenhosPagamentos", ano)
        achatado = clients.achatar_demonstrativo(rows)
        if achatado:
            vazios = 0
            for r in achatado:
                r["_ano"] = ano
            frames.extend(achatado)
        else:
            vazios += 1
            if desde is None and vazios >= config.SONDAGEM_VAZIOS:
                break
    df = pd.DataFrame(frames)
    if not df.empty:
        for c in ("ORCADO", "EMPENHADOEXERCICIO", "LIQUIDADO_EXERCICIO",
                  "PAGO_EXERCICIO", "SALDO_ORCAMENTARIO", "EMPENHA_PERIODO"):
            if c in df:
                df[c] = df[c].map(parse_valor)
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
    processos, itens = [], []
    piso = desde or config.ANO_PISO
    vazios = 0
    for ano in range(ate, piso - 1, -1):
        rows = clients.implanta_ano("ProcessosComprasServicos", ano)
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
    rows = []
    piso = desde or config.ANO_PISO
    vazios = 0
    for ano in range(ate, piso - 1, -1):
        got = clients.implanta_datas("AtasColegiados", ano)
        if got:
            vazios = 0
            for r in got:
                r["_ano"] = ano
            rows.extend(got)
        else:
            vazios += 1
            if desde is None and vazios >= config.SONDAGEM_VAZIOS:
                break
    return _salvar("atas", pd.DataFrame(rows))


def stage_orcamento(desde, ate):
    """Balanço orçamentário e comparativo de receita (denominadores)."""
    orc = _anos_com_sondagem(
        lambda a: clients.implanta_ano("BalancoOrcamentario", a), ate, desde)
    dfo = pd.DataFrame(orc)
    if not dfo.empty:
        for c in ("DOTACOES", "DOTACOES_ATUALIZADAS", "RECEITAS_REALIZADAS",
                  "DESPESAS_EMPENHADAS", "DESPESAS_LIQUIDADAS", "DESPESAS_PAGAS"):
            if c in dfo:
                dfo[c] = dfo[c].map(parse_valor)
    _salvar("balanco_orcamentario", dfo)

    rec = _anos_com_sondagem(
        lambda a: clients.implanta_ano("ComparativoReceita", a), ate, desde)
    dfr = pd.DataFrame(rec)
    if not dfr.empty:
        for c in ("VALOR_ORCADO", "REALIZADO_EXERCICIO", "TOTAL_EXERCICIO"):
            if c in dfr:
                dfr[c] = dfr[c].map(parse_valor)
    return _salvar("comparativo_receita", dfr)


def stage_quadro_pessoal():
    """Quadro de pessoal: tenta Dados Abertos e cai para Implanta QuadrosPessoas."""
    rows = clients.dados_abertos("quadro-colaboradores")
    df = pd.DataFrame(rows)
    if df.empty:
        pessoas = []
        for quadro in clients.implanta("QuadrosPessoas"):
            for p in quadro.get("PESSOAS") or []:
                p["TITULO_QUADRO"] = quadro.get("TITULO")
                pessoas.append(p)
        df = pd.DataFrame(pessoas)
        ren = {"NOME": "nome", "CPF": "cpf", "NOME_CARGO": "nomeCargo",
               "VINCULO": "vinculo", "REMUNERACAO_MENSAL": "renumeracaoMensal",
               "SITUACAO": "situacao", "DATA_ADMISSAO": "dataAdmissao",
               "NOME_LOTACAO": "nomeLotacao"}
        df = df.rename(columns=ren)
    if not df.empty:
        df["remuneracao"] = coluna(df, "renumeracaoMensal", 0).map(parse_valor)
        df["NOME_NORM"] = coluna(df, "nome").map(norm_nome)
        df["VINCULO_NORM"] = coluna(df, "vinculo").map(norm_nome)
    return _salvar("quadro_pessoal", df)


def stage_mandatarios():
    rows = clients.dados_abertos("mandatarios")
    df = pd.DataFrame(rows)
    if df.empty:
        pessoas = []
        for grupo in clients.implanta("Conselheiros"):
            for p in grupo.get("CONSELHEIROS") or []:
                p["TITULO"] = grupo.get("TITULO")
                pessoas.append(p)
        df = pd.DataFrame(pessoas)
        df = df.rename(columns={"NOME_MANDATARIO": "nomeMandatario", "CPF": "cpfMandatario",
                                "CARGO": "cargo", "DATA_POSSE": "dataPosse"})
    if not df.empty:
        df["NOME_NORM"] = coluna(df, "nomeMandatario").map(norm_nome)
    return _salvar("mandatarios", df)


def stage_indenizacoes(desde, ate):
    """Endpoints dedicados de indenizações/diárias (podem estar vazios —
    o próprio vazio vira achado de transparência no check)."""
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


def stage_pncp(desde, ate):
    """Contratos PNCP do Crea-SP e dos órgãos relacionados (rede de influência)."""
    ini = max(desde or 2021, 2021)   # PNCP só existe desde ago/2021
    orgaos = []
    alvo = clients.pncp_resolver_orgao(config.ORGAO_ALVO_RAZAO,
                                       cnpj_fixo=config.ORGAO_ALVO_CNPJ)
    if alvo and alvo.get("cnpj"):
        orgaos.append({**alvo, "papel": "alvo"})
    for razao in config.ORGAOS_RELACIONADOS_RAZAO:
        o = clients.pncp_resolver_orgao(razao)
        if o and o.get("cnpj") and o["cnpj"] != (alvo or {}).get("cnpj"):
            orgaos.append({**o, "papel": "relacionado"})
    _salvar("pncp_orgaos", pd.DataFrame(orgaos))

    contratos, cobrancas = [], []
    for o in orgaos:
        rows = clients.pncp_contratos(o["cnpj"], ini, ate)
        for r in rows:
            contratos.append({
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
            })
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
        df_c["fornecedor_norm"] = df_c["fornecedor_nome"].map(norm_nome)
    _salvar("pncp_contratos", df_c)
    _salvar("pncp_cobrancas", pd.DataFrame(cobrancas))
    return df_c


# ---------------------------------------------------------------- entrada
def build_staged(desde: int | None, ate: int) -> dict[str, pd.DataFrame]:
    """Executa toda a coleta. desde=None ativa sondagem retroativa."""
    staged = {
        "empenhos": stage_empenhos(desde, ate),
        "pagamentos": stage_pagamentos(desde, ate),
        "contratos": stage_contratos(desde, ate),
        "aditivos": stage_aditivos(desde, ate),
        "licitacoes": stage_licitacoes(desde, ate),
        "convenios": stage_convenios(desde, ate),
        "comparativo_despesa": stage_comparativo_despesa(desde, ate),
        "despesas_centro_custo": stage_despesas_centro_custo(desde, ate),
        "demonstrativo": stage_demonstrativo(desde, ate),
        "plano_contas": stage_plano_contas(ate),
        "processos_itens": stage_processos_compras(desde, ate),
        "atas": stage_atas(desde, ate),
        "comparativo_receita": stage_orcamento(desde, ate),
        "quadro_pessoal": stage_quadro_pessoal(),
        "mandatarios": stage_mandatarios(),
        "indenizacoes_endpoint": stage_indenizacoes(desde, ate),
        "pncp_contratos": stage_pncp(desde, ate),
    }
    resumo = {k: len(v) for k, v in staged.items()}
    with open(config.STAGED_DIR / "_resumo.json", "w") as f:
        json.dump(resumo, f, indent=2, ensure_ascii=False)
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
