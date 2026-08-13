"""Frente — Ler o orçamento pela régua que o próprio conselho declara.

Todas as demais frentes classificam despesa com a **nossa** taxonomia: regex
sobre o nome da conta contábil (`src/queda/taxonomia.py`). Funciona, mas é
instrumento de terceiro — quem escreveu as regras fomos nós, e quando uma
regra não casa, o dinheiro simplesmente desaparece da leitura. Já aconteceu:
`\\bpassagen(s)?\\b` nunca casou "passagem" e `\\bhora extra` nunca casou
"Horas Extras"; R$ 227 milhões ficaram fora da conta por causa disso.

O **plano de contas** é a outra régua, e é a régua oficial: a estrutura que o
conselho publica dizendo como ele mesmo organiza a própria despesa. Ancorar a
leitura nela permite três coisas que a taxonomia sozinha não permite:

1. **Medir o nosso buraco com a régua deles.** Se uma família inteira do plano
   oficial — um grupo com muitos milhões embaixo — está caindo em "não
   classificado", o defeito é NOSSO e tem endereço. A lista das famílias mal
   cobertas é a fila de trabalho para corrigir a taxonomia.
2. **Ler a despesa sem intermediação.** Quanto foi para cada nó da hierarquia
   oficial, em dois cortes de profundidade (grupo e subgrupo). Nenhuma regra
   nossa entre o dado e o leitor.
3. **Comparar o desenho com a execução.** Contas que existem no plano e nunca
   receberam um lançamento, contra contas que se movimentaram. O plano é o que
   o conselho diz que faz; a execução é o que ele faz.

Limites honestos desta frente, que valem para todos os números abaixo:

* A execução aqui é **fluxo de caixa** (`pagamentos`), não contabilidade. Não
  compare estes totais com os do comparativo de despesa sem dizer que são
  bases diferentes.
* O plano coletado cobre **um único exercício** (o mais recente que a API
  devolveu), enquanto os pagamentos cobrem a série inteira. Conta extinta ou
  renomeada em exercício antigo não casa — e isso é registrado, não escondido.
* O casamento é por **código** quando o rótulo do pagamento traz um
  ("6.2.2.1.1.01.04.05.002-Diarias com Conselheiros") e por **nome
  normalizado** quando não traz. Nome homônimo em ramos diferentes do plano
  não é atribuído a nenhum nó específico: sobe até o ancestral comum dos
  homônimos, que é o quanto o dado sustenta afirmar, ou não casa.
* Os dois lados passam por `taxonomia.normalizar` antes de qualquer
  comparação. Regex direto em nome de conta com acento já custou achados
  falsos a este projeto — "Convênios RES.1032" não casava `convenio`.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl
from .common import df

FRENTE = "Plano de contas oficial"

# Pisos desta frente. Deveriam migrar para config.py quando houver ocasião —
# ficam aqui para não disputar o arquivo com as outras frentes.
#
# PISO: abaixo disto uma família mal coberta é ruído de arredondamento e
# publicá-la só desgasta a lista. PCT: acima disto a família está mal coberta
# de verdade, não por uma conta perdida na ponta.
PISO_FAMILIA_MAL_COBERTA = 5_000_000.0
PCT_FAMILIA_MAL_COBERTA = 40.0
# Acima disto, valor pago sob código que o plano não conhece deixa de ser
# resíduo de série longa e vira pergunta.
PCT_FORA_DO_PLANO_RELEVANTE = 5.0

# Código contábil sempre tem pelo menos um ponto ("6.2.2.1.1.01.04.05.002").
# Exigir o ponto é deliberado: sem ele, "13o Salário - Pessoal" seria lido
# como conta de código "13", e a família inteira sairia errada.
RX_ROTULO_COM_CODIGO = re.compile(r"^\s*(\d+(?:\.\d+)+)\s*(?:[-–—]|\s)\s*(.*)$")
RX_SO_CODIGO = re.compile(r"^\s*(\d+(?:\.\d+)+)\s*$")

SEM_ROTULO = "(rótulo de conta ausente no pagamento)"

_COLS_PLANO: dict[str, tuple[str, ...]] = {
    "codigo": ("codigo", "CODIGO", "Codigo", "_codigo", "codigo_conta"),
    "nome": ("nome", "NOME", "Nome", "_nome", "descricao", "DESCRICAO"),
    # `nivel` não entra: é sempre recalculado pela cadeia de pais, que é o
    # mesmo caminho usado para agregar por família. Confiar na coluna e
    # agregar pela cadeia daria dois números de nível diferentes no relatório.
    "pai": ("pai", "_pai", "PAI", "Pai"),
    "folha": ("folha", "_folha", "FOLHA", "Folha"),
}


def _coluna_alternativa(d: pd.DataFrame, candidatos: tuple[str, ...]):
    for c in candidatos:
        if c in d.columns:
            return d[c]
    return None


def _codigo_txt(v) -> str:
    """Código contábil como texto estável.

    O staged volta de CSV: uma coluna de códigos toda numérica volta como
    float e "6" vira "6.0", que não casa com nada. Uma coluna mista volta como
    texto e precisa só de limpeza. Os dois casos passam por aqui.
    """
    if v is None or (isinstance(v, float) and v != v):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return re.sub(r"\s+", "", str(v)).strip(" .-")


def _bool_txt(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "sim", "t", "yes", "y")


def _separar_codigo(rotulo: str) -> tuple[str, str]:
    """'6.2.2.1-Diárias' -> ('6.2.2.1', 'Diárias'); sem código -> ('', rótulo)."""
    texto = str(rotulo or "")
    m = RX_SO_CODIGO.match(texto)          # rótulo que é só o código
    if m:
        return _codigo_txt(m.group(1)), ""
    m = RX_ROTULO_COM_CODIGO.match(texto)
    if not m:
        return "", texto.strip()
    return _codigo_txt(m.group(1)), m.group(2).strip()


def _preparar_plano(staged) -> pd.DataFrame:
    """Plano de contas achatado e normalizado.

    Colunas de saída: codigo, nome, nome_norm, pai_efetivo, folha, nivel e
    caminho (a lista de códigos da raiz até o nó, que é o que permite agregar
    por qualquer altura da árvore depois).
    """
    p = df(staged, "plano_contas")
    if p.empty:
        return pd.DataFrame()

    codigo = _coluna_alternativa(p, _COLS_PLANO["codigo"])
    nome = _coluna_alternativa(p, _COLS_PLANO["nome"])
    if nome is None:
        return pd.DataFrame()

    d = pd.DataFrame(index=p.index)
    d["nome_bruto"] = nome.fillna("").astype(str)
    # O nome do plano também pode chegar com o código grudado. Separar os dois
    # aqui evita que o lado do plano e o lado do pagamento sejam normalizados
    # de formas diferentes — e nenhum join casaria.
    separado = d["nome_bruto"].map(_separar_codigo)
    d["codigo_do_nome"] = separado.map(lambda t: t[0])
    d["nome"] = separado.map(lambda t: t[1])
    if codigo is not None:
        d["codigo"] = codigo.map(_codigo_txt)
        d["codigo"] = d["codigo"].where(d["codigo"] != "", d["codigo_do_nome"])
    else:
        d["codigo"] = d["codigo_do_nome"]

    pai = _coluna_alternativa(p, _COLS_PLANO["pai"])
    d["pai"] = pai.map(_codigo_txt) if pai is not None else ""
    folha = _coluna_alternativa(p, _COLS_PLANO["folha"])
    d["folha"] = folha.map(_bool_txt) if folha is not None else pd.NA
    d["_ano"] = pd.to_numeric(coluna(p, "_ano"), errors="coerce")

    d = d[d["codigo"] != ""]
    if d.empty:
        return pd.DataFrame()
    # o mesmo código pode vir repetido entre exercícios coletados: fica o mais
    # recente, que é o desenho vigente
    d = (d.sort_values("_ano", na_position="first")
         .drop_duplicates(subset="codigo", keep="last").reset_index(drop=True))
    d["nome_norm"] = d["nome"].map(T.normalizar)

    codigos = set(d["codigo"])
    # `pai` ausente ou apontando para fora do conjunto: reconstrói pelo próprio
    # código, tirando o último segmento. Sem isso a árvore vira 26 mil raízes e
    # a leitura por família some.
    def pai_efetivo(cod: str, declarado: str) -> str:
        if declarado and declarado in codigos and declarado != cod:
            return declarado
        partes = cod.split(".")
        while len(partes) > 1:
            partes = partes[:-1]
            cand = ".".join(partes)
            if cand in codigos:
                return cand
        return ""

    d["pai_efetivo"] = [pai_efetivo(c, p_) for c, p_ in zip(d["codigo"], d["pai"])]
    mapa_pai = dict(zip(d["codigo"], d["pai_efetivo"]))

    memo: dict[str, list[str]] = {}

    def caminho(cod: str) -> list[str]:
        if cod in memo:
            return memo[cod]
        visto, atual, out = set(), cod, []
        while atual and atual not in visto:
            visto.add(atual)
            out.append(atual)
            atual = mapa_pai.get(atual, "")
        memo[cod] = list(reversed(out))
        return memo[cod]

    d["caminho"] = d["codigo"].map(caminho)
    d["nivel"] = d["caminho"].map(len)          # 1 = raiz
    if d["folha"].isna().all():                 # plano sem a marca: deduz
        pais = set(d["pai_efetivo"]) - {""}
        d["folha"] = ~d["codigo"].isin(pais)
    else:
        d["folha"] = d["folha"].fillna(False).astype(bool)
    return d


def _execucao(staged) -> tuple[pd.DataFrame, str]:
    """Uma linha por lançamento pago: _ano, rotulo, valor.

    Preferência por `pagamentos` (fluxo de caixa, o dado mais granular que o
    portal publica). `empenhos` é a reserva quando não houve pagamento
    coletado — e a fonte usada vai escrita em todo achado, porque empenho e
    pagamento são bases diferentes.
    """
    # A reserva é tentada quando a primeira fonte não produz LINHA UTILIZÁVEL,
    # e não só quando o dataset está ausente: `pagamentos` presente mas sem
    # exercício válido em nenhuma linha deixaria a frente muda por engano.
    for nome_ds, col_conta, fonte in (
            ("pagamentos", "CONTA_EMPENHO", "pagamentos (fluxo de caixa)"),
            ("empenhos", "NOME_CONTA",
             "empenhos (reserva: não houve pagamento utilizável)")):
        base = df(staged, nome_ds)
        if base.empty or "VALOR" not in base.columns:
            continue
        rot = coluna(base, col_conta).fillna("").astype(str)
        alt = coluna(base, "NOME_CONTA").fillna("").astype(str)
        rot = rot.where(rot.str.strip() != "", alt)
        d = pd.DataFrame({
            "_ano": pd.to_numeric(coluna(base, "_ano"), errors="coerce"),
            "rotulo": rot.str.strip(),
            "valor": pd.to_numeric(base["VALOR"], errors="coerce").fillna(0.0)})
        d = d.dropna(subset=["_ano"])
        if d.empty:
            continue
        d["_ano"] = d["_ano"].astype(int)
        d["rotulo"] = d["rotulo"].where(d["rotulo"] != "", SEM_ROTULO)
        d = d[d["valor"] != 0]
        if not d.empty:
            return d.reset_index(drop=True), fonte
    return pd.DataFrame(), ""


def _casar_rotulos(rotulos, plano: pd.DataFrame) -> pd.DataFrame:
    """Casa cada rótulo DISTINTO de conta com um nó do plano oficial.

    Roda sobre os valores distintos (algumas centenas), nunca linha a linha —
    é a mesma decisão da taxonomia, e é o que mantém o custo aceitável em
    milhões de pagamentos.

    Modos, do mais forte para o mais fraco:
      codigo    — o código do rótulo existe no plano;
      prefixo   — não existe, mas um ancestral dele existe (conta mais nova ou
                  mais analítica que o plano coletado);
      nome      — rótulo sem código, nome normalizado bate com UM nó só;
      ancestral — o nome bate com vários nós; atribui-se ao ancestral comum
                  mais profundo, que é o quanto o dado sustenta dizer;
      ausente   — nada casou.
    """
    codigos = set(plano["codigo"])
    nome_para_codigos: dict[str, list[str]] = {}
    for cod, nn in zip(plano["codigo"], plano["nome_norm"]):
        if nn:
            nome_para_codigos.setdefault(nn, []).append(cod)
    caminhos = dict(zip(plano["codigo"], plano["caminho"]))

    def ancestral_comum(cands: list[str]) -> str:
        """Nó mais profundo por onde passam TODOS os candidatos homônimos."""
        listas = [caminhos.get(c, []) for c in cands]
        if not all(listas):
            return ""
        comum: list[str] = []
        for passos in zip(*listas):
            if len(set(passos)) != 1:
                break
            comum.append(passos[0])
        # ancestral raso demais (raiz, ou o grupo logo abaixo dela) não
        # informa nada: melhor assumir que não casou do que fingir precisão
        return comum[-1] if len(comum) >= 3 else ""

    linhas = []
    for rotulo in rotulos:
        cod_rot, nome_rot = _separar_codigo(rotulo)
        casado, modo = "", "ausente"
        if cod_rot:
            if cod_rot in codigos:
                casado, modo = cod_rot, "codigo"
            else:
                partes = cod_rot.split(".")
                while len(partes) > 1 and not casado:
                    partes = partes[:-1]
                    cand = ".".join(partes)
                    if cand in codigos:
                        casado, modo = cand, "prefixo"
        if not casado:
            candidatos = nome_para_codigos.get(T.normalizar(nome_rot), [])
            if len(candidatos) == 1:
                casado, modo = candidatos[0], "nome"
            elif candidatos:
                anc = ancestral_comum(candidatos)
                if anc:
                    casado, modo = anc, "ancestral"
        linhas.append({"rotulo": rotulo, "codigo_plano": casado, "modo": modo,
                       "tem_codigo": bool(cod_rot)})
    return pd.DataFrame(linhas)


def _rotulo_no(codigo: str, nomes: dict[str, str]) -> str:
    nome = nomes.get(codigo, "")
    return f"{codigo} {nome}".strip() if codigo else "(sem correspondência)"


def _nivel_que_discrimina(por_codigo: pd.Series,
                          caminhos: dict[str, list[str]]) -> int:
    """Em que nível da árvore oficial vale a pena agregar.

    Fixar "nível 2" seria natural e estaria errado no plano real: a árvore do
    Sistema Confea/Crea desce de uma raiz única ("6") para um único nó de
    variação patrimonial e um único nó de despesa corrente antes de abrir. Nos
    três primeiros níveis a despesa inteira cai numa família só, e a leitura
    por família não informa nada.

    Escolhe-se, então, o nível mais raso que já **separa** o dinheiro: pelo
    menos dois nós e nenhum deles concentrando mais de 90% do valor. Se nenhum
    nível até o oitavo satisfizer, fica o que mais reparte. O nível escolhido é
    publicado junto com o número, para o leitor saber a que altura da árvore
    está olhando.

    Recebe a despesa JÁ SOMADA por código de conta (algumas milhares de
    linhas), não os lançamentos: varrer oito níveis sobre milhões de
    pagamentos custaria minutos e daria exatamente o mesmo resultado.
    """
    melhor, melhor_n = 2, -1
    for n in range(2, 9):
        fam = por_codigo.index.map(lambda c: _familia(c, n, caminhos))
        soma = por_codigo.groupby(fam).sum()
        soma = soma[soma.index != ""]
        if len(soma) < 2 or soma.sum() <= 0:
            continue
        if float(soma.max()) / float(soma.sum()) < 0.90:
            return n
        if len(soma) > melhor_n:
            melhor, melhor_n = n, len(soma)
    return melhor


def _familia(codigo, nivel: int, caminhos: dict[str, list[str]]) -> str:
    """Ancestral do nó no nível pedido (ou ele mesmo, se for mais raso)."""
    c = caminhos.get(codigo) if isinstance(codigo, str) else None
    if not c:
        return ""
    return c[min(nivel - 1, len(c) - 1)]


def run(staged) -> list[Finding]:
    achados: list[Finding] = []

    plano = _preparar_plano(staged)
    if plano.empty:
        achados.append(Finding(
            id="plano-indisponivel", frente=FRENTE, gravidade="info",
            titulo="Plano de contas oficial ausente: a leitura pela régua do "
                   "próprio conselho está INDISPONÍVEL",
            descricao=(
                "O endpoint `PlanoDeContas` não devolveu linhas utilizáveis "
                "nesta rodada (ou devolveu linhas sem código de conta). Sem a "
                "hierarquia oficial, três medições deixam de existir neste "
                "relatório: a cobertura da nossa taxonomia medida contra a "
                "estrutura declarada, a despesa por nó do plano e o confronto "
                "entre contas desenhadas e contas efetivamente movimentadas."
                "\n\nNada foi estimado para preencher a lacuna. A ausência é o "
                "achado: **a classificação da despesa deste relatório continua "
                "apoiada apenas na taxonomia do projeto**, que é interpretação "
                "nossa sobre o nome da conta e não a estrutura oficial."),
            como_verificar="Pedir o plano de contas vigente em formato aberto, "
                           "com código, nome, nível e conta-pai, para todos os "
                           "exercícios do período.",
            metricas={"linhas_plano": int(len(df(staged, "plano_contas")))},
        ))
        return achados

    nomes = dict(zip(plano["codigo"], plano["nome"]))
    ano_plano = (int(plano["_ano"].max()) if plano["_ano"].notna().any() else None)
    n_folhas = int(plano["folha"].sum())

    execucao, fonte_exec = _execucao(staged)
    if execucao.empty:
        estrutura = (plano.groupby("nivel")
                     .agg(contas=("codigo", "size"), analiticas=("folha", "sum"))
                     .reset_index())
        achados.append(Finding(
            id="plano-execucao-indisponivel", frente=FRENTE, gravidade="info",
            titulo=(f"O plano oficial tem {len(plano)} contas, mas não há "
                    "execução coletada para confrontar com ele"),
            descricao=(
                "A hierarquia oficial foi coletada, mas nem `pagamentos` nem "
                "`empenhos` trouxeram lançamento utilizável nesta rodada — "
                "linha com exercício e valor. O desenho declarado está abaixo; "
                "o que foi efetivamente executado sobre ele **não pôde ser "
                "medido** e nada foi estimado no lugar."),
            evidencia=estrutura,
            metricas={"contas": int(len(plano)), "contas_analiticas": n_folhas},
        ))
        return achados

    mapa = _casar_rotulos(sorted(execucao["rotulo"].unique()), plano)
    d = execucao.merge(mapa, on="rotulo", how="left")
    d["codigo_plano"] = d["codigo_plano"].fillna("")
    caminhos = dict(zip(plano["codigo"], plano["caminho"]))
    # Tudo o que depende só do CÓDIGO é resolvido em dicionários sobre os
    # códigos distintos e depois mapeado. Aplicar lambda linha a linha custaria
    # minutos nos milhões de pagamentos da rodada real — pelo mesmo resultado.
    por_codigo = d.groupby("codigo_plano")["valor"].sum()
    nivel_grupo = _nivel_que_discrimina(por_codigo, caminhos)
    nivel_sub = nivel_grupo + 1
    distintos = list(por_codigo.index)
    mapa_grupo = {c: _familia(c, nivel_grupo, caminhos) for c in distintos}
    mapa_sub = {c: _familia(c, nivel_sub, caminhos) for c in distintos}
    rotulos_no = {c: _rotulo_no(c, nomes)
                  for c in set(mapa_grupo.values()) | set(mapa_sub.values())}
    d["grupo"] = d["codigo_plano"].map(mapa_grupo)
    d["subgrupo"] = d["codigo_plano"].map(mapa_sub)
    d["grupo_rotulo"] = d["grupo"].map(rotulos_no)
    d["subgrupo_rotulo"] = d["subgrupo"].map(rotulos_no)
    # a taxonomia é aplicada ao MESMO texto que o resto do projeto usa (o
    # rótulo bruto do pagamento), senão a cobertura medida aqui não seria a
    # cobertura real das outras frentes
    cat = {r: T.classificar_texto(r) for r in d["rotulo"].unique()}
    d["categoria"] = d["rotulo"].map(cat)
    d["nao_classificado"] = d["categoria"] == "OUTROS"
    d["valor_sem_classe"] = d["valor"].where(d["nao_classificado"], 0.0)

    total = float(d["valor"].sum())
    casado = d[d["codigo_plano"] != ""]
    total_casado = float(casado["valor"].sum())
    pct_casado = 100 * total_casado / total if total else 0.0
    modos = (d.groupby("modo")["valor"].sum() / total * 100).round(1).to_dict() \
        if total else {}
    # Casamento por código é o join oficial; casamento por nome é INFERÊNCIA
    # nossa. Publicar a repartição sem dizer quanto dela veio de inferência
    # venderia como "leitura sem intermediação" um número que em parte é
    # interpretação — a mesma promessa que a taxonomia já não cumpre.
    pct_por_codigo = float(modos.get("codigo", 0.0)) + float(modos.get("prefixo", 0.0))
    pct_por_nome = float(modos.get("nome", 0.0)) + float(modos.get("ancestral", 0.0))

    # ------------------------------------------------ 1. despesa por nó oficial
    # `total_casado > 0` e não só `casado` não-vazio: uma família cujo valor
    # líquido fecha em zero (pagamento + estorno no mesmo nó) publicaria
    # "R$ 0,00 relidos" e um share_pct NaN — zero como medição, que é ruído.
    if not casado.empty and total_casado > 0:
        por_no = (casado.groupby(["grupo_rotulo", "subgrupo_rotulo"])
                  .agg(total=("valor", "sum"), lancamentos=("valor", "size"),
                       contas=("codigo_plano", "nunique"),
                       exercicios=("_ano", "nunique")).reset_index()
                  .sort_values("total", ascending=False))
        por_no["share_pct"] = (100 * por_no["total"] / total_casado).round(2)
        n2 = int(casado["grupo"].nunique())
        n3 = int(casado["subgrupo"].nunique())

        # o gráfico empilhado só é legível com poucas faixas: os 8 maiores
        # grupos e o resto somado, sem esconder o resto
        maiores = (casado.groupby("grupo_rotulo")["valor"].sum()
                   .sort_values(ascending=False).head(8).index)
        c2 = casado[["_ano", "valor", "grupo_rotulo"]].copy()
        c2["faixa"] = c2["grupo_rotulo"].where(
            c2["grupo_rotulo"].isin(maiores), "demais grupos")
        achados.append(Finding(
            id="plano-despesa-por-no", frente=FRENTE, gravidade="info",
            titulo=(f"{fmt_brl(total_casado)} de despesa relidos pelos {n2} "
                    f"grupos e {n3} subgrupos do plano de contas oficial"),
            descricao=(
                "Esta é a despesa organizada pela estrutura que o próprio "
                "conselho declara: cada lançamento foi devolvido ao nó do plano "
                "de contas a que pertence e agregado em dois cortes — **grupo** "
                f"(nível {nivel_grupo} da árvore) e **subgrupo** (nível "
                f"{nivel_sub}); um nó mais raso que o corte entra por si mesmo."
                "\n\n"
                "A altura do corte não é arbitrária nem fixa: a árvore oficial "
                "desce de uma raiz única por vários níveis em que toda a "
                "despesa cai numa família só, e agregar ali não informaria "
                "nada. O código escolhe o nível mais raso que efetivamente "
                "reparte o dinheiro, e publica qual foi.\n\n"
                "Quanto disto é a régua deles e quanto é inferência nossa: "
                f"{pct_por_codigo:.1f}% do valor executado foi para o nó pelo "
                "**código contábil** gravado no próprio lançamento, e "
                f"{pct_por_nome:.1f}% foi atribuído por **nome de conta** — "
                "esse segundo pedaço é um casamento textual feito por nós, não "
                "um vínculo declarado pelo conselho, e erra quando o mesmo nome "
                "existe em ramos diferentes da árvore.\n\n"
                f"A base é {fonte_exec} — não é a contabilidade do comparativo "
                "de despesa, e os totais não têm de coincidir com ela. "
                f"{pct_casado:.1f}% do valor executado no período encontrou "
                "correspondência no plano; o que sobrou, quando sobra, é "
                "detalhado rótulo a rótulo no achado sobre execução fora do "
                "plano.\n\n"
                "Leia a tabela como o mapa oficial do dinheiro: se um subgrupo "
                "que deveria ser marginal aparece entre os maiores, a pergunta "
                "não depende de aceitarmos nenhuma régua nossa — ela nasce da "
                "régua do órgão."),
            evidencia=por_no.head(200),
            serie=A.serie_anual(c2, "valor", by="faixa"), grafico="area_stacked",
            tabela_anual=A.ranking_anual(casado, "subgrupo_rotulo", "valor", top=10),
            base_legal="Lei 4.320/1964, arts. 12 e 15 (despesa discriminada por "
                       "categoria e elemento); Resolução Confea sobre o plano "
                       "de contas do Sistema Confea/Crea.",
            metricas={"total_executado": round(total, 2),
                      "total_conciliado": round(total_casado, 2),
                      "pct_conciliado": round(pct_casado, 2),
                      "nivel_grupo": nivel_grupo, "nivel_subgrupo": nivel_sub,
                      "grupos": n2, "subgrupos": n3,
                      "contas_do_plano": int(len(plano)),
                      "ano_do_plano": ano_plano,
                      "pct_casado_por_codigo": round(pct_por_codigo, 2),
                      "pct_casado_por_nome": round(pct_por_nome, 2),
                      "pct_por_modo_de_casamento": modos},
            como_verificar="Pedir o plano de contas de cada exercício e a "
                           "despesa executada por conta analítica — com os dois, "
                           "a conciliação deixa de depender de casamento por "
                           "nome.",
        ))

        # -------------------------------------------- 2. cobertura da taxonomia
        # `valor_sem_classe` é coluna calculada antes do filtro, e não um
        # lambda que reindexa dentro do groupby: agregação que volta ao frame
        # original pelo índice é a classe de bug que troca valor de linha
        # quando o índice não vem único.
        cob = (casado.groupby("grupo_rotulo")
               .agg(total=("valor", "sum"),
                    nao_classificado=("valor_sem_classe", "sum")).reset_index())
        # `Series.replace(0, pd.NA)` devolve coluna OBJECT, e `.round()` sobre
        # object levanta "TypeError: type NAType doesn't define __round__" —
        # bastava UM grupo cujo valor líquido fechasse em zero (um pagamento e
        # o estorno dele no mesmo nó) para a frente inteira morrer e virar
        # `erro-check-plano`. Máscara com NaN de verdade mantém o dtype float.
        _tot = cob["total"].astype(float)
        cob["pct_nao_classificado"] = (
            100 * cob["nao_classificado"].astype(float) / _tot.where(_tot != 0)
        ).round(2)
        cob = cob.sort_values("nao_classificado", ascending=False)
        nao_class = float(casado.loc[casado["nao_classificado"], "valor"].sum())
        pct_nao = 100 * nao_class / total_casado if total_casado else 0.0
        achados.append(Finding(
            id="plano-cobertura-taxonomia", frente=FRENTE,
            gravidade="media" if pct_nao >= 10 else "info",
            # sem resíduo, o título vira uma medida positiva em vez de um "0%"
            # que o leitor lê como achado vazio
            titulo=(f"{pct_nao:.1f}% do valor conciliado está em contas que a "
                    "taxonomia do projeto não classificou" if nao_class > 0 else
                    "A taxonomia do projeto classificou todo o valor conciliado "
                    "com o plano de contas oficial"),
            descricao=(
                "Este achado mede **o nosso próprio instrumento**, não o "
                "conselho. A taxonomia do projeto classifica despesa por regex "
                "sobre o nome da conta; quando uma regra não casa, o valor cai "
                "em 'não classificado' e some das leituras por natureza — foi "
                "assim que R$ 227 milhões ficaram invisíveis numa rodada "
                "anterior, porque um plural não casava.\n\n"
                "Medir esse resíduo contra a hierarquia oficial muda a "
                "natureza do diagnóstico: em vez de uma pilha anônima de "
                "contas, aparece **em que famílias do plano do órgão** o buraco "
                "está. A tabela abaixo é a fila de trabalho para corrigir "
                f"`taxonomia.py`. O total não classificado soma "
                f"{fmt_brl(nao_class)}.\n\n"
                "Um resíduo alto não significa despesa irregular; significa "
                "leitura incompleta, e as demais frentes deste relatório devem "
                "ser lidas sabendo disso."),
            evidencia=cob.head(120),
            metricas={"valor_nao_classificado": round(nao_class, 2),
                      "pct_nao_classificado": round(pct_nao, 2),
                      "total_conciliado": round(total_casado, 2)},
        ))

        # -------------------------------------------- 3. famílias mal cobertas
        ruins = cob[(cob["nao_classificado"] >= PISO_FAMILIA_MAL_COBERTA) &
                    (cob["pct_nao_classificado"] >= PCT_FAMILIA_MAL_COBERTA)]
        if not ruins.empty:
            # exemplos concretos: as contas que mais pesam dentro de cada
            # família mal coberta, para a correção da regra ser cirúrgica
            resid = casado[casado["nao_classificado"]]
            exemplos = {}
            for fam, g in resid.groupby("grupo_rotulo"):
                top = (g.groupby("rotulo")["valor"].sum()
                       .sort_values(ascending=False).head(3))
                exemplos[fam] = "; ".join(
                    f"{r} ({fmt_brl(v)})" for r, v in top.items())
            ruins = ruins.copy()
            ruins["principais_contas_sem_classificacao"] = (
                ruins["grupo_rotulo"].map(exemplos).fillna(""))
            perdido = float(ruins["nao_classificado"].sum())
            pct_perdido = 100 * perdido / total_casado if total_casado else 0.0
            achados.append(Finding(
                id="plano-familias-nao-cobertas", frente=FRENTE,
                gravidade="alta" if pct_perdido >= 5 else "media",
                titulo=(f"{len(ruins)} família(s) do plano oficial concentram "
                        f"{fmt_brl(perdido)} que a nossa taxonomia não "
                        "classifica"),
                descricao=(
                    "Não é uma conta perdida na ponta: são **famílias inteiras "
                    f"da hierarquia oficial** — grupos de nível {nivel_grupo} — "
                    f"em que a maior parte do dinheiro (pelo menos "
                    f"{PCT_FAMILIA_MAL_COBERTA:.0f}% do valor da família) não "
                    "recebeu categoria nenhuma das regras do projeto.\n\n"
                    "Cada linha traz as contas que mais pesam dentro da "
                    "família, que é exatamente o que precisa virar regra nova "
                    "em `taxonomia.py`. Enquanto não virar, todo número deste "
                    "relatório que reparta despesa por natureza está "
                    f"subestimando essas famílias — {pct_perdido:.1f}% do valor "
                    "conciliado do período.\n\n"
                    "Este achado é uma falha do instrumento de fiscalização, "
                    "não uma acusação ao fiscalizado. Publicá-lo é o que "
                    "impede que o silêncio de uma regra seja lido como "
                    "ausência de gasto."),
                evidencia=ruins,
                metricas={"familias": int(len(ruins)),
                          "valor_nao_classificado": round(perdido, 2),
                          "pct_do_conciliado": round(pct_perdido, 2)},
                como_verificar="Ler os nomes das contas listadas e escrever a "
                               "regra correspondente em `taxonomia.py`, "
                               "sempre sobre texto já passado por "
                               "`T.normalizar` e testando singular e plural.",
            ))

    # ------------------------------------------------ 4. desenho × execução
    # Só as contas casadas com CERTEZA (código exato ou nome único) contam como
    # movimentadas. Casamento por prefixo aponta para um nó sintético e não
    # prova que aquela analítica específica se moveu.
    exatas = set(d.loc[d["modo"].isin(["codigo", "nome"]), "codigo_plano"].dropna())
    folhas_plano = plano[plano["folha"].astype(bool)]
    # O plano de contas vem INTEIRO — ativo, passivo, patrimônio líquido,
    # receita e despesa. Um pagamento não pode cair numa conta de ativo, então
    # contar todas as analíticas do plano no denominador não mede execução:
    # mede o tamanho do plano. Numa prova com 741 contas de despesa TODAS
    # movimentadas mais 1.482 contas de ativo e passivo, o achado publicava
    # "67% sem lançamento" — o número certo era 0%. O universo honesto são as
    # classes (raízes da árvore) em que houve execução conciliada.
    raizes = {caminhos[c][0] for c in casado["codigo_plano"].unique()
              if caminhos.get(c)}
    if raizes and not folhas_plano.empty:
        # `.astype(bool)` não é decoração: `map` sobre coluna vazia devolve
        # Series de dtype object, e indexar DataFrame com máscara object faz o
        # pandas tentar selecionar COLUNAS — a frente morria com KeyError.
        folhas = folhas_plano[folhas_plano["caminho"].map(
            lambda cam: bool(cam) and cam[0] in raizes).astype(bool)]
    else:
        folhas = folhas_plano
    # Restringir à CLASSE não bastou no plano real: a classe de despesa do
    # Crea-SP sozinha tem 15.195 contas analíticas e a execução usa 150. O
    # achado saía "99,0% das contas nunca receberam lançamento", que não mede
    # execução nenhuma — mede o tamanho do plano de contas, e publicado como
    # achado só ensina o leitor a desconfiar do relatório.
    #
    # O universo comparável é mais estreito: as FAMÍLIAS (nós de grupo) em que
    # o órgão de fato executa. Dentro de uma família viva, conta parada diz
    # alguma coisa; uma família inteira sem uso é desenho do plano, não
    # execução.
    familias_vivas = {_familia(c, nivel_grupo, caminhos)
                      for c in exatas} - {None, ""}
    if familias_vivas and not folhas.empty:
        restrito = folhas[folhas["codigo"].map(
            lambda c: _familia(c, nivel_grupo, caminhos) in familias_vivas
        ).astype(bool)]
        if not restrito.empty:
            folhas = restrito
    com_mov = folhas[folhas["codigo"].isin(exatas)]
    sem_mov = folhas[~folhas["codigo"].isin(exatas)]
    # Mesmo depois do estreitamento, se quase tudo continua sem movimento o que
    # se está medindo é granularidade do plano. Publicar seria vender ruído
    # como achado — e este relatório existe para ser citado.
    if not folhas.empty and len(sem_mov) > 0.9 * len(folhas):
        achados.append(Finding(
            id="plano-contas-sem-lancamento-inconclusivo", frente=FRENTE,
            gravidade="info",
            titulo=("Contas do plano sem lançamento: medida inconclusiva neste "
                    f"plano ({len(sem_mov)} de {len(folhas)})"),
            descricao=(
                "O plano de contas coletado é granular demais para que "
                "'quantas contas nunca receberam lançamento' signifique alguma "
                "coisa: mesmo restringindo o universo às famílias em que o "
                "órgão executa, mais de 90% das contas analíticas seguem sem "
                "movimento.\n\n"
                "Isso mede o tamanho e o detalhamento do plano, não a execução. "
                "Um plano de contas contém, por desenho, uma conta para cada "
                "situação prevista — inclusive as que nunca ocorrem. "
                "**Publicar esse percentual como achado seria ruído.**\n\n"
                f"O que resta útil está nos outros achados desta frente: a "
                f"despesa relida pelos nós do plano oficial e as famílias que a "
                "nossa taxonomia não classifica."),
            metricas={"contas_analiticas_no_universo": int(len(folhas)),
                      "com_movimento": int(len(com_mov)),
                      "sem_movimento": int(len(sem_mov)),
                      "motivo": "granularidade do plano, não execução"},
        ))
    elif not folhas.empty and not sem_mov.empty:
        pct_sem = 100 * len(sem_mov) / len(folhas)
        lista = sem_mov[["codigo", "nome"]].copy()
        lista["grupo"] = sem_mov["codigo"].map(
            lambda c: _rotulo_no(_familia(c, nivel_grupo, caminhos), nomes))
        lista = lista.sort_values("codigo")
        achados.append(Finding(
            id="plano-contas-sem-lancamento", frente=FRENTE, gravidade="info",
            titulo=(f"{len(sem_mov)} das {len(folhas)} contas analíticas do "
                    f"plano oficial não receberam lançamento identificado "
                    # uma casa decimal: com o universo restrito às classes de
                    # despesa o percentual fica pequeno, e ".0f" imprimia
                    # "3 das 741 (0%)" — contagem não nula com percentual zero
                    f"({pct_sem:.1f}%)"),
            descricao=(
                "O plano de contas é o **desenho declarado**: tudo o que o "
                "conselho previu que poderia gastar. A execução é o que ele de "
                "fato gastou. A diferença entre os dois diz o quanto da "
                "estrutura contábil está viva.\n\n"
                f"Foram identificados lançamentos em {len(com_mov)} contas "
                f"analíticas; as demais {len(sem_mov)} aparecem sem "
                "movimento no período coletado. Isso **não** é irregularidade: "
                "plano de contas contém contas previstas para situações que "
                "não ocorreram, e é assim que deve ser.\n\n"
                # o parágrafo só faz sentido quando a restrição de fato cortou
                # contas: repetir "o denominador não é o plano inteiro" quando
                # ele É o plano inteiro seria ruído
                + (f"O denominador não é o plano inteiro: o arquivo traz "
                   f"{len(folhas_plano)} contas analíticas de várias classes "
                   "contábeis — ativo, passivo, receita, despesa — e um "
                   "pagamento só pode cair nas classes de despesa. Contar as "
                   "demais no denominador inflaria o percentual sem medir "
                   "nada, então o universo aqui está restrito às classes em "
                   "que houve execução conciliada "
                   f"({', '.join(sorted(raizes))}).\n\n"
                   if len(folhas) < len(folhas_plano) else "")
                +
                "Duas ressalvas honestas antes de usar este número. Primeira: "
                "conta sem movimento aqui pode ser conta cujo lançamento não "
                "casou com o plano — o rótulo do pagamento pode não trazer "
                "código nem nome idêntico. Segunda: o plano coletado é de um "
                "exercício só, e a série de pagamentos é longa. O uso legítimo "
                "desta lista é operacional: é por onde se pede ao conselho a "
                "correspondência oficial entre conta e lançamento."),
            evidencia=lista.head(300),
            metricas={"contas_analiticas": int(len(folhas)),
                      "contas_analiticas_plano_inteiro": int(len(folhas_plano)),
                      "com_movimento": int(len(com_mov)),
                      "sem_movimento": int(len(sem_mov)),
                      "pct_sem_movimento": round(pct_sem, 1)},
            como_verificar="Pedir o razão analítico por conta do plano, por "
                           "exercício — é o documento que fecha esta lista sem "
                           "depender de casamento por nome.",
        ))

    # ------------------------------------------------ 5. execução fora do plano
    fora = d[d["codigo_plano"].fillna("") == ""]
    if not fora.empty and float(fora["valor"].sum()) != 0:
        val_fora = float(fora["valor"].sum())
        pct_fora = 100 * val_fora / total if total else 0.0
        com_cod = fora[fora["tem_codigo"].fillna(False).astype(bool)]
        val_com_cod = float(com_cod["valor"].sum())
        # Rótulo COM código que o plano não conhece é evidência mais forte que
        # rótulo sem código nenhum: o primeiro afirma um código, o segundo só
        # não permite o join. Separar os dois evita transformar limitação de
        # casamento em acusação.
        no_ano_do_plano = (
            float(com_cod.loc[com_cod["_ano"] == ano_plano, "valor"].sum())
            if ano_plano is not None else 0.0)
        grave = (pct_fora >= PCT_FORA_DO_PLANO_RELEVANTE and no_ano_do_plano > 0)
        detalhe = (fora.groupby(["rotulo", "tem_codigo"])
                   .agg(total=("valor", "sum"), lancamentos=("valor", "size"),
                        primeiro_ano=("_ano", "min"), ultimo_ano=("_ano", "max"))
                   .reset_index().sort_values("total", ascending=False))
        achados.append(Finding(
            id="plano-execucao-fora-do-plano", frente=FRENTE,
            gravidade="media" if grave else "info",
            titulo=(f"{fmt_brl(val_fora)} ({pct_fora:.1f}%) foram pagos sob "
                    f"{detalhe['rotulo'].nunique()} rótulos de conta sem "
                    "correspondência no plano oficial coletado"),
            descricao=(
                "Conciliar a execução com o plano oficial deixa um resto, e o "
                "resto é informativo — desde que lido com as duas ressalvas "
                "certas.\n\n"
                f"Do total, {fmt_brl(val_com_cod)} estão em rótulos que "
                "**afirmam um código contábil** que o plano coletado não "
                "contém; o restante está em rótulos sem código, cujo nome "
                "simplesmente não bate com nenhuma conta — aí a falha pode ser "
                "só de nomenclatura, e a coluna `tem_codigo` separa os dois "
                "casos na tabela.\n\n"
                "Ressalva decisiva: o plano coletado cobre o exercício "
                f"{ano_plano if ano_plano is not None else 'mais recente disponível'}"
                ", enquanto os pagamentos cobrem a série inteira. Conta extinta "
                "ou renomeada em exercício antigo cai aqui por construção, e "
                "isso não é irregularidade. O que merece explicação é valor "
                "pago **no próprio exercício do plano** sob código que o plano "
                f"não reconhece — aqui, {fmt_brl(no_ano_do_plano)}."),
            evidencia=detalhe.head(200),
            serie=A.serie_anual(fora, "valor"), grafico="bar",
            # Os arts. 83 e 85 falam em evidenciar a situação de quem efetua
            # despesa e em organizar a contabilidade de modo a permitir o
            # acompanhamento da execução orçamentária — NÃO dizem "segundo o
            # plano de contas", e a redação anterior colocava essa frase na
            # boca da lei. CF art. 37 caput saiu: este achado é, em boa parte,
            # limitação do nosso casamento, e não cabe invocar princípio
            # constitucional sobre um resíduo metodológico.
            base_legal="Lei 4.320/1964, arts. 83 e 85 (a contabilidade deve "
                       "evidenciar quem efetua a despesa e permitir o "
                       "acompanhamento da execução orçamentária).",
            metricas={"valor_fora": round(val_fora, 2),
                      "pct_fora": round(pct_fora, 2),
                      "valor_com_codigo_desconhecido": round(val_com_cod, 2),
                      "valor_no_ano_do_plano": round(no_ano_do_plano, 2),
                      "rotulos": int(detalhe["rotulo"].nunique())},
            como_verificar="Pedir a tabela de-para entre o código gravado em "
                           "cada empenho e a conta do plano vigente no "
                           "exercício correspondente.",
        ))
    return achados
