"""JSON legível por participante; detalhes de rastreabilidade ficam em arquivo auxiliar."""
from pathlib import Path
from suporte import load, save, require

FORMAT = 'debate_por_participante_v1'


def readable(document):
    participants = []
    for participant in document['participantes']:
        speeches = []
        for speech in document['falas']:
            if speech['participante'] != participant['nome']:
                continue
            annotation = speech.get('anotacao')
            interruptions = [{
                'participante': x['participante'], 'texto': x['texto'],
                'apos_caractere_do_texto': sum(
                    next(t['fim']-t['inicio'] for t in document['turnos'] if t['id']==tid)
                    for tid in speech['turnos_titular'][:speech['turnos_titular'].index(x['apos_turno'])+1]
                ) + 2 * speech['turnos_titular'].index(x['apos_turno']),
                'mudanca_de_opiniao_na_retomada': x.get('mudanca_de_opiniao'),
                'observacao': x.get('observacao_retomada')
            } for x in speech['interrupcoes']]
            pending = []
            def collect(value):
                if isinstance(value, dict):
                    if value.get('status') == 'pendente_revisao_humana':
                        pending.append(value['motivo']);return
                    for child in value.values():collect(child)
                elif isinstance(value, list):
                    for child in value:collect(child)
            if annotation:collect(annotation)
            if annotation and not annotation.get('resumo'):
                pending.append(annotation.get('motivo_resumo_pendente', 'Resumo guiado pela taxonomia ainda não gerado para esta fala.'))
            speeches.append({
                'id': speech['id'], 'ordem_no_debate': speech['ordem_inicio'],
                'texto': speech['texto_titular'],
                'taxonomia': {k: v['indicadores'] for k, v in annotation['dimensoes'].items()} if annotation else None,
                'resumo': annotation.get('resumo') if annotation else None,
                'objeto_do_posicionamento': annotation.get('referencia_posicionamento', annotation['opiniao'].get('objeto_do_posicionamento')) if annotation else None,
                'propostas': [p['enunciado'] for p in annotation['propostas']] if annotation else None,
                'interrupcoes': interruptions,
                'pendencias_revisao': list(dict.fromkeys(pending))})
        participants.append({'nome': participant['nome'], 'falas': speeches})
    return {'formato': FORMAT, 'debate_id': document['debate_id'], 'tema': document['tema'],
            'status': document['status'], 'participantes': participants,
            'notas_revisao': document.get('notas_revisao', [])}


def save_debate(path, document):
    path = Path(path)
    companion = path.parent / 'auditoria' / path.name
    save(companion, document)
    view = readable(document)
    view['arquivo_auditoria'] = str(companion.relative_to(path.parent))
    save(path, view)


def load_debate(path):
    path = Path(path)
    value = load(path)
    if value.get('formato') != FORMAT:
        return value  # Compatibilidade com preparações e resultados anteriores.
    relative = Path(value['arquivo_auditoria'])
    require(not relative.is_absolute() and '..' not in relative.parts, 'Caminho de auditoria inválido.')
    document = load(path.parent / relative)
    expected = readable(document)
    require({k: v for k,v in value.items() if k != 'arquivo_auditoria'} == expected,
            'JSON legível diverge da auditoria; alterações humanas não serão ignoradas. Preserve a revisão em uma cópia e confira os arquivos.')
    return document


def migrate(document):
    """Reorganização local: não altera agrupamentos nem inventa novas inferências."""
    import copy
    result = copy.deepcopy(document)
    for participant in result['participantes']:
        for key in ('resumo_opiniao', 'status_resumo', 'motivo_resumo_pendente'):
            participant.pop(key, None)
    for speech in result['falas']:
        annotation = speech.get('anotacao')
        if not annotation or 'resumo' in annotation:
            continue
        opinion = annotation['opiniao']
        parts = [opinion['resumo']]
        labels = '; '.join(f"{name}: {', '.join(item['indicadores'])}"
                          for name, item in annotation['dimensoes'].items() if item['indicadores'])
        if labels:
            parts.append('Classificação desta fala — ' + labels + '.')
        if opinion.get('objeto_do_posicionamento'):
            parts.append('Objeto do posicionamento: ' + opinion['objeto_do_posicionamento'])
        summary = ' '.join(parts)
        if len(summary) <= 1800:
            annotation.update(resumo=summary, status_resumo='concluido',
                              origem_resumo='reorganizacao_literal_da_opiniao_e_rotulos_existentes')
        else:
            annotation.update(resumo=None, status_resumo='pendente_contexto',
                              motivo_resumo_pendente='Síntese local excede o tamanho do campo; conteúdo original preservado na auditoria.')
    result['notas_revisao'] = ['Reorganização sem API: agrupamentos e classificações anteriores foram preservados. Conferir a titularidade e as interrupções; não houve nova análise semântica.']
    return result


if __name__ == '__main__':
    import argparse
    from anotador import structural_errors
    parser = argparse.ArgumentParser(description='Reorganiza resultado existente por participante, sem API, preservando o original.')
    parser.add_argument('--entrada', type=Path, required=True)
    parser.add_argument('--saida', type=Path, required=True, help='Pasta para as cópias organizadas.')
    args = parser.parse_args()
    paths = [args.entrada] if args.entrada.is_file() else sorted(args.entrada.glob('*_anotado.json'))
    require(paths, 'Nenhum resultado encontrado.')
    for path in paths:
        destination = args.saida / path.name
        require(destination.resolve() != path.resolve(), 'Use outra pasta; o arquivo original será preservado.')
        original = load_debate(path)
        errors = structural_errors(original, annotated=all('anotacao' in s for s in original['falas']))
        require(not errors, '; '.join(errors))
        document = migrate(original)
        errors = structural_errors(document, annotated=all("anotacao" in s for s in document["falas"]))
        require(not errors, "; ".join(errors))
        save_debate(destination, document)
        require(load_debate(destination) == document, 'Falha na conferência da cópia organizada.')
        print(f'Organizado: {destination}')
