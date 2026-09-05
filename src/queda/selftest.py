"""Selftest offline: valida o pipeline analítico inteiro sem tocar a rede.

Gera fixtures que imitam os schemas reais das APIs (Implanta, Dados Abertos,
PNCP, BrasilAPI), injeta cenários de irregularidade conhecidos e confere que
cada check os detecta e que o report é gerado.
"""
from __future__ import annotations

import os
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

    # Contratações (editais) no PNCP. As duas primeiras são as compras de
    # origem dos contratos acima (`numeroControlePNCPCompra`): uma por pregão,
    # outra por inexigibilidade — é o que dá modalidade de origem ao contrato.
    # A terceira é uma dispensa sem contrato ligado.
    licitacoes_pncp = pd.DataFrame([
        {"numeroControlePNCP": "00000000000191-1-000123/2023", "anoCompra": 2023,
         "modalidadeId": 6, "modalidadeNome": "Pregão - Eletrônico",
         "objeto": "instalação elétrica da sede", "valorTotalEstimado": 2_500_000.0,
         "valorTotalHomologado": 2_400_000.0, "amparoLegalNome": "Lei 14.133/2021, Art. 28, I",
         "situacaoCompraNome": "Divulgada no PNCP", "_ano": 2023},
        {"numeroControlePNCP": "00000000000191-1-000456/2023", "anoCompra": 2023,
         "modalidadeId": 9, "modalidadeNome": "Inexigibilidade",
         "objeto": "reforma da cobertura", "valorTotalEstimado": 1_800_000.0,
         "valorTotalHomologado": 1_800_000.0, "amparoLegalNome": "Lei 14.133/2021, Art. 74, I",
         "situacaoCompraNome": "Divulgada no PNCP", "_ano": 2023},
        {"numeroControlePNCP": "00000000000191-1-000789/2024", "anoCompra": 2024,
         "modalidadeId": 8, "modalidadeNome": "Dispensa",
         "objeto": "manutenção de elevadores", "valorTotalEstimado": 90_000.0,
         "valorTotalHomologado": 88_000.0, "amparoLegalNome": "Lei 14.133/2021, Art. 75, II",
         "situacaoCompraNome": "Divulgada no PNCP", "_ano": 2024},
    ])

    # CEIS/CNEP. ALFA SERVICOS (99888777000155) recebe em março/2024 e está
    # INIDÔNEA em 2024 → efeito nacional, achado alto. DELTA COMERCIO
    # (_CNPJ_BAIXADO) recebe R$ 120 mil em 10/05/2023 com IMPEDIMENTO aplicado
    # por um município → contexto, não achado. A terceira linha é sanção fora
    # de qualquer data de pagamento.
    sancoes = pd.DataFrame([
        {"cadastro": "ceis", "documento": "99888777000155", "nome": _FORN_FRACIONA,
         "categoria": "Declaração de Inidoneidade", "inicio": pd.Timestamp("2024-01-01"),
         "fim": pd.Timestamp("2024-12-31"), "orgao_sancionador": "MINISTERIO X",
         "esfera": "FEDERAL", "uf_orgao": "DF", "fundamento": "Lei 8.666, art. 87, IV",
         "efeito_nacional": True},
        {"cadastro": "ceis", "documento": _CNPJ_BAIXADO, "nome": "DELTA COMERCIO LTDA",
         "categoria": "Impedimento de licitar e contratar", "inicio": pd.Timestamp("2023-01-01"),
         "fim": pd.Timestamp("2023-12-31"), "orgao_sancionador": "PREFEITURA MUNICIPAL DE Y",
         "esfera": "MUNICIPAL", "uf_orgao": "SP", "fundamento": "Lei 14.133, art. 156, III",
         "efeito_nacional": False},
        {"cadastro": "cnep", "documento": "77888999000122", "nome": "CONSTRUTORA NOVA SEDE",
         "categoria": "Multa", "inicio": pd.Timestamp("2015-01-01"),
         "fim": pd.Timestamp("2015-06-30"), "orgao_sancionador": "CGU",
         "esfera": "FEDERAL", "uf_orgao": "DF", "fundamento": "Lei 12.846",
         "efeito_nacional": False},
    ])

    # Comparação entre Creas: 6 conselhos × 2 exercícios, mesmas contas. SP com
    # a maior parcela em verbas (diárias + jetons), para o ranking ter um 1º.
    _bench = []
    _perfis = {"SP": (60e6, 30e6), "MG": (30e6, 6e6), "RJ": (25e6, 5e6),
               "PR": (15e6, 3e6), "RS": (14e6, 2e6), "BA": (10e6, 1.5e6)}
    for uf, (folha, verbas) in _perfis.items():
        for ano in (2023, 2024):
            _bench += [
                {"_uf": uf, "_host": f"crea-{uf.lower()}", "_ano": ano,
                 "NOME_CONTA": "Vencimentos e Vantagens Fixas", "VALOR_PAGO": folha},
                {"_uf": uf, "_host": f"crea-{uf.lower()}", "_ano": ano,
                 "NOME_CONTA": "Diárias no País", "VALOR_PAGO": verbas * 0.6},
                {"_uf": uf, "_host": f"crea-{uf.lower()}", "_ano": ano,
                 "NOME_CONTA": "Jetons a Conselheiros", "VALOR_PAGO": verbas * 0.4},
                {"_uf": uf, "_host": f"crea-{uf.lower()}", "_ano": ano,
                 "NOME_CONTA": "Serviços de Terceiros PJ", "VALOR_PAGO": folha * 0.5},
            ]
    benchmark_creas = pd.DataFrame(_bench)

    # atas de registro de preços do PNCP (`/v1/atas`), projeção de
    # pipeline._pncp_ata_linha. Duas atas do exercício 2024, nenhuma
    # correspondente às atas do portal — o cruzamento tem de apontá-las.
    pncp_atas = pd.DataFrame([
        {"orgao_cnpj": "00000000000191", "orgao_papel": "alvo",
         "numeroControlePNCPAta": "00000000000191-1-000001/2024-000001",
         "numeroAtaRegistroPreco": "001", "anoAta": 2024,
         "numeroControlePNCPCompra": "00000000000191-1-000001/2024",
         "dataAssinatura": "2024-02-01", "vigenciaInicio": "2024-02-01",
         "vigenciaFim": "2025-01-31", "dataPublicacaoPncp": "2024-02-02",
         "objetoContratacao": "registro de preços de material de expediente",
         "cancelado": False, "_ano": 2024},
        {"orgao_cnpj": "00000000000191", "orgao_papel": "alvo",
         "numeroControlePNCPAta": "00000000000191-1-000002/2024-000001",
         "numeroAtaRegistroPreco": "002", "anoAta": 2024,
         "numeroControlePNCPCompra": "00000000000191-1-000002/2024",
         "dataAssinatura": "2024-05-10", "vigenciaInicio": "2024-05-10",
         "vigenciaFim": "2025-05-09", "dataPublicacaoPncp": "2024-05-11",
         "objetoContratacao": "registro de preços de passagens aéreas",
         "cancelado": False, "_ano": 2024},
    ])

    return {
        "pagamentos": _pagamentos(), "empenhos": empenhos, "contratos": contratos,
        "aditivos": aditivos, "licitacoes": pd.DataFrame(), "convenios": convenios,
        "comparativo_despesa": comparativo, "despesas_centro_custo": centro_custo,
        "comparativo_receita": receita, "balanco_orcamentario": balanco,
        "demonstrativo": demonstrativo,
        "atas": atas, "quadro_pessoal": quadro,
        "mandatarios": mandatarios, "indenizacoes_endpoint": pd.DataFrame(),
        "pncp_contratos": pncp, "pncp_cobrancas": pd.DataFrame(),
        "licitacoes_pncp": licitacoes_pncp, "sancoes": sancoes,
        "pncp_atas": pncp_atas,
        "benchmark_creas": benchmark_creas,
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
    # A fixture não monta `processos_itens` (o payload tem ITENS aninhados e não
    # sobrevive a CSV), então o universo esperado tem 17 etapas e ela cobre 16.
    # O achado ter de aparecer AQUI é o teste: sem ele, uma rodada que abortou
    # na terceira etapa publicaria "Coleta completa: 3 de 3 datasets".
    "diagnostico-etapas-nao-alcancadas",
    "contratos-fora-do-pncp-convivencia",  # 2021-2023: contexto, não acusação
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
    "licitacoes-pncp-rol",             # o rol que o portal não publica está no PNCP
    "contratos-modalidade-de-origem",  # pregão × dispensa × inexigibilidade, por contrato
    "fornecedores-sancionados",        # pago durante inidoneidade (efeito nacional)
    "fornecedores-sancionados-outro-orgao",  # impedimento de outro ente: contexto
    "benchmark-cobertura",             # quantos Creas responderam
    "benchmark-despesa-por-profissional",
    "benchmark-verbas-share",
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
    # PNCP × portal: mesmo que o CNPJ chegue SEM o zero à esquerda (int que
    # passou por CSV), o check tem de recompor e casar — é a defesa em
    # profundidade para a coluna que a lista de texto não previu.
    from .checks import contratos as _C
    _ctr = pd.DataFrame([{"NUMERO_CONTRATO": "C-031/2025", "CONTRATADA": "00.000.424/0001-56 - DEBRITO",
                          "CONTRATADA_CNPJ": "00000424000156", "CONTRATADA_NORM": "DEBRITO",
                          "VALOR": 14_000_000.0, "INI": pd.Timestamp("2025-03-01"),
                          "FIM": pd.Timestamp("2026-03-01"), "DATA_INICIO_VIGENCIA": "2025-03-01"}])
    _pncp = pd.DataFrame([{"orgao_papel": "alvo", "fornecedor_ni": "424000156",   # <- sem zeros
                           "fornecedor_nome": "DEBRITO LTDA", "fornecedor_norm": "DEBRITO LTDA",
                           "numeroContrato": "31/2025", "objeto": "x", "valorGlobal": 14_000_000.0,
                           "valorAcumulado": 0.0, "vigenciaInicio": "2025-03-01", "vigenciaFim": "2026-03-01"}])
    _ids = {f.id for f in _C.run({"contratos": _ctr, "aditivos": pd.DataFrame(),
                                  "pagamentos": pd.DataFrame(), "pncp_contratos": _pncp})}
    if "contratos-ausentes-pncp" in _ids:
        erros.append("contratos-ausentes-pncp: CNPJ do PNCP sem zero à esquerda "
                     "(424000156) não casou com 00000424000156 — o contrato que "
                     "ESTÁ no PNCP saiu como ausente. Foi assim que 25 de 39 "
                     "'ausentes' da rodada real eram falsos")

    # PNCP × portal: o casamento é por CNPJ e o corte é a data da OBRIGAÇÃO.
    # Antes o cruzamento era por NOME e cortava em 2021: o mesmo fornecedor com
    # duas razões sociais virava "ausente do PNCP" estando lá, e contrato
    # firmado sob a Lei 8.666 — que não tinha de ir ao PNCP — entrava como
    # falha de transparência. Na carteira real é onde está o dinheiro grande:
    # R$ 70,8 mi de uma ATA de 2022 encabeçavam a lista de acusações.
    f = por_id.get("contratos-ausentes-pncp")
    if f is not None:
        if not str(f.metricas.get("criterio_de_casamento", "")).startswith("CNPJ do fornecedor"):
            erros.append("contratos-ausentes-pncp: casou por "
                         f"{f.metricas.get('criterio_de_casamento')!r} — com CNPJ "
                         "nos dois lados, casar por nome cria ausência falsa")
        if f.metricas.get("corte_da_obrigacao") != "2023-12-30":
            erros.append("contratos-ausentes-pncp: o corte saiu "
                         f"{f.metricas.get('corte_da_obrigacao')!r}, esperado "
                         "'2023-12-30' (Lei 14.133, art. 193, II)")
    if "contratos-fora-do-pncp-convivencia" not in por_id:
        erros.append("o período de convivência com a Lei 8.666 deixou de sair "
                     "como contexto — sem ele, a ausência no PNCP de contrato "
                     "que a lei da época não mandava publicar lá volta a ser "
                     "publicada como falha")
    elif por_id["contratos-fora-do-pncp-convivencia"].gravidade != "info":
        erros.append("contratos-fora-do-pncp-convivencia saiu como "
                     f"{por_id['contratos-fora-do-pncp-convivencia'].gravidade} "
                     "— é contexto declarado, não achado")

    # PNCP: dois contratos ligados à compra de origem; a inexigibilidade (1,8 mi)
    # é 42,9% do valor dos dois (4,2 mi) — abaixo de 50%, gravidade info.
    exige("contratos-modalidade-de-origem", "contratos_com_origem", 2)
    exige("contratos-modalidade-de-origem", "valor_contratacao_direta", 1_800_000)
    exige("licitacoes-pncp-rol", "contratacoes", 3)
    # Sanções: só ALFA cai em inidoneidade dentro da janela; REFORMA TOTAL é
    # impedimento de outro ente e NÃO pode entrar no achado alto.
    exige("fornecedores-sancionados", "fornecedores", 1)
    f = por_id.get("fornecedores-sancionados")
    if f is not None and f.gravidade != "alta":
        erros.append(f"fornecedores-sancionados saiu {f.gravidade}, esperado alta "
                     "(inidoneidade vale para toda a administração)")
    f = por_id.get("fornecedores-sancionados-outro-orgao")
    if f is not None and f.gravidade != "info":
        erros.append("fornecedores-sancionados-outro-orgao saiu "
                     f"{f.gravidade} — impedimento de OUTRO ente é contexto, não achado")
    # Benchmark: SP tem a maior parcela em verbas (33% da despesa) → 1º de 6.
    f = por_id.get("benchmark-verbas-share")
    if f is not None and f.metricas.get("posicao") != 1:
        erros.append("benchmark-verbas-share: SP deveria ser 1º em parcela de verbas "
                     f"na fixture, saiu {f.metricas.get('posicao')}º")
    exige("benchmark-despesa-por-profissional", "conselhos", 6)

    exige("diagnostico-etapas-nao-alcancadas", "etapas_esperadas", 21)
    exige("diagnostico-etapas-nao-alcancadas", "etapas_nao_alcancadas", 1)
    f = por_id.get("diagnostico-etapas-nao-alcancadas")
    if f is not None and f.metricas.get("quais") != "processos_itens":
        erros.append("diagnostico-etapas-nao-alcancadas.quais = "
                     f"{f.metricas.get('quais')!r}, esperado 'processos_itens' "
                     "— se outra etapa sumiu da fixture, é regressão dela")
    erros.extend(_regressao_dado_nao_some())
    erros.extend(_regressao_recusa_do_portal())
    erros.extend(_regressao_coleta_converge())
    erros.extend(_regressao_sondagem_e_orgaos())
    erros.extend(_regressao_prefixo_e_nome())
    erros.extend(_regressao_novas_fontes())
    from .regressoes import http_config as _reg_http, rodada11 as _reg_r11, rodada12 as _reg_r12
    erros.extend(_reg_http.run())
    erros.extend(_reg_r11.run())
    erros.extend(_reg_r12.run())

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


def _regressao_recusa_do_portal() -> list[str]:
    """Janela que o portal RECUSA não pode ser repetida em toda rodada.

    Medido na rodada real de 04/09/2026, sobre o Drive do operador: o portal
    devolve HTTP 400 para o exercício 2026 em ONZE endpoints, e para 2010-2020
    no DespesasCentroCusto. Como 400 (corretamente) não vira checkpoint, o
    inventário anunciava essas janelas como pendentes PARA SEMPRE e cada rodada
    gastava ~25 requisições que nunca dariam certo — num host que responde 403
    por rajada, e que abortou aquela rodada.

    A recusa é anotada com validade curta: dentro dela a rodada seguinte pula a
    janela; passada, tenta de novo, porque a recusa pode ter sido do NOSSO
    formato de parâmetro. O que ela nunca pode virar é `.vazio`, que afirmaria
    "o órgão não publica este exercício".
    """
    import os
    import tempfile
    from pathlib import Path
    from . import clients, pipeline

    erros: list[str] = []
    guardado = (config.RAW_DIR, config.STAGED_DIR, config.DATA_DIR,
                config.CACHE_DIR, os.environ.get("QUEDA_CHECKPOINT"),
                clients.implanta_ano)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        try:
            os.environ["QUEDA_CHECKPOINT"] = "1"
            config.DATA_DIR = base
            config.CACHE_DIR = base / "cache"
            config.RAW_DIR = base / "raw"
            config.STAGED_DIR = base / "staged"
            config.STAGED_DIR.mkdir(parents=True)

            rede: list[int] = []

            def portal(endpoint, ano, **kw):
                rede.append(ano)
                if ano >= 2026:
                    return None                    # 400: pergunta não aceita
                if ano <= 2011:
                    return []                      # servidor: nada neste ano
                return [{"NUMERO": ano, "VALOR": 1.0,
                         "DATA_EMPENHO": f"01/01/{ano}"}]

            clients.implanta_ano = portal
            pipeline.stage_empenhos(None, 2026)
            if 2026 not in rede:
                erros.append("regressão da recusa: a primeira rodada nem "
                             "tentou o exercício recusado")
            rede.clear()
            pipeline.stage_empenhos(None, 2026)
            if rede:
                erros.append(
                    "regressão da recusa: a segunda rodada voltou à rede pelas "
                    f"janelas {rede} que o portal já recusou — cada rodada "
                    "queima ritmo em pergunta que não vai ser aceita")

            _csv, _jsn, vazio, marcador = pipeline._checkpoint_arquivos(
                "empenhos", "2026")
            if not marcador.exists():
                erros.append("regressão da recusa: o marcador `.recusado` não "
                             "foi gravado")
            if vazio.exists():
                erros.append(
                    "regressão da recusa: a recusa virou `.vazio` — isso "
                    "AFIRMA que o órgão não publica o exercício, a partir de "
                    "um 400 que pode ser do nosso formato de parâmetro")
            if pipeline._janela_estado("empenhos", "2026", 2026) != "recusado":
                erros.append("regressão da recusa: o estado da janela recusada "
                             f"saiu {pipeline._janela_estado('empenhos', '2026', 2026)!r}")

            inv = pipeline.inventario(None, 2026)
            linha = inv[inv["etapa"] == "empenhos"].iloc[0]
            if linha["vai_a_rede"] or linha["janelas_recusadas"] != 1:
                erros.append(
                    "regressão da recusa: o inventário ainda manda a etapa à "
                    f"rede (vai_a_rede={linha['vai_a_rede']}, "
                    f"recusadas={linha['janelas_recusadas']}) — `faltando` e a "
                    "coleta nunca convergem")
            if str(linha["recusados_pelo_portal"]) != "2026":
                erros.append(
                    "regressão da recusa: o inventário não nomeia o exercício "
                    f"recusado ({linha['recusados_pelo_portal']!r}) — o "
                    "operador não tem como distinguir 'coletado' de 'o portal "
                    "não aceita a pergunta'")
        finally:
            (config.RAW_DIR, config.STAGED_DIR, config.DATA_DIR,
             config.CACHE_DIR, checkpoint_antes, clients.implanta_ano) = guardado
            if checkpoint_antes is None:
                os.environ.pop("QUEDA_CHECKPOINT", None)
            else:
                os.environ["QUEDA_CHECKPOINT"] = checkpoint_antes
    return erros


def _regressao_novas_fontes() -> list[str]:
    """CEIS/CNEP e efeito da sanção — o que não pode quebrar em silêncio.

    Nada disto foi testado contra a rede na sessão em que foi escrito. O que
    fica trancado é o contrato: o leitor de ZIP/CSV resolve colunas com acento
    e caixa variável, recua dia a dia até achar publicação, separa `None`
    (rede) de `[]` (sem coluna de documento), e a regra de efeito nacional
    distingue inidoneidade de impedimento.
    """
    import io as _io
    import zipfile
    from datetime import date
    from . import clients

    erros: list[str] = []

    # 1. Colunas por trecho normalizado, com acento e ordem diferentes.
    mapa = clients._resolver_colunas_sancao([
        "TIPO DE PESSOA", "CPF OU CNPJ DO SANCIONADO", "NOME DO SANCIONADO",
        "CATEGORIA DA SANÇÃO", "DATA INÍCIO SANÇÃO", "DATA FINAL SANÇÃO",
        "ÓRGÃO SANCIONADOR", "ESFERA ÓRGÃO SANCIONADOR", "FUNDAMENTAÇÃO LEGAL"])
    esperado = {"documento": "CPF OU CNPJ DO SANCIONADO", "categoria": "CATEGORIA DA SANÇÃO",
                "inicio": "DATA INÍCIO SANÇÃO", "fim": "DATA FINAL SANÇÃO",
                "orgao": "ÓRGÃO SANCIONADOR", "esfera": "ESFERA ÓRGÃO SANCIONADOR",
                "fundamento": "FUNDAMENTAÇÃO LEGAL"}
    for k, v in esperado.items():
        if mapa.get(k) != v:
            erros.append(f"sanções: coluna {k!r} resolvida como {mapa.get(k)!r}, esperado {v!r}")

    # 2. Recuo de data: 404 nos dois primeiros dias, ZIP no terceiro.
    csv = ("TIPO DE PESSOA;CPF OU CNPJ DO SANCIONADO;NOME DO SANCIONADO;CATEGORIA DA SANÇÃO;"
           "DATA INÍCIO SANÇÃO;DATA FINAL SANÇÃO;ÓRGÃO SANCIONADOR;ESFERA ÓRGÃO SANCIONADOR;"
           "FUNDAMENTAÇÃO LEGAL\n"
           "J;99.888.777/0001-55;ALFA;Declaração de Inidoneidade;01/01/2024;31/12/2024;"
           "MIN X;FEDERAL;Lei 8.666 art. 87 IV\n")
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("20260902_CEIS.csv", csv.encode("latin-1"))
    pedidos: list[str] = []

    def _bytes(url, params=None, **kw):
        pedidos.append(url)
        return buf.getvalue() if url.endswith("20260902") else None

    from . import http
    orig = http.get_bytes
    http.get_bytes = _bytes
    try:
        linhas, meta = clients.baixar_sancoes("ceis", hoje=date(2026, 9, 4))
    finally:
        http.get_bytes = orig
    if len(pedidos) != 3 or not pedidos[-1].endswith("20260902"):
        erros.append(f"sanções: o recuo de data não parou na primeira publicação ({pedidos})")
    if not linhas or linhas[0].get("documento") != "99888777000155":
        erros.append(f"sanções: o CSV do ZIP não foi lido/normalizado ({linhas[:1]})")
    if meta.get("data") != "2026-09-02":
        erros.append(f"sanções: meta.data = {meta.get('data')!r}, esperado 2026-09-02")

    # 3. Rede caída em todas as datas → None (indisponível), nunca [].
    http.get_bytes = lambda url, params=None, **kw: (_ for _ in ()).throw(RuntimeError("rede"))
    try:
        linhas, _ = clients.baixar_sancoes("ceis", hoje=date(2026, 9, 4))
    finally:
        http.get_bytes = orig
    if linhas is not None:
        erros.append("sanções: rede caída devolveu lista em vez de None — a frente "
                     "publicaria 'nenhum fornecedor sancionado' sem ter conferido")

    # 4. Efeito da sanção.
    casos = [("Declaração de Inidoneidade", None, True),
             ("Impedimento de licitar e contratar", None, False),
             ("Suspensão temporária", "Lei 8.666 art. 87 III", False),
             ("Proibição de contratar com o Poder Público", None, True),
             ("Multa", "Lei 12.846", False),
             ("Publicação Extraordinária", "Lei 12.846 art. 6º II", False)]
    for cat, fund, esperado_ in casos:
        if clients.efeito_nacional(cat, fund) != esperado_:
            erros.append(f"sanções: efeito_nacional({cat!r}) deveria ser {esperado_}")
    if clients.alcance_sancao("Multa", "Lei 12.846", "FEDERAL") != "nao_impede":
        erros.append("sanções: multa do CNEP (Lei 12.846) saiu como "
                     f"{clients.alcance_sancao('Multa', 'Lei 12.846', 'FEDERAL')!r} — "
                     "multa não restringe contratar, o alcance é 'nao_impede'")
    return erros


def _regressao_prefixo_e_nome() -> list[str]:
    """O que a documentação pública da API ensinou, trancado.

    A captura da página Stoplight (confea-br.implanta.net.br/transparencia/)
    mostrou três coisas: o exemplo cURL chama `/transparencia/api/...` e nós
    chamamos `/portalTransparencia/api/v1.0/...`; "Rol de Mandatários" é um
    endpoint próprio e nós chamávamos `Conselheiros`; Quadros de Pessoas não
    declara parâmetro de consulta. As peças abaixo agem sobre isso sem poder
    tocar o portal (o proxy desta sessão bloqueia o host), então o teste fixa
    o CONTRATO das funções — o que a rodada no Colab vai confirmar ou refutar.
    """
    from . import clients, http

    erros: list[str] = []
    guardado = (clients.get_json, http.get_text, dict(clients._PREFIXO_APRENDIDO),
                dict(clients._NOME_APRENDIDO), dict(clients._FORMATO_APRENDIDO))
    host = config.IMPLANTA_HOST
    try:
        # 1. Prefixo: vazio no antigo, dado no documentado → aprende; o controle
        #    (Empenhos) responde no primeiro e não paga requisição extra.
        chamadas: list[str] = []
        dados = {"/transparencia/api/v1.0/QuadrosPessoas":
                 [{"TITULO": "Efetivos", "PESSOAS": [{"NOME": "A"}]}],
                 "/portalTransparencia/api/v1.0/Empenhos": [{"NUMERO": 1}]}

        def _fake(url, params=None, **kw):
            caminho = url[len(host):]
            chamadas.append(caminho)
            if caminho in dados:
                return dados[caminho]
            return [] if caminho.endswith("/QuadrosPessoas") else None

        clients.get_json = _fake
        clients._PREFIXO_APRENDIDO.clear()
        r = clients.implanta("QuadrosPessoas")
        if not r or clients._PREFIXO_APRENDIDO.get("QuadrosPessoas") != "/transparencia/api/v1.0":
            erros.append("prefixo: QuadrosPessoas vazio no prefixo antigo não foi "
                         "buscado no documentado — o dado que existe em outro "
                         "caminho continua saindo como 'o órgão não publica'")
        chamadas.clear()
        clients.implanta("QuadrosPessoas")
        if chamadas != ["/transparencia/api/v1.0/QuadrosPessoas"]:
            erros.append(f"prefixo: a segunda chamada não foi direto ao aprendido ({chamadas})")
        chamadas.clear()
        clients.implanta("Empenhos", {"referenciaInicio": "01/2025"})
        if chamadas != ["/portalTransparencia/api/v1.0/Empenhos"]:
            erros.append(f"prefixo: Empenhos pagou prefixo extra ({chamadas}) — são "
                         "14 janelas × 11 etapas de requisições a mais por rodada")
        if clients.implanta("Inexistente") is not None:
            erros.append("prefixo: endpoint que ninguém aceita devolveu lista em vez "
                         "de None — viraria checkpoint `.vazio`")

        # 2. O cenário REAL, confirmado pelo cURL da documentação do Crea-SP:
        #    `Conselheiros` é o nome certo; responde vazio no prefixo antigo e
        #    com dado no documentado, SEM parâmetro. Tem de sair resolvido na
        #    primeira passada (sem parâmetro) e com o prefixo aprendido.
        chamadas.clear()

        def _real(url, params=None, **kw):
            caminho = url[len(host):]
            chamadas.append((caminho, dict(params or {})))
            if caminho == "/transparencia/api/v1.0/Conselheiros" and not params:
                return [{"nomeMandatario": "X", "cargo": "Presidente"}]
            return [] if caminho.endswith("/Conselheiros") else None

        clients.get_json = _real
        clients._PREFIXO_APRENDIDO.clear()
        clients._NOME_APRENDIDO.clear()
        linhas, nome = clients.implanta_primeiro_nome(
            config.ENDPOINT_CANDIDATOS["mandatarios"], 2025)
        if nome != "Conselheiros" or not linhas:
            erros.append(f"nome: Conselheiros com dado no prefixo documentado não "
                         f"foi resolvido ({nome}) — mandatários seguem vazios")
        if any(p for _, p in chamadas):
            erros.append("nome: mandou parâmetro antes de tentar sem — a "
                         "documentação não declara nenhum, e cada variante custa "
                         "três requisições")
        if clients._PREFIXO_APRENDIDO.get("Conselheiros") != "/transparencia/api/v1.0":
            erros.append("nome: o prefixo documentado não ficou aprendido para "
                         "Conselheiros")
        # Nome que responde 200 [] : o resultado é [] (respondeu), não None, e
        # NENHUMA variante roda — o ledger da rodada 10 mostrou que Conselheiros
        # e QuadrosPessoas ignoram parâmetro (10 requisições e 110 s por nada).
        # As variantes só rodam sob QUEDA_VARIANTES=1, e aí só no que respondeu.
        clients.get_json = lambda url, params=None, **kw: (
            [] if url.endswith("/Conselheiros") else None)
        clients._NOME_APRENDIDO.clear()
        clients._PREFIXO_APRENDIDO.clear()
        tentados: list[str] = []
        orig_var = clients.implanta_variantes
        clients.implanta_variantes = lambda ep, ano=None, **kw: (tentados.append(ep) or ([], {}))
        try:
            linhas, nome = clients.implanta_primeiro_nome(
                config.ENDPOINT_CANDIDATOS["mandatarios"], 2025)
        finally:
            clients.implanta_variantes = orig_var
        if (linhas, nome) != ([], None) or tentados:
            erros.append(f"nome: com 200 [] saiu {(linhas, nome)} e as variantes "
                         f"rodaram em {tentados} — 200 vazio é resposta, não pede variante")
        variantes_antes = os.environ.get("QUEDA_VARIANTES")
        os.environ["QUEDA_VARIANTES"] = "1"
        clients._NOME_APRENDIDO.clear()
        clients._PREFIXO_APRENDIDO.clear()
        tentados.clear()
        clients.implanta_variantes = lambda ep, ano=None, **kw: (tentados.append(ep) or ([], {}))
        try:
            linhas, nome = clients.implanta_primeiro_nome(
                config.ENDPOINT_CANDIDATOS["mandatarios"], 2025)
        finally:
            clients.implanta_variantes = orig_var
            if variantes_antes is None:
                os.environ.pop("QUEDA_VARIANTES", None)
            else:
                os.environ["QUEDA_VARIANTES"] = variantes_antes
        if (linhas, nome) != ([], None) or tentados != ["Conselheiros"]:
            erros.append(f"nome: sob QUEDA_VARIANTES=1 saiu {(linhas, nome)} e as variantes "
                         f"rodaram em {tentados} — deviam rodar só no que respondeu")

        # 3. A URL do contrato sai do HTML da documentação, relativa ou absoluta.
        html = ('<elements-api apiDescriptionUrl="/transparencia/api/v1.0/swagger.json" '
                'router="hash"></elements-api>')
        u = clients.extrair_api_description_url(html, "https://confea-br.implanta.net.br/transparencia/")
        if u != "https://confea-br.implanta.net.br/transparencia/api/v1.0/swagger.json":
            erros.append(f"doc: apiDescriptionUrl relativa não resolveu ({u})")
        if clients.extrair_api_description_url("<html>nada</html>", "https://x.test/") is not None:
            erros.append("doc: página sem contrato devolveu uma URL inventada")

        # 4. A medição de prefixos separa vazio, dado, sem-JSON e recusado.
        def _texto(url, params=None):
            if "/transparencia/api/v1.0/QuadrosPessoas" in url:
                return (200, '[{"TITULO":"E","PESSOAS":[{}]}]')
            if url.endswith("/QuadrosPessoas"):
                return (200, "[]")
            if "Empenhos" in url and "/portalTransparencia/" in url:
                return (200, '[{"a":1},{"a":2}]')
            if "Diarias" in url:
                return (403, "<html>Forbidden</html>")
            return (404, "")

        http.get_text = _texto
        por = {(r["endpoint"], r["prefixo"]): r for r in clients.medir_prefixos(2025)}
        esperado = {("QuadrosPessoas", "/portalTransparencia/api/v1.0"): "0",
                    ("QuadrosPessoas", "/transparencia/api/v1.0"): "1",
                    ("Empenhos (controle)", "/portalTransparencia/api/v1.0"): "2"}
        for k, v in esperado.items():
            if por.get(k, {}).get("itens") != v:
                erros.append(f"medição: {k} deu {por.get(k, {}).get('itens')!r}, esperado {v!r}")
        if por.get(("DiariasDeslocamentos", "/transparencia/api/v1.0"), {}).get("status") != 403:
            erros.append("medição: o 403 das diárias não apareceu como status")

        # 5. 5xx esgotado num prefixo de RESERVA não derruba a etapa. Na rodada
        #    de 04/09 o terceiro prefixo (hoje removido) respondia 500 para tudo,
        #    o RuntimeError subia e "quadro_pessoal" e "indenizacoes_endpoint"
        #    saíram como "etapa falhou com erro" — sem o prefixo que serve ter
        #    sido tentado. Só sobe se TODOS caírem.
        def _500_na_reserva(url, params=None, **kw):
            if "/transparencia/api/v1.0/" in url:
                raise RuntimeError("HTTP 500")
            return []                      # primário responde vazio
        clients.get_json = _500_na_reserva
        clients._PREFIXO_APRENDIDO.clear()
        try:
            r = clients.implanta("QuadrosPessoas")
        except RuntimeError:
            r = "RAISE"
        if r != []:
            erros.append(f"prefixo: 500 na reserva derrubou a etapa ({r!r}) — o "
                         "primário tinha respondido vazio")
        clients.get_json = lambda url, params=None, **kw: (_ for _ in ()).throw(RuntimeError("500"))
        try:
            clients.implanta("QuadrosPessoas")
            erros.append("prefixo: todos os prefixos fora do ar e a etapa NÃO "
                         "sinalizou indisponibilidade — viraria 'não coletado' "
                         "silencioso")
        except RuntimeError:
            pass
    finally:
        (clients.get_json, http.get_text, pref, nomes, fmt) = guardado
        clients._PREFIXO_APRENDIDO.clear(); clients._PREFIXO_APRENDIDO.update(pref)
        clients._NOME_APRENDIDO.clear(); clients._NOME_APRENDIDO.update(nomes)
        clients._FORMATO_APRENDIDO.clear(); clients._FORMATO_APRENDIDO.update(fmt)
    return erros


def _regressao_sondagem_e_orgaos() -> list[str]:
    """Descobrir em vez de adivinhar — e não aceitar órgão errado no caminho.

    Da leitura do ledger HTTP da rodada de 04/09/2026: QuadrosPessoas e
    Conselheiros aceitaram os cinco formatos de parâmetro e devolveram vazio
    para todos; `servicos.creasp.org.br` foi o único host de Dados Abertos a
    responder 200, e o corpo (não-JSON) foi descartado sem ninguém ver; a
    busca do Confea no PNCP devolveu 1 resultado que não passou, e a da Mútua
    devolveu zero. As peças abaixo atacam cada um desses pontos, e a última
    fecha um buraco que a solução do Confea teria ABERTO.
    """
    import os
    import tempfile
    from pathlib import Path
    from . import clients, http

    erros: list[str] = []

    # 1. O leitor de contrato de API: Swagger 2 e OpenAPI 3, herdando os
    #    parâmetros comuns do caminho e ignorando os de corpo.
    v2 = {"swagger": "2.0", "basePath": "/api/v1.0", "paths": {
        "/QuadrosPessoas": {"get": {"parameters": [
            {"name": "competencia", "in": "query", "required": True},
            {"name": "idUnidade", "in": "query"}]}},
        "/Empenhos": {"parameters": [{"name": "referenciaInicio", "in": "query"}],
                      "get": {"parameters": [{"name": "referenciaTermino", "in": "query"},
                                             {"name": "body", "in": "body"}]}}}}
    v3 = {"openapi": "3.0.1", "paths": {"/api/DadosAbertos/mandatarios": {
        "get": {"parameters": [{"name": "exercicio", "in": "query"}]}}}}
    por = {r["caminho"]: r for r in clients.resumir_openapi(v2) if r["metodo"] == "GET"}
    q = por.get("/api/v1.0/QuadrosPessoas", {})
    if q.get("query") != ["competencia", "idUnidade"] or q.get("obrigatorios") != ["competencia"]:
        erros.append(f"sondagem: leitor de swagger v2 errou QuadrosPessoas: {q}")
    e = por.get("/api/v1.0/Empenhos", {})
    if e.get("query") != ["referenciaInicio", "referenciaTermino"]:
        erros.append("sondagem: leitor de swagger não herdou o parâmetro comum do "
                     f"caminho ou aceitou 'in: body': {e}")
    if [r["query"] for r in clients.resumir_openapi(v3)] != [["exercicio"]]:
        erros.append("sondagem: leitor de OpenAPI 3 falhou")

    # 2. 200 sem JSON: o corpo tem de ser guardado — é a única pista do que o
    #    host está dizendo.
    raw_orig, sess_orig, thr_orig = config.RAW_DIR, http.session, http._throttle
    with tempfile.TemporaryDirectory() as tmp:
        try:
            config.RAW_DIR = Path(tmp) / "raw"
            config.RAW_DIR.mkdir(parents=True)

            class _Resp:
                status_code = 200
                headers = {"Content-Type": "text/html"}
                text = "<html><body>Verificando seu navegador</body></html>"

                def json(self):
                    raise ValueError("não é JSON")

            class _Sess:
                def get(self, url, params=None, timeout=None):
                    return _Resp()

            http.session = lambda: _Sess()
            http._throttle = lambda url: None
            r = http.get_json("https://exemplo.invalid/service-api/api/DadosAbertos/x",
                              use_cache=False)
            guardados = list((config.RAW_DIR / "nao-json").glob("*.txt"))
            if r is not None or not guardados:
                erros.append("sondagem: o corpo de um 200 sem JSON não foi guardado "
                             "— a única pista sobre o host se perde de novo")
            elif "Verificando seu navegador" not in guardados[0].read_text(encoding="utf-8"):
                erros.append("sondagem: o arquivo do corpo não-JSON não contém o corpo")
        finally:
            config.RAW_DIR, http.session, http._throttle = raw_orig, sess_orig, thr_orig

    # 3. Resolver órgão no PNCP: CNPJ candidato ERRADO é descartado, o certo
    #    passa, e `termo_busca` chega à busca.
    get_orig, memo_orig = clients.get_json, clients.memo
    chamadas: list = []

    def _pncp(url, params=None, **kw):
        chamadas.append((url, params))
        if url.endswith("/orgaos/99999999000199"):
            return {"cnpj": "99999999000199", "razaoSocial": "PREFEITURA MUNICIPAL DE XYZ"}
        if url.endswith("/orgaos/33665647000191"):
            return {"cnpj": "33665647000191",
                    "razaoSocial": "CONSELHO FEDERAL DE ENGENHARIA E AGRONOMIA"}
        if url.endswith("/orgaos/"):
            return [{"cnpj": "11111111000111",
                     "razaoSocial": "MUTUA DE ASSISTENCIA DOS PROFISSIONAIS DA "
                                    "ENGENHARIA ARQUITETURA E AGRONOMIA"},
                    {"cnpj": "22222222000122", "razaoSocial": "MUTUA DE SEGUROS LTDA"}]
        return None

    try:
        clients.get_json = _pncp
        clients.memo = lambda k, fn: fn()
        o = clients.pncp_resolver_orgao("CONSELHO FEDERAL DE ENGENHARIA E AGRONOMIA",
                                        cnpj_fixo=["99999999000199", "33665647000191"])
        if not o or o.get("cnpj") != "33665647000191":
            erros.append(
                "órgãos: um CNPJ candidato ERRADO foi aceito como Confea "
                f"({o}) — 'FEDERAL' na lista de genéricas zerava os termos "
                "distintivos, e conjunto vazio desligava a conferência. Todo o "
                "bloco PNCP passaria a falar de outro órgão")
        chamadas.clear()
        o = clients.pncp_resolver_orgao(
            "MUTUA DE ASSISTENCIA DOS PROFISSIONAIS DA ENGENHARIA ARQUITETURA E AGRONOMIA",
            termo_busca="mutua")
        if not any(p and p.get("razaoSocial") == "mutua" for _, p in chamadas):
            erros.append("órgãos: `termo_busca` não chegou à busca do PNCP")
        if not o or o.get("cnpj") != "11111111000111":
            erros.append(f"órgãos: a Mútua não foi resolvida pela busca curta ({o}) "
                         "— ou a homônima 'MUTUA DE SEGUROS' passou no lugar")
        # 4. Nome esperado feito só de palavras genéricas não pode virar
        #    "aceita qualquer um".
        if clients._exige_nome("PREFEITURA MUNICIPAL DE XYZ",
                               "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA"):
            erros.append("órgãos: nome esperado sem termo distintivo aceitou um "
                         "órgão qualquer — o conjunto vazio voltou a valer por "
                         "'sem conferência'")
        if not clients._exige_nome("CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA - CREA",
                                   "CONSELHO REGIONAL DE ENGENHARIA E AGRONOMIA"):
            erros.append("órgãos: a regra estrita rejeitou o próprio nome esperado")
    finally:
        clients.get_json, clients.memo = get_orig, memo_orig
    return erros


def _regressao_coleta_converge() -> list[str]:
    """A rodada seguinte gasta MENOS que a anterior — três defeitos que impediam.

    Todos medidos nas duas rodadas reais de 04/09/2026, sobre o Drive do
    operador, que gastaram ~90 requisições cada uma e morreram no mesmo ponto.
    """
    import os
    import tempfile
    import time
    from pathlib import Path
    from . import clients, pipeline

    erros: list[str] = []
    # `clients.get_json`, e NÃO `http.get_json`: clients faz
    # `from .http import get_json` no import, então são dois nomes distintos e
    # trocar o do módulo http não afeta quem já importou.
    guardado = (config.RAW_DIR, config.STAGED_DIR, config.DATA_DIR,
                config.CACHE_DIR, os.environ.get("QUEDA_CHECKPOINT"),
                clients.implanta_ano, clients.get_json)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        try:
            os.environ["QUEDA_CHECKPOINT"] = "1"
            config.DATA_DIR = base
            config.CACHE_DIR = base / "cache"
            config.RAW_DIR = base / "raw"
            config.STAGED_DIR = base / "staged"
            config.STAGED_DIR.mkdir(parents=True)

            # 1. Payload ANINHADO tinha de virar checkpoint. `contratos` traz
            #    ITENS dentro da linha; `_plano_para_csv` recusava o CSV (uma
            #    lista dentro de um CSV vira texto irreversível) e a janela
            #    ficava SEM checkpoint, num log.debug que ninguém vê. As duas
            #    rodadas de 04/09 registraram, idênticas, "contratos: 0 janela(s)
            #    do checkpoint, 15 coletada(s) agora" — 15 requisições jogadas
            #    fora por rodada, para sempre.
            rede: list[int] = []

            def portal(endpoint, ano, **kw):
                rede.append(ano)
                return [{"NUMERO_CONTRATO": f"CT-{ano}", "VALOR": "1,00",
                         "ITENS": [{"DESC": "x", "QTD": 2}],
                         "DATA_INICIO_VIGENCIA": f"01/01/{ano}"}]

            clients.implanta_ano = portal
            pipeline.stage_contratos(2020, 2023)
            if not rede:
                erros.append("regressão da convergência: a primeira rodada nem "
                             "foi à rede")
            rede.clear()
            df = pipeline.stage_contratos(2020, 2023)
            if rede:
                erros.append(
                    f"regressão da convergência: a segunda rodada re-coletou "
                    f"{rede} — payload aninhado voltou a não virar checkpoint, "
                    "e a etapa vai à rede inteira em TODA rodada")
            if len(df) != 4:
                erros.append("regressão da convergência: o checkpoint aninhado "
                             f"devolveu {len(df)} linhas, esperado 4")
            elif not isinstance(df.iloc[0].get("ITENS"), list):
                erros.append(
                    "regressão da convergência: o campo aninhado voltou como "
                    f"{type(df.iloc[0].get('ITENS')).__name__} — o checkpoint "
                    "está achatando o que a API mandou")

            # 2. Marcador de vazio EXPIRADO no exercício em curso. `atas` tinha
            #    2026 e 2025 vazios; passadas 12h o de 2026 expira, a contagem
            #    de vazios consecutivos se perde e o inventário anunciava 16
            #    exercícios pendentes — para uma coleta que fez UMA requisição.
            d = pipeline._checkpoint_dir("atas")
            d.mkdir(parents=True, exist_ok=True)
            (d / "2026.vazio").write_text("x\n")
            (d / "2025.vazio").write_text("x\n")
            velho = time.time() - (pipeline.CHECKPOINT_TTL_HORAS + 5) * 3600
            os.utime(d / "2026.vazio", (velho, velho))
            pend = pipeline.janelas_pendentes("atas", 2026, None)
            if pend != [2026]:
                erros.append(
                    "regressão da convergência: com o vazio do exercício em "
                    f"curso expirado, o inventário aponta {len(pend)} "
                    f"exercício(s) pendente(s) ({pend[:5]}...) — a coleta vai "
                    "fazer uma requisição e parar nos dois vazios seguidos")

            # 3. A sondagem de diárias não pode MATAR a coleta. Ela pergunta
            #    "este caminho serve?" a endpoints que o swagger define sem
            #    publicar; com retry de rate-limit, o segundo 403 virava
            #    RateLimited, subia até build_staged e abortava tudo. Nas duas
            #    rodadas de 04/09 as duas últimas etapas
            #    (indenizacoes_endpoint, pncp_contratos) nunca foram executadas.
            from .http import RateLimited

            def _403(url, params=None, **kw):
                # É assim que `get_json` se comporta de verdade: com
                # `rate_limit_retries=0` o 403 é resposta final (`None`); com
                # retry, a recusa persistente vira RateLimited.
                if kw.get("rate_limit_retries") == 0:
                    return None
                raise RateLimited("403 persistente (simulado)")

            clients.get_json = _403
            clients._PROBE_MORTOS.clear()
            try:
                clients.implanta_probe_diarias(2024)
            except RateLimited:
                erros.append(
                    "regressão da convergência: a sondagem de diárias levantou "
                    "RateLimited — ela aborta a coleta inteira e as etapas "
                    "seguintes nunca são executadas. 403 numa SONDAGEM é "
                    "resposta ('este caminho não serve'), não bloqueio a "
                    "esperar")
        finally:
            (config.RAW_DIR, config.STAGED_DIR, config.DATA_DIR,
             config.CACHE_DIR, checkpoint_antes, clients.implanta_ano,
             clients.get_json) = guardado
            clients._PROBE_MORTOS.clear()
            if checkpoint_antes is None:
                os.environ.pop("QUEDA_CHECKPOINT", None)
            else:
                os.environ["QUEDA_CHECKPOINT"] = checkpoint_antes
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

            # 2b. O MESMO defeito, na coluna que a lista explícita não tinha:
            #     `fornecedor_ni` (CNPJ do fornecedor no PNCP). Relida como int,
            #     '00000424000156' virava 424000156 e o cruzamento portal × PNCP
            #     deixava de casar todo CNPJ iniciado em 0 — 25 dos 39 "ausentes"
            #     da rodada de 04/09/2026 estavam no PNCP. A proteção agora é por
            #     padrão de nome, e vale para coluna que ainda não existe.
            (config.STAGED_DIR / "pncp_contratos.csv").write_text(
                "fornecedor_ni,valorGlobal,orgao_papel\n00000424000156,14000000.0,alvo\n")
            (config.STAGED_DIR / "sancoes.csv").write_text(
                "cadastro,documento,categoria\nceis,00716656000109,Inidoneidade\n")
            (config.STAGED_DIR / "qualquer.csv").write_text(
                "id_cnpj_do_fornecedor,cpf_socio,ni_orgao,x\n00012345000100,00012345678,00123,1\n")
            st = pipeline.load_staged()
            for ds, col, esperado in (("pncp_contratos", "fornecedor_ni", "00000424000156"),
                                      ("sancoes", "documento", "00716656000109"),
                                      ("qualquer", "id_cnpj_do_fornecedor", "00012345000100"),
                                      ("qualquer", "cpf_socio", "00012345678"),
                                      ("qualquer", "ni_orgao", "00123")):
                v = st[ds][col].iloc[0]
                if v != esperado:
                    erros.append(f"regressão do dado: {ds}.{col} relido como {v!r} "
                                 f"(esperado {esperado!r}) — zero à esquerda perdido")
            if not pd.api.types.is_numeric_dtype(st["qualquer"]["x"]):
                erros.append("regressão do dado: o padrão de coluna-texto engoliu uma "
                             "coluna numérica comum")

            # 3. Etapa que levanta não pode apagar o CSV bom da rodada anterior.
            (config.STAGED_DIR / "empenhos.csv").write_text(
                "_ano,NUMERO\n2020,1\n2021,2\n")
            quebrado = pipeline.stage_empenhos
            outras: dict = {}
            empenhos_preservados = -1
            try:
                pipeline.stage_empenhos = lambda d, a: (_ for _ in ()).throw(
                    KeyError("coluna sumiu"))
                # TODO stage_* menos o quebrado. A lista era fixa, e cada etapa
                # criada depois dela (licitacoes_pncp, pncp_atas, sancoes,
                # benchmark_creas) saía pela internet de verdade a cada
                # selftest — minutos de espera e requisições ao PNCP e à CGU
                # num teste que se diz offline.
                for nome in [n for n in dir(pipeline)
                             if n.startswith("stage_") and n != "stage_empenhos"
                             and callable(getattr(pipeline, n))]:
                    etapa = nome[len("stage_"):]
                    outras[etapa] = getattr(pipeline, nome)
                    setattr(pipeline, nome, lambda *a, **k: pd.DataFrame())
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
        # O ledger da fixture tem 403 nos endpoints Implanta de diárias. Para
        # o diagnóstico atribuí-los a `indenizacoes_endpoint` (veredito "não
        # conseguimos coletar"), os nomes têm de estar no ambiente — como numa
        # rodada real que os definiu. Sem eles a fonte sai como "endpoint não
        # identificado", que é outro veredito.
        env_diarias = config.DIARIAS_ENDPOINTS_ENV
        diarias_antes = os.environ.get(env_diarias)
        os.environ[env_diarias] = "Diarias,DiariasDeslocamentos,Indenizacoes"
        try:
            achados = run_all(staged)
        finally:
            if diarias_antes is None:
                os.environ.pop(env_diarias, None)
            else:
                os.environ[env_diarias] = diarias_antes
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
