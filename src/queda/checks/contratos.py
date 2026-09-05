"""Frente 4 — Contratos e licitações: o instrumento contra a execução.

Duas perguntas diferentes convivem aqui. A primeira é sobre o **instrumento**:
o contrato foi aditivado além do limite legal, tem vigência coerente, valor de
ordem de grandeza plausível, e aparece no PNCP como a lei manda.

A segunda é sobre a **distância entre o que foi contratado e o que foi pago**,
por fornecedor — a medida mais direta que existe de execução contratual e a
que ninguém publica. Ela abre dois caminhos opostos:

* **pago muito acima do contratado** sugere execução além do instrumento,
  aditivo não registrado no portal, ou pagamento sem cobertura contratual;
* **pago muito abaixo do contratado, com a vigência já encerrada**, sugere
  contrato inflado na origem: o valor reservado nunca foi executado, mas
  serviu de referência de preço, de dispensa de novo certame e de habilitação.

O limite honesto dessa segunda análise está no seu próprio material e precisa
ser dito em cada achado: contrato é **instrumento** (compromisso, muitas vezes
plurianual) e pagamento é **fluxo de caixa** de um exercício — não são a mesma
base. A junção entre os dois é feita por NOME normalizado da contratada,
porque o portal nem sempre publica o CNPJ do contrato, e um pagamento pode
quitar contrato anterior à janela coletada. Nenhuma linha destes achados prova
irregularidade: cada uma é uma divergência que o extrato do contrato e a
relação de empenhos resolvem em uma consulta.

Duas consequências disso são regra de código e não de texto, porque as duas já
produziram número fabricado em teste: a subexecução só é medida quando a
vigência inteira cabe na janela de pagamentos coletada (senão o "pago = 0" é a
janela de coleta, não o contrato), e quando o nome quase não casa entre as duas
bases o módulo troca a lista de acusações por UM achado sobre a junção.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from .. import config
from .. import taxonomia as T
from ..findings import Finding
from ..utils import RX_CNPJ_FORMATADO, cnpj14, coluna, fmt_brl
from .common import df

FRENTE = "Contratos e licitações"

# Limiares da razão pago/contratado. Ficam aqui porque este módulo é o único
# consumidor; se outra frente passar a usá-los, migram para config.py junto de
# PISO_ANALISE_FORNECEDOR (que é o piso de materialidade reaproveitado abaixo).
#
# 2,0 e não 1,25: o teto legal de aditivo (25%) mede o INSTRUMENTO, não a
# execução, e a diferença entre caixa e contabilidade produz sozinha desvios
# de dezenas de pontos. Dobrar o valor contratado é outra ordem de coisa.
RAZAO_EXECUCAO_ALTA = 2.0
RAZAO_EXECUCAO_BAIXA = 0.5

# Abaixo desta taxa de contratadas com ALGUM pagamento casado pelo nome, o que
# a razão mede é a junção, não a execução: publicar "N contratadas não
# executaram o contrato" nesse cenário é publicar campo vazio como achado.
TAXA_MINIMA_CASAMENTO_NOME = 25.0


def _valor_implausivel(ctr: pd.DataFrame) -> pd.DataFrame:
    """Contratos cujo VALOR não sobrevive a um teste de ordem de grandeza.

    Um único registro errado contamina toda soma de carteira. Na rodada real,
    C-010/2025 publicou R$ 345.011.843,00 para 180 dias — 4,9× o segundo maior
    contrato e R$ 1,9 milhão POR DIA de vigência, contra R$ 96 mil do segundo
    colocado nesse critério. Não afirmamos que é erro: afirmamos que o valor
    não é comparável aos demais e não pode entrar nas somas sem ressalva.
    """
    if ctr.empty or "VALOR" not in ctr:
        return pd.DataFrame()
    v = pd.to_numeric(ctr["VALOR"], errors="coerce").fillna(0)
    d = ctr[v > 0].copy()
    if len(d) < 20:
        return pd.DataFrame()
    d["_valor"] = pd.to_numeric(d["VALOR"], errors="coerce")
    ordenado = d["_valor"].sort_values(ascending=False)
    segundo = float(ordenado.iloc[1])
    d["_dias"] = 1.0
    if {"INI", "FIM"} <= set(d.columns):
        dias = (pd.to_datetime(d["FIM"], errors="coerce") -
                pd.to_datetime(d["INI"], errors="coerce")).dt.days
        d["_dias"] = dias.where(dias > 0, 1.0)
    d["valor_por_dia"] = (d["_valor"] / d["_dias"]).round(2)
    # Só o critério de VALOR exclui. Valor por dia é alto por construção em
    # contrato curto — usá-lo como corte tirava da carteira 13 contratos,
    # R$ 685 mi, sendo 12 deles atas de registro de preço perfeitamente
    # plausíveis. Fica como coluna de contexto, para quem for conferir.
    return d[d["_valor"] > 4 * segundo]


def contratos_implausiveis(ctr: pd.DataFrame) -> pd.Index:
    """Índices dos contratos com valor fora de ordem de grandeza (mesma regra
    de `contratos-valor-implausivel`). Exposto para `casos.py`: o mesmo
    contrato C-010/2025 entrava em dois achados ALTA e numa carteira com ele e
    noutra sem ele."""
    imp = _valor_implausivel(ctr)
    return imp.index if imp is not None else pd.Index([])


def _periodo_ctr(ctr: pd.DataFrame) -> str:
    anos = pd.to_datetime(ctr["INI"], errors="coerce").dt.year.dropna() if "INI" in ctr else pd.Series(dtype=float)
    if anos.empty:
        return "período não datado"
    a, b = int(anos.min()), int(anos.max())
    return str(a) if a == b else f"{a}–{b}"


def _cnpj14(serie: pd.Series) -> pd.Series:
    """Ver `utils.cnpj14`: CNPJ que passou por int perde o zero à esquerda
    (25 dos 39 "ausentes do PNCP" da rodada de 04/09/2026); CNPJ que passou
    por float ganha ".0" e virava 15 dígitos (44 "ausentes" da rodada 11)."""
    return cnpj14(serie, presumir_cnpj=True)   # só há CNPJ nas duas colunas cruzadas


_RE_NUM_ANO = re.compile(r"^[A-Z\-\s\.]*?(\d+)\s*/\s*(\d{4})")


def _partes_numero(s) -> tuple[int | None, int | None, bool]:
    """('C-019/2024') → (19, 2024, False); ('ATA 004/2024') → (4, 2024, True);
    ('C-22') → (22, None, False); ('3554') → (3554, None, False).

    O portal grafa 'C-019/2024' e o PNCP 'C-22' + anoContrato: casar contrato
    a contrato exige normalizar os dois."""
    t = str(s or "").strip().upper()
    eh_ata = t.startswith("ATA")
    m = _RE_NUM_ANO.match(t)
    if m:
        return int(m.group(1)), int(m.group(2)), eh_ata
    nums = re.findall(r"\d+", t)
    return (int(nums[0]) if nums else None), None, eh_ata


def _janela_pagamentos(pg: pd.DataFrame) -> tuple[pd.Timestamp | None,
                                                  pd.Timestamp]:
    """Entre que datas os dados enxergam execução — o piso e o teto da janela.

    TETO. Usar `hoje` como referência de "vigência encerrada" mente nos dois
    sentidos: a coleta costuma parar antes de hoje, e aí um contrato encerrado
    no último exercício apareceria como "não executado" só porque os
    pagamentos dele ainda não foram publicados. A referência honesta é a data
    do ÚLTIMO pagamento conhecido — depois dela, os dados simplesmente não
    veem execução nenhuma.

    PISO. Faltava, e era o buraco maior: o rol de contratos vem por VIGÊNCIA,
    então um contrato assinado em 2008 entra na coleta enquanto estiver
    vigente, mas os pagamentos dele são anteriores ao primeiro exercício
    coletado. Sem o piso, TODA contratada cuja vigência acabou antes do
    primeiro pagamento conhecido sai com "pago = 0" — e esse zero é produto da
    janela de coleta, não medição de contrato não executado. Num teste com um
    contrato de 2008-2012 e pagamentos a partir de 2013, o módulo publicava
    R$ 9 milhões "contratados e não pagos" que ninguém deixou de pagar.
    """
    if not pg.empty and "DATA" in pg.columns:
        d = pd.to_datetime(pg["DATA"], errors="coerce")
        if d.notna().any():
            return d.min(), d.max()
    return None, pd.Timestamp.today().normalize()


def _pago_vs_contratado(ctr: pd.DataFrame, adt: pd.DataFrame, pg: pd.DataFrame,
                        inicio: pd.Timestamp | None, corte: pd.Timestamp
                        ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Uma linha por contratada: quanto foi contratado e quanto foi pago.

    Devolve também os dois conjuntos que geraram a tabela — contratos e
    pagamentos JÁ FILTRADOS — para que a série anual do relatório seja
    construída sobre exatamente o mesmo material. Recontar a partir das bases
    cruas faria o gráfico somar um total diferente do da tabela logo abaixo
    dele, que é o tipo de incoerência que destrói a confiança no achado.

    As decisões abaixo mudam o resultado e são todas conservadoras — ou seja,
    reduzem o número de achados em vez de aumentá-lo:

    * **Aditivo de valor entra no contratado.** Sem isso todo contrato
      aditivado apareceria como "pago além do instrumento", quando o aditivo é
      justamente o que autoriza pagar mais. O aditivo único com valor idêntico
      ao do contrato é o caso ambíguo do achado
      `contratos-aditivo-indeterminado` (acréscimo de 100% ou valor global
      republicado?) e por isso saem DUAS colunas: `contratado`, com a leitura
      mais provável (não soma o ambíguo), e `contratado_teto`, com a leitura
      mais favorável ao órgão (soma tudo). O achado que acusa excesso usa o
      TETO — a divergência tem de sobreviver à interpretação mais generosa —,
      e o que aponta contrato não executado usa a estimativa menor. As duas
      escolhas erram para o mesmo lado: o de não acusar.
    * **Contratada com vários contratos soma todos antes de dividir.** Dividir
      o pagamento total por um contrato só produz razão inventada.
    * **Pagamento anterior ao início da primeira vigência conhecida não é
      execução** — por definição ele quita contrato que não está na janela
      coletada. O valor excluído volta na coluna `pago_antes_da_vigencia`,
      para que a decisão seja auditável em vez de invisível.
    * **Diária, jeton e locomoção só contam como execução de contrato quando o
      favorecido é COMPROVADAMENTE pessoa jurídica.** Uma pessoa física com
      contrato que também recebe diária apareceria recebendo muitas vezes o
      contratado. A MESMA conta paga a uma agência de viagem ou locadora
      permanece: aí é execução de contrato, não verba. O teste é `TIPO_PESSOA
      == "PJ"`, e não `!= "PF"`, porque documento ausente vira INDEFINIDO e
      não PJ (é a regra de `utils.classificar_pessoa`, escrita depois de um
      rótulo agregado sem CPF contaminar o ranking de fornecedores). Com o
      teste ao contrário, a diária de um favorecido sem documento voltava a
      contar como execução e produzia um achado de gravidade alta — 9,0× o
      contratado — sobre uma verba pessoal.
    * **Vigência fora da janela de pagamentos não é execução medível.** A
      coluna `execucao_observavel` marca a contratada cuja primeira vigência
      começa dentro da janela coletada; só ela pode ser avaliada por
      subexecução.
    """
    vazio = pd.DataFrame()
    if ctr.empty or "CONTRATADA_NORM" not in ctr.columns:
        return vazio, vazio, vazio

    c = ctr.copy()
    c["_nome"] = c["CONTRATADA_NORM"].fillna("").astype(str)
    c = c[c["_nome"].str.len() > 2]
    if c.empty:
        return vazio, vazio, vazio
    c["_valor"] = pd.to_numeric(coluna(c, "VALOR", 0), errors="coerce").fillna(0.0)
    for col in ("INI", "FIM"):
        c[col] = (pd.to_datetime(c[col], errors="coerce") if col in c.columns
                  else pd.Series(pd.NaT, index=c.index, dtype="datetime64[ns]"))

    c["_aditivo"] = 0.0
    c["_aditivo_teto"] = 0.0
    if (not adt.empty and {"NUMERO_CONTRATO", "VALOR"} <= set(adt.columns)
            and "NUMERO_CONTRATO" in c.columns):
        av = adt.copy()
        av["_v"] = pd.to_numeric(av["VALOR"], errors="coerce").fillna(0.0)
        av = av[av["_v"] > 0]              # aditivo de prazo não muda o valor
        if not av.empty:
            soma = av.groupby("NUMERO_CONTRATO")["_v"].agg(["sum", "size"])
            total = c["NUMERO_CONTRATO"].map(soma["sum"]).fillna(0.0)
            n_adt = c["NUMERO_CONTRATO"].map(soma["size"]).fillna(0)
            indeterminado = ((n_adt == 1) &
                             ((total - c["_valor"]).abs() < 0.005) &
                             (c["_valor"] > 0))
            c["_aditivo_teto"] = total
            c["_aditivo"] = total.where(~indeterminado, 0.0)

    g = (c.groupby("_nome")
         .agg(contratos=("_valor", "size"),
              valor_contratado=("_valor", "sum"),
              aditivos=("_aditivo", "sum"),
              _aditivos_teto=("_aditivo_teto", "sum"),
              vigencia_de=("INI", "min"),
              vigencia_ate=("FIM", "max"),
              _sem_fim=("FIM", lambda s: int(s.isna().sum()))))

    p = (pg.copy() if not pg.empty and "FAVORECIDO_NORM" in pg.columns
         else pd.DataFrame())
    if not p.empty:
        p["_nome"] = p["FAVORECIDO_NORM"].fillna("").astype(str)
        p = p[p["_nome"].isin(g.index)]
    if not p.empty:
        p["_valor"] = pd.to_numeric(coluna(p, "VALOR", 0),
                                    errors="coerce").fillna(0.0)
        p["_data"] = (pd.to_datetime(p["DATA"], errors="coerce")
                      if "DATA" in p.columns
                      else pd.Series(pd.NaT, index=p.index,
                                     dtype="datetime64[ns]"))
        # Classificação pelo NOME DA CONTA (nunca por regex crua sobre texto
        # acentuado): T.classificar_texto normaliza antes de casar, e roda
        # sobre os valores distintos, não linha a linha.
        conta = coluna(p, "CONTA_EMPENHO", "").fillna("").astype(str)
        natureza = conta.map({v: T.classificar_texto(v) for v in conta.unique()})
        # Evidência POSITIVA de pessoa jurídica, nunca a ausência de evidência
        # de pessoa física: INDEFINIDO (documento não publicado) fica do lado
        # da verba, que é o lado que não acusa.
        eh_pj = (p["TIPO_PESSOA"].eq("PJ") if "TIPO_PESSOA" in p.columns
                 else coluna(p, "PJ", False).astype(bool))
        p = p[~(~eh_pj & natureza.eq("VERBAS_PESSOAIS"))]

    anteriores = pd.Series(dtype=float)
    tot = pd.DataFrame(columns=["pago", "pagamentos"], dtype=float)
    if not p.empty:
        p = p.join(g["vigencia_de"].rename("_ini"), on="_nome")
        antes = (p["_data"].notna() & p["_ini"].notna() &
                 (p["_data"] < p["_ini"]))
        if antes.any():
            anteriores = p[antes].groupby("_nome")["_valor"].sum()
        p = p[~antes]
    if not p.empty:
        tot = p.groupby("_nome")["_valor"].agg(pago="sum", pagamentos="size")

    out = g.copy()
    out["pago"] = (tot["pago"] if "pago" in tot else pd.Series(dtype=float)
                   ).reindex(out.index).fillna(0.0).astype(float)
    out["pagamentos"] = ((tot["pagamentos"] if "pagamentos" in tot
                          else pd.Series(dtype=float))
                         .reindex(out.index).fillna(0).astype(int))
    out["pago_antes_da_vigencia"] = (anteriores.reindex(out.index)
                                     .fillna(0.0).astype(float))
    out["contratado"] = out["valor_contratado"] + out["aditivos"]
    out["contratado_teto"] = out["valor_contratado"] + out["_aditivos_teto"]
    denom = out["contratado"].where(out["contratado"] > 0)
    out["razao_pago_contratado"] = (out["pago"] / denom).round(3)
    teto = out["contratado_teto"].where(out["contratado_teto"] > 0)
    out["razao_no_teto"] = (out["pago"] / teto).round(3)
    out["divergencia"] = out["pago"] - out["contratado"]
    out["divergencia_no_teto"] = out["pago"] - out["contratado_teto"]
    out["divergencia_abs"] = out["divergencia"].abs()
    # Só é "vigência encerrada" quando TODOS os contratos da contratada têm fim
    # conhecido e anterior ao último pagamento visto. Contrato sem data de fim
    # publicada não pode ser dado como executado nem como abandonado.
    out["encerrada"] = (out["vigencia_ate"].notna() &
                        (out["vigencia_ate"] < corte) & (out["_sem_fim"] == 0))
    # E só é execução MEDÍVEL quando a vigência inteira cai dentro da janela de
    # pagamentos coletada. Se a primeira vigência começa antes do primeiro
    # pagamento conhecido, parte da execução aconteceu fora do alcance dos
    # dados e o que sobra não distingue "não executou" de "executou antes da
    # janela". Sem esta coluna o achado de subexecução publicava a janela de
    # coleta como se fosse achado.
    if inicio is None:
        out["execucao_observavel"] = False
    else:
        out["execucao_observavel"] = (out["vigencia_de"].notna() &
                                      (out["vigencia_de"] >= inicio))
    for col in ("valor_contratado", "aditivos", "contratado", "contratado_teto",
                "pago", "divergencia", "divergencia_no_teto", "divergencia_abs",
                "pago_antes_da_vigencia"):
        out[col] = out[col].round(2)
    tabela = (out.drop(columns=["_sem_fim", "_aditivos_teto"]).reset_index()
              .rename(columns={"_nome": "fornecedor"})
              .sort_values("divergencia_abs", ascending=False))
    c["_contratado"] = c["_valor"] + c["_aditivo"]
    return tabela, c, p


