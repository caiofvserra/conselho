"""Taxonomia da despesa em DOIS EIXOS independentes.

Responde à pergunta central: **quanto foi para remuneração, diárias e custeio
administrativo (despesa-meio) e quanto foi para fiscalização, atendimento e
ações finalísticas (despesa-fim)?**

Os dois eixos são medidos separadamente porque respondem a perguntas
diferentes e colapsá-los num rótulo só destrói informação:

* **NATUREZA** (`classificar_texto`) — *o que* se comprou: salário, diária,
  aluguel, obra, publicidade. Vem do nome da CONTA CONTÁBIL.
* **FUNÇÃO** (`classificar_funcao`) — *para que* se comprou: fiscalização,
  atendimento ao profissional, direção superior, administração. Vem do nome do
  CENTRO DE CUSTO / unidade.

Um salário pode ser de agente fiscal (natureza REMUNERACAO, função
FISCALIZACAO) ou de assessor da presidência (REMUNERACAO / DIRECAO_SUPERIOR).
A matriz cruzada é o que separa "gasta muito com pessoal" de "gasta muito com
pessoal que não fiscaliza".

A classificação roda sobre os VALORES DISTINTOS de nome de conta / centro de
custo (poucos milhares), nunca linha a linha, e o resultado é reaproveitado.
Regras determinísticas primeiro; o LLM (opcional) só resolve o resíduo.
"""
from __future__ import annotations

import logging
import re
import unicodedata

import pandas as pd

log = logging.getLogger("queda.taxonomia")

# ---------------------------------------------------------------- categorias
# grupo macro -> como o usuário quer ver o dinheiro repartido
GRUPOS = {
    "REMUNERACAO": "Pessoal (remuneração e encargos)",
    "VERBAS_PESSOAIS": "Verbas a pessoas (diárias, km, jetons, passagens)",
    "CUSTEIO_ADMIN": "Custeio administrativo (meio)",
    "SERVICOS_PROFISSIONAIS": "Serviços profissionais (jurídico, consultoria)",
    "PUBLICIDADE": "Publicidade e comunicação",
    "FISCALIZACAO": "Fiscalização (finalística)",
    "ATENDIMENTO": "Atendimento ao profissional (finalística)",
    "INSTITUCIONAL": "Ações institucionais e convênios (finalística)",
    "TRANSFERENCIAS": "Transferências ao sistema (Confea/Mútua)",
    "INVESTIMENTO": "Investimento e obras",
    "FINANCEIRA_JUDICIAL": "Financeiras, tributárias e judiciais",
    "OUTROS": "Não classificado",
}

# agrupamento de alto nível para o gráfico-síntese
MACRO = {
    "REMUNERACAO": "Pessoal e verbas",
    "VERBAS_PESSOAIS": "Pessoal e verbas",
    "CUSTEIO_ADMIN": "Administrativo (meio)",
    "SERVICOS_PROFISSIONAIS": "Administrativo (meio)",
    "PUBLICIDADE": "Administrativo (meio)",
    "FISCALIZACAO": "Finalística",
    "ATENDIMENTO": "Finalística",
    "INSTITUCIONAL": "Finalística",
    "TRANSFERENCIAS": "Transferências",
    "INVESTIMENTO": "Investimento",
    "FINANCEIRA_JUDICIAL": "Financeiro/judicial",
    "OUTROS": "Não classificado",
}

