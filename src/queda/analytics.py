"""Utilitários analíticos comuns: séries anuais, rankings por ano, deflação,
tendência e testes forenses estatísticos.

Regra do projeto: **toda métrica é anual**. Nada de "todo o período" sem que
exista também a decomposição ano a ano.
"""
from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

from . import config
from .http import get_json, memo

log = logging.getLogger("queda.analytics")

# IPCA acumulado no ano (% dez/dez, IBGE). Usado só como fallback quando a API
# do BCB não estiver acessível. Ano ausente aqui e no BCB recebe fator 1,0 e o
# relatório marca a série como NOMINAL naquele ano — nunca finge deflação.
IPCA_FALLBACK = {
    2010: 5.91, 2011: 6.50, 2012: 5.84, 2013: 5.91, 2014: 6.41, 2015: 10.67,
    2016: 6.29, 2017: 2.95, 2018: 3.75, 2019: 4.31, 2020: 4.52, 2021: 10.06,
    2022: 5.79, 2023: 4.62, 2024: 4.83,
}


# ------------------------------------------------------------------ séries
def serie_anual(df: pd.DataFrame, valor: str, ano: str = "_ano",
                by: str | None = None, agg: str = "sum") -> pd.DataFrame:
    """Série anual (opcionalmente quebrada por uma dimensão). Índice = ano."""
    if df is None or df.empty or valor not in df.columns or ano not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=[ano]).copy()
    d[ano] = pd.to_numeric(d[ano], errors="coerce").astype("Int64")
    d = d.dropna(subset=[ano])
    if by and by in d.columns:
        out = d.pivot_table(index=ano, columns=by, values=valor, aggfunc=agg,
                            fill_value=0)
    else:
        out = d.groupby(ano)[valor].agg(agg).to_frame(valor)
    return out.sort_index()


def ranking_anual(df: pd.DataFrame, entidade: str, valor: str,
                  ano: str = "_ano", top: int = 10) -> pd.DataFrame:
    """Top-N por ano — substitui rankings de 'todo o período'."""
    if df is None or df.empty or not {entidade, valor, ano} <= set(df.columns):
        return pd.DataFrame()
    d = df.dropna(subset=[ano]).copy()
    d[ano] = pd.to_numeric(d[ano], errors="coerce").astype("Int64")
    g = (d.groupby([ano, entidade])[valor]
         .agg(total="sum", operacoes="size").reset_index())
    g["posicao"] = g.groupby(ano)["total"].rank(ascending=False, method="first")
    g = g[g["posicao"] <= top].sort_values([ano, "total"], ascending=[True, False])
    return g.astype({"posicao": int})


def cagr(serie: pd.Series) -> float:
    """Taxa de crescimento anual composta entre o primeiro e o último ano."""
    s = serie.dropna()
    s = s[s > 0]
    if len(s) < 2:
        return float("nan")
    anos = int(s.index[-1]) - int(s.index[0])
    if anos <= 0:
        return float("nan")
    return (float(s.iloc[-1]) / float(s.iloc[0])) ** (1 / anos) - 1


def variacao_real(serie: pd.Series, base_ano: int | None = None) -> pd.Series:
    """Série deflacionada pelo IPCA para o ano-base (padrão: último ano).

    Anos sem IPCA publicado saem da série real em vez de entrarem com fator
    1,0: misturar um ano a preços correntes com os demais a preços do ano-base
    inventa uma queda real que não existe e contamina o CAGR.
    """
    if serie.empty:
        return serie
    fatores = fatores_ipca(base_ano or int(serie.index[-1]))
    if not fatores:
        return serie.iloc[0:0]
    validos = [a for a in serie.index if int(a) in fatores]
    if len(validos) < len(serie):
        log.info("deflação: %d ano(s) sem IPCA ficaram fora da série real",
                 len(serie) - len(validos))
    s = serie.loc[validos]
    return s * pd.Series({a: fatores[int(a)] for a in validos})


