"""Frente — o que a despesa DIZ que comprou.

Todas as outras frentes leem o RÓTULO: o nome da conta contábil e o nome do
centro de custo. Nenhuma lia o texto livre que acompanha o pagamento, e é nele
que está a única descrição do que o dinheiro efetivamente pagou.

A pergunta que abriu esta frente veio de quem trabalha no conselho: um evento
de colégio de inspetores, de natureza política, teria sido pago com dinheiro de
fiscalização. Pela conta contábil isso é invisível — a conta diria "Diárias com
Inspetores" e o enquadramento pareceria correto. Só o histórico diz o objeto.

REGRA QUE ORGANIZA A FRENTE INTEIRA: o UNIVERSO vem sempre do rótulo (conta e
centro de custo), nunca do texto livre; o texto livre só responde O QUE se
comprou dentro desse universo. Inverter isso reintroduz o falso positivo que
`common.pagamentos_indenizacao` documenta — o histórico costuma repetir a razão
social do fornecedor, e "CS BRASIL TRANSPORTES DE PASSAGEIROS" entrava num
filtro de passagem por causa do próprio nome. Por isso toda ocorrência casada
no histórico é conferida contra o nome do favorecido e descartada quando o
termo veio de lá.

E o que esta frente NÃO afirma: que um evento seja despesa indevida. Encontro
técnico, capacitação de agente fiscal e reunião de inspetoria podem ser apoio
legítimo à atividade. O que se publica é que o OBJETO descrito não é
fiscalização do exercício profissional e está registrado como se fosse — o que
é pergunta de enquadramento contábil, dirigida ao instrumento (empenho e
processo), e não juízo sobre quem decidiu.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl, topo
from .common import df

FRENTE = "O que a despesa diz que comprou"

# Objeto CERIMONIAL ou ASSOCIATIVO: confraternização, homenagem, solenidade,
# colegiado. Nenhum destes é, por si, fiscalização do exercício profissional.
# A lista é deliberadamente estreita — "reunião" e "evento" sozinhos ficaram de
# fora porque unidade de fiscalização se reúne e faz evento técnico, e um termo
# largo transformaria a frente numa máquina de falso positivo.
# Quatro termos saíram depois de um ataque com históricos plausíveis de
# conselho, e cada um casava algo que não é cerimônia nenhuma:
#   \bposse\b      → "imissao na posse do imovel" (imóvel, não solenidade)
#   \bforum\b      → "rua do forum, 120 - locacao" (endereço)
#   semana d[ae]   → "semana de ferias do servidor"
#   curso de       → casava DENTRO de "conCURSO DE projetos" (faltava \b)
# Os dois primeiros voltaram em forma exigente; os dois últimos foram
# descartados: sinal fraco demais para o dano que causam.
RX_CERIMONIAL = re.compile(
    r"colegio de inspetor|encontro de inspetor|confraterniza|comemora"
    r"|homenage|premia\w*|trofeu|\bplacas? comemorativ|coquetel|\bbuffet\b"
    r"|coffee ?break|\bbrinde|\bfesta\b|jantar de|almoco de|solenidade"
    r"|cerimonia de posse|posse solene|sessao de posse"
    r"|posse d[eo]s? (?:novo|nova|conselheir|diretor|president)"
    r"|aniversario|congresso|seminario|simposio"
    r"|forum (?:de|da|do) \w+|sessao plenaria|\bplenaria\b"
    r"|camara especializada|ornamenta\w*|decoracao de (?:evento|espaco)")

# Capacitação é caso à parte: pode ser apoio direto à fiscalização (treinar o
# agente fiscal) e pode ser evento associativo. Vai medida em separado, e nunca
# somada à anterior, justamente porque a leitura depende do documento.
RX_CAPACITACAO = re.compile(
    r"capacita\w*|treinamento|\bcurso de|workshop|palestra")

# QUAL É O UNIVERSO, e por que ele não pode ser só "função = FISCALIZACAO".
#
# A medida de fiscalização do relatório sai do CENTRO DE CUSTO, e o dataset de
# pagamentos — o único que traz histórico — não publica centro de custo. Pela
# conta contábil, a natureza FISCALIZACAO é quase vazia (R$ 0,00 em cinco dos
# seis exercícios da rodada real). Restringir a ela deixaria a frente muda
# justamente onde ela precisa falar.
#
# O universo honesto é a CONTA QUE FINANCIA QUEM SE DESLOCA: diária e
# locomoção de inspetores e de conselheiros são 47,1% de tudo o que a
# ferramenta chama de fiscalização. Um pagamento nessas contas cujo histórico
# descreve uma confraternização é exatamente a pergunta do operador, e a
# pergunta continua de pé qualquer que seja o centro de custo em que caiu.
RX_CONTA_INSPETOR = re.compile(
    r"\bfiscaliza|\bfisca(?:l|is)\b|\bvistoria|\binspetor")
RX_CONTA_CONSELHEIRO = re.compile(r"\bconselheiro")


def _norm(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).map(T.normalizar)


def _base(staged) -> pd.DataFrame:
    """Pagamentos com histórico legível, já classificados nos dois eixos."""
    pg = df(staged, "pagamentos")
    if pg.empty or "VALOR" not in pg or "HISTORICO" not in pg:
        return pd.DataFrame()
    d = pg.copy()
    # Linha de estorno vem com VALOR negativo. Descartá-la contaria o pagamento
    # revertido pelo valor cheio e faria a reversão sumir — o oposto do que
    # `analytics.ranking_anual` faz no resto do projeto, que soma LÍQUIDO e
    # publica a contagem de estornos ao lado. Aqui vale a mesma regra, e o
    # estorno vai declarado nas métricas.
    d["valor"] = pd.to_numeric(d["VALOR"], errors="coerce").fillna(0)
    d = d[d["valor"] != 0]
    if d.empty:
        return pd.DataFrame()
    d["conta"] = coluna(d, "CONTA_EMPENHO").fillna("").astype(str)
    d["unidade"] = coluna(d, "CENTRO_CUSTO").fillna("").astype(str)
    # UNIVERSO SEMPRE PELO RÓTULO — ver o cabeçalho do módulo.
    d["categoria"] = T.classificar_series(d["conta"])
    d["funcao"] = T.classificar_funcao_series(d["unidade"], d["categoria"])
    d["historico"] = _norm(coluna(d, "HISTORICO"))
    d["favorecido"] = _norm(coluna(d, "NOME_RAZAO_SOCIAL"))
    return d


def _termo_que_sustenta(d: pd.DataFrame, rx: re.Pattern) -> pd.Series:
    """O termo que casou no histórico e NÃO veio da razão social do favorecido.

    Sem descontar o nome de quem recebeu, um pagamento a "CONGRESSO BRASILEIRO
    DE ENGENHARIA LTDA" entra por causa do nome, não do que se comprou — o
    filtro casa consigo mesmo e não seleciona nada, o mesmo defeito que a
    triagem por modelo tinha ao aceitar a rubrica inteira como trecho.

    A comparação é TERMO A TERMO, não do padrão inteiro. A primeira versão
    descartava a linha toda quando qualquer termo aparecia no nome, e assim uma
    confraternização legítima paga a uma empresa chamada "BUFFET X" caía: o
    termo "buffet" estava no nome, e "confraternizacao" — que era a evidência
    de verdade — ia junto.

    Devolve o termo, não um booleano: ele vai publicado ao lado do valor, e é
    por ele que se confere a linha sem acreditar na ferramenta.
    """
    def sobra(historico: str, favorecido: str) -> str:
        no_texto = set(rx.findall(historico or ""))
        if not no_texto:
            return ""
        no_nome = set(rx.findall(favorecido or ""))
        restantes = no_texto - no_nome
        # O mais LONGO é o mais específico: entre "confraterniza" e "colegio de
        # inspetor" no mesmo histórico, quem descreve o objeto é o segundo.
        return max(restantes, key=lambda t: (len(t), t)) if restantes else ""

    return pd.Series([sobra(h, f) for h, f in zip(d["historico"], d["favorecido"])],
                     index=d.index)


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    d = _base(staged)
    if d.empty:
        return achados

    # POR QUE CADA LINHA ENTROU NO UNIVERSO — e a resposta vai publicada.
    #
    # Incluir a conta de conselheiro é a decisão discutível desta frente: pela
    # CONTA sozinha, "Diárias com Conselheiros" não é fiscalização, e enquadrá-la
    # aqui seria o mesmo movimento que a ferramenta audita. Ela entra porque o
    # dataset de pagamentos não publica centro de custo — e é pelo centro de
    # custo que essas diárias compõem 4,7% da medida de fiscalização do
    # relatório. A saída não é esconder a escolha: é declará-la, para que quem
    # ler possa refazer a conta sem essa fatia.
    conta_norm = d["conta"].map(T.normalizar)
    por_regra = {
        "unidade de fiscalização (função)": d["funcao"] == "FISCALIZACAO",
        "conta de fiscalização (natureza)": d["categoria"] == "FISCALIZACAO",
        "conta de inspetor ou fiscal": conta_norm.str.contains(
            RX_CONTA_INSPETOR, regex=True, na=False),
        "conta de conselheiro": conta_norm.str.contains(
            RX_CONTA_CONSELHEIRO, regex=True, na=False),
    }
    dentro = por_regra["unidade de fiscalização (função)"]
    for m in por_regra.values():
        dentro = dentro | m
    fisc = d[dentro]
    if fisc.empty:
        return achados

    total_fisc = float(fisc["valor"].sum())
    com_texto = fisc[fisc["historico"].str.len() >= 4]
    pct_legivel = 100 * float(com_texto["valor"].sum()) / total_fisc if total_fisc else 0.0
    estornos = int((fisc["valor"] < 0).sum())
    valor_estornado = float(fisc.loc[fisc["valor"] < 0, "valor"].sum())

    # Período REAL desta frente — não o do resto do relatório. A medida de
    # fiscalização publicada nas outras frentes sai de `despesas_centro_custo`
    # (2021 em diante, porque o portal recusou os exercícios anteriores); esta
    # sai de `pagamentos`, que cobre mais anos. Chamar o denominador daqui de
    # "% da medida" compararia dois universos diferentes.
    anos = sorted(int(a) for a in fisc.get("_ano", pd.Series(dtype=float)).dropna().unique()) \
        if "_ano" in fisc.columns else []
    periodo = f"{anos[0]}–{anos[-1]}" if len(anos) > 1 else (str(anos[0]) if anos else "período não declarado")

    composicao_universo = pd.DataFrame([
        {"regra de entrada": rotulo,
         "valor": round(float(d.loc[m, "valor"].sum()), 2),
         "pagamentos": int(m.sum())}
        for rotulo, m in por_regra.items()
    ]).sort_values("valor", ascending=False)

    # ---------------------------------------- 1. o denominador, antes de tudo
    #
    # Publicar "R$ X em evento dentro da fiscalização" sem dizer que fração da
    # despesa tem descrição legível é publicar um numerador sem denominador: se
    # só 3% dos pagamentos trazem histórico, o achado mede a amostra e não o
    # órgão. Este achado vem primeiro de propósito.
    achados.append(Finding(
        id="objeto-cobertura-do-historico", frente=FRENTE, gravidade="info",
        titulo=(f"{pct_legivel:.1f}% da despesa ligada à fiscalização traz "
                f"descrição do que foi comprado ({periodo})"),
        descricao=(
            "A conta contábil diz a natureza do gasto (diária, salário, "
            "locação); só o campo de histórico diz o OBJETO. Este achado mede "
            "quanto da despesa ligada à fiscalização tem esse campo preenchido "
            "de forma legível — e é o denominador de tudo o que esta frente "
            "publica.\n\n"
            "**O universo, e a escolha discutível dentro dele.** Entram os "
            "pagamentos cuja unidade ou conta a classificação lê como de "
            "fiscalização, MAIS as contas que financiam quem se desloca em nome "
            "dela. A tabela abaixo diz quanto entrou por cada regra.\n\n"
            "Incluir a conta de CONSELHEIRO é a decisão discutível: pela conta "
            "sozinha ela não é fiscalização, e enquadrá-la aqui seria o mesmo "
            "movimento que esta ferramenta audita. Ela entra porque o dataset "
            "de pagamentos não publica centro de custo — e é pelo centro de "
            "custo que essas diárias compõem parte da medida de fiscalização "
            "do relatório. A escolha vai declarada, com o valor ao lado, para "
            "que a conta possa ser refeita sem ela.\n\n"
            f"**Período: {periodo}**, que é o dos PAGAMENTOS. A medida de "
            "fiscalização das outras frentes sai de outro dataset (despesa por "
            "centro de custo, de 2021 em diante) e cobre menos anos — por isso "
            "os percentuais desta frente são sobre o universo dela, e não sobre "
            "aquela medida. Os dois números não se dividem um pelo outro.\n\n"
            "Histórico em branco não é irregularidade: a lei não exige esse "
            "campo no dado aberto. É limite do que se pode auditar de fora."),
        evidencia=composicao_universo,
        cobertura={"base": float(com_texto["valor"].sum()), "universo": total_fisc,
                   "o_que": "despesa ligada à fiscalização com objeto descrito"},
        metricas={"pct_com_historico": round(pct_legivel, 2),
                  "universo": round(total_fisc, 2),
                  "periodo": periodo,
                  "pagamentos": int(len(fisc)),
                  "pagamentos_com_historico": int(len(com_texto)),
                  "estornos": estornos,
                  "valor_estornado": round(valor_estornado, 2),
                  "conta_de_conselheiro_no_universo": round(float(
                      d.loc[por_regra["conta de conselheiro"], "valor"].sum()), 2)},
    ))

    if com_texto.empty:
        return achados

    # ------------------------------- 2. objeto cerimonial dentro da medida
    for chave, rx, rotulo, nota in (
        ("cerimonial", RX_CERIMONIAL, "cerimonial ou de colegiado",
         "Confraternização, homenagem, solenidade, premiação, plenária e "
         "câmara especializada não são fiscalização do exercício profissional "
         "em nenhuma leitura da Lei 5.194/1966, art. 34."),
        ("capacitacao", RX_CAPACITACAO, "de capacitação",
         "Capacitação PODE ser apoio direto à fiscalização — treinar o agente "
         "fiscal é atividade-meio da atividade-fim. Vai medida em separado e "
         "NUNCA somada à anterior: aqui a leitura depende do documento, e o "
         "número sozinho não decide."),
    ):
        termo = _termo_que_sustenta(com_texto, rx)
        se = com_texto[termo != ""].copy()
        if se.empty:
            continue
        se["termo"] = termo[termo != ""]
        valor = float(se["valor"].sum())
        por_texto = (se.groupby(["historico", "termo"])
                     .agg(valor=("valor", "sum"), pagamentos=("valor", "size"))
                     .sort_values("valor", ascending=False).reset_index())
        achados.append(Finding(
            id=f"objeto-{chave}-em-fiscalizacao", frente=FRENTE,
            gravidade="media" if chave == "cerimonial" else "info",
            titulo=(f"{fmt_brl(valor)} com objeto {rotulo} registrados em conta "
                    f"ou unidade de fiscalização — {100 * valor / total_fisc:.1f}% "
                    f"do universo desta frente ({periodo})"),
            descricao=(
                "Pagamentos cujo rótulo contábil os coloca na fiscalização, mas "
                f"cujo histórico descreve outra coisa. {nota}\n\n"
                "**O que este achado NÃO diz.** Não diz que a despesa seja "
                "indevida, nem imputa intenção a ninguém: o que se aponta é que "
                "o OBJETO descrito não é fiscalização do exercício profissional "
                "e a contabilidade publicada não os separa. É pergunta de "
                "enquadramento, e quem a responde é o empenho e o processo, não "
                "o nome da conta.\n\n"
                "**Como o filtro se protege, e como conferir.** O termo tem de "
                "aparecer no histórico E não aparecer na razão social do "
                "favorecido — sem isso, um pagamento a uma empresa com "
                "'congresso' no nome entraria pelo nome de quem recebeu, e não "
                "pelo que se comprou. A comparação é termo a termo, e o termo "
                "que sustentou cada linha vai publicado na coluna `termo`: é "
                "por ele que se confere a linha sem acreditar na ferramenta."),
            evidencia=topo(por_texto, 100, "valor"),
            serie=A.serie_anual(se, "valor") if "_ano" in se.columns else None,
            base_legal="Lei 5.194/1966, art. 34 (finalidade); Lei 4.320/1964, "
                       "arts. 15 e 63 (discriminação e liquidação da despesa).",
            como_verificar=("Pedir a nota de empenho e o processo de cada "
                            "pagamento listado: é o documento que declara a "
                            "finalidade, e é ele que confirma ou desmente o "
                            "enquadramento."),
            cobertura={"base": valor, "universo": total_fisc,
                       "o_que": "despesa ligada à fiscalização com este objeto"},
            metricas={"valor": round(valor, 2), "pagamentos": int(len(se)),
                      "objetos_distintos": int(len(por_texto)),
                      "pct_do_universo": round(100 * valor / total_fisc, 2),
                      "periodo": periodo,
                      "estornos": int((se["valor"] < 0).sum())},
        ))
    return achados
