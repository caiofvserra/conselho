"""Regressões da rodada 19 — a palavra "fiscal" que não é fiscalização.

O operador levantou a suspeita certa pelo motivo certo: a conta contábil de
fiscalização estaria inflada com despesa que não é fiscalização. Ao conferir,
parte da inflação era NOSSA — o regex `\\bfiscal(is)?\\b` classificava como
fiscalização do exercício profissional:

    Conselho Fiscal ................ órgão de controle interno
    Nota fiscal / Notas fiscais .... documento de despesa
    Fiscal de contrato ............. gestão contratual
    Fiscalização de processos ...... auditoria interna

E o mesmo regex NÃO casava o plural real: "fiscais" não é `fiscal(is)?`, de
modo que "Fiscais de contrato" caía em OUTROS enquanto "Fiscal de contrato"
caía em FISCALIZACAO. Duas linhas do mesmo relatório, dois destinos.

POR QUE ISSO IMPORTA MAIS DO QUE PARECE: o denominador de
`finalistica-verbas-vs-fiscalizacao` — a frase mais citada do relatório, "a
verba indenizatória superou o gasto com fiscalização em 5 de 5 exercícios" —
sai daí. Denominador inflado empurra a razão para BAIXO, ou seja, o erro
trabalhava CONTRA a tese. Não importa: número errado a favor continua sendo
número errado, e num contraditório é ele que derruba a peça inteira.

A rodada cobre ainda as duas consequências disso:

* o DENOMINADOR ABERTO — a medida de fiscalização deixa de ser um total
  fechado e passa a sair rótulo a rótulo, com o peso de cada um. Sem isso,
  nem quem publica nem quem contesta consegue ver o que está lá dentro;
* a TRIAGEM POR LLM não inventa — resposta sem trecho literal conferível no
  próprio rótulo é descartada, e com o modelo desligado a lista sai vazia.

Tudo offline: não há rede aqui, só a taxonomia.
"""
from __future__ import annotations

from .. import taxonomia as T
from .rodada12 import _erro

# (texto, é fiscalização do exercício profissional?)
_CASOS: tuple[tuple[str, bool], ...] = (
    # a palavra existe e NÃO é fiscalização do exercício profissional
    ("Conselho Fiscal", False),
    ("Conselhos Fiscais", False),
    ("Nota fiscal - conferencia", False),
    ("Notas fiscais", False),
    ("Cupom fiscal", False),
    ("Documento fiscal", False),
    ("Livros fiscais", False),
    ("Fiscal de contrato", False),
    ("Fiscais de contrato", False),
    ("Gestores e fiscais de contrato", False),
    ("Fiscalizacao de contratos", False),
    ("Fiscalizacao de processos internos", False),
    ("Auditoria interna", False),
    ("Fiscais e gestores de contratos", False),
    ("Escrituracao fiscal", False),
    ("Obrigacoes fiscais acessorias", False),
    ("Responsabilidade fiscal", False),
    ("Incentivos fiscais", False),
    ("Beneficio fiscal", False),
    ("Cupons fiscais", False),
    # e o que É — nenhum destes pode ser perdido pela guarda
    ("Departamento de Fiscalizacao", True),
    ("Vistoria de obras", True),
    ("Agentes fiscais - campo", True),
    ("Fiscais em diligencia", True),
    ("Auto de infracao", True),
    ("Autos de infracao - julgamento", True),
    ("Unidade de Fiscalizacao", True),
    ("Coordenadoria de Fiscalizacao", True),
    ("Fiscalizacao do exercicio profissional", True),
    ("Fiscal de obra", True),
    ("Agente fiscal", True),
    ("Gerencia de Fiscalizacao", True),
)


