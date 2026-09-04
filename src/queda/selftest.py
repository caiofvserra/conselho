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
    # --- transferência LEGAL OBRIGATÓRIA ao Confea e à Mútua
    # Estas não são escolha do conselho (Lei 5.194/1966, arts. 34 e 44) e não
    # podem entrar no repasse discricionário a entidades de classe. Note que a
    # "ASSOCIACAO DOS PROCURADORES DO CREA-SP" acima MENCIONA o Crea e NÃO é
    # órgão do sistema: é a armadilha que já classificou o maior achado do
    # relatório como cumprimento de lei e o fez sumir.
    for ano in range(2013, 2026):
        for quem, cnpj, v in (
                ("CONFEA - CONS. FEDERAL DE ENGENHARIA E AGRONOMIA",
                 "33386261000116", 20_000_000.0),
                ("MUTUA SP - MUTUA ASSIST. PROF. ENG. ARQ. AGRONOMIA",
                 "33386261000205", 8_000_000.0)):
            rows.append({"_ano": ano, "NOME_RAZAO_SOCIAL": quem,
                         "CPF_CNPJ": cnpj,
                         "CONTA_EMPENHO": "6.2.2.1.1.01.08.01-Cota-Parte Confea",
                         "HISTORICO": "cota parte", "VALOR": v,
                         "VALOR_LIQUIDO": 0, "DATA_PAGAMENTO": f"20/01/{ano}",
                         "NUMERO_EMPENHO": 13000, "NUMERO_PAGAMENTO": 13000})
    # --- obra da nova sede
    for ano in range(2022, 2026):
        rows.append({"_ano": ano, "NOME_RAZAO_SOCIAL": "CONSTRUTORA NOVA SEDE LTDA",
                     "CPF_CNPJ": "77888999000122",
                     "CONTA_EMPENHO": "Obras e Instalações",
                     "HISTORICO": "medicao obra nova sede", "VALOR": 12_000_000.0,
                     "VALOR_LIQUIDO": 0, "DATA_PAGAMENTO": f"10/08/{ano}",
                     "NUMERO_EMPENHO": 9000 + ano, "NUMERO_PAGAMENTO": 9000 + ano})

    # --- assinaturas forenses ---------------------------------------------
    # (a) valores uniformes: violam Benford. São 1.100 para que o universo de
    # "preço de mercado" (sem folha nem verba) passe do mínimo de 1.000 do
    # teste de dois dígitos — abaixo disso ele não é aplicável, e o selftest
    # tem de exercitar o caminho em que ele roda.
    for i in range(1100):
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
    # (c) corrida de dezembro — valores VARIADOS: 120 pagamentos idênticos
    # fariam a própria fixture ser reprovada pela guarda de valor tabelado,
    # que é justamente o que o teste de Benford tem de evitar.
    for i in range(120):
        rows.append({"_ano": 2022, "NOME_RAZAO_SOCIAL": f"FIM DE ANO SA {i % 10}",
                     "CPF_CNPJ": f"{30000000000000 + i}",
                     "CONTA_EMPENHO": "Serviços de Terceiros PJ",
                     "HISTORICO": "empenho de dezembro",
                     "VALOR": round(random.uniform(20000, 180000), 2),
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

    # --- família contábil inteira que a nossa taxonomia não alcança
    # Valores DETERMINÍSTICOS de propósito: `random` é semeado uma vez por
    # execução e qualquer chamada nova aqui deslocaria toda a sequência que as
    # fixtures forenses consomem depois.
    for ano in range(2013, 2026):
        for i, (conta, base, cnpj, quem) in enumerate((
                ("6.5.1.01.001-Restituições de Anuidades", 1_500_000.0,
                 "12345678000199", "DIVERSOS PROFISSIONAIS"),
                ("6.5.2.01.001-Custas Processuais", 1_200_000.0,
                 "23456789000188", "TRIBUNAL DE JUSTICA DO ESTADO DE SAO PAULO"),
                ("6.5.2.02.001-Obrigações Tributárias e Contributivas",
                 1_800_000.0, "34567890000177", "SECRETARIA DA RECEITA FEDERAL"))):
            rows.append({
                "_ano": ano, "NOME_RAZAO_SOCIAL": quem, "CPF_CNPJ": cnpj,
                "CONTA_EMPENHO": conta, "HISTORICO": "recolhimento",
                "VALOR": base + 37_000 * ((ano + i * 3) % 7),
                "VALOR_LIQUIDO": 0, "DATA_PAGAMENTO": f"2{i}/06/{ano}",
                "NUMERO_EMPENHO": 12000 + i, "NUMERO_PAGAMENTO": 12000 + i})
    # conta MAIS analítica que o plano coletado: casa pelo prefixo (o pai
    # existe), e é o caso que distingue "conta nova" de "conta inexistente"
    rows.append({"_ano": 2025, "NOME_RAZAO_SOCIAL": "DIVERSOS FUNCIONARIOS",
                 "CPF_CNPJ": "",
                 "CONTA_EMPENHO": "6.2.2.1.1.01.04.05.009-Diárias Eventuais",
                 "HISTORICO": "diaria", "VALOR": 240_000.0, "VALOR_LIQUIDO": 0,
                 "DATA_PAGAMENTO": "12/04/2025", "NUMERO_EMPENHO": 12500,
                 "NUMERO_PAGAMENTO": 12500})
    # código cuja árvore inteira é desconhecida: execução fora do plano
    for ano, valor in ((2015, 260_000.0), (2016, 310_000.0), (2025, 180_000.0)):
        rows.append({"_ano": ano, "NOME_RAZAO_SOCIAL": "BETA SUPRIMENTOS LTDA",
                     "CPF_CNPJ": "45678901000166",
                     "CONTA_EMPENHO": "7.1.1.01.001-Despesas de Exercícios Anteriores",
                     "HISTORICO": "exercicio anterior", "VALOR": valor,
                     "VALOR_LIQUIDO": 0, "DATA_PAGAMENTO": f"09/11/{ano}",
                     "NUMERO_EMPENHO": 12600, "NUMERO_PAGAMENTO": 12600})

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


# Plano de contas oficial em ÁRVORE, já achatado como
# `clients.achatar_plano_contas` devolve: codigo, nome, nivel, pai, folha.
# Espelha o desenho real do Sistema Confea/Crea — raiz única e vários níveis de
# agregação antes de abrir — porque é esse formato que faz a leitura "por nível
# 2" degenerar numa família só. A classe 1 (ativo) está aqui de propósito: o
# endpoint devolve o plano INTEIRO, e nenhum pagamento pode cair nela; é o que
# prova que o denominador de `plano-contas-sem-lancamento` exclui as classes
# sem execução em vez de publicar "99% sem lançamento".
_PLANO = {
    "6": "DESPESA",
    "6.2": "VARIAÇÕES PATRIMONIAIS DIMINUTIVAS",
    "6.2.2": "DESPESA CORRENTE",
    "6.2.2.1": "PESSOAL E ENCARGOS",
    "6.2.2.1.1": "REMUNERAÇÃO E BENEFÍCIOS",
    "6.2.2.1.1.01": "Pessoal",
    "6.2.2.1.1.01.01": "Remuneração",
    "6.2.2.1.1.01.01.01": "Salários e Vencimentos",
    "6.2.2.1.1.01.01.01.001": "Salários",
    "6.2.2.1.1.01.01.01.002": "Horas Extras",
    "6.2.2.1.1.01.04": "Verbas e Indenizações",
    "6.2.2.1.1.01.04.02": "Auxílios",
    "6.2.2.1.1.01.04.02.001": "Auxílio Educação e Creche",
    "6.2.2.1.1.01.04.05": "Diárias",
    "6.2.2.1.1.01.04.05.001": "Diarias com Funcionários",
    "6.2.2.1.1.01.04.05.002": "Diarias com Conselheiros",
    "6.2.2.1.1.01.04.05.003": "Diarias com Colaboradores",
    "6.2.2.1.1.01.04.05.004": "Diarias com Inspetores",
    "6.2.2.1.1.01.04.05.005": "Pagamento de Jeton",
    "6.2.2.1.1.01.04.05.006": "Diárias no País",
    "6.2.2.1.1.01.04.05.007": "Jetons a Conselheiros",
    "6.2.2.1.1.01.04.08": "Locomoção",
    "6.2.2.1.1.01.04.08.001": "Locomoção de Funcionários",
    "6.2.2.1.1.01.04.08.002": "Locomoção de Conselheiros",
    "6.2.2.1.1.01.04.08.008": "Locomoção de Inspetores",
    "6.2.2.2": "USO DE BENS E SERVIÇOS",
    "6.2.2.2.1": "Serviços de Terceiros",
    "6.2.2.2.1.01": "Serviços contratados",
    "6.2.2.2.1.01.001": "Serviços de Terceiros PJ",
    "6.2.2.2.1.01.002": "Serviços Advocatícios",
    "6.2.2.2.1.01.003": "Serviços de Informática",
    "6.2.2.2.1.01.004": "Serviços de Publicidade",
    "6.2.2.2.2": "Material de Consumo e Almoxarifado",
    "6.2.2.2.2.01": "Materiais",
    "6.2.2.2.2.01.001": "Material de Consumo",
    "6.2.2.2.3": "Utilização de Bens de Terceiros",
    "6.2.2.2.3.01": "Locações",
    "6.2.2.2.3.01.001": "Locação de Imóveis",
    "6.4": "INVESTIMENTOS",
    "6.4.1": "Bens Imóveis",
    "6.4.1.01": "Obras",
    "6.4.1.01.001": "Obras e Instalações",
    "6.4.1.01.002": "Reforma de Imóveis",
    # Família inteira que a taxonomia do projeto NÃO classifica: é o buraco
    # nosso que a frente do plano de contas tem de localizar e nomear.
    "6.5": "TRIBUTÁRIAS, RESTITUIÇÕES E JUDICIAIS",
    "6.5.1": "Restituições",
    "6.5.1.01": "Restituições a Profissionais",
    "6.5.1.01.001": "Restituições de Anuidades",
    "6.5.1.01.002": "Restituições de Taxas de Expediente",
    "6.5.2": "Custas e Obrigações",
    "6.5.2.01": "Judiciais",
    "6.5.2.01.001": "Custas Processuais",
    "6.5.2.02": "Tributárias",
    "6.5.2.02.001": "Obrigações Tributárias e Contributivas",
    # classe de ativo: nenhum pagamento cai aqui, por construção contábil
    "1": "ATIVO",
    "1.1": "Ativo Circulante",
    "1.1.01": "Caixa e Equivalentes de Caixa",
    "1.1.01.001": "Caixa",
    "1.1.01.002": "Bancos Conta Movimento",
    "1.1.02": "Créditos a Receber",
    "1.1.02.001": "Anuidades a Receber",
    "1.1.02.002": "Dívida Ativa a Receber",
}


def _plano_contas() -> pd.DataFrame:
    pais = {c.rsplit(".", 1)[0] for c in _PLANO if "." in c}
    return pd.DataFrame([
        {"codigo": cod, "nome": nome, "nivel": cod.count("."),
         "pai": cod.rsplit(".", 1)[0] if "." in cod else "",
         "folha": cod not in pais, "_ano": 2025}
        for cod, nome in _PLANO.items()])


def _coleta_http() -> pd.DataFrame:
    """Ledger HTTP da coleta — o que `http.salvar_ledger` grava por rodada.

    Sem ele, "0 linhas" é indistinguível entre bloqueio do WAF e o órgão não
    publicar o dado. Os três datasets vazios desta fixture cobrem os três
    vereditos: licitações responde 200 com lista vazia (falta de publicação),
    os endpoints de diárias devolvem 403 (limitação nossa) e as cobranças do
    PNCP devolvem 404 (indeterminado).
    """
    linhas = []
    for ano in range(2013, 2026):
        linhas.append({"caminho": "/portalTransparencia/api/v1.0/Licitacoes",
                       "host": "crea-sp.implanta.net.br",
                       "params": f"referenciaInicio=01/{ano}&referenciaTermino=12/{ano}",
                       "status": 200, "resultado": "vazio", "tentativa": 1,
                       "cache": False, "itens": 0, "duracao_s": 0.4})
    for ep in ("Diarias", "DiariasDeslocamentos", "Indenizacoes"):
        for tentativa in (1, 2):
            linhas.append({"caminho": f"/portalTransparencia/api/v1.0/{ep}",
                           "host": "crea-sp.implanta.net.br",
                           "params": "referenciaInicio=01/2024&referenciaTermino=12/2024",
                           "status": 403, "resultado": "bloqueio",
                           "tentativa": tentativa, "cache": False,
                           "itens": None, "duracao_s": 1.2})
    for ano in (2023, 2024):
        linhas.append({"caminho": "/api/consulta/v1/instrumentoscobranca/inclusao",
                       "host": "pncp.gov.br",
                       "params": f"dataInicial={ano}0101&dataFinal={ano}1231",
                       "status": 404, "resultado": "inexistente", "tentativa": 1,
                       "cache": False, "itens": 0, "duracao_s": 0.3})
    # fonte que veio COM dados: o ledger de uma rodada real não é só de falhas,
    # e uma linha servida do cache também tem de aparecer nele
    for ano in range(2013, 2026):
        linhas.append({"caminho": "/portalTransparencia/api/v1.0/Pagamentos",
                       "host": "crea-sp.implanta.net.br",
                       "params": f"referenciaInicio=01/{ano}&referenciaTermino=12/{ano}",
                       "status": 200, "resultado": "ok", "tentativa": 1,
                       "cache": True, "itens": 4200, "duracao_s": 0.1})
    d = pd.DataFrame(linhas)
    d["ts"] = "2026-01-01T00:00:00"
    return d


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
        # A inspetoria é o grosso do rol real (2.124 de 2.455 na coleta manual)
        # e é ela que recebe "Diárias/Locomoção com Inspetores". Sem inspetores
        # aqui, o denominador da verba política nunca é exercitado.
        *[{"nomeMandatario": f"INSPETOR MUNICIPAL {i:02d}", "cargo": "Inspetor",
           "entidade": ""} for i in range(30)],
        # Domínio próprio que coincide com FORNECEDOR do conselho: é o único
        # elo público entre mandatário e quem recebe, já que o portal não
        # publica a entidade que indicou cada um.
        {"nomeMandatario": "SERGIO MENDES OMEGA", "cargo": "Inspetor Especial",
         "entidade": "", "enderecoEletronico": "sergio@omegaengenharia.com.br"},
        # Datas-sentinela: mandato sem início nem fim, que atravessa qualquer
        # filtro de período. Um deles TEM registro paralelo com data real (só
        # duplicata), o outro NÃO tem — e é esse que o achado tem de isolar.
        {"nomeMandatario": "ROBERTO ALVES LIMA", "cargo": "Conselheiro",
         "entidade": "SINDUSCON SP", "dataPosse": "01/01/1900",
         "dataPrevistaFim": "31/12/9999"},
        {"nomeMandatario": "ANTONIO SEM TERMO", "cargo": "Inspetor",
         "entidade": "", "dataPosse": "01/01/1900",
         "dataPrevistaFim": "31/12/9999"},
    ])
    mandatarios["nome"] = mandatarios["nomeMandatario"]
    mandatarios["NOME_NORM"] = mandatarios["nomeMandatario"].map(norm_nome)
    mandatarios["ENTIDADE_NORM"] = mandatarios["entidade"].map(norm_nome)
    # espelha stage_mandatarios: datas do mandato e DOMÍNIO do e-mail (o
    # endereço completo é descartado na coleta e nunca chega ao relatório)
    mandatarios["dataPosse"] = mandatarios.get(
        "dataPosse", pd.Series(dtype=str)).fillna("15/01/2024")
    mandatarios["dataPrevistaFim"] = mandatarios.get(
        "dataPrevistaFim", pd.Series(dtype=str)).fillna("31/12/2026")
    mandatarios["POSSE"] = pd.to_datetime(mandatarios["dataPosse"],
                                          dayfirst=True, errors="coerce")
    mandatarios["FIM_PREVISTO"] = pd.to_datetime(mandatarios["dataPrevistaFim"],
                                                 dayfirst=True, errors="coerce")
    _email = mandatarios.get("enderecoEletronico",
                             pd.Series(dtype=str)).fillna("")
    mandatarios["EMAIL_DOMINIO"] = _email.str.rsplit("@", n=1).str[-1].where(
        _email.str.contains("@"), "gmail.com")
    mandatarios = mandatarios.drop(columns=["enderecoEletronico"],
                                   errors="ignore")

    # Série anual coerente com o demonstrativo: sem ela a conferência de
    # reconciliação acusaria divergência na própria fixture.
    _emp_base = [
        {"_ano": a, "NOME_CONTA": "Serviços de Terceiros PJ",
         "PESSOA": f"FORNECEDOR REGULAR {i}", "VALOR": 125e6 / 20,
         "VALOR_PAGO": 115e6 / 20, "VALOR_ANULADO": 0.0}
        for a in range(2013, 2026) for i in range(20)]

    # Empenhos fantasma: anulados integralmente, nada pago. Metade é rotina de
    # abertura de exercício (primeiros dias / lote no mesmo dia) e tem de ser
    # separada; a outra metade é o que merece pergunta.
    _fantasmas = (
        [{"_ano": a, "NOME_CONTA": "Serviços de Terceiros PJ",
          "PESSOA": f"FANTASMA ROTINA {i}", "VALOR": 50_000.0, "VALOR_PAGO": 0.0,
          "VALOR_ANULADO": 50_000.0, "DATA": pd.Timestamp(f"{a}-01-05")}
         for a in (2023, 2024, 2025) for i in range(6)] +
        [{"_ano": a, "NOME_CONTA": "Serviços de Terceiros PJ",
          "PESSOA": f"FANTASMA ANOMALO {i}", "VALOR": 900_000.0,
          "VALOR_PAGO": 0.0, "VALOR_ANULADO": 900_000.0,
          "DATA": pd.Timestamp(f"{a}-0{5 + i}-1{i}")}
         for a in (2024, 2025) for i in range(3)])

    empenhos = pd.DataFrame(_emp_base + _fantasmas + [
        {"_ano": 2024, "NOME_CONTA": "Diárias", "PESSOA": _CONSELHEIRO,
         "VALOR": 1000.0, "VALOR_PAGO": 1500.0, "VALOR_ANULADO": 0.0},
        {"_ano": 2024, "NOME_CONTA": "Serviços", "PESSOA": _FORN_FRACIONA,
         "VALOR": 100000.0, "VALOR_PAGO": 60000.0, "VALOR_ANULADO": 40000.0},
    ])

    contratos = pd.DataFrame([
        # entidade que é beneficiária de convênio E fornecedora contratada:
        # o duplo papel real, que o cruzamento convênio × pagamento não via
        {"NUMERO_CONTRATO": "CT-09/2021",
         "CONTRATADA": "12.345.678/0001-90 - ASSOCIACAO DOS ENGENHEIROS X",
         "VALOR": 1_200_000.0, "NUMERO_LICITACAO": None,
         "DATA_INICIO_VIGENCIA": "2021-01-01", "DATA_FINAL_VIGENCIA": "2021-12-31"},
        {"NUMERO_CONTRATO": "CT-01/2022", "CONTRATADA": _FORN_REDE, "VALOR": 500000.0,
         "NUMERO_LICITACAO": None,
         "DATA_INICIO_VIGENCIA": "2022-01-01", "DATA_FINAL_VIGENCIA": "2022-12-31"},
        # pago muito acima do contratado: a obra da nova sede recebe
        # R$ 12 mi/ano de 2022 a 2025 (R$ 48 mi no período) contra um contrato
        # de R$ 5 mi — 9,6× o instrumento. Sem esta linha o achado de
        # gravidade alta `contratos-pago-acima-do-contratado` nunca dispara e
        # a regressão dele passa despercebida.
        {"NUMERO_CONTRATO": "CT-04/2022",
         "CONTRATADA": "77.888.999/0001-22 - CONSTRUTORA NOVA SEDE LTDA",
         "VALOR": 5_000_000.0, "NUMERO_LICITACAO": None,
         "DATA_INICIO_VIGENCIA": "2022-01-01", "DATA_FINAL_VIGENCIA": "2025-12-31"},
        # CNAE × objeto: empresa de LIMPEZA contratada para sistema de gestão.
        # A linha seguinte é o contra-exemplo que impede a lista de virar 100%
        # — sem ela o achado deixa de medir incompatibilidade e passa a medir
        # "todo fornecedor tem CNAE diferente do objeto".
        {"NUMERO_CONTRATO": "CT-02/2024",
         "CONTRATADA": "99.888.777/0001-55 - ALFA SERVICOS LTDA",
         "OBJETO": "Contratação de sistema de gestão de processos e "
                   "licenciamento de uso de software",
         "VALOR": 600_000.0, "NUMERO_LICITACAO": None,
         "DATA_INICIO_VIGENCIA": "2024-01-01",
         "DATA_FINAL_VIGENCIA": "2024-12-31"},
        {"NUMERO_CONTRATO": "CT-03/2024",
         "CONTRATADA": "11.222.333/0001-81 - OMEGA ENGENHARIA LTDA",
         "OBJETO": "Reforma e impermeabilização da cobertura do prédio sede",
         "VALOR": 900_000.0, "NUMERO_LICITACAO": None,
         "DATA_INICIO_VIGENCIA": "2024-02-01",
         "DATA_FINAL_VIGENCIA": "2024-12-31"},
    ])
    contratos["INI"] = pd.to_datetime(contratos["DATA_INICIO_VIGENCIA"])
    contratos["FIM"] = pd.to_datetime(contratos["DATA_FINAL_VIGENCIA"])
    # espelha stage_contratos: o portal grava "CNPJ - RAZÃO SOCIAL" e o
    # pipeline separa os dois antes de normalizar. Sem isso o nome
    # normalizado carrega o CNPJ e nenhum join por nome casa.
    contratos["CONTRATADA_CNPJ"] = contratos["CONTRATADA"].str.extract(
        r"(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})", expand=False).map(so_digitos)
    contratos["CONTRATADA_NOME"] = contratos["CONTRATADA"].str.replace(
        r"^\s*[\d./-]{14,20}\s*-\s*", "", regex=True)
    contratos["CONTRATADA_NORM"] = contratos["CONTRATADA_NOME"].map(norm_nome)

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
    # "Diárias no País" tem realizado MENOR que o efetivamente pago nos
    # pagamentos: é a divergência entre fluxo e contabilidade que a conciliação
    # tem de expor.
    comparativo = pd.DataFrame([
        {"_ano": a, "NOME": nome, "ANALITICA": True,
         "REALIZADO_EXERCICIO": base * (1.06 ** (a - 2013)),
         "VALOR_ORCADO": base * (1.06 ** (a - 2013)) * _ORCADO_PCT[nome]}
        for a in range(2013, 2026)
        for nome, base in (("Vencimentos e Salários", 40e6),
                           ("Diárias no País", 2e6),
                           ("Cargos em Comissão", 6e6))])

    # A receita não é só denominador: a ART mede o volume de engenharia que o
    # conselho DEVERIA fiscalizar. Aqui ela cresce 12% ao ano enquanto o
    # Departamento de Fiscalização cresce 1-2% (ver _CENTROS): a cobertura por
    # real de ART cai, e é esse o cenário que a frente de receita tem de pegar.
    _RECEITAS = (
        # (conta, base, crescimento ao ano)
        ("Anuidades de Pessoa Física", 90e6, 1.05),
        ("Anuidades de Pessoa Jurídica", 30e6, 1.05),
        ("ART - Anotação de Responsabilidade Técnica", 30e6, 1.12),
        ("Multas por Infração à Legislação Profissional", 4e6, 1.16),
        ("Multa e Juros de Mora sobre Anuidades", 3e6, 1.04),
        ("Dívida Ativa - Anuidades", 8e6, 1.06),
        ("Receita de Certidões e Emolumentos", 2e6, 1.05),
        ("Rendimentos de Aplicações Financeiras", 6e6, 1.03),
        # A anuidade não se chama "anuidade" no plano real do Crea-SP: é
        # segmentada por nível de formação (PF) e por faixa de capital (PJ),
        # cada uma dividida em exercício Atual e Anterior. Foi essa lacuna que
        # jogou R$ 1,55 bi de anuidade em "outras receitas" e fez o relatório
        # publicar "10% da receita vem da anuidade" quando o correto era 46%.
        ("Nível Superior - Atual", 70e6, 1.05),
        ("Nível Superior - Anterior", 5e6, 1.05),
        ("Nível Médio - Atual", 3e6, 1.05),
        ("Faixa 1 - Atual", 8e6, 1.05),
        ("Faixa 7 - Atual", 6e6, 1.05),
        # e as demais naturezas que também caíam em "outras"
        ("Não Tributária (Multas Disc. Leis 5.194/66 e 6496/77) PJ", 2.5e6, 1.16),
        ("Honorários Advocatícios", 3e6, 1.06),
        ("Aluguéis de Imóveis Próprios", 1.5e6, 1.03),
        ("Restituições de Despesas", 1.8e6, 1.03),
        ("Receitas Não Identificadas", 0.3e6, 1.02),
    )
    receita = pd.DataFrame([
        {"_ano": a, "NOME": nome, "ANALITICA": True,
         "REALIZADO_EXERCICIO": base * (cresc ** (a - 2013))}
        for a in range(2013, 2026)
        for nome, base, cresc in _RECEITAS])

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
        # Despesa de NATUREZA finalística — sem ela o eixo macro da frente
        # "para onde vai o dinheiro" só produzia Pessoal e Administrativo, a
        # coluna "Finalística" nunca existia e `finalistica-razao-meio-fim`
        # — a frase mais citável do relatório inteiro — não era exercitada por
        # nenhum cenário. Deliberadamente pequena: é o denominador minúsculo do
        # caso real que a guarda de cobertura tem de pegar.
        ("Departamento de Fiscalização", "Ações de Fiscalização", 1.1e6, 1.02),
        ("UGI Ribeirão Preto - Atendimento", "Atendimento ao Profissional",
         0.4e6, 1.02),
    )
    # As doze colunas VALOR_JAN..VALOR_DEZ são o único eixo MENSAL que a coleta
    # real entrega, e é sobre elas que a frente de sazonalidade trabalha. Os
    # perfis abaixo são deliberados, um por cenário que a frente tem de pegar:
    #   _MES_UNIFORME — um doze avos por mês (unidade sem sazonalidade)
    #   _MES_DEZEMBRO — 45% do ano em dezembro (corrida de fim de exercício)
    #   _MES_LOTE     — o ano inteiro em três meses (verba paga em lote, não
    #                   em viagem: é o padrão que separa diária de acerto)
    #   _MES_PARCIAL  — 2025 para em agosto. Faz duas coisas de uma vez: a soma
    #                   dos meses deixa de fechar com VALOR_TOTAL (achado
    #                   `sazonalidade-meses-nao-fecham`) e o exercício é
    #                   excluído das estatísticas por calendário incompleto,
    #                   que é o caminho onde um `NOME_MES[0]` já derrubou a
    #                   frente inteira. VALOR_TOTAL NÃO é tocado, então nenhuma
    #                   outra frente muda de número.
    _MESES_CC = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
                 "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
    _MES_UNIFORME = [1 / 12] * 12
    _MES_DEZEMBRO = [0.05] * 11 + [0.45]
    _MES_LOTE = [0.0, 0.0, 0.34, 0.0, 0.0, 0.0,
                 0.33, 0.0, 0.0, 0.0, 0.0, 0.33]
    _MES_PARCIAL = [1 / 12] * 8 + [0.0] * 4
    _ANO_PARCIAL = 2025
    _PERFIL_MENSAL = {
        ("Gabinete da Presidência", "Diárias no País"): _MES_LOTE,
        ("Câmaras Especializadas", "Jetons a Conselheiros"): _MES_LOTE,
        ("Gerência de Tecnologia da Informação",
         "Serviços de Terceiros PJ"): _MES_DEZEMBRO,
        ("Superintendência Regional de Bauru",
         "Material de Consumo"): _MES_DEZEMBRO,
    }

    def _linha_centro_custo(a, cc, conta, base, cresc):
        total = base * (cresc ** (a - 2013))
        pesos = (_MES_PARCIAL if a == _ANO_PARCIAL
                 else _PERFIL_MENSAL.get((cc, conta), _MES_UNIFORME))
        linha = {"_ano": a, "NOME_CENTRO_CUSTO": cc, "NOME_CONTA": conta,
                 "ANALITICO": True, "VALOR_TOTAL": total}
        linha.update({f"VALOR_{m}": total * p
                      for m, p in zip(_MESES_CC, pesos)})
        return linha

    centro_custo = pd.DataFrame([
        _linha_centro_custo(a, cc, conta, base, cresc)
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
        # numeroControlePNCPCompra: o certame de origem que o portal do
        # conselho não informa em nenhum dos seus 965 contratos
        {"orgao_cnpj": "00000000000191", "orgao_papel": "alvo",
         "orgao_nome": "CREA-SP", "fornecedor_ni": "55666777000188",
         "fornecedor_nome": "ELETRICA PREDIAL LTDA",
         "objeto": "instalação elétrica e subestação do prédio sede",
         "valorGlobal": 2_400_000.0, "valorAcumulado": 2_400_000.0,
         "numeroContrato": "7/2023", "vigenciaInicio": "2023-01-01",
         "vigenciaFim": "2023-12-31", "anoContrato": 2023, "tipoPessoa": "PJ",
         "dataAssinatura": "2023-01-01",
         "numeroControlePNCPCompra": "00000000000191-1-000123/2023",
         "processo": "2023/000123"},
        {"orgao_cnpj": "00000000000191", "orgao_papel": "alvo",
         "orgao_nome": "CREA-SP", "fornecedor_ni": "66777888000199",
         "fornecedor_nome": "REFORMA TOTAL LTDA",
         "objeto": "reforma e impermeabilização da cobertura com troca de telhado",
         "valorGlobal": 1_800_000.0, "valorAcumulado": 1_800_000.0,
         "numeroContrato": "8/2023", "vigenciaInicio": "2023-03-01",
         "vigenciaFim": "2024-02-29", "anoContrato": 2023, "tipoPessoa": "PJ",
         "dataAssinatura": "2023-03-01",
         "numeroControlePNCPCompra": "00000000000191-1-000456/2023",
         "processo": "2023/000456"},
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
    # espelha stage_pncp
    pncp["_ano"] = pd.to_numeric(pncp["anoContrato"], errors="coerce")

    return {
        "pagamentos": _pagamentos(), "empenhos": empenhos, "contratos": contratos,
        "aditivos": aditivos, "licitacoes": pd.DataFrame(), "convenios": convenios,
        "comparativo_despesa": comparativo, "despesas_centro_custo": centro_custo,
        "comparativo_receita": receita, "balanco_orcamentario": balanco,
        "demonstrativo": demonstrativo,
        "atas": atas, "quadro_pessoal": quadro,
        "mandatarios": mandatarios, "indenizacoes_endpoint": pd.DataFrame(),
        "pncp_contratos": pncp, "pncp_cobrancas": pd.DataFrame(),
        "plano_contas": _plano_contas(), "_coleta_http": _coleta_http(),
    }


_CNPJ_FRACIONA = "99888777000155"   # já recebe 5 × 15k em 2024 nos pagamentos
_CNPJ_ASSOC = "28097358000101"      # já recebe 5 mi/ano nos pagamentos

_FICHAS_FIXTURE = {
    _CNPJ_REDE: {"razao_social": _FORN_REDE, "descricao_situacao_cadastral": "ATIVA",
                 "data_inicio_atividade": "2023-03-01",
                 "cnae_fiscal": 4120400,
                 "cnae_fiscal_descricao": "Obras de engenharia",
                 "municipio": "São Paulo", "uf": "SP", "codigo_porte": 5,
                 "qsa": [{"nome_socio": _COMISSIONADO},
                         {"nome_socio": "PEDRO HENRIQUE RAMOS"}]},
    # situação só pelo CÓDIGO (8 = baixada), sem descrição: é assim que a
    # minhareceita devolve parte das fichas. Ler o código como texto ("8" não
    # contém "ATIVA") acusava de baixa TODA ficha que viesse só com o número.
    _CNPJ_BAIXADO: {"razao_social": "DELTA COMERCIO LTDA",
                    "situacao_cadastral": 8,
                    "data_inicio_atividade": "2010-01-01",
                    "cnae_fiscal": 4649499,
                    "cnae_fiscal_descricao": "Comércio varejista",
                    "municipio": "Rio de Janeiro", "uf": "RJ",
                    "codigo_porte": "03",
                    "qsa": [{"nome_socio": "PEDRO HENRIQUE RAMOS"}]},
    # ME no interior: sem ela, "quanto chega a micro e pequena" mede zero
    _CNPJ_FRACIONA: {"razao_social": "ALFA SERVICOS LTDA",
                     "descricao_situacao_cadastral": "ATIVA",
                     "data_inicio_atividade": "2011-06-01",
                     "cnae_fiscal": "8121-4/00",
                     "cnae_fiscal_descricao": "Limpeza em prédios e domicílios",
                     "municipio": "Ribeirão Preto", "uf": "SP",
                     "codigo_porte": 1},
    _CNPJ_ASSOC: {"razao_social": "ASSOCIACAO DOS PROCURADORES DO CREA-SP",
                  "descricao_situacao_cadastral": "ATIVA",
                  "data_inicio_atividade": "1995-04-01",
                  "cnae_fiscal": 9430800,
                  "cnae_fiscal_descricao": "Atividades de associações de "
                                           "defesa de direitos sociais",
                  "municipio": "São Paulo", "uf": "SP", "codigo_porte": 5},
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
    "finalistica-razao-meio-fim",      # a frase mais citável — guarda de cobertura
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
    "repr-duplo-canal",                # convênio E contrato (não convênio E pagamento)
    "repr-concentracao",
    "repr-mandatarios-composicao",     # quantos são — o denominador da verba
    "repr-mandato-sem-termo",          # posse 01/01/1900, fim 31/12/9999
    "repr-mandatario-dominio",         # domínio do e-mail = fornecedor do órgão
    # receita: de onde vem o dinheiro e o que ele deveria comprar
    "receita-composicao",
    "receita-anuidade-segmentada",     # anuidade que não se chama anuidade
    "receita-cobertura-por-art",       # ART cresce 12%/ano, fiscalização 1-2%
    "receita-multa-vs-anuidade",
    "receita-dependencia-anuidade",
    # orçamento
    "orcamento-execucao",
    "orcamento-estouro-conta",         # conta que estoura a dotação todo ano
    "orcamento-verbas-vs-dotacao",     # diárias executadas acima do aprovado
    "orcamento-retorno-da-anuidade",   # quanto da receita volta como fiscalização
    "orcamento-conciliacao-verba",     # pago (fluxo) × realizado (contabilidade)
    # opacidade
    "opacidade-agregadores",           # "DIVERSOS FUNCIONARIOS"
    "opacidade-decomposicao-conta",    # o que há dentro do agregado
    # execução
    "empenhos-fracionamento",
    "empenhos-pago-maior-que-empenhado",
    "empenhos-fantasma",               # taxa, com a rotina de abertura separada
    "empenhos-vinculo-empenho-ausente",  # art. 60: a chave não é publicada — um
                                         # achado de metodologia, não 7.059 acusações
    "contratos-aditivos-limite",
    "contratos-pago-acima-do-contratado",   # 48 mi pagos contra 5 mi contratados
    "contratos-contratado-nao-executado",   # vigência encerrada, nada pago
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
    "contratos-certame-no-pncp",       # o PNCP diz qual compra originou o contrato
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
    "fornecedores-geografia",          # 3 municípios, quase tudo num só
    "fornecedores-fora-de-sp",         # DELTA sediada no RJ
    "fornecedores-porte",              # quanto do valor chega a ME/EPP
    "fornecedores-cnae-x-objeto",      # empresa de limpeza contratada para TI
    # sazonalidade: o único eixo MENSAL que a coleta entrega
    "sazonalidade-dezembro",
    "sazonalidade-dezembro-concentrada",
    "sazonalidade-verbas-em-lote",     # o ano inteiro de verba em três meses
    "sazonalidade-perfil-atipico",
    "sazonalidade-meses-nao-fecham",   # os 12 meses não somam o VALOR_TOTAL
    # plano de contas oficial
    "plano-despesa-por-no",
    "plano-cobertura-taxonomia",
    "plano-familias-nao-cobertas",     # família 6.5 inteira sem regra na taxonomia
    "plano-contas-sem-lancamento",
    "plano-execucao-fora-do-plano",    # código 7.1.1.01.001 fora do plano
    # diagnóstico: a causa de cada fonte vazia, lida do status HTTP
    "diagnostico-porque-vazio",
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
    # Receita: a conta tem de bater com as linhas da fixture, senão um regex de
    # fonte que deixe de casar (ex.: "ART" virando OUTRA) passa despercebido —
    # e a cobertura por ART, que é o achado, sai com denominador errado.
    def soma(base, cresc):            # 13 exercícios, 2013..2025
        return sum(base * (cresc ** t) for t in range(13))

    # anuidade nomeada (PF+PJ) + os cinco segmentos que não a nomeiam
    _anuid_segmentada = (soma(70e6, 1.05) + soma(5e6, 1.05) + soma(3e6, 1.05)
                         + soma(8e6, 1.05) + soma(6e6, 1.05))
    exige("receita-dependencia-anuidade", "anuidade",
          soma(90e6, 1.05) + soma(30e6, 1.05) + _anuid_segmentada)
    exige("receita-dependencia-anuidade", "divida_ativa", soma(8e6, 1.06))
    exige("receita-composicao", "total",
          soma(90e6, 1.05) + soma(30e6, 1.05) + soma(30e6, 1.12)
          + soma(4e6, 1.16) + soma(3e6, 1.04) + soma(8e6, 1.06)
          + soma(2e6, 1.05) + soma(6e6, 1.03) + _anuid_segmentada
          + soma(2.5e6, 1.16) + soma(3e6, 1.06) + soma(1.5e6, 1.03)
          + soma(1.8e6, 1.03) + soma(0.3e6, 1.02))
    # ART cresce 12% ao ano; a fiscalização (4e6 a 1%/ano + 5e6 a 2%/ano) fica
    # muito atrás — a cobertura por real de ART tem de CAIR.
    exige("receita-cobertura-por-art", "cagr_art", 0.12, tol=0.02)
    fisc_ini, fisc_fim = 4e6 + 5e6, 4e6 * 1.01 ** 12 + 5e6 * 1.02 ** 12
    exige("receita-cobertura-por-art", "cagr_fiscalizacao",
          (fisc_fim / fisc_ini) ** (1 / 12) - 1, tol=0.05)
    f = por_id.get("receita-cobertura-por-art")
    if f is not None and not (f.metricas.get("cobertura_final", 1)
                              < f.metricas.get("cobertura_inicial", 0)):
        erros.append("receita-cobertura-por-art: a cobertura por real de ART "
                     "deveria cair na fixture e não caiu")
    # conselheiros + inspetores + jeton, 13 exercícios
    exige("diarias-politico-vs-quadro", "estrutura_politica",
          (5_200_000 + 4_100_000 + 700_000 + 2_100_000 + 1_400_000) * 13)
    # funcionários + colaboradores
    exige("diarias-politico-vs-quadro", "quadro_contratado",
          (2_600_000 + 670_000 + 570_000) * 13)

    # art. 60: a fixture de empenhos não publica número de empenho sob rótulo
    # nenhum e o par credor+valor não coincide — o esperado é a guarda de
    # metodologia, com 100% e nenhum casamento, não 7.059 acusações de
    # ilegalidade.
    exige("empenhos-vinculo-empenho-ausente", "taxa_pct", 100.0)
    f = por_id.get("empenhos-vinculo-empenho-ausente")
    # `None`, e NÃO 0. A versão anterior exigia 0 aqui e foi essa exigência que
    # carimbou o zero fabricado: com a chave indisponível, "casaram zero" é
    # afirmação falsa — nada foi tentado. E como a guarda
    # `empenhos-chave-nao-casa` dispara justamente com `casados_por_numero == 0`,
    # o zero fabricado a mantinha desligada, deixando passar a acusação de
    # "64,0% dos pagamentos sem empenho" da rodada de 2026-09-03.
    if f is not None and f.metricas.get("casados_por_numero") is not None:
        erros.append("empenhos-vinculo-empenho-ausente.casados_por_numero = "
                     f"{f.metricas.get('casados_por_numero')}, esperado None "
                     "(chave indisponível não casa zero: não é tentada)")
    erros.extend(_regressao_chave_empenho())
    erros.extend(_regressao_retomada())
    erros.extend(_regressao_dado_nao_some())

    # obra da nova sede: R$ 48 mi pagos contra R$ 5 mi contratados. O achado
    # publica o denominador do TETO — se alguém trocar de volta pela estimativa
    # central, o valor muda e o teste pega.
    exige("contratos-pago-acima-do-contratado", "contratado_teto", 5_000_000)
    exige("contratos-pago-acima-do-contratado", "pago", 48_000_000)
    exige("contratos-pago-acima-do-contratado", "pago_alem_do_contratado",
          43_000_000)
    # Subexecução, três casos com vigência encerrada e a soma discriminada —
    # um total agregado esconderia a troca de um componente por outro:
    #   ASSOCIACAO DOS ENGENHEIROS X  1,2 mi contratados, zero pago
    #   OMEGA ENGENHARIA  1,4 mi em dois contratos + 200k de aditivo, 800k pagos
    #   ALFA SERVICOS     600k contratados, 75k pagos (5 × 15k de 2024)
    exige("contratos-contratado-nao-executado", "nao_executado",
          1_200_000 + (1_600_000 - 800_000) + (600_000 - 75_000))
    exige("contratos-contratado-nao-executado", "fornecedores", 3)
    exige("contratos-contratado-nao-executado",
          "sem_pagamento_correspondente", 1)

    # Plano de contas. `total_conciliado` confere que o casamento por código E
    # por nome funcionam ao mesmo tempo: se o casamento por nome quebrar,
    # "Diárias no País"/"Jetons a Conselheiros"/"Serviços de Terceiros PJ" saem
    # da conta e o número despenca sem o achado desaparecer.
    # +364.000.000 = (20 mi Confea + 8 mi Mútua) x 13 exercícios
    exige("plano-despesa-por-no", "total_conciliado",
          451_466_962.55 + (20_000_000 + 8_000_000) * 13)
    exige("plano-despesa-por-no", "total_executado",
          452_216_962.55 + (20_000_000 + 8_000_000) * 13)
    # família 6.5 inteira: 3 contas × 13 exercícios, com o jitter determinístico
    _familia_65 = sum(
        base + 37_000 * ((ano + i * 3) % 7)
        for ano in range(2013, 2026)
        for i, base in enumerate((1_500_000.0, 1_200_000.0, 1_800_000.0)))
    exige("plano-cobertura-taxonomia", "valor_nao_classificado", _familia_65)
    exige("plano-familias-nao-cobertas", "familias", 1)
    exige("plano-execucao-fora-do-plano", "valor_fora",
          260_000 + 310_000 + 180_000)
    # O denominador tem de EXCLUIR as 4 folhas de ativo (classe 1): contar o
    # plano inteiro publicava 67% de contas "sem lançamento" numa prova em que
    # 100% das contas de despesa estavam movimentadas.
    exige("plano-contas-sem-lancamento", "contas_analiticas", 25)
    exige("plano-contas-sem-lancamento", "contas_analiticas_plano_inteiro", 29)

    # Transferência obrigatória (Confea + Mútua, 13 exercícios) NÃO pode entrar
    # no repasse discricionário, e a Associação dos Procuradores — que menciona
    # o Crea no nome — TEM de continuar dentro dele. Este par de asserções é o
    # que impede a regressão que apagaria `casos-entidade-da-casa`.
    exige("repr-dinheiro-por-interesse", "transferencia_obrigatoria_sistema",
          (20_000_000 + 8_000_000) * 13)
    exige("repr-dinheiro-por-interesse", "total", 49_590_000)
    exige("casos-entidade-da-casa", "total", 40_000_000)

    # A anuidade segmentada: 5 contas de nível/faixa, 13 exercícios. Se um
    # desses padrões deixar de casar, a anuidade despenca e o relatório volta a
    # publicar um share impossível para um conselho profissional.
    exige("receita-anuidade-segmentada", "contas", 5)
    exige("receita-anuidade-segmentada", "valor_reclassificado",
          soma(70e6, 1.05) + soma(5e6, 1.05) + soma(3e6, 1.05)
          + soma(8e6, 1.05) + soma(6e6, 1.05))
    # e a multa de fiscalização tem de reconhecer as leis citadas no nome
    exige("receita-multa-vs-anuidade", "cagr_multa", 0.16, tol=0.02)

    # A razão meio/fim é a frase mais citável do relatório e a mais frágil: o
    # denominador é a atividade-fim que NÓS identificamos. Com ela abaixo de 5%
    # da despesa o achado tem de sair como info, com a cobertura no título —
    # nunca como manchete de gravidade alta.
    f = por_id.get("finalistica-razao-meio-fim")
    if f is not None:
        if f.gravidade != "info":
            erros.append(f"finalistica-razao-meio-fim saiu como '{f.gravidade}' "
                         "com atividade-fim abaixo de 5% — tem de ser info")
        if not f.cobertura or f.cobertura.get("pct", 100) > 5:
            erros.append("finalistica-razao-meio-fim sem cobertura declarada "
                         "ou com cobertura acima do esperado na fixture")

    # Mandatários: 7 conselheiros nomeados + 30 inspetores + 1 inspetor com
    # domínio próprio + 1 sem termo = 39 pessoas. Se o regex de cargo parar de
    # separar inspetor de conselheiro, o denominador da verba política sai
    # errado sem o achado desaparecer.
    exige("repr-mandatarios-composicao", "pessoas", 39)
    exige("repr-mandatarios-composicao", "conselheiros", 6)
    exige("repr-mandatarios-composicao", "inspetores", 32)
    # dois registros-sentinela; só um deles NÃO tem registro paralelo com data
    exige("repr-mandato-sem-termo", "registros", 2)
    exige("repr-mandato-sem-termo", "so_com_data_sentinela", 1)
    # OMEGA ENGENHARIA: 800k em pagamento + 500k e 900k em contrato
    exige("repr-mandatario-dominio", "vinculos_com_recebedor", 1)
    exige("repr-mandatario-dominio", "valor_recebido",
          800_000 + 500_000 + 900_000)

    # A fixture do ledger tem UM vazio de cada tipo. Se a atribuição
    # tentativa -> dataset deixar de casar (mudança de caminho no clients.py),
    # o veredito migra em silêncio e o achado continua "detectado".
    exige("diagnostico-porque-vazio", "sem_publicacao", 1)
    exige("diagnostico-porque-vazio", "falha_da_coleta", 1)
    exige("diagnostico-porque-vazio", "indeterminadas", 1)
    return erros


def _regressao_retomada() -> list[str]:
    """"Rodar só o que falta": a retomada vê o buraco, e a recusa não vira vazio.

    Três defeitos concretos, todos no caminho que o notebook do Colab percorre
    em TODA reconexão (`main.py all --retomar`):

    1. `--retomar` olhava só se `data/staged/<etapa>.csv` tinha linhas. Uma
       etapa que trouxe 2016-2025 e falhou em 2013-2015 parecia pronta, e o
       buraco não fechava em rodada nenhuma.
    2. `implanta_variantes` devolvia `[]` quando NENHUM formato de parâmetro era
       aceito. `_anos_com_sondagem` grava `.vazio` para toda janela que devolve
       lista, e `.vazio` de exercício encerrado vale para sempre — a recusa
       virava permanente no Drive do operador, e a rodada seguinte lia "vazio"
       do disco sem tocar na rede.
    3. o inventário precisa parar de sondar exatamente onde o laço real para,
       senão anuncia trabalho que não existe.

    Usa exercícios ENCERRADOS (2020-2023) de propósito: o checkpoint do ano em
    curso expira em 12h, e um teste que dependesse disso passaria ou falharia
    conforme a hora do dia.
    """
    import os
    import tempfile
    from pathlib import Path
    from . import clients, pipeline

    erros: list[str] = []
    guardado = (config.RAW_DIR, config.STAGED_DIR, config.DATA_DIR,
                config.CACHE_DIR, os.environ.get("QUEDA_CHECKPOINT"))
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        try:
            # `QUEDA_CHECKPOINT=0` no ambiente desliga a leitura de checkpoint
            # (pipeline._checkpoint_ligado) e derruba as seis asserções de uma
            # vez — medido. Um teste que passa ou falha conforme a variável de
            # quem o chamou não testa nada.
            os.environ["QUEDA_CHECKPOINT"] = "1"
            # DATA_DIR junto: `manual_rows` lê `config.DATA_DIR/'manual'` no
            # começo de stage_licitacoes, e um arquivo manual na árvore de quem
            # roda faria a etapa nem chegar ao checkpoint.
            config.DATA_DIR = base
            config.CACHE_DIR = base / "cache"
            config.RAW_DIR = base / "raw"
            config.STAGED_DIR = base / "staged"
            config.STAGED_DIR.mkdir(parents=True)
            cp = config.RAW_DIR / "checkpoints"
            # empenhos: 2021-2023 em checkpoint, 2020 faltando; staged CHEIO
            (cp / "empenhos").mkdir(parents=True)
            for ano in (2021, 2022, 2023):
                (cp / "empenhos" / f"{ano}.csv").write_text("NUMERO,VALOR\n1,10\n")
            (config.STAGED_DIR / "empenhos.csv").write_text(
                "_ano,NUMERO,VALOR\n2021,1,10\n")
            # contratos: todas as janelas em checkpoint; staged CHEIO
            (cp / "contratos").mkdir(parents=True)
            for ano in range(2020, 2024):
                (cp / "contratos" / f"{ano}.csv").write_text("NUMERO_CONTRATO\nCT-1\n")
            (config.STAGED_DIR / "contratos.csv").write_text(
                "_ano,NUMERO_CONTRATO\n2021,CT-1\n")

            pronto, pend = pipeline.etapa_pronta("empenhos", 2020, 2023)
            if pronto is not None or pend != [2020]:
                erros.append(
                    "regressão da retomada: etapa com CSV cheio e o exercício "
                    f"2020 faltando foi dada por pronta (pendentes={pend}) — "
                    "o buraco nunca fecharia")
            pronto, pend = pipeline.etapa_pronta("contratos", 2020, 2023)
            if pronto is None or pend:
                erros.append(
                    "regressão da retomada: etapa com todas as janelas em "
                    f"checkpoint NÃO foi pulada (pendentes={pend}) — a "
                    "retomada barata deixou de existir")

            # sondagem retroativa: dois exercícios vazios seguidos encerram a
            # busca, exatamente como em `_anos_com_sondagem`
            (cp / "atas").mkdir(parents=True)
            for ano in (2022, 2023):
                (cp / "atas" / f"{ano}.vazio").write_text("x\n")
            if pipeline.janelas_pendentes("atas", 2023, None) != []:
                erros.append(
                    "regressão da retomada: dois exercícios vazios seguidos "
                    "deveriam encerrar a sondagem retroativa e não "
                    f"encerraram ({pipeline.janelas_pendentes('atas', 2023, None)})")
            if pipeline.janelas_pendentes("atas", 2023, 2020) != [2021, 2020]:
                erros.append(
                    "regressão da retomada: com --desde explícito a sondagem "
                    "não pode parar nos vazios — esperado [2021, 2020], veio "
                    f"{pipeline.janelas_pendentes('atas', 2023, 2020)}")

            inv = pipeline.inventario(2020, 2023)
            linha = inv[inv["etapa"] == "empenhos"].iloc[0]
            if not linha["vai_a_rede"] or linha["exercicios_faltando"] != "2020":
                erros.append(
                    "regressão da retomada: o inventário não aponta o "
                    f"exercício que falta (veio {linha['exercicios_faltando']!r}, "
                    f"vai_a_rede={linha['vai_a_rede']})")

            # A propriedade que dá nome ao modo: refazer a etapa vai à rede SÓ
            # pelo buraco. Sem esta asserção, uma retomada que refizesse tudo
            # passaria nos testes acima e torraria a cota do portal.
            rede: list[int] = []

            def _fake_ano(endpoint, ano, **kw):
                rede.append(ano)
                return [{"NUMERO": 9, "VALOR": 5.0,
                         "DATA_EMPENHO": f"01/01/{ano}"}]

            de_verdade = clients.implanta_ano
            try:
                clients.implanta_ano = _fake_ano
                df = pipeline.stage_empenhos(2020, 2023)
            finally:
                clients.implanta_ano = de_verdade
            if rede != [2020]:
                erros.append(
                    "regressão da retomada: a etapa refeita foi à rede pelos "
                    f"exercícios {rede}, e só 2020 faltava — as janelas em "
                    "checkpoint deveriam sair do disco")
            if len(df) != 4:
                erros.append(
                    "regressão da retomada: 3 janelas do disco + 1 da rede "
                    f"deveriam dar 4 linhas, deram {len(df)} — o dado do "
                    "checkpoint não está sendo reaproveitado")
        finally:
            (config.RAW_DIR, config.STAGED_DIR, config.DATA_DIR,
             config.CACHE_DIR, checkpoint_antes) = guardado
            if checkpoint_antes is None:
                os.environ.pop("QUEDA_CHECKPOINT", None)
            else:
                os.environ["QUEDA_CHECKPOINT"] = checkpoint_antes

    # `[]` só quando o SERVIDOR respondeu; `None` quando nenhum formato passou
    orig = clients.implanta
    try:
        clients.implanta = lambda ep, params=None: None      # 400/403: recusado
        clients._FORMATO_APRENDIDO.clear()
        if clients.implanta_variantes("Licitacoes", 2019)[0] is not None:
            erros.append(
                "regressão da retomada: formato recusado voltou a devolver "
                "lista — `_anos_com_sondagem` gravaria `.vazio` e congelaria a "
                "recusa no Drive do operador")
        clients.implanta = lambda ep, params=None: []        # servidor: nada aqui
        clients._FORMATO_APRENDIDO.clear()
        if clients.implanta_variantes("Licitacoes", 2019)[0] != []:
            erros.append(
                "regressão da retomada: resposta vazia legítima do servidor "
                "virou `None` — o exercício vazio deixaria de encerrar a "
                "sondagem retroativa e a série histórica se estenderia sozinha")
    finally:
        clients.implanta = orig

    return erros


def _regressao_dado_nao_some() -> list[str]:
    """Quatro maneiras de o dado sumir entre o disco e a análise, todas medidas.

    Nenhuma delas dá erro: o relatório sai bonito, com menos dado do que existe.
    É a pior forma de falhar numa ferramenta de fiscalização, porque o silêncio
    se parece com regularidade.
    """
    import os
    import tempfile
    from pathlib import Path
    from . import clients, pipeline

    erros: list[str] = []
    guardado = (config.RAW_DIR, config.STAGED_DIR, config.DATA_DIR,
                config.CACHE_DIR, os.environ.get("QUEDA_CHECKPOINT"))
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        try:
            os.environ["QUEDA_CHECKPOINT"] = "1"
            config.DATA_DIR = base
            config.CACHE_DIR = base / "cache"
            config.RAW_DIR = base / "raw"
            config.STAGED_DIR = base / "staged"
            config.STAGED_DIR.mkdir(parents=True)

            # 1. A variante SEM FILTRO dentro de um laço por exercício. Se o
            #    endpoint responde a tabela inteira, cada janela recebe o mesmo
            #    payload com um `_ano` diferente: medido, 2 linhas viraram 28.
            tabela = [{"NUMERO": 1}, {"NUMERO": 2}]
            de_verdade = clients.implanta
            try:
                clients.implanta = (lambda ep, params=None:
                                    list(tabela) if not params else [])
                clients._FORMATO_APRENDIDO.clear()
                df = pipeline.stage_licitacoes(2013, 2026)
            finally:
                clients.implanta = de_verdade
            if len(df) > len(tabela):
                erros.append(
                    "regressão do dado: a resposta SEM FILTRO entrou no laço "
                    f"por exercício e virou {len(df)} linhas a partir de "
                    f"{len(tabela)} — cada janela carimbou a mesma tabela com "
                    "um ano diferente")

            # 2. CNPJ é string de dígitos, não número. Relido com inferência,
            #    perde o zero à esquerda e some do filtro `str.len() == 14`.
            (config.STAGED_DIR / "pagamentos.csv").write_text(
                "FAVORECIDO_CNPJ,VALOR\n01234567000189,10.5\n")
            recarregado = pipeline.load_staged()["pagamentos"]
            if recarregado["FAVORECIDO_CNPJ"].iloc[0] != "01234567000189":
                erros.append(
                    "regressão do dado: load_staged destruiu o zero à esquerda "
                    f"do CNPJ ({recarregado['FAVORECIDO_CNPJ'].iloc[0]!r}) — o "
                    "fornecedor desaparece da análise sem erro nenhum")
            if not isinstance(recarregado["VALOR"].iloc[0], float):
                erros.append(
                    "regressão do dado: load_staged transformou VALOR em texto "
                    "ao proteger as colunas de dígitos — toda soma cai")

            # 3. Etapa que levanta não pode apagar o CSV bom da rodada anterior.
            (config.STAGED_DIR / "empenhos.csv").write_text(
                "_ano,NUMERO\n2020,1\n2021,2\n")
            quebrado = pipeline.stage_empenhos
            outras: dict = {}
            empenhos_preservados = -1
            try:
                pipeline.stage_empenhos = lambda d, a: (_ for _ in ()).throw(
                    KeyError("coluna sumiu"))
                for etapa in ("pagamentos", "contratos", "aditivos",
                              "licitacoes", "convenios", "comparativo_despesa",
                              "despesas_centro_custo", "demonstrativo",
                              "plano_contas", "processos_compras", "atas",
                              "orcamento", "quadro_pessoal", "mandatarios",
                              "indenizacoes", "pncp"):
                    outras[etapa] = getattr(pipeline, f"stage_{etapa}")
                    setattr(pipeline, f"stage_{etapa}",
                            lambda *a, **k: pd.DataFrame())
                staged = pipeline.build_staged(2020, 2021, retomar=False)
                empenhos_preservados = len(staged.get("empenhos", []))

                # 4. Dataset gravado por um stage_* mas NÃO devolvido por ele
                #    (`balanco_orcamentario`, `pncp_orgaos`) precisa chegar à
                #    análise: `analyze` o encontrava e `all` o perdia, sobre o
                #    mesmo dado em disco. Roda com os stubs AINDA no lugar —
                #    restaurá-los antes daqui manda `stage_pncp` à rede de
                #    verdade, e `build_staged` engole a exceção por etapa, então
                #    o teste não falha: só demora minutos e sai pela internet.
                (config.STAGED_DIR / "balanco_orcamentario.csv").write_text(
                    "_ano,DOTACOES\n2021,100\n")
                staged = pipeline.build_staged(2020, 2021, retomar=True)
            finally:
                pipeline.stage_empenhos = quebrado
                for etapa, fn in outras.items():
                    setattr(pipeline, f"stage_{etapa}", fn)
            if empenhos_preservados != 2:
                erros.append(
                    "regressão do dado: uma etapa que levantou apagou o CSV da "
                    "rodada anterior com `_vazio` — 40 mil linhas boas se "
                    f"perdem por um KeyError passageiro (sobraram "
                    f"{empenhos_preservados})")
            if "balanco_orcamentario" not in staged:
                erros.append(
                    "regressão do dado: `balanco_orcamentario` está em disco e "
                    "não chegou ao dicionário da análise — `main.py all` perde "
                    "os achados de orçamento que `main.py analyze` encontra")
        finally:
            (config.RAW_DIR, config.STAGED_DIR, config.DATA_DIR,
             config.CACHE_DIR, checkpoint_antes) = guardado
            if checkpoint_antes is None:
                os.environ.pop("QUEDA_CHECKPOINT", None)
            else:
                os.environ["QUEDA_CHECKPOINT"] = checkpoint_antes
    return erros


def _regressao_chave_empenho() -> list[str]:
    """A chave pagamento → empenho: casa quando deve, e é vetada quando não deve.

    Esta regressão existe por causa de um defeito concreto. O portal publica o
    número do empenho como `NUMERO` no conjunto Empenhos e como
    `NUMERO_EMPENHO` no conjunto Pagamentos; o cruzamento procurava
    `NUMERO_EMPENHO` **nos dois lados**, não achava do lado dos empenhos, caía
    inteiro no par credor + valor e publicava `casados_por_numero: 0`. Daí saiu
    o achado de gravidade alta "64,0% dos pagamentos (291.322 de 455.262) não
    encontram empenho nos dados abertos" — que media a taxa de erro da nossa
    chave, não a conformidade do conselho.

    A fixture principal não pega isso: os empenhos dela não publicam número
    sob rótulo nenhum, então o caminho da chave por número nunca é percorrido.
    Por isso os dois cenários abaixo são montados à mão.
    """
    from .checks import empenhos as E

    anos = range(2013, 2026)
    n_por_ano = 200

    def montar(chave_certa: bool):
        rnd = random.Random(7)
        emp, pg = [], []
        for a in anos:
            for n in range(1, n_por_ano + 1):
                v = 1000.0 + n
                emp.append({"_ano": a, "NUMERO": n, "VALOR": v, "VALOR_PAGO": v,
                            "VALOR_ANULADO": 0.0, "PESSOA": f"FORN {n % 11}",
                            "DATA": pd.Timestamp(f"{a}-03-01")})
                # numeração REINICIA a cada exercício: no cenário torto o
                # pagamento cita um número que existe, mas de outro empenho.
                citado = n if chave_certa else rnd.randint(1, n_por_ano)
                # resto a pagar: 5% quitados no exercício seguinte
                ano_pg = a + 1 if (chave_certa and n % 20 == 0 and a < 2025) else a
                pg.append({"_ano": ano_pg, "NUMERO_EMPENHO": citado, "VALOR": v,
                           "FAVORECIDO_NORM": f"FORN {n % 11}",
                           "NOME_RAZAO_SOCIAL": f"FORN {n % 11}",
                           "DATA": pd.Timestamp(f"{ano_pg}-06-01")})
        return {"empenhos": pd.DataFrame(emp), "pagamentos": pd.DataFrame(pg)}

    erros: list[str] = []

    certos = E._empenho_previo(montar(True))
    m = certos[0].metricas if certos else {}
    if not m.get("casados_por_numero"):
        erros.append(
            "regressão da chave: com `NUMERO` nos empenhos e `NUMERO_EMPENHO` "
            "nos pagamentos apontando para o mesmo empenho, "
            f"casados_por_numero = {m.get('casados_por_numero')} — a chave por "
            "número voltou a não ser tentada")
    if m.get("rotulo_numero_empenhos") != "NUMERO":
        erros.append("regressão da chave: o rótulo do número nos empenhos "
                     f"saiu {m.get('rotulo_numero_empenhos')!r}, esperado 'NUMERO'")
    if (m.get("conferencia_bate_pct") or 0) < E.CONFERENCIA_MINIMA_PCT:
        erros.append("regressão da chave: a conferência pelo VALOR_PAGO "
                     f"reprovou a chave CERTA ({m.get('conferencia_bate_pct')}%)")

    tortos = E._empenho_previo(montar(False))
    ids = {f.id for f in tortos}
    mt = tortos[0].metricas if tortos else {}
    if "empenhos-chave-nao-casa" not in ids:
        erros.append(
            "regressão da chave: números que apenas COINCIDEM (sequência que "
            "reinicia por exercício) passaram pela conferência de VALOR_PAGO "
            f"e não viraram diagnóstico — saiu {sorted(ids)}")
    if mt.get("casados_por_numero"):
        erros.append("regressão da chave: casamento por coincidência não foi "
                     f"descartado ({mt.get('casados_por_numero')} casados)")

    return erros


def run() -> int:
    common._FICHAS = dict(_FICHAS_FIXTURE)   # evita rede no selftest
    # A saída do selftest é FIXTURE e vai para uma subpasta própria. Escrevendo
    # em `config.OUTPUT_DIR` direto, ele sobrescrevia o report.html da rodada
    # real, apagava `output/csv/` inteiro (report.gerar faz rmtree) e acrescentava
    # uma "rodada" falsa ao histórico — que o relatório seguinte usaria como
    # base do "o que mudou desde a rodada anterior". No Colab, com OUTPUT_DIR
    # apontando para o Drive, isso destrói o resultado do operador.
    saida_real = config.OUTPUT_DIR
    config.OUTPUT_DIR = saida_real / "_selftest"
    try:
        staged = _staged()
        achados = run_all(staged)
        ids = {f.id for f in achados}
        faltando = [e for e in ESPERADOS if e not in ids]
        metricas_erradas = _confere_metricas(achados)
        report.gerar(achados, staged)
    finally:
        saida_selftest, config.OUTPUT_DIR = config.OUTPUT_DIR, saida_real

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
    html = (saida_selftest / "report.html")
    assert html.exists() and html.stat().st_size > 100_000, "report.html não gerado"
    print(f"\nOK — todos os {len(ESPERADOS)} cenários esperados foram detectados; "
          f"report em {html}")
    return 0
