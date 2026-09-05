"""Regressões da rodada 16 — o que a rodada 13 mostrou, com a rede de verdade.

A rodada 13 foi a primeira em que as fontes novas encostaram num servidor real,
e trouxe três coisas:

* **O portal diz por que recusa.** As 34 recusas com HTTP 400 trazem a mesma
  frase: *"O período de referência informado está fora do período de liberação
  configurado."* Não é erro do nosso parâmetro — é uma janela de liberação por
  endpoint, e ela explica a série curta que o relatório vinha publicando sem
  saber a causa.
* **O TCU respondeu com 763 responsáveis, e nenhum casou.** O motivo provável
  era nosso: o CPF vem mascarado (`***.456.789-**`, dígitos 4 a 9) e a
  comparação era pelos ÚLTIMOS dígitos — o meio de um contra o fim do outro.
* **Os acórdãos vieram com 499 "Acesso Bloqueado"**, e o corpo guardado era
  4 KB de folha de estilo: a mensagem e o número da requisição, que é o que se
  usa para pedir desbloqueio, ficaram fora do corte.

Tudo offline: rede substituída por funções locais, disco em pasta temporária.
"""
from __future__ import annotations

import pandas as pd

from .. import config
from ..checks import diagnostico as D
from ..checks import fornecedores as F
from ..http import _texto_de_html, _headers_do_host
from .rodada12 import _ambiente, _erro


class _Resposta:
    """O mínimo de `requests.Response` que `_guardar_corpo_4xx` usa."""

    def __init__(self, texto: str, ctype: str, status: int = 400):
        self.text = texto
        self.status_code = status
        self.headers = {"Content-Type": ctype}

    def json(self):
        import json
        return json.loads(self.text)


def _corpo_html_vira_texto(erros: list[str]) -> None:
    """Página de bloqueio guardada como TEXTO: 4 KB de CSS não dizem nada."""
    from ..http import _guardar_corpo_4xx
    pagina = ("<!DOCTYPE html><html><head><style>\n"
              + ("--tcu-blue:#003A8F; padding:32px 16px; " * 400)   # ~9 KB de CSS
              + "</style></head><body><h1>Acesso Bloqueado</h1>"
                "<p>Sua requisição foi bloqueada.</p>"
                "<p>ID da requisição: abc-123-def</p></body></html>")
    _erro(erros, len(pagina) > 8000,
          "regressão mal montada: a página precisa passar de 8 KB para valer")
    with _ambiente():
        _guardar_corpo_4xx("https://dados-abertos.apps.tcu.gov.br/api/acordao/x",
                           {"inicio": 0}, _Resposta(pagina, "text/html", 499))
        arquivos = list((config.RAW_DIR / "http-4xx").glob("*.txt"))
        _erro(erros, len(arquivos) == 1, f"corpo 4xx não foi gravado ({arquivos})")
        if arquivos:
            salvo = arquivos[0].read_text(encoding="utf-8")
            _erro(erros, "Acesso Bloqueado" in salvo and "abc-123-def" in salvo,
                  "corpo 4xx: a mensagem e o id da requisição não sobreviveram ao "
                  "corte de 4 KB — é o id que o operador usa para pedir desbloqueio")
            _erro(erros, "--tcu-blue" not in salvo,
                  "corpo 4xx: a folha de estilo foi guardada em vez do texto")
    _erro(erros, _texto_de_html("<style>a{x:1}</style><p>oi</p><script>y</script>") == "oi",
          "texto de HTML: script e estilo deveriam sair")


def _cabecalho_por_host(erros: list[str]) -> None:
    """Só o host configurado recebe cabeçalho extra; os demais seguem iguais."""
    h = _headers_do_host("https://dados-abertos.apps.tcu.gov.br/api/x")
    _erro(erros, h and "Accept" in h,
          f"cabeçalhos do TCU não aplicados ({h})")
    _erro(erros, _headers_do_host("https://crea-sp.implanta.net.br/x") is None,
          "o portal do Crea-SP não pode receber os cabeçalhos de outro host — o "
          "Accept documentado dele é outro")
    _erro(erros, _headers_do_host("nao-e-url") is None,
          "URL malformada não pode estourar na montagem do cabeçalho")


def _cpf_mascarado_alinha_pelo_meio(erros: list[str]) -> None:
    """`***.456.789-**` expõe os dígitos 4 a 9. Comparar os ÚLTIMOS N compara o
    meio de um com o fim do outro: deixa de casar quem é a mesma pessoa e casa
    quem não é."""
    casos = [
        ("***456789**", "12345678901", True),    # mascarado × completo
        ("456789", "12345678901", True),
        ("456789", "98456789012", False),        # dígitos no lugar errado
        ("12345678901", "12345678901", True),
        ("456789", "456789", True),
        ("11222333000144", "12345678901", False),  # CNPJ não é pessoa
        ("", "12345678901", False),
        ("4567", "12345678901", False),          # tamanho que não alinha
    ]
    for a, b, esperado in casos:
        got = F._mesmo_cpf(a, b)
        _erro(erros, got is esperado,
              f"_mesmo_cpf({a!r}, {b!r}) = {got}, esperado {esperado}")
    # o antigo "últimos N dígitos" casaria estes dois; o novo não
    _erro(erros, not F._mesmo_cpf("456789", "11111456789"),
          "alinhamento pelo FIM voltou — mascarado casando com quem termina igual")