_COLS_RAZAO = ["fornecedor", "contratos", "valor_contratado", "aditivos",
               "contratado", "contratado_teto", "pago", "pagamentos",
               "razao_pago_contratado", "divergencia", "vigencia_de",
               "vigencia_ate", "encerrada", "execucao_observavel",
               "pago_antes_da_vigencia"]
# O achado de excesso mostra o denominador que usou (o teto), e não a
# estimativa central — quem confere a lista precisa ver a conta que a gerou.
_COLS_EXCESSO = ["fornecedor", "contratos", "valor_contratado", "aditivos",
                 "contratado_teto", "pago", "pagamentos", "razao_no_teto",
                 "divergencia_no_teto", "vigencia_de", "vigencia_ate",
                 "encerrada", "pago_antes_da_vigencia"]

# Repetido em todos os achados desta análise porque cada um é lido isolado no
# dashboard e nenhum deles pode circular sem o seu limite.
_RESSALVA = (
    "**O que estes números não provam.** Contrato e pagamento são bases "
    "diferentes: o contrato é o instrumento (compromisso, muitas vezes "
    "plurianual, às vezes com valor apenas estimado, como em ata de registro "
    "de preço) e o pagamento é fluxo de caixa do exercício. A junção entre os "
    "dois é feita pelo NOME normalizado da contratada, não pelo CNPJ — o "
    "portal nem sempre publica o CNPJ no contrato —, e razão social parecida "
    "ou grafia diferente separa ou funde fornecedores indevidamente. Um "
    "pagamento pode quitar contrato de exercício anterior à janela coletada, "
    "que esta ferramenta não enxerga. Cada linha é uma divergência a "
    "explicar, não uma irregularidade demonstrada.")