def _a_palavra_fiscal_nao_basta(erros: list[str]) -> None:
    """A guarda vale nos DOIS eixos: natureza da despesa e função da unidade."""
    for texto, e_fiscalizacao in _CASOS:
        natureza = T.classificar_texto(texto)
        funcao = T.classificar_funcao(texto)
        if e_fiscalizacao:
            _erro(erros, natureza == "FISCALIZACAO",
                  f"taxonomia: {texto!r} É fiscalização e saiu como natureza "
                  f"{natureza!r} — a guarda comeu um caso verdadeiro")
            _erro(erros, funcao == "FISCALIZACAO",
                  f"taxonomia: {texto!r} É fiscalização e saiu com função {funcao!r}")
        else:
            _erro(erros, natureza != "FISCALIZACAO",
                  f"taxonomia: {texto!r} NÃO é fiscalização do exercício profissional "
                  f"e entrou como natureza FISCALIZACAO — infla o denominador da "
                  f"comparação verba × fiscalização")
            _erro(erros, funcao != "FISCALIZACAO",
                  f"taxonomia: {texto!r} NÃO é fiscalização e entrou com função "
                  f"FISCALIZACAO — infla o denominador")


def _plural_e_singular_no_mesmo_lado(erros: list[str]) -> None:
    """Foi a incoerência que denunciou o defeito: o singular ia para um lado e o
    plural para outro, no mesmo relatório."""
    pares = (("Fiscal de contrato", "Fiscais de contrato"),
             ("Agente fiscal", "Agentes fiscais"),
             ("Conselho Fiscal", "Conselhos Fiscais"))
    for singular, plural in pares:
        a, b = T.classificar_texto(singular), T.classificar_texto(plural)
        _erro(erros, a == b,
              f"taxonomia: {singular!r} deu {a!r} e {plural!r} deu {b!r} — singular e "
              "plural do mesmo termo não podem cair em categorias diferentes")


def _a_guarda_nao_e_generica(erros: list[str]) -> None:
    """A exclusão precisa ser cirúrgica: se ela derrubar qualquer texto com
    "fiscal", mata a medida inteira em vez de corrigi-la."""
    _erro(erros, T._e_fiscalizacao_de_verdade("departamento de fiscalizacao"),
          "a guarda derrubou o caso mais central de todos")
    _erro(erros, not T._e_fiscalizacao_de_verdade("conselho fiscal"),
          "a guarda deixou passar 'conselho fiscal'")


def _o_denominador_abre(erros: list[str]) -> None:
    """A medida de fiscalização tem de sair aberta, e a abertura tem de somar
    exatamente o total — nem rótulo perdido, nem rótulo de fora."""
    import pandas as pd

    from ..checks import finalistica as F

    base = pd.DataFrame({
        "_ano": [2024, 2024, 2024, 2024],
        "valor": [100.0, 50.0, 25.0, 900.0],
        "categoria": ["VERBAS_PESSOAIS", "REMUNERACAO", "FISCALIZACAO",
                      "CUSTEIO_ADMIN"],
        "funcao": ["FISCALIZACAO", "FISCALIZACAO", "FISCALIZACAO",
                   "ADMINISTRACAO"],
        "unidade": ["Depto de Fiscalizacao"] * 3 + ["Superintendencia"],
        "rotulo": ["Depto de Fiscalizacao / Diarias no Pais",
                   "Depto de Fiscalizacao / Vencimentos",
                   "Depto de Fiscalizacao / Acoes de Fiscalizacao",
                   "Superintendencia / Aluguel"],
    })
    achados = F._abrir_a_fiscalizacao(base, "funcao")
    comp = [a for a in achados if a.id == "finalistica-fiscalizacao-composicao"]
    _erro(erros, len(comp) == 1,
          "a composição da fiscalização não foi publicada — o denominador "
          "voltou a ser um total fechado, que ninguém consegue conferir")
    if not comp:
        return
    ev = comp[0].evidencia
    _erro(erros, abs(float(ev["valor"].sum()) - 175.0) < 0.01,
          f"a abertura somou {float(ev['valor'].sum())}, e a medida é 175 — "
          "abertura que não fecha com o total publicado é pior que não abrir")
    _erro(erros, not ev["rótulo"].str.contains("Aluguel").any(),
          "a abertura trouxe um rótulo que não está na medida de fiscalização")
    # e o achado mais citável: metade do que se chama fiscalização é diária
    linha = ev[ev["rótulo"].str.contains("Diarias")]
    _erro(erros, not linha.empty and abs(float(linha["% da fiscalização"].iloc[0]) - 57.1) < 0.2,
          "o percentual por rótulo saiu errado — é ele que mostra o peso de "
          "cada coisa dentro da medida")