# Regras em ordem de precedência: a primeira que casar vence. Escritas sobre
# texto normalizado (sem acento, minúsculo). \b evita o erro de "passageiros"
# casar com "passagem".
REGRAS: list[tuple[str, str]] = [
    # --- transferências obrigatórias (antes de tudo: são nominais) ---
    ("TRANSFERENCIAS", r"\bconfea\b|cota[- ]parte|\bmutua\b|contribui\w* ao sistema"),

    # --- verbas pagas a pessoas ---
    ("VERBAS_PESSOAIS",
     r"\bdiaria(s)?\b|\bjeton|ajuda de custo|verba de representa|"
     r"indeniza\w* de transporte|auxilio deslocamento|"
     # "Locomoção de Conselheiros/Inspetores/Funcionários" são as contas de
     # quilometragem do Crea-SP e somam R$ 87,7 mi na série real. Sem esta
     # alternativa elas caíam em OUTROS e ficavam fora da frente de verbas —
     # metade do gasto de deslocamento dos conselheiros, invisível.
     r"\blocomoca|\blocomoçã|\bdeslocamento\b|"
     r"\bquilometragem\b|\bkm rodado|reembolso de despesa|reembolso de viagem|"
     # plural em -ns: `passagen(s)?` casava "passagens" e NUNCA "passagem",
     # deixando a conta no singular — a mais comum — fora da categoria.
     r"\bpassage(m|ns)\b|\bhospedagem\b|\bpernoite\b|adiantamento de viagem"),

    # --- remuneração e encargos ---
    ("REMUNERACAO",
     r"\bvencimento|\bsalario|remunera\w+ (de |do |dos )?(pessoal|empregad|servidor)|"
     r"\bfolha de pagamento|\bencargo(s)? (social|patronal|trabalhista)|"
     r"\bfgts\b|\binss\b|\b13\w* salario|decimo terceiro|\bferias\b|"
     r"\bvale[- ](transporte|refeicao|alimentacao)|\bcesta basica\b|"
     r"plano de saude|assistencia medica|assistencia odonto|seguro de vida|"
     r"\bestagiario|\bbolsa de estagio|\baprendiz\b|previdencia complementar|"
     # R$ 255 mi caíam em "não classificado" na rodada real, e eram todos
     # folha: gratificação, auxílios, PDV, substituição. `\bhora extra` ainda
     # não casava "Horas Extras" — o mesmo defeito de plural de "passagem".
     r"\bgratifica|\bauxilio (educacao|creche|doenca|funeral|natalidade|"
     r"alimentacao|filho|maternidade|invalidez)|\bpdv\b|"
     r"demissao voluntaria|\bsubstituic(ao|oes)\b|"
     r"reembolso de (oculos|medicamento|despesa medica)|"
     r"resciso|\brescisao\b|indeniza\w* trabalhista|\bhora(s)? extra"),

    # --- fiscalização (finalística) ---
    ("FISCALIZACAO",
     r"\bfiscaliza|\bfiscal(is)?\b|\bvistoria|auto(s)? de infra|"
     r"agente(s)? fiscal|\bdenuncia|\bpericia\b|apoio a fiscaliza"),

    # --- atendimento ao profissional (finalística) ---
    ("ATENDIMENTO",
     r"\bart\b|anotacao de responsabilidade|\bacervo tecnico\b|\bcat\b|"
     r"carteira profissional|\bregistro profissional|\batendimento\b|"
     r"posto(s)? de (atendimento|servico)|unidade(s)? de atendimento"),

    # --- ações institucionais / convênios (finalística) ---
    ("INSTITUCIONAL",
     r"\bconvenio|\bpatrocinio|\bpremio\b|premia\w+|\bcapacitacao\b|"
     r"\btreinamento\b|\bcurso(s)?\b|\bcongresso|\bseminario|\bsimposio|"
     r"\bfeira\b|\bevento(s)?\b|camara(s)? especializada|colegiado|"
     r"\bplenaria\b|\binspetoria|entidade(s)? de classe|\bmutirao|"
     r"\bpublicacao (tecnica|institucional)|\brevista\b|programa institucional"),

    # --- publicidade e comunicação ---
    ("PUBLICIDADE",
     r"\bpublicidade\b|\bpropaganda\b|\bmidia\b|\bmarketing\b|"
     r"\bassessoria de imprensa|comunicacao (social|institucional)|"
     r"\bveiculacao|\banuncio|\bpublicacao legal|\bdiario oficial"),

    # --- serviços profissionais ---
    ("SERVICOS_PROFISSIONAIS",
     r"\badvocat|\badvogad|\bhonorario|\bjuridic|\bassessoria juridica|"
     r"\bcontabil|\bauditoria|\bconsultoria|\bassessoria tecnica|"
     r"\bperito\b|servico(s)? tecnico(s)? profissional"),

    # --- locação é custeio, não investimento (precede a regra de imóvel/bem) ---
    ("CUSTEIO_ADMIN", r"\b(aluguel|alugue|locacao|arrendamento|leasing)\b"),

    # --- investimento / obras ---
    ("INVESTIMENTO",
     r"\bobra(s)?\b|\bconstrucao\b|\breforma\b|\bedificacao|\bimovel|\bimoveis|"
     r"\bsede\b|\bbenfeitoria|\bequipamento(s)?\b|\bmobiliario|\bmaquina(s)?\b|"
     r"\bveiculo(s)?\b(?!.*loca)|\baquisicao de (bem|bens)|\bsoftware\b|"
     r"\blicenca de uso|\bimobilizado|\bintangivel|investimento"),

    # --- financeiro / tributário / judicial ---
    ("FINANCEIRA_JUDICIAL",
     r"\bjuros\b|\bmulta(s)?\b|\bencargo(s)? financeiro|\btarifa bancaria|"
     r"\biof\b|\bimposto|\btributo|\btaxa(s)? \w*|\biss\b|\bpis\b|\bcofins\b|"
     r"\bprecatorio|\bsentenca|\bdeposito judicial|\bcusta(s)? judicia|"
     r"\bacordo judicial|\brestituicao|\bdevolucao de "),

    # --- custeio administrativo (mais genérico: por último) ---
    ("CUSTEIO_ADMIN",
     r"\baluguel|\blocacao\b|\bcondominio|\benergia eletrica|\bagua e esgoto|"
     r"\btelefon|\btelecomunicacao|\binternet\b|\blink de dados|\bdados moveis|"
     r"\bcorreio|\bpostagem|\bmaterial de (consumo|expediente|limpeza)|"
     r"\blimpeza\b|\bvigilancia\b|\bseguranca patrimonial|\bportaria\b|"
     r"\brecepcao\b|\bcopa\b|\bmanutencao\b|\bconservacao\b|\bseguro\b|"
     r"\bcombustivel|\bfrota\b|\bveiculo(s)? locad|\bpassagem aerea corporativa|"
     r"\binformatica\b|\btecnologia da informacao\b|\bdatacenter|"
     r"\bhospedagem de (site|sistema)|"
     r"\bsuprimento|\bgrafica\b|\bimpressao\b|\bxerox|\breprografia|"
     r"\bagua mineral|\bterceiriza|\bmao de obra|\bapoio administrativo|"
     r"\bservico(s)? de terceiro|\bpessoa juridica\b|\bdespesa(s)? diversa"),
]

