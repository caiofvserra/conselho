"""Frente — Diagnóstico da própria coleta.

Um relatório com poucos achados pode significar duas coisas opostas: o órgão
está em ordem, ou a ferramenta não conseguiu baixar os dados que revelariam o
problema. Sem esta frente as duas hipóteses são indistinguíveis — e a segunda
é a mais provável, porque as APIs do portal caem, mudam de formato de
parâmetro e devolvem 403 por rajada.

Aqui a ferramenta audita a si mesma: o que veio, o que faltou, quais anos
ficaram fora e quais análises estão desligadas por falta de fonte.
"""
from __future__ import annotations

import pandas as pd

from ..findings import Finding
from .common import df

FRENTE = "Diagnóstico da coleta"

# dataset -> (o que ele habilita, gravidade da ausência)
CRITICOS = {
    "pagamentos": ("toda a análise de despesa: verbas, fornecedores, "
                   "fracionamento, forense", "alta"),
    "empenhos": ("execução orçamentária, anulações, restos a pagar", "alta"),
    "contratos": ("aditivos, vigência, cruzamento com o PNCP", "alta"),
    "quadro_pessoal": ("classificação do beneficiário de verba por vínculo "
                       "(comissionado × carreira) e cruzamento salário × "
                       "indenização", "alta"),
    "mandatarios": ("identificação de conselheiros entre os beneficiários de "
                    "verbas e nos quadros societários", "alta"),
    "despesas_centro_custo": ("eixo de FUNÇÃO: separar fiscalização de "
                              "administração e direção superior", "alta"),
    "comparativo_despesa": ("série histórica por conta contábil e orçado × "
                            "realizado", "media"),
    "convenios": ("repasses a entidades de classe e teste de ciclo eleitoral",
                  "media"),
    "processos_itens": ("sobrepreço por item e cotação de cobertura", "media"),
    "atas": ("deliberações e presença em sessão (conferência dos jetons)",
             "media"),
    "indenizacoes_endpoint": ("evento, trecho, datas e valor unitário de cada "
                              "diária", "media"),
    "pncp_contratos": ("contratos no PNCP e rede entre órgãos do sistema",
                       "media"),
    "licitacoes": ("vinculação contrato × certame", "baixa"),
}


