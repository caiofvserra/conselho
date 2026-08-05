"""Frente — O ritmo mensal do gasto: o que o total do exercício esconde.

Todas as demais frentes deste projeto medem o ano fechado. Mas `despesas_centro_custo`
publica **VALOR_JAN..VALOR_DEZ** — doze colunas por centro de custo e conta — e
esse é o único eixo mensal que sobreviveu à coleta. Ele responde a perguntas
que o total anual apaga:

* **Quando** o dinheiro saiu. Um mês vale 8,33% do ano. Dezembro muito acima
  disso é a assinatura clássica da corrida de fim de exercício: empenhar às
  pressas para não devolver dotação, com menos tempo de análise e mais risco de
  contratação apressada (Lei 4.320/1964, arts. 35 e 38).
* **Se a verba a pessoas acompanha a atividade.** Diária e locomoção legítimas
  seguem o deslocamento e se espalham pelo calendário. Concentradas em dois ou
  três meses, sugerem lote — acerto retroativo, pagamento em bloco — e não
  viagem.
* **Qual unidade tem perfil de gasto atípico.** A dispersão mensal, medida e
  ranqueada, é um filtro de onde olhar.

Três limites honestos, ditos aqui para não precisarem ser repetidos em cada
achado:

1. **Base diferente da forense.** Já existe `forense-dezembro`, que mede a
   concentração dos **pagamentos** (regime de caixa, data de pagamento). Esta
   frente mede a **despesa lançada por centro de custo** (competência contábil,
   por unidade). São universos distintos, com totais distintos: os dois números
   não se somam, não se conferem entre si e não devem ser lidos como
   confirmação um do outro.
2. **Concentração não é irregularidade.** Décimo terceiro cai em dezembro,
   contrato anual é liquidado quando a medição fecha, campanha de fiscalização
   tem calendário próprio. O que a frente entrega é o ranking do que precisa de
   explicação, nunca a explicação.
3. **As doze colunas podem não fechar com VALOR_TOTAL.** Quando não fecham, a
   divergência é publicada como achado próprio em vez de escolhermos
   silenciosamente um dos dois lados.
"""
from __future__ import annotations

import re
import unicodedata

import pandas as pd

from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl
from .common import df

FRENTE = "Sazonalidade mensal da despesa"

# Sufixos das colunas mensais, na ordem do calendário. A posição na tupla é o
# número do mês — é o que traduz "VALOR_DEZ" em 12 sem tabela auxiliar.
MESES = ("JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
         "JUL", "AGO", "SET", "OUT", "NOV", "DEZ")