_COMPILADAS = [(cat, re.compile(rx)) for cat, rx in REGRAS]


def normalizar(texto) -> str:
    if not isinstance(texto, str):
        return ""
    t = unicodedata.normalize("NFD", texto)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip().lower()


def classificar_texto(texto: str) -> str:
    """Categoria de finalidade de um nome de conta / centro de custo."""
    t = normalizar(texto)
    if not t:
        return "OUTROS"
    for cat, rx in _COMPILADAS:
        if rx.search(t):
            return cat
    return "OUTROS"


def classificar_series(*textos: pd.Series) -> pd.Series:
    """Classifica combinando várias colunas de texto (conta, centro de custo...).

    Usa o primeiro texto que produzir categoria diferente de OUTROS, na ordem
    em que as colunas foram passadas (a mais confiável primeiro).
    """
    if not textos:
        return pd.Series(dtype=str)
    base = textos[0]
    resultado = pd.Series("OUTROS", index=base.index, dtype=object)
    for serie in textos:
        pendentes = resultado == "OUTROS"
        if not pendentes.any():
            break
        s = serie.reindex(base.index) if serie is not base else serie
        distintos = s[pendentes].fillna("").astype(str)
        mapa = {v: classificar_texto(v) for v in distintos.unique()}
        resultado.loc[pendentes] = distintos.map(mapa)
    return resultado


def macro(categoria: pd.Series) -> pd.Series:
    return categoria.map(MACRO).fillna("Não classificado")


# ---------------------------------------------------- quem recebeu a verba
# Quando o favorecido vem agregado ("DIVERSOS FUNCIONÁRIOS"), a identidade da
# pessoa se perde — mas a CONTA CONTÁBIL ainda diz a que categoria ela
# pertence: "Diarias com Conselheiros", "Locomoção de Inspetores". É o que
# permite responder quem recebe a verba mesmo sem nome do beneficiário.
CATEGORIAS_BENEFICIARIO = {
    "CONSELHEIRO": "Conselheiros",
    "INSPETOR": "Inspetores",
    "DIRECAO": "Direção (presidência, diretoria)",
    "FUNCIONARIO": "Funcionários (quadro)",
    "COLABORADOR": "Colaboradores e estagiários",
    "INDEFINIDO": "Categoria não declarada na conta",
}

