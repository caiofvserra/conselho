"""Frente — Quem ocupa o plenário e para onde vai o dinheiro institucional.

O plenário de um Crea não é eleito pelos profissionais diretamente: as
**entidades de classe** registradas indicam os conselheiros, e o colégio
eleitoral que escolhe a diretoria é formado por essas mesmas entidades. Quem
tem assento decide sobre convênio, contrato e verba — inclusive os que
beneficiam a própria entidade que o indicou.

O que esta frente mede, e o que ela **não** mede:

* **Mede**: quanto dinheiro o conselho repassa às entidades que compõem sua
  base política, separado por qual interesse cada entidade representa
  (empresarial/patronal, profissional, ensino); quais delas recebem por dois
  canais ao mesmo tempo (convênio e contrato); e, quando o rol de mandatários
  está disponível, a interseção entre ter assento e receber recurso.
* **Não mede**: se um conselheiro específico votou a favor de quem o indicou.
  Isso exige a ata nominal da votação. Sem ela, o que existe aqui é o
  **vínculo financeiro** entre o conselho e sua base — que é a condição
  necessária do conflito, não sua prova.

A classificação de interesse é heurística, por nome da entidade, e vem
publicada na evidência justamente para poder ser corrigida.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from ..findings import Finding
from ..utils import coluna, fmt_brl, norm_nome
from .common import df

FRENTE = "Representação e interesses"

# --------------------------------------------------------------- interesses
# Ordem importa: patronal antes de profissional, porque "sindicato da indústria
# da construção" contém "sindicato", e escola antes de tudo por ser inequívoca.
INTERESSES: list[tuple[str, str]] = [
    ("ENSINO",
     r"universidade|faculdade|centro universitario|instituto federal|"
     r"\bunesp\b|\busp\b|\bunicamp\b|\bfatec\b|\betec\b|\bescola\b|"
     r"fundacao educacional|centro (de )?ensino|colegio"),

    ("PATRONAL",
     r"sinduscon|\bfiesp\b|\bciesp\b|sindicato da industria|sindicato das "
     r"industrias|sindicato das empresas|sindicato patronal|"
     r"federacao das industrias|associacao comercial|\bsecovi\b|\bsinaenco\b|"
     r"\bsinicesp\b|\bcbic\b|\babrainc\b|camara (dos |de )?dirigentes|"
     r"associacao (das |de )?empresas|sindicato (dos )?empregadores|"
     r"associacao brasileira (da |de )?industria|\bapeop\b|"
     r"sindicato (da |das )?(construcao|habitacao|empresas)"),

    # Conselhos profissionais são pares institucionais, não entidades de
    # classe: CFT, CFTA e CAU disputam atribuição COM o Crea.
    ("CONSELHO_PROFISSIONAL",
     r"conselho federal dos tecnicos|conselho regional dos tecnicos|"
     r"\bcft\b|\bcfta\b|\bcau\b|conselho de arquitetura"),

    # Padrões com LACUNA: "ASSOCIACAO PAULISTA DOS ENGENHEIROS" e "INSTITUTO
    # BRASILEIRO DE AVALIACOES E PERICIAS DE ENGENHARIA" não casavam as
    # colocações rígidas e caíam em "Outras".
    ("PROFISSIONAL",
     r"\bsenge\b|\bseesp\b|\bsintesp\b|\bibape\b|\bipeea\b|"
     r"(sindicato|associacao|federacao|instituto|clube|uniao)"
     r"(\s+\S+){0,4}\s+(de|dos|das|do|da)\s+"
     r"(engenh|arquitet|agronom|geolog|geograf|meteorolog|tecnolog|tecnic|"
     r"avaliac|pericia|profissiona)|"
     r"instituto de engenharia|clube de engenharia|\baea\b|"
     r"\bfaeasp\b|\bfisenge\b"),

    ("SISTEMA",
     r"\bconfea\b|\bmutua\b|\bcrea\b|caixa de assistencia|"
     r"conselho regional de engenharia|conselho federal de engenharia"),
]
_INTERESSES = [(k, re.compile(rx, re.IGNORECASE)) for k, rx in INTERESSES]

# Uma entidade cujo nome MENCIONA o Crea não é o Crea. "Associação dos
# Procuradores do CREA-SP" é associação de pessoas que trabalham no conselho —
# e é, na rodada real, a maior beneficiária isolada fora do sistema
# (R$ 41,5 milhões). Sem esta ressalva, `\bcrea\b` a classificava como órgão do
# sistema Confea/Crea e ela seria contada como transferência legal obrigatória:
# o achado mais forte do relatório desapareceria dentro de uma linha sobre
# cumprimento da lei.
RX_NAO_E_ORGAO_DO_SISTEMA = re.compile(
    r"\bassociac|\bsindicato|\binstituto\b|\bclube\b|\bsociedade\b|"
    r"\bcooperativa\b|\bfundacao\b|\bfederacao\b|\bcaixa de previd",
    re.IGNORECASE)

# Transferência que o conselho é OBRIGADO a fazer, e portanto não é escolha
# dele: cota-parte ao Confea (Lei 5.194/1966, arts. 34, "e", e 44) e repasse à
# Mútua. Deliberadamente mais estreita que a categoria SISTEMA — o Crea de
# outro estado, por exemplo, é do sistema mas não é repasse obrigatório.
RX_TRANSFERENCIA_OBRIGATORIA = re.compile(
    r"\bconfea\b|conselho federal de engenharia|\bmutua\b|"
    r"caixa de assistencia dos profissionais", re.IGNORECASE)


def transferencia_obrigatoria(nome: str) -> bool:
    n = str(nome or "")
    if RX_NAO_E_ORGAO_DO_SISTEMA.search(n):
        return False
    return bool(RX_TRANSFERENCIA_OBRIGATORIA.search(n))

ROTULO = {
    "PATRONAL": "Patronal / empresarial",
    "PROFISSIONAL": "Profissional / trabalhador",
    "ENSINO": "Instituições de ensino",
    "SISTEMA": "Sistema Confea/Crea",
    "CONSELHO_PROFISSIONAL": "Outros conselhos profissionais",
    "OUTRA": "Outras entidades",
}

# Só nomes com cara de entidade coletiva entram na análise: uma empreiteira
# fornecedora não compõe o colégio eleitoral e distorceria o retrato.
RX_COLETIVA = re.compile(
    r"sindicat|associac|federac|instituto|fundac|conselho|clube|"
    r"universidade|faculdade|centro universitario|sociedade|camara|uniao|"
    r"cooperativa|\bordem\b", re.IGNORECASE)

# ...mas "INSTITUTO DE TECNOLOGIA LTDA" e "COOPERATIVA DE TRANSPORTES S/A" são
# empresas: o sufixo societário denuncia. Elas só entram se o nome casar com
# um padrão de interesse específico, nunca pela palavra genérica.
RX_EMPRESARIAL = re.compile(
    r"\bltda\b|\bltda\.|\bs[/.]?a\b|\beireli\b|\bme\b$|\bepp\b|"
    r"\bsociedade (simples|empresaria|limitada)\b|\bs\.?a\.?$", re.IGNORECASE)


def interesse(nome: str) -> str:
    n = str(nome or "")
    for cat, rx in _INTERESSES:
        if rx.search(n):
            return cat
    return "OUTRA"


# Órgão público, autarquia federal e cartório aparecem como "INSTITUTO" ou
# "CONSELHO" no nome e não compõem colégio eleitoral nenhum.
RX_NAO_ENTIDADE = re.compile(
    r"\binss\b|instituto nacional do seguro|\binpi\b|receita federal|"
    r"\bcartorio|tabeliona|instituto de estudos de protesto|junta comercial|"
    r"\bdetran\b|\bcorreios\b|caixa economica|banco do brasil|\bunimed\b|"
    r"instituto de pesquisas economicas", re.IGNORECASE)


def _e_coletiva(nome: str) -> bool:
    """Entidade de classe, e não fornecedor com nome de aparência coletiva.

    A sigla vem primeiro: SINDUSCON, SENGE, FIESP e CONFEA não contêm nenhuma
    das palavras genéricas ("sindicato", "associação"), e exigi-las excluía
    justamente as entidades mais relevantes do colégio eleitoral.
    """
    n = str(nome or "")
    if RX_NAO_ENTIDADE.search(n):
        return False
    if interesse(n) != "OUTRA":
        return True                        # padrão de interesse reconhecido
    if not RX_COLETIVA.search(n):
        return False
    return not RX_EMPRESARIAL.search(n)    # genérica com sufixo de empresa: fora


def _entidades_pagas(staged) -> pd.DataFrame:
    """Repasses a entidades coletivas, unindo convênio e pagamento direto."""
    frames = []
    conv = df(staged, "convenios")
    if not conv.empty and "BENEFICIARIO" in conv:
        c = conv.copy()
        c["entidade"] = c["BENEFICIARIO"].fillna("").astype(str)
        c["valor"] = pd.to_numeric(coluna(c, "VALOR_TOTAL", 0),
                                   errors="coerce").fillna(0)
        c["canal"] = "convênio"
        c["TIPO_PESSOA"] = "PJ"          # convênio é sempre com entidade
        cols = ["_ano", "entidade", "valor", "canal", "TIPO_PESSOA"]
        frames.append(c[cols] if "_ano" in c else c.assign(_ano=pd.NA)[cols])
    pg = df(staged, "pagamentos")
    if not pg.empty and "FAVORECIDO_NORM" in pg:
        p = pg.copy()
        p["entidade"] = coluna(p, "NOME_RAZAO_SOCIAL").fillna("").astype(str)
        p["valor"] = pd.to_numeric(p["VALOR"], errors="coerce").fillna(0)
        p["canal"] = "pagamento direto"
        if "TIPO_PESSOA" not in p.columns:
            p["TIPO_PESSOA"] = "INDEFINIDO"
        frames.append(p[["_ano", "entidade", "valor", "canal", "TIPO_PESSOA"]])
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)
    d = d[d["valor"] > 0]
    # Normalizar ANTES de filtrar: o filtro rodava sobre o texto cru e a
    # classificação sobre o normalizado, e R$ 236 mi ficavam fora do ranking
    # por causa dessa assimetria.
    d["entidade_norm"] = d["entidade"].map(norm_nome)
    # verba a pessoa física nunca é entidade de classe
    if "TIPO_PESSOA" in d.columns:
        d = d[d["TIPO_PESSOA"] != "PF"]
    coletivas = {n: _e_coletiva(n) for n in d["entidade_norm"].unique()}
    d = d[d["entidade_norm"].map(coletivas)]
    if d.empty:
        return d
    unicos = {e: interesse(e) for e in d["entidade_norm"].unique()}
    d["interesse"] = d["entidade_norm"].map(unicos)
    d["interesse_rotulo"] = d["interesse"].map(ROTULO)
    return d


# --------------------------------------------------------------- mandatários
# Provedores de e-mail gratuito: o domínio não diz nada sobre vínculo, é só
# onde a pessoa abriu uma conta. Fora desta lista, o domínio é do empregador,
# da própria empresa ou da instituição — e aí ele informa.
DOMINIOS_LIVRES = {
    "gmail.com", "gmail.con", "hotmail.com", "hotmail.com.br", "yahoo.com",
    "yahoo.com.br", "ymail.com", "outlook.com", "outlook.com.br", "live.com",
    "msn.com", "uol.com.br", "bol.com.br", "terra.com.br", "ig.com.br",
    "globo.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
    "proton.me", "zipmail.com.br", "globomail.com", "r7.com", "itelefonica.com.br",
}

# Datas-sentinela que o portal usa quando não há data real. Não são datas: são
# a ausência de data com aparência de dado, e por isso precisam de teste
# próprio — uma posse "em 1900" passa por qualquer filtro de período.
_ANO_MIN_PLAUSIVEL = 1950
_ANO_MAX_PLAUSIVEL = 2100


def _nucleo_dominio(dominio: str) -> str:
    """`construtoratucanos.com.br` -> `CONSTRUTORATUCANOS`.

    Só as letras da primeira parte, para poder procurar como pedaço contínuo
    dentro da razão social comprimida do fornecedor.
    """
    base = str(dominio or "").split(".")[0]
    return re.sub(r"[^A-Za-z]", "", base).upper()


def _mandatarios(staged) -> pd.DataFrame:
    m = df(staged, "mandatarios")
    if m.empty or "NOME_NORM" not in m.columns:
        return pd.DataFrame()
    d = m.copy()
    d["cargo"] = coluna(d, "cargo").fillna("").astype(str).str.strip()
    # `nome` é criado pelo staging, mas indexar coluna que pode não existir
    # derruba a frente inteira — e uma frente que estoura some do relatório
    # sem dizer que sumiu. O nome normalizado sempre existe e serve de reserva.
    nome = coluna(d, "nome").fillna("").astype(str)
    d["nome"] = nome.where(nome.str.len() > 0, d["NOME_NORM"])
    for c in ("POSSE", "FIM_PREVISTO"):
        if c in d.columns:
            d[c] = pd.to_datetime(d[c], errors="coerce")
    return d


def _recebedores(staged) -> pd.DataFrame:
    """TODO mundo que recebeu dinheiro do conselho — não só entidade de classe.

    Universo diferente do de `_entidades_pagas` de propósito, e a confusão
    entre os dois é fácil de cometer: para o cruzamento por domínio, a
    construtora e o escritório de engenharia são exatamente o que interessa,
    e são justamente eles que o filtro de entidade coletiva descarta.
    """
    frames = []
    pg = df(staged, "pagamentos")
    if not pg.empty and "NOME_RAZAO_SOCIAL" in pg:
        p = pg[pg.get("TIPO_PESSOA", "PJ") != "PF"] if "TIPO_PESSOA" in pg else pg
        frames.append(pd.DataFrame({
            "nome": coluna(p, "NOME_RAZAO_SOCIAL").fillna("").astype(str),
            "valor": pd.to_numeric(p["VALOR"], errors="coerce").fillna(0),
            "canal": "pagamento"}))
    ctr = df(staged, "contratos")
    if not ctr.empty:
        nome = coluna(ctr, "CONTRATADA_NOME")
        if nome.fillna("").astype(str).str.len().sum() == 0:
            nome = coluna(ctr, "CONTRATADA")
        frames.append(pd.DataFrame({
            "nome": nome.fillna("").astype(str),
            "valor": pd.to_numeric(coluna(ctr, "VALOR", 0),
                                   errors="coerce").fillna(0),
            "canal": "contrato"}))
    conv = df(staged, "convenios")
    if not conv.empty and "BENEFICIARIO" in conv:
        frames.append(pd.DataFrame({
            "nome": conv["BENEFICIARIO"].fillna("").astype(str),
            "valor": pd.to_numeric(coluna(conv, "VALOR_TOTAL", 0),
                                   errors="coerce").fillna(0),
            "canal": "convênio"}))
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)
    d = d[(d["valor"] > 0) & (d["nome"].str.len() > 3)]
    if d.empty:
        return d
    d["nome_norm"] = d["nome"].map(norm_nome)
    return (d.groupby("nome_norm")
            .agg(valor=("valor", "sum"), canais=("canal", lambda s: ", ".join(sorted(set(s)))))
            .reset_index())


def _achados_mandatarios(staged, pagos: pd.DataFrame) -> list[Finding]:
    """O que o rol de mandatários permite medir por si só.

    Este bloco existe porque o rol chegou sem o campo de ENTIDADE — que é o
    elo entre assento e interesse. Sem ele, `repr-composicao` não roda. Mas o
    rol traz outras três coisas que ninguém publica junto: quantos mandatos
    existem e de que tipo, quais deles não têm início nem fim, e o domínio do
    e-mail de contato, que é o único proxy público do vínculo de cada um.
    """
    achados: list[Finding] = []
    d = _mandatarios(staged)
    if d.empty:
        return achados

    pessoas = int(d["NOME_NORM"].nunique())
    por_cargo = (d.groupby("cargo")
                 .agg(mandatos=("NOME_NORM", "size"),
                      pessoas=("NOME_NORM", "nunique")).reset_index()
                 .sort_values("mandatos", ascending=False))
    por_cargo["share_pct"] = (100 * por_cargo["mandatos"] / len(d)).round(1)
    rx_conselheiro = re.compile(r"titular|suplente|conselheir", re.I)
    rx_inspetor = re.compile(r"inspetor", re.I)
    n_cons = int(d[d["cargo"].str.contains(rx_conselheiro)]["NOME_NORM"].nunique())
    n_insp = int(d[d["cargo"].str.contains(rx_inspetor)]["NOME_NORM"].nunique())

    # ------------------------------------------------ 1. o tamanho da estrutura
    achados.append(Finding(
        id="repr-mandatarios-composicao", frente=FRENTE,
        gravidade="media" if n_insp > 4 * max(n_cons, 1) else "info",
        titulo=(f"{pessoas} pessoas ocupam mandato no conselho: {n_cons} "
                f"conselheiro(s) e {n_insp} inspetor(es)"),
        descricao=(
            "Este é o denominador que faltava. A frente de verbas mostra "
            "quanto o conselho paga em diárias e locomoção a conselheiros e a "
            "inspetores, mas sem saber **quantos são** não se sabe se o valor "
            "é grande ou se a estrutura é grande.\n\n"
            "Inspetor não é empregado: é profissional com mandato, que atua "
            "no município e recebe por diária e deslocamento, não por salário. "
            "Uma estrutura de inspetoria numerosa é uma decisão institucional "
            "legítima — e é também o que explica o volume de verba "
            "indenizatória. As duas leituras precisam do mesmo número, e ele "
            "só existe aqui.\n\n"
            "O rol é uma **foto do momento**, não uma série histórica: o "
            "portal devolve os mandatos vigentes. Ele não serve para dizer "
            "quem era inspetor em 2015."),
        evidencia=por_cargo,
        grafico="bar", unidade="mandatos",
        base_legal=("Lei 5.194/1966, arts. 30 a 34 e 45 (câmaras "
                    "especializadas e inspetorias); Lei 12.514/2011."),
        como_verificar=("Dividir a verba paga em 'Diárias/Locomoção com "
                        "Inspetores' pelo número de inspetores do exercício "
                        "correspondente — o que exige o rol POR ANO, que o "
                        "portal não publica."),
        metricas={"pessoas": pessoas, "mandatos": int(len(d)),
                  "conselheiros": n_cons, "inspetores": n_insp,
                  "cargos_distintos": int(d["cargo"].nunique())},
    ))

    # ------------------------------------------------ 2. mandato sem termo
    if {"POSSE", "FIM_PREVISTO"} <= set(d.columns):
        sem_termo = d[(d["POSSE"].dt.year < _ANO_MIN_PLAUSIVEL) |
                      (d["FIM_PREVISTO"].dt.year > _ANO_MAX_PLAUSIVEL)]
        if not sem_termo.empty:
            # Quem tem OUTRO registro com data real está apenas duplicado; quem
            # não tem, está no rol sem nenhuma data de mandato.
            com_par = set(sem_termo["NOME_NORM"]) & set(
                d[~d.index.isin(sem_termo.index)]["NOME_NORM"])
            so_sentinela = int(sem_termo["NOME_NORM"].nunique() - len(com_par))
            achados.append(Finding(
                id="repr-mandato-sem-termo", frente=FRENTE,
                gravidade="media" if so_sentinela else "info",
                titulo=(f"{len(sem_termo)} mandato(s) registrados com posse em "
                        f"01/01/1900 e término em 31/12/9999"),
                descricao=(
                    "Não são datas: são o preenchimento automático que o "
                    "sistema usa quando não há data. Publicadas como se datas "
                    "fossem, elas atravessam qualquer filtro de período — um "
                    "mandato assim está vigente em todos os exercícios de "
                    "qualquer consulta.\n\n"
                    f"De {sem_termo['NOME_NORM'].nunique()} pessoas nessa "
                    f"situação, **{so_sentinela}** não têm nenhum outro "
                    "registro com data real: para elas, o portal não informa "
                    "quando o mandato começou nem quando termina. As demais "
                    "aparecem duas vezes, uma com data e outra sem.\n\n"
                    "Mandato tem prazo por lei. A ausência de termo no registro "
                    "público impede conferir se quem exerce a função ainda tem "
                    "mandato válido."),
                evidencia=(sem_termo[["nome", "cargo", "POSSE", "FIM_PREVISTO"]]
                           .sort_values("nome").head(200)),
                base_legal=("Lei 5.194/1966, art. 32 (mandato dos conselheiros); "
                            "Lei 12.527/2011, art. 8º, §1º, II."),
                como_verificar=("Pedir, por ato de nomeação, a data de posse e "
                                "o termo final de cada mandato listado."),
                metricas={"registros": int(len(sem_termo)),
                          "pessoas": int(sem_termo["NOME_NORM"].nunique()),
                          "so_com_data_sentinela": so_sentinela},
            ))

    # ------------------------------------------------ 3. o domínio como vínculo
    if "EMAIL_DOMINIO" in d.columns:
        dom = d[d["EMAIL_DOMINIO"].fillna("").astype(str).str.contains(r"\.")]
        corp = dom[~dom["EMAIL_DOMINIO"].str.lower().isin(DOMINIOS_LIVRES)]
        if not corp.empty:
            tabela = (corp.groupby("EMAIL_DOMINIO")
                      .agg(pessoas=("NOME_NORM", "nunique"),
                           cargos=("cargo", lambda s: ", ".join(sorted(set(s)))))
                      .reset_index().sort_values("pessoas", ascending=False))
            pct = 100 * len(corp) / len(dom) if len(dom) else 0

            # Cruzamento com quem recebe dinheiro do conselho — fornecedor,
            # contratada e conveniada, não só entidade de classe.
            #
            # O núcleo tem de ter 8 letras ou mais: abaixo disso o pedaço
            # aparece dentro de razão social por acaso ("eng", "sp", "tec"), e
            # a lista vira ruído. Erra para o lado de achar menos.
            batidas = pd.DataFrame()
            alvo = _recebedores(staged)
            if not alvo.empty:
                nucleos = {n: _nucleo_dominio(n)
                           for n in corp["EMAIL_DOMINIO"].unique()}
                nucleos = {k: v for k, v in nucleos.items() if len(v) >= 8}
                if nucleos:
                    alvo = alvo.copy()
                    alvo["_compacto"] = alvo["nome_norm"].str.replace(
                        r"[^A-Za-z]", "", regex=True).str.upper()
                    linhas = []
                    for dominio, nucleo in nucleos.items():
                        bate = alvo[alvo["_compacto"].str.contains(nucleo,
                                                                   regex=False)]
                        for _, r in bate.iterrows():
                            quem = corp[corp["EMAIL_DOMINIO"] == dominio]
                            linhas.append({
                                "dominio": dominio,
                                "recebedor": r["nome_norm"],
                                "valor_recebido": float(r["valor"]),
                                "canais": r["canais"],
                                "mandatarios": int(quem["NOME_NORM"].nunique()),
                                "nomes": ", ".join(sorted(quem["nome"].unique())[:5]),
                                "cargos": ", ".join(sorted(set(quem["cargo"]))),
                            })
                    batidas = (pd.DataFrame(linhas)
                               .sort_values("valor_recebido", ascending=False)
                               if linhas else pd.DataFrame())

            tem_batida = not batidas.empty
            val = float(batidas["valor_recebido"].sum()) if tem_batida else 0.0
            achados.append(Finding(
                id="repr-mandatario-dominio", frente=FRENTE,
                gravidade="alta" if tem_batida else "info",
                titulo=((f"{len(batidas)} vínculo(s) entre o domínio de contato "
                         f"de um mandatário e quem recebe do conselho — "
                         f"{fmt_brl(val)}") if tem_batida else
                        (f"{int(corp['NOME_NORM'].nunique())} mandatários usam "
                         f"e-mail de domínio próprio ({pct:.0f}% dos com "
                         "e-mail), em {n} domínios".format(
                             n=int(corp['EMAIL_DOMINIO'].nunique())))),
                descricao=(
                    "O portal não publica a entidade que indicou cada "
                    "mandatário — que é justamente o elo entre assento e "
                    "interesse. O domínio do e-mail de contato é o substituto "
                    "possível: quem usa `unesp.br` está ligado a uma "
                    "instituição de ensino, quem usa o domínio de uma "
                    "construtora está ligado a uma empresa do setor que o "
                    "conselho fiscaliza.\n\n"
                    + ("O cruzamento abaixo é o que interessa: domínio de "
                       "mandatário que coincide com o nome de quem recebe "
                       "dinheiro do conselho. **Coincidência de nome não é "
                       "prova de nada** — pode ser homonímia, pode ser empresa "
                       "distinta com nome parecido, e mesmo o vínculo real não "
                       "é irregular por si. É o ponto de partida da verificação "
                       "de impedimento e suspeição, não a conclusão dela.\n\n"
                       if tem_batida else
                       "Nenhum domínio coincidiu com nome de recebedor de "
                       "recurso do conselho. A tabela fica publicada como "
                       "mapa do vínculo declarado.\n\n")
                    + "Os endereços de e-mail não são publicados aqui: só o "
                    "domínio, que é o que carrega informação pública."),
                evidencia=batidas if tem_batida else tabela.head(200),
                base_legal=("Lei 9.784/1999, arts. 18 a 21 (impedimento e "
                            "suspeição); Lei 14.133/2021, art. 9º; Lei "
                            "5.194/1966, art. 34."),
                como_verificar=("Para cada linha, conferir no cadastro da "
                                "empresa (QSA na Receita) se o mandatário é "
                                "sócio ou administrador, e nas atas se ele "
                                "participou de deliberação sobre o contrato."),
                metricas={"dominios_proprios": int(corp["EMAIL_DOMINIO"].nunique()),
                          "mandatarios_com_dominio_proprio":
                              int(corp["NOME_NORM"].nunique()),
                          "pct_dominio_proprio": round(pct, 1),
                          "vinculos_com_recebedor": int(len(batidas)),
                          "valor_recebido": round(val, 2)},
            ))
    return achados


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    mand = df(staged, "mandatarios")
    d = _entidades_pagas(staged)
    achados.extend(_achados_mandatarios(staged, d))

    # ------------------------------------------------ 1. composição do plenário
    if not mand.empty and "ENTIDADE_NORM" in mand and \
            mand["ENTIDADE_NORM"].fillna("").str.len().gt(2).any():
        m = mand[mand["ENTIDADE_NORM"].fillna("").str.len() > 2].copy()
        m["interesse"] = m["ENTIDADE_NORM"].map(interesse)
        m["interesse_rotulo"] = m["interesse"].map(ROTULO)
        assentos = (m.groupby(["interesse_rotulo"])
                    .agg(conselheiros=("NOME_NORM", "nunique"),
                         entidades=("ENTIDADE_NORM", "nunique")).reset_index()
                    .sort_values("conselheiros", ascending=False))
        tot = int(assentos["conselheiros"].sum())
        patronal = int(assentos.loc[assentos["interesse_rotulo"] ==
                                    ROTULO["PATRONAL"], "conselheiros"].sum())
        pct_pat = 100 * patronal / tot if tot else 0
        achados.append(Finding(
            id="repr-composicao", frente=FRENTE,
            gravidade="alta" if pct_pat > 40 else "media",
            titulo=(f"{patronal} de {tot} conselheiros foram indicados por "
                    f"entidades patronais ou empresariais ({pct_pat:.0f}%)"),
            descricao=(
                "Composição do plenário pela natureza da entidade que indicou "
                "cada conselheiro. Um conselho profissional fiscaliza o "
                "exercício da profissão, o que frequentemente significa "
                "fiscalizar as empresas que contratam esses profissionais. "
                "Quando a maioria dos assentos vem de entidades que "
                "representam justamente o lado empresarial, o órgão fiscaliza "
                "quem o compõe — e isso condiciona a leitura de todo o resto "
                "deste relatório."),
            evidencia=assentos,
            serie=(A.serie_anual(m.assign(_um=1), "_um", by="interesse_rotulo")
                   if "_ano" in m else pd.DataFrame()),
            grafico="bar_stacked", unidade="assentos",
            base_legal="Lei 5.194/1966, arts. 30 a 34 (composição e competência "
                       "dos Creas); Lei 9.784/1999, arts. 18 a 21 (impedimento "
                       "e suspeição).",
            metricas={"total_conselheiros": tot, "patronal": patronal,
                      "pct_patronal": round(pct_pat, 1)},
            como_verificar="Conferir o rol de entidades de classe registradas e "
                           "com direito a indicar representante, e as atas de "
                           "posse de cada mandato.",
        ))
    else:
        achados.append(Finding(
            id="repr-sem-mandatarios", frente=FRENTE, gravidade="alta",
            titulo=("O rol de mandatários não informa por qual entidade cada um "
                    "tem assento" if not mand.empty else
                    "O rol de conselheiros e das entidades que os indicaram não "
                    "está disponível"),
            descricao=(
                ("O rol de mandatários existe e está medido acima — nome, "
                 "cargo, datas de mandato. O que ele **não** traz é a entidade "
                 "que indicou cada um, e é exatamente esse campo que liga o "
                 "assento ao interesse representado.\n\n"
                 "A distinção importa para o pedido de acesso: não se trata de "
                 "publicar o rol, que já é publicado, e sim de acrescentar a "
                 "ele uma coluna que o próprio conselho necessariamente possui "
                 "— ninguém toma posse sem que se saiba quem o indicou.\n\n"
                 if not mand.empty else
                 "Sem saber quem ocupa o plenário e por qual entidade, é "
                 "impossível ligar decisão a interesse — e é justamente essa "
                 "ligação que a transparência ativa deveria permitir. A "
                 "indisponibilidade não é detalhe técnico: é o que impede o "
                 "controle social sobre o conflito de interesses.\n\n")
                + "Os repasses às entidades continuam medidos abaixo, mas sem "
                "o outro lado da equação."),
            base_legal="Lei 12.527/2011, art. 8º, §1º, incisos II e III "
                       "(divulgação de informações sobre a estrutura "
                       "organizacional e os repasses); Decreto 8.777/2016.",
            como_verificar="Pedido de acesso à informação exigindo, por "
                           "exercício, a relação de conselheiros titulares e "
                           "suplentes com a entidade de classe ou instituição "
                           "de ensino que indicou cada um.",
        ))

    if d.empty:
        return achados

    # ------------------------------------------------ 2. dinheiro por interesse
    #
    # A cota-parte ao Confea e o repasse à Mútua são OBRIGAÇÃO LEGAL (Lei
    # 5.194/1966, arts. 34, "e", e 44; Lei 6.496/1977, art. 3º). Somá-los ao
    # repasse discricionário e dizer "o conselho repassou R$ 1,36 bilhão às
    # entidades da sua base política" descreve errado o que aconteceu: na
    # rodada real, R$ 890 milhões desse total — 65% — eram Confea e Mútua, que
    # o conselho não escolhe repassar.
    #
    # Os dois ficam medidos e visíveis, em linha própria. O que muda é a
    # atribuição: obrigação não é escolha, e um relatório de fiscalização que
    # confunde as duas coisas perde a razão nas duas.
    obrig = d["entidade_norm"].map(transferencia_obrigatoria)
    sistema = d[obrig]
    disc = d[~obrig]
    val_sistema = float(sistema["valor"].sum())
    total_geral = float(d["valor"].sum())
    if disc.empty:
        disc = d.iloc[0:0]
    serie = A.serie_anual(disc, "valor", by="interesse_rotulo")
    total = float(disc["valor"].sum())
    por_int = (d.groupby("interesse_rotulo")
               .agg(total=("valor", "sum"), entidades=("entidade_norm", "nunique"),
                    repasses=("valor", "size")).reset_index()
               .sort_values("total", ascending=False))
    por_int["obrigatorio"] = por_int["interesse_rotulo"].eq(ROTULO["SISTEMA"])
    pat = float(disc.loc[disc["interesse"] == "PATRONAL", "valor"].sum())
    prof = float(disc.loc[disc["interesse"] == "PROFISSIONAL", "valor"].sum())
    achados.append(Finding(
        id="repr-dinheiro-por-interesse", frente=FRENTE,
        gravidade="alta" if pat > prof and pat > 0 else "media",
        titulo=(f"{fmt_brl(total)} repassados a entidades de classe por decisão "
                f"do conselho — {fmt_brl(pat)} a patronais e {fmt_brl(prof)} a "
                "profissionais"
                if pat > 0 else
                f"{fmt_brl(total)} repassados a entidades de classe por decisão "
                "do conselho; nenhuma entidade patronal foi identificada pelo "
                "classificador"),
        descricao=(
            "Dinheiro que saiu do conselho para sindicatos, associações, "
            "federações, institutos e escolas, somando convênio e pagamento "
            "direto, separado por qual interesse cada entidade representa. "
            "Estas são as organizações que indicam conselheiros e formam o "
            "colégio eleitoral: o repasse vai para a própria base política do "
            "órgão.\n\n"
            + (f"**Fora desta conta, e de propósito:** {fmt_brl(val_sistema)} "
               "repassados ao Confea e à Mútua, que são **obrigação legal** e "
               "não decisão do conselho (Lei 5.194/1966, arts. 34 e 44). Eles "
               "aparecem na tabela abaixo marcados como obrigatórios, e "
               f"representam {100 * val_sistema / total_geral:.0f}% de tudo o "
               "que saiu para entidades — somá-los ao repasse discricionário "
               "inflaria a medida e atribuiria ao conselho uma escolha que ele "
               "não fez.\n\n"
               if val_sistema > 0 else "")
            + "A classificação por interesse é heurística, feita pelo nome — a "
            "lista completa está na evidência para ser conferida entidade por "
            "entidade."),
        evidencia=por_int,
        serie=serie, grafico="bar_stacked",
        tabela_anual=A.ranking_anual(disc, "entidade_norm", "valor", top=12),
        base_legal="Lei 14.133/2021, art. 9º, III; Decreto 6.170/2007 "
                   "(convênios); Lei 5.194/1966, arts. 34 e 44 (cota-parte "
                   "obrigatória ao Confea); CF, art. 37, caput.",
        metricas={"total": round(total, 2), "patronal": round(pat, 2),
                  "profissional": round(prof, 2),
                  "transferencia_obrigatoria_sistema": round(val_sistema, 2),
                  "total_com_obrigatorias": round(total_geral, 2),
                  "entidades": int(disc["entidade_norm"].nunique())},
    ))

    # A concentração e o ranking passam a medir o repasse DISCRICIONÁRIO pela
    # mesma razão: com o Confea dentro, "5 entidades concentram 69%" era uma
    # frase sobre a lei, não sobre o conselho.
    d = disc if not disc.empty else d

    # ---- ranking nominal, para apuração individual
    por_ent = (d.groupby(["entidade_norm", "interesse_rotulo"])
               .agg(total=("valor", "sum"), repasses=("valor", "size"),
                    exercicios=("_ano", "nunique"),
                    canais=("canal", "nunique")).reset_index()
               .sort_values("total", ascending=False))
    achados.append(Finding(
        id="repr-ranking-entidades", frente=FRENTE, gravidade="info",
        titulo=f"{len(por_ent)} entidades coletivas receberam recurso do conselho",
        descricao="Ranking nominal com o interesse atribuído a cada uma, em "
                  "quantos exercícios recebeu e por quantos canais. É a lista "
                  "de partida para conferir a classificação e para pedir os "
                  "processos individualmente.",
        evidencia=por_ent.head(150),
        tabela_anual=A.ranking_anual(d, "entidade_norm", "valor", top=10),
    ))

    # ------------------------------------------------ 3. duplo canal
    #
    # A primeira versão comparava "convênio" contra "pagamento direto" e
    # flagrava 183 entidades — mas o pagamento É o desembolso do convênio: a
    # mesma transação vista em dois datasets, o instrumento e a quitação.
    # O duplo papel de verdade é ser beneficiária de convênio E fornecedora
    # com CONTRATO, porque aí a entidade disputa licitação com quem a financia.
    ctr = df(staged, "contratos")
    com_contrato: set[str] = set()
    if not ctr.empty:
        for col in ("CONTRATADA_NORM", "CONTRATADA_NOME", "CONTRATADA"):
            if col in ctr.columns:
                com_contrato |= set(ctr[col].dropna().map(norm_nome))
    beneficiarias = set(d.loc[d["canal"] == "convênio", "entidade_norm"])
    duplo = por_ent[por_ent["entidade_norm"].isin(beneficiarias & com_contrato)]
    if not duplo.empty:
        achados.append(Finding(
            id="repr-duplo-canal", frente=FRENTE, gravidade="alta",
            titulo=(f"{len(duplo)} entidade(s) são beneficiárias de convênio E "
                    "têm contrato como fornecedoras"),
            descricao=(
                "Convênio pressupõe interesse recíproco e plano de trabalho; "
                "contrato pressupõe fornecedor disputando com outros. A mesma "
                "entidade nos dois papéis, no mesmo período, é o desenho que "
                "permite pagar por fora do que o convênio já cobre — e escapa "
                "das duas fiscalizações, porque cada canal é auditado à parte."
                "\n\nO cruzamento é convênio × **contrato registrado**, não "
                "convênio × pagamento: o pagamento é o desembolso do próprio "
                "convênio, e contá-lo como segundo canal transformava repasse "
                "rotineiro em achado."),
            evidencia=duplo,
            base_legal="Lei 14.133/2021, arts. 9º e 75; Decreto 6.170/2007, "
                       "art. 2º; Lei 8.666/1993, art. 116 (quando aplicável).",
            como_verificar="Pedir o plano de trabalho do convênio e o objeto do "
                           "contrato da mesma entidade e verificar sobreposição "
                           "de escopo no mesmo período.",
        ))

    # ------------------------------------------------ 4. assento × dinheiro
    if not mand.empty and "ENTIDADE_NORM" in mand:
        com_assento = set(mand["ENTIDADE_NORM"].dropna()) - {""}
        if com_assento:
            cruz = por_ent[por_ent["entidade_norm"].isin(com_assento)]
            if not cruz.empty:
                valor_cruz = float(cruz["total"].sum())
                achados.append(Finding(
                    id="repr-assento-e-dinheiro", frente=FRENTE, gravidade="alta",
                    titulo=(f"{len(cruz)} entidade(s) têm assento no conselho E "
                            f"receberam recurso dele ({fmt_brl(valor_cruz)})"),
                    descricao=(
                        "A interseção exata: organizações que indicam quem "
                        "decide e que também recebem do orçamento decidido. Não "
                        "prova que o conselheiro indicado votou o próprio "
                        "repasse — isso exige a ata nominal da votação —, mas "
                        "delimita onde essa verificação precisa ser feita, e "
                        "onde a declaração de impedimento deveria constar."),
                    evidencia=cruz,
                    serie=A.serie_anual(
                        d[d["entidade_norm"].isin(com_assento)], "valor",
                        by="interesse_rotulo"),
                    grafico="bar_stacked",
                    base_legal="Lei 9.784/1999, arts. 18 a 21; Lei 14.133/2021, "
                               "art. 9º, III; CF, art. 37.",
                    como_verificar="Pedir as atas das sessões que aprovaram cada "
                                   "repasse e verificar se o conselheiro "
                                   "indicado pela beneficiária declarou "
                                   "impedimento e se absteve.",
                    metricas={"entidades": int(len(cruz)),
                              "valor": round(valor_cruz, 2)},
                ))

    # ------------------------------------------------ 5. concentração
    if len(por_ent) >= 5:
        top5 = float(por_ent.head(5)["total"].sum())
        pct = 100 * top5 / total if total else 0
        if pct > 60:
            achados.append(Finding(
                id="repr-concentracao", frente=FRENTE, gravidade="media",
                titulo=f"5 entidades concentram {pct:.0f}% de todo o repasse a "
                       "entidades coletivas",
                descricao=("Repasse institucional pulverizado sugere política; "
                           "concentrado em meia dúzia de nomes, ano após ano, "
                           "sugere relação. Confira se as beneficiárias do topo "
                           "são as mesmas que mantêm assento no plenário."),
                evidencia=por_ent.head(15),
                metricas={"pct_top5": round(pct, 1)},
            ))
    return achados
