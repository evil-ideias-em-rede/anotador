import copy
import tempfile
import unittest
from pathlib import Path
from test_pipeline import annotated, doc, Fake
import anotador as a
from formato import readable, save_debate, load_debate, migrate
from suporte import load, save


class FormatTests(unittest.TestCase):
    def test_participants_contain_all_their_speeches_and_only_their_speeches(self):
        d=annotated();result=readable(d)
        self.assertNotIn('falas',result)
        self.assertNotIn('transcricao',result)
        ana=result['participantes'][0]
        self.assertEqual([s['id'] for s in ana['falas']],['f00001','f00003'])
        self.assertNotIn('resumo_opiniao',ana)
        self.assertEqual(ana['falas'][1]['resumo'],d['falas'][2]['anotacao']['resumo'])
        self.assertEqual(len(ana['falas'][0]['interrupcoes']),1)
        self.assertNotIn('mais um minuto',ana['falas'][0]['texto'])
        self.assertNotIn('João',[p['nome'] for p in result['participantes']])
        self.assertEqual(ana['falas'][0]['interrupcoes'][0]['participante'],'João')

    def test_round_trip_and_review_edits_never_silently_ignored(self):
        d=annotated()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'debate.json'
            save_debate(path,d)
            self.assertEqual(load_debate(path),d)
            view=load(path);view['participantes'][0]['falas'][0]['texto']='alterado'
            save(path,view)
            with self.assertRaisesRegex(ValueError,'diverge'):load_debate(path)

    def test_duplicate_interruption_as_speech_is_rejected(self):
        d=annotated();speech=copy.deepcopy(d['falas'][0]);i=speech['interrupcoes'][0]
        speech.update(id='extra',participante=i['participante'],turnos_titular=[i['turno_id']],
                      texto_titular=i['texto'],interrupcoes=[])
        d['falas'].append(speech)
        self.assertTrue(a.structural_errors(d,False))

    def test_short_request_stays_interruption_until_floor_is_granted(self):
        text=('O SR. ANA - Apresentação.\nO SR. BRUNO - Presidente...\n'
              'O SR. ANA - Pois não, tem a palavra.\nO SR. BRUNO - Minha exposição.\n')
        d=a.prepare({'id':1,'transcricao':text,'metadados':{'assunto':'Teste'}},'fonte',1)
        class Decision(Fake):
            def ask(self,task,data,validate):
                candidate=data['candidato_id']
                result={'decisao':'nova_fala' if candidate=='t00004' else 'mesma_fala',
                        'justificativa':'Concessão distingue pedido de exposição.',
                        'mudanca_de_opiniao':None,'observacao_retomada':'Sem mudança.'}
                if candidate=='t00002':
                    assert 'Pois não' in data['turno_seguinte_contexto'][0]['texto']
                validate(result);return result
        a.group_speeches(d,Decision())
        self.assertEqual(len(d['falas']),2)
        self.assertEqual(d['falas'][0]['interrupcoes'][0]['turno_id'],'t00002')
        self.assertEqual(d['falas'][1]['turnos_titular'],['t00004'])
        self.assertEqual(a.structural_errors(d,False),[])

    def test_migration_preserves_source_and_never_combines_speeches(self):
        d=annotated()
        for i,speech in enumerate(d['falas']):
            speech['anotacao'].pop('resumo')
            speech['anotacao']['opiniao']['resumo']=f'Opinião exclusiva número {i}.'
        before=copy.deepcopy(d)
        migrated=migrate(d)
        self.assertEqual(d,before)
        self.assertIn('número 0',migrated['falas'][0]['anotacao']['resumo'])
        self.assertNotIn('número 2',migrated['falas'][0]['anotacao']['resumo'])
        self.assertEqual(a.structural_errors(migrated),[])
        self.assertEqual([s['texto_titular'] for s in migrated['falas']],
                         [s['texto_titular'] for s in d['falas']])