def run(staged) -> list[Finding]:
    achados: list[Finding] = []
    diag = df(staged, "_coleta_diagnostico")

    if diag.empty:
        # análise rodando sobre staged carregado do disco sem o inventário
        diag = pd.DataFrame([
            {"dataset": k, "linhas": 0 if v is None else int(len(v)),
             "colunas": 0 if v is None or v.empty else int(v.shape[1]),
             "ano_min": None, "ano_max": None, "anos_cobertos": 0,
             "anos_faltando": ""}
            for k, v in sorted(staged.items()) if not k.startswith("_")])
        for i, r in diag.iterrows():
            d = staged.get(r["dataset"])
            if d is not None and not d.empty and "_ano" in d.columns:
                anos = sorted({int(a) for a in
                               pd.to_numeric(d["_ano"], errors="coerce")
                               .dropna().unique()})
                if anos:
                    diag.loc[i, ["ano_min", "ano_max", "anos_cobertos"]] = (
                        anos[0], anos[-1], len(anos))
    if diag.empty:
        return achados

    # ---- etapas que falharam com erro (fonte fora do ar, formato mudou)
    if "erro" in diag.columns:
        falhas = diag[diag["erro"].notna() & (diag["erro"].astype(str) != "")]
        if not falhas.empty:
            achados.append(Finding(
                id="diagnostico-etapas-com-erro", frente=FRENTE, gravidade="alta",
                titulo=f"{len(falhas)} etapa(s) da coleta falharam com erro",
                descricao=(
                    "Estas fontes não foram coletadas por erro de rede ou do "
                    "servidor — não por ausência de dado. A coleta seguiu com "
                    "as demais em vez de abortar, mas o que estas fontes "
                    "alimentam está fora deste relatório. Erro de servidor "
                    "(5xx) costuma ser transitório: rodar de novo com "
                    "`--retomar` tenta só o que faltou."),
                evidencia=falhas[["dataset", "linhas", "erro"]],
            ))

    vazios = diag[diag["linhas"] == 0]["dataset"].tolist()
    faltando = [(d, *CRITICOS[d]) for d in vazios if d in CRITICOS]
    graves = [f for f in faltando if f[2] == "alta"]

    achados.append(Finding(
        id="diagnostico-cobertura", frente=FRENTE,
        gravidade="alta" if graves else ("media" if faltando else "info"),
        titulo=(f"{len(graves)} fonte(s) essencial(is) não foram coletadas — "
                "há análises desligadas neste relatório"
                if graves else
                f"Coleta completa: {len(diag[diag['linhas'] > 0])} de "
                f"{len(diag)} datasets com dados"),
        descricao=(
            "Inventário do que a coleta trouxe. **Poucos achados numa frente "
            "cuja fonte veio vazia não é sinal de regularidade — é ausência de "
            "dado.** A tabela mostra, por dataset, quantas linhas vieram e que "
            "intervalo de exercícios foi coberto; a lista de fontes ausentes "
            "diz exatamente qual análise ficou desligada. Fontes vazias são "
            "quase sempre 403 por rajada (rode de novo, o cache preserva o que "
            "já veio) ou mudança no formato de parâmetro do endpoint."),
        evidencia=diag.sort_values("linhas", ascending=False),
        metricas={"datasets_vazios": vazios,
                  "essenciais_ausentes": [f[0] for f in graves]},
    ))

    if faltando:
        achados.append(Finding(
            id="diagnostico-analises-desligadas", frente=FRENTE,
            gravidade="alta" if graves else "media",
            titulo=f"{len(faltando)} análise(s) indisponíveis por falta de fonte",
            descricao=(
                "O que este relatório **não** está medindo, e por quê. Cada "
                "linha é uma pergunta que os dados coletados não respondem — "
                "trate como lacuna a cobrir, não como ausência de problema."),
            evidencia=pd.DataFrame(
                [{"fonte_ausente": d, "analise_desligada": o, "impacto": g}
                 for d, o, g in faltando]),
            como_verificar="Rodar `python main.py fetch` de novo (o cache em "
                           "disco preserva o que já veio, então a nova rodada "
                           "só tenta o que faltou). Se persistir, pedir a base "
                           "por LAI — a indisponibilidade da API é, ela "
                           "própria, achado de transparência.",
            base_legal="Lei 12.527/2011 (LAI), art. 8º, §3º, II e III "
                       "(divulgação em formato aberto e legível por máquina); "
                       "Decreto 8.777/2016.",
        ))

    # ---- cobertura temporal: o pedido do usuário era alcançar o pré-2016
    com_ano = diag[(diag["linhas"] > 0) & diag["ano_min"].notna()]
    if not com_ano.empty:
        curtos = com_ano[pd.to_numeric(com_ano["ano_min"], errors="coerce") > 2016]
        if not curtos.empty:
            achados.append(Finding(
                id="diagnostico-serie-curta", frente=FRENTE, gravidade="media",
                titulo=f"{len(curtos)} dataset(s) não alcançam 2016 — a "
                       "comparação com o período anterior fica prejudicada",
                descricao=(
                    "A sondagem retroativa para quando dois exercícios seguidos "
                    "voltam vazios. Se o portal simplesmente não publica os anos "
                    "antigos, isso é achado de transparência; se parou por "
                    "bloqueio de rajada, uma nova rodada estende a série."),
                evidencia=curtos[["dataset", "ano_min", "ano_max",
                                  "anos_cobertos", "linhas"]],
                base_legal="Lei 12.527/2011, art. 8º; Decreto 7.724/2012, "
                           "art. 7º (histórico das informações).",
            ))
        buracos = com_ano[com_ano["anos_faltando"].fillna("") != ""]
        if not buracos.empty:
            achados.append(Finding(
                id="diagnostico-anos-faltando", frente=FRENTE, gravidade="media",
                titulo=f"{len(buracos)} dataset(s) com exercícios faltando no meio "
                       "da série",
                descricao=(
                    "Buraco no meio da série distorce toda tendência: um ano "
                    "ausente vira 'queda' no gráfico e contamina CAGR, "
                    "sazonalidade e qualquer comparação ano a ano. Confira "
                    "estes exercícios antes de interpretar as curvas."),
                evidencia=buracos[["dataset", "ano_min", "ano_max",
                                   "anos_faltando", "linhas"]],
            ))
    return achados
