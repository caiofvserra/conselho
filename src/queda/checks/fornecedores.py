"""Frente 6 — Fornecedores: situação cadastral e empresas recém-criadas."""
from __future__ import annotations

import pandas as pd

from .. import config
from ..findings import Finding
from ..utils import parse_data, so_digitos
from .common import df, fichas_cnpj

FRENTE = "Fornecedores"


def _primeiro_pagamento(staged) -> dict[str, pd.Timestamp]:
    pg = df(staged, "pagamentos")
    if pg.empty or "CPF_CNPJ" not in pg or "DATA" not in pg:
        return {}
    pj = pg[~pg.get("PF", False).astype(bool)].copy()
    pj["cnpj"] = pj["CPF_CNPJ"].map(so_digitos)
    pj = pj[pj["cnpj"].str.len() == 14].dropna(subset=["DATA"])
    return pj.groupby("cnpj")["DATA"].min().to_dict()


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
        achados.append(Finding(
            id="fornecedores-situacao-irregular", frente=FRENTE, gravidade="alta",
            titulo=f"{len(situacoes)} fornecedor(es) com situação cadastral não-ATIVA "
                   "na Receita",
            descricao="Empresa baixada, inapta ou suspensa recebendo dinheiro público "
                      "é red flag imediata (pode ter sido baixada depois — conferir "
                      "datas).",
            evidencia=pd.DataFrame(situacoes),
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
