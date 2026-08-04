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


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    mand = df(staged, "mandatarios")
    d = _entidades_pagas(staged)

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
            titulo="O rol de conselheiros e das entidades que os indicaram não "
                   "está disponível",
            descricao=(
                "Sem saber quem ocupa o plenário e por qual entidade, é "
                "impossível ligar decisão a interesse — e é justamente essa "
                "ligação que a transparência ativa deveria permitir. A "
                "indisponibilidade não é detalhe técnico: é o que impede o "
                "controle social sobre o conflito de interesses. Os repasses "
                "às entidades continuam medidos abaixo, mas sem o outro lado "
                "da equação."),
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
    serie = A.serie_anual(d, "valor", by="interesse_rotulo")
    total = float(d["valor"].sum())
    por_int = (d.groupby("interesse_rotulo")
               .agg(total=("valor", "sum"), entidades=("entidade_norm", "nunique"),
                    repasses=("valor", "size")).reset_index()
               .sort_values("total", ascending=False))
    pat = float(d.loc[d["interesse"] == "PATRONAL", "valor"].sum())
    prof = float(d.loc[d["interesse"] == "PROFISSIONAL", "valor"].sum())
    achados.append(Finding(
        id="repr-dinheiro-por-interesse", frente=FRENTE,
        gravidade="alta" if pat > prof and pat > 0 else "media",
        titulo=(f"{fmt_brl(total)} repassados a entidades coletivas — "
                f"{fmt_brl(pat)} a patronais e {fmt_brl(prof)} a entidades "
                "profissionais"
                if pat > 0 else
                f"{fmt_brl(total)} repassados a entidades coletivas; nenhuma "
                "entidade patronal foi identificada pelo classificador"),
        descricao=(
            "Todo o dinheiro que saiu do conselho para sindicatos, "
            "associações, federações, institutos e escolas, somando convênio e "
            "pagamento direto, separado por qual interesse cada entidade "
            "representa. Estas são as organizações que indicam conselheiros e "
            "formam o colégio eleitoral: o repasse vai para a própria base "
            "política do órgão. A classificação por interesse é heurística, "
            "feita pelo nome — a lista completa está na evidência para ser "
            "conferida entidade por entidade."),
        evidencia=por_int,
        serie=serie, grafico="bar_stacked",
        tabela_anual=A.ranking_anual(d, "entidade_norm", "valor", top=12),
        base_legal="Lei 14.133/2021, art. 9º, III; Decreto 6.170/2007 "
                   "(convênios); CF, art. 37, caput (impessoalidade).",
        metricas={"total": round(total, 2), "patronal": round(pat, 2),
                  "profissional": round(prof, 2),
                  "entidades": int(d["entidade_norm"].nunique())},
    ))

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
    duplo = por_ent[por_ent["canais"] > 1]
    if not duplo.empty:
        achados.append(Finding(
            id="repr-duplo-canal", frente=FRENTE, gravidade="alta",
            titulo=(f"{len(duplo)} entidade(s) recebem por convênio E como "
                    "fornecedor contratado"),
            descricao=(
                "Convênio pressupõe interesse recíproco e plano de trabalho; "
                "contrato pressupõe fornecedor disputando com outros. A mesma "
                "entidade nos dois papéis, no mesmo período, é o desenho que "
                "permite pagar por fora do que o convênio já cobre — e escapa "
                "das duas fiscalizações, porque cada canal é auditado à parte."),
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
