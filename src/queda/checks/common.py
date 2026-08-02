"""Helpers compartilhados entre as frentes."""
from __future__ import annotations

import pandas as pd

from .. import clients, config
from ..utils import norm_nome, so_digitos


def df(staged: dict, nome: str) -> pd.DataFrame:
    d = staged.get(nome)
    return d if isinstance(d, pd.DataFrame) else pd.DataFrame()


def pagamentos_indenizacao(staged) -> pd.DataFrame:
    """Pagamentos classificados como diária/indenização/km pelo texto da conta
    ou do histórico — fonte primária, já que os endpoints dedicados costumam
    estar vazios."""
    pg = df(staged, "pagamentos")
    if pg.empty:
        return pg
    texto = (pg.get("CONTA_EMPENHO", "").fillna("").astype(str) + " " +
             pg.get("HISTORICO", "").fillna("").astype(str))
    mask = texto.str.contains(config.REGEX_INDENIZACAO, case=False, regex=True)
    out = pg[mask].copy()
    out["_texto_match"] = texto[mask]
    return out


def classificar_beneficiario(nomes_norm: pd.Series, staged) -> pd.Series:
    """conselheiro | comissionado | efetivo | outro — por nome normalizado."""
    mand = set(df(staged, "mandatarios").get("NOME_NORM", pd.Series(dtype=str)).dropna())
    quadro = df(staged, "quadro_pessoal")
    comis, efet = set(), set()
    if not quadro.empty and "NOME_NORM" in quadro:
        vinc = quadro.get("VINCULO_NORM", quadro.get("vinculo", "")).fillna("").astype(str)
        eh_comis = vinc.str.contains("COMISS|LIVRE|CONFIAN", case=False, regex=True)
        comis = set(quadro.loc[eh_comis, "NOME_NORM"])
        efet = set(quadro.loc[~eh_comis, "NOME_NORM"])

    def rotulo(n):
        if n in mand:
            return "conselheiro"
        if n in comis:
            return "comissionado"
        if n in efet:
            return "efetivo"
        return "outro"
    return nomes_norm.map(rotulo)


_FICHAS: dict[str, dict] | None = None


def fichas_cnpj(staged) -> dict[str, dict]:
    """Consulta (com cache) a ficha cadastral dos principais fornecedores PJ.

    Universo: quem recebeu acima de PISO_ANALISE_FORNECEDOR em pagamentos ou
    tem contrato no PNCP, limitado a MAX_CONSULTAS_CNPJ para não gerar tráfego
    desnecessário.
    """
    global _FICHAS
    if _FICHAS is not None:
        return _FICHAS
    candidatos: dict[str, float] = {}
    pg = df(staged, "pagamentos")
    if not pg.empty and "CPF_CNPJ" in pg:
        pj = pg[~pg.get("PF", False).astype(bool)].copy()
        pj["cnpj"] = pj["CPF_CNPJ"].map(so_digitos)
        tot = pj[pj["cnpj"].str.len() == 14].groupby("cnpj")["VALOR"].sum()
        for cnpj, v in tot[tot >= config.PISO_ANALISE_FORNECEDOR].items():
            candidatos[cnpj] = candidatos.get(cnpj, 0) + float(v)
    pncp = df(staged, "pncp_contratos")
    if not pncp.empty:
        alvo = pncp[pncp.get("orgao_papel") == "alvo"]
        for _, r in alvo.iterrows():
            ni = so_digitos(r.get("fornecedor_ni"))
            if len(ni) == 14:
                candidatos[ni] = candidatos.get(ni, 0) + float(r.get("valorGlobal") or 0)
    top = sorted(candidatos.items(), key=lambda kv: -kv[1])[: config.MAX_CONSULTAS_CNPJ]
    fichas = {}
    for cnpj, _ in top:
        ficha = clients.consultar_cnpj(cnpj)
        if ficha:
            fichas[cnpj] = ficha
    _FICHAS = fichas
    return fichas


def qsa_nomes(ficha: dict) -> list[str]:
    """Nomes dos sócios (QSA) numa ficha BrasilAPI/minhareceita."""
    qsa = ficha.get("qsa") or ficha.get("socios") or []
    nomes = []
    for s in qsa:
        n = s.get("nome_socio") or s.get("nome") or s.get("nome_socio_razao_social")
        if n:
            nomes.append(norm_nome(n))
    return nomes
