"""Frente — Diagnóstico da própria coleta.

Um relatório com poucos achados pode significar duas coisas opostas: o órgão
está em ordem, ou a ferramenta não conseguiu baixar os dados que revelariam o
problema. Sem esta frente as duas hipóteses são indistinguíveis — e a segunda
é a mais provável, porque as APIs do portal caem, mudam de formato de
parâmetro e devolvem 403 por rajada.

Aqui a ferramenta audita a si mesma: o que veio, o que faltou, quais anos
ficaram fora e quais análises estão desligadas por falta de fonte.

A pergunta que faltava — e que esta frente passa a responder — é **por que**
uma fonte veio vazia. "0 linhas" não é diagnóstico: pode ser bloqueio do WAF
(403), endpoint inexistente (404), formato de parâmetro recusado (400),
instabilidade (5xx/rede) ou **200 com lista vazia**. Só o último autoriza dizer
que o órgão não publica o dado, e essa é a única das cinco hipóteses que é
achado de transparência — as outras quatro são limitação nossa. Sem o status
HTTP de cada tentativa (o ledger gravado por `http.salvar_ledger`), as cinco
chegam ao relatório com a mesma cara e nenhuma afirmação sobre a fonte ausente
se sustenta.
"""
from __future__ import annotations

import pandas as pd

from .. import clients
from .. import http as H
from ..findings import Finding
from .common import df

FRENTE = "Diagnóstico da coleta"

# dataset -> (o que ele habilita, gravidade da ausência)
CRITICOS = {
    "pagamentos": ("toda a análise de despesa: verbas, fornecedores, "
                   "fracionamento, forense", "alta"),
    "empenhos": ("execução orçamentária, anulações, restos a pagar", "alta"),
    "contratos": ("aditivos, vigência, cruzamento com o PNCP", "alta"),
    "quadro_pessoal": ("classificação do beneficiário de verba por vínculo "
                       "(comissionado × carreira) e cruzamento salário × "
                       "indenização", "alta"),
    "mandatarios": ("identificação de conselheiros entre os beneficiários de "
                    "verbas e nos quadros societários", "alta"),
    "despesas_centro_custo": ("eixo de FUNÇÃO: separar fiscalização de "
                              "administração e direção superior", "alta"),
    "comparativo_despesa": ("série histórica por conta contábil e orçado × "
                            "realizado", "media"),
    "convenios": ("repasses a entidades de classe e teste de ciclo eleitoral",
                  "media"),
    "processos_itens": ("sobrepreço por item e cotação de cobertura", "media"),
    "atas": ("deliberações e presença em sessão (conferência dos jetons)",
             "media"),
    "indenizacoes_endpoint": ("evento, trecho, datas e valor unitário de cada "
                              "diária", "media"),
    "pncp_contratos": ("contratos no PNCP e rede entre órgãos do sistema",
                       "media"),
    "licitacoes": ("vinculação contrato × certame", "baixa"),
    "licitacoes_pncp": ("rol de contratações pós-2023 e modalidade de origem "
                        "de cada contrato (o portal não publica)", "media"),
    "sancoes": ("pagamento a fornecedor inidôneo/impedido na data do pagamento "
                "(CEIS/CNEP)", "media"),
    "benchmark_creas": ("posição do Crea-SP entre os pares por profissional "
                        "ativo (opt-in)", "baixa"),
}


# ------------------------------------------------------- ledger da coleta
def _implanta(*endpoints: str) -> tuple[str, ...]:
    return tuple(f"/api/v1.0/{e}".lower() for e in endpoints)


