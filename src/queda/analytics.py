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
    # Somar líquido e contar bruto engana: um fornecedor com 10 pagamentos e
    # 4 estornos aparecia com "14 operações" e o total já compensado. As duas
    # coisas passam a ser visíveis lado a lado.
    d["_estorno"] = pd.to_numeric(d[valor], errors="coerce").fillna(0) < 0
    g = (d.groupby([ano, entidade])
         .agg(total=(valor, "sum"),
              operacoes=(valor, "size"),
              estornos=("_estorno", "sum"),
              valor_estornado=(valor, lambda s: float(s[s < 0].sum())))
         .reset_index())
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
# Benford descreve grandezas que variam por ordens de magnitude — preço de
# mercado, receita, população. Ela NÃO descreve tarifa: uma diária de valor
# fixo repetida 117 mil vezes produz sempre os mesmos dígitos, e o teste
# reprova a base inteira sem que exista qualquer manipulação. Na rodada real,
# 75,6% dos lançamentos eram diária, locomoção e jeton, e o par "64" sozinho
# respondia por 15,75% contra 0,673% esperados.
# Dois critérios, porque um só erra: em Benford o par mais frequente ("10")
# já vale 4,1% legitimamente, então um corte absoluto sozinho reprovaria base
# conforme. Exigimos share alto E muito acima do esperado para aquele par.
LIMITE_DOMINANCIA = 0.05
LIMITE_EXCESSO = 3.0


def _valor_dominante(v: pd.Series) -> dict | None:
    """Detecta base dominada por valor tabelado, onde Benford não se aplica."""
    if v.empty:
        return None
    d2 = v.map(lambda x: str(int(x))[:2])
    top = d2.value_counts(normalize=True)
    par = str(top.index[0])
    share = float(top.iloc[0])
    try:
        esperado = math.log10(1 + 1 / int(par))
    except (ValueError, ZeroDivisionError):
        return None
    if share <= LIMITE_DOMINANCIA or share < LIMITE_EXCESSO * esperado:
        return None
    frequentes = v.value_counts().head(3)
    return {
        "motivo": (f"base dominada por valor tabelado: os dígitos '{par}' "
                   f"aparecem em {100 * share:.1f}% dos "
                   "lançamentos (esperado por Benford: "
                   f"{100 * esperado:.2f}%, ou seja {share / esperado:.0f}x "
                   "mais). Benford "
                   "descreve preço que varia por ordens de magnitude, não "
                   "tarifa repetida — o teste não se aplica a este universo."),
        "digitos_dominantes": par,
        "share_dominante": round(share, 4),
        "excesso_sobre_esperado": round(share / esperado, 1),
        "valores_mais_repetidos": {str(k): int(n) for k, n in frequentes.items()},
    }


def benford_primeiro_digito(valores: pd.Series) -> dict:
    """Conformidade com a Lei de Benford (1º dígito).

    Usa o MAD de Nigrini: <0,006 conforme; 0,006–0,012 aceitável;
    0,012–0,015 marginal; >0,015 não conforme.
    """
    # estorno não é pagamento: `.abs()` transformava devolução em despesa e
    # duplicava o dígito. Negativos saem.
    v = pd.to_numeric(valores, errors="coerce").dropna()
    v = v[v >= 10]                                  # valores pequenos distorcem
    if len(v) < 300:
        return {"n": int(len(v)), "aplicavel": False,
                "motivo": "amostra menor que 300 registros"}
    dominante = _valor_dominante(v)
    if dominante:
        return {"n": int(len(v)), "aplicavel": False, **dominante}
    d1 = v.map(lambda x: int(str(int(x))[0]))
    obs = d1.value_counts(normalize=True).reindex(range(1, 10), fill_value=0.0)
    esp = pd.Series({d: math.log10(1 + 1 / d) for d in range(1, 10)})
    mad = float((obs - esp).abs().mean())
    # O qui-quadrado cresce LINEARMENTE com n: a n=493 mil ele reprova
    # qualquer base real, e o número não informa nada. Cramér's V é o mesmo
    # desvio normalizado pelo tamanho da amostra, comparável entre rodadas.
    qui = float(((obs - esp) ** 2 / esp).sum())
    cramer = float(math.sqrt(qui / 8))       # 8 = graus de liberdade (9-1)
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
        "cramer_v": round(cramer, 4), "conformidade": conform,
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
    v = pd.to_numeric(valores, errors="coerce").dropna()
    v = v[v >= 10]
    if len(v) < 1000:
        return {"n": int(len(v)), "aplicavel": False,
                "motivo": "amostra menor que 1.000 registros"}
    dominante = _valor_dominante(v)
    if dominante:
        return {"n": int(len(v)), "aplicavel": False, **dominante}
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
    out = {
        "limite": limite, "n_abaixo": int(len(baixo)), "n_acima": int(len(alto)),
        "razao": round(len(baixo) / n_alto, 2),
        "valor_abaixo": round(float(baixo.sum()), 2),
        "valores_distintos_abaixo": int(baixo.nunique()),
        "conclusivo": True,
    }
    # Degenerescência: uma parcela mensal de contrato repetida cai sempre na
    # mesma faixa e simula represamento. No Crea-SP era a parcela de convênio
    # de R$ 17.459,54 (R$ 209.514,48 ÷ 12) produzindo "6,9x" em 2019 — que
    # sumia sozinho quando o teto mudou para R$ 50.000 em 2021.
    if len(baixo) >= 10:
        dominante = baixo.value_counts(normalize=True)
        if float(dominante.iloc[0]) > 0.20:
            out["conclusivo"] = False
            out["valor_repetido"] = float(dominante.index[0])
            out["share_valor_repetido"] = round(float(dominante.iloc[0]), 3)
    return out


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
