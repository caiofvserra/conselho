"""Configuração central da ferramenta de fiscalização do Crea-SP."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("QUEDA_ROOT", Path(__file__).resolve().parents[2]))
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
STAGED_DIR = DATA_DIR / "staged"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = ROOT / "output"
ASSETS_DIR = ROOT / "assets"

# ---------------------------------------------------------------- fontes
IMPLANTA_BASE = "https://crea-sp.implanta.net.br/portalTransparencia/api/v1.0"

# O swagger do serviço de Dados Abertos publica apenas `servers: /service-api`.
# O host é descoberto em tempo de execução testando os candidatos abaixo.
DADOS_ABERTOS_CANDIDATES = [
    "https://transparencia.creasp.org.br/service-api",
    "https://portaltransparencia.creasp.org.br/service-api",
    "https://servicos.creasp.org.br/service-api",
    "https://api.creasp.org.br/service-api",
    "https://www.creasp.org.br/service-api",
    "https://creasp.org.br/service-api",
]

PNCP_CONSULTA = "https://pncp.gov.br/api/consulta"
PNCP_API = "https://pncp.gov.br/api/pncp"

CNPJ_APIS = [
    "https://brasilapi.com.br/api/cnpj/v1/{cnpj}",
    "https://minhareceita.org/{cnpj}",
]

# Órgãos do "sistema Confea/Crea" usados na frente de rede de influência.
#
# ATENÇÃO: os 27 CREAs compartilham o prefixo "CONSELHO REGIONAL DE ENGENHARIA
# E AGRONOMIA DO ..." — resolver por prefixo de razão social devolvia o CREA de
# outro estado. Por isso o CNPJ do órgão-alvo é fixado aqui (Receita Federal:
# 60.985.017/0001-77, autarquia federal, ativa) e a busca por nome só é usada
# como último recurso, com verificação de UF.
ORGAO_ALVO_CNPJ = "60985017000177"
ORGAO_ALVO_RAZAO = "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO ESTADO DE SAO PAULO"
ORGAO_ALVO_UF = "SP"

ORGAOS_RELACIONADOS_RAZAO = [
    "CONSELHO FEDERAL DE ENGENHARIA E AGRONOMIA",
    "MUTUA CAIXA DE ASSISTENCIA DOS PROFISSIONAIS DA ENGENHARIA",
    "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO ESTADO DO RIO DE JANEIRO",
    "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DE MINAS GERAIS",
    "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO PARANA",
]

# ---------------------------------------------------------------- período
ANO_ATUAL = 2026
ANO_PISO = 2010          # nunca sondar antes disso
SONDAGEM_VAZIOS = 2      # anos vazios consecutivos encerram a sondagem retroativa

# ---------------------------------------------------------------- rede/anonimato
# User-Agent genérico de navegador: as consultas não devem carregar nenhum
# identificador do operador da ferramenta.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
THROTTLE_SECONDS = 2.5   # intervalo mínimo entre requisições por host (evita WAF)
TIMEOUT = 60
RETRIES = 3              # tentativas para erro de rede / 5xx
# 403/429 = rate-limit por rajada (o servidor serve, mas bloqueia após muitas
# chamadas seguidas). Espera crescente, respeitando Retry-After; nunca cacheado.
RATE_LIMIT_STATUS = (403, 429)
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_BACKOFF_BASE = 20   # segundos: 20, 40, 80, 160, 180 (teto abaixo)
RATE_LIMIT_BACKOFF_MAX = 180

# ---------------------------------------------------------------- thresholds
# Termos que identificam indenizações/diárias/km em contas e históricos
REGEX_INDENIZACAO = (
    r"di[áa]ria|indeniza|quilometr|\bkm\b|ajuda de custo|jeton|passag|"
    r"desloc|reembols|hospedag|aux[íi]lio repres"
)
REGEX_JURIDICO = r"advocat|advog|honor[áa]r|judicial|senten[çc]a|precat[óo]r|per[íi]cia"

TETO_DIARIA_UNITARIA = 900.0     # R$: diária unitária acima disso vira achado
MAX_DIARIAS_ANO = 100            # nº de diárias/ano por pessoa acima disso vira achado

# Jetons (verba de presença de conselheiro): o teto é normativo do Confea e
# muda por resolução. Estes são os parâmetros usados como referência — ambos
# devem ser conferidos contra a resolução vigente no exercício analisado.
MAX_JETONS_MES = 12              # nº de sessões remuneráveis por mês
MAX_JETONS_DIA = 1               # mais de um jeton no mesmo dia = acumulação

# Limite de dispensa de licitação por EXERCÍCIO, para compras e serviços comuns.
# Comparar um pagamento de 2014 com o limite de hoje subestima gravemente o
# fracionamento: em 2014 o limite era R$ 8.000, não R$ 50.000.
#   até 18/06/2018 ..... Lei 8.666/1993, art. 24, II ............ R$  8.000,00
#   a partir de 2018 ... Decreto 9.412/2018 .................... R$ 17.600,00
#   a partir de 2021 ... Lei 14.133/2021, art. 75, II .......... R$ 50.000,00
#   a partir de 2023 ... Decreto 11.317/2022 .................. R$ 59.906,02
# Exercícios posteriores são atualizados por decreto anual: enquanto o valor
# não for confirmado, mantém-se o último conhecido (o relatório publica qual
# limite foi aplicado em cada ano, para que a conferência seja possível).
LIMITES_DISPENSA = {
    2010: 8_000.0, 2011: 8_000.0, 2012: 8_000.0, 2013: 8_000.0,
    2014: 8_000.0, 2015: 8_000.0, 2016: 8_000.0, 2017: 8_000.0,
    2018: 17_600.0, 2019: 17_600.0, 2020: 17_600.0,
    2021: 50_000.0, 2022: 50_000.0,
    2023: 59_906.02,
}
LIMITE_DISPENSA = 50_000.0       # fallback e piso de outras análises


def limite_dispensa(ano) -> float:
    """Limite de dispensa vigente no exercício (R$). Anos posteriores ao
    último confirmado herdam o último valor conhecido."""
    try:
        a = int(ano)
    except (TypeError, ValueError):
        return LIMITE_DISPENSA
    if a in LIMITES_DISPENSA:
        return LIMITES_DISPENSA[a]
    conhecidos = sorted(LIMITES_DISPENSA)
    if a < conhecidos[0]:
        return LIMITES_DISPENSA[conhecidos[0]]
    return LIMITES_DISPENSA[conhecidos[-1]]


JANELA_FRACIONAMENTO_DIAS = 90
ADITIVO_LIMITE_PCT = 25.0        # % acumulado de aditivos sobre o contrato original
PISO_CONTRATO_ADITIVO = 50_000.0 # contratos abaixo disso distorcem o % de aditivo
PISO_ANALISE_FORNECEDOR = 50_000.0   # só consulta CNPJ de quem recebeu acima disso
MAX_CONSULTAS_CNPJ = 150
IDADE_MINIMA_EMPRESA_DIAS = 180  # empresa criada há menos que isso antes do 1º contrato

# Anos de eleição para a presidência do Crea (mandato de 3 anos). Usados para
# testar se repasses e convênios a entidades de classe — que indicam eleitores
# do colégio eleitoral — se concentram em ano de eleição.
# CONFIRA o calendário eleitoral do Confea/Crea antes de usar como prova: o
# teste é sensível a esta lista.
ANOS_ELEITORAIS_CREA = [2011, 2014, 2017, 2020, 2023, 2026]
