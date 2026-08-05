"""Frente 3 — Empenhos e pagamentos: concentração, fracionamento, anulações.

Inclui o teste do **art. 60 da Lei 4.320/1964** ("é vedada a realização de
despesa sem prévio empenho"), que é das pouquíssimas regras de finanças
públicas verificáveis só com dados abertos: basta cruzar o que foi pago com o
que foi empenhado.

O limite honesto desse cruzamento precisa vir antes do resultado. "Não achei o
empenho no dataset" não é "o empenho não existe" — é "o empenho não está no
arquivo que o portal publicou, sob a chave que o portal publicou". As duas
coisas só coincidem se (a) os dois datasets divulgarem o mesmo identificador e
(b) cobrirem os mesmos exercícios. Quando não coincidem, o cruzamento deixa de
medir ilegalidade e passa a medir a si mesmo — e é isso, e não uma lista de
milhares de ilegalidades, que este módulo publica.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from .. import config
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl, norm_nome
from .common import df, fornecedores_mercado, transferencias_sistema

FRENTE = "Empenhos e pagamentos"

# Acima desta taxa o resultado deixa de ser um conjunto de despesas sem empenho
# e passa a ser um defeito da chave de junção ou da cobertura temporal. Deveria
# migrar para config.py junto dos demais limiares do projeto.
TAXA_LIMITE_CRUZAMENTO_PCT = 30.0
# Estorno é reversão de despesa, não despesa: não tem empenho próprio e
# entrava na lista como "pagamento sem empenho". Mesmo defeito que já havia
# contaminado o fracionamento.
PISO_VALOR_ART60 = 0.0


def _hhi(shares: pd.Series) -> float:
    p = shares / shares.sum()
    return float((p ** 2).sum())


def _anatomia_anulacoes(emp: pd.DataFrame):
    """Separa a anulação de rotina das três assinaturas suspeitas.

    Devolve (resumo_por_ano, empenhos_fantasma, reempenhos) ou None quando os
    campos necessários não vieram na coleta.
    """
    if "VALOR_ANULADO" not in emp.columns:
        return None
    d = emp.copy()
    for col in ("VALOR", "VALOR_ANULADO", "VALOR_PAGO"):
        d[col] = pd.to_numeric(d.get(col, 0), errors="coerce").fillna(0)

    # fantasma: anulado tudo (ou quase) e nada pago
    integral = d["VALOR_ANULADO"] >= d["VALOR"] * 0.999
    fantasmas = d[integral & (d["VALOR"] > 0) & (d["VALOR_PAGO"] <= 0)]

    # dezembro: anulação carimbada no último mês do exercício
    mes = (pd.to_datetime(d["DATA"], errors="coerce").dt.month
           if "DATA" in d.columns else pd.Series(pd.NA, index=d.index))
    d["_dez"] = mes.eq(12).fillna(False)

    resumo = d.groupby("_ano").apply(
        lambda g: pd.Series({
            "empenhado": float(g["VALOR"].sum()),
            "anulado": float(g["VALOR_ANULADO"].sum()),
            "fantasma": float(g.loc[integral.reindex(g.index, fill_value=False) &
                                    (g["VALOR_PAGO"] <= 0), "VALOR"].sum()),
            "anulado_dezembro": float(g.loc[g["_dez"], "VALOR_ANULADO"].sum()),
        }), include_groups=False) if not d.empty else pd.DataFrame()
    if resumo.empty:
        return None
    resumo["pct_fantasma"] = (100 * resumo["fantasma"] /
                              resumo["empenhado"].replace(0, pd.NA)).fillna(0).round(1)
    resumo["pct_anulado_em_dezembro"] = (
        100 * resumo["anulado_dezembro"] /
        resumo["anulado"].replace(0, pd.NA)).fillna(0).round(1)

    # Reempenho: mesmo credor + mesma conta, anulado num ano e empenhado no
    # seguinte. Na rodada real o dataset de empenhos não trazia credor nem
    # conta, `chave` vinha VAZIA e o ramo nunca executava — mas o relatório
    # publicava "reempenhos: 0", que é afirmação negativa sem lastro. Agora a
    # ausência do dado é declarada como ausência.
    reempenhos = pd.DataFrame()
    reempenho_testavel = False
    chave = [c for c in ("FAVORECIDO_NORM", "PESSOA", "CONTA_EMPENHO",
                         "NOME_CONTA") if c in d.columns]
    if chave and not fantasmas.empty:
        reempenho_testavel = True
        vivos = (d[d["VALOR_PAGO"] > 0]
                 .groupby(chave + ["_ano"])["VALOR"].sum().reset_index())
        alvo = fantasmas.groupby(chave + ["_ano"])["VALOR"].sum().reset_index()
        alvo["_ano_seguinte"] = alvo["_ano"] + 1
        reempenhos = alvo.merge(
            vivos, left_on=chave + ["_ano_seguinte"], right_on=chave + ["_ano"],
            suffixes=("_anulado", "_reempenhado"))
    return resumo.round(2), fantasmas, reempenhos, reempenho_testavel


def _chave_empenho(v) -> str:
    """Número do empenho normalizado para comparar os dois datasets.

    O mesmo empenho aparece como inteiro, como float ('1000.0', quando o JSON
    devolve número) e como texto com zeros à esquerda ('0001000'). Comparar as
    formas cruas casa zero linhas — e um join que casa zero linhas vira, sem
    esta função, uma acusação em massa contra o órgão por um defeito de tipo
    nosso. É a mesma classe de bug do acento em nome de conta.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = None
    if f is not None:
        if f != f:                       # NaN
            return ""
        if float(f).is_integer():
            return str(int(f))
    s = re.sub(r"\s+", "", str(v)).upper()
    if s in ("", "NAN", "NONE", "NAT", "<NA>", "-"):
        return ""
    return re.sub(r"^0+(?=.)", "", s)


