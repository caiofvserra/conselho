"""Helpers compartilhados entre as frentes."""
from __future__ import annotations

import pandas as pd

from .. import clients, config
from .. import taxonomia as T
from ..utils import norm_nome, so_digitos


def df(staged: dict, nome: str) -> pd.DataFrame:
    d = staged.get(nome)
    return d if isinstance(d, pd.DataFrame) else pd.DataFrame()


def pagamentos_indenizacao(staged, *, apenas_pessoa_fisica: bool = True) -> pd.DataFrame:
    """Pagamentos de diária / quilometragem / indenização a pessoas.

    A classificação usa **apenas o nome da conta contábil** (`CONTA_EMPENHO`),
    nunca o histórico nem o nome do favorecido. Essa é a correção do falso
    positivo da primeira rodada: o histórico costuma repetir a razão social, e
    empresas como "CS BRASIL TRANSPORTES DE PASSAGEIROS" ou operadoras de
    telecom entravam no filtro por casarem 'passag'/'link' no texto livre.

    Por padrão devolve só pessoas físicas — diária e quilometragem são verbas
    de pessoa física. Passe `apenas_pessoa_fisica=False` para incluir PJ
    (agências de viagem, locadoras), que é despesa de custeio, não verba.
    """
    pg = df(staged, "pagamentos")
    if pg.empty or "VALOR" not in pg.columns:
        return pd.DataFrame()

    conta = pg.get("CONTA_EMPENHO")
    if conta is None:
        conta = pg.get("NOME_CONTA", pd.Series("", index=pg.index))
    conta = conta.fillna("").astype(str)

    # classifica os valores DISTINTOS de conta (poucas centenas), não linha a linha
    mapa = {v: T.classificar_texto(v) for v in conta.unique()}
    categoria = conta.map(mapa)
    mask = categoria == "VERBAS_PESSOAIS"

    out = pg[mask].copy()
    out["_conta_match"] = conta[mask]
    if apenas_pessoa_fisica and "PF" in out.columns:
        out = out[out["PF"].astype(bool)]
    return out


def pagamentos_viagem_pj(staged) -> pd.DataFrame:
    """Contrapartida: gasto de viagem pago a PESSOA JURÍDICA (agências, cias
    aéreas, locadoras). É custeio administrativo — separado das verbas a
    pessoas para não inflar o número de 'diárias'."""
    todos = pagamentos_indenizacao(staged, apenas_pessoa_fisica=False)
    if todos.empty or "PF" not in todos.columns:
        return pd.DataFrame()
    return todos[~todos["PF"].astype(bool)]


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
