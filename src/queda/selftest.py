"""Selftest offline: valida o pipeline analítico inteiro sem tocar a rede.

Gera fixtures que imitam os schemas reais das APIs (Implanta, Dados Abertos,
PNCP, BrasilAPI), injeta cenários de irregularidade conhecidos e confere que
cada check os detecta e que o report é gerado.
"""
from __future__ import annotations

import random

import pandas as pd

from . import config, report
from .checks import common, run_all
from .utils import eh_pf, norm_nome

random.seed(42)

_CONSELHEIRO = "JOAO DA SILVA PEREIRA"
_COMISSIONADO = "MARIA APARECIDA COSTA"
_EFETIVO = "CARLOS EDUARDO LIMA"
_FORN_FRACIONA = "ALFA SERVICOS LTDA"
_FORN_REDE = "OMEGA ENGENHARIA LTDA"
_CNPJ_REDE = "11222333000181"
_CNPJ_BAIXADO = "44555666000199"


def _pagamentos() -> pd.DataFrame:
    rows = []
    # diárias sistemáticas para conselheiro/comissionado, 2013-2025
    for ano in range(2013, 2026):
        for nome, cpf, n in ((_CONSELHEIRO, "***456789**", 130),
                             (_COMISSIONADO, "***111222**", 60),
                             (_EFETIVO, "***333444**", 5)):
            for i in range(n):
                rows.append({
                    "_ano": ano, "NOME_RAZAO_SOCIAL": nome, "CPF_CNPJ": cpf,
                    "CONTA_EMPENHO": "Diárias no País",
                    "HISTORICO": f"Pagamento de diária evento {i}",
                    "VALOR": round(random.uniform(400, 1200), 2),
                    "VALOR_LIQUIDO": 0,
                    "DATA_PAGAMENTO": f"{(i % 28) + 1:02d}/{(i % 12) + 1:02d}/{ano}",
                    "NUMERO_EMPENHO": i, "NUMERO_PAGAMENTO": i,
                })
    # fracionamento: 5 pagamentos de 15k em 60 dias ao mesmo fornecedor
    for i in range(5):
        rows.append({"_ano": 2024, "NOME_RAZAO_SOCIAL": _FORN_FRACIONA,
                     "CPF_CNPJ": "99888777000155",
                     "CONTA_EMPENHO": "Serviços de Terceiros PJ",
                     "HISTORICO": "manutenção predial",
                     "VALOR": 15000.0, "VALOR_LIQUIDO": 15000.0,
                     "DATA_PAGAMENTO": f"{i * 6 + 1:02d}/03/2024",
                     "NUMERO_EMPENHO": 900 + i, "NUMERO_PAGAMENTO": 900 + i})
    # fornecedor da rede + fornecedor baixado
    for cnpj, nome, v in ((_CNPJ_REDE, _FORN_REDE, 800000.0),
                          (_CNPJ_BAIXADO, "DELTA COMERCIO LTDA", 120000.0)):
        rows.append({"_ano": 2023, "NOME_RAZAO_SOCIAL": nome, "CPF_CNPJ": cnpj,
                     "CONTA_EMPENHO": "Serviços de Terceiros PJ", "HISTORICO": "contrato",
                     "VALOR": v, "VALOR_LIQUIDO": v, "DATA_PAGAMENTO": "10/05/2023",
                     "NUMERO_EMPENHO": 1000, "NUMERO_PAGAMENTO": 1000})
    df = pd.DataFrame(rows)
    df["DATA"] = pd.to_datetime(df["DATA_PAGAMENTO"], dayfirst=True, errors="coerce")
    df["PF"] = df["CPF_CNPJ"].map(eh_pf)
    df["FAVORECIDO_NORM"] = df["NOME_RAZAO_SOCIAL"].map(norm_nome)
    return df