# Estrutura política eleita/indicada × quadro contratado. A separação é o
# núcleo da pergunta: quem decide o próprio pagamento está do lado de cá.
POLITICOS = ("CONSELHEIRO", "INSPETOR", "DIRECAO")
QUADRO = ("FUNCIONARIO", "COLABORADOR")

_REGRAS_BENEFICIARIO: list[tuple[str, str]] = [
    ("CONSELHEIRO", r"\bconselheir|\bjeton|\bplenari|\bcamara especializada"),
    ("INSPETOR", r"\binspetor"),
    ("DIRECAO", r"\bdiretor|\bpresident|\bdiretoria\b|\bgabinete\b"),
    ("COLABORADOR", r"\bcolaborador|\bestagiari|\baprendiz|\bterceiriz"),
    ("FUNCIONARIO", r"\bfuncionari|\bservidor|\bempregad|\bpessoal\b|\bquadro\b"),
]
_BENEFICIARIO_COMPILADAS = [(c, re.compile(rx)) for c, rx in _REGRAS_BENEFICIARIO]


def categoria_beneficiario(nome_conta: str) -> str:
    """A que categoria de pessoa a conta destina a verba."""
    t = normalizar(nome_conta)
    if not t:
        return "INDEFINIDO"
    for cat, rx in _BENEFICIARIO_COMPILADAS:
        if rx.search(t):
            return cat
    return "INDEFINIDO"


def refinar_residuo(rotulos: pd.Series, categorias: pd.Series,
                    valores: pd.Series, *, top: int = 250) -> tuple[pd.Series, dict]:
    """Manda ao LLM só o resíduo `OUTROS`, priorizado por R$.

    A regra determinística resolve a massa; o que sobra são nomes de conta
    genéricos ou idiossincráticos, em que um classificador de linguagem se sai
    melhor que regex. Três salvaguardas: só as contas que concentram valor vão
    (num modelo local o custo é tempo de GPU, e o benefício está onde o
    dinheiro está), a resposta só é aceita nas categorias existentes, e a
    origem de cada classificação fica registrada — nada do que o modelo
    decidir se confunde com regra.

    Sem Ollama no ar devolve as categorias intactas.
    """
    from . import llm

    info = {"llm_disponivel": llm.disponivel(), "reclassificadas": 0,
            "valor_reclassificado": 0.0}
    pendentes = categorias == "OUTROS"
    if not pendentes.any() or not llm.disponivel():
        return categorias, info

    fila = (pd.DataFrame({"rotulo": rotulos[pendentes].fillna("").astype(str),
                          "valor": valores[pendentes]})
            .groupby("rotulo")["valor"].sum().sort_values(ascending=False))
    fila = fila[fila.index.str.strip() != ""].head(top)
    if fila.empty:
        return categorias, info

    mapa = {t: c for t, c in llm.classificar_rubricas(list(fila.index)).items()
            if c in GRUPOS and c != "OUTROS"}
    if not mapa:
        return categorias, info

    novo = categorias.copy()
    alvo = pendentes & rotulos.fillna("").astype(str).isin(mapa)
    novo.loc[alvo] = rotulos[alvo].map(mapa)
    info["reclassificadas"] = int(alvo.sum())
    info["valor_reclassificado"] = round(float(valores[alvo].sum()), 2)
    info["rubricas"] = len(mapa)
    log.info("LLM resolveu %d rubrica(s) do resíduo (%d linhas, R$ %.2f)",
             len(mapa), info["reclassificadas"], info["valor_reclassificado"])
    return novo, info


# ================================================================= EIXO B
# FUNÇÃO — para que serve a unidade que gastou. Lido do centro de custo.
FUNCOES = {
    "FISCALIZACAO": "Fiscalização do exercício profissional",
    "ATENDIMENTO": "Atendimento e registro profissional",
    "POLITICO": "Órgãos políticos (plenário, câmaras, inspetorias)",
    "DIRECAO_SUPERIOR": "Direção superior (presidência, diretoria, gabinete)",
    "ADMINISTRACAO": "Administração e apoio (meio)",
    "TRANSFERENCIA": "Transferências legais ao sistema",
    "NAO_IDENTIFICADA": "Função não identificada",
}

