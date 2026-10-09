"""Anota vários debates preparados em processos simultâneos, com fila, novas tentativas e progresso."""
from __future__ import annotations

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from anotador import structural_errors
from formato import load_debate
from progresso import log
from suporte import require, save

BASE_DIR = Path(__file__).parent
# Falhas que se repetiriam em todos os debates: interromper tudo em vez de insistir.
FATAL = re.compile(r"HTTP (401|402|403)|Autenticação|Teto de chamadas")


def load_env(path):
    """Carrega CHAVE = valor do arquivo sem sobrescrever variáveis já definidas; nunca imprime valores."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        name = name.strip()
        if sep and name and not name.startswith("#") and name not in os.environ:
            os.environ[name] = value.strip().strip("'\"")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entrada", type=Path, default=BASE_DIR / "preparados")
    parser.add_argument("--saida", type=Path, default=BASE_DIR / "resultados")
    parser.add_argument("--config", type=Path, default=BASE_DIR / ".config")
    parser.add_argument("--env", type=Path, default=BASE_DIR / ".env", help="Arquivo com as chaves (CHAVE = valor).")
    parser.add_argument("--processos", type=int, default=10, help="Debates anotados ao mesmo tempo.")
    parser.add_argument("--paralelo", type=int, default=18, help="Falas simultâneas em cada debate.")
    parser.add_argument("--rodadas", type=int, default=3, help="Tentativas por debate antes de desistir dele.")
    parser.add_argument("--inicio", type=int, default=1, help="Primeira linha do LDS (inclusiva).")
    parser.add_argument("--fim", type=int, default=None, help="Última linha do LDS (inclusiva).")
    parser.add_argument("--reaproveitar", type=Path, action="append", default=[],
                        help="Pasta com resultados anotados válidos (de qualquer modelo); esses debates são pulados. Pode repetir.")
    parser.add_argument("--listar", action="store_true", help="Só mostra o que seria anotado, sem chamar a API.")
    args = parser.parse_args()
    require(args.processos >= 1 and args.paralelo >= 1 and args.rodadas >= 1, "Use valores >= 1.")
    require(args.config.is_file(), f"Configuração não encontrada: {args.config}")
    load_env(args.env)

    reused = {}
    for folder in args.reaproveitar:
        for result in sorted(folder.glob("debate_linha_*_anotado.json")):
            document = load_debate(result)
            if document.get("status") == "anotado_aguardando_revisao_humana" and not structural_errors(document):
                reused.setdefault(result.name[:len("debate_linha_00000")], str(result))
    if reused:
        log(f"Reaproveitados, sem API: {len(reused)} debate(s) de {', '.join(map(str, args.reaproveitar))}.")

    debates = []
    for path in sorted(args.entrada.glob("debate_linha_*.json")):
        if path.stem in reused:
            continue
        line = int(re.search(r"(\d+)", path.stem).group(1))
        if line < args.inicio or (args.fim is not None and line > args.fim):
            continue
        document = load_debate(path)
        if document.get("status") == "preparado_para_anotacao":
            debates.append((len(document["falas"]), path))
    require(debates, "Nenhum debate preparado encontrado no intervalo.")
    # Maiores primeiro: os pequenos preenchem os processos no fim, sem deixar um debate longo por último.
    debates.sort(key=lambda item: -item[0])
    total_falas = sum(n for n, _ in debates)
    if args.listar:
        log(f"Seriam anotados {len(debates)} debate(s), {total_falas} falas; maiores: "
            + ", ".join(f"{p.stem[-5:]} ({n})" for n, p in debates[:5]) + ".")
        return 0

    logs = args.saida / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    pending = queue.Queue()
    for n, path in debates:
        pending.put((n, path, 1))
    lock = threading.Lock()
    state = {"concluidos": [], "falhas": [], "em_andamento": {}, "falas_concluidas": 0}
    # Muitas falhas seguidas sem nenhum sucesso indicam causa comum (rede, configuração): parar em vez de insistir.
    streak = {"falhas_seguidas": 0}
    streak_limit = max(5, 2 * args.processos)
    stop = threading.Event()
    started = time.monotonic()

    def report():
        elapsed = (time.monotonic() - started) / 60
        rate = state["falas_concluidas"] / elapsed if elapsed else 0
        remaining = (total_falas - state["falas_concluidas"]) / rate if rate else 0
        log(f"[{time.strftime('%H:%M')}] {len(state['concluidos'])}/{len(debates)} debates, "
            f"{state['falas_concluidas']}/{total_falas} falas, {len(state['falhas'])} desistência(s), "
            f"{len(state['em_andamento'])} em andamento | {rate:.1f} falas/min, faltam ~{remaining / 60:.1f} h")
        save(args.saida / "distribuicao.json", {**state, "total_debates": len(debates), "total_falas": total_falas,
                                                "reaproveitados": reused})

    def worker():
        while not stop.is_set():
            try:
                n, path, attempt = pending.get_nowait()
            except queue.Empty:
                return
            log_path = logs / f"{path.stem}.log"
            # Teto de chamadas por debate: generoso para novas tentativas, mas limita um laço descontrolado.
            command = [sys.executable, "-u", str(BASE_DIR / "anotador.py"), "--config", str(args.config),
                       "--paralelo", str(args.paralelo), "--max-chamadas", str(n * 7 * 3 + 20),
                       "--entrada", str(path), "--saida", str(args.saida)]
            with lock:
                state["em_andamento"][path.stem] = attempt
            with log_path.open("a", encoding="utf-8") as output:
                output.write(f"\n===== tentativa {attempt} | {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                output.flush()
                code = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT).returncode
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
            lines = [line for line in tail.splitlines() if line.strip()]
            reason = next((line for line in reversed(lines) if line.startswith("Interrompida")), lines[-1] if lines else "")[:300]
            with lock:
                state["em_andamento"].pop(path.stem, None)
                streak["falhas_seguidas"] = 0 if code == 0 else streak["falhas_seguidas"] + 1
                if code == 0:
                    state["concluidos"].append(path.stem)
                    state["falas_concluidas"] += n
                elif FATAL.search(tail):
                    state["falhas"].append({"debate": path.stem, "erro": reason or f"código {code}"})
                    log(f"Erro que afetaria todos os debates ({reason}); nenhum debate novo será iniciado.")
                    stop.set()
                elif streak["falhas_seguidas"] >= streak_limit:
                    state["falhas"].append({"debate": path.stem, "erro": reason or f"código {code}"})
                    log(f"{streak['falhas_seguidas']} falhas seguidas sem nenhum sucesso ({reason}); "
                        "provável causa comum (rede, configuração). Nenhum debate novo será iniciado.")
                    stop.set()
                elif attempt < args.rodadas:
                    log(f"{path.stem} falhou na tentativa {attempt} ({reason}); volta para o fim da fila.")
                    pending.put((n, path, attempt + 1))
                else:
                    state["falhas"].append({"debate": path.stem, "erro": reason or f"código {code}"})
                    log(f"{path.stem}: desistência após {attempt} tentativa(s) ({reason}).")
                report()

    log(f"{len(debates)} debates ({total_falas} falas) em {args.processos} processos × {args.paralelo} falas. "
        f"Logs em {logs}.")
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.processos)]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        stop.set()
        log("Interrompido pelo usuário. Rode o mesmo comando para retomar: o cache preserva o que já foi pago.")
        raise SystemExit(130)
    report()
    log("Concluído." if not state["falhas"] else "Concluído com desistências; rode de novo para retomar só o que faltou (o cache reaproveita o que já foi pago).")
    return 1 if state["falhas"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