def _ipca_indice_medio() -> dict[int, float]:
    """Índice MÉDIO ANUAL do IPCA (base arbitrária, só a razão importa).

    Despesa é fluxo: o gasto de um exercício ocorre distribuído ao longo do
    ano, não em 31/12. Deflacionar pelo acumulado dez/dez superestima a
    inflação sofrida por esse fluxo — o índice correto é a média dos índices
    mensais do ano. Com IPCA de 10% num ano, a diferença entre os dois
    critérios chega a ~5 pontos, o bastante para inverter o sinal de um CAGR
    real.

    Tenta a série mensal do BCB (433) para calcular a média de verdade; sem
    ela, aproxima a média pela média geométrica dos índices de fim de ano.
    """
    def buscar():
        url = ("https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados"
               "?formato=json&dataInicial=01/01/2009")
        data = get_json(url)
        if not isinstance(data, list) or not data:
            return None
        df = pd.DataFrame(data)
        df["ano"] = df["data"].str[-4:].astype(int)
        df["v"] = df["valor"].astype(str).str.replace(",", ".").astype(float)
        df = df.sort_values("data", key=lambda s: s.str[-4:] + s.str[3:5])
        nivel = (1 + df["v"] / 100).cumprod()
        return {int(a): float(nivel[g.index].mean())
                for a, g in df.groupby("ano")}

    try:
        vindo = memo("ipca_indice_medio_433", buscar)
    except Exception as e:                                   # pragma: no cover
        log.warning("IPCA: BCB indisponível (%s) — usando tabela embutida", e)
        vindo = None
    if vindo:
        return {int(k): float(v) for k, v in vindo.items()}

    # Fallback: índice de fim de ano a partir das taxas anuais; a média do ano
    # é aproximada pela média geométrica entre o fim do ano anterior e o dele.
    anos = sorted(IPCA_FALLBACK)
    fim: dict[int, float] = {}
    nivel = 1.0
    for a in anos:
        anterior = nivel
        nivel *= 1 + IPCA_FALLBACK[a] / 100
        fim[a] = nivel
        fim[f"ini{a}"] = anterior                            # type: ignore[index]
    return {a: math.sqrt(fim[f"ini{a}"] * fim[a]) for a in anos}  # type: ignore[index]


def anos_sem_deflator(anos) -> list[int]:
    """Anos para os quais não há IPCA — a série ali é NOMINAL, não real."""
    indice = _ipca_indice_medio()
    return sorted({int(a) for a in anos if int(a) not in indice})


def _ipca_anual() -> dict[int, float]:
    """% anual do IPCA (dez/dez). Mantido para exibição, não para deflação."""
    def buscar():
        url = ("https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados"
               "?formato=json&dataInicial=01/01/2009")
        data = get_json(url)
        if not isinstance(data, list) or not data:
            return None
        df = pd.DataFrame(data)
        df["ano"] = df["data"].str[-4:].astype(int)
        df["v"] = df["valor"].astype(str).str.replace(",", ".").astype(float)
        # composição das variações mensais dentro de cada ano
        return {int(a): float((np.prod(1 + g["v"] / 100) - 1) * 100)
                for a, g in df.groupby("ano")}
    try:
        vindo = memo("ipca_anual", buscar)
    except Exception as e:                          # pragma: no cover
        log.warning("IPCA: falha ao consultar BCB (%s) — usando fallback", e)
        vindo = None
    if not vindo:
        log.info("IPCA: usando tabela embutida (2013–2024)")
        return dict(IPCA_FALLBACK)
    return {int(k): float(v) for k, v in vindo.items()}


def fatores_ipca(base_ano: int) -> dict[int, float]:
    """Fator multiplicativo que traz cada ano a preços médios de `base_ano`.

    Razão entre índices MÉDIOS anuais — o critério correto para deflacionar
    fluxo de despesa (ver `_ipca_indice_medio`).
    """
    indice = _ipca_indice_medio()
    if not indice:
        return {}
    base = indice.get(base_ano)
    if base is None:                       # ano-base sem IPCA: usa o último
        base = indice[max(indice)]
    return {a: (base / v if v else 1.0) for a, v in indice.items()}


# ------------------------------------------------------------------ forense
def benford_primeiro_digito(valores: pd.Series) -> dict:
    """Conformidade com a Lei de Benford (1º dígito).

    Usa o MAD de Nigrini: <0,006 conforme; 0,006–0,012 aceitável;
    0,012–0,015 marginal; >0,015 não conforme.
    """
    v = pd.to_numeric(valores, errors="coerce").dropna().abs()
    v = v[v >= 10]                                  # valores pequenos distorcem
    if len(v) < 300:
        return {"n": int(len(v)), "aplicavel": False,
                "motivo": "amostra menor que 300 registros"}
    d1 = v.map(lambda x: int(str(int(x))[0]))
    obs = d1.value_counts(normalize=True).reindex(range(1, 10), fill_value=0.0)
    esp = pd.Series({d: math.log10(1 + 1 / d) for d in range(1, 10)})
    mad = float((obs - esp).abs().mean())
    qui = float(((obs - esp) ** 2 / esp).sum() * len(v))
    if mad < 0.006:
        conform = "conforme"
    elif mad < 0.012:
        conform = "aceitavel"
    elif mad < 0.015:
        conform = "marginal"
    else:
        conform = "nao_conforme"
    return {
        "n": int(len(v)), "aplicavel": True, "mad": round(mad, 5),
        "qui_quadrado": round(qui, 2), "conformidade": conform,
        "observado": {int(d): round(float(obs[d]), 4) for d in range(1, 10)},
        "esperado": {int(d): round(float(esp[d]), 4) for d in range(1, 10)},
        "maior_desvio_digito": int((obs - esp).abs().idxmax()),
    }


