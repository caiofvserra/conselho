"""Frente — Fornecedores: quem são, onde estão e de que tamanho.

O portal publica o CNPJ de quem recebeu, e só. A ficha cadastral da Receita,
consultada por CNPJ, acrescenta atributos que o portal não publica e que
respondem a perguntas diferentes sobre a mesma compra:

* **situação cadastral** e **data de abertura** — se a empresa existia e estava
  regular quando recebeu (as duas análises que este módulo já fazia);
* **município e UF** — onde o fornecedor está sediado. O Crea-SP fiscaliza 645
  municípios e mantém unidades no interior inteiro (superintendências
  regionais, UGIs); se o dinheiro sai quase todo para empresas de um punhado de
  cidades, isso é mensurável sem pedir nada a ninguém;
* **porte** — quanto do dinheiro chega a micro e pequenas empresas, que a LC
  123/2006 e a Lei 14.133/2021 mandam tratar de forma diferenciada;
* **CNAE** — a atividade econômica declarada, confrontável com o objeto do que
  foi contratado.

**Todo percentual desta frente é percentual DO QUE TEM FICHA, nunca do total.**
A consulta de CNPJ tem piso (`PISO_ANALISE_FORNECEDOR`) e teto
(`MAX_CONSULTAS_CNPJ`): só os maiores fornecedores têm ficha. Por isso cada
achado publica a COBERTURA — quanto do valor pago a pessoa jurídica está
coberto por ficha. Sem esse denominador o número engana, e engana sempre na
mesma direção: o universo é enviesado para empresa grande, o que **subestima**
a fatia de micro e pequenas empresas e **superestima** a concentração
geográfica em capitais.

Nenhum dos achados geográficos é acusação. Fornecedor de fora do estado não é
irregularidade — é mapa. O que o mapa permite perguntar é outra coisa: por que
uma unidade de campo compra sistematicamente longe.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import analytics as A
from .. import config
from .. import taxonomia as T
from ..findings import Finding
from ..utils import coluna, fmt_brl, norm_nome, parse_data, so_digitos
from .common import df, fichas_cnpj, fornecedores_mercado

FRENTE = "Fornecedores"

# Constantes locais. Deveriam migrar para config.py — não foram definidas lá
# porque o arquivo está sendo editado em paralelo por outra frente.
MUNICIPIOS_SP = 645           # municípios do estado que o Crea-SP fiscaliza
CAPITAL_SP = "SAO PAULO"      # já normalizado (sem acento, maiúsculas)
PISO_CNAE_OBJETO = 50_000.0   # valor mínimo de contrato para olhar CNAE × objeto
TOP_MUNICIPIOS = 5            # tamanho do "punhado de municípios" do título


# ------------------------------------------------------------------ leitura
def _campo(ficha: dict, *chaves: str) -> str:
    """Primeiro valor de texto não vazio entre as chaves dadas.

    BrasilAPI e minhareceita divergem no nome de vários campos e no tipo
    (`cnae_fiscal` vem int numa e string na outra). Ler por uma chave só
    devolvia vazio em metade das fichas e a análise inteira sumia em silêncio.
    """
    for k in chaves:
        v = ficha.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            v = str(v)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def municipio_uf(ficha: dict) -> tuple[str, str]:
    """(município normalizado, UF) da sede declarada na ficha."""
    mun = norm_nome(_campo(ficha, "municipio", "municipio_nome",
                           "descricao_municipio", "cidade"))
    uf = _campo(ficha, "uf", "sigla_uf", "estado_sigla").upper()
    return mun, (uf if len(uf) == 2 and uf.isalpha() else "")


_PORTE_POR_CODIGO = {1: "ME", 3: "EPP", 5: "DEMAIS"}
PORTE_ROTULO = {
    "ME": "Microempresa (ME)",
    "EPP": "Empresa de pequeno porte (EPP)",
    "DEMAIS": "Demais empresas (médio e grande porte)",
    "NAO_INFORMADO": "Porte não informado na ficha",
}


def porte_classe(ficha: dict) -> str:
    """'ME' | 'EPP' | 'DEMAIS' | 'NAO_INFORMADO'.

    Duas fontes independentes dentro da mesma ficha: o código (01/03/05) e o
    texto. O código vem como inteiro na BrasilAPI e como string com zero à
    esquerda na minhareceita — por isso a conversão passa por `int()` em vez
    de comparar strings, senão "01" nunca casaria 1.

    O texto só é lido depois de `T.normalizar` (regra 1 do projeto): "MICRO
    EMPRESA", "Microempresa" e "MICROEMPRESA" são a mesma coisa e nenhuma
    comparação crua casa as três.
    """
    try:
        # float antes de int: "01" e 1 já casavam, mas 1.0 (que é como um JSON
        # relido por pandas devolve o mesmo campo) estourava ValueError e o
        # porte caía silenciosamente em NAO_INFORMADO.
        cod = int(float(str(ficha.get("codigo_porte")).strip()))
    except (TypeError, ValueError):
        cod = None
    if cod in _PORTE_POR_CODIGO:
        return _PORTE_POR_CODIGO[cod]
    t = T.normalizar(_campo(ficha, "porte", "descricao_porte"))
    if "pequeno porte" in t or t == "epp":
        return "EPP"
    if "micro" in t or t == "me":
        return "ME"
    if "demais" in t or "grande" in t or "medio" in t:
        return "DEMAIS"
    # MEI é microempresa por definição legal (LC 123/2006, art. 18-A): quando
    # o porte não vem, a opção pelo MEI ainda decide o caso.
    if ficha.get("opcao_pelo_mei") is True:
        return "ME"
    return "NAO_INFORMADO"


# Tabela de situação cadastral da Receita. O campo `situacao_cadastral` vem
# como CÓDIGO (2 = ativa) e o `descricao_situacao_cadastral` vem como texto.
# Ler o código como se fosse texto era um falso positivo grave: "2" não contém
# "ATIVA", então toda ficha que voltasse só com o código virava "fornecedor com
# situação cadastral não-ATIVA" — um achado de gravidade ALTA fabricado a
# partir de um campo que diz exatamente o contrário.
_SITUACAO_POR_CODIGO = {1: "NULA", 2: "ATIVA", 3: "SUSPENSA", 4: "INAPTA",
                        8: "BAIXADA"}


def situacao_cadastral(ficha: dict) -> str:
    """Situação cadastral em TEXTO, venha ela como texto ou como código."""
    txt = _campo(ficha, "descricao_situacao_cadastral", "situacao")
    if txt and not txt.isdigit():
        return txt.upper()
    bruto = _campo(ficha, "situacao_cadastral")
    if bruto and not bruto.isdigit():
        return bruto.upper()
    try:
        return _SITUACAO_POR_CODIGO.get(int(bruto), "")
    except (TypeError, ValueError):
        return ""


def _cnaes_secundarios(ficha: dict) -> list[tuple[str, str]]:
    """[(código, descrição)] dos CNAEs secundários declarados."""
    lista = (ficha.get("cnaes_secundarios") or ficha.get("cnaes_secundarias")
             or ficha.get("cnae_secundarios") or [])
    out: list[tuple[str, str]] = []
    for c in lista:
        if isinstance(c, dict):
            out.append((str(c.get("codigo") or c.get("code") or ""),
                        str(c.get("descricao") or c.get("text") or "")))
    return out


# ------------------------------------------------------- setores econômicos
# Divisão do CNAE (2 primeiros dígitos) -> setor. A divisão é MUITO mais
# estável que a descrição: "4399-1/03" e "Obras de alvenaria" apontam para o
# mesmo lugar, mas só o código sobrevive a variação de redação entre APIs.
CNAE_DIVISAO: dict[int, str] = {
    **{d: "AGRO" for d in range(1, 4)},
    **{d: "EXTRATIVA" for d in range(5, 10)},
    **{d: "INDUSTRIA" for d in list(range(10, 18)) + list(range(19, 34))},
    18: "GRAFICA",
    35: "ENERGIA",
    **{d: "AGUA_SANEAMENTO" for d in range(36, 40)},
    **{d: "CONSTRUCAO" for d in (41, 42, 43)},
    **{d: "COMERCIO" for d in (45, 46, 47)},
    **{d: "TRANSPORTE" for d in range(49, 54)},
    **{d: "ALIMENTACAO" for d in (55, 56)},
    **{d: "EDITORIAL_MIDIA" for d in (58, 59, 60)},
    61: "TELECOM",
    62: "TI", 63: "TI",
    **{d: "FINANCEIRO" for d in (64, 65, 66)},
    68: "IMOBILIARIO",
    69: "JURIDICO_CONTABIL",
    70: "CONSULTORIA",
    71: "ENGENHARIA",
    72: "PESQUISA",
    73: "PUBLICIDADE",
    74: "TECNICO_DIVERSAS",
    75: "VETERINARIA",
    77: "LOCACAO",
    78: "RH_TERCEIRIZACAO",
    79: "VIAGENS",
    80: "SEGURANCA",
    81: "LIMPEZA_PREDIAL",
    82: "ESCRITORIO_APOIO",
    84: "ADM_PUBLICA",
    85: "EDUCACAO",
    **{d: "SAUDE" for d in (86, 87, 88)},
    **{d: "CULTURA_ESPORTE" for d in range(90, 94)},
    94: "ASSOCIATIVAS",
    95: "MANUTENCAO",
    96: "SERVICOS_PESSOAIS",
}

# Setores que NÃO acusam ninguém quando são a atividade principal. "Serviços
# combinados de escritório e apoio administrativo" (8211-3/00), consultoria em
# gestão e organizações associativas cobrem legitimamente quase qualquer
# objeto — sinalizá-los produziria uma lista longa e sem força.
GENERICOS = {"ESCRITORIO_APOIO", "CONSULTORIA", "TECNICO_DIVERSAS",
             "ASSOCIATIVAS", "ADM_PUBLICA", "DESCONHECIDO", "OUTRO"}

# Setor do OBJETO contratado, lido do texto. Ordem importa: a primeira que
# casar vence, e as regras ambíguas vêm depois das inequívocas — "seguro de
# responsabilidade civil" não pode virar obra por causa de "civil".
REGRAS_OBJETO: list[tuple[str, str]] = [
    ("SAUDE", r"plano de saude|assistencia medica|assistencia odonto|"
              r"\bodontolog|exame(s)? (medico|ocupacional|admissional)|"
              r"medicina do trabalho|\bsesmt\b|\bambulator"),
    ("SEGUROS", r"\bseguro(s)?\b|\bapolice(s)?\b|corretora de seguro"),
    ("JURIDICO_CONTABIL",
     r"\badvocat|\badvogad|honorario(s)? advocat|servico(s)? juridic|"
     r"assessoria juridica|\bcontabil|auditoria (independente|externa|contabil)|"
     r"pericia contabil|\bactuari|\batuari"),
    ("PUBLICIDADE", r"\bpublicidade\b|\bpropaganda\b|assessoria de imprensa|"
                    r"\bmarketing\b|\bveiculacao\b|\bmidia\b|\banuncio"),
    ("EDUCACAO", r"\bcurso(s)?\b|\btreinamento(s)?\b|\bcapacitac|\bpalestra|"
                 r"\bcongresso|\bseminario|\bworkshop\b|\bqualificacao profissional"),
    # "sistema de ..." sozinho casava DEMAIS: "sistema de ar condicionado",
    # "sistema de combate a incêndio" e "sistema de CFTV" viravam TI, e o
    # cruzamento CNAE × objeto passava a acusar construtora e empresa de
    # segurança de atuar fora do próprio ramo. O qualificador depois de
    # "sistema" agora tem de ser de informática.
    ("TI", r"\bsoftware\b|\bsistema(s)? (informat|de gestao|de informac|"
           r"integrad(o|a)s? de gestao|operacional|erp\b)|"
           r"licenc(a|as|iamento) de uso|desenvolvimento de sistema|"
           r"hospedagem de (site|sistema)|\bdatacenter\b|\bcloud\b|"
           r"tecnologia da informacao|\binformatica\b|\bmicrocomputador|"
           r"\bnotebook|outsourcing de impressao|\bhelp ?desk\b|"
           r"rede logica|cabeamento estruturado|\bantivirus\b|banco de dados|"
           r"\bsuprimento(s)? de informatica"),
    ("TELECOM", r"\btelefonia\b|link de dados|\binternet\b|\btelecomunicac|"
                r"dados moveis|\bvoip\b"),
    ("GRAFICA", r"\bgrafic|impressao (de|grafica)|\bencaderna|\breprografia\b|"
                r"\bxerox\b|material impresso"),
    # Regra 2 do projeto (plural/singular): "\bhotel\b" não casava "hotéis",
    # "\bcartucho\b" não casava "cartuchos" e "\bmotorista\b" não casava
    # "motoristas" — as duas formas aparecem no mesmo lote de contratos.
    ("VIAGENS", r"\bpassage(m|ns)\b|agenciamento de viage|agencia(s)? de viage|"
                r"\bhote(l|is)\b|\bhotelar|hospedagem em hotel"),
    ("ALIMENTACAO", r"\bbuffet(s)?\b|coffee ?break\b|\balimentac|\brefeic|"
                    r"\bcatering\b|agua mineral|\bcopa\b"),
    ("SEGURANCA", r"\bvigilancia\b|seguranca patrimonial|\bportaria(s)?\b|"
                  r"brigada de incendio|\bcftv\b|controle de acesso"),
    ("LIMPEZA_PREDIAL", r"\blimpeza\b|conservacao predial|\bhigieniza|"
                        r"\bjardinagem\b|\bdedetiza|\bcopeirage|\bpaisagis"),
    ("RH_TERCEIRIZACAO", r"\bterceiriza|mao de obra|\brecrutamento\b|"
                         r"selecao de pessoal|\bestagiari|agente de integracao"),
    ("IMOBILIARIO", r"loca(cao|ção) de imove|aluguel de imove|\bcondominio(s)?\b|"
                    r"\bcorretagem\b|avaliacao imobiliaria"),
    ("LOCACAO", r"loca(cao|ção) de (veiculo|automovel|frota|maquina|equipamento)|"
                r"aluguel de (veiculo|maquina|equipamento)"),
    ("TRANSPORTE", r"\btransporte\b|\bfrete(s)?\b|\bmotorista(s)?\b|\bmudanca\b|"
                   r"\blogistica\b|\bcorreio|\bpostage(m|ns)\b"),
    ("ENERGIA", r"energia eletrica|fornecimento de energia"),
    ("MATERIAL_ESCRITORIO", r"material de (expediente|escritorio)|\bpapelaria\b|"
                            r"\btoner(s)?\b|\bcartucho(s)?\b"),
    ("MOBILIARIO", r"\bmobiliario\b|\bmove(l|is)\b|\bcadeira(s)?\b"),
    # COMPRA de material de obra é outra coisa que serviço de obra: quem vende
    # material é comércio, e comércio não está na lista de CNAEs compatíveis
    # com CONSTRUCAO — de propósito, porque construtora-comércio é sinal.
    # Sem separar os dois, "aquisição de material elétrico" comprada de uma
    # distribuidora virava acusação contra a distribuidora. Vem ANTES de
    # CONSTRUCAO porque a primeira regra que casa vence.
    ("MATERIAL_CONSTRUCAO",
     r"material (de construcao|eletric|hidraulic|de acabamento)|"
     r"\bferragens\b|\bferramenta(s)?\b|"
     r"aquisicao de (aparelho(s)? de )?ar condicionado"),
    # Obra por último: é o setor com mais sinônimos e o que mais atrai texto
    # solto ("reforma do sistema", "manutenção predial").
    ("CONSTRUCAO",
     r"\bobra(s)?\b|\bconstruc|\breforma|\bempreiteir|\balvenaria|"
     r"\bimpermeabiliza|\bhidraulic|\beletric|\bsubestacao\b|\bmarcenaria|"
     r"\bedificac|\bretrofit|\btelhado\b|\bpintura\b|\belevador(es)?\b|"
     r"instalac(ao|oes) (eletrica|hidraulica|predial)|ar condicionado|"
     r"\bclimatizacao\b|manutencao predial"),
]
_REGRAS_OBJETO = [(s, re.compile(rx)) for s, rx in REGRAS_OBJETO]

# Setores de CNAE aceitáveis para cada setor de objeto. Generosos de
# propósito: o objetivo é sinalizar poucos casos fortes, não produzir lista.
COMPATIVEIS: dict[str, set[str]] = {
    "CONSTRUCAO": {"CONSTRUCAO", "ENGENHARIA", "INDUSTRIA", "LOCACAO",
                   "MANUTENCAO", "ENERGIA", "AGUA_SANEAMENTO", "IMOBILIARIO",
                   "LIMPEZA_PREDIAL", "EXTRATIVA"},
    "TI": {"TI", "TELECOM", "MANUTENCAO", "COMERCIO", "ENGENHARIA",
           "PESQUISA", "EDITORIAL_MIDIA", "INDUSTRIA"},
    "TELECOM": {"TELECOM", "TI", "ENGENHARIA", "COMERCIO"},
    # CFTV e controle de acesso são instalados por eletricista (43) e mantidos
    # por oficina de eletrônica (95); sem eles a instalação de câmera acusava
    # o instalador.
    "SEGURANCA": {"SEGURANCA", "LIMPEZA_PREDIAL", "RH_TERCEIRIZACAO", "TI",
                  "CONSTRUCAO", "MANUTENCAO", "COMERCIO"},
    "LIMPEZA_PREDIAL": {"LIMPEZA_PREDIAL", "SEGURANCA", "RH_TERCEIRIZACAO",
                        "CONSTRUCAO", "COMERCIO", "AGUA_SANEAMENTO",
                        "MANUTENCAO"},
    "ALIMENTACAO": {"ALIMENTACAO", "COMERCIO", "INDUSTRIA", "AGRO"},
    "VIAGENS": {"VIAGENS", "TRANSPORTE", "ALIMENTACAO"},
    # motorista e transporte de valores vêm de terceirização (78) e de
    # empresa de segurança (80), não só de transportadora.
    "TRANSPORTE": {"TRANSPORTE", "LOCACAO", "VIAGENS", "COMERCIO",
                   "RH_TERCEIRIZACAO", "SEGURANCA"},
    "LOCACAO": {"LOCACAO", "TRANSPORTE", "COMERCIO", "INDUSTRIA", "TI"},
    "IMOBILIARIO": {"IMOBILIARIO", "CONSTRUCAO"},
    "GRAFICA": {"GRAFICA", "EDITORIAL_MIDIA", "COMERCIO", "INDUSTRIA",
                "PUBLICIDADE"},
    "JURIDICO_CONTABIL": {"JURIDICO_CONTABIL", "PESQUISA"},
    "PUBLICIDADE": {"PUBLICIDADE", "EDITORIAL_MIDIA", "GRAFICA",
                    "CULTURA_ESPORTE"},
    "EDUCACAO": {"EDUCACAO", "PESQUISA", "CULTURA_ESPORTE", "ENGENHARIA",
                 "EDITORIAL_MIDIA"},
    "SAUDE": {"SAUDE", "FINANCEIRO", "COMERCIO"},   # operadora de plano = div. 65
    "RH_TERCEIRIZACAO": {"RH_TERCEIRIZACAO", "LIMPEZA_PREDIAL", "SEGURANCA",
                         "EDUCACAO"},
    "SEGUROS": {"FINANCEIRO"},
    "ENERGIA": {"ENERGIA", "AGUA_SANEAMENTO", "CONSTRUCAO"},
    "MATERIAL_ESCRITORIO": {"COMERCIO", "INDUSTRIA", "GRAFICA", "TI"},
    "MOBILIARIO": {"COMERCIO", "INDUSTRIA", "LOCACAO"},
    "MATERIAL_CONSTRUCAO": {"COMERCIO", "INDUSTRIA", "CONSTRUCAO",
                            "ENGENHARIA", "EXTRATIVA", "ENERGIA"},
}


def setor_do_objeto(texto) -> str:
    """Setor econômico do que foi contratado, a partir do texto do objeto."""
    t = T.normalizar(texto)
    if not t:
        return "OUTRO"
    for setor, rx in _REGRAS_OBJETO:
        if rx.search(t):
            return setor
    return "OUTRO"


def cnae_setor(codigo, descricao: str = "") -> str:
    """Setor econômico do CNAE. Usa a divisão (2 dígitos); o texto é reserva."""
    d = so_digitos(codigo)
    if len(d) >= 4:                       # 4399103 e "4399-1/03" -> 43
        setor = CNAE_DIVISAO.get(int(d[:2]))
        if setor:
            return setor
    pelo_texto = setor_do_objeto(descricao)
    return pelo_texto if pelo_texto != "OUTRO" else "DESCONHECIDO"


def _combina(setor_objeto: str, setor_cnae: str) -> bool:
    if not setor_objeto or setor_objeto == "OUTRO":
        return True                       # objeto ilegível não acusa ninguém
    if setor_cnae in GENERICOS:
        return True
    return setor_cnae in (COMPATIVEIS.get(setor_objeto, set()) | {setor_objeto})


def ficha_combina_com_objeto(ficha: dict, setor_objeto: str) -> bool:
    """A empresa tem alguma atividade declarada compatível com o objeto?

    Três chances antes de sinalizar, porque um falso positivo aqui vira
    acusação de fachada contra empresa legítima:
    1. o CNAE principal, pela divisão;
    2. o TEXTO do CNAE principal — "comércio varejista de material de
       construção" é divisão 47 (comércio), mas quem vende material de obra
       para uma obra é fornecedor compatível;
    3. qualquer CNAE secundário. Aqui a regra é mais estrita: só vale
       compatibilidade POSITIVA, porque um secundário genérico de escritório
       (8211-3/00) é quase universal e liberaria todo mundo.
    """
    cod = _campo(ficha, "cnae_fiscal", "cnae_fiscal_codigo", "cnae_principal")
    desc = _campo(ficha, "cnae_fiscal_descricao", "cnae_principal_descricao")
    if _combina(setor_objeto, cnae_setor(cod, desc)):
        return True
    if setor_do_objeto(desc) == setor_objeto:
        return True
    for c, d in _cnaes_secundarios(ficha):
        setor = cnae_setor(c, d)
        if setor == setor_objeto or setor in COMPATIVEIS.get(setor_objeto, set()):
            return True
        if setor_do_objeto(d) == setor_objeto:
            return True
    return False


# ------------------------------------------------------------------- bases
def _perfil_fichas(fichas: dict[str, dict]) -> pd.DataFrame:
    """Uma linha por CNPJ com ficha: sede, porte e atividade declarada."""
    linhas = []
    for cnpj, f in fichas.items():
        mun, uf = municipio_uf(f)
        porte = porte_classe(f)
        linhas.append({
            "cnpj": cnpj,
            "razao_social": _campo(f, "razao_social", "nome_fantasia"),
            "municipio": mun, "uf": uf,
            "local": f"{mun}/{uf}" if mun and uf else (mun or ""),
            "regiao": _regiao(mun, uf),
            "porte": porte, "porte_rotulo": PORTE_ROTULO[porte],
            "cnae": _campo(f, "cnae_fiscal_descricao",
                           "cnae_principal_descricao"),
            "cnae_setor": cnae_setor(
                _campo(f, "cnae_fiscal", "cnae_fiscal_codigo"),
                _campo(f, "cnae_fiscal_descricao")),
        })
    return pd.DataFrame(linhas)


def _regiao(municipio: str, uf: str) -> str:
    if not uf:
        return "Sede não informada na ficha"
    if uf != "SP":
        return f"Fora de SP ({uf})"
    if not municipio:
        return "SP, município não informado"
    return ("Capital (São Paulo)" if municipio == CAPITAL_SP
            else "Interior e litoral de SP")


def _pagamentos_pj(staged) -> pd.DataFrame:
    """Pagamentos de MERCADO a pessoa jurídica com CNPJ legível.

    Usa `fornecedores_mercado`, que já tira folha agregada e repasse legal ao
    Confea/Mútua — o Confea fica em Brasília, e mantê-lo aqui faria "fornecedor
    de fora de SP" medir transferência obrigatória em vez de compra.

    Estornos (valor negativo) saem: devolução não é dinheiro que chegou a
    município nenhum, e somá-la ao lado do positivo mistura dois sinais.
    """
    pg = fornecedores_mercado(staged)
    if pg.empty or "VALOR" not in pg.columns:
        return pd.DataFrame()
    d = pg.copy()
    cnpj = coluna(d, "FAVORECIDO_CNPJ").fillna("").astype(str)
    if not cnpj.str.len().eq(14).any():          # staged antigo, sem a coluna
        cnpj = coluna(d, "CPF_CNPJ").map(so_digitos)
    d["cnpj"] = cnpj
    d["valor"] = pd.to_numeric(d["VALOR"], errors="coerce").fillna(0)
    d = d[(d["cnpj"].str.len() == 14) & (d["valor"] > 0)]
    return d


def _porque_sem_geografia(staged, base: pd.DataFrame,
                          perfil: pd.DataFrame) -> dict:
    """Qual dos elos quebrou: o CNPJ nos pagamentos, a ficha, ou o encontro.

    Três causas produzem o mesmo resultado vazio e pedem providências opostas:
    o portal não publicar o CNPJ inteiro (nada a fazer na coleta de fichas), a
    ficha voltar sem município (repetir a consulta resolve), ou os dois lados
    existirem e não se cruzarem (o piso de consulta pegou outros CNPJs).
    """
    pg = df(staged, "pagamentos")
    total_pago = float(pd.to_numeric(coluna(pg, "VALOR", 0),
                                     errors="coerce").fillna(0).clip(lower=0).sum())
    valor_pj = float(base["valor"].sum()) if not base.empty else 0.0
    com_mun = int((perfil.get("municipio", pd.Series(dtype=str))
                   .fillna("").astype(str).str.len() > 0).sum()) if not perfil.empty else 0
    m = {"valor_total_pago": round(total_pago, 2),
         "valor_pj_com_cnpj_legivel": round(valor_pj, 2),
         "pct_do_pago_com_cnpj_legivel": (round(100 * valor_pj / total_pago, 2)
                                          if total_pago else 0.0),
         "fichas_com_municipio": com_mun}
    if total_pago > 0 and valor_pj < 0.1 * total_pago:
        return {
            "metricas": m, "texto": (
                f"o portal não publica o CNPJ completo do favorecido na maior "
                f"parte dos pagamentos. Apenas {fmt_brl(valor_pj)} de "
                f"{fmt_brl(total_pago)} pagos "
                f"({m['pct_do_pago_com_cnpj_legivel']:.1f}%) têm um CNPJ de 14 "
                "dígitos legível, e sem ele não existe chave para consultar a "
                "Receita. **Repetir a coleta das fichas não resolveria** — o "
                "elo que falta está do lado do portal, não do nosso."),
            "como_verificar": (
                "Pedir a relação de pagamentos com o CNPJ completo do "
                "favorecido, ou ao menos com os oito primeiros dígitos (raiz), "
                "que já bastam para identificar a empresa. Lei 12.527/2011, "
                "art. 8º, §1º, III; Decreto 7.724/2012, art. 7º, §3º, VI — "
                "pessoa jurídica que recebe recurso público não tem "
                "expectativa de sigilo sobre o próprio CNPJ.")}
    if com_mun == 0 and not perfil.empty:
        return {
            "metricas": m, "texto": (
                f"as {len(perfil)} fichas consultadas voltaram sem o campo de "
                "município. O CNPJ está publicado nos pagamentos; o que faltou "
                "foi a resposta da API de CNPJ."),
            "como_verificar": ("Repetir a coleta das fichas (BrasilAPI ou "
                               "minhareceita) e conferir se `municipio` voltou "
                               "preenchido.")}
    return {
        "metricas": m, "texto": (
            "os dois lados existem, mas os CNPJs com ficha não são os que "
            "aparecem nos pagamentos deste recorte — provavelmente o piso de "
            "materialidade e o teto de consultas selecionaram outro conjunto."),
        "como_verificar": ("Baixar `fornecedores_fichas.csv` e "
                           "`pagamentos.csv` de `data/staged` e conferir a "
                           "interseção de CNPJs entre os dois.")}


def _coluna_centro_custo(pg: pd.DataFrame) -> pd.Series | None:
    """A coluna de centro de custo dos pagamentos, se o portal publicar uma.

    Na rodada real ela não veio: `despesas_centro_custo` sabe QUAL unidade
    gastou mas não sabe PARA QUEM, e `pagamentos` sabe para quem mas não de
    qual unidade. Sem esta coluna o cruzamento unidade × sede do fornecedor
    simplesmente não existe — e é isso que o achado diz, em vez de estimar.
    """
    for c in ("CENTRO_CUSTO", "NOME_CENTRO_CUSTO", "CENTRO_DE_CUSTO"):
        if c in pg.columns:
            s = pg[c].fillna("").astype(str)
            if s.str.strip().ne("").any():
                return s
    return None


def _objetos_contratados(staged) -> pd.DataFrame:
    """Objeto contratado por CNPJ, das duas fontes que publicam objeto.

    São bases DIFERENTES — o portal do conselho e o PNCP — e não são somadas:
    cada linha é um contrato e a coluna `fonte` diz de onde veio. O valor aqui
    é valor de CONTRATO, não de pagamento; ele serve só como corte de
    materialidade para decidir o que vale investigar.
    """
    partes: list[pd.DataFrame] = []

    ct = df(staged, "contratos")
    if not ct.empty and "CONTRATADA_CNPJ" in ct.columns:
        partes.append(pd.DataFrame({
            "cnpj": coluna(ct, "CONTRATADA_CNPJ").fillna("").astype(str),
            "objeto": coluna(ct, "OBJETO").fillna("").astype(str),
            "valor": pd.to_numeric(coluna(ct, "VALOR"), errors="coerce").fillna(0),
            "referencia": coluna(ct, "NUMERO_CONTRATO").fillna("").astype(str),
            "_ano": coluna(ct, "_ano"),
            "fonte": "contrato (portal do conselho)",
        }))

    pn = df(staged, "pncp_contratos")
    # `orgao_papel` separa o contrato DO CREA-SP do contrato de órgão apenas
    # relacionado (Confea, por exemplo, que a coleta traz para comparação).
    # Sem a coluna não há como distinguir, e o default não pode ser "é do
    # alvo": isso atribuiria ao Crea-SP contrato de outro órgão e acusaria o
    # fornecedor errado. Sem a coluna, o PNCP simplesmente não entra.
    if not pn.empty and {"fornecedor_ni", "orgao_papel"} <= set(pn.columns):
        papel = pn["orgao_papel"].fillna("").astype(str).str.lower()
        alvo = pn[papel.eq("alvo")]
        if not alvo.empty:
            glob = pd.to_numeric(coluna(alvo, "valorGlobal"),
                                 errors="coerce").fillna(0)
            acum = pd.to_numeric(coluna(alvo, "valorAcumulado"),
                                 errors="coerce").fillna(0)
            partes.append(pd.DataFrame({
                "cnpj": coluna(alvo, "fornecedor_ni").map(so_digitos),
                "objeto": coluna(alvo, "objeto").fillna("").astype(str),
                "valor": glob.where(glob > 0, acum),
                "referencia": ("PNCP " +
                               coluna(alvo, "numeroContrato").fillna("").astype(str)),
                "_ano": coluna(alvo, "_ano"),
                "fonte": "contrato (PNCP)",
            }))

    if not partes:
        return pd.DataFrame()
    d = pd.concat(partes, ignore_index=True)
    d = d[(d["cnpj"].str.len() == 14) & (d["objeto"].str.strip() != "")]
    d = d.drop_duplicates(subset=["cnpj", "objeto", "valor"])
    # O MESMO contrato costuma aparecer nas duas bases — o portal publica e o
    # PNCP recebe a mesma avença (a fixture tem exatamente isso: CT-01/2022 no
    # portal e "1/2022" no PNCP, mesmo fornecedor e mesmo valor). O
    # `drop_duplicates` acima não pega o espelho porque o objeto é redigido
    # diferente em cada base; sem esta segunda passada o contrato conta duas
    # vezes em "contratos examinados" e o valor sinalizado sai somado em
    # dobro. A chave é fornecedor + valor, e fica a linha do portal, que é a
    # fonte primária. Duplicata DENTRO da mesma base é preservada de
    # propósito: contrato repetido de mesmo valor é sinal, não erro.
    if d.empty:
        return d
    portal = d["fonte"].str.contains("portal", regex=False)
    chave = d["cnpj"] + "|" + d["valor"].round(2).astype(str)
    espelho = (~portal) & chave.isin(set(chave[portal]))
    return d[~espelho]


# ----------------------------------------------------------------- achados
def _achados_geografia(staged, base: pd.DataFrame,
                       perfil: pd.DataFrame) -> list[Finding]:
    """Concentração geográfica, sede fora de SP e cruzamento com a unidade."""
    achados: list[Finding] = []
    total_pj = float(base["valor"].sum())
    com_ficha = base[base["municipio"].fillna("") != ""]
    valor_geo = float(com_ficha["valor"].sum())
    if valor_geo <= 0 or total_pj <= 0:
        # "ou" não serve num relatório de fiscalização: entre duas causas
        # possíveis, a ferramenta tem os dados para dizer qual é. Na rodada
        # real o universo PJ inteiro somou R$ 18,5 milhões contra R$ 4,29
        # bilhões pagos — não foi a ficha que veio vazia, foi o CNPJ que o
        # portal não publica inteiro, e nesse caso nenhuma consulta à Receita
        # resolveria. Recomendar "repetir a coleta das fichas" seria mandar o
        # operador gastar horas no lugar errado.
        causa = _porque_sem_geografia(staged, base, perfil)
        achados.append(Finding(
            id="fornecedores-geografia-indisponivel", frente=FRENTE,
            gravidade="info",
            titulo="Análise geográfica de fornecedores INDISPONÍVEL nesta rodada",
            descricao=(
                "Sem sede declarada de um lado e pagamento do outro não há como "
                "dizer para onde o dinheiro foi no território, e este relatório "
                "não estima o que não mediu. A ausência aqui não é indício de "
                "nada — é falta de dado.\n\n"
                f"**Causa apurada:** {causa['texto']}"),
            metricas={"fornecedores_com_ficha": int(perfil.shape[0]),
                      "valor_pj_total": round(total_pj, 2),
                      "valor_com_sede_na_ficha": round(valor_geo, 2),
                      **causa["metricas"]},
            como_verificar=causa["como_verificar"],
        ))
        return achados

    cobertura = 100 * valor_geo / total_pj
    por_mun = (com_ficha.groupby("local")
               .agg(total=("valor", "sum"), fornecedores=("cnpj", "nunique"),
                    pagamentos=("valor", "size"))
               .reset_index().sort_values("total", ascending=False))
    por_mun["share_pct"] = (100 * por_mun["total"] / valor_geo).round(2)
    por_mun["acumulado_pct"] = por_mun["share_pct"].cumsum().round(2)
    n_mun = int(len(por_mun))
    k = min(TOP_MUNICIPIOS, n_mun)
    share_topk = float(por_mun["share_pct"].head(k).sum())
    share_top1 = float(por_mun["share_pct"].iloc[0])
    # HHI sobre a distribuição municipal do valor: 1,0 = tudo num município.
    hhi = float(((por_mun["total"] / valor_geo) ** 2).sum())
    # A capital é identificada pelo NOME do município, não pela UF: a ficha às
    # vezes volta com município e sem UF, e nesse caso "SAO PAULO" continua
    # sendo a capital. O que a UF faz aqui é só impedir que um homônimo de
    # outro estado entre na conta.
    capital = float(com_ficha.loc[(com_ficha["municipio"] == CAPITAL_SP)
                                  & (com_ficha["uf"].isin(["SP", ""])),
                                  "valor"].sum())
    pct_capital = 100 * capital / valor_geo
    mun_sp = int(com_ficha.loc[com_ficha["uf"] == "SP", "municipio"].nunique())

    lideres = por_mun["local"].head(6).tolist()
    serie = A.serie_anual(com_ficha[com_ficha["local"].isin(lideres)],
                          "valor", by="local")
    # Com poucos municípios o "X% em k de n" vira tautologia ("100% em 4 de 4"):
    # o título passa a dizer o que de fato mede, que é a estreiteza do mapa.
    # O denominador destes percentuais é o valor pago a fornecedor cuja ficha
    # trouxe MUNICÍPIO — que é menos que "com ficha", porque parte das fichas
    # volta sem sede. Dizer "do valor com ficha" no título inflaria o
    # denominador declarado em relação ao que a conta realmente usa.
    titulo = (f"{share_topk:.0f}% do valor pago a fornecedor com sede "
              f"identificada ficou em {k} município(s), de {n_mun} no total "
              f"(cobertura: {cobertura:.0f}% do valor pago a PJ de mercado)"
              if n_mun > TOP_MUNICIPIOS else
              f"Os fornecedores com sede identificada estão em {n_mun} "
              f"município(s), e {share_top1:.1f}% do valor ficou num só "
              f"(cobertura: {cobertura:.0f}% do valor pago a PJ de mercado)")
    achados.append(Finding(
        id="fornecedores-geografia", frente=FRENTE,
        gravidade="baixa" if (share_topk >= 85 and cobertura >= 40) else "info",
        titulo=titulo,
        descricao=(
            f"O Crea-SP fiscaliza {MUNICIPIOS_SP} municípios e mantém unidades "
            "no estado inteiro — superintendências regionais e UGIs no "
            "interior. Este é o mapa de onde estão sediadas as empresas que "
            "receberam o dinheiro, pela sede declarada na Receita.\n\n"
            f"Os fornecedores com sede identificada estão em {n_mun} "
            f"município(s), dos quais {mun_sp} com UF de São Paulo declarada "
            f"na ficha; {pct_capital:.0f}% do valor ficou com "
            "empresas sediadas na capital. Concentração **não é "
            "irregularidade**: a sede do conselho fica na capital, contrato "
            "grande costuma ser de empresa grande, e empresa grande se sedia em "
            "centro econômico. O que o número permite perguntar é se a compra "
            "descentralizada existe de fato ou se toda unidade do interior "
            "compra da mesma praça.\n\n"
            f"**Limite honesto:** a ficha só existe para quem passou do piso de "
            f"{fmt_brl(config.PISO_ANALISE_FORNECEDOR)} e coube no teto de "
            f"{config.MAX_CONSULTAS_CNPJ} consultas. Este percentual é do "
            "universo com SEDE identificada na ficha, que é "
            f"{cobertura:.0f}% do valor pago a pessoa jurídica de mercado — "
            "ou seja, já fora a folha paga a rótulo agregado e o repasse "
            "obrigatório ao Confea e à Mútua. Esse universo é enviesado para "
            "empresa grande, e empresa grande concentra mais em capital: o "
            "número, portanto, **superestima** a concentração do conjunto "
            "completo de fornecedores."),
        evidencia=por_mun.head(80),
        serie=serie, grafico="area_stacked",
        tabela_anual=A.ranking_anual(com_ficha, "local", "valor", top=8),
        base_legal="Lei 14.133/2021, art. 5º (competitividade e "
                   "impessoalidade); Lei 5.194/1966, art. 34.",
        como_verificar="Pedir, por unidade regional, a relação de fornecedores "
                       "contratados e o número de propostas recebidas em cada "
                       "certame — é o que separa concentração de mercado de "
                       "restrição de disputa.",
        metricas={"cobertura_pct": round(cobertura, 1),
                  "valor_com_sede_na_ficha": round(valor_geo, 2),
                  "valor_pj_total": round(total_pj, 2),
                  "municipios": n_mun, "municipios_sp": mun_sp,
                  "share_top1_pct": round(share_top1, 1),
                  "share_top5_pct": round(share_topk, 1),
                  "pct_capital": round(pct_capital, 1),
                  "hhi_municipios": round(hhi, 4),
                  "fornecedores_com_ficha": int(com_ficha["cnpj"].nunique())},
    ))

    # ---------------------------------------------- sede fora de São Paulo
    fora = com_ficha[(com_ficha["uf"] != "") & (com_ficha["uf"] != "SP")]
    v_fora = float(fora["valor"].sum())
    if not fora.empty and v_fora > 0:
        pct_fora = 100 * v_fora / valor_geo
        por_uf = (fora.groupby("uf")
                  .agg(total=("valor", "sum"), fornecedores=("cnpj", "nunique"))
                  .reset_index().sort_values("total", ascending=False))
        por_uf["share_do_com_sede_pct"] = (100 * por_uf["total"] /
                                          valor_geo).round(2)
        tem_cc = _coluna_centro_custo(base) is not None
        achados.append(Finding(
            id="fornecedores-fora-de-sp", frente=FRENTE,
            gravidade="baixa" if pct_fora >= 40 else "info",
            titulo=(f"{pct_fora:.1f}% do valor pago a fornecedor com sede "
                    f"identificada ({fmt_brl(v_fora)}) foi "
                    f"para {int(fora['cnpj'].nunique())} fornecedor(es) "
                    f"sediado(s) fora de São Paulo, em {len(por_uf)} UF(s)"),
            descricao=(
                "Contratar empresa de outro estado **não é irregularidade**. A "
                "licitação é aberta a todo o país por princípio, e limitar a "
                "disputa por território seria justamente o ilícito. Este achado "
                "é um mapa, não uma acusação.\n\n"
                "O que o mapa serve para perguntar: se uma unidade do interior "
                "paulista compra serviço rotineiro de empresa sediada a mil "
                "quilômetros, ou a disputa foi mais ampla que o mercado local, "
                "ou o mercado local não foi consultado. As duas leituras são "
                "possíveis e só o processo licitatório decide entre elas."
                + ("" if tem_cc else
                   "\n\n**O cruzamento com a unidade que gastou não foi "
                   "possível nesta rodada**: a base de pagamentos publicada não "
                   "traz centro de custo, e a base que traz centro de custo "
                   "(`despesas_centro_custo`) não traz o fornecedor. Ligar as "
                   "duas exigiria o empenho com as duas informações, que o "
                   "portal não publica.")),
            evidencia=por_uf,
            serie=A.serie_anual(com_ficha, "valor", by="regiao"),
            grafico="area_stacked",
            metricas={"pct_fora_de_sp": round(pct_fora, 2),
                      "valor_fora_de_sp": round(v_fora, 2),
                      "ufs_fora_de_sp": int(len(por_uf)),
                      "fornecedores_fora_de_sp": int(fora["cnpj"].nunique()),
                      "cobertura_pct": round(cobertura, 1),
                      "cruzamento_com_centro_de_custo": int(tem_cc)},
            como_verificar="Pedir o edital e o mapa de propostas dos contratos "
                           "listados: quantas empresas locais participaram e "
                           "por que não venceram.",
        ))

    # --------------------------- unidade que gastou × sede do fornecedor
    cc = _coluna_centro_custo(base)
    if cc is not None:
        from .superintendencias import ROTULO_CAMADA, camada, normalizar_unidade
        d = com_ficha.copy()
        d["unidade"] = cc.reindex(d.index).map(normalizar_unidade)
        d = d[d["unidade"].str.len() > 2]
        if not d.empty:
            unicos = {u: camada(u) for u in d["unidade"].unique()}
            d["camada"] = d["unidade"].map(unicos).map(ROTULO_CAMADA)
            cruz = (d.pivot_table(index="unidade", columns="regiao",
                                  values="valor", aggfunc="sum", fill_value=0.0))
            cruz["total"] = cruz.sum(axis=1)
            colunas_fora = [c for c in cruz.columns if str(c).startswith("Fora de SP")]
            cruz["fora_de_sp"] = (cruz[colunas_fora].sum(axis=1)
                                  if colunas_fora else 0.0)
            cruz["pct_fora_de_sp"] = (100 * cruz["fora_de_sp"] /
                                      cruz["total"].replace(0, pd.NA)).round(1)
            cruz = cruz.sort_values("total", ascending=False)
            campo = d[d["camada"] == ROTULO_CAMADA["CAMPO"]]
            v_campo_fora = float(campo.loc[campo["uf"].ne("SP") &
                                           campo["uf"].ne(""), "valor"].sum())
            achados.append(Finding(
                id="fornecedores-geografia-por-unidade", frente=FRENTE,
                gravidade="info",
                titulo=(f"{int(cruz.shape[0])} unidade(s) cruzadas com a sede "
                        f"do fornecedor; {fmt_brl(v_campo_fora)} saíram de "
                        "unidades de campo para empresas de fora de SP"),
                descricao=(
                    "O cruzamento entre a unidade que gastou e o município onde "
                    "o fornecedor é sediado. Serve para ver se a "
                    "descentralização administrativa corresponde a alguma "
                    "descentralização de compra — e não para acusar: unidade de "
                    "campo comprando de fornecedor distante pode ser contrato "
                    "corporativo assinado na sede e rateado, que é prática "
                    "normal e frequentemente mais barata.\n\n"
                    f"Cobertura: {cobertura:.0f}% do valor pago a pessoa "
                    "jurídica tem ficha; o resto não entra nesta tabela."),
                evidencia=cruz.round(2).reset_index().head(80),
                metricas={"unidades": int(cruz.shape[0]),
                          "valor_campo_para_fora_de_sp": round(v_campo_fora, 2),
                          "cobertura_pct": round(cobertura, 1)},
            ))
    return achados


def _achado_porte(base: pd.DataFrame, perfil: pd.DataFrame) -> list[Finding]:
    """Quanto do dinheiro chega a micro e pequenas empresas."""
    total_pj = float(base["valor"].sum())
    com_porte = base[base["porte"].fillna("NAO_INFORMADO").isin(
        ["ME", "EPP", "DEMAIS"])]
    valor_porte = float(com_porte["valor"].sum())
    if valor_porte <= 0 or total_pj <= 0:
        return [Finding(
            id="fornecedores-porte-indisponivel", frente=FRENTE,
            gravidade="info",
            titulo="Medição do que chega a micro e pequenas empresas "
                   "INDISPONÍVEL nesta rodada",
            descricao=(
                "Nenhuma ficha consultada trouxe o porte da empresa (campos "
                "`porte`/`codigo_porte`), ou nenhum CNPJ com ficha aparece nos "
                "pagamentos. Sem o porte não há como dizer quanto do dinheiro "
                "chegou a ME e EPP, e este relatório não estima o que não "
                "mediu."),
            metricas={"fornecedores_com_ficha": int(perfil.shape[0]),
                      "valor_pj_total": round(total_pj, 2)},
            como_verificar="Repetir a coleta das fichas e conferir o campo "
                           "`codigo_porte` (01 = ME, 03 = EPP, 05 = demais).",
        )]

    cobertura = 100 * valor_porte / total_pj
    por_porte = (com_porte.groupby(["porte", "porte_rotulo"])
                 .agg(total=("valor", "sum"), fornecedores=("cnpj", "nunique"))
                 .reset_index().sort_values("total", ascending=False))
    por_porte["share_pct"] = (100 * por_porte["total"] / valor_porte).round(2)
    v_me_epp = float(com_porte.loc[com_porte["porte"].isin(["ME", "EPP"]),
                                   "valor"].sum())
    n_me_epp = int(com_porte.loc[com_porte["porte"].isin(["ME", "EPP"]),
                                 "cnpj"].nunique())
    pct_me_epp = 100 * v_me_epp / valor_porte
    serie = A.serie_anual(com_porte, "valor", by="porte_rotulo")
    pct_ano = (serie.div(serie.sum(axis=1).replace(0, pd.NA), axis=0) * 100
               if not serie.empty else pd.DataFrame())
    # "0 de N" é rebaixado automaticamente e com razão — mas aqui o zero É a
    # medição. O título muda de forma para dizê-lo sem parecer achado vazio.
    n_porte = int(com_porte["cnpj"].nunique())
    # Denominador: valor pago a fornecedor cujo porte a ficha trouxe. Não é o
    # mesmo que "valor com ficha" — ficha sem porte fica de fora dos dois
    # lados da fração, e anunciar o denominador maior no título seria dizer
    # que a medida cobre mais do que cobre.
    titulo = (f"{pct_me_epp:.1f}% do valor pago a fornecedor com porte "
              f"conhecido ({fmt_brl(v_me_epp)}) foi "
              f"para micro e pequenas empresas — {n_me_epp} de {n_porte} "
              "fornecedor(es) com porte conhecido"
              if v_me_epp > 0 else
              f"Nenhum dos {n_porte} fornecedores com porte conhecido é micro "
              f"ou pequena empresa ({fmt_brl(valor_porte)} examinados)")
    return [Finding(
        id="fornecedores-porte", frente=FRENTE, gravidade="info",
        titulo=titulo,
        descricao=(
            "A Lei 14.133/2021, art. 4º, manda aplicar às contratações públicas "
            "o tratamento diferenciado dos arts. 42 a 49 da LC 123/2006 — "
            "comprovação fiscal adiada, preferência no desempate, licitação "
            "exclusiva até R$ 80 mil, cota de até 25% em compras divisíveis. "
            "Medir quanto efetivamente chega a ME e EPP é medir se a política "
            "produz efeito.\n\n"
            "**O que este número NÃO prova.** Ele não afirma descumprimento. "
            "A própria lei traz exceções expressas: o art. 4º, §1º, afasta o "
            "tratamento diferenciado quando o valor estimado do ITEM — ou, em "
            "obra e serviço de engenharia, o da própria licitação — supera a "
            "receita bruta máxima admitida para enquadramento como empresa de "
            "pequeno porte; o §2º do mesmo artigo limita o benefício à ME ou "
            "EPP que ainda não tenha extrapolado esse teto somando os "
            "contratos já celebrados com a Administração no ano-calendário; e "
            "o art. 49 da LC 123/2006 dispensa o tratamento diferenciado "
            "quando não há ao menos três fornecedores competitivos "
            "enquadrados e sediados local ou regionalmente, quando o "
            "tratamento não é vantajoso para a Administração ou prejudica o "
            "conjunto do objeto, e quando a licitação é dispensável ou "
            "inexigível. Contrato de obra grande, plano de saúde e serviço de "
            "TI corporativo raramente cabem numa ME, e isso é legítimo.\n\n"
            "**Dois vieses conhecidos, ambos na mesma direção.** Primeiro, o "
            "universo: só tem ficha quem recebeu acima de "
            f"{fmt_brl(config.PISO_ANALISE_FORNECEDOR)} e coube nas "
            f"{config.MAX_CONSULTAS_CNPJ} maiores consultas — ou seja, os "
            "grandes. Segundo, o porte da ficha é o de HOJE, não o da época do "
            "contrato: empresa que era EPP em 2015 e cresceu aparece aqui como "
            "'demais'. Os dois vieses **subestimam** a fatia de ME e EPP, e o "
            f"número vale para {cobertura:.0f}% do valor pago a pessoa "
            "jurídica de mercado — já fora a folha paga a rótulo agregado e o "
            "repasse obrigatório ao Confea e à Mútua —, não para o total "
            "gasto pelo conselho."),
        evidencia=por_porte,
        serie=serie, grafico="area_stacked",
        tabela_anual=(pct_ano.round(1).reset_index() if not pct_ano.empty
                      else None),
        base_legal="Lei 14.133/2021, art. 4º; LC 123/2006, arts. 42 a 49; CF, "
                   "arts. 170, IX, e 179.",
        como_verificar="Pedir, por certame, se houve item exclusivo para ME/EPP "
                       "ou cota reservada (LC 123/2006, arts. 48, I e III) e, "
                       "quando não houve, a justificativa do art. 49.",
        metricas={"pct_me_epp": round(pct_me_epp, 2),
                  "valor_me_epp": round(v_me_epp, 2),
                  "valor_com_porte": round(valor_porte, 2),
                  "valor_pj_total": round(total_pj, 2),
                  "cobertura_pct": round(cobertura, 1),
                  "fornecedores_me_epp": n_me_epp,
                  "fornecedores_com_porte": int(com_porte["cnpj"].nunique())},
    )]


def _achado_cnae(staged, fichas: dict[str, dict],
                 perfil: pd.DataFrame) -> list[Finding]:
    """CNAE principal sem relação com o objeto contratado — casos fortes."""
    obj = _objetos_contratados(staged)
    if obj.empty:
        # Regra 5 do projeto: fonte ausente vira achado que DIZ que está
        # ausente, nunca silêncio. Sem texto de objeto — o portal publica
        # contrato sem descrever o que foi comprado, e o PNCP não trouxe nada
        # — não há o que confrontar com o CNAE.
        return [Finding(
            id="fornecedores-cnae-x-objeto-indisponivel", frente=FRENTE,
            gravidade="info",
            titulo="Cruzamento CNAE × objeto INDISPONÍVEL: nenhum contrato com "
                   "objeto descrito",
            descricao=(
                "Nem o portal do conselho nem o PNCP devolveram contrato com "
                "CNPJ da contratada e texto de objeto nesta rodada. Sem saber "
                "o que foi comprado não há como confrontar a compra com a "
                "atividade econômica declarada pela empresa na Receita. Nada "
                "é estimado no lugar disso."),
            metricas={"fornecedores_com_ficha": int(perfil.shape[0])},
            como_verificar="Conferir se a coleta de `contratos` trouxe as "
                           "colunas OBJETO e CONTRATADA_CNPJ preenchidas.",
        )]
    obj = obj[obj["cnpj"].isin(list(fichas.keys()))]
    obj = obj.assign(valor=pd.to_numeric(obj["valor"], errors="coerce").fillna(0))
    obj = obj[obj["valor"] >= PISO_CNAE_OBJETO]
    if obj.empty:
        return []
    # classifica os textos DISTINTOS de objeto, não linha a linha
    mapa = {t: setor_do_objeto(t) for t in obj["objeto"].unique()}
    obj = obj.assign(setor_objeto=obj["objeto"].map(mapa))
    obj = obj[obj["setor_objeto"] != "OUTRO"]
    if obj.empty:
        return []

    linhas = []
    for _, r in obj.iterrows():
        ficha = fichas[r["cnpj"]]
        if ficha_combina_com_objeto(ficha, r["setor_objeto"]):
            continue
        mun, uf = municipio_uf(ficha)
        linhas.append({
            "cnpj": r["cnpj"],
            "razao_social": _campo(ficha, "razao_social"),
            "sede": f"{mun}/{uf}" if mun and uf else (mun or ""),
            "cnae_principal": _campo(ficha, "cnae_fiscal_descricao"),
            "setor_do_cnae": cnae_setor(_campo(ficha, "cnae_fiscal"),
                                        _campo(ficha, "cnae_fiscal_descricao")),
            "objeto": r["objeto"], "setor_do_objeto": r["setor_objeto"],
            "valor_contrato": float(r["valor"]),
            "referencia": r["referencia"], "fonte": r["fonte"],
            "cnaes_secundarios": len(_cnaes_secundarios(ficha)),
        })
    if not linhas:
        return []

    ev = pd.DataFrame(linhas).sort_values("valor_contrato", ascending=False)
    total = float(ev["valor_contrato"].sum())
    examinados = int(len(obj))
    pct_sinalizado = 100 * len(ev) / examinados
    # Válvula de segurança: se metade dos contratos examinados for sinalizada,
    # a hipótese mais provável não é meia contratação irregular — é a
    # classificação de setor estar errada. Nesse caso o achado continua
    # publicado, mas rebaixado e com o aviso no texto.
    suspeito_de_si = pct_sinalizado > 50
    return [Finding(
        id="fornecedores-cnae-x-objeto", frente=FRENTE,
        gravidade="baixa" if suspeito_de_si else "media",
        titulo=(f"{len(ev)} de {examinados} contrato(s) examinados, somando "
                f"{fmt_brl(total)}, têm fornecedor cujo CNAE declarado não tem "
                "relação com o objeto"),
        descricao=(
            "A empresa declara à Receita sua atividade econômica principal "
            "(CNAE) e as secundárias. Quando o objeto contratado pertence a um "
            "setor econômico claramente distinto de todos os CNAEs declarados, "
            "cabe perguntar como a qualificação técnica foi comprovada.\n\n"
            "**O que isto NÃO prova.** O CNAE é declaratório e não limita o "
            "objeto social nem a capacidade legal da empresa de executar o "
            "contrato; empresa pode atuar fora do CNAE registrado sem cometer "
            "ilícito. O sinal só vira suspeita quando se soma a outros: "
            "empresa recém-criada, sócio em comum com outro fornecedor, "
            "ausência de certame.\n\n"
            "**Por que a lista é curta, de propósito.** Antes de entrar aqui, "
            "cada fornecedor passa por três filtros de defesa: o CNAE "
            "principal pela divisão, o texto do CNAE principal e todos os "
            "CNAEs secundários. CNAE genérico — 'serviços combinados de "
            "escritório e apoio administrativo', consultoria em gestão, "
            "organização associativa — nunca é sinalizado, porque cobre "
            "atividade legítima demais. Preferimos poucos casos fortes a uma "
            "lista longa e fraca.\n\n"
            "**Bases diferentes:** o valor exibido é o do CONTRATO (portal do "
            "conselho ou PNCP), não o pago; ele serve como corte de "
            f"materialidade — só contratos acima de {fmt_brl(PISO_CNAE_OBJETO)}, "
            "com fornecedor que tem ficha e com objeto que o classificador "
            f"conseguiu enquadrar num setor econômico, entram na conta: foram "
            f"{examinados} deles. Objeto que o classificador não reconhece "
            "fica de fora e nunca acusa ninguém. Contrato que aparece nas duas "
            "bases é contado uma vez só, pela linha do portal — os valores do "
            "portal e do PNCP não são somados."
            + ("\n\n**Aviso metodológico:** mais da metade dos contratos "
               f"examinados ({pct_sinalizado:.0f}%) foi sinalizada. Uma taxa "
               "assim quase nunca significa que metade das contratações é "
               "anômala — significa, muito mais provavelmente, que a "
               "classificação de setor econômico está errada para este "
               "conjunto de objetos. Trate a lista como pista a conferir uma a "
               "uma, não como medida." if suspeito_de_si else "")),
        evidencia=ev.head(40),
        base_legal="Lei 14.133/2021, arts. 62, II, e 67 (habilitação técnica); "
                   "Lei 8.666/1993, art. 30, para os contratos anteriores.",
        como_verificar="Pedir o processo de contratação e os atestados de "
                       "capacidade técnica aceitos para cada contrato listado.",
        metricas={"contratos": int(len(ev)),
                  "contratos_examinados": examinados,
                  "pct_sinalizado": round(pct_sinalizado, 1),
                  "valor_contratado": round(total, 2),
                  "fornecedores": int(ev["cnpj"].nunique()),
                  "piso_examinado": PISO_CNAE_OBJETO},
    )]


# -------------------------------------------------------- situação e idade
def _pagamentos_por_cnpj(staged) -> pd.core.groupby.DataFrameGroupBy | None:
    pg = df(staged, "pagamentos")
    if pg.empty or "CPF_CNPJ" not in pg or "DATA" not in pg:
        return None
    pj = pg.copy()
    pj["cnpj"] = pj["CPF_CNPJ"].map(so_digitos)
    pj = pj[pj["cnpj"].str.len() == 14].dropna(subset=["DATA"])
    return None if pj.empty else pj.groupby("cnpj")["DATA"]


def _primeiro_pagamento(staged) -> dict[str, pd.Timestamp]:
    g = _pagamentos_por_cnpj(staged)
    return {} if g is None else g.min().to_dict()


def _ultimo_pagamento(staged) -> dict[str, pd.Timestamp]:
    g = _pagamentos_por_cnpj(staged)
    return {} if g is None else g.max().to_dict()


def _achados_sancoes(staged) -> list[Finding]:
    """Pagamento a fornecedor que constava no CEIS/CNEP na data do pagamento.

    Duas listas, porque a lei separa: inidoneidade e proibição de contratar
    valem para toda a administração (efeito nacional); impedimento e suspensão
    valem para o órgão que os aplicou. Pagar a empresa impedida por um
    município não é irregularidade do Crea-SP — é contexto. Misturar as duas
    acusaria onde não há o que acusar.
    """
    san = df(staged, "sancoes")
    pg = _pagamentos_pj(staged)
    if san.empty:
        return [Finding(
            id="fornecedores-sancionados-indisponivel", frente=FRENTE, gravidade="info",
            titulo="Cruzamento com CEIS/CNEP não realizado: cadastros de sanção não coletados",
            descricao=("Os cadastros da CGU (CEIS: inidôneas e suspensas; CNEP: "
                       "Lei Anticorrupção) são baixados em ZIP diário do Portal da "
                       "Transparência. Nesta rodada nenhuma data respondeu, ou a "
                       "etapa não rodou. Sem eles, nenhum pagamento foi conferido "
                       "contra sanção — o que não é o mesmo que não haver."),
            como_verificar="Rodar de novo com rede; ou baixar o CSV do CEIS em "
                           "portaldatransparencia.gov.br/download-de-dados/ceis e "
                           "salvar em data/manual/sancoes.csv.",
        )]
    if pg.empty or "cnpj" not in pg.columns:
        return []
    san = san.copy()
    san["documento"] = san["documento"].astype(str).str.replace(r"\D", "", regex=True)
    san = san[san["documento"].str.len() == 14]
    pagos = pg.copy()
    pagos["DATA"] = pd.to_datetime(coluna(pagos, "DATA", None), errors="coerce")
    j = pagos.merge(san, left_on="cnpj", right_on="documento", how="inner")
    if j.empty:
        return [Finding(
            id="fornecedores-sancionados", frente=FRENTE, gravidade="info",
            titulo="Nenhum fornecedor pago consta no CEIS/CNEP",
            descricao=(f"{pg['cnpj'].nunique()} CNPJ(s) pagos conferidos contra "
                       f"{san['documento'].nunique()} sancionados. A foto dos "
                       "cadastros é a de hoje: sanção já expirada e removida não "
                       "aparece."),
            metricas={"cnpjs_conferidos": int(pg["cnpj"].nunique()),
                      "sancionados_no_cadastro": int(san["documento"].nunique())},
        )]
    ini = pd.to_datetime(j["inicio"], errors="coerce")
    fim = pd.to_datetime(j["fim"], errors="coerce")
    dentro = j["DATA"].notna() & ini.notna() & (j["DATA"] >= ini) & (fim.isna() | (j["DATA"] <= fim))
    j["pagamento_durante_sancao"] = dentro
    cols = [c for c in ("cnpj", "FAVORECIDO_NORM", "DATA", "VALOR", "cadastro", "categoria",
                        "inicio", "fim", "orgao_sancionador", "esfera", "efeito_nacional")
            if c in j.columns]
    nac = j[dentro & j["efeito_nacional"].astype(bool)]
    esc = j[dentro & ~j["efeito_nacional"].astype(bool)]
    achados: list[Finding] = []
    if not nac.empty:
        v = float(pd.to_numeric(nac["VALOR"], errors="coerce").fillna(0).sum())
        achados.append(Finding(
            id="fornecedores-sancionados", frente=FRENTE, gravidade="alta",
            titulo=(f"{nac['cnpj'].nunique()} fornecedor(es) receberam {fmt_brl(v)} "
                    "enquanto constavam como inidôneos ou proibidos de contratar "
                    "(efeito em toda a administração)"),
            descricao=(
                "Pagamento com data dentro do período de uma sanção de EFEITO "
                "NACIONAL: declaração de inidoneidade (Lei 8.666, art. 87, IV; "
                "Lei 14.133, art. 156, IV) ou proibição de contratar (Lei "
                "12.846). Essas sanções valem para toda a administração pública, "
                "não só para o órgão que as aplicou.\n\n"
                "**O que isto não prova:** que o contrato foi firmado durante a "
                "sanção. Pagamento de contrato anterior à sanção é execução de "
                "obrigação já assumida — a data da assinatura decide, e ela está "
                "no portal de contratos. O cruzamento aponta onde olhar."),
            evidencia=nac[cols].sort_values("VALOR", ascending=False).head(100),
            base_legal="Lei 8.666/1993, art. 87, IV; Lei 14.133/2021, art. 156, IV; "
                       "Lei 12.846/2013, art. 19, IV.",
            como_verificar="Para cada par, comparar a data de assinatura do contrato "
                           "com o início da sanção; pedir ao órgão a consulta ao "
                           "CEIS feita à época (art. 91, §4º, Lei 14.133).",
            metricas={"fornecedores": int(nac["cnpj"].nunique()),
                      "pagamentos": int(len(nac)), "valor": round(v, 2)},
        ))
    if not esc.empty:
        v = float(pd.to_numeric(esc["VALOR"], errors="coerce").fillna(0).sum())
        achados.append(Finding(
            id="fornecedores-sancionados-outro-orgao", frente=FRENTE, gravidade="info",
            titulo=(f"{esc['cnpj'].nunique()} fornecedor(es) pagos ({fmt_brl(v)}) tinham "
                    "impedimento ou suspensão aplicados por OUTRO órgão"),
            descricao=(
                "Contexto, **não achado**: impedimento e suspensão (Lei 8.666, art. "
                "87, III; Lei 14.133, art. 156, III) valem para o ente que os "
                "aplicou. Um impedimento de um município não proíbe o Crea-SP de "
                "contratar a empresa. A lista existe porque quem cruzar as bases "
                "sem ler a categoria da sanção vai publicar isto como "
                "irregularidade — e não é."),
            evidencia=esc[cols].sort_values("VALOR", ascending=False).head(100),
            metricas={"fornecedores": int(esc["cnpj"].nunique()), "valor": round(v, 2)},
        ))
    if not achados:
        achados.append(Finding(
            id="fornecedores-sancionados", frente=FRENTE, gravidade="info",
            titulo=(f"{j['cnpj'].nunique()} fornecedor(es) pagos constam no CEIS/CNEP, "
                    "mas nenhum pagamento caiu dentro do período da sanção"),
            descricao="Sanção anterior ou posterior aos pagamentos. Sem sobreposição "
                      "de datas não há o que apontar.",
            evidencia=j[cols].sort_values("DATA").head(100),
        ))
    return achados


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    fichas = fichas_cnpj(staged)
    if not fichas:
        achados.append(Finding(
            id="fornecedores-sem-fichas", frente=FRENTE, gravidade="info",
            titulo="Nenhuma ficha de CNPJ consultada",
            descricao="APIs de CNPJ indisponíveis ou nenhum fornecedor acima do piso.",
        ))
        return achados

    primeiro_pg = _primeiro_pagamento(staged)
    situacoes, jovens = [], []
    for cnpj, ficha in fichas.items():
        razao = ficha.get("razao_social") or ""
        situacao = situacao_cadastral(ficha)
        abertura = parse_data(ficha.get("data_inicio_atividade")
                              or ficha.get("data_inicio_atividades"))
        cnae = ficha.get("cnae_fiscal_descricao") or ""
        registro = {"cnpj": cnpj, "razao_social": razao, "situacao": situacao,
                    "abertura": abertura, "cnae": cnae}
        if situacao and "ATIVA" not in situacao:
            situacoes.append(registro)
        primeira = primeiro_pg.get(cnpj)
        if primeira is not None and pd.notna(abertura):
            idade = (primeira - abertura).days
            if 0 <= idade < config.IDADE_MINIMA_EMPRESA_DIAS:
                jovens.append({**registro, "primeiro_pagamento": primeira,
                               "idade_dias_no_1o_pagamento": idade})

    if situacoes:
        ev = pd.DataFrame(situacoes)
        ultimo = _ultimo_pagamento(staged)
        ev["ultimo_pagamento"] = ev["cnpj"].map(ultimo)
        ev["anos_desde_ultimo_pagamento"] = (
            (pd.Timestamp.today() - pd.to_datetime(ev["ultimo_pagamento"],
                                                   errors="coerce")).dt.days / 365
        ).round(1)
        recentes = ev[ev["anos_desde_ultimo_pagamento"] <= 2]
        achados.append(Finding(
            id="fornecedores-situacao-irregular", frente=FRENTE,
            gravidade="alta" if not recentes.empty else "media",
            titulo=(f"{len(recentes)} fornecedor(es) com situação cadastral "
                    "não-ATIVA e pagamento nos últimos 2 anos"
                    if not recentes.empty else
                    f"{len(ev)} fornecedor(es) hoje não-ATIVOS na Receita "
                    "(pagamentos antigos)"),
            descricao=(
                "A ficha da Receita é a de HOJE; o pagamento pode ser de anos "
                "atrás, quando a empresa estava regular. A coluna "
                "`anos_desde_ultimo_pagamento` separa os dois casos: baixa "
                "recente com pagamento recente é red flag; empresa que encerrou "
                "atividade anos depois do último pagamento não é."),
            evidencia=ev.sort_values("anos_desde_ultimo_pagamento"),
            como_verificar="Consultar a data de baixa/inaptidão na Receita e "
                           "compará-la com a data de cada pagamento.",
        ))
    if jovens:
        achados.append(Finding(
            id="fornecedores-empresa-recente", frente=FRENTE, gravidade="alta",
            titulo=f"{len(jovens)} fornecedor(es) receberam o 1º pagamento com menos de "
                   f"{config.IDADE_MINIMA_EMPRESA_DIAS} dias de existência",
            descricao="Empresa criada às vésperas da contratação é padrão típico de "
                      "empresa de fachada/direcionamento.",
            evidencia=pd.DataFrame(jovens),
        ))

    # ------------------------------------------------ sanções (CEIS/CNEP, CGU)
    achados.extend(_achados_sancoes(staged))

    # ---------------------------------------------- geografia, porte e CNAE
    perfil = _perfil_fichas(fichas)
    pagamentos = _pagamentos_pj(staged)
    base = (pagamentos.merge(perfil, on="cnpj", how="left")
            if not pagamentos.empty else pd.DataFrame())
    if not base.empty:
        achados.extend(_achados_geografia(staged, base, perfil))
        achados.extend(_achado_porte(base, perfil))
    else:
        achados.append(Finding(
            id="fornecedores-geografia-indisponivel", frente=FRENTE,
            gravidade="info",
            titulo="Análise geográfica e de porte INDISPONÍVEL: sem base de "
                   "pagamentos a pessoa jurídica",
            descricao=(
                "Há fichas cadastrais, mas não há pagamentos a pessoa jurídica "
                "com CNPJ legível no período coletado — não existe valor para "
                "distribuir no território nem por porte de empresa. Nada é "
                "estimado aqui."),
            metricas={"fornecedores_com_ficha": int(len(fichas))},
        ))
    achados.extend(_achado_cnae(staged, fichas, perfil))

    # ------------------------------------------------------------- universo
    resumo = perfil.copy()
    if not pagamentos.empty:
        recebido = pagamentos.groupby("cnpj")["valor"].sum()
        resumo["recebido_no_periodo"] = resumo["cnpj"].map(recebido).fillna(0.0)
        resumo = resumo.sort_values("recebido_no_periodo", ascending=False)
        total_pj = float(pagamentos["valor"].sum())
        cobertura = (100 * float(resumo["recebido_no_periodo"].sum()) / total_pj
                     if total_pj else 0.0)
    else:
        total_pj, cobertura = 0.0, 0.0
    achados.append(Finding(
        id="fornecedores-fichas", frente=FRENTE, gravidade="info",
        titulo=(f"Fichas cadastrais de {len(fichas)} CNPJ(s), cobrindo "
                f"{cobertura:.0f}% do valor pago a pessoa jurídica de mercado"),
        descricao=(
            "O universo desta frente inteira. A consulta cobre quem recebeu "
            f"acima de {fmt_brl(config.PISO_ANALISE_FORNECEDOR)}, limitada às "
            f"{config.MAX_CONSULTAS_CNPJ} maiores — não é amostra aleatória e "
            "não representa o conjunto dos fornecedores: representa o dinheiro. "
            "Todo percentual dos achados desta frente tem este denominador, e "
            "não o total pago pelo conselho.\n\n"
            "**O denominador, exatamente.** 'Pessoa jurídica de mercado' é o "
            "pagamento a PJ com CNPJ legível, já fora a folha lançada em "
            "rótulo agregado (que não tem fornecedor) e o repasse obrigatório "
            "ao Confea e à Mútua (que é determinado em lei e não disputa "
            "licitação). Os achados de geografia e de porte usam recortes "
            "ainda menores — só o valor pago a fornecedor cuja ficha trouxe, "
            "respectivamente, município e porte —, e cada um declara o seu no "
            "próprio título."),
        evidencia=resumo,
        metricas={"fichas": int(len(fichas)),
                  "cobertura_pct": round(cobertura, 1),
                  "valor_pj_total": round(total_pj, 2),
                  "piso": config.PISO_ANALISE_FORNECEDOR,
                  "teto_consultas": config.MAX_CONSULTAS_CNPJ},
    ))
    return achados
