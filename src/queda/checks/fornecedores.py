"""Frente 6 — Fornecedores: situação cadastral e empresas recém-criadas."""
from __future__ import annotations

import pandas as pd

from .. import config
from ..findings import Finding
from ..utils import parse_data, so_digitos
from .common import df, fichas_cnpj

FRENTE = "Fornecedores"


def _pagamentos_por_cnpj(staged) -> pd.core.groupby.DataFrameGroupBy | None:
    pg = df(staged, "pagamentos")
    if pg.empty or "CPF_CNPJ" not in pg or "DATA" not in pg:
        return None
    pj = pg.copy()
    pj["cnpj"] = pj["CPF_CNPJ"].map(so_digitos)
    pj = pj[pj["cnpj"].str.len() == 14].dropna(subset=["DATA"])
    return None if pj.empty else pj.groupby("cnpj")["DATA"]


def _primeiro_pagamento(staged) -> dict[str, pd.Timestamp]:
    g = _pagamentos_por_cnpj(staged)
    return {} if g is None else g.min().to_dict()


def _ultimo_pagamento(staged) -> dict[str, pd.Timestamp]:
    g = _pagamentos_por_cnpj(staged)
    return {} if g is None else g.max().to_dict()


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    fichas = fichas_cnpj(staged)
    if not fichas:
        achados.append(Finding(
            id="fornecedores-sem-fichas", frente=FRENTE, gravidade="info",
            titulo="Nenhuma ficha de CNPJ consultada",
            descricao="APIs de CNPJ indisponíveis ou nenhum fornecedor acima do piso.",
        ))
        return achados

    primeiro_pg = _primeiro_pagamento(staged)
    situacoes, jovens = [], []
    for cnpj, ficha in fichas.items():
        razao = ficha.get("razao_social") or ""
        situacao = str(ficha.get("descricao_situacao_cadastral")
                       or ficha.get("situacao_cadastral") or "").upper()
        abertura = parse_data(ficha.get("data_inicio_atividade")
                              or ficha.get("data_inicio_atividades"))
        cnae = ficha.get("cnae_fiscal_descricao") or ""
        registro = {"cnpj": cnpj, "razao_social": razao, "situacao": situacao,
                    "abertura": abertura, "cnae": cnae}
        if situacao and "ATIVA" not in situacao:
            situacoes.append(registro)
        primeira = primeiro_pg.get(cnpj)
        if primeira is not None and pd.notna(abertura):
            idade = (primeira - abertura).days
            if 0 <= idade < config.IDADE_MINIMA_EMPRESA_DIAS:
                jovens.append({**registro, "primeiro_pagamento": primeira,
                               "idade_dias_no_1o_pagamento": idade})

    if situacoes:
        ev = pd.DataFrame(situacoes)
        ultimo = _ultimo_pagamento(staged)
        ev["ultimo_pagamento"] = ev["cnpj"].map(ultimo)
        ev["anos_desde_ultimo_pagamento"] = (
            (pd.Timestamp.today() - pd.to_datetime(ev["ultimo_pagamento"],
                                                   errors="coerce")).dt.days / 365
        ).round(1)
        recentes = ev[ev["anos_desde_ultimo_pagamento"] <= 2]
        achados.append(Finding(
            id="fornecedores-situacao-irregular", frente=FRENTE,
            gravidade="alta" if not recentes.empty else "media",
            titulo=(f"{len(recentes)} fornecedor(es) com situação cadastral "
                    "não-ATIVA e pagamento nos últimos 2 anos"
                    if not recentes.empty else
                    f"{len(ev)} fornecedor(es) hoje não-ATIVOS na Receita "
                    "(pagamentos antigos)"),
            descricao=(
                "A ficha da Receita é a de HOJE; o pagamento pode ser de anos "
                "atrás, quando a empresa estava regular. A coluna "
                "`anos_desde_ultimo_pagamento` separa os dois casos: baixa "
                "recente com pagamento recente é red flag; empresa que encerrou "
                "atividade anos depois do último pagamento não é."),
            evidencia=ev.sort_values("anos_desde_ultimo_pagamento"),
            como_verificar="Consultar a data de baixa/inaptidão na Receita e "
                           "compará-la com a data de cada pagamento.",
        ))
    if jovens:
        achados.append(Finding(
            id="fornecedores-empresa-recente", frente=FRENTE, gravidade="alta",
            titulo=f"{len(jovens)} fornecedor(es) receberam o 1º pagamento com menos de "
                   f"{config.IDADE_MINIMA_EMPRESA_DIAS} dias de existência",
            descricao="Empresa criada às vésperas da contratação é padrão típico de "
                      "empresa de fachada/direcionamento.",
            evidencia=pd.DataFrame(jovens),
        ))

    resumo = pd.DataFrame([
        {"cnpj": c, "razao_social": f.get("razao_social"),
         "situacao": f.get("descricao_situacao_cadastral") or f.get("situacao_cadastral"),
         "abertura": f.get("data_inicio_atividade"),
         "cnae": f.get("cnae_fiscal_descricao"),
         "municipio": f.get("municipio"), "uf": f.get("uf")}
        for c, f in fichas.items()])
    achados.append(Finding(
        id="fornecedores-fichas", frente=FRENTE, gravidade="info",
        titulo=f"Fichas cadastrais consultadas ({len(fichas)} CNPJs)",
        descricao="Universo: fornecedores acima do piso de análise, limitado a "
                  f"{config.MAX_CONSULTAS_CNPJ} consultas.",
        evidencia=resumo,
    ))
    return achados
