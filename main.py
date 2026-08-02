#!/usr/bin/env python3
"""QUEDA — fiscalização de dados abertos do Crea-SP.

Uso:
  python main.py fetch    [--desde AAAA|auto] [--ate AAAA]   # só coleta
  python main.py analyze                                     # checks sobre staged
  python main.py report                                      # regenera o HTML
  python main.py all      [--desde AAAA|auto] [--ate AAAA]   # tudo
  python main.py selftest                                    # pipeline com fixtures, sem rede
  python main.py llm                                         # prepara o modelo local (Ollama)

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

sys.path.insert(0, str(Path(__file__).parent / "src"))

from queda import config  # noqa: E402


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["fetch", "analyze", "report", "all",
                                   "selftest", "llm"])
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
    if args.cmd in ("fetch", "all"):
        try:
            staged = pipeline.build_staged(desde, args.ate, retomar=args.retomar)
        except RateLimited as e:
            print(f"\n⏳ {e}\n\nO progresso já baixado ficou em "
                  f"{config.RAW_DIR}. Rode a MESMA célula/comando de novo com "
                  "`--retomar` — ele pula as etapas já concluídas e continua de "
                  "onde parou. Se insistir, aumente THROTTLE_SECONDS em "
                  "src/queda/config.py.", file=sys.stderr)
            return 2
    else:
        staged = pipeline.load_staged()
        if not staged:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
