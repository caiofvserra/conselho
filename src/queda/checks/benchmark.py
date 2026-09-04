"""Frente — Comparação entre Creas.

A mesma API Implanta serve os 27 conselhos regionais e o Confea. Um número do
Crea-SP sozinho ("gastou X em diárias") não diz se X é muito: a comparação com
os pares, pelo MESMO critério e pela MESMA fonte, diz. O denominador é o número
de profissionais ativos de cada Crea (Confea/SIC), porque conselhos servem
profissionais — despesa por profissional é a régua que faz sentido.

Opt-in: são ~28 hosts × exercícios de requisições. Desligada, a frente publica
um único achado dizendo como ligar — e nunca um ranking com um só participante.
"""
from __future__ import annotations

import pandas as pd

from .. import config
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl
from .common import df

FRENTE = "Comparação entre Creas"
MINIMO_PARES = 5          # abaixo disso um ranking é anedota


def _valor(d: pd.DataFrame) -> pd.Series:
    for c in ("VALOR_PAGO", "VALOR_LIQUIDADO", "VALOR_EMPENHADO", "REALIZADO_EXERCICIO",
              "VALOR"):
        if c in d.columns:
            v = pd.to_numeric(d[c], errors="coerce")
            if v.notna().any():
                return v.fillna(0.0)
    return pd.Series(0.0, index=d.index)


def _rotulo(d: pd.DataFrame) -> pd.Series:
    for c in ("NOME_CONTA", "DESCRICAO", "CONTA", "NOME", "RUBRICA", "NATUREZA"):
        if c in d.columns:
            return d[c].fillna("").astype(str)
    return pd.Series("", index=d.index)