def _faturamento_pncp(staged) -> list[Finding]:
    """Notas fiscais registradas no PNCP contra os contratos do órgão.

    Terceira fonte independente sobre o mesmo contrato: empenho é compromisso,
    pagamento é saída de caixa, e a nota fiscal é a ENTREGA faturada. É a única
    das três que traz o CNPJ de quem emitiu — e emitente diferente do
    contratado é subcontratação ou cessão que o contrato não declara.

    O endpoint (`/v1/instrumentoscobranca/inclusao`) só aceita janela de 30
    dias; a janela anual que mandávamos até a rodada 12 era recusada com 422 e
    o dataset vinha vazio em toda rodada. O DTO nunca foi lido contra a rede
    nesta sessão: se a chave de ligação vier vazia, o achado diz isso em vez de
    publicar cruzamento nenhum.
    """
    cob = df(staged, "pncp_cobrancas")
    if cob.empty:
        return []
    pncp = df(staged, "pncp_contratos")
    valor = pd.to_numeric(coluna(cob, "valorNota", 0), errors="coerce").fillna(0)
    total = float(valor.sum())
    chave = coluna(cob, "numeroControlePNCP", "").fillna("").astype(str)
    ligadas = int(chave.str.strip().ne("").sum())
    achados: list[Finding] = []

    if not ligadas:
        return [Finding(
            id="contratos-faturamento-sem-chave", frente=FRENTE, gravidade="erro",
            titulo=(f"{len(cob)} nota(s) fiscal(is) coletadas do PNCP, mas sem a chave "
                    "que liga cada uma ao contrato"),
            descricao=(
                "As notas vieram, e o campo que aponta o contrato de origem "
                "(`numeroControlePNCP`, dentro de `recuperarContratoDTO`) veio vazio "
                "em todas — provavelmente outra grafia no DTO. Sem a chave não há "
                "cruzamento, e este relatório não estima o que não mediu. O JSON "
                "cru está em `data/raw/pncp.gov.br/`: conferir a grafia e ajustar "
                "`_pncp_cobranca_linha`, em `pipeline.py`."),
            evidencia=cob.head(20),
            metricas={"notas": int(len(cob)), "valor": round(total, 2)},
            fontes=["pncp_cobrancas"],
        )]

    por_contrato = pd.DataFrame()
    if not pncp.empty and "numeroControlePNCP" in pncp.columns:
        alvo = pncp[coluna(pncp, "orgao_papel", "").eq("alvo")].copy()
        alvo["_chave"] = coluna(alvo, "numeroControlePNCP", "").fillna("").astype(str)
        notas = cob.assign(_chave=chave, _valor=valor)
        agregado = notas.groupby("_chave").agg(
            notas=("_valor", "size"), faturado=("_valor", "sum")).reset_index()
        por_contrato = alvo.merge(agregado, on="_chave", how="inner")

    # ---- emitente diferente da contratada: subcontratação não declarada
    if not por_contrato.empty and "fornecedor_ni" in por_contrato.columns:
        notas = cob.assign(_chave=chave,
                           _emitente=_cnpj14(coluna(cob, "emitente_ni", "")))
        forn = dict(zip(por_contrato["_chave"],
                        _cnpj14(por_contrato["fornecedor_ni"])))
        notas["_contratada"] = notas["_chave"].map(forn).fillna("")
        outra = notas[notas["_contratada"].str.len().eq(14)
                      & notas["_emitente"].str.len().eq(14)
                      & notas["_emitente"].ne(notas["_contratada"])
                      # matriz e filial da mesma empresa não são terceiro
                      & (notas["_emitente"].str[:8] != notas["_contratada"].str[:8])]
        if not outra.empty:
            v = float(pd.to_numeric(coluna(outra, "valorNota", 0),
                                    errors="coerce").fillna(0).sum())
            achados.append(Finding(
                id="contratos-nota-de-terceiro", frente=FRENTE, gravidade="media",
                titulo=(f"{len(outra)} nota(s) fiscal(is) de {fmt_brl(v)} foram emitidas "
                        "por CNPJ diferente do da empresa contratada"),
                descricao=(
                    "A nota registrada no PNCP contra o contrato veio de um CNPJ de "
                    "raiz diferente da contratada — matriz e filial da mesma empresa "
                    "já estão excluídas. **O que isto pode ser:** subcontratação "
                    "(admitida quando o edital a prevê, Lei 14.133/2021, art. 122), "
                    "cessão contratual, ou erro de digitação do emitente no registro. "
                    "**O que não é:** prova de irregularidade — é a lista do que "
                    "precisa ter o edital conferido, porque quem executa o objeto "
                    "não é quem venceu o certame."),
                evidencia=outra.head(100),
                base_legal="Lei 14.133/2021, art. 122 (subcontratação) e art. 137, "
                           "§ 1º (cessão); Lei 8.666/1993, art. 72 e art. 78, VI.",
                como_verificar=("Para cada linha, pedir o edital e o termo de "
                                "subcontratação ou cessão autorizado pelo conselho."),
                metricas={"notas": int(len(outra)), "valor": round(v, 2)},
                fontes=["pncp_cobrancas", "pncp_contratos"],
            ))

    # ---- cobertura: quanto do contratado tem entrega faturada
    contratado = faturado = 0.0
    if not por_contrato.empty:
        contratado = float(pd.to_numeric(coluna(por_contrato, "valorGlobal", 0),
                                         errors="coerce").fillna(0).sum())
        faturado = float(por_contrato["faturado"].sum())
    achados.append(Finding(
        id="contratos-faturamento-pncp", frente=FRENTE, gravidade="info",
        titulo=(f"{len(cob)} nota(s) fiscal(is) de {fmt_brl(total)} registradas no PNCP "
                f"contra {len(por_contrato)} contrato(s) do conselho"),
        descricao=(
            "A nota fiscal é a terceira fonte sobre o mesmo contrato: o empenho diz "
            "o que foi comprometido, o pagamento diz o que saiu do caixa, e a nota "
            "diz o que foi entregue e faturado — com o CNPJ de quem emitiu.\n\n"
            "**O que este número NÃO é:** o total faturado ao conselho. Só entram "
            "notas que o próprio órgão registrou no PNCP, e só de contratos "
            "publicados lá — ou seja, a partir de 2021 e, na prática, do que a Lei "
            "14.133 tornou obrigatório. Contrato antigo e compra sem contrato ficam "
            "fora por construção."),
        evidencia=(por_contrato[[c for c in ("numeroContrato", "fornecedor_nome",
                                             "valorGlobal", "notas", "faturado")
                                 if c in por_contrato.columns]].head(200)
                   if not por_contrato.empty else cob.head(50)),
        metricas={"notas": int(len(cob)), "valor_faturado": round(total, 2),
                  "notas_ligadas_a_contrato": ligadas,
                  "contratos_com_nota": int(len(por_contrato)),
                  "valor_contratado_desses": round(contratado, 2),
                  "faturado_desses": round(faturado, 2)},
        fontes=["pncp_cobrancas", "pncp_contratos"],
    ))
    return achados


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    ctr = df(staged, "contratos")
    adt = df(staged, "aditivos")
    pg = df(staged, "pagamentos")
    pncp = df(staged, "pncp_contratos")

    # Contratadas que perdem um contrato na limpeza de valor implausível ficam
    # com denominador MENOR do que o cadastro publica — ver o bloco abaixo.
    sem_carteira_confiavel: set[str] = set()

    # ---------------------------------------- sanidade de valor, ANTES de somar
    implausiveis = _valor_implausivel(ctr)
    if not implausiveis.empty:
        cols = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                            "valor_por_dia", "DATA_INICIO_VIGENCIA",
                            "DATA_FINAL_VIGENCIA", "OBJETO")
                if c in implausiveis]
        excluido = float(implausiveis["VALOR"].sum())
        carteira = float(pd.to_numeric(ctr["VALOR"], errors="coerce").fillna(0).sum())
        achados.append(Finding(
            id="contratos-valor-implausivel", frente=FRENTE, gravidade="alta",
            titulo=(f"{len(implausiveis)} contrato(s) com valor fora de ordem de "
                    f"grandeza — {fmt_brl(excluido)} de "
                    f"{fmt_brl(carteira)} da carteira"),
            descricao=(
                "Contratos cujo valor publicado é várias vezes maior que o "
                "segundo maior da carteira. A coluna `valor_por_dia` entra "
                "como contexto e NÃO como critério: contrato curto tem valor "
                "por dia alto por construção, e usá-lo para excluir tirava da "
                "carteira atas de registro de preço perfeitamente plausíveis. "
                "**Não estamos dizendo que é "
                "erro de digitação** — estamos dizendo que o número não é "
                "comparável aos demais e que somá-lo à carteira sem ressalva "
                "distorce todos os totais. A divergência é, ela própria, o "
                "pedido: o extrato do contrato e os empenhos correspondentes "
                "resolvem em uma consulta."),
            evidencia=implausiveis[cols].sort_values("VALOR", ascending=False),
            metricas={"valor_excluido": round(excluido, 2),
                      "carteira_bruta": round(carteira, 2),
                      "carteira_sem_implausiveis": round(carteira - excluido, 2)},
            como_verificar="Pedir o extrato do contrato e a relação de empenhos "
                           "emitidos contra ele; comparar com o total "
                           "efetivamente pago à contratada no período.",
        ))
        ctr = ctr[~ctr.index.isin(implausiveis.index)]
        # A exclusão acima protege as somas de carteira e ENVENENA a razão
        # pago/contratado: a contratada continua com todos os pagamentos dela
        # no numerador e perde do denominador justo o maior contrato. Num teste
        # com uma contratada de R$ 1 mi + R$ 400 mi (este implausível) e
        # R$ 300 mi pagos, o módulo publicava, com gravidade alta, "R$ 299
        # milhões pagos além dos instrumentos" — número inteiramente produzido
        # por este filtro. Quem cai aqui sai inteiro da razão.
        sem_carteira_confiavel = set(
            coluna(implausiveis, "CONTRATADA_NORM").dropna().astype(str)) - {""}

    # -------------------------------------------------- aditivos acima do limite
    if not ctr.empty and not adt.empty and "NUMERO_CONTRATO" in adt:
        # Aditivo de valor zero é prorrogação de prazo, não acréscimo — misturar
        # os dois inflava o achado na primeira rodada.
        adt_valor = adt[adt["VALOR"] > 0]
        adt_prazo = adt[adt["VALOR"] <= 0]
        soma_adt = (adt_valor.groupby("NUMERO_CONTRATO")["VALOR"]
                    .agg(total_aditivos="sum", n_aditivos="size"))
        base = ctr.set_index("NUMERO_CONTRATO").join(soma_adt).reset_index()
        base["total_aditivos"] = base["total_aditivos"].fillna(0)
        base["n_aditivos"] = base["n_aditivos"].fillna(0).astype(int)
        base["pct_aditivo"] = (100 * base["total_aditivos"] /
                               base["VALOR"].replace(0, pd.NA)).fillna(0).round(1)

        # SEMÂNTICA INDETERMINADA: quando o único aditivo registrado tem valor
        # IGUAL ao valor do contrato, ao centavo, o campo quase certamente
        # carrega o valor GLOBAL revisado e não o incremento. Na rodada real
        # eram 59 de 79 linhas e R$ 417,8 mi de R$ 464,6 mi — 90% do achado.
        # Tratar isso como acréscimo de 100% é acusar com base em ambiguidade
        # de cadastro.
        base["_indeterminado"] = ((base["n_aditivos"] == 1) &
                                  ((base["total_aditivos"] - base["VALOR"]).abs()
                                   < 0.005) & (base["VALOR"] > 0))

        # Contratos de valor irrisório fazem o percentual explodir sem que isso
        # signifique irregularidade relevante — separados como ruído de base.
        relevantes = base[(base["VALOR"] >= config.PISO_CONTRATO_ADITIVO) &
                          (~base["_indeterminado"])]
        indeterminados = base[base["_indeterminado"]]
        if not indeterminados.empty:
            cols_i = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                  "total_aditivos", "DATA_INICIO_VIGENCIA")
                      if c in indeterminados]
            achados.append(Finding(
                id="contratos-aditivo-indeterminado", frente=FRENTE,
                gravidade="media",
                titulo=(f"{len(indeterminados)} contrato(s) cujo aditivo tem "
                        "valor idêntico ao do contrato — o campo é ambíguo"),
                descricao=(
                    "O portal publica o aditivo com valor exatamente igual ao "
                    "do contrato, ao centavo, e um único registro. Isso admite "
                    "duas leituras opostas: acréscimo de 100% do valor "
                    "original, ou simples repetição do valor global revisado. "
                    "Não dá para escolher pelos dados, e a diferença entre as "
                    "duas leituras é de "
                    f"{fmt_brl(float(indeterminados['total_aditivos'].sum()))}. "
                    "Estes contratos ficam FORA do achado de acréscimo acima "
                    "do limite, e a ambiguidade vira um pedido: basta ler um "
                    "termo aditivo para resolver todos."),
                evidencia=indeterminados[cols_i].sort_values(
                    "VALOR", ascending=False),
                metricas={"contratos": int(len(indeterminados)),
                          "valor_em_disputa":
                              round(float(indeterminados["total_aditivos"].sum()), 2)},
                como_verificar="Pedir UM termo aditivo desta lista e verificar "
                               "se ele diz 'acréscimo de R$ X' ou 'valor "
                               "global revisado para R$ X'.",
            ))
        estourados = relevantes[relevantes["pct_aditivo"] > config.ADITIVO_LIMITE_PCT]
        if not estourados.empty:
            acrescimo = float(estourados["total_aditivos"].sum())
            cols = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                "total_aditivos", "n_aditivos", "pct_aditivo",
                                "DATA_INICIO_VIGENCIA") if c in estourados]
            serie = pd.DataFrame()
            if "INI" in estourados:
                serie = A.serie_anual(
                    estourados.assign(_ano=estourados["INI"].dt.year),
                    "total_aditivos").rename(
                        columns={"total_aditivos": "acréscimo por aditivo"})
            achados.append(Finding(
                id="contratos-aditivos-limite", frente=FRENTE, gravidade="alta",
                titulo=f"{len(estourados)} contrato(s) com acréscimo por aditivo "
                       f"acima de {config.ADITIVO_LIMITE_PCT:.0f}% — "
                       f"{fmt_brl(acrescimo)} somados",
                descricao=(
                    "Acréscimo acumulado de VALOR (aditivos de prazo puro foram "
                    "excluídos) sobre contratos de pelo menos "
                    f"{fmt_brl(config.PISO_CONTRATO_ADITIVO)}. Percentuais muito "
                    "acima de 100% podem indicar também erro de cadastro no "
                    "portal (aditivo lançado com o valor total do contrato em vez "
                    "do incremento) — conferir o termo aditivo antes de concluir."),
                evidencia=estourados[cols].sort_values("pct_aditivo", ascending=False),
                serie=serie, grafico="bar",
                base_legal="Lei 8.666/1993, art. 65, §1º e Lei 14.133/2021, "
                           "art. 125 — acréscimo limitado a 25% (50% para reforma "
                           "de edifício ou equipamento).",
                como_verificar="Solicitar os termos aditivos dos contratos listados "
                               "e verificar se o valor lançado é incremento ou "
                               "valor global revisado, e qual a justificativa técnica.",
                metricas={"acrescimo_total": round(acrescimo, 2),
                          "contratos_ignorados_valor_irrisorio":
                              int(len(base) - len(relevantes))},
            ))
        if not adt_prazo.empty:
            achados.append(Finding(
                id="contratos-prorrogacoes", frente=FRENTE, gravidade="info",
                titulo=f"{len(adt_prazo)} aditivo(s) sem acréscimo de valor "
                       "(prorrogação de prazo)",
                descricao="Separados do achado anterior. Prorrogações sucessivas do "
                          "mesmo contrato indicam serviço contínuo renovado em vez "
                          "de nova licitação — legítimo até o limite legal de "
                          "prazo, mas vale acompanhar a contagem.",
                evidencia=(adt_prazo.groupby("NUMERO_CONTRATO").size()
                           .sort_values(ascending=False)
                           .to_frame("prorrogacoes").reset_index().head(50)),
            ))

    # -------------------------------------------------- pagamento após vigência
    if not ctr.empty and not pg.empty and "FIM" in ctr and "FAVORECIDO_NORM" in pg:
        fim_por_contratada = ctr.dropna(subset=["FIM"]).groupby("CONTRATADA_NORM")["FIM"].max()
        pj = pg[~pg.get("PF", False).astype(bool)].dropna(subset=["DATA"])
        m = pj.join(fim_por_contratada.rename("FIM_VIGENCIA"), on="FAVORECIDO_NORM")
        tarde = m[m["DATA"] > m["FIM_VIGENCIA"] + pd.Timedelta(days=60)]
        if not tarde.empty:
            resumo = (tarde.groupby("FAVORECIDO_NORM")
                      .agg(pagamentos=("VALOR", "size"), total=("VALOR", "sum"),
                           fim_vigencia=("FIM_VIGENCIA", "max"),
                           ultimo_pagamento=("DATA", "max")).reset_index()
                      .sort_values("total", ascending=False))
            achados.append(Finding(
                id="contratos-pagamento-pos-vigencia", frente=FRENTE, gravidade="media",
                titulo=f"{len(resumo)} contratada(s) recebendo mais de 60 dias após o "
                       "fim da última vigência conhecida",
                descricao="Pagamento sem cobertura contratual vigente (ou contrato não "
                          "publicado no portal). Aproximação por nome da contratada — "
                          "conferir caso a caso.",
                evidencia=resumo.head(50),
            ))

    # -------------------------------------------------- contratos sem licitação
    if not ctr.empty and "NUMERO_LICITACAO" in ctr:
        sem_lic = ctr[ctr["NUMERO_LICITACAO"].isna() |
                      (ctr["NUMERO_LICITACAO"].astype(str).str.strip().isin(["", "None", "nan"]))]
        if not sem_lic.empty:
            tot = float(sem_lic["VALOR"].sum())
            taxa = 100 * len(sem_lic) / len(ctr)
            lic = df(staged, "licitacoes")
            lic_pncp = df(staged, "licitacoes_pncp")
            # Um check que aciona quase todo o universo mudou de natureza: não
            # está apontando contratações diretas, está apontando que o campo
            # nunca é preenchido. Na rodada real foram 965 de 965 — e o
            # dataset de licitações veio com zero linhas.
            if taxa >= 95:
                achados.append(Finding(
                    id="contratos-vinculo-certame-ausente", frente=FRENTE,
                    gravidade="alta",
                    titulo=(f"{taxa:.1f}% dos contratos do portal próprio ({len(sem_lic)} de "
                            f"{len(ctr)}, {_periodo_ctr(ctr)}) não informam a licitação de origem"
                            + ("; o rol de licitações não veio do portal (endpoint "
                               "respondeu vazio ou recusou os parâmetros)"
                               if lic.empty else "")
                            + (f"; o PNCP traz {len(lic_pncp)} contratações desde jan/"
                               f"{config.LICITACOES_PNCP_DESDE}"
                               if not lic_pncp.empty else "")),
                    descricao=(
                        "Quando o campo falta em praticamente todos os "
                        "registros, ele não distingue contratação direta de "
                        "licitada — simplesmente não é preenchido. Isso não é "
                        f"uma lista de {len(sem_lic)} irregularidades: é **um** achado de "
                        "transparência sobre o portal próprio, porque torna "
                        "impossível saber, a partir dele, qual contratação foi "
                        "licitada e qual foi direta. "
                        + ("O conjunto de licitações do portal também veio vazio: o "
                           "endpoint respondeu 200 com lista vazia nos exercícios "
                           "anteriores e recusou (400) o exercício em curso — o que "
                           "não permite afirmar que o rol não exista, só que não veio "
                           "por este caminho. "
                           if lic.empty else "")
                        + (f"Para o que foi publicado a partir de "
                           f"{config.LICITACOES_PNCP_DESDE}, o PNCP traz o rol "
                           f"({len(lic_pncp)} contratações) e {len(pncp[pncp.get('orgao_papel') == 'alvo']) if not pncp.empty else 0} "
                           "contratos do conselho, com a compra de origem de cada um — "
                           "é por lá que a verificação segue. "
                           if not lic_pncp.empty else "")
                        + f"A carteira afetada soma {fmt_brl(tot)} — que "
                        "**não** deve ser lida como 'contratos sem licitação', "
                        "e sim como 'contratos cuja forma de contratação o "
                        "portal não informa'."),
                    evidencia=sem_lic[["NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                       "DATA_INICIO_VIGENCIA"]]
                    .sort_values("VALOR", ascending=False).head(50),
                    metricas={"taxa_pct": round(taxa, 1),
                              "contratos": int(len(sem_lic)),
                              "universo": int(len(ctr)),
                              "valor_total": round(tot, 2),
                              "licitacoes_coletadas": int(len(lic))},
                    base_legal="Lei 12.527/2011, art. 8º, §1º, IV (divulgação de "
                               "licitações e contratos); Lei 14.133/2021, art. 174 "
                               "(PNCP como fonte nacional).",
                    como_verificar="Pedir a relação de licitações do período e "
                                   "o vínculo contrato ↔ certame; para 2023 em diante, "
                                   "seguir pelo `numeroControlePNCPCompra` de cada "
                                   "contrato no PNCP.",
                    fontes=["contratos", "licitacoes", "licitacoes_pncp"],
                ))
                # O contrato publicado no PNCP carrega o número de controle da
                # COMPRA de origem — e por ele se chega ao edital e ao termo de
                # referência. Isso muda o enquadramento do achado acima: não é
                # que o certame não exista, é que o portal PRÓPRIO não o publica
                # enquanto o órgão o declara no sistema nacional.
                if not pncp.empty and "numeroControlePNCPCompra" in pncp:
                    alvo_c = pncp[pncp.get("orgao_papel") == "alvo"]
                    com_compra = alvo_c[coluna(alvo_c, "numeroControlePNCPCompra")
                                        .fillna("").astype(str).str.len() > 5]
                    if not com_compra.empty:
                        achados.append(Finding(
                            id="contratos-certame-no-pncp", frente=FRENTE,
                            gravidade="media",
                            titulo=(f"{len(com_compra)} contrato(s) têm o certame "
                                    "de origem identificado no PNCP, que o portal "
                                    "do conselho não informa"),
                            descricao=(
                                "O mesmo órgão que deixa o campo de licitação "
                                "vazio no portal próprio declara, no PNCP, o "
                                "número de controle da COMPRA que originou cada "
                                "contrato. Por esse número se chega ao edital, ao "
                                "termo de referência e à modalidade.\n\n"
                                "A informação existe e é pública — só não está "
                                "onde o cidadão a procuraria. Cada linha abaixo é "
                                "um caminho pronto para pedir os documentos do "
                                "certame."),
                            evidencia=(com_compra[[c for c in
                                                   ("numeroContrato", "fornecedor_nome",
                                                    "valorGlobal", "objeto",
                                                    "numeroControlePNCPCompra",
                                                    "processo")
                                                   if c in com_compra]]
                                       .sort_values("valorGlobal", ascending=False)
                                       .head(200)),
                            base_legal="Lei 14.133/2021, arts. 94 e 174; Lei "
                                       "12.527/2011, art. 8º, §1º, IV.",
                            como_verificar=(
                                "Para cada número de controle, consultar "
                                "`pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/"
                                "{ano}/{sequencial}/arquivos` e baixar o edital e "
                                "o termo de referência."),
                            metricas={"com_certame_no_pncp": int(len(com_compra)),
                                      "sem_certame_no_portal": int(len(sem_lic))},
                        ))
            else:
                achados.append(Finding(
                    id="contratos-sem-licitacao", frente=FRENTE,
                    gravidade="media" if tot > 1_000_000 else "baixa",
                    titulo=f"{len(sem_lic)} contrato(s) sem número de licitação "
                           f"vinculado ({taxa:.0f}% da carteira)",
                    descricao="Contratação direta (dispensa/inexigibilidade) ou "
                              "falha de registro. Ranqueado por valor.",
                    evidencia=sem_lic[["NUMERO_CONTRATO", "CONTRATADA", "VALOR",
                                       "DATA_INICIO_VIGENCIA", "DATA_FINAL_VIGENCIA"]]
                    .sort_values("VALOR", ascending=False).head(100),
                    metricas={"valor_total": round(tot, 2),
                              "taxa_pct": round(taxa, 1)},
                ))

    # -------------------------------------------------- cross-check Implanta × PNCP
    if not pncp.empty:
        alvo = pncp[pncp.get("orgao_papel") == "alvo"]
        if not ctr.empty and not alvo.empty:
            achados.extend(_cruzamento_pncp(ctr, alvo, staged))
        # valor acumulado muito acima do global
        infl = alvo[(alvo["valorAcumulado"] > 0) &
                    (alvo["valorAcumulado"] > alvo["valorGlobal"] * (1 + config.ADITIVO_LIMITE_PCT / 100))]
        if not infl.empty:
            achados.append(Finding(
                id="contratos-pncp-acumulado", frente=FRENTE, gravidade="media",
                titulo=f"{len(infl)} contrato(s) no PNCP com valor acumulado "
                       f">{config.ADITIVO_LIMITE_PCT:.0f}% acima do valor global",
                descricao="Indicativo de aditivos sucessivos registrados no PNCP.",
                evidencia=infl[["numeroContrato", "fornecedor_nome", "objeto",
                                "valorGlobal", "valorAcumulado"]],
            ))

    # --------------------------- modalidade de origem, via PNCP (o portal não diz)
    achados.extend(_achados_modalidade_origem(pncp, df(staged, "licitacoes_pncp")))

    # ------------------------------------ razão pago/contratado por fornecedor
    achados.extend(_achados_razao(ctr, adt, pg, sem_carteira_confiavel))

    # ---------------------- nota fiscal: a entrega, terceira fonte do contrato
    achados.extend(_faturamento_pncp(staged))
    return achados


def _cruzamento_pncp(ctr: pd.DataFrame, alvo: pd.DataFrame, staged) -> list[Finding]:
    """Portal próprio × PNCP, contrato a contrato — e cada dúvida rotulada.

    Quatro lições da rodada 10 (05/09/2026), quando o achado dizia "11
    contratos ausentes, R$ 37 mi":

    * 6 dos 11 eram ATAS de registro de preços, que o PNCP publica em cadastro
      próprio (`/v1/atas`), não em `/v1/contratos` — nunca casariam;
    * o casamento era por FORNECEDOR (conjunto de CNPJs), não por contrato;
    * os dois maiores (R$ 35,2 mi) têm ata de maio/2023 do mesmo CNPJ no
      portal: podem ser contratos decorrentes de certame anterior a 30/12/2023,
      regidos pela Lei 8.666 durante toda a vigência (Lei 14.133, art. 191,
      parágrafo único) e sem obrigação de PNCP — o edital diz;
    * CPF nunca casa porque o portal só expõe CNPJ.

    Cada uma vira rótulo na coluna `situacao`, e o título conta só o que ficou
    sem rótulo de dúvida.
    """
    achados: list[Finding] = []
    doc_portal = _cnpj14(coluna(ctr, "CONTRATADA_CNPJ", ""))
    if "CONTRATADA" in ctr.columns:
        # a fonte primária é o CNPJ formatado dentro de CONTRATADA — imune a
        # qualquer coisa que a coluna derivada tenha sofrido no CSV
        formatado = _cnpj14(ctr["CONTRATADA"].fillna("").astype(str)
                            .str.extract(RX_CNPJ_FORMATADO, expand=False).fillna(""))
        doc_portal = formatado.where(formatado.str.len().eq(14), doc_portal)
    doc_pncp = _cnpj14(coluna(alvo, "fornecedor_ni", ""))
    por_cnpj = doc_portal.str.len().eq(14).any() and doc_pncp.str.len().eq(14).any()
    if por_cnpj:
        chave_portal, chave_pncp = doc_portal, doc_pncp
        criterio = "CNPJ do fornecedor e, dentro dele, ano e número (ou valor) do contrato"
    else:
        chave_portal = ctr["CONTRATADA_NORM"].fillna("")
        chave_pncp = alvo["fornecedor_norm"].fillna("")
        criterio = ("nome normalizado do fornecedor — aproximado, porque um dos "
                    "lados não publicou CNPJ")
    pf_fora = int((chave_portal.eq("")).sum())
    no_pncp = set(chave_pncp[chave_pncp.ne("")])
    raizes_pncp = {c[:8] for c in no_pncp if len(c) == 14}

    partes = ctr["NUMERO_CONTRATO"].map(_partes_numero) if "NUMERO_CONTRATO" in ctr else \
        pd.Series([(None, None, False)] * len(ctr), index=ctr.index)
    num = partes.map(lambda t: t[0])
    ano_num = partes.map(lambda t: t[1])
    eh_ata = partes.map(lambda t: t[2]).astype(bool)
    ini = ctr["INI"] if "INI" in ctr else pd.Series(pd.NaT, index=ctr.index)
    ano_ini = pd.to_datetime(ini, errors="coerce").dt.year
    ano_ctr = ano_num.where(ano_num.notna(), ano_ini)
    valor = pd.to_numeric(coluna(ctr, "VALOR", 0), errors="coerce").fillna(0)

    # chaves individuais do PNCP: (cnpj, ano, número) e valores por cnpj
    pn = alvo.assign(_k=chave_pncp)
    pn_num = coluna(pn, "numeroContrato", "").map(lambda x: _partes_numero(x)[0])
    pn_ano = pd.to_numeric(coluna(pn, "anoContrato", None), errors="coerce")
    pn_val = pd.to_numeric(coluna(pn, "valorGlobal", 0), errors="coerce").fillna(0)
    chaves_pncp = {(k, int(a), n) for k, a, n in zip(pn["_k"], pn_ano, pn_num)
                   if k and pd.notna(a) and n is not None}
    valores_pncp: dict[str, list[float]] = {}
    for k, v in zip(pn["_k"], pn_val):
        if k:
            valores_pncp.setdefault(k, []).append(float(v))

    def casou_contrato(k, a, n, v) -> bool:
        if k and pd.notna(a) and n is not None and (k, int(a), n) in chaves_pncp:
            return True
        return any(v > 0 and abs(v - x) <= 0.01 * max(v, x) for x in valores_pncp.get(k, []))

    obrigatorio = pd.Timestamp("2023-12-30")
    convivencia_ini = pd.Timestamp("2021-08-01")
    em_conv = ini.lt(obrigatorio) & ini.ge(convivencia_ini)
    cnpjs_conv = set(chave_portal[em_conv & chave_portal.ne("")])

    situacao = []
    for k, a, n, v, ata in zip(chave_portal, ano_ctr, num, valor, eh_ata):
        if not k:
            situacao.append("sem CNPJ no portal (pessoa física ou campo vazio)")
        elif ata:
            situacao.append("ata")
        elif k not in no_pncp:
            if len(k) == 14 and k[:8] in raizes_pncp:
                situacao.append("provável correspondente (filial/matriz) no PNCP")
            elif k in cnpjs_conv:
                situacao.append("possível contrato decorrente de certame anterior a "
                                "30/12/2023 (art. 191, p.u.) — conferir regime no edital")
            else:
                situacao.append("ausente")
        elif casou_contrato(k, a, n, v):
            situacao.append("casado")
        else:
            situacao.append("fornecedor presente no PNCP com outro contrato")
    ctr = ctr.assign(situacao=situacao, CONTRATADA_CNPJ14=chave_portal)
    pos = ctr[ini.ge(obrigatorio)]
    cols_ev = [c for c in ("NUMERO_CONTRATO", "CONTRATADA", "CONTRATADA_CNPJ14", "VALOR",
                           "DATA_INICIO_VIGENCIA", "situacao") if c in ctr.columns]

    def soma(sub):
        return float(pd.to_numeric(sub["VALOR"], errors="coerce").fillna(0).sum())

    # ---- atas: cadastro próprio no PNCP
    atas_portal = pos[pos["situacao"].eq("ata")]
    atas_pncp = df(staged, "pncp_atas")
    if not atas_portal.empty:
        casadas = pd.Series(False, index=atas_portal.index)
        if not atas_pncp.empty and "numeroAtaRegistroPreco" in atas_pncp.columns:
            chaves_atas = {(int(a), _partes_numero(n)[0]) for a, n in zip(
                pd.to_numeric(coluna(atas_pncp, "anoAta", None), errors="coerce"),
                coluna(atas_pncp, "numeroAtaRegistroPreco", ""))
                if pd.notna(a) and _partes_numero(n)[0] is not None}
            casadas = pd.Series([(int(a), n) in chaves_atas if pd.notna(a) and n is not None else False
                                 for a, n in zip(ano_ctr.loc[atas_portal.index], num.loc[atas_portal.index])],
                                index=atas_portal.index)
        pendentes = atas_portal[~casadas]
        if not pendentes.empty:
            motivo = ("o cadastro de atas do PNCP (`/v1/atas`) não foi coletado nesta rodada"
                      if atas_pncp.empty else
                      "não têm ata com o mesmo ano e número no cadastro de atas do PNCP")
            achados.append(Finding(
                id="contratos-atas-nao-cruzadas", frente=FRENTE, gravidade="info",
                titulo=(f"{len(pendentes)} ata(s) de registro de preços do portal a partir de "
                        f"30/12/2023 ({fmt_brl(soma(pendentes))}) — {motivo}"),
                descricao=(
                    "Atas de registro de preços ficam FORA do achado de contratos "
                    "ausentes do PNCP: o PNCP as publica em cadastro próprio, não em "
                    "`/v1/contratos`, e cruzá-las com contratos acusaria uma ausência "
                    "que é só uma prateleira diferente. Quando o cadastro de atas foi "
                    "coletado, o casamento é por ano e número da ata. O que fica aqui "
                    f"é o que não casou. Fonte: portal do Crea-SP (contratos) × PNCP; "
                    f"exercícios {int(ano_ini.loc[pendentes.index].min())}–"
                    f"{int(ano_ini.loc[pendentes.index].max())}."),
                evidencia=pendentes[cols_ev].sort_values("VALOR", ascending=False).head(100),
                metricas={"atas": int(len(pendentes)), "valor": round(soma(pendentes), 2),
                          "cadastro_de_atas_coletado": not atas_pncp.empty},
                fontes=["contratos", "pncp_atas"],
            ))

    # ---- contratos (não atas) firmados a partir de 30/12/2023
    ausentes = pos[pos["situacao"].eq("ausente")]
    filial = pos[pos["situacao"].str.startswith("provável correspondente")]
    art191 = pos[pos["situacao"].str.startswith("possível contrato decorrente")]
    outro = pos[pos["situacao"].eq("fornecedor presente no PNCP com outro contrato")]
    if not (ausentes.empty and filial.empty and art191.empty):
        anos_pos = ano_ini.loc[pos.index].dropna()
        periodo = (f"{int(anos_pos.min())}–{int(anos_pos.max())}"
                   if not anos_pos.empty and anos_pos.min() != anos_pos.max()
                   else (str(int(anos_pos.min())) if not anos_pos.empty else "—"))
        listados = pd.concat([ausentes, art191, filial])
        achados.append(Finding(
            id="contratos-ausentes-pncp", frente=FRENTE,
            gravidade="media" if not ausentes.empty else "info",
            titulo=(f"{len(ausentes)} contrato(s) firmados a partir de 30/12/2023 "
                    f"({fmt_brl(soma(ausentes))}, {periodo}) sem correspondente no PNCP"
                    + (f" — mais {len(art191) + len(filial)} com dúvida rotulada "
                       f"({fmt_brl(soma(art191) + soma(filial))})"
                       if not (art191.empty and filial.empty) else "")),
            descricao=(
                "A Lei 14.133/2021 obriga a divulgação no Portal Nacional de "
                "Contratações Públicas; a obrigação alcança o que foi contratado sob "
                "a nova lei e tornou-se a única via em 30/12/2023, fim da "
                "convivência com a Lei 8.666 (art. 193, II).\n\n"
                f"O cruzamento é por **{criterio}**, entre o portal próprio do "
                "conselho e o que o PNCP devolve para o CNPJ do órgão. Ausência é "
                "falha de publicação — não diz nada sobre a legalidade da "
                "contratação.\n\n"
                "**O que este número não prova, e como cada dúvida está rotulada "
                "na coluna `situacao`:**\n"
                f"* {len(art191)} contrato(s) ({fmt_brl(soma(art191))}) têm ata ou "
                "contrato do MESMO CNPJ no portal entre ago/2021 e 30/12/2023. "
                "Contrato decorrente de licitação ou ata publicada sob a Lei 8.666 "
                "continua regido por ela durante toda a vigência (Lei 14.133, art. "
                "191, parágrafo único) e não tinha de ir ao PNCP — o edital diz o "
                "regime, e é lá que se confere.\n"
                f"* {len(filial)} contrato(s) ({fmt_brl(soma(filial))}) têm outro "
                "estabelecimento (filial/matriz) do mesmo CNPJ-raiz no PNCP: "
                "provável correspondente.\n"
                f"* {len(outro)} contrato(s) ({fmt_brl(soma(outro))}) são de fornecedor "
                "PRESENTE no PNCP com outro contrato, sem casamento individual por "
                "ano/número ou valor — não contam como ausentes.\n"
                f"* {pf_fora} registro(s) do portal sem CNPJ (pessoa física ou campo "
                "vazio) ficaram fora do cruzamento.\n"
                "* Atas de registro de preços estão em achado próprio.\n"
                "* Contrato publicado sob outro CNPJ ou unidade compradora do "
                "conselho não é alcançado: a consulta cobre o CNPJ do órgão."),
            evidencia=listados[cols_ev].sort_values("VALOR", ascending=False).head(100),
            base_legal="Lei 14.133/2021, arts. 54 e 94; art. 191, parágrafo único; "
                       "art. 193, II.",
            como_verificar=("Para cada contrato: (1) localizar no portal a ata ou "
                            "licitação de origem e ler no edital o regime declarado "
                            "(8.666 ou 14.133); (2) se 14.133, consultar pncp.gov.br pelo "
                            "CNPJ do conselho, ano e número; (3) se não constar, pedir o "
                            "comprovante de divulgação (art. 94)."),
            metricas={"contratos": int(len(ausentes)), "valor": round(soma(ausentes), 2),
                      "possivel_art_191": {"contratos": int(len(art191)),
                                           "valor": round(soma(art191), 2)},
                      "provavel_filial_matriz": {"contratos": int(len(filial)),
                                                 "valor": round(soma(filial), 2)},
                      "fornecedor_presente_outro_contrato": {"contratos": int(len(outro)),
                                                             "valor": round(soma(outro), 2)},
                      "pf_fora_do_cruzamento": pf_fora,
                      "criterio_de_casamento": criterio, "corte_da_obrigacao": "2023-12-30",
                      "periodo": periodo},
            fontes=["contratos", "pncp_contratos"],
        ))

    # ---- convivência (contexto, como antes)
    ausente_forn = chave_portal.ne("") & ~chave_portal.isin(no_pncp)
    convivencia = ctr[ausente_forn & em_conv]
    if not convivencia.empty:
        achados.append(Finding(
            id="contratos-fora-do-pncp-convivencia", frente=FRENTE, gravidade="info",
            titulo=(f"{len(convivencia)} contrato(s) de 2021 a 2023 "
                    f"({fmt_brl(soma(convivencia))}) não estão no PNCP — e não "
                    "precisavam estar"),
            descricao=(
                "Contexto, **não achado**. Estes contratos começaram entre a entrada "
                "do PNCP no ar (ago/2021) e o fim da convivência com a Lei 8.666 "
                "(30/12/2023). Quem contratou sob a lei antiga não tinha obrigação de "
                "publicá-los no PNCP, e a ausência deles ali não é irregularidade.\n\n"
                "A lista existe por um motivo só: é aqui que está a maior parte do "
                "dinheiro da carteira, e quem cruzar as duas bases sem olhar a data "
                "vai concluir 'falha de transparência' sobre contrato que a lei da "
                "época mandava publicar exatamente onde ele está — no portal próprio."),
            evidencia=convivencia[cols_ev].sort_values("VALOR", ascending=False).head(100),
            base_legal="Lei 14.133/2021, art. 193, II.",
            metricas={"contratos": int(len(convivencia)), "valor": round(soma(convivencia), 2)},
            fontes=["contratos", "pncp_contratos"],
        ))

    # ---- só no PNCP
    no_portal = set(chave_portal[chave_portal.ne("")])
    so = pn[pn["_k"].ne("") & ~pn["_k"].isin(no_portal)].copy()
    if not so.empty:
        tipo_pessoa = coluna(so, "tipoPessoa", "").fillna("").astype(str).str.upper()
        so = so[~tipo_pessoa.eq("PF") & so["_k"].str.len().eq(14)]
    if not so.empty:
        anos_portal = set(int(a) for a in ano_ini.dropna().unique())
        vig = pd.to_datetime(coluna(so, "vigenciaInicio", None), errors="coerce")
        so["_ano_vig"] = vig.dt.year
        nao_verificavel = so["_ano_vig"].notna() & ~so["_ano_vig"].isin(anos_portal)
        tipo = coluna(so, "tipoContrato_nome", "").fillna("").astype(str)
        empenho = tipo.str.upper().str.contains("EMPENHO", na=False)
        so["situacao"] = "sem contrato do mesmo CNPJ no portal"
        so.loc[empenho, "situacao"] = "publicado no PNCP como empenho (o portal publica empenhos em outro conjunto)"
        so.loc[nao_verificavel, "situacao"] = "exercício não coletado do portal — não verificável"
        firmes = so[~empenho & ~nao_verificavel]
        vg = pd.to_numeric(coluna(so, "valorGlobal", 0), errors="coerce").fillna(0)
        anos_so = so["_ano_vig"].dropna()
        periodo = (f"{int(anos_so.min())}–{int(anos_so.max())}" if not anos_so.empty else "—")
        achados.append(Finding(
            id="contratos-so-no-pncp", frente=FRENTE, gravidade="baixa",
            titulo=(f"{len(so)} contrato(s) de {so['_k'].nunique()} fornecedor(es) no PNCP "
                    f"({fmt_brl(float(vg.sum()))}, {periodo}) sem contrato do mesmo CNPJ no "
                    f"portal próprio — {int(empenho.sum())} publicados no PNCP como empenho, "
                    f"{int(nao_verificavel.sum())} em exercício que o portal não devolveu, "
                    f"{len(firmes)} sem rastro"),
            descricao=(
                "Situação inversa: publicado no PNCP e sem contrato do mesmo CNPJ no "
                "portal próprio. Três leituras, separadas na coluna `situacao`: o que "
                "o PNCP registra como **empenho** o portal publica no conjunto de "
                "empenhos, não em contratos — não é omissão; o que tem vigência em "
                "exercício que o portal **não devolveu** nesta rodada não pode ser "
                "verificado; e o resto é o que de fato não tem rastro em contratos. "
                "Casamento por CNPJ; pessoas físicas ficam fora porque o portal só "
                "expõe CNPJ. Fonte: PNCP (contratos do órgão) × portal do Crea-SP "
                f"(contratos {min(anos_portal) if anos_portal else '—'}–"
                f"{max(anos_portal) if anos_portal else '—'})."),
            evidencia=so[[c for c in ("numeroContrato", "fornecedor_nome", "_k", "tipoContrato_nome",
                                      "objeto", "valorGlobal", "vigenciaInicio", "vigenciaFim",
                                      "situacao") if c in so.columns]]
            .rename(columns={"_k": "fornecedor_ni"}).head(100),
            metricas={"contratos": int(len(so)), "fornecedores": int(so["_k"].nunique()),
                      "valor": round(float(vg.sum()), 2), "como_empenho": int(empenho.sum()),
                      "exercicio_nao_coletado": int(nao_verificavel.sum()),
                      "sem_rastro": int(len(firmes)), "periodo": periodo},
            fontes=["pncp_contratos", "contratos"],
        ))
    return achados


def _achados_modalidade_origem(pncp: pd.DataFrame, lic: pd.DataFrame) -> list[Finding]:
    """De que certame veio cada contrato? O portal do conselho não publica
    número de licitação em nenhum dos seus contratos; o PNCP publica, e liga
    contrato → compra pelo `numeroControlePNCPCompra`. Cruzando com as
    contratações, cada contrato ganha a MODALIDADE de origem — pregão,
    dispensa, inexigibilidade — que é a pergunta que "100% sem licitação
    vinculada" deixava sem resposta.
    """
    achados: list[Finding] = []
    if lic.empty:
        return achados
    mods = lic.copy()
    mods["modalidadeNome"] = coluna(mods, "modalidadeNome", "").fillna("").astype(str)
    valor_est = pd.to_numeric(coluna(mods, "valorTotalEstimado", 0), errors="coerce").fillna(0)
    por_mod = (mods.assign(_v=valor_est).groupby("modalidadeNome")
               .agg(contratacoes=("_v", "size"), valor_estimado=("_v", "sum"))
               .sort_values("valor_estimado", ascending=False).reset_index())
    total_v = float(por_mod["valor_estimado"].sum())
    diretas = por_mod[por_mod["modalidadeNome"].str.upper().str.contains(
        "DISPENSA|INEXIGIB", regex=True, na=False)]
    pct_direta = 100 * float(diretas["valor_estimado"].sum()) / total_v if total_v else 0.0
    pct_cont = 100 * float(diretas["contratacoes"].sum()) / len(mods) if len(mods) else 0.0
    anos_c = pd.to_numeric(coluna(mods, "_ano", None), errors="coerce").dropna()
    faixa = (f"ano da compra {int(anos_c.min())}–{int(anos_c.max())}" if not anos_c.empty else "")
    achados.append(Finding(
        id="licitacoes-pncp-rol", frente=FRENTE,
        gravidade="media" if pct_direta >= 50 else "info",
        titulo=(f"O rol de contratações está no PNCP: {len(mods)} publicadas desde jan/"
                f"{config.LICITACOES_PNCP_DESDE}" + (f" ({faixa})" if faixa else "")
                + f" — {pct_direta:.0f}% do valor estimado e {pct_cont:.0f}% das "
                "contratações por dispensa ou inexigibilidade"),
        descricao=(
            "O rol de licitações não veio do portal próprio do conselho, e seus "
            "contratos não informam certame de origem. O PNCP publica as "
            "contratações sob a Lei 14.133 — obrigatórias desde 30/12/2023; as de "
            "2023 já estão lá porque o conselho passou a usar a nova lei antes do "
            "fim da convivência (art. 191). A varredura é pela data de publicação "
            f"no PNCP, de jan/{config.LICITACOES_PNCP_DESDE} até a coleta; o ano da "
            "compra pode ser anterior à publicação.\n\n"
            "A tabela é o rol por modalidade. **Contratação direta não é "
            "irregularidade**: dispensa e inexigibilidade têm hipóteses legais "
            "(arts. 74 e 75 da Lei 14.133). O que este número mostra é a "
            "proporção — e a lista, com objeto e valor, é o ponto de partida "
            "para conferir a hipótese invocada em cada uma.\n\n"
            "Varredura por modalidade: " + ", ".join(config.PNCP_MODALIDADES_CONSELHO.values()) +
            ". Modalidades fora dessa lista (leilão, concurso, diálogo "
            "competitivo) não foram consultadas."),
        evidencia=por_mod,
        base_legal="Lei 14.133/2021, arts. 54, 74, 75 e 94.",
        como_verificar="Abrir cada contratação em pncp.gov.br pelo "
                       "`numeroControlePNCP` e ler o amparo legal declarado.",
        metricas={"contratacoes": int(len(mods)),
                  "valor_estimado_total": round(total_v, 2),
                  "pct_valor_contratacao_direta": round(pct_direta, 1),
                  "pct_contagem_contratacao_direta": round(pct_cont, 1),
                  "por_ano": {int(a): int(n) for a, n in
                              anos_c.astype(int).value_counts().sort_index().items()},
                  "modalidades_consultadas": list(config.PNCP_MODALIDADES_CONSELHO.values())},
        fontes=["licitacoes_pncp"],
    ))

    if pncp.empty or "numeroControlePNCPCompra" not in pncp.columns:
        return achados
    alvo = pncp[pncp.get("orgao_papel") == "alvo"].copy()
    chave = alvo["numeroControlePNCPCompra"].fillna("").astype(str)
    lic_k = mods.set_index(mods["numeroControlePNCP"].fillna("").astype(str))
    lic_k = lic_k[~lic_k.index.duplicated()]
    alvo["modalidade_origem"] = chave.map(lic_k["modalidadeNome"]).fillna("")
    alvo["amparo_legal"] = chave.map(coluna(lic_k, "amparoLegalNome", "")).fillna("")
    com = alvo[alvo["modalidade_origem"].ne("")]
    if com.empty:
        return achados
    vg = pd.to_numeric(com["valorGlobal"], errors="coerce").fillna(0)
    resumo = (com.assign(_v=vg).groupby("modalidade_origem")
              .agg(contratos=("_v", "size"), valor_global=("_v", "sum"))
              .sort_values("valor_global", ascending=False).reset_index())
    direta = com[com["modalidade_origem"].str.upper().str.contains("DISPENSA|INEXIGIB", regex=True)]
    vd = float(pd.to_numeric(direta["valorGlobal"], errors="coerce").fillna(0).sum())
    vt = float(vg.sum())
    achados.append(Finding(
        id="contratos-modalidade-de-origem", frente=FRENTE,
        gravidade="media" if vt and vd / vt >= 0.5 else "info",
        titulo=(f"{len(com)} de {len(alvo)} contratos no PNCP têm a compra de origem "
                f"identificada — {100 * vd / vt if vt else 0:.0f}% do valor veio de "
                "dispensa ou inexigibilidade"),
        descricao=(
            "Ligação contrato → compra pelo `numeroControlePNCPCompra` do próprio "
            "PNCP, e da compra à modalidade. É a informação que o portal do "
            "conselho não dá em nenhum dos seus contratos.\n\n"
            "Contratação direta é legal quando cabe numa hipótese dos arts. 74 "
            "e 75 da Lei 14.133; a coluna `amparo_legal` traz a hipótese "
            "declarada. O que merece conferência é o par objeto × amparo — um "
            "serviço continuado de valor alto sob inexigibilidade pede a "
            "justificativa de fornecedor exclusivo."),
        evidencia=com[["numeroContrato", "fornecedor_nome", "objeto", "valorGlobal",
                       "modalidade_origem", "amparo_legal", "numeroControlePNCPCompra"]]
        .sort_values("valorGlobal", ascending=False).head(100),
        base_legal="Lei 14.133/2021, arts. 74 e 75 (contratação direta); art. 94 (divulgação).",
        metricas={"contratos_com_origem": int(len(com)), "contratos_pncp": int(len(alvo)),
                  "valor_contratacao_direta": round(vd, 2), "valor_total": round(vt, 2),
                  "resumo_por_modalidade": resumo.to_dict("records")},
    ))
    return achados


def _achados_razao(ctr: pd.DataFrame, adt: pd.DataFrame, pg: pd.DataFrame,
                   sem_carteira_confiavel: set[str] | None = None
                   ) -> list[Finding]:
    """O contrato diz quanto foi comprometido; o pagamento, quanto saiu."""
    achados: list[Finding] = []
    piso = config.PISO_ANALISE_FORNECEDOR

    # Fonte ausente não vira estimativa nem silêncio: vira um achado que diz
    # que a medida não pôde ser feita (e por quê).
    if ctr.empty or pg.empty or "FAVORECIDO_NORM" not in pg.columns:
        achados.append(Finding(
            id="contratos-razao-pago-indisponivel", frente=FRENTE,
            gravidade="info",
            titulo=("Razão pago/contratado por fornecedor: análise "
                    f"INDISPONÍVEL ({len(ctr)} contrato(s) e {len(pg)} "
                    "pagamento(s) coletados)"),
            descricao=(
                "A comparação entre o valor contratado e o valor efetivamente "
                "pago a cada fornecedor exige as DUAS fontes: o rol de "
                "contratos e o rol de pagamentos. Pelo menos uma delas veio "
                "vazia nesta coleta, e por isso a medida não foi feita — "
                "nenhum número foi estimado no lugar dela. **A ausência desta "
                "análise não é sinal de regularidade**: é sinal de que o "
                "portal não entregou o insumo."),
            metricas={"contratos": int(len(ctr)), "pagamentos": int(len(pg))},
            como_verificar=("Pedir a relação de contratos e a de pagamentos do "
                            "período em formato aberto, com CNPJ da contratada "
                            "em ambas."),
        ))
        return achados

    inicio, corte = _janela_pagamentos(pg)
    razao, contratos_base, pagos_base = _pago_vs_contratado(
        ctr, adt, pg, inicio, corte)
    if razao.empty:
        return achados
    fora_por_valor = 0
    if sem_carteira_confiavel:
        antes = len(razao)
        razao = razao[~razao["fornecedor"].isin(sem_carteira_confiavel)]
        fora_por_valor = antes - len(razao)
    if razao.empty:
        return achados
    # Piso de materialidade: fornecedor pequeno faz a razão explodir sem que a
    # diferença signifique nada (R$ 1.000 contratados e R$ 50 mil pagos dão 50x
    # e R$ 49 mil de divergência).
    base = razao[(razao["contratado"] >= piso) | (razao["pago"] >= piso)]
    if base.empty:
        return achados

    # A qualidade da JUNÇÃO antes de qualquer acusação: a única chave entre as
    # duas bases é o nome normalizado, e quando ele quase não casa, "pago = 0"
    # deixa de medir execução e passa a medir a chave. Isso é um achado de
    # metodologia — UM —, não uma lista de contratadas a explicar. É a mesma
    # decisão já tomada em `contratos-vinculo-certame-ausente`.
    casadas = int((base["pagamentos"] > 0).sum())
    taxa_casamento = 100.0 * casadas / len(base)
    juncao_confiavel = taxa_casamento >= TAXA_MINIMA_CASAMENTO_NOME
    if not juncao_confiavel:
        achados.append(Finding(
            id="contratos-juncao-por-nome-sem-correspondencia", frente=FRENTE,
            gravidade="media",
            titulo=(f"Só {taxa_casamento:.1f}% das contratadas ({casadas} de "
                    f"{len(base)}) têm algum pagamento localizável pelo nome"),
            descricao=(
                "A única chave que liga o rol de contratos ao rol de "
                "pagamentos é o nome normalizado da contratada — o portal não "
                "publica o CNPJ nos dois lados. Quando esse nome casa em "
                "quase nenhum caso, o zero da coluna `pago` não mede execução "
                "contratual: mede a chave.\n\n"
                "Por isso **a lista de contratos não executados não foi "
                "emitida nesta rodada**. Publicá-la seria apresentar "
                f"{len(base) - casadas} contratadas como quem recebeu contrato "
                "e não executou, quando o que os dados mostram é que a "
                "ferramenta não conseguiu encontrar os pagamentos delas.\n\n"
                "Este achado é mais grave que a lista que ele substitui: sem "
                "identificador comum entre contrato e pagamento, a execução "
                "contratual do conselho não é auditável a partir dos dados "
                "abertos por ninguém, nem por esta ferramenta.\n\n" + _RESSALVA),
            evidencia=base[_COLS_RAZAO].head(200),
            metricas={"taxa_casamento_pct": round(taxa_casamento, 1),
                      "contratadas_com_pagamento": casadas,
                      "contratadas": int(len(base))},
            base_legal="Lei 12.527/2011, art. 8º, §3º, II e III (dados abertos "
                       "em formato que permita cruzamento).",
            como_verificar=("Pedir o rol de contratos e o de pagamentos com o "
                            "CNPJ da contratada em AMBOS, ou o relatório de "
                            "execução contratual por fornecedor."),
        ))

    # ---------------------------------------- 1. pago muito acima do contratado
    # Denominador pelo TETO: com o aditivo ambíguo somado, que é a leitura mais
    # favorável ao órgão. Quem sobra recebeu o dobro sob qualquer interpretação.
    excesso = base[(base["contratado_teto"] > 0) &
                   (base["razao_no_teto"] >= RAZAO_EXECUCAO_ALTA) &
                   (base["divergencia_no_teto"] >= piso)]
    if not excesso.empty:
        alem = float(excesso["divergencia_no_teto"].sum())
        maior = excesso.sort_values("divergencia_no_teto", ascending=False).iloc[0]
        achados.append(Finding(
            id="contratos-pago-acima-do-contratado", frente=FRENTE,
            gravidade="alta",
            titulo=(f"{len(excesso)} fornecedor(es) com pagamento de pelo menos "
                    f"o dobro do que foi contratado — {fmt_brl(alem)} pagos "
                    "além dos instrumentos"),
            descricao=(
                "Somando todos os contratos de cada fornecedor **e todos os "
                "aditivos de valor**, e comparando com o que lhe foi pago a "
                "partir do início da primeira vigência conhecida, estes "
                "fornecedores receberam pelo menos o dobro do contratado. O "
                f"maior caso é {maior['fornecedor']}: "
                f"{fmt_brl(float(maior['contratado_teto']))} contratados e "
                f"{fmt_brl(float(maior['pago']))} pagos "
                f"({maior['razao_no_teto']:.1f}x).\n\n"
                "Três explicações cabem no mesmo número, e só o extrato "
                "resolve qual é: aditivo celebrado e não publicado no portal; "
                "contrato anterior à janela coletada ainda em execução; ou "
                "pagamento sem cobertura contratual. As três são perguntas "
                "legítimas, e a terceira é a que a Lei 4.320 proíbe.\n\n"
                "A lista está ordenada pela DIVERGÊNCIA EM REAIS, não pela "
                "razão: um contrato de R$ 1.000 pago 50 vezes lidera qualquer "
                "ranking por razão e não move o orçamento. A coluna usada é "
                "`contratado_teto` — o valor contratado somado a TODOS os "
                "aditivos, inclusive os de valor ambíguo, que é a leitura mais "
                "favorável ao órgão. Quem aparece aqui recebeu o dobro sob "
                "qualquer interpretação do cadastro.\n\n" + _RESSALVA),
            evidencia=(excesso[_COLS_EXCESSO]
                       .sort_values("divergencia_no_teto",
                                    ascending=False).head(200)),
            base_legal=("Lei 14.133/2021, art. 95, §2º (é nulo o contrato "
                        "verbal com a Administração) e art. 125 (acréscimo "
                        "limitado a 25%, 50% para reforma); Lei 8.666/1993, "
                        "art. 60, parágrafo único, e art. 65, §1º; Lei "
                        "4.320/1964, arts. 60 a 63 (despesa exige empenho "
                        "prévio e liquidação regular antes do pagamento)."),
            como_verificar=(
                "Pedir, para cada fornecedor da lista, a relação completa de "
                "contratos e termos aditivos vigentes no período e o extrato "
                "de empenhos que lastreiam os pagamentos. Um contrato anterior "
                "à janela coletada explica parte dos casos; o que sobrar é "
                "pagamento a justificar."),
            # A chave é `contratado_teto` e não `contratado`: o número somado é
            # o do denominador que o achado usou, e o nome tem de dizer isso —
            # senão ele é lido contra o `contratado_total` do panorama, que é
            # outra conta.
            metricas={"fornecedores": int(len(excesso)),
                      "pago_alem_do_contratado": round(alem, 2),
                      "contratado_teto": round(
                          float(excesso["contratado_teto"].sum()), 2),
                      "pago": round(float(excesso["pago"].sum()), 2),
                      "razao_maxima": round(
                          float(excesso["razao_no_teto"].max()), 2)},
        ))

    # ------------------------- 2. contrato encerrado com execução muito abaixo
    ociosos = base[base["encerrada"] & base["execucao_observavel"] &
                   (base["contratado"] >= piso) &
                   (base["razao_pago_contratado"] <= RAZAO_EXECUCAO_BAIXA) &
                   ((base["contratado"] - base["pago"]) >= piso)].copy()
    if not juncao_confiavel:
        ociosos = ociosos.iloc[0:0]
    if not ociosos.empty:
        ociosos["nao_executado"] = (ociosos["contratado"] -
                                    ociosos["pago"]).round(2)
        reservado = float(ociosos["nao_executado"].sum())
        sem_pagamento = int((ociosos["pagamentos"] == 0).sum())
        achados.append(Finding(
            id="contratos-contratado-nao-executado", frente=FRENTE,
            gravidade="media",
            titulo=(f"{len(ociosos)} contratada(s) com vigência encerrada e "
                    "menos da metade do contrato executada — "
                    f"{fmt_brl(reservado)} contratados e não pagos"),
            descricao=(
                "Só entram aqui contratadas cujos contratos JÁ TERMINARAM "
                "(todos com data final publicada e anterior ao último "
                "pagamento conhecido) — contrato em curso tem execução menor "
                "por construção, e incluí-lo seria fabricar achado. E só "
                "entram as que cabem INTEIRAS na janela de pagamentos "
                "coletada: um contrato que começou antes do primeiro "
                "pagamento publicado teve parte da execução fora do alcance "
                "destes dados, e o zero dele seria a janela de coleta "
                "aparecendo como se fosse achado.\n\n"
                "Contrato muito acima do que se executou não é ilegal por si. "
                "Ele importa por três consequências práticas: o valor "
                "contratado é o que define a modalidade de licitação e a "
                "exigência de qualificação econômica — superdimensionar "
                "restringe quem pode disputar; ele vira referência de preço "
                "para as contratações seguintes; e imobiliza dotação que "
                "poderia custear outra coisa. Ata de registro de preço é a "
                "exceção legítima e frequente: ali o valor é estimativa e não "
                "compromisso — vale conferir a natureza do instrumento antes "
                "de cobrar explicação."
                + (f"\n\nDe {len(ociosos)} contratada(s), {sem_pagamento} não "
                   "tiveram NENHUM pagamento casado pelo nome. Isso reforça a "
                   "hipótese de contrato não executado, mas também é "
                   "exatamente o que aconteceria se a razão social estivesse "
                   "grafada de outro jeito na folha de pagamentos."
                   if sem_pagamento else "")
                + "\n\n" + _RESSALVA),
            evidencia=(ociosos[_COLS_RAZAO + ["nao_executado"]]
                       .sort_values("nao_executado", ascending=False).head(200)),
            base_legal=("Lei 14.133/2021, art. 18, §1º, IV (estimativa das "
                        "quantidades com memória de cálculo) e art. 23 "
                        "(estimativa do valor da contratação); Lei 8.666/1993, "
                        "art. 7º, §4º (vedados quantitativos que não "
                        "correspondam às previsões reais)."),
            como_verificar=(
                "Para os contratos listados, pedir as medições/ordens de "
                "serviço efetivamente executadas, o termo de encerramento com "
                "o saldo não executado e a memória de cálculo que sustentou a "
                "estimativa original. Conferir também se o instrumento é ata "
                "de registro de preço, em que o valor é estimado."),
            metricas={"fornecedores": int(len(ociosos)),
                      "contratado": round(float(ociosos["contratado"].sum()), 2),
                      "pago": round(float(ociosos["pago"].sum()), 2),
                      "nao_executado": round(reservado, 2),
                      "sem_pagamento_correspondente": sem_pagamento},
        ))

    # ----------------------------------------------------- 3. panorama da razão
    contratado_total = float(base["contratado"].sum())
    pago_total = float(base["pago"].sum())
    sem_valor = int((base["contratado"] <= 0).sum())
    observaveis = int(base["execucao_observavel"].sum())
    nomes = set(base["fornecedor"])

    def _serie_anual(d: pd.DataFrame, data_col: str, valor_col: str):
        if d.empty or valor_col not in d.columns:
            return pd.Series(dtype=float)
        x = d[d["_nome"].isin(nomes)].copy()
        if x.empty:
            return pd.Series(dtype=float)
        x["_ano"] = pd.to_datetime(coluna(x, data_col, pd.NaT),
                                   errors="coerce").dt.year
        s = A.serie_anual(x, valor_col)
        return s[valor_col] if not s.empty else pd.Series(dtype=float)

    # As duas curvas correm em relógios DIFERENTES — contrato pelo ano em que a
    # vigência começa, pagamento pelo ano em que o dinheiro saiu. Um contrato
    # plurianual assinado em 2021 aparece inteiro em 2021 e é pago até 2024.
    # É por isso que a comparação vale como acumulado do período, não ano a ano.
    serie = pd.DataFrame({
        "contratado (ano de início da vigência)":
            _serie_anual(contratos_base, "INI", "_contratado"),
        "pago (ano do pagamento)":
            _serie_anual(pagos_base, "_data", "_valor"),
    }).fillna(0.0)
    serie.index.name = "_ano"
    achados.append(Finding(
        id="contratos-pago-vs-contratado", frente=FRENTE, gravidade="info",
        titulo=(f"{len(base)} fornecedor(es) acima de {fmt_brl(piso)}: "
                f"{fmt_brl(pago_total)} pagos contra {fmt_brl(contratado_total)} "
                "contratados"),
        descricao=(
            "A razão entre o que se paga e o que se contrata, fornecedor a "
            "fornecedor, é a medida mais direta de execução contratual que os "
            "dados abertos permitem — e não é publicada por ninguém. Esta é a "
            "tabela completa que sustenta os demais achados desta análise, "
            "ordenada pela divergência em reais: aditivos de valor já somados "
            "ao contratado, contratos da mesma contratada agregados antes da "
            "divisão, e pagamentos anteriores ao início da primeira vigência "
            "conhecida separados na coluna `pago_antes_da_vigencia` (eles "
            "pertencem a contrato que a janela coletada não alcança). A coluna "
            "`contratado_teto` repete o contratado somando também os aditivos "
            "de valor ambíguo — é a leitura mais favorável ao órgão, e é ela "
            "que o achado de excesso usa como denominador.\n\n"
            "A leitura útil é por extremos: razão perto de 1 é execução "
            "normal; muito acima de 1 é execução além do instrumento; muito "
            "abaixo, com a vigência encerrada, é contrato que reservou mais do "
            "que consumiu."
            + (f" Em {sem_valor} fornecedor(es) a razão nem pode ser "
               "calculada, porque o portal não publica valor no contrato — "
               "ausência que é, por si, falha de transparência."
               if sem_valor else "")
            + (f"\n\nDe {len(base)} fornecedor(es), {casadas} têm algum "
               f"pagamento localizado pelo nome ({taxa_casamento:.1f}%) e "
               f"{observaveis} têm a vigência inteira dentro da janela de "
               "pagamentos coletada (coluna `execucao_observavel`) — só estes "
               "podem ser avaliados por subexecução. Nos demais, o `pago` "
               "baixo pode ser execução anterior ao primeiro exercício "
               "coletado, e não contrato ocioso."
               )
            + (f"\n\n{fora_por_valor} contratada(s) ficaram FORA desta tabela "
               "por terem contrato de valor fora de ordem de grandeza (achado "
               "`contratos-valor-implausivel`): com o contrato suspeito "
               "excluído da carteira, o denominador delas ficaria menor que o "
               "que o portal publica e a razão acusaria um excesso produzido "
               "por esta ferramenta, não pelos dados."
               if fora_por_valor else "")
            + "\n\n" + _RESSALVA),
        evidencia=base[_COLS_RAZAO].head(200),
        serie=serie, grafico="bar",
        tabela_anual=serie.round(2).reset_index(),
        metricas={"fornecedores": int(len(base)),
                  "contratado_total": round(contratado_total, 2),
                  "pago_total": round(pago_total, 2),
                  "razao_global": (round(pago_total / contratado_total, 3)
                                   if contratado_total > 0 else None),
                  "pago_antes_da_vigencia":
                      round(float(base["pago_antes_da_vigencia"].sum()), 2),
                  "fornecedores_sem_pagamento":
                      int((base["pagamentos"] == 0).sum()),
                  "fornecedores_sem_valor_contratado": sem_valor,
                  "taxa_casamento_pct": round(taxa_casamento, 1),
                  "fornecedores_execucao_observavel": observaveis,
                  "fornecedores_fora_por_valor_implausivel": fora_por_valor},
        como_verificar=("Pedir o relatório de execução contratual por "
                        "fornecedor (empenhado, liquidado e pago por "
                        "contrato), que é o documento que fecha esta conta "
                        "sem aproximação por nome."),
    ))
    return achados