def _o_modelo_nao_inventa(erros: list[str]) -> None:
    """A triagem por LLM não pode produzir linha sem lastro no texto auditado, e
    com o modelo desligado não pode produzir linha nenhuma."""
    from .. import llm

    rubrica = "GERENCIA DE ENGENHARIA - GENG / Reformas"
    _erro(erros, llm._trecho_confere("Colegio de Inspetores",
                                     "Evento — Colégio de Inspetores 2024"),
          "o trecho literal existe no rótulo e foi rejeitado (a comparação tem "
          "de ser frouxa em acento e caixa)")
    _erro(erros, not llm._trecho_confere("vistoria de obra",
                                         "Evento — Colégio de Inspetores 2024"),
          "trecho que NÃO está no rótulo passou na conferência")
    _erro(erros, not llm._trecho_confere("", "qualquer coisa"),
          "trecho vazio passou na conferência")
    # O defeito da rodada real: a rubrica inteira é substring dela mesma, e as
    # 20 linhas em que o modelo devolveu a rubrica como "trecho" passaram na
    # conferência que o relatório anuncia como barreira contra alucinação.
    _erro(erros, not llm._trecho_confere(rubrica, rubrica),
          "a rubrica INTEIRA passou como trecho — repetir o enunciado não "
          "seleciona nada, e a barreira volta a não filtrar coisa alguma")
    _erro(erros, llm._trecho_confere("Reformas", rubrica),
          "um pedaço legítimo do rótulo foi rejeitado")

    # Eco do enunciado: o modelo devolveu a definição do próprio prompt como
    # "motivo" na linha de MAIOR valor da rodada real.
    _erro(erros, llm._e_eco_do_prompt(
        "Verifica se quem exerce engenharia tem registro e responsabilidade tecnica."),
        "o motivo que repete a definição do prompt não foi detectado como eco")
    for motivo in ("Nota fiscal e documento de despesa, nao fiscalizacao do "
                   "exercicio profissional",
                   "Conselho fiscal e orgao de controle interno",
                   "Diarias com Inspetores sugerem atividade de fiscalizacao"):
        _erro(erros, not llm._e_eco_do_prompt(motivo),
              f"motivo legítimo acusado de eco: {motivo!r} — o detector não pode "
              "confundir vocabulário obrigatório do domínio com citação")
    _erro(erros, llm.auditar_enquadramento({}) == [],
          "a auditoria devolveu algo sem rubrica nenhuma para auditar")
    # A definição tem de ser a MESMA nos dois lugares, senão o detector e o
    # prompt divergem e o eco volta a passar.
    _erro(erros, llm._DEFINICAO_FISCALIZACAO in llm._SISTEMA_AUDITORIA,
          "a definição usada pelo detector de eco saiu do prompt de sistema")


def _a_varredura_paginada_nao_congela(erros: list[str]) -> None:
    """`--retomar` decide por "tem linhas em staged", e a varredura de acórdãos
    do TCU grava linhas já na primeira fatia. Sem uma regra própria, a segunda
    rodada pula a etapa para sempre e a varredura congela parecendo concluída —
    no caderno do operador, que roda `all --retomar` em toda reconexão, isso é
    invisível."""
    import json
    import tempfile
    from pathlib import Path

    from .. import config, pipeline

    raw_real = config.RAW_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config.RAW_DIR = Path(tmp)
            pasta = config.RAW_DIR / "tcu-acordaos"
            pasta.mkdir(parents=True)
            arquivo = pasta / "progresso.json"

            arquivo.write_text(json.dumps(
                {"proximo_inicio": 1500, "varridos": 1500, "fim_da_base": False}),
                encoding="utf-8")
            _erro(erros, bool(pipeline._varredura_paginada_pendente("tcu_acordaos")),
                  "varredura do TCU parada no registro 1500 foi dada como pronta "
                  "— a etapa seria pulada para sempre e nunca chegaria ao fim")

            arquivo.write_text(json.dumps(
                {"proximo_inicio": 0, "varridos": 90000, "fim_da_base": True}),
                encoding="utf-8")
            _erro(erros, not pipeline._varredura_paginada_pendente("tcu_acordaos"),
                  "base percorrida até o fim e a etapa continua pendente — "
                  "toda rodada voltaria à rede sem ter o que buscar")

            arquivo.unlink()
            _erro(erros, not pipeline._varredura_paginada_pendente("tcu_acordaos"),
                  "sem progresso.json a regra normal do --retomar é que decide")
            _erro(erros, not pipeline._varredura_paginada_pendente("pagamentos"),
                  "a regra vazou para uma etapa que não é varredura paginada")
    finally:
        config.RAW_DIR = raw_real