def benford_dois_digitos(valores: pd.Series) -> dict:
    """Conformidade com Benford nos DOIS primeiros dígitos (10–99).

    Muito mais sensível que o teste de 1 dígito: 90 categorias em vez de 9,
    e é onde aparecem as assinaturas de valor "arredondado para caber" —
    concentração em 10, 15, 20, 25, 50. Os limiares de Nigrini são PRÓPRIOS
    deste teste (0,0012 / 0,0018 / 0,0022); reaproveitar os de 1 dígito
    reprovaria qualquer base.
    """
    v = pd.to_numeric(valores, errors="coerce").dropna().abs()
    v = v[v >= 10]
    if len(v) < 1000:
        return {"n": int(len(v)), "aplicavel": False,
                "motivo": "amostra menor que 1.000 registros"}
    d2 = v.map(lambda x: int(str(int(x))[:2]))
    obs = d2.value_counts(normalize=True).reindex(range(10, 100), fill_value=0.0)
    esp = pd.Series({d: math.log10(1 + 1 / d) for d in range(10, 100)})
    mad = float((obs - esp).abs().mean())
    if mad < 0.0012:
        conform = "conforme"
    elif mad < 0.0018:
        conform = "aceitavel"
    elif mad < 0.0022:
        conform = "marginal"
    else:
        conform = "nao_conforme"
    desvio = (obs - esp).sort_values(ascending=False)
    return {
        "n": int(len(v)), "aplicavel": True, "mad": round(mad, 6),
        "conformidade": conform,
        "observado": {int(d): round(float(obs[d]), 5) for d in obs.index},
        "esperado": {int(d): round(float(esp[d]), 5) for d in esp.index},
        "maiores_excessos": {int(d): round(float(desvio[d]), 5)
                             for d in desvio.head(8).index},
    }


def bunching(valores: pd.Series, limite: float, faixa: float = 0.10) -> dict:
    """Concentração de valores logo ABAIXO de um limite legal (ex.: dispensa).

    Compara a densidade na faixa [limite*(1-faixa), limite) com a faixa
    simétrica acima. Razão >> 1 indica represamento artificial.
    """
    v = pd.to_numeric(valores, errors="coerce").dropna()
    baixo = v[(v >= limite * (1 - faixa)) & (v < limite)]
    alto = v[(v >= limite) & (v < limite * (1 + faixa))]
    n_alto = max(len(alto), 1)
    return {
        "limite": limite, "n_abaixo": int(len(baixo)), "n_acima": int(len(alto)),
        "razao": round(len(baixo) / n_alto, 2),
        "valor_abaixo": round(float(baixo.sum()), 2),
    }


def zscore_robusto(serie: pd.Series) -> pd.Series:
    """Z-score por mediana/MAD — resistente a outliers (que é o que buscamos)."""
    s = pd.to_numeric(serie, errors="coerce")
    med = s.median()
    mad = (s - med).abs().median()
    if not mad or math.isnan(mad):
        return pd.Series(0.0, index=s.index)
    return 0.6745 * (s - med) / mad


def concentracao_dezembro(df: pd.DataFrame, data_col: str, valor_col: str,
                          ano_col: str = "_ano") -> pd.DataFrame:
    """Share de dezembro no total do ano — empenho de fim de exercício."""
    if df is None or df.empty or data_col not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=[data_col]).copy()
    d["_mes"] = pd.to_datetime(d[data_col], errors="coerce").dt.month
    d = d.dropna(subset=["_mes"])
    tot = d.groupby(ano_col)[valor_col].sum()
    dez = d[d["_mes"] == 12].groupby(ano_col)[valor_col].sum()
    out = pd.DataFrame({"total_ano": tot, "dezembro": dez}).fillna(0)
    out["share_dezembro"] = (out["dezembro"] / out["total_ano"].replace(0, np.nan)
                             ).fillna(0).round(4)
    return out.reset_index()