# Ordem de precedência. Direção superior vem antes de administração porque
# "Gabinete da Presidência" casaria com regras genéricas de administração; e
# os órgãos políticos vêm antes porque "Câmara Especializada de Engenharia
# Civil" contém "engenharia", que aparece em várias unidades técnicas.
REGRAS_FUNCAO: list[tuple[str, str]] = [
    ("FISCALIZACAO",
     r"\bfiscaliza|\bfiscal(is|izacao)?\b|\bvistoria|agente(s)? fiscal|"
     r"auto(s)? de infra|\bdenuncia|\bperiferia de fiscal|"
     r"unidade(s)? de fiscaliza|\bufis\b|coordenadoria de fiscaliza"),

    ("ATENDIMENTO",
     r"\batendimento\b|\bregistro\b|\bacervo\b|\bart\b|\bcat\b|"
     r"anotacao de responsabilidade|carteira profissional|\bprotocolo\b|"
     r"posto(s)? de (atendimento|servico)|\bugi\b|unidade(s)? de gestao integrada|"
     r"central de relacionamento|\bcadastro\b|\bcertidao|\bcertidoes"),

    # CIPA e comissões administrativas ANTES de POLITICO: `\bcomissao\b`
    # sozinho puxava a Comissão Interna de Prevenção de Acidentes para
    # "órgãos políticos". Ética, Orçamento e Tomada de Contas continuam
    # colegiados e caem em POLITICO abaixo.
    ("ADMINISTRACAO",
     r"comissao interna de prevencao|\bcipa\b|comissao de licitac|"
     r"comissao de sindicancia|comissao de compras"),

    ("POLITICO",
     r"\bplenario\b|\bplenaria\b|camara(s)? especializada|\bcolegiado|"
     r"\bconselheiro|\bconselho diretor|\binspetoria|\bcomissao\b|"
     r"\bentidade(s)? de classe|\bcoordenadoria de camara|\bjuntas?\b|"
     r"\bmandatario|\brepresentacao institucional"),

    ("DIRECAO_SUPERIOR",
     r"\bpresidencia\b|\bpresidente\b|\bgabinete\b|\bdiretoria\b|\bdiretor\b|"
     r"\bsuperintendencia\b|\bsuperintendente\b|\bchefia de gabinete|"
     r"\bassessoria (da |de )?(presidencia|diretoria|superintendencia)|"
     r"\balta administracao|\bvice-?presidencia"),

    ("ADMINISTRACAO",
     # "FINANÇAS" normaliza para "financas" e NÃO casava `\bfinanceir`;
     # "GESTAO DE PESSOAS" não casava `\bpessoal\b`; "ADMINISTRAÇÃO" não
     # casava `\badministrativ`. Eram R$ 1,06 bi caindo em "não identificada"
     # e inflando artificialmente o resíduo da matriz natureza × função.
     r"\badministra|\brecursos humanos|\brh\b|\bpessoal\b|\bpessoas\b|"
     r"\bfinanceir|\bfinanca|\bcontabil|\borcament|\bjuridic|"
     r"\bprocuradoria\b|\bcompras\b|\blicitac|\bcontratos\b|"
     r"\bpatrimonio\b|\balmoxarifado\b|\btecnologia da informacao|"
     r"\binformatica\b|\bsistemas\b|\bcomunicacao\b|\bmarketing\b|"
     r"\bouvidoria\b|\bauditoria\b|\bcontroladoria\b|\bcorregedoria\b|"
     r"\bmanutencao\b|\bservicos gerais|\bsede\b|\bpredio\b|"
     r"\btransporte\b|\bfrota\b|\bplanejamento\b|\bapoio\b|"
     r"\bsuporte\b|\bsuprimento|\blogistica\b|\binfraestrutura\b|"
     r"\bexperiencia e (atendimento|jornada)"),

    # Genérica de estrutura, por ÚLTIMO. Vem depois de fiscalização e
    # atendimento de propósito: senão a Gerência de Fiscalização e a Gerência
    # de Atendimento ao Cliente virariam administração.
    ("ADMINISTRACAO",
     r"\bgerencia\b|\bequipe\b|\bsecretaria executiva\b|\bchefia\b|"
     r"\bdepartamento\b|\bdepto\b|\bdivisao\b|\bsetor\b|"
     r"\bcoordenadoria\b|\bunidade\b"),
]