# Qual chamada HTTP alimenta qual dataset. O casamento é pelo FIM do caminho,
# nunca por substring: ".../api/v1.0/Contratos" e ".../api/v1.0/ContratosAditivos"
# são datasets diferentes, e um `in` faria toda tentativa de aditivo contar
# como tentativa de contrato — o diagnóstico erraria a causa dos dois.
#
# Só entram aqui datasets com endpoint PRÓPRIO. Os derivados de outra chamada
# (pncp_orgaos, implanta_probe_*) não têm linha e o texto do achado diz isso.
ENDPOINTS: dict[str, tuple[str, ...]] = {
    "pagamentos": _implanta("Pagamentos"),
    "empenhos": _implanta("Empenhos"),
    "contratos": _implanta("Contratos"),
    "aditivos": _implanta("ContratosAditivos"),
    "licitacoes": _implanta("Licitacoes"),
    "convenios": _implanta("Convenios"),
    "comparativo_despesa": _implanta("ComparativoDespesa"),
    "comparativo_receita": _implanta("ComparativoReceita"),
    "balanco_orcamentario": _implanta("BalancoOrcamentario"),
    "despesas_centro_custo": _implanta("DespesasCentroCusto"),
    "demonstrativo": _implanta("DemonstrativoEmpenhosPagamentos"),
    "plano_contas": _implanta("PlanoDeContas"),
    "processos_itens": _implanta("ProcessosComprasServicos"),
    "processos_compras": _implanta("ProcessosComprasServicos"),
    "atas": _implanta("AtasColegiados"),
    # dois caminhos por dataset: o portal Implanta e o serviço de Dados
    # Abertos. O vazio só é do órgão se OS DOIS responderem vazio.
    "quadro_pessoal": _implanta("QuadrosPessoas") + (
        "/api/dadosabertos/quadro-colaboradores",),
    "mandatarios": _implanta("Conselheiros") + ("/api/dadosabertos/mandatarios",),
    # os nomes Implanta de diárias entram em `_endpoints()`, lidos do
    # ambiente na chamada (a sessão do Colab define-os depois do import)
    "indenizacoes_endpoint": ("/api/dadosabertos/indenizacoes",),
    "pncp_contratos": ("/api/consulta/v1/contratos",),
    "pncp_atas": ("/api/consulta/v1/atas",),
    "pncp_cobrancas": ("/api/consulta/v1/instrumentoscobranca/inclusao",),
    "licitacoes_pncp": ("/api/consulta/v1/contratacoes/publicacao",),
    "sancoes": ("/download-de-dados/ceis", "/download-de-dados/cnep",
                "/download-de-dados/cepim"),
    # opt-in: vazio por padrão não é falha de coleta, é escolha (ver `desligado`
    # no inventário)
    "benchmark_creas": ("/ComparativoDespesa",),
}

# Os dois vereditos que a tarefa inteira existe para separar.
# "Respondeu vazio" e não "não publica": o 200 [] prova ausência NO ENDPOINT
# CONSULTADO, COM OS PARÂMETROS TESTADOS. Sem o contrato da API (a
# especificação nunca foi lida) não dá para afirmar que não há parâmetro
# obrigatório que estamos deixando de mandar — e "o órgão não publica" é
# exatamente a frase que este relatório não pode dizer sem prova.
NAO_PUBLICA = "endpoint respondeu 200 com lista vazia nos parâmetros testados"
NAO_COLETAMOS = "não conseguimos coletar"
INDETERMINADO = "indeterminado"

