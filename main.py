#!/usr/bin/env python3
"""QUEDA — fiscalização de dados abertos do Crea-SP.

Uso:
  python main.py faltando [--desde AAAA|auto] [--ate AAAA]   # o que falta, sem rede
  python main.py fetch    [--desde AAAA|auto] [--ate AAAA]   # só coleta
  python main.py analyze                                     # checks sobre staged
  python main.py report                                      # regenera o HTML
  python main.py all      [--desde AAAA|auto] [--ate AAAA]   # tudo
  python main.py selftest                                    # pipeline com fixtures, sem rede
  python main.py llm                                         # prepara o modelo local (Ollama)

`all` vai à rede SÓ pelo que falta (checkpoint por exercício em
data/raw/checkpoints) e SEMPRE entrega o relatório: se a coleta abortar por
bloqueio de ritmo, ele analisa o que já está em data/staged e sai com código 2
para dizer que a coleta ficou incompleta. Rode `faltando` antes para ver o
tamanho do que sobrou.

A camada de IA é opcional e roda 100% local via Ollama — nenhum dado do
conselho sai da máquina. Sem ela, o relatório sai completo, sem o resumo
executivo, sem a leitura das atas e sem o refino da taxonomia.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "src"))

from queda import config  # noqa: E402


def _tem_dado(staged: dict) -> bool:
    """Há dataset com linha, ignorando os metadados da própria coleta.

    `load_staged` devolve também `_coleta_diagnostico` e `_coleta_http`, que o
    `finally` de `build_staged` grava ANTES de a exceção subir. Testar o dicionário
    por verdade — `if not staged` — dava sempre verdadeiro e a guarda "nada a
    analisar" era inalcançável: com data/staged contendo só o ledger, os checks
    rodavam e o relatório saía afirmando ausência de achado sobre ausência de dado.
    """
    return any(len(v) for k, v in staged.items() if not str(k).startswith("_"))


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["fetch", "analyze", "report", "all",
                                   "faltando", "limpar", "selftest", "llm"])
    p.add_argument("--etapa", default="",
                   help="limpar: etapas separadas por vírgula "
                        "(ex.: licitacoes,atas)")
    p.add_argument("--tudo", action="store_true",
                   help="limpar: apagar também os checkpoints COM dado, não só "
                        "os marcadores de janela vazia")
    p.add_argument("--desde", default="auto",
                   help="ano inicial, ou 'auto' para sondar retroativamente (padrão)")
    p.add_argument("--ate", type=int, default=config.ANO_ATUAL)
    p.add_argument("--retomar", action="store_true",
                   help="pular as etapas que já têm dado em disco (use depois "
                        "de uma queda de sessão)")
    p.add_argument("--sem-llm", action="store_true",
                   help="rodar sem a camada de IA mesmo com o modelo local no ar")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main():
    args = _parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log = logging.getLogger("queda")
    log.info("%s", config.onde_estao_os_dados())
    from queda import pipeline, report
    from queda.checks import run_all

    if args.cmd == "selftest":
        from queda import selftest
        return selftest.run()

    if args.cmd == "llm":
        from queda import llm
        print(f"Ollama em {llm.OLLAMA_HOST}, modelo {llm.MODELO_PADRAO}")
        if llm.preparar():
            print("✅ modelo local pronto — `python main.py all` já usa a "
                  "camada de IA")
            print(f"   processador: {llm.onde_roda()}")
            return 0
        print("\n❌ modelo local indisponível. Para instalar:\n"
              "   curl -fsSL https://ollama.com/install.sh | sh\n"
              f"   ollama pull {llm.MODELO_PADRAO}\n"
              f"   (o serviço é iniciado por este comando; log em "
              f"{llm.LOG_SERVIDOR})\n\n"
              "O relatório roda completo sem isto — só ficam de fora o resumo "
              "executivo, a leitura das atas e o refino da taxonomia.",
              file=sys.stderr)
        return 1

    from queda.http import RateLimited
    desde = None if args.desde == "auto" else int(args.desde)

    if args.cmd == "limpar":
        # A instrução anterior era "rode sem --retomar para forçar recoleta", e
        # ela é FALSA: `retomar` só governa o pulo de etapa; a leitura do
        # checkpoint por janela é incondicional (só `QUEDA_CHECKPOINT=0` a
        # desliga). Quem quisesse refazer um período tinha de apagar arquivo à
        # mão dentro do Drive.
        etapas = [e.strip() for e in args.etapa.split(",") if e.strip()]
        if not etapas:
            print("Diga quais etapas: `--etapa licitacoes,atas`. Os nomes são "
                  "os de `python main.py faltando`.", file=sys.stderr)
            return 1
        raiz = config.RAW_DIR / pipeline.CHECKPOINT_SUBDIR
        total = 0
        for etapa in etapas:
            pasta = raiz / etapa
            if not pasta.is_dir():
                print(f"{etapa}: sem checkpoint em {pasta}")
                continue
            padrao = "*" if args.tudo else "*.vazio"
            alvos = [f for f in pasta.rglob(padrao) if f.is_file()]
            for f in alvos:
                f.unlink()
            total += len(alvos)
            print(f"{etapa}: {len(alvos)} checkpoint(s) apagado(s) "
                  f"({'todos' if args.tudo else 'só os de janela vazia'})")
        print(f"\n{total} arquivo(s) removido(s). A próxima rodada vai à rede "
              "por essas janelas.")
        return 0

    if args.cmd == "faltando":
        inv = pipeline.inventario(desde, args.ate)
        # max_colwidth=None: a coluna `exercicios_faltando` É a resposta do
        # comando. Truncá-la com "..." esconderia justamente os anos que o
        # operador veio conferir.
        with pd.option_context("display.max_colwidth", None,
                               "display.width", None,
                               "display.max_rows", None):
            print(inv.to_string(index=False))
        falta = inv[inv["vai_a_rede"]]
        if falta.empty:
            print("\nNada a coletar: toda janela conferida já está em disco. "
                  "`python main.py analyze` entrega o relatório sem rede.")
        else:
            print(f"\n{len(falta)} etapa(s) ainda iriam à rede: "
                  f"{', '.join(falta['etapa'])}.\n"
                  "`python main.py all --retomar` busca só essas — o resto sai "
                  "do disco.")
        return 0

    # Coleta incompleta NÃO cancela a análise. O modo de falhar era este: o
    # portal bloqueava por ritmo no meio, `all` saía com 2, e a rodada inteira
    # terminava sem relatório — inclusive sobre as etapas que já tinham vindo.
    # Quem operava do Colab descobria isso ao abrir um zip de 164 bytes.
    coleta_incompleta = ""
    if args.cmd in ("fetch", "all"):
        try:
            staged = pipeline.build_staged(desde, args.ate, retomar=args.retomar)
        except RateLimited as e:
            coleta_incompleta = str(e)
            print(f"\n⏳ {e}\n\nO progresso já baixado ficou em "
                  f"{config.RAW_DIR}. Rode a MESMA célula/comando de novo com "
                  "`--retomar` — ele vai à rede só pelos exercícios que ainda "
                  "faltam. Se insistir, aumente THROTTLE_SECONDS em "
                  "src/queda/config.py.", file=sys.stderr)
            if args.cmd == "fetch":
                return 2
            print("\nSeguindo para a análise do que JÁ está em data/staged — "
                  "o relatório sai com o diagnóstico da coleta dizendo o que "
                  "faltou.", file=sys.stderr)
            staged = pipeline.load_staged()
            if not _tem_dado(staged):
                print("Nada em data/staged: a coleta abortou antes de gravar a "
                      "primeira etapa. Nada a analisar.", file=sys.stderr)
                return 2
    else:
        staged = pipeline.load_staged()
        if not _tem_dado(staged):
            print("Nada em data/staged — rode `python main.py fetch` antes.", file=sys.stderr)
            return 1

    if args.cmd == "fetch":
        print(json.dumps({k: len(v) for k, v in staged.items()}, indent=2))
        return 0

    achados = run_all(staged)

    # Resumo executivo pelo modelo LOCAL: opcional e não bloqueante. Ele NÃO
    # calcula nada nem vê dado bruto — só redige sobre os números que os checks
    # determinísticos já produziram. Sem Ollama no ar, o relatório sai igual,
    # sem a seção.
    resumo_llm = ""
    if not args.sem_llm:
        from queda import llm
        if llm.disponivel():
            log.info("gerando resumo executivo com %s (local)", llm.MODELO_PADRAO)
            resumo_llm = llm.resumo_executivo([
                {"id": f.id, "frente": f.frente, "gravidade": f.gravidade,
                 "titulo": f.titulo, "metricas": f.metricas}
                for f in achados if f.gravidade in ("alta", "media")][:40])
        else:
            log.info("camada de IA desligada — rode `python main.py llm` para "
                     "preparar o modelo local")

    report.gerar(achados, staged, resumo_llm=resumo_llm)
    altas = [f for f in achados if f.gravidade == "alta"]
    print(f"\n{len(achados)} achados ({len(altas)} de gravidade alta)")
    for f in altas:
        print(f"  [ALTA] {f.titulo}")
    print(f"\nRelatório: {config.OUTPUT_DIR / 'report.html'}")
    if coleta_incompleta:
        # Relatório entregue, coleta pela metade: o código de saída diz a
        # segunda parte, que o relatório sozinho não diria em voz alta.
        print("\n⚠️  A COLETA NÃO COMPLETOU nesta rodada — o relatório acima "
              "cobre só o que já estava em disco.\n    Rode `python main.py "
              "faltando` para ver o que sobrou e `python main.py all "
              "--retomar` para buscar só isso.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
