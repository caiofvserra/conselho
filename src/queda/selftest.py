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
from .utils import classificar_pessoa, norm_nome, so_digitos

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

    # --- rótulo agregador que esconde o beneficiário (o "DIVERSOS FUNCIONARIOS")
    for ano in range(2013, 2026):
        for conta, v in (("Vencimentos e Salários", 4_000_000.0),
                         ("Diárias no País", 300_000.0),
                         ("Vale-Refeição", 500_000.0)):
            rows.append({"_ano": ano, "NOME_RAZAO_SOCIAL": "DIVERSOS FUNCIONARIOS",
                         "CPF_CNPJ": "", "CONTA_EMPENHO": conta,
                         "HISTORICO": "folha", "VALOR": v, "VALOR_LIQUIDO": v,
                         "DATA_PAGAMENTO": f"20/06/{ano}",
                         "NUMERO_EMPENHO": 5000, "NUMERO_PAGAMENTO": 5000})

    # --- entidade associativa que leva o nome do próprio conselho
    for ano in range(2018, 2026):
        rows.append({"_ano": ano,
                     "NOME_RAZAO_SOCIAL": "ASSOCIACAO DOS PROCURADORES DO CREA-SP",
                     "CPF_CNPJ": "28097358000101",
                     "CONTA_EMPENHO": "Serviços Advocatícios",
                     "HISTORICO": "assessoria juridica", "VALOR": 5_000_000.0,
                     "VALOR_LIQUIDO": 0, "DATA_PAGAMENTO": f"15/03/{ano}",
                     "NUMERO_EMPENHO": 8000 + ano, "NUMERO_PAGAMENTO": 8000 + ano})
    # --- obra da nova sede
    for ano in range(2022, 2026):
        rows.append({"_ano": ano, "NOME_RAZAO_SOCIAL": "CONSTRUTORA NOVA SEDE LTDA",
                     "CPF_CNPJ": "77888999000122",
                     "CONTA_EMPENHO": "Obras e Instalações",
                     "HISTORICO": "medicao obra nova sede", "VALOR": 12_000_000.0,
                     "VALOR_LIQUIDO": 0, "DATA_PAGAMENTO": f"10/08/{ano}",
                     "NUMERO_EMPENHO": 9000 + ano, "NUMERO_PAGAMENTO": 9000 + ano})

    # --- assinaturas forenses ---------------------------------------------
    # (a) valores uniformes: violam Benford
    for i in range(700):
        rows.append({"_ano": 2024, "NOME_RAZAO_SOCIAL": f"FORNECEDOR UNIF {i % 40}",
                     "CPF_CNPJ": f"{10000000000000 + i}",
                     "CONTA_EMPENHO": "Material de Consumo", "HISTORICO": "compra",
                     "VALOR": round(random.uniform(300, 999), 2), "VALOR_LIQUIDO": 0,
                     "DATA_PAGAMENTO": f"{(i % 28) + 1:02d}/{(i % 12) + 1:02d}/2024",
                     "NUMERO_EMPENHO": 2000 + i, "NUMERO_PAGAMENTO": 2000 + i})
    # (b) represamento logo abaixo do limite de dispensa
    for i in range(40):
        rows.append({"_ano": 2023, "NOME_RAZAO_SOCIAL": f"BORDA LTDA {i % 8}",
                     "CPF_CNPJ": f"{20000000000000 + i}",
                     "CONTA_EMPENHO": "Serviços de Terceiros PJ",
                     "HISTORICO": "servico", "VALOR": 47000.0 + i * 50,
                     "VALOR_LIQUIDO": 0, "DATA_PAGAMENTO": f"{(i % 28) + 1:02d}/07/2023",
                     "NUMERO_EMPENHO": 3000 + i, "NUMERO_PAGAMENTO": 3000 + i})
    # (c) corrida de dezembro
    for i in range(120):
        rows.append({"_ano": 2022, "NOME_RAZAO_SOCIAL": f"FIM DE ANO SA {i % 10}",
                     "CPF_CNPJ": f"{30000000000000 + i}",
                     "CONTA_EMPENHO": "Serviços de Terceiros PJ",
                     "HISTORICO": "empenho de dezembro", "VALOR": 90000.0,
                     "VALOR_LIQUIDO": 0, "DATA_PAGAMENTO": f"{(i % 28) + 1:02d}/12/2022",
                     "NUMERO_EMPENHO": 4000 + i, "NUMERO_PAGAMENTO": 4000 + i})
    # (d) estornos
    for i in range(6):
        rows.append({"_ano": 2024, "NOME_RAZAO_SOCIAL": "GAMA SERVICOS LTDA",
                     "CPF_CNPJ": "55666777000188",
                     "CONTA_EMPENHO": "Serviços de Terceiros PJ",
                     "HISTORICO": "estorno", "VALOR": -25000.0, "VALOR_LIQUIDO": 0,
                     "DATA_PAGAMENTO": f"{i + 1:02d}/09/2024",
                     "NUMERO_EMPENHO": 6000 + i, "NUMERO_PAGAMENTO": 6000 + i})
    # (e) mesmo favorecido + mesmo valor, repetido em janela curta
    for i in range(4):
        rows.append({"_ano": 2024, "NOME_RAZAO_SOCIAL": "REPETE LTDA",
                     "CPF_CNPJ": "66777888000199",
                     "CONTA_EMPENHO": "Serviços de Terceiros PJ",
                     "HISTORICO": "parcela", "VALOR": 33333.33, "VALOR_LIQUIDO": 0,
                     "DATA_PAGAMENTO": f"{i * 7 + 1:02d}/04/2024",
                     "NUMERO_EMPENHO": 7000 + i, "NUMERO_PAGAMENTO": 7000 + i})
    df = pd.DataFrame(rows)
    df["DATA"] = pd.to_datetime(df["DATA_PAGAMENTO"], dayfirst=True, errors="coerce")
    # espelha exatamente o que o pipeline de produção grava
    df["TIPO_PESSOA"] = [classificar_pessoa(d, n) for d, n
                         in zip(df["CPF_CNPJ"], df["NOME_RAZAO_SOCIAL"])]
    df["PF"] = df["TIPO_PESSOA"] == "PF"
    df["PJ"] = df["TIPO_PESSOA"] == "PJ"
    df["FAVORECIDO_NORM"] = df["NOME_RAZAO_SOCIAL"].map(norm_nome)
    df["FAVORECIDO_CNPJ"] = df["CPF_CNPJ"].map(so_digitos)
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

    # Valor redondo idêntico repetido entre beneficiárias distintas e volume
    # concentrado em ano de eleição do Crea: as duas assinaturas que a frente
    # de rede tem de separar de repasse legítimo.
    convenios = pd.DataFrame(
        [{"_ano": a, "BENEFICIARIO": "ASSOCIACAO DOS ENGENHEIROS X",
          "VALOR_TOTAL": 90000.0}
         for a in (2019, 2020, 2021, 2022, 2023, 2024)] +
        [{"_ano": a, "BENEFICIARIO": f"SINDICATO DOS TECNICOS {i}",
          "VALOR_TOTAL": 90000.0}
         for a in (2020, 2023) for i in range(1, 6)])

    # VALOR_ORCADO propositalmente subestimado nas diárias: é o padrão que a
    # frente de orçamento tem de flagrar (dotação figurativa, execução muito
    # acima) e que não aparece olhando só a despesa.
    _ORCADO_PCT = {"Vencimentos e Salários": 1.05, "Diárias no País": 0.35,
                   "Cargos em Comissão": 1.02}
    comparativo = pd.DataFrame([
        {"_ano": a, "NOME": nome, "ANALITICA": True,
         "REALIZADO_EXERCICIO": base * (1.06 ** (a - 2013)),
         "VALOR_ORCADO": base * (1.06 ** (a - 2013)) * _ORCADO_PCT[nome]}
        for a in range(2013, 2026)
        for nome, base in (("Vencimentos e Salários", 40e6),
                           ("Diárias no País", 2e6),
                           ("Cargos em Comissão", 6e6))])

    receita = pd.DataFrame([
        {"_ano": a, "NOME": "Anuidades", "ANALITICA": True,
         "REALIZADO_EXERCICIO": 120e6 * (1.05 ** (a - 2013))}
        for a in range(2013, 2026)])

    balanco = pd.DataFrame([
        {"_ano": a, "DOTACOES": 130e6 * (1.05 ** (a - 2013)),
         "DOTACOES_ATUALIZADAS": 138e6 * (1.05 ** (a - 2013)),
         "RECEITAS_REALIZADAS": 120e6 * (1.05 ** (a - 2013)),
         "DESPESAS_EMPENHADAS": 125e6 * (1.05 ** (a - 2013)),
         "DESPESAS_LIQUIDADAS": 118e6 * (1.05 ** (a - 2013)),
         "DESPESAS_PAGAS": 115e6 * (1.05 ** (a - 2013))}
        for a in range(2013, 2026)])

    # Fonte preferencial da frente "para onde vai o dinheiro": traz o CENTRO DE
    # CUSTO, única evidência do eixo FUNÇÃO (para que unidade se gastou).
    centro_custo = pd.DataFrame([
        {"_ano": a, "NOME_CENTRO_CUSTO": cc, "NOME_CONTA": conta,
         "ANALITICO": True, "VALOR_TOTAL": base * (1.06 ** (a - 2013))}
        for a in range(2013, 2026)
        for cc, conta, base in (
            ("Gabinete da Presidência", "Diárias no País", 3.0e6),
            ("Câmaras Especializadas", "Jetons a Conselheiros", 4.0e6),
            ("Departamento de Fiscalização", "Vencimentos e Salários", 9.0e6),
            ("Departamento de Fiscalização", "Diárias no País", 1.2e6),
            ("Superintendência Administrativa", "Vencimentos e Salários", 22.0e6),
            ("Gerência de Tecnologia da Informação", "Serviços de Terceiros PJ", 8.0e6),
            ("UGI Campinas - Atendimento", "Material de Consumo", 1.5e6),
        )])

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
        "comparativo_despesa": comparativo, "despesas_centro_custo": centro_custo,
        "comparativo_receita": receita, "balanco_orcamentario": balanco,
        "quadro_pessoal": quadro,
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
    # diárias e verbas
    "diarias-serie-anual",
    "diarias-volume-anual",            # >100 pagamentos de verba/ano
    "diarias-endpoint-vazio",          # endpoint vazio com gasto nos pagamentos
    "diarias-top-beneficiarios-ano",   # ranking ANUAL, não do período todo
    # pessoal e finalidade
    "pessoal-renda-invisivel",         # indenização >30% do salário
    "pessoal-serie-historica",
    "finalistica-panorama",            # meio × fim
    "finalistica-categorias",
    "finalistica-matriz",              # natureza × função (dois eixos)
    "finalistica-verbas-politicas",    # diárias/jetons na direção e no plenário
    "finalistica-centros-custo",
    # orçamento
    "orcamento-execucao",
    "orcamento-estouro-conta",         # conta que estoura a dotação todo ano
    "orcamento-verbas-vs-dotacao",     # diárias executadas acima do aprovado
    "orcamento-retorno-da-anuidade",   # quanto da receita volta como fiscalização
    # opacidade
    "opacidade-agregadores",           # "DIVERSOS FUNCIONARIOS"
    "opacidade-decomposicao-conta",    # o que há dentro do agregado
    # execução
    "empenhos-fracionamento",
    "empenhos-pago-maior-que-empenhado",
    "contratos-aditivos-limite",
    # rede
    "rede-fornecedores-compartilhados",
    "rede-socio-homonimo",             # sócia = comissionada
    "rede-socios-compartilhados",      # grupos econômicos distintos
    "rede-grupos-economicos",          # componente conexa fornecedor–sócio
    "rede-intermediadores",            # quem conecta quem recebe
    "rede-convenios-ciclo-eleitoral",  # repasse concentrado em ano de eleição
    "rede-convenios-valores",          # valor redondo idêntico = rateio
    # casos concentrados
    "casos-entidade-da-casa",
    "casos-obras",
    # forense estatística
    "forense-benford",
    "forense-bunching-dispensa",
    "forense-dezembro",
    "forense-estornos",
    "forense-valores-repetidos",
    # fornecedores
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