# causa -> (veredito, próximo passo concreto). A ordem em que são testadas está
# em `_causa` e é conservadora de propósito: qualquer sinal de falha nossa
# impede o veredito de falta de publicação, porque acusar o órgão de não
# publicar um dado que talvez ele publique é o erro caro deste relatório.
CAUSAS: dict[str, tuple[str, str]] = {
    "resposta recebida e descartada no staging": (
        NAO_COLETAMOS,
        "A API devolveu registros e o dataset ficou vazio mesmo assim: o erro "
        "está no nosso parser (nome de campo, payload agrupado). Conferir a "
        "função stage_* correspondente contra o JSON em data/raw/."),
    "bloqueio 403/429 (WAF ou rate-limit)": (
        NAO_COLETAMOS,
        "Reexecutar `python main.py fetch --retomar` com intervalo maior entre "
        "requisições, de outro IP. Se persistir em todas as tentativas, o "
        "bloqueio a consulta anônima é ele próprio achado de transparência — "
        "mas exige ser afirmado como bloqueio, não como ausência de dado."),
    "parâmetro recusado (4xx determinístico)": (
        NAO_COLETAMOS,
        "Testar os formatos aceitos pelo endpoint (`exercicio`, "
        "`referenciaInicio/Termino`, `dataInicio/Termino`, "
        "`vigenciaInicio/Termino`) — `clients.implanta_variantes` já faz isso "
        "e pode não estar sendo usado nesta etapa."),
    "falha de rede ou do servidor (5xx/timeout)": (
        NAO_COLETAMOS,
        "Instabilidade costuma passar: reexecutar com `--retomar`, que tenta "
        "só o que faltou. Se repetir em todas as rodadas, registrar a "
        "indisponibilidade com data e hora."),
    "servidor respondeu 200 com lista vazia": (
        NAO_PUBLICA,
        "Ler o contrato da API (`python main.py sondar`, ou a página de "
        "documentação Stoplight) para confirmar que o endpoint não exige "
        "parâmetro que não mandamos; depois confirmar o vazio por LAI (e-SIC), "
        "pedindo a base do período em formato aberto."),
    "servidor respondeu 200 com lista vazia; outros formatos deram 404": (
        NAO_PUBLICA,
        "O formato aceito devolveu vazio e os demais nem existem como rota. "
        "Conferir na documentação se falta parâmetro; depois pedir por LAI."),
    "endpoint não identificado (caminho não consta da documentação lida)": (
        INDETERMINADO,
        "Nenhum nome de endpoint válido é conhecido para esta fonte — os nomes "
        "tentados antes deram 404 no prefixo documentado. Copiar da página de "
        "documentação (F12 → Network, filtro `api/v1.0`) o caminho real e "
        "defini-lo em QUEDA_DIARIAS_ENDPOINTS; até lá a fonte fica sem veredito."),
    "endpoint inexistente (404)": (
        INDETERMINADO,
        "404 não distingue 'o órgão não oferece este dado' de 'estamos pedindo "
        "o caminho errado'. Conferir o caminho no swagger do portal e, se ele "
        "não existir mesmo, pedir o dado por LAI."),
    "etapa retomada do disco (nenhuma requisição nesta rodada)": (
        INDETERMINADO,
        "Rodar `python main.py fetch` SEM `--retomar` para que a fonte seja "
        "efetivamente consultada e o status apareça no ledger."),
    "sem registro de tentativa": (
        INDETERMINADO,
        "Nenhuma requisição foi registrada para esta fonte: ou a etapa nem "
        "chegou a pedir (uma etapa anterior falhou, como a descoberta do host "
        "de Dados Abertos), ou o ledger é de uma rodada em que ela não correu."),
    "só houve cache descartado (nenhuma resposta nova)": (
        INDETERMINADO,
        "As únicas linhas registradas são de cache não confiável descartado, "
        "sem resposta nova do servidor. Limpar `data/raw/` para esta fonte e "
        "reexecutar `python main.py fetch`."),
}


def _status_txt(s: pd.Series) -> pd.Series:
    """Status como texto.

    O CSV volta com essa coluna como int, float ou object conforme o que a
    rodada produziu (nome de exceção junto de código numérico faz virar
    object; uma linha sem status faz virar float). Sem normalizar, "403" e
    "403.0" seriam contados como dois status diferentes.
    """
    def um(v):
        if v is None or (isinstance(v, float) and v != v):
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()
    return s.map(um)


def _endpoints() -> dict[str, tuple[str, ...]]:
    """ENDPOINTS com os nomes de diárias lidos do ambiente NA CHAMADA: o
    diagnóstico tem de enxergar os mesmos caminhos que a coleta tentou."""
    out = dict(ENDPOINTS)
    extra = _implanta(*clients.diarias_probe())
    if extra:
        out["indenizacoes_endpoint"] = out["indenizacoes_endpoint"] + extra
    return out