def _staged() -> dict[str, pd.DataFrame]:
    quadro = pd.DataFrame([
        {"nome": _COMISSIONADO, "vinculo": "Cargo em Comissão", "renumeracaoMensal": "12.000,00"},
        {"nome": _EFETIVO, "vinculo": "Efetivo", "renumeracaoMensal": "8.000,00"},
        {"nome": "ANA BEATRIZ SOUZA", "vinculo": "Efetivo", "renumeracaoMensal": "7.500,00"},
    ])
    quadro["remuneracao"] = [12000.0, 8000.0, 7500.0]
    quadro["NOME_NORM"] = quadro["nome"].map(norm_nome)
    quadro["VINCULO_NORM"] = quadro["vinculo"].map(norm_nome)

    mandatarios = pd.DataFrame([{"nomeMandatario": _CONSELHEIRO, "cargo": "Presidente"}])
    mandatarios["NOME_NORM"] = mandatarios["nomeMandatario"].map(norm_nome)

    empenhos = pd.DataFrame([
        {"_ano": 2024, "NOME_CONTA": "Diárias", "PESSOA": _CONSELHEIRO,
         "VALOR": 1000.0, "VALOR_PAGO": 1500.0, "VALOR_ANULADO": 0.0},
        {"_ano": 2024, "NOME_CONTA": "Serviços", "PESSOA": _FORN_FRACIONA,
         "VALOR": 100000.0, "VALOR_PAGO": 60000.0, "VALOR_ANULADO": 40000.0},
    ])

    contratos = pd.DataFrame([
        {"NUMERO_CONTRATO": "CT-01/2022", "CONTRATADA": _FORN_REDE, "VALOR": 500000.0,
         "NUMERO_LICITACAO": None,
         "DATA_INICIO_VIGENCIA": "2022-01-01", "DATA_FINAL_VIGENCIA": "2022-12-31"},
    ])
    contratos["INI"] = pd.to_datetime(contratos["DATA_INICIO_VIGENCIA"])
    contratos["FIM"] = pd.to_datetime(contratos["DATA_FINAL_VIGENCIA"])
    contratos["CONTRATADA_NORM"] = contratos["CONTRATADA"].map(norm_nome)

    aditivos = pd.DataFrame([{"NUMERO_CONTRATO": "CT-01/2022", "VALOR": 200000.0}])

    convenios = pd.DataFrame([
        {"BENEFICIARIO": "ASSOCIACAO DOS ENGENHEIROS X", "VALOR_TOTAL": 90000.0}
        for _ in range(4)])

    comparativo = pd.DataFrame([
        {"_ano": a, "NOME": nome, "ANALITICA": True,
         "REALIZADO_EXERCICIO": base * (1.06 ** (a - 2013))}
        for a in range(2013, 2026)
        for nome, base in (("Vencimentos e Salários", 40e6),
                           ("Diárias no País", 2e6),
                           ("Cargos em Comissão", 6e6))])

    pncp = pd.DataFrame([
        {"orgao_cnpj": "00000000000191", "orgao_papel": "alvo", "orgao_nome": "CREA-SP",
         "fornecedor_ni": _CNPJ_REDE, "fornecedor_nome": _FORN_REDE,
         "objeto": "obra sede", "valorGlobal": 500000.0, "valorAcumulado": 900000.0,
         "numeroContrato": "1/2022", "vigenciaInicio": "2022-01-01",
         "vigenciaFim": "2022-12-31", "anoContrato": 2022, "tipoPessoa": "PJ",
         "dataAssinatura": "2022-01-01"},
        {"orgao_cnpj": "00000000000272", "orgao_papel": "relacionado",
         "orgao_nome": "CONFEA", "fornecedor_ni": _CNPJ_REDE,
         "fornecedor_nome": _FORN_REDE, "objeto": "reforma", "valorGlobal": 300000.0,
         "valorAcumulado": 300000.0, "numeroContrato": "9/2023",
         "vigenciaInicio": "2023-01-01", "vigenciaFim": "2023-12-31",
         "anoContrato": 2023, "tipoPessoa": "PJ", "dataAssinatura": "2023-01-01"},
    ])
    pncp["fornecedor_norm"] = pncp["fornecedor_nome"].map(norm_nome)

    return {
        "pagamentos": _pagamentos(), "empenhos": empenhos, "contratos": contratos,
        "aditivos": aditivos, "licitacoes": pd.DataFrame(), "convenios": convenios,
        "comparativo_despesa": comparativo, "quadro_pessoal": quadro,
        "mandatarios": mandatarios, "indenizacoes_endpoint": pd.DataFrame(),
        "pncp_contratos": pncp, "pncp_cobrancas": pd.DataFrame(),
    }


_FICHAS_FIXTURE = {
    _CNPJ_REDE: {"razao_social": _FORN_REDE, "descricao_situacao_cadastral": "ATIVA",
                 "data_inicio_atividade": "2023-03-01",
                 "cnae_fiscal_descricao": "Obras de engenharia",
                 "qsa": [{"nome_socio": _COMISSIONADO},
                         {"nome_socio": "PEDRO HENRIQUE RAMOS"}]},
    _CNPJ_BAIXADO: {"razao_social": "DELTA COMERCIO LTDA",
                    "descricao_situacao_cadastral": "BAIXADA",
                    "data_inicio_atividade": "2010-01-01",
                    "cnae_fiscal_descricao": "Comércio varejista",
                    "qsa": [{"nome_socio": "PEDRO HENRIQUE RAMOS"}]},
}

ESPERADOS = [
    "diarias-volume-anual",            # >100 diárias/ano
    "diarias-endpoint-vazio",          # endpoint vazio com gasto nos pagamentos
    "pessoal-renda-invisivel",         # indenização >30% do salário
    "pessoal-serie-historica",
    "empenhos-fracionamento",
    "empenhos-pago-maior-que-empenhado",
    "contratos-aditivos-limite",       # 40% de aditivo
    "rede-fornecedores-compartilhados",
    "rede-socio-homonimo",             # sócia = comissionada
    "rede-socios-compartilhados",      # Pedro em 2 empresas
    "fornecedores-situacao-irregular", # DELTA baixada
    "fornecedores-empresa-recente",    # OMEGA aberta em 2023-03, pagamento 2023-05
]


def run() -> int:
    common._FICHAS = dict(_FICHAS_FIXTURE)   # evita rede no selftest
    staged = _staged()
    achados = run_all(staged)
    ids = {f.id for f in achados}
    faltando = [e for e in ESPERADOS if e not in ids]
    report.gerar(achados, staged)

    print(f"selftest: {len(achados)} achados gerados")
    for f in achados:
        print(f"  [{f.gravidade:5}] {f.id}: {f.titulo}")
    if faltando:
        print(f"\nFALHOU — checks esperados ausentes: {faltando}")
        return 1
    html = (config.OUTPUT_DIR / "report.html")
    assert html.exists() and html.stat().st_size > 100_000, "report.html não gerado"
    print(f"\nOK — todos os {len(ESPERADOS)} cenários esperados foram detectados; "
          f"report em {html}")
    return 0
