"""Feedback local de progresso; sem dependências e sem alterar chamadas ou resultados."""
from __future__ import annotations

import sys
import threading
import time


def log(message):
    print(message, file=sys.stderr, flush=True)


def progress(stage, completed, total):
    """A barra mede itens concluídos, nunca estima o avanço interno do modelo."""
    ratio = completed / total if total else 1
    filled = int(20 * ratio)
    log(f"{stage} [{'#' * filled}{'-' * (20 - filled)}] {completed}/{total} ({ratio:.0%})")


class Waiting:
    """Anima no terminal; em logs redirecionados emite uma linha a cada 10 segundos."""
    def __init__(self, label, stream=None, interval=None):
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.tty = self.stream.isatty()
        self.interval = interval if interval is not None else (0.2 if self.tty else 10)
        self.stop = threading.Event()
        self.thread = None

    def _render(self, status, frame=0, final=False):
        elapsed = time.monotonic() - self.started
        marker = '|/-\\'[frame % 4] if not final else '-'
        text = f"  {marker} {self.label} | {status} | {elapsed:.0f}s"
        if self.tty:
            self.stream.write('\r\033[2K' + text + ('\n' if final else ''))
        else:
            self.stream.write(text + '\n')
        self.stream.flush()

    def _animate(self):
        frame = 1
        while not self.stop.wait(self.interval):
            self._render('aguardando', frame)
            frame += 1

    def __enter__(self):
        self.started = time.monotonic()
        self._render('aguardando')
        self.thread = threading.Thread(target=self._animate, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, kind, value, traceback):
        self.stop.set()
        self.thread.join()
        self._render('concluído' if kind is None else 'interrompido', final=True)
        return False


def task_label(task, data):
    labels = {'teste_conexao': 'Teste de conexão', 'segmentacao': 'Verificação de fronteira',
              'opiniao': 'Opinião da fala', 'anotacao': 'Taxonomia',
              'propostas': 'Extração de propostas', 'equivalencia': 'Comparação de propostas',
              'resumo_fala': 'Resumo desta fala', 'avaliar_resumo': 'Validação do resumo'}
    base = task.removesuffix('_por_ids')
    label = labels.get(base, 'Processamento')
    if base == 'anotacao':
        dimension = data.get('dimensao', {}).get('id')
        if dimension in (1, 2, 3, 4):
            label += f' — dimensão {dimension}/4'
    if task.endswith('_por_ids'):
        label += ' (recuperação de evidências)'
    return label