NUMERO_MES = {m: i + 1 for i, m in enumerate(MESES)}
NOME_MES = {1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
            5: "maio", 6: "junho", 7: "julho", 8: "agosto",
            9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro"}

# Único parâmetro de referência honesto desta frente: não existe patamar legal
# de gasto mensal, mas existe a divisão aritmética do ano em doze.
SHARE_UNIFORME = 1 / 12

# --- limiares próprios desta frente ------------------------------------------
# Deveriam migrar para config.py junto com os demais thresholds do projeto;
# ficam aqui porque config.py é editado por outra frente em paralelo.
PISO_UNIDADE = 100_000.0        # abaixo disso o % mensal é ruído de arredondamento
PISO_CONTA = 100_000.0
TOL_FECHAMENTO_PCT = 0.5        # divergência meses × VALOR_TOTAL tolerada, em %
SHARE_DEZEMBRO_ALTO = 0.25      # 3x o mês uniforme
MESES_EFETIVOS_BAIXO = 6.0      # metade do calendário

# A mesma unidade aparece duas vezes no portal, uma com o prefixo "Não
# utilizar - ", e sem fundir as duas o perfil mensal sai partido ao meio. A
# regra já existe na frente de superintendências e é reaproveitada — o import
# é tolerante de propósito: esta frente não pode deixar de rodar porque a
# vizinha mudou de nome de função.
try:                                        # pragma: no cover - caminho feliz
    from .superintendencias import normalizar_unidade as _canonico
except Exception:                           # pragma: no cover
    _canonico = None

_RX_DESATIVADO = re.compile(r"^\s*n[ãa]o\s+utilizar\s*[-–]\s*", re.IGNORECASE)


def _rotulo_ultimo_mes(m) -> str:
    """Nome do último mês com lançamento de um exercício.

    `max(..., default=0)` devolve 0 quando o exercício INTEIRO veio zerado — o
    portal publica VALOR_TOTAL de exercícios antigos sem abertura mensal, e a
    linha existe com as doze colunas em zero. `NOME_MES[0]` não existe: o
    KeyError derrubava a frente toda (virava `erro-check-sazonalidade`)
    justamente no caso que a regra de calendário incompleto deveria descartar
    em silêncio.
    """
    return NOME_MES.get(int(m), "nenhum mês com lançamento")


def _ate_quando(m) -> str:
    return (f"vai até {NOME_MES[int(m)]}" if int(m) in NOME_MES
            else "sem nenhum mês lançado")


def _unidade_canonica(nome) -> str:
    if _canonico is not None:
        return _canonico(nome)
    n = unicodedata.normalize("NFD", str(nome or ""))
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    n = _RX_DESATIVADO.sub("", n)
    n = re.sub(r"\s*-\s*", " - ", n)
    return re.sub(r"\s+", " ", n).strip().upper()


# --------------------------------------------------------------- base mensal
def _mensal(staged) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(despesa em formato longo, fechamento por exercício, diagnóstico).

    O formato longo é `_ano, unidade, conta, natureza, mes, valor` — uma linha
    por mês. O fechamento guarda, por exercício, a soma das doze colunas contra
    o VALOR_TOTAL declarado; é a matéria-prima do achado de consistência.
    """
    cc = df(staged, "despesas_centro_custo")
    diag: dict = {"linhas_fonte": int(len(cc))}
    vazio = (pd.DataFrame(), pd.DataFrame(), diag)

    if cc.empty:
        diag["motivo"] = ("o dataset `despesas_centro_custo` não veio na coleta "
                          "ou veio sem nenhuma linha")
        return vazio

    ausentes = [m for m in MESES if f"VALOR_{m}" not in cc.columns]
    diag["meses_ausentes"] = ausentes
    if ausentes:
        # Exigimos as DOZE. Com um subconjunto, o denominador de qualquer
        # percentual mensal é um ano incompleto e todo share sai inflado — o
        # erro seria invisível no relatório e grande no número.
        diag["motivo"] = (
            "a fonte trouxe o gasto por centro de custo, mas sem as doze "
            "colunas mensais: faltam "
            + ", ".join(f"VALOR_{m}" for m in ausentes)
            + ". Com um subconjunto de meses o denominador de qualquer "
              "percentual mensal seria um ano incompleto, e toda concentração "
              "sairia superestimada.")
        return vazio

    d = cc.copy()
    # ÁRVORE CONTÁBIL: a conta sintética repete a soma das filhas. Sem este
    # filtro o mesmo real entra três ou quatro vezes e a concentração mensal
    # sai multiplicada — com os percentuais intactos, o que torna o erro
    # indetectável na leitura.
    if "ANALITICO" in d:
        d = d[d["ANALITICO"].astype(bool)]
    if d.empty:
        diag["motivo"] = ("nenhuma linha analítica sobrou após descartar os "
                          "totalizadores da árvore contábil")
        return vazio

    colunas = [f"VALOR_{m}" for m in MESES]
    for c in colunas:
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0.0)
    soma_meses = float(d[colunas].to_numpy().sum())
    diag["soma_das_colunas_mensais"] = round(soma_meses, 2)
    if soma_meses == 0:
        diag["motivo"] = ("as doze colunas mensais existem na fonte mas vieram "
                          "todas zeradas — não há eixo mensal a medir, ainda "
                          "que o total do exercício esteja preenchido")
        return vazio

    d["_ano"] = pd.to_numeric(coluna(d, "_ano"), errors="coerce")
    d = d.dropna(subset=["_ano"])
    if d.empty:
        diag["motivo"] = "nenhuma linha da fonte tem exercício identificável"
        return vazio
    d["_ano"] = d["_ano"].astype(int)

    d["unidade"] = (coluna(d, "NOME_CENTRO_CUSTO").fillna("").astype(str)
                    .map(_unidade_canonica))
    d["conta"] = (coluna(d, "NOME_CONTA").fillna("").astype(str)
                  .str.strip())
    # NATUREZA vem do nome da conta e, só como reforço, do centro de custo —
    # a mesma ordem usada pelas outras frentes. Passa por T.normalizar dentro
    # da taxonomia: aplicar regex direto em "Diárias no País" ou "Férias"
    # nunca casaria (é o bug que já fabricou um excedente de R$ 1,5 bilhão).
    d["natureza"] = T.classificar_series(d["conta"], d["unidade"])

    if "VALOR_TOTAL" in d.columns:
        d["_total_declarado"] = pd.to_numeric(
            d["VALOR_TOTAL"], errors="coerce").fillna(0.0)
        d["_soma_meses"] = d[colunas].sum(axis=1)
        fechamento = (d.groupby("_ano")
                      .agg(soma_dos_meses=("_soma_meses", "sum"),
                           valor_total_declarado=("_total_declarado", "sum")))
        fechamento["divergencia"] = (fechamento["soma_dos_meses"]
                                     - fechamento["valor_total_declarado"])
        fechamento["divergencia_pct"] = pd.to_numeric(
            100 * fechamento["divergencia"]
            / fechamento["valor_total_declarado"].replace(0, pd.NA),
            errors="coerce").round(2)
    else:
        diag["sem_valor_total"] = True
        fechamento = pd.DataFrame()

    longo = (d[["_ano", "unidade", "conta", "natureza"] + colunas]
             .melt(id_vars=["_ano", "unidade", "conta", "natureza"],
                   var_name="_coluna", value_name="valor"))
    longo["mes"] = longo["_coluna"].str[-3:].map(NUMERO_MES)
    longo["mes_nome"] = longo["mes"].map(NOME_MES)
    return longo.drop(columns=["_coluna"]), fechamento, diag


# ----------------------------------------------------------------- dispersão
def _matriz(longo: pd.DataFrame, chave: str) -> pd.DataFrame:
    """Matriz chave × mês em R$, com os doze meses sempre presentes."""
    m = longo.pivot_table(index=chave, columns="mes", values="valor",
                          aggfunc="sum", fill_value=0.0)
    return m.reindex(columns=range(1, 13), fill_value=0.0)


def _perfil(matriz: pd.DataFrame) -> pd.DataFrame:
    """Resumo do perfil mensal de cada linha da matriz.

    A medida de dispersão é o **inverso do índice Herfindahl-Hirschman** das
    doze participações mensais: `1 / Σ(share_mês²)`. Ela devolve o número
    EQUIVALENTE de meses — 12,0 para quem gastou um doze avos por mês, 1,0 para
    quem gastou tudo num mês só.

    Escolhida em vez de Gini e de coeficiente de variação por três razões:

    * **Leitura direta.** "Esta unidade gastou como se o ano tivesse 3,2 meses"
      é uma frase que um leitor não técnico confere sozinho na tabela mensal.
      Um Gini de 0,73 não é.
    * **Invariância de escala.** Depende só das participações, então uma UGI de
      R$ 900 mil e a superintendência de R$ 300 milhões entram no mesmo
      ranking. O coeficiente de variação também seria invariante, mas devolve
      um número sem unidade e explode quando a média mensal é próxima de zero.
    * **Sem ordenação nem interpolação.** Gini sobre 12 pontos depende de como
      se interpola a curva de Lorenz, e implementações diferentes dão números
      diferentes para os mesmos dados — ruim para um relatório que quer ser
      reconferido por terceiros.

    O limite: o HHI não diz QUAL mês concentra. Uma unidade que empurra tudo
    para março é tão "atípica" quanto uma que empurra para dezembro, e só a
    primeira não tem nada a ver com corrida orçamentária. Por isso as colunas
    `mes_pico` e `share_dezembro_pct` viajam junto na mesma tabela.

    Linhas com qualquer mês NEGATIVO no agregado (estorno maior que o gasto do
    mês) saem com `meses_efetivos` vazio: participação negativa quebra o
    índice, e inventar um valor ali seria pior que admitir a lacuna.
    """
    if matriz.empty:
        return pd.DataFrame(columns=[
            matriz.index.name or "chave", "total", "dezembro",
            "share_dezembro_pct", "mes_pico", "share_pico_pct",
            "meses_efetivos", "share_3_maiores_meses_pct"])
    total = matriz.sum(axis=1)
    positivo = total.replace(0, pd.NA)
    shares = matriz.div(positivo, axis=0)
    hhi = (shares ** 2).sum(axis=1)
    meses_efetivos = 1 / hhi.replace(0, pd.NA)
    invalida = (matriz < 0).any(axis=1) | (total <= 0)
    meses_efetivos = meses_efetivos.mask(invalida)
    top3 = shares.apply(lambda r: r.nlargest(3).sum(), axis=1).mask(invalida)

    out = pd.DataFrame({
        "total": total.round(2),
        "dezembro": matriz[12].round(2),
        "share_dezembro_pct": (100 * matriz[12] / positivo).round(1),
        "mes_pico": matriz.idxmax(axis=1).map(NOME_MES),
        "share_pico_pct": (100 * matriz.max(axis=1) / positivo).round(1),
        "meses_efetivos": meses_efetivos.round(2),
        "share_3_maiores_meses_pct": (100 * top3).round(1),
    })
    out.index.name = matriz.index.name
    return out.reset_index()


# --------------------------------------------------------------------- run
def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    longo, fechamento, diag = _mensal(staged)

    if longo.empty:
        # Regra do projeto: fonte ausente vira achado explícito. Sem isto, a
        # ausência do eixo mensal seria lida como "não há sazonalidade".
        return [Finding(
            id="sazonalidade-indisponivel", frente=FRENTE, gravidade="info",
            titulo="Análise mensal indisponível: as doze colunas de "
                   "VALOR_JAN a VALOR_DEZ não puderam ser usadas",
            descricao=(
                "O portal publica a despesa por centro de custo com uma coluna "
                "por mês, e esse é o único eixo mensal disponível em toda a "
                "coleta. Nesta rodada ele não pôde ser lido: "
                + diag.get("motivo", "motivo não determinado") + ".\n\n"
                "Nada foi estimado no lugar. **Não interprete a ausência desta "
                "frente como ausência de sazonalidade**: concentração em "
                "dezembro, verba paga em lote e unidade com perfil atípico "
                "seguem possíveis e simplesmente não foram medidas."),
            metricas=diag,
            como_verificar="Pedir ao Crea-SP o relatório de despesa por centro "
                           "de custo com abertura mensal (as colunas de janeiro "
                           "a dezembro do próprio portal), por exercício.",
        )]

    # ------------------------------------------------ 0. os meses fecham?
    #
    # As duas grandezas vêm do MESMO endpoint e deveriam ser a mesma coisa.
    # Quando não são, publicamos a diferença: escolher um dos lados em
    # silêncio esconderia um erro da fonte dentro de um número nosso.
    divergencia_global_pct = 0.0
    if not fechamento.empty:
        soma = float(fechamento["soma_dos_meses"].sum())
        declarado = float(fechamento["valor_total_declarado"].sum())
        # `declarado == 0` com meses preenchidos é divergência TOTAL, não
        # ausência de divergência: sem este ramo o percentual ficava em 0,0 e o
        # achado nunca saía — justamente o silêncio que a seção promete não
        # produzir. Sem denominador não há percentual, então o corte usa o
        # valor absoluto e o título passa a falar em R$.
        sem_denominador = declarado == 0 and soma != 0
        if declarado:
            divergencia_global_pct = 100 * (soma - declarado) / declarado
        if sem_denominador or abs(divergencia_global_pct) > TOL_FECHAMENTO_PCT:
            piores = fechamento.reindex(
                fechamento["divergencia_pct"].abs().sort_values(
                    ascending=False).index)
            achados.append(Finding(
                id="sazonalidade-meses-nao-fecham", frente=FRENTE,
                gravidade=("alta" if sem_denominador
                           or abs(divergencia_global_pct) > 5 else "media"),
                titulo=(f"A soma das doze colunas mensais é de {fmt_brl(soma)} "
                        "e o total do exercício vem zerado na fonte"
                        if sem_denominador else
                        f"A soma das doze colunas mensais difere do total do "
                        f"exercício em {divergencia_global_pct:+.1f}% "
                        f"({fmt_brl(soma - declarado)})"),
                descricao=(
                    "O mesmo endpoint publica, para cada centro de custo e "
                    "conta, doze valores mensais **e** um VALOR_TOTAL. As duas "
                    "grandezas deveriam ser a mesma despesa vista de duas "
                    "formas, e não são: no período somam "
                    f"{fmt_brl(soma)} pelo lado mensal contra "
                    f"{fmt_brl(declarado)} pelo total declarado.\n\n"
                    "Publicamos a divergência em vez de escolher um dos dois "
                    "lados. Ela pode ser exercício em aberto (meses ainda não "
                    "lançados contra um total já consolidado), ajuste "
                    "posterior lançado só no total, ou erro de extração. Toda "
                    "conclusão desta frente é calculada pelo lado MENSAL — que "
                    "é o único que tem eixo de tempo — e portanto herda esta "
                    "diferença. A tabela mostra por exercício onde ela está."),
                evidencia=piores.round(2).reset_index(),
                serie=fechamento[["divergencia_pct"]].rename(
                    columns={"divergencia_pct":
                             "divergência dos meses sobre o total (%)"}),
                grafico="bar", unidade="%",
                tabela_anual=fechamento.round(2).reset_index(),
                base_legal="Lei 4.320/1964, art. 89 (a contabilidade evidencia "
                           "os fatos ligados à administração orçamentária); "
                           "Lei 12.527/2011, art. 5º (integridade da informação "
                           "divulgada).",
                metricas={"divergencia_pct": (None if sem_denominador else
                                              round(divergencia_global_pct, 3)),
                          "soma_dos_meses": round(soma, 2),
                          "valor_total_declarado": round(declarado, 2)},
                como_verificar="Pedir a conciliação entre o razão mensal e o "
                               "total do exercício por centro de custo nos anos "
                               "listados.",
            ))

    # --------------------------------- exercícios com o calendário incompleto
    #
    # O exercício corrente sempre tem meses futuros zerados; medir dezembro
    # nele daria 0% e puxaria a média para baixo por artefato de calendário.
    # A regra olha o ÚLTIMO mês com lançamento: zeros no meio do ano não
    # desqualificam nada, zeros no fim sim.
    por_ano = _matriz(longo, "_ano")
    ultimo_mes = por_ano.apply(
        lambda linha: max([m for m in range(1, 13) if linha[m] != 0], default=0),
        axis=1)
    completos = sorted(int(a) for a in ultimo_mes[ultimo_mes == 12].index)
    incompletos = {int(a): int(m) for a, m in ultimo_mes.items()
                   if int(m) < 12}

    if not completos:
        return achados + [Finding(
            id="sazonalidade-exercicios-incompletos", frente=FRENTE,
            gravidade="info",
            titulo=(f"Nenhum dos {len(ultimo_mes)} exercícios tem os doze meses "
                    "lançados — a concentração mensal não é comparável"),
            descricao=(
                "Em todos os exercícios coletados os últimos meses do ano estão "
                "zerados — em alguns, o ano inteiro. Isso costuma significar "
                "exercício em aberto, exercício antigo publicado só com o total, "
                "ou extração parcial, e não gasto zero. Medir o peso de dezembro "
                "num ano que termina em agosto produziria 0% por artefato de "
                "calendário — um número que parece medição e não é.\n\n"
                "A tabela mostra, por exercício, o último mês com lançamento."),
            evidencia=(pd.DataFrame({"_ano": list(incompletos),
                                     "ultimo_mes_com_lancamento":
                                         [_rotulo_ultimo_mes(m)
                                          for m in incompletos.values()]})),
            metricas={"exercicios": len(ultimo_mes)},
        )]

    d_ok = longo[longo["_ano"].isin(completos)]
    nota_incompletos = (
        f" {len(incompletos)} exercício(s) ficaram de fora por terem o "
        "calendário incompleto na fonte (últimos meses zerados), o que é "
        "exercício em aberto ou extração parcial, não gasto zero: "
        + ", ".join(f"{a} ({_ate_quando(m)})"
                    for a, m in sorted(incompletos.items())) + "."
        if incompletos else "")

    # ------------------------------------------------ 1. o peso de dezembro
    matriz_ano = _matriz(d_ok, "_ano")
    total_ano = matriz_ano.sum(axis=1)
    dez_ano = matriz_ano[12]
    share_ano = (dez_ano / total_ano.replace(0, pd.NA)).dropna()
    total_periodo = float(total_ano.sum())
    dez_periodo = float(dez_ano.sum())

    perfil_unidade = _perfil(_matriz(d_ok, "unidade"))
    perfil_unidade = perfil_unidade[perfil_unidade["total"] >= PISO_UNIDADE]
    perfil_conta = _perfil(_matriz(d_ok, "conta"))
    perfil_conta = perfil_conta[perfil_conta["total"] >= PISO_CONTA]

    if dez_periodo > 0 and total_periodo > 0:
        # DOIS NÚMEROS DIFERENTES, os dois publicados com o nome certo. O do
        # título é a MÉDIA DOS EXERCÍCIOS (cada ano pesa igual: mede se o
        # padrão se repete). O agregado é dezembro do período sobre o total do
        # período (os anos recentes, maiores, pesam mais). Antes só o primeiro
        # ia no título, ao lado dos dois valores em R$ do agregado — quem
        # dividisse os R$ da própria descrição encontrava um percentual
        # diferente do anunciado, sem nada dizendo por quê.
        share_medio = float(share_ano.mean())
        share_agregado = dez_periodo / total_periodo
        excesso = share_medio / SHARE_UNIFORME
        unid_altas = perfil_unidade[
            perfil_unidade["share_dezembro_pct"] > 100 * SHARE_DEZEMBRO_ALTO]
        achados.append(Finding(
            id="sazonalidade-dezembro", frente=FRENTE,
            gravidade=("alta" if share_medio > 0.25 else
                       "media" if share_medio > 0.12 else "info"),
            titulo=(f"Dezembro concentra {100 * share_medio:.1f}% da despesa "
                    "lançada por centro de custo, em média por exercício — "
                    f"{excesso:.1f}× o peso de um mês qualquer"),
            descricao=(
                "Um mês vale 8,3% do ano. A fatia que dezembro leva na despesa "
                "**por centro de custo** é de "
                f"{100 * share_medio:.1f}% na média dos "
                f"{len(share_ano)} exercício(s) medidos — cada ano pesando "
                "igual — e de "
                f"{100 * share_agregado:.1f}% no agregado do período, que soma "
                f"{fmt_brl(dez_periodo)} de {fmt_brl(total_periodo)} e portanto "
                "é dominado pelos exercícios mais recentes, que são os "
                "maiores. Os dois respondem a perguntas diferentes: o primeiro, "
                "se o padrão se repete todo ano; o segundo, quanto dinheiro "
                "isso representa.\n\n"
                "**Base diferente da forense.** O achado `forense-dezembro` "
                "mede a mesma corrida de fim de exercício sobre os "
                "**pagamentos** — regime de caixa, data em que o dinheiro "
                "saiu. Este mede a **despesa lançada por unidade** — "
                "competência contábil, com o centro de custo que gastou. São "
                "universos distintos e totais distintos: um número não confirma "
                "nem contradiz o outro, e somá-los contaria despesa duas "
                "vezes.\n\n"
                "Concentração em dezembro não é irregularidade por si: décimo "
                "terceiro, medição de contrato anual e acerto de exercício caem "
                "legitimamente ali. Vira pergunta quando é grande, quando "
                "cresce, e quando se concentra em unidades e contas específicas "
                + ("— que é o que a tabela abaixo ordena."
                   if not perfil_unidade.empty else
                   f"— o que aqui não foi possível ordenar: nenhum centro de "
                   f"custo alcançou o piso de {fmt_brl(PISO_UNIDADE)} no "
                   "período, abaixo do qual o percentual mensal é ruído de "
                   "arredondamento, e a tabela por unidade saiu vazia.")
                + nota_incompletos),
            evidencia=perfil_unidade.sort_values(
                "share_dezembro_pct", ascending=False).head(80),
            serie=pd.DataFrame(
                {"% da despesa lançada em dezembro": (100 * share_ano).round(1)}),
            grafico="line", unidade="%",
            tabela_anual=pd.DataFrame({
                "total do exercício": total_ano.round(2),
                "dezembro": dez_ano.round(2),
                "% em dezembro": (100 * dez_ano / total_ano.replace(0, pd.NA)
                                  ).round(1)}).reset_index(),
            base_legal="Lei 4.320/1964, arts. 35 e 38 (a despesa pertence ao "
                       "exercício em que se empenha; restos não processados são "
                       "anulados) e arts. 58 a 60 (empenho prévio).",
            metricas={
                "share_dezembro_medio": round(share_medio, 4),
                "share_dezembro_agregado": round(share_agregado, 4),
                "excesso_sobre_mes_uniforme": round(excesso, 2),
                "dezembro_total": round(dez_periodo, 2),
                "total_periodo": round(total_periodo, 2),
                "exercicios_considerados": len(completos),
                "exercicios_incompletos": len(incompletos),
                "unidades_acima_do_limiar": int(len(unid_altas)),
            },
            como_verificar="Pedir, para as unidades no topo da tabela, as datas "
                           "de empenho e os processos de contratação das "
                           "despesas lançadas em dezembro — e conferir se a "
                           "instrução do processo começou no mesmo mês.",
        ))

        # ---- quem puxa dezembro: unidades E contas, na mesma tabela
        conta_altas = perfil_conta[
            perfil_conta["share_dezembro_pct"] > 100 * SHARE_DEZEMBRO_ALTO]
        if not unid_altas.empty or not conta_altas.empty:
            def _rotular(base, eixo, chave):
                out = base.rename(columns={chave: "nome"}).copy()
                out.insert(0, "eixo", eixo)
                return out
            # `pd.concat` com um lado vazio inventa colunas e dispara aviso de
            # depreciação — os lados vazios ficam de fora da lista.
            partes = [_rotular(b, eixo, chave) for b, eixo, chave in (
                (unid_altas, "centro de custo", "unidade"),
                (conta_altas, "conta contábil", "conta")) if not b.empty]
            concentrados = pd.concat(partes, ignore_index=True).sort_values(
                "share_dezembro_pct", ascending=False)
            pior = float(concentrados["share_dezembro_pct"].max())
            # Título montado só com os eixos que têm alguma linha: "0 conta(s)"
            # seria ruído de medição num achado que de fato achou algo.
            quantos = " e ".join(
                t for t in (f"{len(unid_altas)} centro(s) de custo"
                            if not unid_altas.empty else "",
                            f"{len(conta_altas)} conta(s) contábil(eis)"
                            if not conta_altas.empty else "") if t)
            achados.append(Finding(
                id="sazonalidade-dezembro-concentrada", frente=FRENTE,
                gravidade="alta" if pior > 40 else "media",
                # O limiar vem da constante, não escrito à mão: com "25%" fixo
                # no texto, recalibrar SHARE_DEZEMBRO_ALTO (o parâmetro mais
                # provável de mudar depois da primeira rodada real) deixaria o
                # título afirmando um corte diferente do que o código aplicou.
                titulo=(f"{quantos} lançam mais de "
                        f"{100 * SHARE_DEZEMBRO_ALTO:.0f}% do ano em dezembro — "
                        f"o pico chega a {pior:.0f}%"),
                descricao=(
                    "A média do órgão dilui: o interesse está em quem a puxa. "
                    "A tabela junta os dois eixos que a fonte oferece — a "
                    "unidade que gastou e a conta contábil em que gastou — e "
                    "lista tudo o que concentra em dezembro mais de três vezes "
                    "o peso de um mês uniforme.\n\n"
                    "Os dois eixos se sobrepõem de propósito: a mesma despesa "
                    "aparece uma vez pela unidade e outra pela conta. **Não "
                    "some as linhas** — a coluna `eixo` existe para impedir "
                    "essa leitura. O que a sobreposição dá é o cruzamento: "
                    "quando uma unidade e uma conta aparecem as duas no topo, "
                    "o alvo do pedido de explicação fica nomeado dos dois "
                    "lados.\n\n"
                    "Conta de pessoal concentrada em dezembro é décimo "
                    "terceiro e se explica sozinha. Conta de serviço de "
                    "terceiro, material e obra concentrada em dezembro é o que "
                    "merece o processo à vista."),
                evidencia=concentrados.head(80),
                base_legal="Lei 4.320/1964, arts. 35, 38 e 58 a 60; Lei "
                           "14.133/2021, art. 5º (planejamento).",
                metricas={"unidades": int(len(unid_altas)),
                          "contas": int(len(conta_altas)),
                          "maior_share_pct": round(pior, 1)},
                como_verificar="Para cada linha da tabela, pedir a relação de "
                               "empenhos de dezembro com data de abertura do "
                               "processo — planejamento anual não produz "
                               "contratação decidida no último mês.",
            ))

    # ------------------------------------------------ 2. verba a pessoas
    #
    # A classificação é a da taxonomia (VERBAS_PESSOAIS): diária, jeton,
    # locomoção, quilometragem, ajuda de custo, passagem, hospedagem. Vem do
    # nome da CONTA, normalizado — nunca de regex direto sobre o texto com
    # acento, que é o bug que já deixou "Convênios RES.1032" e "Férias" fora
    # das suas categorias.
    verba = d_ok[d_ok["natureza"] == "VERBAS_PESSOAIS"]
    if not verba.empty and float(verba["valor"].sum()) > 0:
        matriz_verba_ano = _matriz(verba, "_ano")
        perfil_verba_ano = _perfil(matriz_verba_ano).set_index("_ano")
        me_ano = perfil_verba_ano["meses_efetivos"].dropna()
        matriz_verba = matriz_verba_ano.sum(axis=0)
        total_verba = float(matriz_verba.sum())
        perfil_verba_conta = _perfil(_matriz(verba, "conta")).sort_values(
            "meses_efetivos")
        # MESMA BLINDAGEM QUE `_perfil` APLICA LINHA A LINHA, aplicada aqui ao
        # agregado do período. Sem ela o índice era calculado sobre
        # participações negativas (mês em que o estorno supera o lançamento) e
        # publicava número IMPOSSÍVEL: o inverso do HHI de doze participações
        # não-negativas vive entre 1,0 e 12,0, e o cálculo desprotegido saía
        # com "0,2 meses" e "0,0 meses", com o share dos três maiores meses em
        # 499.501% — um número fabricado pela aritmética, não medido no dado.
        meses_negativos = [int(m) for m in matriz_verba.index
                           if float(matriz_verba[m]) < 0]
        if meses_negativos or total_verba <= 0:
            achados.append(Finding(
                id="sazonalidade-verbas-em-lote", frente=FRENTE,
                gravidade="info",
                titulo=(f"Verba a pessoas de {fmt_brl(total_verba)} com "
                        f"{len(meses_negativos)} mês(es) de estorno líquido — a "
                        "concentração mensal não é calculável"),
                descricao=(
                    "Diária, jeton, locomoção, quilometragem, ajuda de custo, "
                    "passagem e hospedagem lançados por centro de custo. A "
                    "medida desta frente é o número EQUIVALENTE de meses "
                    "(inverso do índice Herfindahl-Hirschman das doze "
                    "participações mensais), e ela só existe quando as doze "
                    "participações são não-negativas.\n\n"
                    "Não é o caso aqui: "
                    + (", ".join(NOME_MES[m] for m in meses_negativos)
                       + " fecharam o período com valor negativo — o estorno "
                         "lançado no mês supera a despesa do mês."
                       if meses_negativos else
                       "o total do período não é positivo.")
                    + " Participação negativa quebra o índice, e o número que "
                      "sairia dali seria produto da aritmética, não medição. "
                      "**Nada foi estimado no lugar**: a tabela por conta traz "
                      "o índice só das rubricas em que ele é calculável, e a "
                      "tabela anual mostra os doze meses em R$ para leitura "
                      "direta." + nota_incompletos),
                evidencia=perfil_verba_conta.head(60),
                tabela_anual=(matriz_verba_ano.round(2)
                              .rename(columns=NOME_MES).reset_index()),
                metricas={"total": round(total_verba, 2),
                          "meses_com_estorno_liquido": len(meses_negativos),
                          "contas": int(len(perfil_verba_conta))},
                como_verificar="Pedir a memória de cálculo dos estornos de "
                               "verba nos meses listados: o que foi devolvido, "
                               "por quem e referente a que competência.",
            ))
        else:
            shares_verba = matriz_verba / total_verba
            me_periodo = float(1 / (shares_verba ** 2).sum())
            top3 = float(shares_verba.nlargest(3).sum())
            picos = ", ".join(f"{NOME_MES[m]} ({100 * shares_verba[m]:.0f}%)"
                              for m in shares_verba.nlargest(3).index)

            achados.append(Finding(
                id="sazonalidade-verbas-em-lote", frente=FRENTE,
                gravidade=("alta" if me_periodo < 4 else
                           "media" if me_periodo < 7 else "info"),
                titulo=(f"A verba a pessoas de {fmt_brl(total_verba)} se distribui "
                        f"como se o ano tivesse {me_periodo:.1f} meses"),
                descricao=(
                    "Diária, jeton, locomoção, quilometragem, ajuda de custo, "
                    "passagem e hospedagem lançados por centro de custo. A medida é "
                    "o número EQUIVALENTE de meses (inverso do índice "
                    "Herfindahl-Hirschman das doze participações mensais): 12,0 "
                    "significa um doze avos por mês; 1,0 significa o ano inteiro "
                    "num mês só.\n\n"
                    f"Os três meses de maior peso são {picos}, e juntos levam "
                    f"{100 * top3:.0f}% do total — num calendário uniforme levariam "
                    "25%.\n\n"
                    "**O que isso sugere e o que não prova.** Verba de deslocamento "
                    "acompanha a atividade que a justifica: fiscalização, sessão, "
                    "vistoria acontecem ao longo do ano e a despesa deveria "
                    "acompanhá-las. Concentrada em dois ou três meses, o padrão é "
                    "de **lote** — acerto retroativo, pagamento em bloco, "
                    "regularização de exercício — e não de viagem. Isso não prova "
                    "irregularidade: pode ser calendário real de campanha ou de "
                    "plenárias. Prova que a verba não foi paga quando a atividade "
                    "aconteceu, e essa é a pergunta a fazer.\n\n"
                    "A tabela abre por conta: mostra qual rubrica de verba é a "
                    "concentrada e em que mês ela pica." + nota_incompletos),
                evidencia=perfil_verba_conta.head(60),
                serie=(pd.DataFrame({"meses efetivos da verba": me_ano.round(2)})
                       if not me_ano.empty else None),
                grafico="line", unidade="meses",
                tabela_anual=(matriz_verba_ano.round(2)
                              .rename(columns=NOME_MES).reset_index()),
                base_legal="Lei 4.320/1964, arts. 62 e 63 (o pagamento só após a "
                           "liquidação, que comprova a origem e o objeto do que se "
                           "deve pagar); CF, art. 37, caput.",
                metricas={"total": round(total_verba, 2),
                          "meses_efetivos": round(me_periodo, 2),
                          "share_3_maiores_meses": round(top3, 4),
                          "mes_pico": int(shares_verba.idxmax()),
                          "share_mes_pico": round(float(shares_verba.max()), 4),
                          "contas": int(len(perfil_verba_conta))},
                como_verificar="Pedir, para os meses de pico, os relatórios de "
                               "viagem e as atas de sessão correspondentes — a "
                               "verba tem de casar com deslocamento ou presença "
                               "efetiva na data, não com o mês do acerto.",
            ))

    # ------------------------------------------------ 3. perfil mensal atípico
    #
    # `_perfil` explica por que a medida é o HHI invertido e não Gini nem CV.
    # Aqui só se ordena por ela e se separa quem está fora do padrão.
    ranking = perfil_unidade.dropna(subset=["meses_efetivos"]).sort_values(
        "meses_efetivos")
    negativas = int(perfil_unidade["meses_efetivos"].isna().sum())
    if not ranking.empty:
        atipicas = ranking[ranking["meses_efetivos"] < MESES_EFETIVOS_BAIXO]
        menor = float(ranking["meses_efetivos"].iloc[0])
        nota_neg = (f" {negativas} unidade(s) ficaram fora do índice por terem "
                    "algum mês com valor negativo (estorno lançado no mês): "
                    "participação negativa quebra o índice e um número "
                    "inventado ali seria pior que a lacuna."
                    if negativas else "")
        achados.append(Finding(
            id="sazonalidade-perfil-atipico", frente=FRENTE,
            gravidade=("alta" if not atipicas.empty and menor < 3 else
                       "media" if not atipicas.empty else "info"),
            titulo=(f"{len(atipicas)} centro(s) de custo concentram o ano em "
                    f"menos de {MESES_EFETIVOS_BAIXO:.0f} meses efetivos — o "
                    f"mais concentrado, em {menor:.1f}"
                    if not atipicas.empty else
                    f"Perfil mensal de {len(ranking)} centros de custo: o menos "
                    f"distribuído concentra o ano em {menor:.1f} meses "
                    "efetivos"),
            descricao=(
                "Ranking das unidades pela dispersão do gasto ao longo do ano. "
                "A medida é o número EQUIVALENTE de meses — o inverso do índice "
                "Herfindahl-Hirschman das doze participações mensais. Uma "
                "unidade que gasta um doze avos por mês marca 12,0; uma que "
                "gasta tudo em março marca 1,0.\n\n"
                "Foi escolhida em vez de Gini e de coeficiente de variação por "
                "ser diretamente legível ('o ano desta unidade coube em 3,2 "
                "meses'), por não depender da escala — a UGI de R$ 900 mil e a "
                "superintendência de R$ 300 milhões entram no mesmo ranking — e "
                "por não exigir a interpolação da curva de Lorenz, que faz "
                "implementações diferentes de Gini darem números diferentes "
                "para os mesmos dados.\n\n"
                "**O índice não diz qual mês.** Concentrar em março é tão "
                "'atípico' para ele quanto concentrar em dezembro, e só o "
                "segundo tem a ver com corrida orçamentária. Por isso as "
                "colunas `mes_pico` e `share_dezembro_pct` estão na mesma "
                "tabela: elas separam o gasto sazonal legítimo (uma unidade que "
                "concentra sempre no mesmo mês por causa de um evento anual) do "
                "gasto empurrado para o fim do exercício."
                + nota_neg + nota_incompletos),
            evidencia=ranking.head(80),
            base_legal="Lei 4.320/1964, art. 48 (a fixação de cotas visa "
                       "distribuir a despesa ao longo do exercício e manter o "
                       "equilíbrio entre receita e despesa).",
            metricas={"unidades_avaliadas": int(len(ranking)),
                      "unidades_atipicas": int(len(atipicas)),
                      "menor_meses_efetivos": round(menor, 2),
                      "mediana_meses_efetivos":
                          round(float(ranking["meses_efetivos"].median()), 2),
                      "unidades_sem_indice": negativas},
            como_verificar="Para as unidades no topo, pedir o cronograma de "
                           "desembolso aprovado e as cotas trimestrais — a Lei "
                           "4.320 prevê que a despesa seja distribuída ao longo "
                           "do exercício, e o desvio deveria estar justificado "
                           "em algum ato.",
        ))
    return achados