def _so_digitos_chave(s: str) -> str:
    """Variante só-dígitos: casa '123/2024' com '1232024' quando um dos lados
    grava o exercício no próprio número e o outro não."""
    d = re.sub(r"\D", "", s or "")
    return re.sub(r"^0+(?=.)", "", d) if d else ""


def _favorecido_empenho(emp: pd.DataFrame) -> pd.Series:
    """Credor do empenho, sob qualquer rótulo que o portal já usou.

    Devolve série vazia quando nenhum rótulo existe — e é isso que DESLIGA a
    segunda chave, em vez de parear por valor puro, que casaria empenho de um
    credor com pagamento de outro só por coincidir o centavo.
    """
    for col in ("FAVORECIDO_NORM", "NOME_RAZAO_SOCIAL", "PESSOA", "CREDOR",
                "FAVORECIDO", "NOME_FAVORECIDO", "BENEFICIARIO"):
        if col in emp.columns:
            s = emp[col].fillna("").astype(str).map(norm_nome)
            if s.str.len().gt(0).any():
                return s
    return pd.Series("", index=emp.index, dtype=str)


def _janela(anos) -> str:
    anos = sorted(int(a) for a in anos)
    return f"{anos[0]}–{anos[-1]}" if anos else "sem exercício"


def _art60_indisponivel(motivo: str, gravidade: str, metricas: dict) -> Finding:
    """Art. 60 não testável. Declarar a indisponibilidade, nunca estimar.

    Um relatório que simplesmente omite a análise deixa o leitor concluir que
    não há despesa sem empenho. Não há é como saber — e a impossibilidade de
    saber, num dado que a LAI manda publicar, é ela própria o achado.
    """
    return Finding(
        id="empenhos-empenho-previo-indisponivel", frente=FRENTE,
        gravidade=gravidade,
        titulo="Empenho prévio (art. 60) não é verificável com os dados publicados",
        descricao=(
            "O art. 60 da Lei 4.320/1964 proíbe realizar despesa sem prévio "
            "empenho, e é uma das raras regras testáveis apenas com dados "
            "abertos: bastaria cruzar o que foi pago com o que foi "
            "empenhado.\n\n"
            f"**Esse cruzamento não pôde ser feito.** {motivo}\n\n"
            "Isto **não** é afirmação de que houve despesa sem empenho, nem de "
            "que não houve. É o registro de que o conjunto publicado não "
            "permite responder — e nenhum número foi estimado para preencher a "
            "lacuna."),
        metricas=metricas,
        # O inciso da despesa é o III ("registros das despesas"); o II é o de
        # repasses e transferências, e não sustenta o que a frase afirma.
        base_legal="Lei 4.320/1964, arts. 58 a 61 (empenho prévio e nota de "
                   "empenho); Lei 12.527/2011, art. 8º, §1º, III (registros "
                   "das despesas) e §3º, II e III (formato aberto e legível "
                   "por máquina, que é o que permite o cruzamento).",
        como_verificar="Pedir, em pedido de acesso à informação, a relação de "
                       "empenhos do mesmo período dos pagamentos publicados, "
                       "com o número do empenho e o credor em cada linha — que "
                       "é o campo que liga as duas bases.")