def _o_achado_nao_pode_se_acusar_sozinho(erros: list[str]) -> None:
    """Um número que só pode dar um valor não mede nada.

    O achado "N unidades territoriais com menos de 5% da despesa em contas de
    fiscalização" saía com 0,0% para TODAS as unidades, sempre — porque a
    camada CAMPO casa "UGI" e a regra de função manda "UGI" para ATENDIMENTO.
    A interseção CAMPO ∩ FISCALIZAÇÃO era vazia por desenho, e o achado
    oferecia como hipótese que "a unidade opera como posto administrativo".

    A guarda pergunta à própria taxonomia, com os nomes reais da rodada, se
    alguma unidade territorial PODERIA ser lida como de fiscalização. Este
    teste percorre os dois ramos.
    """
    import pandas as pd

    from ..checks import superintendencias as S

    def rodar(nomes_e_valores):
        linhas = []
        for unidade, natureza, valor in nomes_e_valores:
            linhas.append({"_ano": 2024, "valor": valor, "natureza": natureza,
                           "unidade": unidade})
        d = pd.DataFrame(linhas)
        d["camada"] = d["unidade"].map(S.camada)
        d["camada_rotulo"] = d["camada"].map(S.ROTULO_CAMADA)
        d["funcao"] = [T.classificar_funcao(u) for u in d["unidade"]]
        return d

    # RAMO 1 — a regra não alcança o território: só UGIs, todas atendimento.
    d1 = rodar([("UGI Santos", "REMUNERACAO", 900_000.0),
                ("UGI Campinas", "CUSTEIO_ADMIN", 800_000.0)])
    _erro(erros, set(d1["camada"]) == {"CAMPO"},
          "a fixture do teste não produziu unidades de campo — o teste não "
          "estaria exercitando nada")
    _erro(erros, not any(f == "FISCALIZACAO" for f in d1["funcao"]),
          "uma UGI foi lida como de fiscalização: o ramo 1 deixou de existir")

    # RAMO 2 — o órgão nomeia uma unidade de campo pela função, e aí a
    # comparação passa a medir alguma coisa.
    d2 = rodar([("UGI Santos", "REMUNERACAO", 900_000.0),
                ("UGI Bauru - Fiscalização", "FISCALIZACAO", 800_000.0)])
    _erro(erros, any(f == "FISCALIZACAO" for f in d2["funcao"]),
          "nenhuma unidade territorial pôde ser lida como de fiscalização nem "
          "quando o nome dela diz 'Fiscalização' — a guarda ficaria presa no "
          "ramo do limite para sempre")
    _erro(erros, S.camada("UGI Bauru - Fiscalização") == "CAMPO",
          "a unidade de campo com nome de fiscalização saiu de outra camada")


def _cpf_mascarado_nao_e_cpf_ausente(erros: list[str]) -> None:
    """Documento protegido por lei não pode ser publicado como ocultação.

    O portal grava o CPF parcial ("***456789**"), que é o que o Decreto
    7.724/2012 e a LGPD mandam. A regra antiga (`dígitos < 11`) chamava isso de
    "sem beneficiário identificado" e o relatório publicou R$ 256 milhões nessa
    conta — bastava abrir uma linha do portal, onde o NOME está, para derrubar.
    """
    import pandas as pd

    from ..checks import opacidade as O

    pg = pd.DataFrame({
        "FAVORECIDO_NORM": ["FULANO DE TAL", "SICRANA DE TAL",
                            "DIVERSOS FUNCIONARIOS", "BELTRANO"],
        "CPF_CNPJ": ["***456789**", "12345678901", "", ""],
        "VALOR": [100.0, 200.0, 300.0, 400.0],
    })
    ausente, mascarado = O._documento(pg)
    _erro(erros, bool(mascarado.iloc[0]) and not bool(ausente.iloc[0]),
          "CPF mascarado foi lido como documento ausente — é o formato que a "
          "LGPD prescreve, e chamá-lo de ocultação derruba o achado inteiro")
    _erro(erros, not bool(mascarado.iloc[1]) and not bool(ausente.iloc[1]),
          "CPF completo entrou como mascarado ou ausente")
    _erro(erros, bool(ausente.iloc[3]),
          "documento realmente ausente deixou de ser detectado")

    agr = O._agregadores(pg)
    _erro(erros, not bool(agr.iloc[0]),
          "o pagamento com CPF mascarado e nome publicado entrou no conjunto "
          "'não se sabe quem recebeu'")
    _erro(erros, bool(agr.iloc[2]),
          "o rótulo coletivo 'DIVERSOS FUNCIONARIOS' saiu do conjunto opaco — "
          "é o caso central do achado")
    _erro(erros, bool(agr.iloc[3]),
          "pagamento sem documento nenhum saiu do conjunto opaco")


