"""Prepara LDS uma única vez: um JSON por debate, com falas integrais e interrupções."""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

from api import Client
from formato import load_debate, save_debate
from anotador import prepare, group_speeches, prepared_errors
from progresso import log
from suporte import PromptCatalog, digest, load, save, require, model_arguments, interval_arguments, check_interval

PREPARATION_VERSION = 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entrada', type=Path, default=Path(__file__).parent / 'dados originais dos debates/PublicHearingBR_LDS.jsonl')
    parser.add_argument('--saida', type=Path, default=Path(__file__).parent / 'preparados')
    interval_arguments(parser)
    model_arguments(parser, 'anotador')
    args = parser.parse_args()
    check_interval(args)
    prompts = PromptCatalog(args.prompts)
    client, completed, reused = None, 0, 0
    failures = []
    with args.entrada.open(encoding='utf-8') as source:
        for line, raw in enumerate(itertools.islice(source, args.inicio - 1, args.fim), args.inicio):
            document = None
            destination = args.saida / f'debate_linha_{line:05d}.json'
            try:
                record = json.loads(raw)
                document = prepare(record, args.entrada, line)
                signature = digest({'fonte': document['fonte'], 'id': document['debate_id'], 'tema': document['tema'],
                    'preparacao': PREPARATION_VERSION, 'prompt': prompts.render('segmentacao', 'anotador'),
                    'modelo': args.modelo, 'endpoint': args.base_url})
                if destination.exists():
                    previous = load_debate(destination)
                    require(previous.get('assinatura_preparacao') == signature,
                            'Já existe preparação de outra fonte/configuração com esse nome. Use outra pasta --saida para preservá-la.')
                    require(not prepared_errors(previous), 'Preparação existente inválida; revise-a ou use outra pasta de saída.')
                    log(f'Preparação reutilizada, sem API: {destination.name}')
                    reused += 1
                    continue
                if client is None:
                    client = Client(args, args.saida / 'cache_preparacao', prompts=prompts)
                    client.start()
                log(f'Preparando linha {line}: {len(document["turnos"])} turnos.')
                group_speeches(document, client)
                document['status'] = 'preparado_para_anotacao'
                document['assinatura_preparacao'] = signature
                document['versao_preparacao'] = PREPARATION_VERSION
                document['modelo_preparacao'] = client.identity()
                document['revisao_humana'] = {'necessaria': True, 'observacao': 'Conferir titularidade, nomes e interrupções. Sem anotação de taxonomia nesta etapa.'}
                errors = prepared_errors(document)
                require(not errors, '; '.join(errors))
                save_debate(destination, document)
                completed += 1
                log(f'Salvo: {destination} | {len(document["falas"])} falas completas.')
            except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
                failures.append({'linha': line, 'erro': str(error)})
                if document is not None:
                    document['erro_preparacao'] = str(error)
                    save(args.saida / 'pendentes' / destination.name, document)
                log(f'Preparação interrompida na linha {line}: {error}')
                break
    save(args.saida / 'ultima_preparacao.json', {'concluidos': completed, 'reutilizados': reused, 'falhas': failures})
    log(f'Preparação encerrada: {completed} concluído(s), {reused} reutilizado(s), {len(failures)} falha(s).')
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
