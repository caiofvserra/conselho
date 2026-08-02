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
# CNPJs são resolvidos em runtime via PNCP (busca por razão social) e cacheados.
ORGAO_ALVO_RAZAO = "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO ESTADO DE SAO PAULO"
ORGAOS_RELACIONADOS_RAZAO = [
    "CONSELHO FEDERAL DE ENGENHARIA E AGRONOMIA",
    "MUTUA CAIXA DE ASSISTENCIA DOS PROFISSIONAIS DO CREA",
    "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO RIO DE JANEIRO",
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
LIMITE_DISPENSA = 50_000.0       # piso p/ análise de fracionamento (Lei 14.133 serviços)
JANELA_FRACIONAMENTO_DIAS = 90
ADITIVO_LIMITE_PCT = 25.0        # % acumulado de aditivos sobre o contrato original
PISO_CONTRATO_ADITIVO = 50_000.0 # contratos abaixo disso distorcem o % de aditivo
PISO_ANALISE_FORNECEDOR = 50_000.0   # só consulta CNPJ de quem recebeu acima disso
MAX_CONSULTAS_CNPJ = 150
IDADE_MINIMA_EMPRESA_DIAS = 180  # empresa criada há menos que isso antes do 1º contrato
