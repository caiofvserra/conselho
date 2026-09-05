"""Configuração central da ferramenta de fiscalização do Crea-SP."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("QUEDA_ROOT", Path(__file__).resolve().parents[2]))

# Dados e saída são separáveis do código, e é isso que salva a coleta quando a
# sessão do Colab cai: o clone do repositório é descartável (leva segundos para
# refazer), mas `data/raw` guarda horas de coleta com throttle e `data/cache`
# guarda o que o modelo local já classificou. Apontando QUEDA_DATA_DIR para uma
# pasta do Google Drive, a reconexão RETOMA em vez de recomeçar.
#
#   QUEDA_DATA_DIR=/content/drive/MyDrive/queda/data
#   QUEDA_OUTPUT_DIR=/content/drive/MyDrive/queda/output
DATA_DIR = Path(os.environ.get("QUEDA_DATA_DIR") or (ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
STAGED_DIR = DATA_DIR / "staged"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = Path(os.environ.get("QUEDA_OUTPUT_DIR") or (ROOT / "output"))
ASSETS_DIR = ROOT / "assets"          # vendorizado no repo, nunca no Drive

# Identificador da execução. Cada rodada grava em `output/rodadas/<RUN_ID>/`,
# de modo que corrigir um bug e rodar de novo NÃO apaga o retrato anterior —
# dá para comparar o que mudou. Pode ser fixado por variável de ambiente para
# reproduzir uma rodada específica.
RUN_ID = os.environ.get("QUEDA_RUN_ID") or datetime.now().strftime("%Y-%m-%d_%H%M")


def onde_estao_os_dados() -> str:
    """Uma linha dizendo se a coleta sobrevive a uma queda de sessão."""
    persistente = "drive" in str(DATA_DIR).lower()
    return (f"dados: {DATA_DIR}  |  saída: {OUTPUT_DIR}"
            + ("  (Drive: sobrevive à reconexão)" if persistente else
               "  (efêmero: some se a sessão cair)"))

# ---------------------------------------------------------------- fontes
IMPLANTA_HOST = "https://crea-sp.implanta.net.br"
# Prefixos da API. O primeiro é o que serve Empenhos, Pagamentos e Contratos
# com 200 e dado. O segundo é o DOCUMENTADO: o exemplo cURL da documentação
# do próprio Crea-SP (Stoplight, 04/09/2026) chama
# `https://crea-sp.implanta.net.br/transparencia/api/v1.0/...`. Na rodada de
# 04/09, QuadrosPessoas e Conselheiros devolveram 200 com lista vazia no
# primeiro prefixo, e os cinco caminhos de diárias, 403 — a hipótese é que só
# respondam no documentado, e o WAF devolva 403 para rota inexistente no
# antigo. Um endpoint que vier vazio ou recusado no primeiro é tentado nos
# seguintes, e o prefixo que responder com dado fica aprendido para o resto da
# rodada. O antigo continua na frente porque é o que comprovadamente serve as
# três maiores bases; trocar a ordem custaria uma requisição a mais por janela
# na primeira rodada se o documentado não as servir.
# MEDIDO em 04/09/2026 (`sondar`, exercício 2025): os dois prefixos versionados
# são ESPELHOS — Empenhos devolve os mesmos 12.575 itens nos dois, e
# QuadrosPessoas e Conselheiros devolvem 200 com ZERO itens nos dois. A
# hipótese de que o prefixo documentado destravaria essas fontes caiu; o que
# ficou de pé é o veredito "o órgão não publica por esses endpoints", agora
# com evidência nos dois caminhos. `/transparencia/api` sem versão respondia
# 404 ou 500 para tudo e derrubava etapas inteiras: saiu.
IMPLANTA_PREFIXOS = ["/portalTransparencia/api/v1.0",
                     "/transparencia/api/v1.0"]
IMPLANTA_BASE = IMPLANTA_HOST + IMPLANTA_PREFIXOS[0]

# Nomes de endpoint por dataset, CONFIRMADOS pelo exemplo cURL da documentação
# do próprio Crea-SP (04/09/2026):
#   GET https://crea-sp.implanta.net.br/transparencia/api/v1.0/QuadrosPessoas
#   GET https://crea-sp.implanta.net.br/transparencia/api/v1.0/Conselheiros
# Os dois sem parâmetro de consulta. "Rol de Mandatários" na documentação É o
# endpoint `Conselheiros` — o nome que usávamos estava certo; o que estava
# errado era o PREFIXO. A versão anterior deste dicionário tinha palpites
# (RolMandatarios, Mandatarios...) que custariam ~9 requisições em endpoints
# inexistentes: saíram. Continua sendo lista para o dia em que um conselho
# renomear algo.
ENDPOINT_CANDIDATOS = {
    "mandatarios": ["Conselheiros"],
    "quadro_pessoal": ["QuadrosPessoas"],
}

# Onde a documentação Stoplight do produto mora, para `sondar` extrair o
# `apiDescriptionUrl` (a URL do OpenAPI) do HTML em vez de adivinhar. O host
# do Confea entra porque é o que a documentação pública usa — o CONTRATO é o
# mesmo produto; só o dado é de outro conselho.
DOC_IMPLANTA_CANDIDATOS = [
    "https://crea-sp.implanta.net.br/transparencia/",
    "https://crea-sp.implanta.net.br/transparencia/api-docs",
    "https://confea-br.implanta.net.br/transparencia/",
    "https://confea-br.implanta.net.br/transparencia/api-docs",
]

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
# Em dez rodadas (até 05/09/2026) nenhum dos seis candidatos devolveu JSON:
# 403, timeout de 60 s x4 ou o shell HTML da "Plataforma de Serviços" — ~297 s
# por rodada sem dado. A sondagem só roda quando o operador fixar o host por
# esta variável de ambiente (base do serviço, ex.
# `https://transparencia.creasp.org.br/service-api`); sem ela, a fonte é
# declarada "endpoint não identificado" sem gastar rede.
DADOS_ABERTOS_HOST_ENV = "QUEDA_DADOS_ABERTOS_HOST"


def dados_abertos_host() -> str | None:
    """Base do serviço de Dados Abertos fixada pelo operador, ou None."""
    valor = (os.environ.get(DADOS_ABERTOS_HOST_ENV) or "").strip().rstrip("/")
    return valor or None


# Diárias/indenizações no Implanta: os 7 nomes de endpoint tentados até a
# rodada 10 (DiariasDeslocamentos etc.) responderam 404 no prefixo documentado
# e 403 no antigo — os nomes não existem. Sem nome confirmado pela documentação
# do portal, nenhum é tentado; quem souber o nome real informa aqui, separado
# por vírgula (ex. `QUEDA_DIARIAS_ENDPOINTS=Diarias,Deslocamentos`).
DIARIAS_ENDPOINTS_ENV = "QUEDA_DIARIAS_ENDPOINTS"


def diarias_endpoints() -> list[str]:
    """Nomes de endpoint de diárias informados pelo operador (padrão: nenhum)."""
    bruto = os.environ.get(DIARIAS_ENDPOINTS_ENV) or ""
    return [n.strip().strip("/") for n in bruto.split(",") if n.strip()]


PNCP_CONSULTA = "https://pncp.gov.br/api/consulta"
PNCP_API = "https://pncp.gov.br/api/pncp"
# Atas de registro de preços (consulta pública por vigência). NÃO verificado
# contra a rede: o parâmetro do órgão provavelmente é `cnpj` (nos contratos é
# `cnpjOrgao`) — o cliente tenta os dois e, se nada responder, declara o
# dataset indisponível sem derrubar a coleta. Importa porque 6 dos 11
# "contratos ausentes do PNCP" da rodada 10 eram ATAs, que `/v1/contratos`
# não devolve.
PNCP_ATAS_PATH = "/v1/atas"

# ---------------------------------------------------------------- fontes novas
# Nada abaixo pôde ser testado contra a rede na sessão em que foi escrito (o
# proxy bloqueava os hosts). URLs e nomes de coluna são os publicados pelas
# fontes até onde se sabe; o código resolve colunas por aproximação e declara
# "indisponível" quando não encontra — nunca inventa.

# Contratações (editais) do órgão no PNCP — `/v1/contratacoes/publicacao` EXIGE
# a modalidade. Para um conselho profissional, as que importam: sem as demais
# (leilão, concurso, diálogo competitivo) a varredura cai de 13 para 5
# modalidades × meses. Códigos da Lei 14.133 no PNCP.
PNCP_MODALIDADES_CONSELHO = {4: "Concorrência eletrônica", 6: "Pregão eletrônico",
                             8: "Dispensa", 9: "Inexigibilidade", 12: "Credenciamento"}
# Só faz sentido a partir de quando a Lei 14.133 passou a ser obrigatória
# (30/12/2023): antes, contratação sob a 8.666 não tinha de ir ao PNCP.
LICITACOES_PNCP_DESDE = 2023

# Cadastros de sanção do Portal da Transparência (CGU), em ZIP diário. Não há
# publicação em dia não útil: o cliente recua dia a dia até achar. CEIS =
# inidôneas e suspensas (8.666 art. 87 e 14.133 art. 156); CNEP = punidas pela
# Lei Anticorrupção (12.846).
# CEPIM = entidades privadas sem fins lucrativos impedidas de receber
# transferências (convênios), o cadastro que fala diretamente aos repasses a
# entidades de classe. Diferente de CEIS/CNEP, NÃO tem data de início/fim de
# sanção: colunas típicas CNPJ ENTIDADE; NOME ENTIDADE; NÚMERO CONVÊNIO; ÓRGÃO
# CONCEDENTE; MOTIVO DO IMPEDIMENTO — quem lê o arquivo trata inicio/fim como
# ausentes e usa o motivo como categoria.
SANCOES_CADASTROS = {
    "ceis": "https://portaldatransparencia.gov.br/download-de-dados/ceis/{data}",
    "cnep": "https://portaldatransparencia.gov.br/download-de-dados/cnep/{data}",
    "cepim": "https://portaldatransparencia.gov.br/download-de-dados/cepim/{data}",
}
SANCOES_RECUO_DIAS = 10

# ------------------------------------------------------- TCU (Lei 8.443/1992)
# Quem julga as contas dos conselhos de fiscalização profissional é o TCU, não
# a CGU: o CEIS cobre a sanção administrativa do Executivo, e a inidoneidade
# declarada pelo TRIBUNAL (art. 46) fica de fora dele. São duas perguntas
# distintas sobre o mesmo fornecedor, e só a primeira estava sendo feita.
#
# Os caminhos abaixo são CANDIDATOS: nenhum foi verificado contra a rede na
# sessão em que isto foi escrito (o proxy bloqueia *.gov.br). O cliente tenta
# um a um, guarda o corpo de cada 4xx em data/raw/http-4xx/ e aprende o que
# respondeu. Enquanto nenhum responder, a fonte fica declarada indisponível —
# nunca "não há inidôneos".
# Hosts REAIS do TCU (informados em 05/09/2026). O serviço de dados abertos é
# `dados-abertos.apps.tcu.gov.br`; as certidões da Administração Pública Federal
# ficam em `certidoes-apf.apps.tcu.gov.br`. Os caminhos `ords/condenacao/...`
# continuam como último recurso porque foram PALPITE — se algum existir, a
# sondagem descobre; se não, o corpo do 404 fica em data/raw/http-4xx/.
TCU_CADASTROS: dict[str, tuple[str, ...]] = {
    # empresas: liga por CNPJ, direto no cruzamento de fornecedores
    "tcu_inidoneos": (
        "https://dados-abertos.apps.tcu.gov.br/api/licitante-inidoneo/recupera-licitantes-inidoneos",
        "https://dados-abertos.apps.tcu.gov.br/api/inidoneo/recupera-inidoneos",
        "https://certidoes-apf.apps.tcu.gov.br/api/rest/publico/licitantes-inidoneos",
        "https://contas.tcu.gov.br/ords/condenacao/consulta/inidoneos",
    ),
    # pessoas: liga por CPF do sócio nas fichas cadastrais
    "tcu_inabilitados": (
        "https://dados-abertos.apps.tcu.gov.br/api/inabilitado/recupera-inabilitados",
        "https://certidoes-apf.apps.tcu.gov.br/api/rest/publico/inabilitados",
        "https://contas.tcu.gov.br/ords/condenacao/consulta/inabilitados",
    ),
    "tcu_contas_irregulares": (
        "https://dados-abertos.apps.tcu.gov.br/api/conta-irregular/recupera-contas-irregulares",
        "https://certidoes-apf.apps.tcu.gov.br/api/rest/publico/contas-irregulares",
        "https://contas.tcu.gov.br/ords/condenacao/consulta/contas-irregulares",
    ),
}
# O ORDS pagina com offset; 500 por página é o teto usual.
TCU_PAGINA = 500
TCU_MAX_PAGINAS = 200

# Catálogo das certidões da APF: UMA requisição que diz quais certidões existem
# e com que nome. É por onde se descobre o caminho da consulta por CPF/CNPJ sem
# adivinhar — o resultado fica em data/raw/sondar/ para a rodada seguinte usar.
TCU_TIPOS_CERTIDOES = "https://certidoes-apf.apps.tcu.gov.br/api/rest/publico/tipos-certidoes"

# ------------------------------------------------------ acórdãos do TCU
# Decisão do Tribunal sobre contas — inclusive as dos conselhos de fiscalização
# profissional. É a única fonte que diz o que já foi APURADO sobre o Crea-SP e
# sobre seus fornecedores, por quem tem competência para apurar.
#
# A assinatura documentada é só de paginação (`inicio`, `quantidade`): não há
# busca por texto, então a varredura é sequencial e o filtro é nosso. Por isso
# ela é LIMITADA por rodada e retomável — cada bloco vira checkpoint, e o
# relatório publica quantos acórdãos foram varridos em vez de fingir cobertura
# total. Ausência aqui nunca significa que não exista.
TCU_ACORDAOS_URLS = (
    "https://dados-abertos.apps.tcu.gov.br/api/acordao/recupera-acordaos",
    "http://dados-abertos.apps.tcu.gov.br/api/acordao/recupera-acordaos",
)
TCU_ACORDAOS_QUANTIDADE = int(os.environ.get("QUEDA_TCU_ACORDAOS_PAGINA") or 500)
# Blocos por rodada. 40 × 500 = 20 mil acórdãos por execução, retomando de onde
# parou. `0` = sem limite (varredura completa, cara na primeira vez).
TCU_ACORDAOS_BLOCOS_POR_RODADA = int(
    os.environ.get("QUEDA_TCU_ACORDAOS_BLOCOS") or 40)
# Termos que fazem um acórdão ser guardado. O CNPJ do alvo entra em tempo de
# execução; estes são os nomes pelos quais o conselho aparece no texto.
TCU_ACORDAOS_TERMOS_ALVO = (
    "CREA-SP", "CREA/SP", "CREA SP",
    "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO ESTADO DE SAO PAULO",
)

# Comparação entre Creas: a MESMA API Implanta serve os 27 conselhos e o
# Confea — o host segue o padrão `crea-XX.implanta.net.br` (confirmado para
# crea-sp e confea-br pela documentação pública; os demais são CANDIDATOS que
# `sondar` confere com uma requisição cada). Opt-in: QUEDA_COMPARAR_CREAS=1 ou
# `--comparar-creas`, porque são ~27 hosts × exercícios de requisições.
CREAS_IMPLANTA = {uf: f"crea-{uf.lower()}" for uf in (
    "AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO"
).split()}
CREAS_IMPLANTA["BR"] = "confea-br"
BENCHMARK_EXERCICIOS = 3
BENCHMARK_LIGADO_ENV = "QUEDA_COMPARAR_CREAS"

# Denominador da comparação: profissionais ATIVOS por Crea. Copiado do dataset
# `confea-uf` do projeto engineer-statistics (fonte: SIC do Confea, estoque em
# 2026-08-30, consulta
# https://relatorio.confea.org.br/Profissional/ProfissionaisPorGenero/Buscar).
# 27 UFs, soma 1.217.708 = total publicado pelo Confea. Profissional ≠ título:
# um profissional pode ter mais de um título; aqui são PESSOAS.
PROFISSIONAIS_ATIVOS_FONTE = {"fonte": "Confea/SIC — estoque 2026-08-30",
                              "url": "https://relatorio.confea.org.br/Profissional/ProfissionaisPorGenero/Buscar",
                              "capturado_em": "2026-08-30"}
PROFISSIONAIS_ATIVOS_POR_CREA = {
    "RO": 8048, "AC": 2742, "AM": 16888, "RR": 1795, "PA": 36583, "AP": 1879,
    "TO": 7755, "MA": 17034, "PI": 10580, "CE": 25279, "RN": 15190, "PB": 14137,
    "PE": 28626, "AL": 9382, "SE": 7970, "BA": 54481, "MG": 161214, "ES": 25300,
    "RJ": 119957, "SP": 346824, "PR": 76749, "SC": 50887, "RS": 69604, "MS": 18033,
    "MT": 29253, "GO": 37988, "DF": 23530,
}

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
#
# São dois candidatos porque as fontes públicas divergem sobre qual é o CNPJ
# da autarquia. Nenhum é aceito no escuro: `clients.pncp_resolver_orgao`
# consulta cada um no PNCP e só aceita o que devolver razão social com os
# termos distintivos ("SAO PAULO") e UF conferida. Um CNPJ errado aqui faria
# toda a frente de contratos falar de outro conselho — foi o que aconteceu na
# primeira rodada, quando a busca por razão social devolveu o Crea-PE.
ORGAO_ALVO_CNPJ_CANDIDATOS = ["60985017000177", "60986174000114"]
ORGAO_ALVO_CNPJ = ORGAO_ALVO_CNPJ_CANDIDATOS[0]
ORGAO_ALVO_RAZAO = "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO ESTADO DE SAO PAULO"
ORGAO_ALVO_UF = "SP"

# Órgãos relacionados para a rede entre entes do sistema. Cada um pode trazer
# `cnpj_candidatos` (conferidos contra o PNCP antes do uso — um CNPJ errado aqui
# é DESCARTADO com aviso, nunca aceito no escuro) e `termo_busca` (o texto que
# vai na busca por razão social, quando o nome registrado no PNCP não começa
# como o nosso).
#
# Na rodada de 04/09/2026 só o alvo foi resolvido. A busca do Confea devolveu
# 1 resultado e ele não passou nos termos distintivos; a da Mútua devolveu
# VAZIO, porque o termo era "mutua caixa de assistencia dos profissionais" e a
# razão social no PNCP não é essa. Sem os dois, a "rede entre órgãos" fica
# reduzida ao próprio Crea-SP — e o relatório dizia isso só no CSV.
ORGAOS_RELACIONADOS = [
    {"razao": "CONSELHO FEDERAL DE ENGENHARIA E AGRONOMIA",
     # CNPJ do Confea segundo registros públicos; confirmado em tempo de
     # execução por `_confere_orgao` (razão social + termos distintivos).
     "cnpj_candidatos": ["33665647000191"]},
    {"razao": "MUTUA DE ASSISTENCIA DOS PROFISSIONAIS DA ENGENHARIA ARQUITETURA E AGRONOMIA",
     "termo_busca": "mutua"},
    {"razao": "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO ESTADO DO RIO DE JANEIRO"},
    {"razao": "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DE MINAS GERAIS"},
    {"razao": "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA DO PARANA"},
]
# Compatibilidade: quem só quer os nomes.
ORGAOS_RELACIONADOS_RAZAO = [o["razao"] for o in ORGAOS_RELACIONADOS]

# Onde os portais costumam publicar o contrato da API (Swashbuckle/.NET e
# afins). `main.py sondar` tenta cada um e guarda o que responder. É a
# alternativa a ADIVINHAR nome de endpoint e de parâmetro: na rodada de
# 04/09/2026, QuadrosPessoas e Conselheiros aceitaram os cinco formatos de
# parâmetro que conhecemos e devolveram lista vazia para todos — o que sugere
# um parâmetro que não estamos mandando, e só o contrato diz qual.
# Na rodada de 05/09/2026 o host confea-br respondeu 404 a openapi.json e aos
# dois swagger/v1.0 — e a página de documentação (Stoplight Elements) carrega
# um `config.json` ao abrir (visto no DevTools pelo operador): é ali que a URL
# real do contrato deve estar. Daí os candidatos `config.json`,
# `assets/config.json` e as variantes v1/api-docs/openapi.yaml nos dois hosts.
_SWAGGER_SUFIXOS_NOVOS = [
    "/transparencia/config.json",
    "/transparencia/assets/config.json",
    "/transparencia/swagger.json",
    "/transparencia/swagger/v1/swagger.json",
    "/transparencia/api/v1.0/swagger/v1/swagger.json",
    "/transparencia/api-docs.json",
    "/transparencia/openapi.yaml",
]
SWAGGER_CANDIDATOS_IMPLANTA = [
    # prefixo documentado primeiro — é onde a doc Stoplight vive
    "https://crea-sp.implanta.net.br/transparencia/swagger/v1.0/swagger.json",
    "https://crea-sp.implanta.net.br/transparencia/api/swagger/v1.0/swagger.json",
    "https://crea-sp.implanta.net.br/transparencia/api/v1.0/swagger.json",
    "https://crea-sp.implanta.net.br/transparencia/openapi.json",
    *[IMPLANTA_HOST + s for s in _SWAGGER_SUFIXOS_NOVOS],
    "https://confea-br.implanta.net.br/transparencia/swagger/v1.0/swagger.json",
    "https://confea-br.implanta.net.br/transparencia/api/swagger/v1.0/swagger.json",
    "https://confea-br.implanta.net.br/transparencia/openapi.json",
    *["https://confea-br.implanta.net.br" + s for s in _SWAGGER_SUFIXOS_NOVOS],
    "https://crea-sp.implanta.net.br/portalTransparencia/swagger/v1.0/swagger.json",
    "https://crea-sp.implanta.net.br/portalTransparencia/swagger/v1/swagger.json",
    "https://crea-sp.implanta.net.br/portalTransparencia/api/swagger/v1.0/swagger.json",
    "https://crea-sp.implanta.net.br/portalTransparencia/api/v1.0/swagger.json",
    "https://crea-sp.implanta.net.br/portalTransparencia/swagger/docs/v1.0",
    "https://crea-sp.implanta.net.br/portalTransparencia/swagger/docs/v1",
]
SWAGGER_CANDIDATOS_DADOS_ABERTOS_SUFIXOS = [
    "/swagger/v1/swagger.json", "/swagger/v1.0/swagger.json",
    "/swagger/docs/v1", "/swagger.json", "/api-docs",
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
# Ritmo depois do PRIMEIRO 403/429 de um host na execução (http._throttle).
# Medido em 05/09/2026: a 2,5 s o portal bloqueou 6 vezes na rodada, sempre
# após ~10-14 requisições seguidas, e cada bloqueio custou ~61 s de espera
# (374 s no total). A 6 s (10 req/min) o padrão não se repete; o custo por
# requisição sobe só para o host que já provou bloquear.
THROTTLE_APOS_403 = 6.0
TIMEOUT = 60
RETRIES = 3              # tentativas para erro de rede / 5xx
# 403/429 = rate-limit por rajada (o servidor serve, mas bloqueia após muitas
# chamadas seguidas). Espera crescente, respeitando Retry-After; nunca cacheado.
RATE_LIMIT_STATUS = (403, 429)
RATE_LIMIT_RETRIES = 5
# Nas 6 ocorrências da rodada de 05/09/2026 a 1ª espera de 20 s NUNCA bastou
# e a 2ª (40 s, total ~61 s) sempre bastou: o bloqueio abre em ~1 min. Começar
# em 45 s poupa uma requisição bloqueada por ocorrência.
RATE_LIMIT_BACKOFF_BASE = 45.0   # segundos: 45, 90, 180, 180, 180 (teto abaixo)
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
# Teto de fichas cadastrais por rodada. `None` = sem teto: consulta todo
# fornecedor acima do piso. O teto de 150 cobria 82% do VALOR pago mas só 149
# das ~800 empresas, e a rede de sócios só enxerga o que tem ficha — quatro
# grupos econômicos num universo de 149. O custo é da PRIMEIRA rodada (o
# cache fica em data/raw, no Drive); as seguintes não gastam requisição.
MAX_CONSULTAS_CNPJ = int(os.environ.get("QUEDA_MAX_FICHAS") or 0) or None
IDADE_MINIMA_EMPRESA_DIAS = 180  # empresa criada há menos que isso antes do 1º contrato

# Anos de eleição para a presidência do Crea (mandato de 3 anos). Usados para
# testar se repasses e convênios a entidades de classe — que indicam eleitores
# do colégio eleitoral — se concentram em ano de eleição.
# CONFIRA o calendário eleitoral do Confea/Crea antes de usar como prova: o
# teste é sensível a esta lista.
ANOS_ELEITORAIS_CREA = [2011, 2014, 2017, 2020, 2023, 2026]