def _empenho_previo(staged) -> list[Finding]:
    """Cruza pagamentos × empenhos para testar o art. 60 da Lei 4.320/1964.

    Duas chaves, nesta ordem: (1) o NÚMERO do empenho declarado no pagamento;
    (2) para o que sobrar, credor normalizado + valor exato + exercício
    compatível. A segunda é deliberadamente GENEROSA — na dúvida o empenho é
    dado por existente. Num achado que acusa ilegalidade, errar para o lado de
    absolver é o único erro tolerável.
    """
    pg = df(staged, "pagamentos")
    emp = df(staged, "empenhos")

    if pg.empty or emp.empty:
        qual = ("os dois datasets voltaram vazios" if pg.empty and emp.empty
                else "o dataset de pagamentos voltou vazio" if pg.empty
                else "o dataset de empenhos voltou vazio")
        return [_art60_indisponivel(
            f"Na coleta, {qual}, e o cruzamento precisa dos dois lados.",
            "media",
            {"pagamentos_coletados": int(len(pg)),
             "empenhos_coletados": int(len(emp))})]

    # Sem `_ano` não há como saber se as bases cobrem o mesmo período, e o
    # `coluna(..., 0)` abaixo criaria um exercício fictício "0" que apareceria
    # no relatório como janela conferida. Exercício inventado é número
    # fabricado, ainda que valha zero.
    faltando = [n for n, d in (("pagamentos", pg), ("empenhos", emp))
                if "_ano" not in d.columns]
    if faltando:
        return [_art60_indisponivel(
            "O dataset de " + " e o de ".join(faltando) + " não traz o "
            "exercício de cada linha. Sem ele não é possível verificar se as "
            "duas bases cobrem o mesmo período, e cruzar períodos diferentes "
            "produz 'pagamento sem empenho' por construção.",
            "media",
            {"datasets_sem_exercicio": ", ".join(faltando),
             "pagamentos_coletados": int(len(pg)),
             "empenhos_coletados": int(len(emp))})]

    anos_pg = set(pd.to_numeric(coluna(pg, "_ano", 0), errors="coerce")
                  .dropna().astype(int))
    anos_emp = set(pd.to_numeric(coluna(emp, "_ano", 0), errors="coerce")
                   .dropna().astype(int))
    comuns = sorted(anos_pg & anos_emp)
    if not comuns:
        return [_art60_indisponivel(
            f"Os pagamentos cobrem {_janela(anos_pg)} e os empenhos, "
            f"{_janela(anos_emp)}: **não há um único exercício com dado nos "
            "dois lados**. Cruzar bases que não se sobrepõem produziria 100% de "
            "pagamentos 'sem empenho' por construção.",
            "alta",
            {"janela_pagamentos": _janela(anos_pg),
             "janela_empenhos": _janela(anos_emp),
             "anos_em_comum": 0})]

    # Restos a pagar: o pagamento do primeiro exercício coberto pode liquidar
    # empenho do exercício anterior, que ninguém coletou. Esse ano é o mais
    # exposto a falso positivo — sai da apuração quando há janela sobrando, e a
    # métrica declara se a mitigação foi possível.
    mitigado = len(comuns) >= 3
    apuracao = comuns[1:] if mitigado else comuns

    # `reset_index`: mais adiante a máscara "achou empenho" é aplicada por
    # RÓTULO de índice (`base.index.isin(resgatados)`). Se `pg` chegar com
    # rótulos repetidos, marcar um pagamento absolve o vizinho de mesmo rótulo
    # junto — e `casados_por_credor_e_valor` deixa de bater com o que foi de
    # fato marcado. Índice novo elimina a classe inteira.
    base = pg[pd.to_numeric(coluna(pg, "_ano", 0), errors="coerce")
              .isin(apuracao)].copy().reset_index(drop=True)
    # `coluna` e não `base["_ano"]`: o acesso direto levanta KeyError quando a
    # coluna falta e derruba a frente inteira — a guarda acima já barra esse
    # caso, e aqui a rede fica dupla porque foi exatamente assim que a versão
    # anterior estourava.
    base["_ano"] = (pd.to_numeric(coluna(base, "_ano", 0), errors="coerce")
                    .fillna(0).astype(int))
    base["VALOR"] = pd.to_numeric(coluna(base, "VALOR", 0), errors="coerce").fillna(0)
    # Estorno satisfaz qualquer teste de "não achei empenho" por ser lançamento
    # de reversão, sem nota de empenho própria.
    base = base[base["VALOR"] > PISO_VALOR_ART60]
    if base.empty:
        return [_art60_indisponivel(
            "Depois de restringir aos exercícios com dado nos dois datasets e "
            "de excluir estornos, não sobrou pagamento a conferir.",
            "media",
            {"janela_pagamentos": _janela(anos_pg),
             "janela_empenhos": _janela(anos_emp),
             "anos_em_comum": len(comuns)})]

    # ---------------------------------------------------- chave 1: nº do empenho
    chaves_emp: set[str] = set()
    if "NUMERO_EMPENHO" in emp.columns:
        chaves_emp = set(emp["NUMERO_EMPENHO"].map(_chave_empenho)) - {""}
    tem_numero = bool(chaves_emp) and "NUMERO_EMPENHO" in base.columns

    # Contado sobre a base inteira e INDEPENDENTE do cruzamento. Antes só era
    # calculado dentro de `if tem_numero`: quando o dataset de EMPENHOS não
    # publicava número, ou quando a coluna faltava nos PAGAMENTOS, a métrica
    # saía 0 — publicando "nenhum pagamento deixa de declarar o empenho"
    # justamente no caso em que nenhum o declara. Zero fabricado é pior que
    # ausência declarada.
    k = (base["NUMERO_EMPENHO"].map(_chave_empenho)
         if "NUMERO_EMPENHO" in base.columns
         else pd.Series("", index=base.index, dtype=str))
    sem_numero_declarado = int(k.eq("").sum())

    marcado = pd.Series(False, index=base.index)
    if tem_numero:
        marcado = k.ne("") & k.isin(chaves_emp)
        digitos_emp = {d for d in (_so_digitos_chave(c) for c in chaves_emp) if d}
        if digitos_emp:
            marcado |= k.ne("") & k.map(_so_digitos_chave).isin(digitos_emp)
    via_numero = int(marcado.sum())

    # ---------------------------------- chave 2: credor + valor + exercício
    fav_emp = _favorecido_empenho(emp)
    tem_fallback = (fav_emp.str.len().gt(0).any()
                    and "FAVORECIDO_NORM" in base.columns
                    and "VALOR" in emp.columns)
    via_fallback = 0
    if tem_fallback and (~marcado).any():
        e = pd.DataFrame({
            "_fav": fav_emp,
            "_val": pd.to_numeric(coluna(emp, "VALOR", 0), errors="coerce").round(2),
            "_ano_emp": pd.to_numeric(coluna(emp, "_ano", 0), errors="coerce"),
            "_data_emp": pd.to_datetime(coluna(emp, "DATA", None), errors="coerce"),
        })
        e = e[e["_fav"].ne("") & e["_val"].notna()]
        if not e.empty:
            g = (e.groupby(["_fav", "_val"])
                 .agg(ano_min=("_ano_emp", "min"), data_min=("_data_emp", "min"))
                 .reset_index())
            alvo = base.loc[~marcado].copy()
            alvo["_idx"] = alvo.index
            alvo["_fav"] = alvo["FAVORECIDO_NORM"].fillna("").astype(str).map(norm_nome)
            alvo["_val"] = alvo["VALOR"].round(2)
            alvo["_dt"] = pd.to_datetime(coluna(alvo, "DATA", None), errors="coerce")
            m = alvo[["_idx", "_ano", "_fav", "_val", "_dt"]].merge(
                g, on=["_fav", "_val"], how="left")
            # Generoso de propósito: basta existir empenho do MESMO credor pelo
            # MESMO valor em exercício igual ou anterior. Não se exige o mesmo
            # ano — restos a pagar são legítimos.
            ok = m["ano_min"].notna() & (m["ano_min"] <= m["_ano"])
            ok &= (m["data_min"].isna() | m["_dt"].isna() |
                   (m["data_min"] <= m["_dt"] + pd.Timedelta(days=5)))
            resgatados = set(m.loc[ok, "_idx"])
            if resgatados:
                # `.loc[lista_de_rótulos]` casa por rótulo e duplica linha
                # quando o índice repete; `isin` sobre o índice não tem esse
                # risco e devolve máscara já alinhada.
                marcado |= pd.Series(base.index.isin(list(resgatados)),
                                     index=base.index)
                via_fallback = int(len(resgatados))

    if not tem_numero and not tem_fallback:
        return [_art60_indisponivel(
            "O dataset de empenhos não publica **nem o número do empenho nem o "
            "credor**. Sem um dos dois não existe chave que ligue a nota de "
            "empenho ao pagamento: as duas bases falam da mesma despesa sem "
            "nenhum campo em comum.",
            "alta",
            {"janela_pagamentos": _janela(anos_pg),
             "janela_empenhos": _janela(anos_emp),
             "anos_em_comum": len(comuns),
             "colunas_empenhos": ", ".join(map(str, emp.columns[:20]))})]

    # ------------------------------------------------------------- resultado
    sem = base[~marcado]
    # Recorte da MESMA base da lista. `sem_numero_declarado` vale para todos os
    # pagamentos conferidos, e um pagamento sem número declarado pode ter sido
    # resgatado pela segunda chave — usá-lo numa frase que começa com "destes"
    # publicava um número maior que a própria lista (16 numa lista de 4).
    sem_numero_na_lista = int(k[~marcado].eq("").sum())
    n_tot, n_sem = int(len(base)), int(len(sem))
    taxa = 100 * n_sem / n_tot
    valor_sem = float(sem["VALOR"].sum())
    chave_usada = ("número do empenho" if tem_numero and not tem_fallback else
                   "número do empenho, com credor + valor como segunda chave"
                   if tem_numero else "credor + valor + exercício")

    d = base[["_ano", "VALOR"]].copy()
    d["_sem"] = (~marcado).astype(int)
    d["_valor_sem"] = base["VALOR"].where(~marcado, 0.0)
    por_ano = d.groupby("_ano").agg(
        pagamentos=("VALOR", "size"), sem_empenho=("_sem", "sum"),
        valor_pago=("VALOR", "sum"), valor_sem_empenho=("_valor_sem", "sum"),
    ).reset_index()
    por_ano["pct_sem_empenho"] = (
        100 * por_ano["sem_empenho"] /
        por_ano["pagamentos"].replace(0, pd.NA)).fillna(0).round(1)
    por_ano = por_ano.round(2)

    metricas = {
        "taxa_pct": round(taxa, 2),
        "pagamentos_sem_empenho": n_sem,
        "pagamentos_conferidos": n_tot,
        "valor_sem_empenho": round(valor_sem, 2),
        "universo": _janela(apuracao),
        "primeiro_ano_excluido": int(comuns[0]) if mitigado else None,
        "janela_pagamentos": _janela(anos_pg),
        "janela_empenhos": _janela(anos_emp),
        "chave": chave_usada,
        "casados_por_numero": via_numero,
        "casados_por_credor_e_valor": via_fallback,
        "pagamentos_sem_numero_declarado": sem_numero_declarado,
        "sem_empenho_e_sem_numero_declarado": sem_numero_na_lista,
    }

    universo_txt = (
        f"Universo conferido: **{_janela(apuracao)}**, que são os exercícios "
        f"com dado nos dois datasets (pagamentos cobrem {_janela(anos_pg)}; "
        f"empenhos, {_janela(anos_emp)})."
        + (f" O primeiro exercício em comum ({comuns[0]}) ficou de fora: um "
           "pagamento feito nele pode liquidar empenho do ano anterior, que "
           "ninguém coletou." if mitigado else
           " A sobreposição é curta demais para descartar o primeiro "
           "exercício, então o efeito de restos a pagar do ano anterior "
           "permanece embutido no número."))

    # -------------------------------------------------- guarda de metodologia
    if taxa > TAXA_LIMITE_CRUZAMENTO_PCT:
        # Uma taxa desta ordem não são milhares de ilegalidades: é a chave que
        # não serve ou a cobertura que não bate. Publicar a lista seria
        # transformar um defeito do dado aberto em acusação contra o órgão —
        # mesmo raciocínio de `contratos-vinculo-certame-ausente`.
        return [Finding(
            id="empenhos-vinculo-empenho-ausente", frente=FRENTE,
            gravidade="alta",
            titulo=(f"{taxa:.1f}% dos pagamentos ({n_sem} de {n_tot}) não "
                    "encontram empenho nos dados abertos — o vínculo entre as "
                    "duas bases não é publicado"),
            descricao=(
                "O art. 60 da Lei 4.320/1964 proíbe realizar despesa sem prévio "
                "empenho. O teste é simples: todo pagamento deve encontrar sua "
                "nota de empenho.\n\n"
                f"Aqui ele **não** encontrou em {taxa:.1f}% dos casos — e é "
                "justamente por ser tão alto que o número não pode ser lido "
                f"como {n_sem} ilegalidades. Um órgão que pagasse a maioria de "
                "suas despesas sem empenho não fecharia um balanço. O que uma "
                "taxa desta ordem indica é outra coisa: **a chave que ligaria "
                "as duas bases não está publicada, ou as bases não cobrem o "
                "mesmo período**.\n\n"
                f"{universo_txt}\n\n"
                f"Chave tentada: {chave_usada}. Casaram por número de empenho: "
                f"{via_numero}; resgatados por credor + valor: {via_fallback}; "
                f"pagamentos que sequer declaram número de empenho: "
                f"{sem_numero_declarado}.\n\n"
                f"Os {fmt_brl(valor_sem)} sem empenho localizado **não** devem "
                "ser lidos como 'despesa sem empenho', e sim como 'despesa "
                "cujo empenho o portal não permite localizar'. Este é **um** "
                "achado de transparência, e é mais grave que a lista: enquanto "
                "o vínculo não for publicado, a regra mais básica da execução "
                "orçamentária brasileira fica fora do alcance do controle "
                "social."),
            evidencia=por_ano,
            serie=por_ano.set_index("_ano")[["pct_sem_empenho"]].rename(
                columns={"pct_sem_empenho": "% dos pagamentos sem empenho localizado"}),
            grafico="line", unidade="%",
            tabela_anual=por_ano,
            base_legal="Lei 4.320/1964, art. 60 (vedada a despesa sem prévio "
                       "empenho) e arts. 58 e 61 (nota de empenho); Lei "
                       "12.527/2011, art. 8º, §1º, III e §3º, II e III.",
            como_verificar="Pedir a relação de empenhos do período com número "
                           "e credor por linha, e o dicionário de dados que "
                           "informe qual campo do pagamento aponta para qual "
                           "campo do empenho.",
            metricas=metricas)]

    # ------------------------------------------------------ resultado negativo
    if n_sem == 0:
        # Nada a sinalizar. Publicado como "info" e com título afirmativo: um
        # título do tipo "0 de N" é ruído e seria rebaixado de todo modo.
        return [Finding(
            id="empenhos-pagamento-sem-empenho", frente=FRENTE, gravidade="info",
            titulo=(f"Todos os {n_tot} pagamentos conferidos têm empenho "
                    f"correspondente ({_janela(apuracao)})"),
            descricao=(
                "Resultado negativo, e negativo com método: o cruzamento "
                "pagamento × empenho rodou, a chave funcionou "
                f"({chave_usada}) e nenhum pagamento ficou sem nota de "
                f"empenho localizável.\n\n{universo_txt}\n\n"
                + (f"Ressalva de método: {via_fallback} desses casamentos "
                   "vieram da segunda chave, credor + valor, que é "
                   "deliberadamente generosa — ela dá o empenho por existente "
                   "sempre que houver, no dataset, empenho do mesmo credor "
                   "pelo mesmo valor em exercício igual ou anterior. Um "
                   "resultado 'tudo casou' apoiado nela é mais fraco que um "
                   "apoiado no número do empenho.\n\n"
                   if via_fallback else "")
                + "Vale o alcance exato: isso diz que **existe** um empenho "
                "correspondente publicado, não que ele seja **prévio** ao "
                "pagamento nem que o valor confira — a data do empenho não é "
                "comparável linha a linha com a do pagamento nos dados "
                "abertos."),
            evidencia=por_ano, tabela_anual=por_ano,
            base_legal="Lei 4.320/1964, art. 60.",
            metricas=metricas)]

    # ------------------------------------------------------------- a lista
    cols = [c for c in ("_ano", "DATA", "NUMERO_EMPENHO", "NUMERO_PAGAMENTO",
                        "NOME_RAZAO_SOCIAL", "CPF_CNPJ", "CONTA_EMPENHO",
                        "HISTORICO", "VALOR") if c in sem.columns]
    return [Finding(
        id="empenhos-pagamento-sem-empenho", frente=FRENTE,
        gravidade="alta" if taxa >= 2 else "media",
        # "sem empenho LOCALIZADO": o dado sustenta que o empenho não está no
        # arquivo publicado, não que ele não exista. O título é o que sobrevive
        # à leitura rápida — se ele afirmar mais que a descrição, é o título
        # que vira a manchete errada.
        titulo=(f"{n_sem} pagamento(s) sem empenho localizado nos dados "
                f"publicados, somando {fmt_brl(valor_sem)} ({taxa:.2f}% dos "
                "pagamentos conferidos)"),
        descricao=(
            "O art. 60 da Lei 4.320/1964 é categórico: **é vedada a realização "
            "de despesa sem prévio empenho**. O empenho é o ato que reserva a "
            "dotação e cria a obrigação — pagar antes dele é gastar dinheiro "
            "que ninguém verificou existir.\n\n"
            f"{universo_txt}\n\n"
            f"Chave usada: {chave_usada}. Casaram por número de empenho "
            f"{via_numero} pagamentos e outros {via_fallback} foram resgatados "
            "pelo par credor + valor antes de sobrar esta lista. O pareamento "
            "por credor e valor é deliberadamente generoso: basta existir "
            "empenho do mesmo credor, pelo mesmo valor, em exercício igual ou "
            "anterior. Na dúvida, o empenho foi dado por existente.\n\n"
            "O que a lista **não** prova: ausência no dataset publicado não é "
            "ausência do empenho no sistema do órgão — pode ser falha de "
            "publicação. Por isso cada linha é um pedido de nota de empenho a "
            "fazer, não uma ilegalidade demonstrada."
            + (f"\n\nDestes, {sem_numero_na_lista} pagamento(s) sequer "
               "declaram um número de empenho no próprio registro publicado, "
               "o que já é falha de divulgação independentemente do "
               f"cruzamento (no conjunto conferido inteiro são "
               f"{sem_numero_declarado})." if sem_numero_na_lista else "")),
        evidencia=sem[cols].sort_values("VALOR", ascending=False).head(200),
        serie=por_ano.set_index("_ano")[["valor_sem_empenho"]].rename(
            columns={"valor_sem_empenho": "pago sem empenho localizado"}),
        grafico="bar",
        tabela_anual=por_ano,
        base_legal="Lei 4.320/1964, art. 60 — 'é vedada a realização de despesa "
                   "sem prévio empenho'; arts. 58, 59 e 61 (o empenho não pode "
                   "exceder o limite dos créditos concedidos e gera a nota de "
                   "empenho). Decreto-Lei 200/1967, art. 73.",
        como_verificar="Pedir a nota de empenho de cada pagamento listado, com "
                       "data anterior à do pagamento — ou, quando for o caso, "
                       "o reconhecimento de dívida de exercício anterior que a "
                       "tenha substituído, com a justificativa do ordenador.",
        metricas=metricas)]


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    emp = df(staged, "empenhos")
    pg = df(staged, "pagamentos")

    # -------------------------------------------------- concentração por credor
    if not pg.empty:
        pj = fornecedores_mercado(staged)
        if not pj.empty:
            por_credor = (pj.groupby(["_ano", "FAVORECIDO_NORM"])["VALOR"].sum()
                          .reset_index())
            linhas = []
            for ano, g in por_credor.groupby("_ano"):
                tot = g["VALOR"].sum()
                if tot <= 0:
                    continue
                top10 = g.nlargest(10, "VALOR")
                linhas.append({"ano": int(ano), "total_pj": float(tot),
                               "share_top10": float(top10["VALOR"].sum() / tot),
                               "hhi": round(_hhi(g["VALOR"]), 4)})
            conc = pd.DataFrame(linhas)
            criticos = conc[conc["hhi"] > 0.15]
            achados.append(Finding(
                id="empenhos-concentracao", frente=FRENTE,
                gravidade="media" if not criticos.empty else "info",
                titulo="Concentração de pagamentos por fornecedor (HHI e top-10 por ano)",
                descricao="HHI > 0,15 indica mercado de fornecedores muito concentrado "
                          "para um órgão dessa dimensão — favorece direcionamento.",
                evidencia=conc,
                metricas={"anos_criticos": criticos["ano"].tolist() if not criticos.empty else []},
            ))
            rank = A.ranking_anual(pj, "FAVORECIDO_NORM", "VALOR", top=10)
            serie_top = pd.DataFrame()
            if not rank.empty:
                lideres = (rank.groupby("FAVORECIDO_NORM")["total"].sum()
                           .nlargest(6).index.tolist())
                serie_top = A.serie_anual(
                    pj[pj["FAVORECIDO_NORM"].isin(lideres)], "VALOR",
                    by="FAVORECIDO_NORM")
            achados.append(Finding(
                id="empenhos-top-fornecedores", frente=FRENTE, gravidade="info",
                titulo="Maiores fornecedores por ano",
                descricao="Top 10 de cada exercício. O gráfico acompanha os seis "
                          "maiores do período — mostra quando cada fornecedor "
                          "entrou, cresceu ou saiu.",
                evidencia=rank, tabela_anual=rank,
                serie=serie_top, grafico="bar_stacked",
            ))
            fixos = (rank.groupby("FAVORECIDO_NORM")["_ano"].nunique()
                     .sort_values(ascending=False))
            fixos = fixos[fixos >= max(int(0.7 * pj["_ano"].nunique()), 3)]
            if not fixos.empty:
                achados.append(Finding(
                    id="empenhos-fornecedores-cativos", frente=FRENTE,
                    gravidade="media",
                    titulo=f"{len(fixos)} fornecedor(es) permanecem no top-10 em "
                           "70% ou mais dos exercícios",
                    descricao="Permanência prolongada no topo, num órgão que deve "
                              "licitar periodicamente, sugere renovação sucessiva "
                              "de contrato ou baixa competição real.",
                    evidencia=fixos.to_frame("anos_no_top10").reset_index(),
                ))

    # -------------------------------------------------- fracionamento
    if not pg.empty and {"FAVORECIDO_NORM", "DATA", "VALOR"} <= set(pg.columns):
        pj = fornecedores_mercado(staged).dropna(subset=["DATA"]).copy()
        # Fornecedor com contrato formal registrado executa parcelas mensais —
        # isso não é fracionamento. Sem esse recorte, o achado listava jornais,
        # rádios e prestadores de serviço contínuo (413 na primeira rodada).
        ctr = df(staged, "contratos")
        com_contrato = set(ctr.get("CONTRATADA_NORM", pd.Series(dtype=str)).dropna())
        pj = pj[~pj["FAVORECIDO_NORM"].isin(com_contrato)].sort_values("DATA")

        conta_col = "CONTA_EMPENHO" if "CONTA_EMPENHO" in pj.columns else None
        # Fracionamento é burla de LICITAÇÃO — só existe onde havia dever de
        # licitar. Convênio, termo de colaboração, repasse de ART, sentença
        # judicial e tributo não se licitam, e respondiam por quase todo o
        # achado (727 casos na rodada real, dos quais ~60 sobrevivem).
        if conta_col:
            nat = pj[conta_col].fillna("").astype(str)
            mapa_nat = {c: T.classificar_texto(c) for c in nat.unique()}
            nao_licitavel = nat.map(mapa_nat).isin(
                ["TRANSFERENCIAS", "FINANCEIRA_JUDICIAL", "REMUNERACAO",
                 "VERBAS_PESSOAIS"])
            # SEM ACENTO: "Convênios RES. 1032" não casa `convenio`. É o mesmo
            # defeito que fabricou o excedente de R$ 1,5 bi na conciliação —
            # todo regex sobre nome de conta tem de passar por T.normalizar.
            mapa_norm = {c: T.normalizar(c) for c in nat.unique()}
            nao_licitavel |= nat.map(mapa_norm).str.contains(
                r"convenio|termo de (?:colaboracao|fomento|parceria)|"
                r"\brepasse|subvencao|auxilio financeiro|restitui|devolu|"
                r"\bart\b|deposito judicial|precatorio",
                regex=True, na=False)
            pj = pj[~nao_licitavel]
        pj = pj[pj["VALOR"] > 0]        # estorno satisfazia "< limite" e
        if len(pj) < 3:                 # empurrava a contagem para 3
            pj = pj.iloc[0:0]
        suspeitas = []
        janela = pd.Timedelta(days=config.JANELA_FRACIONAMENTO_DIAS)
        chaves = ["FAVORECIDO_NORM"] + ([conta_col] if conta_col else [])
        for chave, g in (pj.groupby(chaves) if not pj.empty else []):
            if len(g) < 3:
                continue
            datas = g["DATA"].reset_index(drop=True)
            vals = g["VALOR"].reset_index(drop=True)
            anos = g["_ano"].reset_index(drop=True)
            i = 0
            for j in range(len(datas)):
                while datas[j] - datas[i] > janela:
                    i += 1
                # O limite é o do exercício em que a janela se fecha; comparar
                # um pagamento de 2014 com o teto de hoje esconde o achado.
                limite = config.limite_dispensa(anos[j])
                trecho = vals[i:j + 1]
                pequenos = trecho[trecho < limite]
                n = int(len(pequenos))
                soma = float(pequenos.sum())
                if n >= 3 and soma > limite:
                    dias = (datas[j] - datas[i]).days
                    # Três pagamentos no MESMO dia não é fracionamento no
                    # tempo: é parcela de uma nota só, ou lote de repasse.
                    if dias < 1:
                        continue
                    # cadência mensal regular = execução de contrato, não rajada
                    if n <= 4 and dias >= 60:
                        continue
                    nomes = chave if isinstance(chave, tuple) else (chave,)
                    suspeitas.append({
                        "fornecedor": nomes[0],
                        "conta": nomes[1] if len(nomes) > 1 else "",
                        "exercicio": int(anos[j]),
                        "limite_dispensa_no_ano": limite,
                        "inicio": datas[i].date(), "fim": datas[j].date(),
                        "dias": dias, "pagamentos": n,
                        "soma_janela": round(soma, 2),
                        "excedente": round(soma - limite, 2)})
                    break
        if suspeitas:
            ev = (pd.DataFrame(suspeitas)
                  .sort_values("soma_janela", ascending=False))
            por_ano = (ev.groupby("exercicio")
                       .agg(casos=("fornecedor", "size"),
                            soma=("soma_janela", "sum")))
            achados.append(Finding(
                id="empenhos-fracionamento", frente=FRENTE, gravidade="media",
                titulo=f"{len(ev)} caso(s) de possível fracionamento de despesa",
                descricao=(
                    "Gravidade média, e não alta, por uma razão: sem o processo "
                    "de compra não dá para saber se os pagamentos correspondem "
                    "ao mesmo objeto — que é o que caracteriza o fracionamento. "
                    "A lista é ponto de partida para pedir os processos, não "
                    "conclusão.\n\n"
                    "≥3 pagamentos ao mesmo fornecedor, na mesma conta contábil, "
                    f"em até {config.JANELA_FRACIONAMENTO_DIAS} dias, cada um "
                    "abaixo do limite de dispensa **vigente naquele exercício** e "
                    "somando acima dele. O limite aplicado aparece em "
                    "`limite_dispensa_no_ano`: R$ 8.000 até 2017 (Lei 8.666, art. "
                    "24, II), R$ 17.600 de 2018 a 2020 (Decreto 9.412/2018), "
                    "R$ 50.000 a partir de 2021 (Lei 14.133, art. 75, II) e "
                    "R$ 59.906,02 de 2023 em diante (Decreto 11.317/2022) — "
                    "confira o valor dos exercícios mais recentes, atualizados "
                    "por decreto anual.\n\nJá excluídos, porque não são "
                    "fracionamento: fornecedores com contrato formal "
                    "registrado; cadências mensais regulares; despesas que "
                    "**não se licitam** (convênio, termo de colaboração, "
                    "repasse, sentença judicial, tributo, folha e verba "
                    "indenizatória); rajadas de um único dia, que são parcela "
                    "de nota só; e estornos, que satisfaziam 'abaixo do "
                    "limite' por serem negativos."),
                evidencia=ev,
                serie=por_ano[["soma"]].rename(
                    columns={"soma": "valor fracionado"}),
                grafico="bar",
                tabela_anual=por_ano.reset_index(),
                base_legal="Lei 14.133/2021, art. 29 e art. 75, §1º (e, para os "
                           "exercícios anteriores, Lei 8.666/1993, art. 23, §5º) "
                           "— vedado fracionar despesa para fugir da modalidade "
                           "licitatória.",
                como_verificar="Pedir os processos de compra dos casos listados e "
                               "verificar se o objeto é o mesmo e se houve "
                               "planejamento anual da contratação.",
                metricas={"casos": int(len(ev)),
                          "soma_total": round(float(ev["soma_janela"].sum()), 2)},
            ))

    # -------------------------------------------------- transferências ao sistema
    transf = transferencias_sistema(staged)
    if not transf.empty:
        serie_t = A.serie_anual(transf, "VALOR", by="FAVORECIDO_NORM")
        total_t = float(transf["VALOR"].sum())
        achados.append(Finding(
            id="empenhos-transferencias-sistema", frente=FRENTE, gravidade="info",
            titulo=f"{fmt_brl(total_t)} transferidos ao Confea e à Mútua no período",
            descricao=("Repasses determinados por lei e estatuto, isolados do "
                       "ranking de fornecedores (não disputam licitação). Mostrados "
                       "à parte porque respondem por parcela relevante da receita "
                       "arrecadada dos profissionais e alimentam a rede de "
                       "influência entre os órgãos do sistema."),
            evidencia=A.ranking_anual(transf, "FAVORECIDO_NORM", "VALOR", top=6),
            serie=serie_t, grafico="bar_stacked",
            tabela_anual=A.ranking_anual(transf, "FAVORECIDO_NORM", "VALOR", top=6),
        ))

    # -------------------------------------------------- anulações e saldos
    if not emp.empty and {"VALOR", "VALOR_ANULADO"} <= set(emp.columns):
        por_ano = emp.groupby("_ano").agg(
            empenhado=("VALOR", "sum"), anulado=("VALOR_ANULADO", "sum")).reset_index()
        por_ano["pct_anulado"] = (100 * por_ano["anulado"] /
                                  por_ano["empenhado"].replace(0, pd.NA)).fillna(0).round(1)
        criticos = por_ano[por_ano["pct_anulado"] > 20]
        serie_anul = por_ano.set_index("_ano")[["pct_anulado"]].rename(
            columns={"pct_anulado": "% do empenhado anulado"})
        achados.append(Finding(
            id="empenhos-anulacoes", frente=FRENTE,
            gravidade="media" if not criticos.empty else "info",
            titulo=(f"{len(criticos)} exercício(s) com mais de 20% do valor "
                    "empenhado anulado" if not criticos.empty else
                    "Percentual anual de empenho anulado"),
            descricao="Anulação em massa sugere planejamento orçamentário ruim ou "
                      "manobra de reserva/liberação de dotação: empenha-se no "
                      "início do exercício para travar dotação e anula-se depois, "
                      "sem que a despesa jamais existisse.",
            evidencia=por_ano,
            serie=serie_anul, grafico="line", unidade="%",
            tabela_anual=por_ano,
            base_legal="Lei 4.320/1964, arts. 58 a 60 (empenho prévio e limite da "
                       "dotação) e art. 38 (anulação da despesa).",
            metricas={"anos_criticos": criticos["_ano"].astype(int).tolist()},
        ))

        # ---- anatomia: empenho fantasma, dezembro, reempenho
        anat = _anatomia_anulacoes(emp)
        if anat is not None:
            resumo, fantasmas, reempenhos, reempenho_testavel = anat
            achados.append(Finding(
                id="empenhos-anulacao-anatomia", frente=FRENTE,
                gravidade="alta" if float(resumo["fantasma"].sum()) > 0 else "info",
                titulo=("Anatomia das anulações: empenho fantasma, corrida de "
                        "dezembro e reempenho"),
                descricao=(
                    "A anulação tem três assinaturas distintas e só a primeira é "
                    "rotina. **Fantasma**: empenho anulado integralmente e nunca "
                    "pago — a despesa nunca existiu, mas ocupou dotação. "
                    "**Dezembro**: anulação concentrada no fim do exercício, típica "
                    "de liberação de saldo para acomodar outra despesa. "
                    "**Reempenho**: o mesmo credor volta a ser empenhado na mesma "
                    "conta no exercício seguinte — indica que a despesa era real e "
                    "a anulação apenas empurrou o compromisso para o ano seguinte, "
                    "fugindo do controle do exercício."),
                evidencia=resumo.reset_index(),
                serie=resumo[["fantasma", "anulado_dezembro"]].rename(
                    columns={"fantasma": "anulação integral (fantasma)",
                             "anulado_dezembro": "anulado em dezembro"}),
                grafico="bar_stacked",
                tabela_anual=resumo.reset_index(),
                base_legal="Lei 4.320/1964, arts. 35, 36 e 38; princípio da "
                           "anualidade orçamentária.",
                como_verificar="Pedir a nota de anulação de cada empenho fantasma "
                               "com a justificativa do ordenador de despesa.",
                metricas={
                    "empenhos_fantasma": int(len(fantasmas)),
                    "valor_fantasma": round(float(resumo["fantasma"].sum()), 2),
                    "reempenhos_ano_seguinte": (int(len(reempenhos))
                                                if reempenho_testavel
                                                else "nao_testado"),
                    "reempenho_motivo": (None if reempenho_testavel else
                                         "o dataset de empenhos não trouxe "
                                         "credor nem conta contábil — sem eles "
                                         "não há como parear anulação e "
                                         "reempenho"),
                },
            ))
            if not fantasmas.empty:
                cols = [c for c in ("_ano", "NUMERO", "FAVORECIDO_NORM",
                                    "CONTA_EMPENHO", "VALOR", "VALOR_ANULADO",
                                    "VALOR_PAGO", "DATA")
                        if c in fantasmas.columns]
                # Anulação nos primeiros dias do exercício, ou em lote no mesmo
                # dia, é abertura de exercício: rotina contábil, não anomalia.
                fant = fantasmas.copy()
                dt = pd.to_datetime(fant.get("DATA"), errors="coerce")
                inicio_ano = dt.dt.dayofyear.le(15).fillna(False)
                lote = dt.dt.date.map(dt.dt.date.value_counts()).ge(5).fillna(False)
                fant["_rotina"] = inicio_ano | lote
                anomalos = fant[~fant["_rotina"]]
                taxa = resumo["pct_fantasma"]
                pior = taxa.idxmax() if not taxa.empty else None
                achados.append(Finding(
                    id="empenhos-fantasma", frente=FRENTE,
                    gravidade="media" if not anomalos.empty else "info",
                    titulo=(f"Empenho anulado sem pagamento: {taxa.mean():.1f}% do "
                            f"empenhado em média, pico de {taxa.max():.1f}% em "
                            f"{int(pior)}" if pior is not None else
                            f"{len(fantasmas)} empenho(s) anulados integralmente"),
                    descricao=(
                        "Reserva de dotação que nunca virou despesa. Publicado "
                        "como TAXA e não como contagem: mil empenhos fantasma "
                        "num órgão que emite cem mil é rotina; a mesma "
                        "contagem em quem emite mil é outra coisa.\n\n"
                        "Já separada a rotina contábil — anulação nos 15 "
                        f"primeiros dias do exercício ou em lote no mesmo dia "
                        f"({len(fant) - len(anomalos)} de {len(fant)}). O que "
                        f"resta são {len(anomalos)} anulações integrais fora "
                        "desse padrão, que é onde vale perguntar por que a "
                        "despesa foi empenhada."),
                    evidencia=(anomalos[cols].sort_values(
                        "VALOR", ascending=False) if not anomalos.empty
                        else fant[cols].sort_values("VALOR", ascending=False)),
                    serie=resumo[["pct_fantasma"]].rename(
                        columns={"pct_fantasma": "% do empenhado anulado sem pagar"}),
                    grafico="line", unidade="%",
                    metricas={"taxa_media_pct": round(float(taxa.mean()), 2),
                              "taxa_maxima_pct": round(float(taxa.max()), 2),
                              "ano_pico": int(pior) if pior is not None else None,
                              "rotina_abertura": int(len(fant) - len(anomalos)),
                              "fora_do_padrao": int(len(anomalos))},
                ))
        # `VALOR_PAGO` vem do MESMO registro do empenho — é a comparação certa
        # para "pago acima do empenhado", porque não mistura fluxo de caixa com
        # contabilidade. Só faltava a guarda de coluna: sem ela, uma coleta que
        # traga VALOR_ANULADO e não VALOR_PAGO derruba a frente inteira.
        estouro = (emp[emp["VALOR_PAGO"] > emp["VALOR"] * 1.001]
                   if "VALOR_PAGO" in emp.columns else pd.DataFrame())
        if not estouro.empty:
            achados.append(Finding(
                id="empenhos-pago-maior-que-empenhado", frente=FRENTE, gravidade="alta",
                titulo=f"{len(estouro)} empenho(s) com valor pago maior que o empenhado",
                descricao="Pagamento acima do valor do empenho viola a execução "
                          "orçamentária (Lei 4.320/64, art. 59).",
                evidencia=estouro.head(100),
            ))

    # -------------------------------------------------- art. 60: empenho prévio
    achados.extend(_empenho_previo(staged))
    return achados
