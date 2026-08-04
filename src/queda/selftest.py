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
        # `por_mes` concentra os pagamentos do conselheiro em poucos meses: é o
        # padrão de jeton acima do limite mensal que a frente tem de flagrar.
        for nome, cpf, n, por_mes in ((_CONSELHEIRO, "***456789**", 130, 14),
                                      (_COMISSIONADO, "***111222**", 60, 5),
                                      (_EFETIVO, "***333444**", 5, 1)):
            for i in range(n):
                mes = (i // por_mes) % 12 + 1
                rows.append({
                    "_ano": ano, "NOME_RAZAO_SOCIAL": nome, "CPF_CNPJ": cpf,
                    "CONTA_EMPENHO": "Diárias no País",
                    "HISTORICO": f"Pagamento de diária evento {i}",
                    "VALOR": round(random.uniform(400, 1200), 2),
                    "VALOR_LIQUIDO": 0,
                    "DATA_PAGAMENTO": f"{(i % 28) + 1:02d}/{mes:02d}/{ano}",
                    "NUMERO_EMPENHO": i, "NUMERO_PAGAMENTO": i,
                })
    # acumulação no mesmo dia: diária de viagem e jeton de sessão presencial
    # pelo mesmo dia — as duas coisas não acontecem ao mesmo tempo
    for ano in range(2013, 2026):
        for mes in (4, 9):
            rows.append({
                "_ano": ano, "NOME_RAZAO_SOCIAL": _CONSELHEIRO,
                "CPF_CNPJ": "***456789**", "CONTA_EMPENHO": "Jetons a Conselheiros",
                "HISTORICO": "sessao plenaria", "VALOR": 1500.0, "VALOR_LIQUIDO": 0,
                "DATA_PAGAMENTO": f"15/{mes:02d}/{ano}",
                "NUMERO_EMPENHO": 11000, "NUMERO_PAGAMENTO": 11000})
            rows.append({
                "_ano": ano, "NOME_RAZAO_SOCIAL": _CONSELHEIRO,
                "CPF_CNPJ": "***456789**", "CONTA_EMPENHO": "Diárias no País",
                "HISTORICO": "diaria de viagem", "VALOR": 900.0, "VALOR_LIQUIDO": 0,
                "DATA_PAGAMENTO": f"15/{mes:02d}/{ano}",
                "NUMERO_EMPENHO": 11500, "NUMERO_PAGAMENTO": 11500})

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
    # Os nomes de conta abaixo são os REAIS do plano de contas do Crea-SP: é a
    # conta que declara a QUEM a verba se destina quando o favorecido some.
    for ano in range(2013, 2026):
        for conta, v, n in (
                ("6.2.2.1.1.01.01.01.001-Salários", 4_000_000.0, 1),
                ("6.2.2.1.1.01.04.02.001-Auxílio Educação e Creche", 300_000.0, 1),
                # estrutura política: recebe mais que o quadro, em muito mais
                # pagamentos — o padrão que a frente tem de isolar
                ("6.2.2.1.1.01.04.05.002-Diarias com Conselheiros", 5_200_000.0, 90),
                ("6.2.2.1.1.01.04.08.002-Locomoção de Conselheiros", 4_100_000.0, 80),
                ("6.2.2.1.1.01.04.05.005-Pagamento de Jeton", 700_000.0, 17),
                ("6.2.2.1.1.01.04.05.004-Diarias com Inspetores", 2_100_000.0, 21),
                ("6.2.2.1.1.01.04.08.008-Locomoção de Inspetores", 1_400_000.0, 20),
                # quadro contratado
                ("6.2.2.1.1.01.04.05.001-Diarias com Funcionários", 2_600_000.0, 25),
                ("6.2.2.1.1.01.04.08.001-Locomoção de Funcionários", 670_000.0, 13),
                ("6.2.2.1.1.01.04.05.003-Diarias com Colaboradores", 570_000.0, 11),
        ):
            for i in range(n):
                rows.append({"_ano": ano,
                             "NOME_RAZAO_SOCIAL": "DIVERSOS FUNCIONARIOS",
                             "CPF_CNPJ": "", "CONTA_EMPENHO": conta,
                             "HISTORICO": "folha", "VALOR": v / n,
                             "VALOR_LIQUIDO": v / n,
                             "DATA_PAGAMENTO": f"{(i % 28) + 1:02d}/{(i % 12) + 1:02d}/{ano}",
                             "NUMERO_EMPENHO": 5000 + i, "NUMERO_PAGAMENTO": 5000 + i})

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
    # (b) represamento logo abaixo do limite de dispensa VIGENTE NO ANO —
    # em 2015 o teto era R$ 8.000 e em 2023, R$ 59.906,02. Testar os dois
    # exercícios contra o mesmo limite procuraria a fronteira errada.
    for ano, teto, base in ((2015, 8_000.0, 7_300.0), (2023, 59_906.02, 56_000.0)):
        for i in range(40):
            rows.append({"_ano": ano, "NOME_RAZAO_SOCIAL": f"BORDA LTDA {i % 8}",
                         "CPF_CNPJ": f"{20000000000000 + i}",
                         "CONTA_EMPENHO": "Serviços de Terceiros PJ",
                         "HISTORICO": "servico",
                         "VALOR": round(base + i * (teto - base) / 45, 2),
                         "VALOR_LIQUIDO": 0,
                         "DATA_PAGAMENTO": f"{(i % 28) + 1:02d}/07/{ano}",
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
    quadro["lotacao"] = ["Gabinete da Presidência",
                         "Superintendência Administrativa",
                         "Departamento de Fiscalização"]
    quadro["NOME_NORM"] = quadro["nome"].map(norm_nome)
    quadro["VINCULO_NORM"] = quadro["vinculo"].map(norm_nome)
    quadro["LOTACAO_NORM"] = quadro["lotacao"].map(norm_nome)

    # A ENTIDADE que indicou é o elo entre assento e interesse representado.
    # Maioria patronal, e duas das indicantes também recebem repasse — a
    # interseção que a frente de representação tem de isolar.
    mandatarios = pd.DataFrame([
        {"nomeMandatario": _CONSELHEIRO, "cargo": "Presidente",
         "entidade": "SINDUSCON SP"},
        {"nomeMandatario": "CARLOS EDUARDO PRADO", "cargo": "Conselheiro",
         "entidade": "SINDICATO DA INDUSTRIA DA CONSTRUCAO DE CAMPINAS"},
        {"nomeMandatario": "MARIA LUCIA FARIAS", "cargo": "Conselheira",
         "entidade": "ASSOCIACAO COMERCIAL DE SANTOS"},
        {"nomeMandatario": "ROBERTO ALVES LIMA", "cargo": "Conselheiro",
         "entidade": "SINDUSCON SP"},
        {"nomeMandatario": "JOANA PEREIRA DIAS", "cargo": "Conselheira",
         "entidade": "SENGE SP"},
        {"nomeMandatario": "PAULO ROBERTO NEVES", "cargo": "Conselheiro",
         "entidade": "ASSOCIACAO DOS ENGENHEIROS X"},
        {"nomeMandatario": "TEREZA CRISTINA MELO", "cargo": "Conselheira",
         "entidade": "UNIVERSIDADE ESTADUAL PAULISTA"},
    ])
    mandatarios["NOME_NORM"] = mandatarios["nomeMandatario"].map(norm_nome)
    mandatarios["ENTIDADE_NORM"] = mandatarios["entidade"].map(norm_nome)

    # Série anual coerente com o demonstrativo: sem ela a conferência de
    # reconciliação acusaria divergência na própria fixture.
    _emp_base = [
        {"_ano": a, "NOME_CONTA": "Serviços de Terceiros PJ",
         "PESSOA": f"FORNECEDOR REGULAR {i}", "VALOR": 125e6 / 20,
         "VALOR_PAGO": 115e6 / 20, "VALOR_ANULADO": 0.0}
        for a in range(2013, 2026) for i in range(20)]

    empenhos = pd.DataFrame(_emp_base + [
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
         for a in (2020, 2023) for i in range(1, 6)] +
        # patronal com assento no plenário e volume muito maior
        [{"_ano": a, "BENEFICIARIO": "SINDUSCON SP", "VALOR_TOTAL": 800000.0}
         for a in range(2018, 2026)] +
        [{"_ano": a, "BENEFICIARIO": "ASSOCIACAO COMERCIAL DE SANTOS",
          "VALOR_TOTAL": 250000.0} for a in range(2019, 2026)])

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

    # Demonstrativo em ÁRVORE: a sintética repete a soma das filhas. Somar
    # tudo conta o mesmo real duas vezes — o bug que inflou a execução
    # orçamentária em 3-4x na rodada real.
    demonstrativo = pd.DataFrame(
        [{"_ano": a, "NOME": "6.2.2 DESPESA CORRENTE", "_nivel": 0,
          "_folha": False, "ORCADO": 130e6, "EMPENHADOEXERCICIO": 125e6,
          "LIQUIDADO_EXERCICIO": 118e6, "PAGO_EXERCICIO": 115e6}
         for a in range(2013, 2026)] +
        [{"_ano": a, "NOME": f"6.2.2.{i} conta analítica {i}", "_nivel": 1,
          "_folha": True, "ORCADO": 130e6 / 2, "EMPENHADOEXERCICIO": 125e6 / 2,
          "LIQUIDADO_EXERCICIO": 118e6 / 2, "PAGO_EXERCICIO": 115e6 / 2}
         for a in range(2013, 2026) for i in (1, 2)])

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
    # Camadas territoriais de um Crea: direção e administração na sede,
    # superintendências regionais e UGIs no interior. A despesa de
    # fiscalização é deliberadamente pequena e cresce menos que o total —
    # é o padrão que a frente das superintendências tem de flagrar.
    _CENTROS = (
        # (centro de custo, conta, base, crescimento ao ano)
        ("Gabinete da Presidência", "Diárias no País", 3.0e6, 1.09),
        ("Câmaras Especializadas", "Jetons a Conselheiros", 4.0e6, 1.09),
        ("Superintendência Administrativa", "Vencimentos e Salários", 22.0e6, 1.07),
        ("Gerência de Tecnologia da Informação", "Serviços de Terceiros PJ", 8.0e6, 1.08),
        ("Departamento de Fiscalização", "Vencimentos e Salários", 4.0e6, 1.01),
        ("Departamento de Fiscalização", "Diárias no País", 5.0e6, 1.02),
        ("Superintendência Regional de Campinas", "Vencimentos e Salários", 3.0e6, 1.03),
        ("Superintendência Regional de Bauru", "Material de Consumo", 0.6e6, 1.03),
        ("UGI Ribeirão Preto - Atendimento", "Vencimentos e Salários", 1.2e6, 1.03),
        ("UGI Santos - Atendimento", "Locação de Imóveis", 0.9e6, 1.03),
    )
    centro_custo = pd.DataFrame([
        {"_ano": a, "NOME_CENTRO_CUSTO": cc, "NOME_CONTA": conta,
         "ANALITICO": True, "VALOR_TOTAL": base * (cresc ** (a - 2013))}
        for a in range(2013, 2026)
        for cc, conta, base, cresc in _CENTROS])

    atas = pd.DataFrame([
        {"_ano": a, "NUMERO": f"{a}/0{i}", "COLEGIADO": "Plenário",
         "DATA_INICIO": f"1{i}/0{i}/{a}",
         "DELIBERACOES": ("Aprovado, por unanimidade, o repasse à Associação "
                          "dos Engenheiros X, no valor de R$ 90.000,00, "
                          "conforme plano de trabalho apresentado.")}
        for a in (2022, 2023, 2024) for i in (3, 8)])

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
        "demonstrativo": demonstrativo,
        "atas": atas, "quadro_pessoal": quadro,
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
    "diarias-limite-mensal",           # mais de 12 verbas num mês
    "diarias-acumulacao-diaria",       # duas verbas no mesmo dia
    "diarias-politico-vs-quadro",      # a conta declara a quem a verba se destina
    "diarias-sem-beneficiario",        # verba paga a favorecido agregado
    # pessoal e finalidade
    "pessoal-renda-invisivel",         # indenização >30% do salário
    "pessoal-serie-historica",
    "finalistica-panorama",            # meio × fim
    "finalistica-categorias",
    "finalistica-matriz",              # natureza × função (dois eixos)
    "finalistica-verbas-politicas",    # diárias/jetons na direção e no plenário
    "finalistica-centros-custo",
    # superintendências: onde o dinheiro fica × onde ele chega
    "super-central-vs-territorio",
    "super-fiscalizacao-efetiva",
    "super-verba-supera-salario",      # verba > folha na própria fiscalização
    "super-ranking-unidades",
    "super-unidade-sem-fiscalizacao",
    "super-tendencia-divergente",      # total cresce mais que a atividade-fim
    "super-quadro-por-camada",
    # representação: quem ocupa o plenário e para onde vai o repasse
    "repr-composicao",
    "repr-dinheiro-por-interesse",
    "repr-ranking-entidades",
    "repr-assento-e-dinheiro",         # entidade com assento que recebe
    "repr-concentracao",
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
    # atas (LLM opcional: sem chave, avisa em vez de silenciar)
    "atas-llm-desligado",
    # casos concentrados
    "casos-entidade-da-casa",
    "casos-obras",
    # forense estatística
    "forense-benford",
    "forense-benford-2d",              # 90 categorias: valor "arredondado"
    "forense-bunching-dispensa",
    "forense-dezembro",
    "forense-estornos",
    "forense-valores-repetidos",
    # fornecedores
    "fornecedores-situacao-irregular", # DELTA baixada
    "fornecedores-empresa-recente",    # OMEGA aberta em 2023-03, pagamento 2023-05
]


# Alguns achados precisam ser conferidos pelo VALOR, não só pela presença: o
# filtro de entidade coletiva chegou a excluir o SINDUSCON — a maior
# beneficiária com assento — e o cenário continuou "detectado", porque o
# achado disparava com as duas entidades restantes.
def _confere_metricas(achados) -> list[str]:
    por_id = {f.id: f for f in achados}
    erros: list[str] = []

    def exige(fid, chave, esperado, tol=0.01):
        f = por_id.get(fid)
        if f is None:
            return
        obtido = f.metricas.get(chave)
        if obtido is None:
            erros.append(f"{fid}.{chave} ausente")
        elif abs(float(obtido) - esperado) > abs(esperado) * tol:
            erros.append(f"{fid}.{chave} = {obtido}, esperado ~{esperado}")

    # SINDUSCON (800k x 8 anos) + Assoc. Comercial (250k x 7) + Assoc. Eng. (90k x 6)
    exige("repr-assento-e-dinheiro", "valor", 800_000 * 8 + 250_000 * 7 + 90_000 * 6)
    exige("repr-assento-e-dinheiro", "entidades", 3)
    # patronal = SINDUSCON + Assoc. Comercial
    exige("repr-dinheiro-por-interesse", "patronal", 800_000 * 8 + 250_000 * 7)
    # execução tem de somar só as folhas (130e6), não árvore + folhas (260e6)
    f = por_id.get("orcamento-execucao")
    if f is not None and f.serie is not None and not f.serie.empty:
        orcado = float(f.serie["orçado"].iloc[0])
        if abs(orcado - 130e6) > 1e6:
            erros.append(f"orcamento-execucao somou a árvore: orçado={orcado:,.0f}"
                         f" (esperado 130.000.000 = só as folhas)")
    # conselheiros + inspetores + jeton, 13 exercícios
    exige("diarias-politico-vs-quadro", "estrutura_politica",
          (5_200_000 + 4_100_000 + 700_000 + 2_100_000 + 1_400_000) * 13)
    # funcionários + colaboradores
    exige("diarias-politico-vs-quadro", "quadro_contratado",
          (2_600_000 + 670_000 + 570_000) * 13)
    return erros


def run() -> int:
    common._FICHAS = dict(_FICHAS_FIXTURE)   # evita rede no selftest
    staged = _staged()
    achados = run_all(staged)
    ids = {f.id for f in achados}
    faltando = [e for e in ESPERADOS if e not in ids]
    metricas_erradas = _confere_metricas(achados)
    report.gerar(achados, staged)

    print(f"selftest: {len(achados)} achados gerados")
    for f in achados:
        print(f"  [{f.gravidade:5}] {f.id}: {f.titulo}")
    if faltando:
        print(f"\nFALHOU — checks esperados ausentes: {faltando}")
        return 1
    if metricas_erradas:
        print("\nFALHOU — achados presentes mas com valor errado:")
        for e in metricas_erradas:
            print(f"  {e}")
        return 1
    html = (config.OUTPUT_DIR / "report.html")
    assert html.exists() and html.stat().st_size > 100_000, "report.html não gerado"
    print(f"\nOK — todos os {len(ESPERADOS)} cenários esperados foram detectados; "
          f"report em {html}")
    return 0