def _par_nao_confirmado_e_contado(erros: list[str]) -> None:
    """Nome que bate sem documento que confirme não vira achado, mas VIRA
    número: é esse número que denuncia máscara desalinhada na rodada seguinte."""
    from ..checks import common as CM
    fichas = {"11111111000191": {"razao_social": "ALFA LTDA",
                                 "qsa": [{"nome_socio": "JOAO DA SILVA",
                                          "cnpj_cpf_do_socio": "***456789**"}]}}
    resp = pd.DataFrame([
        {"cadastro": "tcu_inabilitados", "documento": "98765432100",
         "nome": "JOAO DA SILVA", "inicio": "2022-01-01", "fim": "2030-01-01",
         "processo": "TC 1/2021", "deliberacao": "Acórdão 1/2022", "uf": "SP"},
    ])
    pg = pd.DataFrame([
        {"TIPO_PESSOA": "PJ", "CPF_CNPJ": "11.111.111/0001-91",
         "FAVORECIDO_NORM": "ALFA", "VALOR": 500_000.0, "DATA": "2024-01-10"}])
    guardado = CM._FICHAS
    try:
        CM._FICHAS = fichas
        achados = {f.id: f for f in F._achados_tcu_responsaveis(
            {"tcu_responsaveis": resp, "pagamentos": pg})}
    finally:
        CM._FICHAS = guardado
    _erro(erros, "fornecedores-socio-condenado-tcu" not in achados,
          "homônimo com documento que não confirma virou achado")
    f = achados.get("fornecedores-tcu-sem-cruzamento")
    _erro(erros, f is not None
          and f.metricas.get("nome_bateu_documento_nao_confirmou") == 1,
          f"o par não confirmado não foi contado ({None if f is None else f.metricas})")


def _periodo_de_liberacao(erros: list[str]) -> None:
    """A frase do servidor vira achado, com o primeiro exercício servido."""
    with _ambiente():
        pasta = config.RAW_DIR / "http-4xx"
        pasta.mkdir(parents=True, exist_ok=True)
        casos = [("DespesasCentroCusto", a) for a in range(2010, 2021)]
        casos += [("BalancoOrcamentario", 2010), ("BalancoOrcamentario", 2011)]
        casos += [("Contratos", 2010)]
        for i, (ep, ano) in enumerate(casos):
            (pasta / f"{i}.txt").write_text(
                f"url: https://crea-sp.implanta.net.br/transparencia/api/v1.0/{ep}\n"
                f'params: {{"referenciaInicio": "01/{ano}", "referenciaTermino": "12/{ano}"}}\n'
                "status: 400\ncontent-type: application/json\n"
                "data: 2026-09-05T13:00:00\n---\n"
                '{"Message": "O período de referência informado está fora do '
                'período de liberação configurado."}', encoding="utf-8")
        # um 4xx de outra natureza não pode entrar nesta conta
        (pasta / "outro.txt").write_text(
            "url: https://pncp.gov.br/api/consulta/v1/instrumentoscobranca/inclusao\n"
            'params: {"dataInicial": "20240101", "dataFinal": "20241231"}\n'
            "status: 422\ncontent-type: application/json\n"
            "data: 2026-09-05T13:00:00\n---\n"
            '{"message": "Período inicial e final maior que 30 dias."}',
            encoding="utf-8")
        achados = D._recusas_do_portal({})
        if len(achados) != 1:
            erros.append(f"período de liberação: saíram {len(achados)} achado(s)")
            return
        f = achados[0]
        _erro(erros, f.metricas.get("endpoints_com_janela") == 3,
              f"endpoints com janela: {f.metricas}")
        _erro(erros, f.metricas.get("primeiro_exercicio_servido") == 2021,
              "DespesasCentroCusto recusou até 2020, então o primeiro servido é "
              f"2021 — saiu {f.metricas.get('primeiro_exercicio_servido')}")
        _erro(erros, "período de liberação" in f.descricao,
              "a descrição tem de CITAR a frase do servidor, não parafraseá-la")
        _erro(erros, "não é" in f.descricao.lower() and "exista" in f.descricao,
              "falta a ressalva de que o dado existe e o que não o serve é a API")
        linha = f.evidencia[f.evidencia["endpoint"] == "BalancoOrcamentario"]
        _erro(erros, not linha.empty and int(linha.iloc[0]["primeiro_servido"]) == 2012,
              f"BalancoOrcamentario deveria começar em 2012 ({linha.to_dict('records')})")

    # sem corpo de 4xx guardado não há achado — e não pode estourar
    with _ambiente():
        _erro(erros, D._recusas_do_portal({}) == [],
              "sem corpos de 4xx a frente não pode publicar nada")


def run() -> list[str]:
    erros: list[str] = []
    for passo in (_corpo_html_vira_texto, _cabecalho_por_host,
                  _cpf_mascarado_alinha_pelo_meio, _par_nao_confirmado_e_contado,
                  _periodo_de_liberacao):
        try:
            passo(erros)
        except Exception as e:                       # noqa: BLE001
            erros.append(f"{passo.__name__} estourou: {type(e).__name__}: {e}")
    return erros