def _tentativas_por_dataset(led: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Atribui cada tentativa registrada ao dataset que ela alimentava."""
    caminho = (led["caminho"].fillna("").astype(str).str.lower()
               .str.split("?").str[0].str.rstrip("/"))
    out: dict[str, pd.DataFrame] = {}
    for dataset, marcadores in _endpoints().items():
        mask = pd.Series(False, index=led.index)
        for m in marcadores:
            mask |= caminho.str.endswith(m)
        if mask.any():
            out[dataset] = led[mask]
    return out


def _sem_superados(sub: pd.DataFrame) -> pd.DataFrame:
    """Descarta 403/429 que foram SUPERADOS por um 2xx posterior ao mesmo
    (caminho, params): rate-limit retentado com sucesso não é bloqueio do
    dado. Na rodada 10, dois 403 de rajada seguidos de 200 [] fizeram
    `mandatarios` sair como "bloqueio" em vez de "respondeu vazio"."""
    if sub is None or sub.empty or "resultado" not in sub.columns:
        return sub
    d = sub.copy()
    d["_ok"] = _status_txt(d["status"]).str.startswith("2")
    d["_chave"] = (d["caminho"].fillna("").astype(str) + "?" +
                   (d["params"].fillna("").astype(str) if "params" in d.columns else ""))
    if "ts" in d.columns:
        d = d.sort_values("ts", kind="stable")
    superados: list = []
    for _, grupo in d.groupby("_chave", sort=False):
        oks = [i for i, ok in zip(grupo.index, grupo["_ok"]) if ok]
        if not oks:
            continue
        pos = list(grupo.index).index(oks[-1])
        for i in list(grupo.index)[:pos]:
            if str(grupo.at[i, "resultado"]) == H.R_BLOQUEIO:
                superados.append(i)
    return sub.drop(index=superados) if superados else sub


def _causa(sub: pd.DataFrame, retomado: bool, tem_caminho: bool = True) -> str:
    """Causa provável do vazio, a partir dos status observados.

    A ordem dos testes é a regra de prova: primeiro o que denuncia erro NOSSO,
    e só no fim o 200-vazio que autoriza dizer que o órgão não publica. Um
    dataset com 200-vazio em dezesseis exercícios e um 403 numa retentativa
    sai como "bloqueio" — subafirmar é o custo aceitável; afirmar falta de
    publicação onde houve bloqueio, não.
    """
    if sub is None or sub.empty:
        if not tem_caminho:
            return "endpoint não identificado (caminho não consta da documentação lida)"
        return ("etapa retomada do disco (nenhuma requisição nesta rodada)"
                if retomado else "sem registro de tentativa")
    sub = _sem_superados(sub)
    res = set(sub["resultado"].fillna("").astype(str))
    if pd.to_numeric(sub["itens"], errors="coerce").fillna(0).max() > 0:
        return "resposta recebida e descartada no staging"
    if H.R_BLOQUEIO in res:
        return "bloqueio 403/429 (WAF ou rate-limit)"
    if H.R_RECUSADO in res:
        return "parâmetro recusado (4xx determinístico)"
    if H.R_ERRO_SERVIDOR in res or H.R_ERRO_REDE in res or H.R_SEM_JSON in res:
        return "falha de rede ou do servidor (5xx/timeout)"
    if H.R_VAZIO in res or H.R_OK in res:
        # R_OK aqui só acontece com payload escalar (sem lista para contar);
        # em ambos os casos o host respondeu e não entregou registro nenhum.
        if H.R_INEXISTENTE in res:
            return "servidor respondeu 200 com lista vazia; outros formatos deram 404"
        return "servidor respondeu 200 com lista vazia"
    if H.R_INEXISTENTE in res:
        return "endpoint inexistente (404)"
    # Sobra só o cache descartado: dizer "sem registro de tentativa" com a
    # coluna `tentativas` mostrando 3 seria contradizer a própria evidência.
    return "só houve cache descartado (nenhuma resposta nova)"


def _resumo_status(sub: pd.DataFrame, limite: int = 5) -> str:
    """"403 bloqueio ×12; 200 vazio ×3" — a evidência crua atrás do veredito."""
    if sub is None or sub.empty:
        return ""
    d = pd.DataFrame({"status": _status_txt(sub["status"]),
                      "resultado": sub["resultado"].fillna("").astype(str)})
    contagem = d.groupby(["status", "resultado"]).size().sort_values(ascending=False)
    partes = [f"{st or 's/status'} {res} ×{n}" for (st, res), n in contagem.items()]
    extra = len(partes) - limite
    return "; ".join(partes[:limite]) + (f"; +{extra}" if extra > 0 else "")


def _porque_vazio(staged, diag: pd.DataFrame, vazios: list[str]) -> list[Finding]:
    """Para cada dataset vazio, a causa provável a partir do status HTTP."""
    led = df(staged, "_coleta_http")
    # Só os vazios que têm endpoint próprio: um dataset derivado de outra
    # chamada não tem tentativa para atribuir, e listá-lo como "sem registro"
    # seria transformar a nossa modelagem em achado sobre o órgão.
    alvos = [d for d in vazios if d in ENDPOINTS]
    if not alvos:
        return []

    if led.empty or not {"caminho", "resultado", "status", "itens"} <= set(led.columns):
        # Regra da casa: fonte ausente não vira estimativa. Sem o ledger a
        # análise simplesmente não existe, e o relatório tem de dizer isso.
        return [Finding(
            id="diagnostico-porque-vazio-indisponivel", frente=FRENTE,
            gravidade="media",
            titulo=(f"Não é possível dizer por que {len(alvos)} fonte(s) vieram "
                    "vazias: o status HTTP da coleta não foi registrado"),
            descricao=(
                "Um dataset vazio pode ser bloqueio (403), endpoint inexistente "
                "(404), parâmetro recusado (400), instabilidade (5xx) ou **200 "
                "com lista vazia** — e só o último significa que o órgão não "
                "publica o dado. Esta rodada não gravou `_coleta_http.csv`, "
                "então nenhuma dessas hipóteses pode ser afirmada nem "
                "descartada. **Não trate as fontes abaixo como ausência de "
                "publicação**: o que se sabe delas é apenas que este relatório "
                "não as tem."),
            evidencia=pd.DataFrame({"fonte_vazia": alvos}),
            como_verificar=("Reexecutar a coleta com a versão que grava o "
                            "ledger (`python main.py fetch`) e reler este "
                            "achado: ele passa a apontar a causa de cada "
                            "vazio."),
            metricas={"fontes_vazias_sem_causa": len(alvos)},
        )]

    por_dataset = _tentativas_por_dataset(led)
    retomados = set()
    if "retomado" in diag.columns:
        # Comparação por TEXTO: lido do CSV esse campo vem como "True"/"" e um
        # `astype(bool)` sobre a string "False" devolveria True — a etapa
        # retomada seria confundida com etapa consultada.
        marcado = diag["retomado"].astype(str).str.strip().str.lower()
        retomados = set(diag.loc[marcado.isin(("true", "1", "sim")), "dataset"])

    linhas = []
    for dataset in alvos:
        sub = por_dataset.get(dataset)
        # Fonte cuja lista de caminhos ficou só com o serviço legado (nenhum
        # nome de endpoint Implanta conhecido): sem caminho, não há o que
        # tentar — e o veredito tem de dizer isso, não "bloqueio".
        tem_caminho = not (dataset == "indenizacoes_endpoint" and not clients.diarias_probe())
        causa = _causa(sub, dataset in retomados, tem_caminho)
        veredito, o_que_fazer = CAUSAS[causa]
        analise, _grav = CRITICOS.get(dataset, ("—", "baixa"))
        itens = pd.to_numeric(sub["itens"], errors="coerce") if sub is not None else None
        linhas.append({
            "fonte_vazia": dataset,
            "veredito": veredito,
            "causa_provavel": causa,
            "tentativas": 0 if sub is None else int(len(sub)),
            "status_observados": _resumo_status(sub),
            "itens_recebidos": (0 if itens is None or itens.dropna().empty
                                else int(itens.fillna(0).max())),
            "analise_desligada": analise,
            "o_que_fazer": o_que_fazer,
        })
    ev = pd.DataFrame(linhas)

    n_orgao = int((ev["veredito"] == NAO_PUBLICA).sum())
    n_nosso = int((ev["veredito"] == NAO_COLETAMOS).sum())
    n_ind = int((ev["veredito"] == INDETERMINADO).sum())
    essenciais_sem_publicacao = [
        r["fonte_vazia"] for _, r in ev.iterrows()
        if r["veredito"] == NAO_PUBLICA and CRITICOS.get(r["fonte_vazia"], ("", ""))[1] == "alta"]
    perdemos_dado = bool((ev["causa_provavel"] ==
                          "resposta recebida e descartada no staging").any())

    # O título é exaustivo de propósito: dizer só o total de vazios repetiria o
    # achado de cobertura, e dizer só a parte de transparência esconderia
    # quanto do vazio é responsabilidade da própria ferramenta.
    partes = []
    if n_orgao:
        partes.append(f"{n_orgao} responderam 200 com lista vazia nos parâmetros testados")
    if n_nosso:
        partes.append(f"{n_nosso} por falha da nossa coleta")
    if n_ind:
        partes.append(f"{n_ind} sem caminho identificado ou sem causa determinável")
    titulo = f"Por que {len(ev)} fonte(s) vieram vazias: " + "; ".join(partes)

    ordem = {NAO_PUBLICA: 0, NAO_COLETAMOS: 1, INDETERMINADO: 2}
    return [Finding(
        id="diagnostico-porque-vazio", frente=FRENTE,
        gravidade=("alta" if (essenciais_sem_publicacao or perdemos_dado)
                   else "media"),
        titulo=titulo,
        descricao=(
            "Cada fonte vazia recebe aqui a causa provável, lida do status HTTP "
            "de todas as tentativas da coleta (arquivo `_coleta_http.csv`). A "
            "distinção que a coluna `veredito` faz é a única coisa que importa "
            "nesta tabela:\n\n"
            "* **endpoint respondeu 200 com lista vazia nos parâmetros testados** — "
            "o endpoint existe, aceitou os parâmetros e devolveu nada. É o "
            "sinal mais forte de que o dado não está publicado ali, mas não "
            "prova falta de publicação pelo órgão: sem o contrato da API não se "
            "sabe se há parâmetro obrigatório que não mandamos. Confirmar pela "
            "documentação e por LAI antes de afirmar.\n"
            "* **não conseguimos coletar** — houve 403 (bloqueio de WAF ou "
            "rate-limit), 400 (formato de parâmetro recusado), 5xx/timeout, ou "
            "a resposta veio com registros e o nosso staging os descartou. "
            "Limitação da ferramenta. **Sobre estas fontes o relatório não "
            "afirma nada quanto ao órgão** — nem que publica, nem que deixa de "
            "publicar.\n"
            "* **indeterminado** — 404 (o caminho não existe nesta API, e isso "
            "tanto pode ser dado não oferecido quanto caminho errado nosso) ou "
            "nenhuma tentativa registrada.\n\n"
            "Limites honestos desta leitura: (a) o 200-vazio prova ausência **no "
            "endpoint consultado**, não no órgão — o dado pode existir em outro "
            "caminho da API, em PDF ou em sistema próprio; (b) a atribuição "
            "tentativa → fonte é feita pelo caminho da URL, então uma fonte "
            "alimentada por dois endpoints soma as tentativas dos dois; (c) "
            "fontes montadas a partir da chamada de outra fonte não aparecem "
            "nesta tabela, porque não têm requisição própria para atribuir."),
        evidencia=ev.assign(_o=ev["veredito"].map(ordem)).sort_values(
            ["_o", "tentativas"], ascending=[True, False]).drop(columns="_o"),
        base_legal=("Lei 12.527/2011 (LAI), art. 8º, caput e §1º (dever de "
                    "divulgação independente de pedido) e §3º, II e III "
                    "(formato aberto e legível por máquina); Decreto "
                    "8.777/2016 (Política de Dados Abertos)."),
        como_verificar=(
            "Reproduzir a requisição registrada: cada linha de "
            "`data/staged/_coleta_http.csv` traz host, caminho e os parâmetros "
            "usados — `curl -s -o /dev/null -w '%{http_code}\\n' "
            "'<host><caminho>?<params>'` devolve o mesmo status fora da "
            "ferramenta. Para as fontes que responderam vazio, "
            "protocolar pedido pelo e-SIC citando o endpoint e a data da "
            "consulta."),
        metricas={"fontes_vazias": int(len(ev)),
                  "sem_publicacao": n_orgao,       # nome antigo, mantido
                  "responderam_vazio": n_orgao,
                  "falha_da_coleta": n_nosso,
                  "indeterminadas": n_ind,
                  "tentativas_registradas": int(len(led)),
                  "essenciais_sem_publicacao": len(essenciais_sem_publicacao)},
    )]


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    diag = df(staged, "_coleta_diagnostico")

    if diag.empty:
        # análise rodando sobre staged carregado do disco sem o inventário
        diag = pd.DataFrame([
            {"dataset": k, "linhas": 0 if v is None else int(len(v)),
             "colunas": 0 if v is None or v.empty else int(v.shape[1]),
             "ano_min": None, "ano_max": None, "anos_cobertos": 0,
             "anos_faltando": ""}
            for k, v in sorted(staged.items()) if not k.startswith("_")])
        for i, r in diag.iterrows():
            d = staged.get(r["dataset"])
            if d is not None and not d.empty and "_ano" in d.columns:
                anos = sorted({int(a) for a in
                               pd.to_numeric(d["_ano"], errors="coerce")
                               .dropna().unique()})
                if anos:
                    diag.loc[i, ["ano_min", "ano_max", "anos_cobertos"]] = (
                        anos[0], anos[-1], len(anos))
    if diag.empty:
        return achados

    # ---- etapas que a rodada NUNCA ALCANÇOU
    #
    # `diag` só tem linha para etapa que passou por `_salvar` ou foi pulada pelo
    # `--retomar`. A que a rodada não chegou a executar — porque abortou por
    # bloqueio de ritmo antes dela — não aparece como vazia: não aparece. E o
    # título saía "Coleta completa: 3 de 3 datasets com dados" numa rodada de 3
    # de 17 etapas (medido). Como o relatório passou a sair SEMPRE, inclusive
    # depois de um abort, esse silêncio virou o caminho mais provável para um
    # atestado de saúde sobre coleta pela metade.
    from ..pipeline import DATASETS_ESPERADOS
    vistos = set(diag["dataset"].astype(str))
    nunca = [d for d in DATASETS_ESPERADOS if d not in vistos]
    if nunca:
        # De propósito FORA de `diag`: "nunca tentada" e "veio vazia" são
        # categorias diferentes, e é a confusão entre as duas que este módulo
        # existe para desfazer. Misturá-las mandaria cada etapa não alcançada
        # para a análise de "por que veio vazia", que responderia "sem registro
        # de tentativa — indeterminado" para algo que tem causa conhecida: a
        # rodada parou antes.
        achados.append(Finding(
            id="diagnostico-etapas-nao-alcancadas", frente=FRENTE,
            gravidade="alta",
            titulo=(f"{len(nunca)} de {len(DATASETS_ESPERADOS)} etapas da "
                    "coleta não chegaram a ser executadas nesta rodada"),
            descricao=(
                "Estas fontes não vieram vazias — elas **não foram tentadas**. "
                "A rodada parou antes delas, quase sempre por bloqueio de ritmo "
                "do portal (403 em rajada), e o relatório abaixo cobre só o que "
                "já estava em disco.\n\n"
                "A distinção importa para ler qualquer número deste relatório: "
                "uma frente sem achado porque a fonte não foi coletada não é "
                "uma frente sem irregularidade. **Poucos achados numa frente "
                "cuja fonte não foi tentada não é sinal de regularidade.**\n\n"
                "`python main.py faltando` mostra o que ficou, e `python "
                "main.py all --retomar` vai à rede só por isso — as janelas já "
                "gravadas em checkpoint saem do disco."),
            evidencia=pd.DataFrame({"etapa_nao_alcancada": nunca}),
            como_verificar=("Rodar de novo com `--retomar` até `python main.py "
                            "faltando` não listar mais nenhuma etapa indo à "
                            "rede."),
            metricas={"etapas_esperadas": len(DATASETS_ESPERADOS),
                      "etapas_nao_alcancadas": len(nunca),
                      "quais": ", ".join(nunca)},
        ))

    # ---- etapas que falharam com erro (fonte fora do ar, formato mudou)
    if "erro" in diag.columns:
        falhas = diag[diag["erro"].notna() & (diag["erro"].astype(str) != "")]
        if not falhas.empty:
            achados.append(Finding(
                id="diagnostico-etapas-com-erro", frente=FRENTE, gravidade="alta",
                titulo=f"{len(falhas)} etapa(s) da coleta falharam com erro",
                descricao=(
                    "Estas fontes não foram coletadas por erro de rede ou do "
                    "servidor — não por ausência de dado. A coleta seguiu com "
                    "as demais em vez de abortar, mas o que estas fontes "
                    "alimentam está fora deste relatório. Erro de servidor "
                    "(5xx) costuma ser transitório: rodar de novo com "
                    "`--retomar` tenta só o que faltou."),
                evidencia=falhas[["dataset", "linhas", "erro"]],
            ))

    vazios = diag[diag["linhas"] == 0]["dataset"].tolist()
    faltando = [(d, *CRITICOS[d]) for d in vazios if d in CRITICOS]
    graves = [f for f in faltando if f[2] == "alta"]

    achados.append(Finding(
        id="diagnostico-cobertura", frente=FRENTE,
        gravidade=("alta" if graves or nunca else
                   ("media" if faltando else "info")),
        # `nunca` entra no título porque sem ele a frase "Coleta completa" era
        # LITERALMENTE falsa numa rodada abortada: ela contava o que estava no
        # inventário, e o inventário só tem as etapas que a rodada alcançou.
        titulo=(f"{len(graves)} fonte(s) essencial(is) não foram coletadas — "
                "há análises desligadas neste relatório"
                if graves else
                f"{len(nunca)} etapa(s) não chegaram a ser executadas — a "
                "cobertura deste relatório é parcial" if nunca else
                f"Coleta completa: {len(diag[diag['linhas'] > 0])} de "
                f"{len(diag)} datasets com dados"),
        descricao=(
            "Inventário do que a coleta trouxe. **Poucos achados numa frente "
            "cuja fonte veio vazia não é sinal de regularidade — é ausência de "
            "dado.** A tabela mostra, por dataset, quantas linhas vieram e que "
            "intervalo de exercícios foi coberto; a lista de fontes ausentes "
            "diz exatamente qual análise ficou desligada. Fontes vazias são "
            "quase sempre 403 por rajada (rode de novo, o cache preserva o que "
            "já veio) ou mudança no formato de parâmetro do endpoint."),
        evidencia=diag.sort_values("linhas", ascending=False),
        metricas={"datasets_vazios": vazios,
                  "essenciais_ausentes": [f[0] for f in graves],
                  "etapas_nao_alcancadas": nunca},
    ))

    if faltando:
        achados.append(Finding(
            id="diagnostico-analises-desligadas", frente=FRENTE,
            gravidade="alta" if graves else "media",
            titulo=f"{len(faltando)} análise(s) indisponíveis por falta de fonte",
            descricao=(
                "O que este relatório **não** está medindo, e por quê. Cada "
                "linha é uma pergunta que os dados coletados não respondem — "
                "trate como lacuna a cobrir, não como ausência de problema."),
            evidencia=pd.DataFrame(
                [{"fonte_ausente": d, "analise_desligada": o, "impacto": g}
                 for d, o, g in faltando]),
            como_verificar="Rodar `python main.py fetch` de novo (o cache em "
                           "disco preserva o que já veio, então a nova rodada "
                           "só tenta o que faltou). Se persistir, pedir a base "
                           "por LAI — a indisponibilidade da API é, ela "
                           "própria, achado de transparência.",
            base_legal="Lei 12.527/2011 (LAI), art. 8º, §3º, II e III "
                       "(divulgação em formato aberto e legível por máquina); "
                       "Decreto 8.777/2016.",
        ))

    # ---- por que cada fonte veio vazia (o status HTTP de cada tentativa)
    achados.extend(_porque_vazio(staged, diag, vazios))

    # ---- cobertura temporal: o pedido do usuário era alcançar o pré-2016
    com_ano = diag[(diag["linhas"] > 0) & diag["ano_min"].notna()]
    if not com_ano.empty:
        curtos = com_ano[pd.to_numeric(com_ano["ano_min"], errors="coerce") > 2016]
        if not curtos.empty:
            achados.append(Finding(
                id="diagnostico-serie-curta", frente=FRENTE, gravidade="media",
                titulo=f"{len(curtos)} dataset(s) não alcançam 2016 — a "
                       "comparação com o período anterior fica prejudicada",
                descricao=(
                    "A sondagem retroativa para quando dois exercícios seguidos "
                    "voltam vazios. Se o portal simplesmente não publica os anos "
                    "antigos, isso é achado de transparência; se parou por "
                    "bloqueio de rajada, uma nova rodada estende a série."),
                evidencia=curtos[["dataset", "ano_min", "ano_max",
                                  "anos_cobertos", "linhas"]],
                base_legal="Lei 12.527/2011, art. 8º; Decreto 7.724/2012, "
                           "art. 7º (histórico das informações).",
            ))
        buracos = com_ano[com_ano["anos_faltando"].fillna("") != ""]
        if not buracos.empty:
            achados.append(Finding(
                id="diagnostico-anos-faltando", frente=FRENTE, gravidade="media",
                titulo=f"{len(buracos)} dataset(s) com exercícios faltando no meio "
                       "da série",
                descricao=(
                    "Buraco no meio da série distorce toda tendência: um ano "
                    "ausente vira 'queda' no gráfico e contamina CAGR, "
                    "sazonalidade e qualquer comparação ano a ano. Confira "
                    "estes exercícios antes de interpretar as curvas."),
                evidencia=buracos[["dataset", "ano_min", "ano_max",
                                   "anos_faltando", "linhas"]],
            ))
    return achados