def run(staged) -> list[Finding]:
    bench = df(staged, "benchmark_creas")
    if bench.empty or "_uf" not in bench.columns:
        return [Finding(
            id="benchmark-indisponivel", frente=FRENTE, gravidade="info",
            titulo="Comparação entre Creas desligada nesta rodada",
            descricao=(
                "Ligar com `QUEDA_COMPARAR_CREAS=1` (ou `--comparar-creas`). A "
                "coleta consulta o ComparativoDespesa dos últimos "
                f"{config.BENCHMARK_EXERCICIOS} exercícios encerrados em cada "
                "host `crea-XX.implanta.net.br` que responder — os hosts são "
                "candidatos pelo padrão de nome; `python main.py sondar` mostra "
                "quais existem. O denominador (profissionais ativos por Crea) "
                f"vem do {config.PROFISSIONAIS_ATIVOS_FONTE['fonte']}."),
        )]

    ufs = sorted(bench["_uf"].dropna().unique())
    d = bench.copy()
    d["_v"] = _valor(d)
    d["_cat"] = _rotulo(d).map(T.classificar_texto)
    d["_macro"] = T.macro(d["_cat"])
    anos = sorted(pd.to_numeric(d.get("_ano"), errors="coerce").dropna().unique())
    achados: list[Finding] = [Finding(
        id="benchmark-cobertura", frente=FRENTE, gravidade="info",
        titulo=(f"{len(ufs)} de {len(config.CREAS_IMPLANTA)} conselhos responderam "
                f"({', '.join(ufs)}) — exercícios {int(min(anos)) if anos else '?'}–"
                f"{int(max(anos)) if anos else '?'}"),
        descricao=(
            "Quem não respondeu não está no ranking, e o ranking é sobre quem "
            "respondeu — não sobre o sistema inteiro. Mesma API, mesmo endpoint "
            "(ComparativoDespesa), mesma taxonomia aplicada ao nome da conta. "
            "Diferenças de plano de contas entre conselhos aparecem como "
            "diferença de classificação; a coluna `nao_classificado_pct` mede "
            "quanto de cada um a taxonomia não reconheceu."),
        metricas={"ufs": ufs, "exercicios": [int(a) for a in anos]},
    )]
    if len(ufs) < MINIMO_PARES:
        achados[0].descricao += (f"\n\n**Menos de {MINIMO_PARES} conselhos "
                                 "responderam: nenhum ranking é publicado.**")
        return achados

    tot = d.groupby("_uf")["_v"].sum()
    # Pela CATEGORIA, não pelo macro: o macro "Pessoal e verbas" abriga também
    # a folha (REMUNERACAO), e medir "verbas" com ele somaria salário a diária.
    # `VERBAS_PESSOAIS` é a categoria da taxonomia para diárias, jetons,
    # indenizações e auxílios; `OUTROS` é o que ela não reconheceu.
    verbas = d[d["_cat"].eq("VERBAS_PESSOAIS")].groupby("_uf")["_v"].sum()
    nao_class = d[d["_cat"].eq("OUTROS")].groupby("_uf")["_v"].sum()
    n_anos = max(len(anos), 1)
    tabela = pd.DataFrame({
        "uf": tot.index,
        "despesa_media_anual": (tot / n_anos).round(2).values,
        "profissionais_ativos": [config.PROFISSIONAIS_ATIVOS_POR_CREA.get(u) for u in tot.index],
    })
    tabela["despesa_por_profissional_ano"] = (
        tabela["despesa_media_anual"] / tabela["profissionais_ativos"]).round(2)
    tabela["verbas_pct"] = (100 * verbas.reindex(tot.index).fillna(0) / tot.replace(0, pd.NA)).astype(float).round(1).values
    tabela["nao_classificado_pct"] = (100 * nao_class.reindex(tot.index).fillna(0) / tot.replace(0, pd.NA)).astype(float).round(1).values
    tabela = tabela.dropna(subset=["profissionais_ativos"]).sort_values(
        "despesa_por_profissional_ano", ascending=False).reset_index(drop=True)
    tabela["posicao"] = tabela.index + 1
    sp = tabela[tabela["uf"] == "SP"]
    if not sp.empty:
        pos = int(sp["posicao"].iloc[0]); n = len(tabela)
        mediana = float(tabela["despesa_por_profissional_ano"].median())
        valor_sp = float(sp["despesa_por_profissional_ano"].iloc[0])
        achados.append(Finding(
            id="benchmark-despesa-por-profissional", frente=FRENTE,
            gravidade="media" if pos <= max(1, n // 4) else "info",
            titulo=(f"Crea-SP: {fmt_brl(valor_sp)} de despesa por profissional ativo "
                    f"ao ano — {pos}º de {n} conselhos (mediana {fmt_brl(mediana)})"),
            descricao=(
                "Despesa total média anual (ComparativoDespesa) dividida pelos "
                "profissionais ativos do Crea, para cada conselho que respondeu. "
                "É uma régua de escala, não de eficiência: conselhos pequenos têm "
                "custo fixo por profissional maior por natureza, e São Paulo é o "
                "maior de todos — a expectativa é que fique ABAIXO da mediana. "
                "Ficar acima é o que pede explicação.\n\n"
                f"Denominador: {config.PROFISSIONAIS_ATIVOS_FONTE['fonte']} "
                f"({config.PROFISSIONAIS_ATIVOS_FONTE['url']})."),
            evidencia=tabela[["posicao", "uf", "despesa_media_anual", "profissionais_ativos",
                              "despesa_por_profissional_ano", "nao_classificado_pct"]],
            metricas={"posicao": pos, "conselhos": n, "valor_sp": round(valor_sp, 2),
                      "mediana": round(mediana, 2)},
        ))
        verb = tabela.sort_values("verbas_pct", ascending=False).reset_index(drop=True)
        verb["posicao"] = verb.index + 1
        vsp = verb[verb["uf"] == "SP"]
        if not vsp.empty and float(verb["verbas_pct"].sum()) > 0:
            posv = int(vsp["posicao"].iloc[0])
            achados.append(Finding(
                id="benchmark-verbas-share", frente=FRENTE,
                gravidade="media" if posv <= max(1, n // 4) else "info",
                titulo=(f"Diárias, jetons e indenizações: {float(vsp['verbas_pct'].iloc[0]):.1f}% "
                        f"da despesa do Crea-SP — {posv}º de {n} (mediana "
                        f"{float(verb['verbas_pct'].median()):.1f}%)"),
                descricao=(
                    "Parcela da despesa classificada pela taxonomia do projeto como "
                    "verba a pessoas (diárias, jetons, indenizações, auxílios), "
                    "aplicada ao nome da conta de cada conselho. Como os planos de "
                    "contas variam, `nao_classificado_pct` diz quanto de cada "
                    "conselho a régua não alcançou — um conselho com 40% não "
                    "classificado não é comparável e deve ser lido com essa "
                    "ressalva."),
                evidencia=verb[["posicao", "uf", "verbas_pct", "despesa_media_anual",
                                "nao_classificado_pct"]],
                metricas={"posicao": posv, "conselhos": n,
                          "verbas_pct_sp": round(float(vsp["verbas_pct"].iloc[0]), 1)},
            ))
    return achados
