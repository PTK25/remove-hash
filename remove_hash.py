#!/usr/bin/env python3
"""
Linha de comando — Removedor de Metadados & Alterador de Hash

Uso:
  python3 remove_hash.py <arquivo_ou_pasta> [...] [-o DESTINO]
  python3 remove_hash.py --inspecionar <arquivo> [...]

Exemplos:
  python3 remove_hash.py video.mp4
  python3 remove_hash.py ~/Fotos ~/Videos -o ~/Desktop
  python3 remove_hash.py --inspecionar foto.jpg
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import remove_hash_core as core  # noqa: E402


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"


if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    for _k in list(vars(C)):
        if not _k.startswith("_"):
            setattr(C, _k, "")


def banner():
    print(f"""
{C.CYAN}{C.BOLD}╔══════════════════════════════════════════════════════════╗
║   🛡️  REMOVEDOR DE METADADOS & ALTERADOR DE HASH  v{core.__version__}   ║
║   Offline · sem perda de qualidade · fotos, vídeos, áudio ║
╚══════════════════════════════════════════════════════════╝{C.RESET}""")


def ok(msg):
    print(f"  {C.GREEN}✓{C.RESET}  {msg}")


def info(msg):
    print(f"  {C.BLUE}ℹ{C.RESET}  {msg}")


def warn(msg):
    print(f"  {C.YELLOW}⚠{C.RESET}  {msg}")


def err(msg):
    print(f"  {C.RED}✗{C.RESET}  {msg}")


def bar(i, total, width=24):
    filled = int(width * i / total) if total else 0
    return f"{C.CYAN}[{'█' * filled}{'░' * (width - filled)}] {int(100 * i / total) if total else 0:3d}%{C.RESET}"


def cmd_inspect(paths: list[str]) -> int:
    rc = 0
    for p in paths:
        if not os.path.exists(p):
            err(f"não encontrado: {p}")
            rc = 1
            continue
        if os.path.isdir(p):
            files = core.collect_files([p])
        else:
            files = [p]
        for f in files:
            r = core.inspect_file(f)
            print(f"\n  {C.BOLD}{f}{C.RESET}  {C.DIM}({r['tipo']}, {r['tamanho']}){C.RESET}")
            print(f"  {C.DIM}SHA-256 {r['sha256']}{C.RESET}")
            if r["erro"]:
                err(r["erro"])
                rc = 1
            elif r["metadados"]:
                warn(f"{len(r['metadados'])} metadado(s) encontrado(s):")
                for m in r["metadados"]:
                    print(f"       • {m}")
            else:
                ok("nenhum metadado pessoal encontrado (limpo)")
            for t in r["tecnicos"]:
                print(f"       {C.DIM}· técnico: {t}{C.RESET}")
    print()
    return rc


def cmd_process(args) -> int:
    for p in args.caminhos:
        if not os.path.exists(p):
            err(f"caminho não encontrado: {p}")
            return 1

    files = core.collect_files(args.caminhos)
    if not files:
        err("nenhum arquivo suportado encontrado")
        info("formatos: " + ", ".join(sorted(core.ALL_SUPPORTED)))
        return 1

    needs_ffmpeg = any(core.get_file_type(f) != "image" for f in files)
    ver = core.ffmpeg_version()
    if needs_ffmpeg and not ver:
        err("FFmpeg não encontrado (necessário para vídeos/áudios). Instale com: brew install ffmpeg")
        return 1
    if ver:
        ok(f"FFmpeg {ver}")

    kinds = {"video": 0, "image": 0, "audio": 0}
    total_size = 0
    for f in files:
        kinds[core.get_file_type(f)] += 1
        total_size += os.path.getsize(f)
    base_out = os.path.abspath(args.saida) if args.saida else os.getcwd()
    print(f"""
  {C.BOLD}Resumo{C.RESET}
     Arquivos: {C.BOLD}{len(files)}{C.RESET}  ·  🖼  {kinds['image']} fotos  ·  🎬 {kinds['video']} vídeos  ·  🎵 {kinds['audio']} áudios
     Tamanho:  {core.human_size(total_size)}
     Destino:  {C.CYAN}{base_out}{C.RESET}
