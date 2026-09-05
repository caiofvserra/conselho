"""Regressões da rodada 11 — o que a rodada 10 (05/09/2026) publicou errado.

Cada caso reproduz um defeito lido das saídas reais e trava a correção:

* sanções: alcance por fundamento/esfera, pagamento contado uma vez por
  índice, data do contrato decidindo a gravidade;
* benchmark: `_ano` reposto na leitura do checkpoint; sem `_ano` a frente
  degrada em vez de estourar; só contas analíticas somam;
* PNCP: chave `numeroControlePncpCompra` lida sem distinguir caixa;
* recusa: TTL longo só para exercício encerrado, confirmado em duas rodadas
  com a mesma assinatura de parâmetros;
* relatório: gravidade `erro` fora da contagem de altas;
* diagnóstico: 403 superado por 200 não é bloqueio.

Sem rede, isolado em tempdir quando escreve em disco.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pandas as pd


def _erro(erros: list[str], cond: bool, msg: str) -> None:
    if not cond:
        erros.append(msg)


def _sancoes(erros: list[str]) -> None:
    from queda import clients
    from queda.checks import fornecedores as F

    casos = [
        ("Impedimento/proibição de contratar com prazo determinado", "Lei 10.520 art. 7º", "ESTADUAL", "ente"),
        ("Impedimento/proibição de contratar com prazo determinado", "Lei 10.520 art. 7º", "FEDERAL", "federal"),
        ("Impedimento/proibição de contratar com prazo determinado", None, "ESTADUAL", "ente"),
        ("Declaração de Inidoneidade sem prazo determinado", None, "MUNICIPAL", "nacional"),
        ("Impedimento/proibição de contratar com prazo determinado", "Lei 8.429/1992 art. 12", "ESTADUAL", "nacional"),
        ("Suspensão temporária de participação em licitação", "Lei 8.666 art. 87 III", "FEDERAL", "federal"),
        (None, None, None, "indeterminado"),
        ("Multa", "Lei 12.846 art. 6º I", "FEDERAL", "nao_impede"),
        ("Publicação Extraordinária", "Lei 12.846", "FEDERAL", "nao_impede"),
        ("Proibição de receber incentivos, subsídios, subvenções", "Lei 12.846 art. 19 IV", "FEDERAL", "nao_impede"),
    ]
    for cat, fund, esf, esperado in casos:
        got = clients.alcance_sancao(cat, fund, esf)
        _erro(erros, got == esperado,
              f"alcance_sancao({cat!r}, {fund!r}, {esf!r}) = {got!r}, esperado {esperado!r}")

    ceis = ["CADASTRO", "CÓDIGO DA SANÇÃO", "TIPO DE PESSOA", "CPF OU CNPJ DO SANCIONADO",
            "NOME DO SANCIONADO", "NOME INFORMADO PELO ÓRGÃO SANCIONADOR",
            "RAZÃO SOCIAL - CADASTRO RECEITA", "NOME FANTASIA - CADASTRO RECEITA",
            "NÚMERO DO PROCESSO", "CATEGORIA DA SANÇÃO", "DATA INÍCIO SANÇÃO",
            "DATA FINAL SANÇÃO", "DATA PUBLICAÇÃO", "PUBLICAÇÃO", "DETALHAMENTO",
            "DATA DO TRÂNSITO EM JULGADO", "ABRANGÊNCIA DEFINIDA EM DECISÃO JUDICIAL",
            "ÓRGÃO SANCIONADOR", "UF ÓRGÃO SANCIONADOR", "ESFERA ÓRGÃO SANCIONADOR",
            "FUNDAMENTAÇÃO LEGAL", "DATA ORIGEM INFORMAÇÃO", "ORIGEM INFORMAÇÕES", "OBSERVAÇÕES"]
    m = clients._resolver_colunas_sancao(ceis)
    _erro(erros, m["orgao"] == "ÓRGÃO SANCIONADOR",
          f"CEIS: coluna do órgão resolvida para {m['orgao']!r} (rodada 10 publicou o nome da empresa)")
    _erro(erros, m["nome"] == "NOME DO SANCIONADO", f"CEIS: nome resolvido para {m['nome']!r}")
    _erro(erros, m["fundamento"] == "FUNDAMENTAÇÃO LEGAL", "CEIS: fundamento não resolvido")
    cepim = clients._resolver_colunas_sancao(["CNPJ ENTIDADE", "NOME ENTIDADE", "NÚMERO CONVÊNIO",
                                              "ÓRGÃO CONCEDENTE", "MOTIVO DO IMPEDIMENTO"])
    _erro(erros, cepim["documento"] == "CNPJ ENTIDADE" and cepim["orgao"] == "ÓRGÃO CONCEDENTE"
          and cepim["inicio"] is None, f"CEPIM: mapa {cepim}")

    # cruzamento: DSPM com dois registros; ATENTO estadual; MAX ESCOLAR federal
    # com ata anterior; ALFA inidoneidade com contrato firmado durante a sanção
    pag = pd.DataFrame({
        "FAVORECIDO_CNPJ": ["08457879000107"] * 3 + ["06069276000102", "51628440000129", "99888777000155"],
        "FAVORECIDO_NORM": ["DSPM"] * 3 + ["ATENTO", "MAX ESCOLAR", "ALFA"],
        "DATA": pd.to_datetime(["2013-01-15", "2013-01-15", "2013-01-16",
                                "2021-12-23", "2025-09-19", "2024-06-10"]),
        "VALOR": [500.0, 500.0, 470.75, 199805.48, 5380.30, 10000.0],
        "CATEGORIA": ["SERVICOS"] * 6,
    })
    san = pd.DataFrame({
        "cadastro": ["ceis"] * 5,
        "documento": ["08457879000107", "08457879000107", "06069276000102", "51628440000129", "99888777000155"],
        "categoria": ["Declaração de Inidoneidade sem prazo determinado"] * 2
        + ["Impedimento/proibição de contratar com prazo determinado"] * 2
        + ["Declaração de Inidoneidade sem prazo determinado"],
        "inicio": ["2009-08-10", "2011-02-03", "2021-12-01", "2025-09-02", "2024-01-01"],
        "fim": [None, None, "2026-11-30", "2027-09-02", None],
        "orgao_sancionador": ["Prefeitura X", "Prefeitura Y", "Secretaria Z (SP)", "Ministério W", "TCU"],
        "esfera": ["MUNICIPAL", "MUNICIPAL", "ESTADUAL", "FEDERAL", "FEDERAL"],
        "fundamento": [None, None, "Lei 10.520 art. 7º", "Lei 14.133 art. 156 III", None],
    })
    ctr = pd.DataFrame({
        "NUMERO_CONTRATO": ["ATA 006/2024", "C-001/2024"],
        "CONTRATADA_CNPJ": ["51628440000129", "99888777000155"],
        "DATA_INICIO_VIGENCIA": ["2024-01-08", "2024-03-01"],
        "INI": pd.to_datetime(["2024-01-08", "2024-03-01"]),
        "VALOR": [10000.0, 50000.0],
    })
    staged = {"sancoes": san, "pagamentos": pag, "contratos": ctr}
    try:
        achados = {f.id: f for f in F._achados_sancoes(staged)}
    except Exception as e:                           # noqa: BLE001
        erros.append(f"_achados_sancoes estourou: {type(e).__name__}: {e}")
        return
    principal = achados.get("fornecedores-sancionados")
    _erro(erros, principal is not None, "fornecedores-sancionados não emitido")
    if principal is not None:
        m = principal.metricas
        _erro(erros, m.get("pagamentos") == 4,
              f"pagamentos contados {m.get('pagamentos')} (esperado 4: 3 DSPM uma vez cada + ALFA)")
        _erro(erros, abs(float(m.get("valor", 0)) - 11470.75) < 0.01,
              f"valor {m.get('valor')} (esperado 11.470,75 sem a dobra da DSPM)")
        _erro(erros, principal.gravidade == "alta",
              f"gravidade {principal.gravidade}: ALFA tem contrato firmado durante a sanção → alta")
        ev = principal.evidencia
        _erro(erros, ev is not None and "orgao" in ev.columns
              and not ev["orgao"].astype(str).str.contains("DSPM|ATENTO|ESCOLAR|ALFA").any(),
              "coluna orgao da evidência traz nome de empresa")
        _erro(erros, ev is not None and not (ev["cnpj"] == "06069276000102").any(),
              "ATENTO (impedimento ESTADUAL) entrou no achado principal")
    outro = achados.get("fornecedores-sancionados-outro-orgao")
    _erro(erros, outro is not None and outro.metricas.get("fornecedores") == 1
          and abs(float(outro.metricas.get("valor", 0)) - 199805.48) < 0.01,
          "ATENTO deveria estar em outro-orgao com R$ 199.805,48")
    ant = achados.get("fornecedores-sancionados-contrato-anterior")
    _erro(erros, ant is not None and ant.gravidade == "info"
          and abs(float(ant.metricas.get("valor", 0)) - 5380.30) < 0.01,
          "MAX ESCOLAR (ata de jan/2024, sanção de set/2025) deveria ser contexto de contrato anterior")


def _benchmark(erros: list[str]) -> None:
    from queda.checks import benchmark as B
    sem_ano = pd.DataFrame({"NOME": ["A"], "ANALITICA": ["True"], "REALIZADO_EXERCICIO": [10.0],
                            "_uf": ["SP"], "_host": ["crea-sp"]})
    try:
        r = B.run({"benchmark_creas": sem_ano})
    except Exception as e:                           # noqa: BLE001
        erros.append(f"benchmark sem _ano estourou: {type(e).__name__}: {e}")
        return
    _erro(erros, [f.id for f in r] == ["benchmark-indisponivel"],
          f"benchmark sem _ano devolveu {[f.id for f in r]}")
    rows = []
    for uf in ["SP", "MG", "RJ", "PR", "RS", "BA"]:
        for ano in (2023, 2024, 2025):
            if uf == "BA" and ano != 2025:
                continue
            rows.append({"NOME": "DESPESA CORRENTE", "ANALITICA": "False",
                         "REALIZADO_EXERCICIO": 1e9, "_uf": uf, "_ano": ano})
            rows.append({"NOME": "DIARIAS", "ANALITICA": "True",
                         "REALIZADO_EXERCICIO": 100.0, "_uf": uf, "_ano": ano})
            rows.append({"NOME": "VENCIMENTOS", "ANALITICA": "True",
                         "REALIZADO_EXERCICIO": 900.0, "_uf": uf, "_ano": ano})
    r = {f.id: f for f in B.run({"benchmark_creas": pd.DataFrame(rows)})}
    f = r.get("benchmark-despesa-por-profissional")
    _erro(erros, f is not None, "benchmark com _ano não produziu o ranking")
    if f is not None:
        ev = f.evidencia.set_index("uf")
        _erro(erros, abs(float(ev.loc["BA", "despesa_media_anual"]) - 1000.0) < 0.01
              and abs(float(ev.loc["SP", "despesa_media_anual"]) - 1000.0) < 0.01,
              "média anual por host errada (sintéticas somadas ou n_anos global)")
        _erro(erros, int(ev.loc["BA", "exercicios"]) == 1 and int(ev.loc["SP", "exercicios"]) == 3,
              "exercícios por host não refletem o que cada um respondeu")


def _pipeline(erros: list[str]) -> None:
    from queda import config, pipeline as P
    dto = {"numeroControlePncpCompra": "60985017000177-1-000019/2024",
           "tipoContrato": {"id": 7, "nome": "Empenho"}, "niFornecedor": "00424000156",
           "numeroContratoEmpenho": "3554", "unidadeOrgao": {"codigoUnidade": "389423"},
           "valorGlobal": 75000.0}
    linha = P._pncp_contrato_linha({"cnpj": "60985017000177", "papel": "alvo"}, dto)
    _erro(erros, linha["numeroControlePNCPCompra"] == "60985017000177-1-000019/2024",
          "compra de origem não lida do DTO com 'Pncp' minúsculo")
    _erro(erros, linha["tipoContrato_nome"] == "Empenho" and linha["fornecedor_ni"] == "00424000156",
          "tipoContrato_nome/fornecedor_ni errados na projeção")

    guardar = {k: getattr(config, k) for k in ("DATA_DIR", "RAW_DIR", "STAGED_DIR", "CACHE_DIR")}
    env_ant = os.environ.get(P._ENV_CHECKPOINT)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        config.DATA_DIR, config.RAW_DIR = base, base / "raw"
        config.STAGED_DIR, config.CACHE_DIR = base / "staged", base / "cache"
        os.environ[P._ENV_CHECKPOINT] = "1"
        try:
            chamadas = []

            def buscar(a):
                chamadas.append(a)
                return [{"NOME": "X", "REALIZADO_EXERCICIO": 1.0}]

            janelas = lambda: ((str(a), a, a) for a in (2023, 2024))  # noqa: E731
            out1 = P._janelas_nomeadas("benchmark/crea-zz", janelas(), buscar, dataset="t")
            out2 = P._janelas_nomeadas("benchmark/crea-zz", janelas(), buscar, dataset="t")
            _erro(erros, sorted(r["_ano"] for r in out1) == [2023, 2024],
                  f"_janelas_nomeadas não carimbou _ano na rede: {out1}")
            _erro(erros, sorted(r["_ano"] for r in out2) == [2023, 2024] and len(chamadas) == 2,
                  "_janelas_nomeadas não repôs _ano ao ler do checkpoint (ou foi à rede de novo)")

            # recusa: 12 h; segunda rodada com a mesma pergunta em exercício encerrado → 720 h
            ass = "referenciaInicio,referenciaTermino"
            P._marcar_recusa("empenhos", "2010", ass)
            _erro(erros, P._recusa_fresca("empenhos", "2010", 2010, ass), "recusa recém-marcada não é fresca")
            *_, rec = P._checkpoint_arquivos("empenhos", "2010")
            velho = time.time() - 13 * 3600
            os.utime(rec, (velho, velho))
            _erro(erros, not P._recusa_fresca("empenhos", "2010", 2010, ass),
                  "recusa de 13 h com n=1 ainda fresca")
            P._marcar_recusa("empenhos", "2010", ass)          # 2ª rodada
            os.utime(rec, (velho, velho))
            _erro(erros, P._recusa_fresca("empenhos", "2010", 2010, ass),
                  "recusa confirmada em 2 rodadas de exercício encerrado deveria valer 30 dias")
            _erro(erros, not P._recusa_fresca("empenhos", "2010", 2010, "vigenciaInicio,vigenciaTermino"),
                  "assinatura diferente deveria invalidar a recusa")
            P._marcar_recusa("empenhos", "2026", ass); P._marcar_recusa("empenhos", "2026", ass)
            *_, rec26 = P._checkpoint_arquivos("empenhos", "2026")
            os.utime(rec26, (velho, velho))
            _erro(erros, not P._recusa_fresca("empenhos", "2026", 2026, ass),
                  "exercício em curso nunca ganha o TTL longo")

            # esquema: staged sem coluna obrigatória não está pronto
            config.STAGED_DIR.mkdir(parents=True, exist_ok=True)
            (config.STAGED_DIR / "benchmark_creas.csv").write_text("NOME,_uf\nA,SP\n")
            _erro(erros, P._ja_coletado("benchmark_creas") is None,
                  "staged benchmark sem _ano foi aceito como pronto")
            _erro(erros, P._ja_coletado("benchmark_creas", exigir_esquema=False) is not None,
                  "exigir_esquema=False deveria devolver o staged antigo")
        finally:
            for k, v in guardar.items():
                setattr(config, k, v)
            if env_ant is None:
                os.environ.pop(P._ENV_CHECKPOINT, None)
            else:
                os.environ[P._ENV_CHECKPOINT] = env_ant


def _diagnostico_e_report(erros: list[str]) -> None:
    from queda import http as H
    from queda.checks import diagnostico as D
    from queda.findings import Finding
    led = pd.DataFrame({
        "ts": ["t1", "t2", "t3"],
        "caminho": ["/transparencia/api/v1.0/Conselheiros"] * 3,
        "params": ["", "", ""],
        "status": [403, 403, 200],
        "resultado": [H.R_BLOQUEIO, H.R_BLOQUEIO, H.R_VAZIO],
        "itens": [None, None, 0],
    })
    _erro(erros, D._causa(led, False) == "servidor respondeu 200 com lista vazia",
          f"403 superado por 200 ainda vira bloqueio: {D._causa(led, False)!r}")
    so_403 = led.iloc[:2]
    _erro(erros, D._causa(so_403, False).startswith("bloqueio"), "403 sem 2xx deveria ser bloqueio")
    _erro(erros, D._causa(None, False, tem_caminho=False).startswith("endpoint não identificado"),
          "fonte sem caminho conhecido deveria sair como 'endpoint não identificado'")
    _erro(erros, D.NAO_PUBLICA != "o órgão não publica", "veredito voltou a afirmar 'o órgão não publica'")
    try:
        f = Finding(id="erro-check-x", frente="Diagnóstico da coleta", gravidade="erro",
                    titulo="t", descricao="d")
        _erro(erros, f.gravidade == "erro" and "fontes" in f.resumo(), "Finding sem gravidade erro/fontes")
    except AssertionError:
        erros.append("gravidade 'erro' não aceita por Finding")


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_sancoes, _benchmark, _pipeline, _diagnostico_e_report):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
