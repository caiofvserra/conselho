#!/usr/bin/env python3
"""QUEDA — fiscalização de dados abertos do Crea-SP.

Uso:
  python main.py fetch    [--desde AAAA|auto] [--ate AAAA]   # só coleta
  python main.py analyze                                     # checks sobre staged
  python main.py report                                      # regenera o HTML
  python main.py all      [--desde AAAA|auto] [--ate AAAA]   # tudo
  python main.py selftest                                    # pipeline com fixtures, sem rede
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
    p.add_argument("cmd", choices=["fetch", "analyze", "report", "all", "selftest"])
    p.add_argument("--desde", default="auto",
                   help="ano inicial, ou 'auto' para sondar retroativamente (padrão)")
    p.add_argument("--ate", type=int, default=config.ANO_ATUAL)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main():
    args = _parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from queda import pipeline, report
    from queda.checks import run_all

    if args.cmd == "selftest":
        from queda import selftest
        return selftest.run()

    desde = None if args.desde == "auto" else int(args.desde)
    if args.cmd in ("fetch", "all"):
        staged = pipeline.build_staged(desde, args.ate)
    else:
        staged = pipeline.load_staged()
        if not staged:
            print("Nada em data/staged — rode `python main.py fetch` antes.", file=sys.stderr)
            return 1

    if args.cmd == "fetch":
        print(json.dumps({k: len(v) for k, v in staged.items()}, indent=2))
        return 0

    achados = run_all(staged)
    report.gerar(achados, staged)
    altas = [f for f in achados if f.gravidade == "alta"]
    print(f"\n{len(achados)} achados ({len(altas)} de gravidade alta)")
    for f in altas:
        print(f"  [ALTA] {f.titulo}")
    print(f"\nRelatório: {config.OUTPUT_DIR / 'report.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