_FUNCAO_COMPILADAS = [(cat, re.compile(rx)) for cat, rx in REGRAS_FUNCAO]

# Função é atributo da UNIDADE, não da compra; mas quando não há centro de
# custo, a natureza da conta ainda carrega um sinal fraco de função. Este mapa
# só é usado como último recurso e o resultado é marcado como inferido.
_NATUREZA_PARA_FUNCAO = {
    # repasse ao Confea/Mútua é imposição legal, não escolha de gestão: sem
    # coluna própria, R$ 431,8 mi inchavam a "máquina administrativa"
    "TRANSFERENCIAS": "TRANSFERENCIA",
    "FISCALIZACAO": "FISCALIZACAO",
    "ATENDIMENTO": "ATENDIMENTO",
    "INSTITUCIONAL": "POLITICO",
    "PUBLICIDADE": "ADMINISTRACAO",
    "SERVICOS_PROFISSIONAIS": "ADMINISTRACAO",
    "CUSTEIO_ADMIN": "ADMINISTRACAO",
}


def classificar_funcao(texto: str) -> str:
    """Função da unidade que gastou, a partir do nome do centro de custo."""
    t = normalizar(texto)
    if not t:
        return "NAO_IDENTIFICADA"
    for cat, rx in _FUNCAO_COMPILADAS:
        if rx.search(t):
            return cat
    return "NAO_IDENTIFICADA"


def classificar_funcao_series(centro_custo: pd.Series,
                              natureza: pd.Series | None = None) -> pd.Series:
    """Função linha a linha, com fallback fraco pela natureza da despesa."""
    s = centro_custo.fillna("").astype(str)
    mapa = {v: classificar_funcao(v) for v in s.unique()}
    resultado = s.map(mapa)
    if natureza is not None:
        pendentes = resultado == "NAO_IDENTIFICADA"
        if pendentes.any():
            inferido = natureza[pendentes].map(_NATUREZA_PARA_FUNCAO)
            resultado.loc[pendentes] = inferido.fillna("NAO_IDENTIFICADA")
    return resultado


def matriz(natureza: pd.Series, funcao: pd.Series,
           valores: pd.Series) -> pd.DataFrame:
    """Matriz natureza × função em R$ — o cruzamento que separa
    'gasta com pessoal' de 'gasta com pessoal que não fiscaliza'."""
    d = pd.DataFrame({"natureza": natureza, "funcao": funcao, "valor": valores})
    if d.empty:
        return pd.DataFrame()
    m = d.pivot_table(index="natureza", columns="funcao", values="valor",
                      aggfunc="sum", fill_value=0.0)
    ordem_f = [f for f in FUNCOES if f in m.columns]
    ordem_n = [n for n in GRUPOS if n in m.index]
    m = m.reindex(index=ordem_n, columns=ordem_f, fill_value=0.0)
    m.index = [GRUPOS.get(i, i) for i in m.index]
    m.columns = [FUNCOES.get(c, c) for c in m.columns]
    return m.round(2)


def cobertura(categorias: pd.Series, valores: pd.Series) -> dict:
    """Quanto do dinheiro ficou sem classificação — mede a confiabilidade."""
    total = float(valores.sum())
    if total <= 0:
        return {"total": 0.0, "pct_nao_classificado": 0.0}
    nao = float(valores[categorias == "OUTROS"].sum())
    return {
        "total": round(total, 2),
        "valor_nao_classificado": round(nao, 2),
        "pct_nao_classificado": round(100 * nao / total, 2),
    }


def maiores_nao_classificados(textos: pd.Series, categorias: pd.Series,
                              valores: pd.Series, top: int = 40) -> pd.DataFrame:
    """Contas sem categoria ordenadas por valor — fila de trabalho do LLM
    (e checklist para melhorar as regras determinísticas)."""
    d = pd.DataFrame({"texto": textos, "cat": categorias, "valor": valores})
    d = d[d["cat"] == "OUTROS"]
    if d.empty:
        return pd.DataFrame()
    return (d.groupby("texto")["valor"].agg(total="sum", ocorrencias="size")
            .reset_index().sort_values("total", ascending=False).head(top))