def _totalizador_nunca_entra_na_soma(erros: list[str]) -> None:
    """A conta sintética totaliza as filhas: somar as duas dobra a despesa.

    A forma antiga, `d[d["ANALITICA"].astype(bool)]`, funciona hoje porque o
    leitor de staged devolve a coluna como booleano. Em coluna de TEXTO ela
    devolve True para a string "False" — e o totalizador volta para a soma sem
    nenhum aviso, com o total ainda parecendo plausível. Basta o portal gravar
    "S"/"N" para o bug voltar.
    """
    import pandas as pd

    from ..utils import so_analiticas

    for coluna in ("ANALITICA", "ANALITICO"):
        for valores, esperado in ((["True", "False"], 10.0),
                                  ([True, False], 10.0),
                                  (["S", "N"], 10.0),
                                  (["1", "0"], 10.0)):
            d = pd.DataFrame({coluna: valores, "v": [10.0, 999.0]})
            obtido = float(so_analiticas(d)["v"].sum())
            _erro(erros, abs(obtido - esperado) < 0.01,
                  f"so_analiticas({coluna}={valores!r}) somou {obtido} e devia "
                  f"somar {esperado} — o totalizador entrou na conta")
    # sem a coluna, nada é filtrado
    d = pd.DataFrame({"v": [10.0, 999.0]})
    _erro(erros, abs(float(so_analiticas(d)["v"].sum()) - 1009.0) < 0.01,
          "so_analiticas filtrou linhas numa base que não declara conta analítica")


def _tabela_truncada_declara_o_corte(erros: list[str]) -> None:
    """Quem soma o CSV tem de chegar ao número do título — ou saber por que não.

    Três achados caíram na conferência da rodada pelo mesmo motivo: o arquivo
    tinha exatamente 100 linhas, o título somava o conjunto inteiro, e nada no
    arquivo dizia que houve corte.
    """
    import pandas as pd

    from ..utils import topo

    d = pd.DataFrame({"u": [f"U{i}" for i in range(150)],
                      "valor": [float(150 - i) for i in range(150)]})
    t = topo(d, 120, "valor")
    corte = t.attrs.get("_corte") or {}
    _erro(erros, len(t) == 120, f"topo devolveu {len(t)} linhas, e o teto era 120")
    _erro(erros, corte.get("linhas_totais") == 150,
          "o corte não registrou quantas linhas existiam de verdade")
    _erro(erros, abs(float(corte.get("valor_omitido", 0)) - 465.0) < 0.01,
          f"o valor omitido saiu {corte.get('valor_omitido')} e devia ser 465,00 "
          "— é ele que explica a diferença entre o CSV e o título")
    _erro(erros, "_corte" not in topo(d.head(50), 120, "valor").attrs,
          "tabela que coube inteira foi marcada como truncada")


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_a_palavra_fiscal_nao_basta, _plural_e_singular_no_mesmo_lado,
                  _a_guarda_nao_e_generica, _o_denominador_abre,
                  _o_modelo_nao_inventa, _a_varredura_paginada_nao_congela,
                  _o_achado_nao_pode_se_acusar_sozinho,
                  _cpf_mascarado_nao_e_cpf_ausente,
                  _totalizador_nunca_entra_na_soma,
                  _tabela_truncada_declara_o_corte):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