""")

    options = core.Options(keep_icc=not args.sem_icc, reencode_fallback=not args.sem_recodificar)
    state = {"current": ""}

    tty = sys.stdout.isatty()

    def on_start(i, n, path):
        state["current"] = os.path.basename(path)
        if tty:
            print(f"  {bar(i, n)}  {C.BOLD}{state['current']}{C.RESET}", end="", flush=True)

    def on_progress(i, frac):
        if tty:
            print(f"\r  {bar(i, len(files))}  {C.BOLD}{state['current']}{C.RESET}  {C.DIM}{int(frac * 100)}%{C.RESET}   ", end="", flush=True)

    def on_done(i, r: core.FileResult):
        if tty:
            print("\r" + " " * 110 + "\r", end="")
        if r.success:
            extra = ""
            if r.method == "convert":
                extra = f" → {os.path.basename(r.output)}"
            elif r.method == "reencode":
                extra = " (recodificado)"
            ok(f"{C.BOLD}{os.path.basename(r.input)}{C.RESET}{extra}  "
               f"{C.DIM}{r.hash_before[:10]}…{C.RESET} → {C.GREEN}{r.hash_after[:10]}…{C.RESET}  "
               f"{C.DIM}{core.human_size(r.size_before)} → {core.human_size(r.size_after)} · {r.seconds}s{C.RESET}")
            if not args.silencioso:
                if r.removed:
                    print(f"       {C.DIM}removido: {', '.join(r.removed)[:160]}{C.RESET}")
                if not r.clean:
                    warn(f"tags restantes: {', '.join(r.remaining)[:160]}")
        elif r.error == "cancelado":
            warn(f"{os.path.basename(r.input)} — cancelado")
        else:
            err(f"{C.BOLD}{os.path.basename(r.input)}{C.RESET} — {r.error}")

    try:
        output_dir, results = core.process_batch(args.caminhos, base_out, options,
                                                 on_start=on_start, on_progress=on_progress, on_done=on_done)
    except KeyboardInterrupt:
        print()
        err("interrompido")
        return 130
    except Exception as e:  # noqa: BLE001
        err(str(e))
        return 1

    n_ok = sum(1 for r in results if r.success)
    n_fail = len(results) - n_ok
    n_clean = sum(1 for r in results if r.success and r.clean)
    print(f"""
{C.CYAN}{'═' * 60}{C.RESET}
  {C.GREEN}✓ Sucesso:{C.RESET} {n_ok}/{len(results)}   {C.RED}✗ Falhas:{C.RESET} {n_fail}   {C.DIM}verificados limpos: {n_clean}{C.RESET}

  {C.BOLD}📁 Saída:{C.RESET} {C.CYAN}{output_dir}{C.RESET}
  {C.DIM}Relatórios: relatorio.txt · relatorio.json{C.RESET}
{C.CYAN}{'═' * 60}{C.RESET}
""")
    if args.abrir and sys.platform == "darwin":
        subprocess.Popen(["open", output_dir])
    return 0 if n_fail == 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="remove_hash.py",
        description="Remove metadados (EXIF, GPS, XMP, tags…) e altera o hash de fotos, vídeos e áudios.",
        epilog="Formatos: " + ", ".join(sorted(core.ALL_SUPPORTED)),
    )
    parser.add_argument("caminhos", nargs="*", help="arquivo(s) ou pasta(s) a processar")
    parser.add_argument("-o", "--saida", help="pasta de destino (padrão: pasta atual)")
    parser.add_argument("-i", "--inspecionar", action="store_true",
                        help="só listar os metadados presentes, sem modificar nada")
    parser.add_argument("--sem-icc", action="store_true", help="remover também o perfil de cor ICC")
    parser.add_argument("--sem-recodificar", action="store_true",
                        help="não recodificar vídeos quando a cópia direta falhar (reporta erro)")
    parser.add_argument("--abrir", action="store_true", help="abrir a pasta de saída no Finder ao terminar")
    parser.add_argument("-s", "--silencioso", action="store_true", help="menos detalhes na saída")
    parser.add_argument("-v", "--versao", action="version", version=f"%(prog)s {core.__version__}")
    args = parser.parse_args(argv)

    banner()
    if not args.caminhos:
        parser.print_help()
        return 1
    if args.inspecionar:
        return cmd_inspect(args.caminhos)
    return cmd_process(args)


if __name__ == "__main__":
    sys.exit(main())
