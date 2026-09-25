import sys, json, tempfile, unittest, copy, io, argparse
from pathlib import Path
from unittest.mock import patch
MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
import api, anotador as a, julgador as j
from contexto import ContextBudget, model_limits

def transport_client(args, cache):
    # Testes de transporte isolam a descoberta. Testes de start() verificam o fluxo real separadamente.
    client = api.Client(args, cache)
    client.budget = ContextBudget(32768, output=3000)
    return client

TEXT = ('Introdução da audiência.\nO SR. ANA(Partido - BA) - Defendo a proposta de transparência. ' +
        'O acesso público permite a fiscalização. ' * 30 +
        '\nO SR. PRESIDENTE(João. Partido - RS) - A senhora dispõe de mais um minuto.\n' +
        'O SR. ANA(Partido - BA) - Continuo defendendo a transparência.\n' +
        'O SR. BRUNO - Discordo da proposta de transparência.\n' +
        'O SR. ANA(Partido - BA) - Agora tenho uma nova intervenção.')

def doc():
    return a.prepare({'id': 123, 'transcricao': TEXT, 'metadados': {'assunto': 'Transparência pública'}}, '/tmp/source.jsonl', 1, 300)

class Fake:
    def __init__(self, disagree=False):
        self.disagree = disagree
        self.receipts=[]
        self.calls=0
    def identity(self):
        return {'modelo': 'fake-judge' if self.disagree else 'fake-annotator', 'endpoint': 'local-test', 'versao_codigo': api.VERSION}
    def ask(self, task, data, validate):
        self.calls += 1
        if task == 'segmentacao':
            last = data['turnos'][-1]
            same = 'mais um minuto' in last['texto'] or last['participante']==data['titular']
            result = {'decisao': 'mesma_fala' if same else 'nova_fala', 'justificativa': 'Comparação de fronteira simulada.', 'mudanca_de_opiniao': None, 'observacao_retomada': 'Teste; não inferir causalidade.'}
        elif task == 'propostas':
            content=data['fala']['texto']
            quote='proposta de transparência'
            result={'propostas': [{'enunciado': 'Deve haver transparência.', 'evidencia': {'trecho': quote}}] if quote in content else []}
        elif task == 'opiniao':
            result={'resumo':'Defende transparência.',
                    'evidencias':[{'trecho':data['fala']['texto'].strip()[:30]}]}
        elif task == 'resumo_fala':
            result={'resumo':'Nesta fala, aborda a transparência conforme a taxonomia fornecida.'}
        elif task == 'avaliar_resumo':
            result={'fiel':True,'justificativa':'Síntese compatível neste teste.', 'problemas':[]}
        elif task == 'equivalencia':
            result={'equivalentes': True, 'justificativa': 'Equivalência simulada.'}
        elif task == 'anotacao':
            content=data['fala']['texto']
            quote=content.strip()[:30]
            dimension=data['dimensao']
            label=dimension['indicators'][1 if self.disagree else 0]
            result={'indicadores': [label], 'justificativa': 'Anotação simulada para testar a mecânica.', 'evidencias': [{'indicador': label, 'trecho': quote}], 'pendencias': []} if quote else {'indicadores': [], 'justificativa': 'Sem conteúdo.', 'evidencias': [], 'pendencias': []}
        else:
            raise AssertionError(task)
        validate(result)
        return result

def annotated():
    d=doc(); c=Fake()
    a.group_speeches(d,c)
    for s in d['falas']:
        s['anotacao']=a.annotate_speech(d,s,c)
    d['status']='anotado_aguardando_julgamento'; d['modelo_anotador']=c.identity()
    return d

class Tests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        config=Path(self.folder.name)/'.config'
        config.write_text('[comum]\nprompts='+str(MODULE_DIR/'prompts.xml')+'\n')
        argv=patch('sys.argv',['testes','--config',str(config)])
        argv.start(); self.addCleanup(argv.stop)
        environment=patch.dict('os.environ',{},clear=True)
        environment.start(); self.addCleanup(environment.stop)
    def test_parse_second_real_line_and_full_coverage(self):
        source=MODULE_DIR/'dados originais dos debates/PublicHearingBR_LDS.jsonl'
        if not source.exists(): self.skipTest('Corpus LDS não disponível.')
        with source.open(encoding='utf-8') as file:
            next(file)  # Só atravessa a primeira linha; interpreta exclusivamente a segunda.
            record=json.loads(next(file))
        d=a.prepare(record,str(source),2,1800)
        a.group_speeches(d,None)
        self.assertEqual(len(d['turnos']),125)
        self.assertEqual(len(d['participantes']),25)
        self.assertEqual(a.structural_errors(d,False),[])
    def test_chunks_cover_unicode_exactly(self):
        text='ação! 📚\n'*1001
        blocks=list(a.chunks(text,0,len(text),203))
        self.assertEqual(''.join(b['texto'] for b in blocks),text)
        self.assertTrue(all(b['fim']-b['inicio']<=203 for b in blocks))
        self.assertTrue(all(x['fim']==y['inicio'] for x,y in zip(blocks,blocks[1:])))
    def test_grouping_interruption_and_return_after_floor_transfer(self):
        d=doc(); a.group_speeches(d,Fake())
        self.assertEqual(len(d['falas']),3)
        self.assertEqual(d['falas'][0]['turnos_titular'],['t00001','t00003'])
        self.assertEqual(d['falas'][0]['interrupcoes'][0]['turno_id'],'t00002')
        self.assertNotIn('mais um minuto',d['falas'][0]['texto_titular'])
        self.assertEqual(d['falas'][2]['turnos_titular'],['t00005'])
        self.assertEqual(a.structural_errors(d,False),[])
    def test_complete_synthetic_pipeline(self):
        d=annotated()
        self.assertEqual(a.structural_errors(d),[])
        self.assertEqual(len(d['falas'][0]['anotacao']['propostas']),1)
        self.assertNotIn('blocos',d['falas'][0]['anotacao'])
        self.assertIn('Posicionamento',d['falas'][0]['anotacao']['dimensoes'])
        self.assertTrue(d['falas'][0]['anotacao']['resumo'])
    def test_tampered_evidence_is_rejected(self):
        d=annotated(); d['falas'][0]['anotacao']['dimensoes']['Alinhamento Temático']['evidencias'][0]['trecho']='inventado'
        self.assertTrue(a.structural_errors(d))
    def test_missing_turn_is_rejected(self):
        d=doc(); a.group_speeches(d,Fake()); d['falas'].pop()
        self.assertTrue(a.structural_errors(d,False))
    def test_judge_independent_no_mutation(self):
        d=annotated(); before=copy.deepcopy(d)
        result=j.judge_document(d,Fake(True))
        self.assertEqual(d,before)
        self.assertEqual(result['status'],'avaliado_aguardando_revisao_humana')
        self.assertGreater(result['metricas']['divergencias'],0)
        self.assertEqual(result['metricas']['concordancia_exata_com_alguma_classificacao'],0)
    def test_structure_only_does_not_claim_semantic_validation(self):
        result=j.judge_document(annotated(),None)
        self.assertEqual(result['status'],'estrutura_valida_sem_avaliacao_semantica')
    def test_quote_from_interrupter_rejected_for_titular(self):
        d=doc(); a.group_speeches(d,Fake())
        byid={t['id']:t for t in d['turnos']}
        allowed=[(byid[x]['inicio'],byid[x]['fim']) for x in d['falas'][0]['turnos_titular']]
        with self.assertRaises(ValueError):
            a.resolve_quotes({'trecho':'mais um minuto'},TEXT,allowed)
    def test_client_cache_and_no_secret(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost:9999/v1','--sem-chave'])
        response={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}], 'usage':{'total_tokens':10}}
        with tempfile.TemporaryDirectory() as cache:
            c=transport_client(args,cache)
            with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as call:
                value=c.ask('teste_conexao',{},lambda v:api.require(v.get('ok') is True,'bad'))
            with patch('urllib.request.urlopen',side_effect=AssertionError('cache missed')):
                self.assertEqual(value,c.ask('teste_conexao',{},lambda v:api.require(v.get('ok') is True,'bad')))
            self.assertEqual(c.calls,1);self.assertEqual(c.hits,1)
            self.assertEqual(len(list(Path(cache).glob('*.json'))),1)
    def test_lone_surrogate_response_is_retried_not_fatal(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost:9999/v1','--sem-chave','--tentativas','2'])
        bad={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true,"x":"\udc83"}'}}]}
        good={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
        replies=[io.BytesIO(json.dumps(bad).encode()),io.BytesIO(json.dumps(good).encode())]
        with tempfile.TemporaryDirectory() as cache, patch('api.time.sleep'):
            c=transport_client(args,cache)
            with patch('urllib.request.urlopen',side_effect=replies):
                value=c.ask('teste_conexao',{},lambda v:api.require(v.get('ok') is True,'bad'))
            self.assertEqual(value,{'ok':True});self.assertEqual(c.calls,2)
    def test_client_budget_failure_without_request(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost:9999/v1','--sem-chave'])
        with tempfile.TemporaryDirectory() as cache:
            c=transport_client(args,cache)
            with patch('urllib.request.urlopen',side_effect=AssertionError('should not call')):
                with self.assertRaises(ValueError):
                    c.ask('teste_conexao',{'texto':'ç'*25000},lambda v:None)
            self.assertEqual(c.calls,0)
    def test_bad_json_never_cached(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost:9999/v1','--sem-chave','--tentativas','1'])
        response={'choices':[{'finish_reason':'length','message':{'content':'{"ok":true}'}}]}
        with tempfile.TemporaryDirectory() as cache:
            c=transport_client(args,cache)
            with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())):
                with self.assertRaises(RuntimeError):c.ask('teste_conexao',{},lambda v:None)
            self.assertEqual(list(Path(cache).glob('*.json')),[])


    def test_config_common_inherited_and_role_override(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'.config'
            path.write_text('[comum]\nmodelo=comum\nbase_url=http://localhost/v1\ntoken=segredo-ficticio\n[anotador]\nmodelo=\n[julgador]\nmodelo=juiz\n[processamento]\nbloco_caracteres=900\n')
            with patch('sys.argv',['teste','--config',str(path)]), patch.dict('os.environ',{},clear=True):
                p=argparse.ArgumentParser();api.model_arguments(p,'anotador'); args=p.parse_args()
                self.assertEqual(args.modelo,'comum')
                self.assertEqual(args._config_token,'segredo-ficticio')
                p=argparse.ArgumentParser();api.model_arguments(p,'julgador'); self.assertEqual(p.parse_args().modelo,'juiz')
    def test_cli_over_environment_over_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'.config';path.write_text('[comum]\nmodelo=arquivo\n')
            with patch('sys.argv',['teste','--config',str(path)]), patch.dict('os.environ',{'ANNOTATOR_MODEL':'ambiente'},clear=True):
                p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
                self.assertEqual(p.parse_args().modelo,'ambiente')
                self.assertEqual(p.parse_args(['--modelo','terminal']).modelo,'terminal')
    def test_malformed_config_does_not_echo_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'.config';path.write_text('TOKEN-FICTICIO-MUITO-SECRETO')
            with self.assertRaises(ValueError) as error:api.read_config(path)
            self.assertNotIn('TOKEN-FICTICIO',str(error.exception))
    def test_xml_changes_change_fingerprint(self):
        source=Path(api.__file__).parent/'prompts.xml'
        catalog=api.PromptCatalog(source)
        import xml.etree.ElementTree as ET
        for name in catalog.items:
            self.assertEqual(ET.fromstring(catalog.render(name,'julgador')).tag,'instrucoes')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'prompts.xml';path.write_text(source.read_text().replace('FALA INTEGRAL','FALA COMPLETA'))
            self.assertNotEqual(catalog.sha256,api.PromptCatalog(path).sha256)
    def test_every_classification_receives_full_speech(self):
        seen=[]
        class Capture(Fake):
            def ask(self,task,data,validate):
                if task in ('opiniao','anotacao','propostas'):
                    seen.append((task,copy.deepcopy(data)))
                return super().ask(task,data,validate)
        d=doc();c=Capture();a.group_speeches(d,c)
        a.annotate_speech(d,d['falas'][0],c)
        self.assertEqual(len(seen),6)
        for task,data in seen:
            self.assertEqual(data['fala']['texto'],d['falas'][0]['texto_titular'])
            self.assertIn('Continuo defendendo',data['fala']['texto'])
            self.assertNotIn('mais um minuto',data['fala']['texto'])
            self.assertNotIn('bloco',data)
        self.assertEqual(sum(task=='anotacao' for task,_ in seen),4)

    def test_stance_uses_same_theme_and_only_the_current_full_speech(self):
        d=doc();a.group_speeches(d,Fake());seen=[]
        class Capture(Fake):
            def ask(self,task,data,validate):
                if task=='anotacao' and data['dimensao']['id']==4:
                    seen.append(copy.deepcopy(data))
                return super().ask(task,data,validate)
        for speech in d['falas']:
            result=a.annotate_speech(d,speech,Capture())
            self.assertNotIn('objeto_do_posicionamento',result['opiniao'])
            self.assertEqual(result['referencia_posicionamento'],d['tema'])
            self.assertEqual(result['dimensoes']['Posicionamento']['indicadores'],['Favorável'])
        for data,speech in zip(seen,d['falas']):
            self.assertEqual(set(data),{'tema','fala','dimensao'})
            self.assertEqual(data['tema'],d['tema'])
            self.assertEqual(data['fala']['texto'],speech['texto_titular'])
        self.assertEqual(len(seen),len(d['falas']))

    def test_no_proposal_still_classifies_all_dimensions(self):
        class NoProposal(Fake):
            def ask(self,task,data,validate):
                if task=='propostas':
                    value={'propostas':[]};validate(value);return value
                return super().ask(task,data,validate)
        d=doc();a.group_speeches(d,Fake())
        value=a.annotate_speech(d,d['falas'][0],NoProposal())
        self.assertEqual(value['propostas'],[])
        self.assertEqual(len(value['dimensoes']),4)

    def test_uncertain_boundary_stops_instead_of_splitting(self):
        class Uncertain(Fake):
            def ask(self,task,data,validate):
                value={'decisao':'incerto','justificativa':'Sem certeza.',
                       'mudanca_de_opiniao':None,'observacao_retomada':'Revisar.'}
                validate(value);return value
        with self.assertRaisesRegex(ValueError,'não será dividida'):
            a.group_speeches(doc(),Uncertain())

    def test_different_stance_objects_not_counted_as_agreement(self):
        original=annotated()['falas'][0]['anotacao'];other=copy.deepcopy(original)
        other['referencia_posicionamento']='Outro tema.'
        result=j.compare_annotations(original,other)[-1]
        self.assertIsNone(result['concordancia_exata'])
        self.assertTrue(result['revisao_necessaria'])

    def test_summary_receives_only_one_complete_speech(self):
        d=doc();a.group_speeches(d,Fake());seen=[]
        class Summary(Fake):
            def ask(self,task,data,validate):
                if task=='resumo_fala':seen.append(data)
                return super().ask(task,data,validate)
        for speech in d['falas']:a.annotate_speech(d,speech,Summary())
        self.assertEqual(len(seen),len(d['falas']))
        for data,speech in zip(seen,d['falas']):
            self.assertEqual(data['fala']['texto'],speech['texto_titular'])
            self.assertEqual(set(data['taxonomia']),{dim['dimension'] for dim in a.TAXONOMY})
            self.assertNotIn('analises_falas',data)

    def test_boundary_receives_complete_turns(self):
        d=doc();seen=[]
        class Capture(Fake):
            def ask(self,task,data,validate):
                if task=='segmentacao':seen.extend(data['turnos'])
                return super().ask(task,data,validate)
        a.group_speeches(d,Capture())
        byid={t['id']:t for t in d['turnos']}
        for t in seen:
            original=byid[t['id']]
            self.assertEqual(t['texto'],TEXT[original['inicio']:original['fim']])
            self.assertFalse(t['contexto_parcial'])

    def test_full_speech_overflow_fails_before_any_request(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost/v1','--sem-chave'])
        text='O SR. ANA - '+'Texto integral extenso. '*3000
        d=a.prepare({'id':1,'transcricao':text,'metadados':{'assunto':'Teste'}},'fonte',1)
        a.group_speeches(d,None)
        c=transport_client(args,Path(self.folder.name)/'cache')
        with patch('urllib.request.urlopen',side_effect=AssertionError('Não enviar')):
            with self.assertRaisesRegex(ValueError,'fala não será dividida'):
                a.annotate_speech(d,d['falas'][0],c)
        self.assertEqual(c.calls,0)
        self.assertEqual(d['falas'][0]['texto_titular'],text[len('O SR. ANA -'):])

    def test_missing_stance_object_does_not_force_neutral(self):
        class NoStance(Fake):
            def ask(self,task,data,validate):
                if task=='opiniao':
                    value={'resumo':'Fala procedimental.', 'objeto_do_posicionamento':None,'evidencias':[]}
                elif task=='anotacao' and data['dimensao']['id']==4:
                    value={'indicadores':[],'justificativa':'Sem posicionamento identificável.',
                           'evidencias':[],'pendencias':[]}
                else:return super().ask(task,data,validate)
                validate(value);return value
        d=doc();a.group_speeches(d,Fake())
        result=a.annotate_speech(d,d['falas'][0],NoStance())
        self.assertEqual(result['dimensoes']['Posicionamento']['indicadores'],[])

    def test_interruption_longer_than_old_limit_keeps_single_speech(self):
        text=('O SR. ANA - Exposição inicial.\nO SR. PRESIDENTE - A senhora dispõe de mais um minuto. '+
              'Ajuste técnico. '*100+'\nO SR. ANA - Continuação.\nO SR. BRUNO - Outra exposição.')
        d=a.prepare({'id':1,'transcricao':text,'metadados':{'assunto':'Teste'}},'fonte',1)
        a.group_speeches(d,Fake())
        self.assertEqual(len(d['falas']),2)
        self.assertEqual(d['falas'][0]['turnos_titular'],['t00001','t00003'])
        self.assertGreater(len(d['falas'][0]['interrupcoes'][0]['texto']),1200)
        self.assertEqual(a.structural_errors(d,False),[])

    def test_adjacent_same_speaker_can_have_new_floor(self):
        class Ended(Fake):
            def ask(self,task,data,validate):
                if task=='segmentacao':
                    value={'decisao':'nova_fala','justificativa':'Concessão de nova rodada.',
                           'mudanca_de_opiniao':None,'observacao_retomada':'Nova fala.'}
                    validate(value);return value
                return super().ask(task,data,validate)
        text='O SR. ANA - Encerro.\nO SR. ANA - Recebo novamente a palavra.'
        d=a.prepare({'id':1,'transcricao':text,'metadados':{'assunto':'Teste'}},'fonte',1)
        a.group_speeches(d,Ended());self.assertEqual(len(d['falas']),2)

    def test_old_format_rejected_clearly(self):
        d=annotated();d['versao']='3.1.0'
        self.assertIn('Formato antigo',a.structural_errors(d)[0])

    def test_judge_flags_unfaithful_summary(self):
        class BadSummary(Fake):
            def ask(self,task,data,validate):
                if task=='avaliar_resumo':
                    value={'fiel':False,'justificativa':'Atribuição indevida.', 'problemas':['Resumo extrapola.']}
                    validate(value);return value
                return super().ask(task,data,validate)
        result=j.judge_document(annotated(),BadSummary())
        self.assertTrue(any(x['tipo']=='resumo_inconsistente' for x in result['fila_revisao']))

    def test_invalid_response_receives_bounded_feedback(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost:9999/v1','--sem-chave','--tentativas','2'])
        responses=[{'choices':[{'finish_reason':'stop','message':{'content':'{"ok":false}'}}]}, {'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}]
        sent=[]
        def respond(request,**kwargs):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps(responses[len(sent)-1]).encode())
        with tempfile.TemporaryDirectory() as cache:
            c=transport_client(args,cache)
            with patch('urllib.request.urlopen',side_effect=respond),patch('time.sleep'):
                c.ask('teste_conexao',{},lambda v:api.require(v.get('ok') is True,'ok precisa ser true'))
        self.assertEqual(len(sent),2)
        self.assertIn('<correcao_formato>',sent[1]['messages'][0]['content'])
        self.assertLessEqual(c.budget.estimate(sent[1]['messages']) + c.budget.output,c.budget.total)
    def test_credentials_not_written_to_receipts(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost:9999/v1'])
        args._config_token='SEGREDO-SO-PARA-TESTE'
        response={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
        with tempfile.TemporaryDirectory() as cache, patch.dict('os.environ',{},clear=True):
            c=transport_client(args,cache)
            with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())):
                c.ask('teste_conexao',{},lambda v:None)
            self.assertNotIn(args._config_token,''.join(p.read_text() for p in Path(cache).glob('*.json')))

    def test_context_fraction_includes_output_and_rejects_overflow(self):
        budget=ContextBudget(32000,0.75,output=4000)
        self.assertEqual(budget.total,24000)
        self.assertEqual(budget.output,4000)
        self.assertLessEqual(budget.check([{'role':'user','content':'texto'}])+budget.output,24000)
        with self.assertRaises(ValueError):budget.check([{'role':'user','content':'a'*20000}])
        for invalid in (0.69,0.81):
            with self.assertRaises(ValueError):ContextBudget(32000,invalid)
    def test_model_metadata_exact_match_and_smallest_limits(self):
        payload={'data':[{'id':'outro','context_length':999999}, {'id':'fake','context_length':128000,'top_provider':{'context_length':64000,'max_completion_tokens':2000}}]}
        self.assertEqual(model_limits(payload,'fake'),{'context':64000,'output':2000})
        self.assertEqual(model_limits(payload,'ausente'),{'context':0,'output':0})
        self.assertEqual(model_limits({'data':[{'id':'fake','max_tokens':100000}]},'fake')['context'],0)
    def test_start_discovers_context_then_probes_without_debate(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost/v1','--sem-chave'])
        requests=[]
        def respond(request,**kwargs):
            requests.append(request)
            if request.data is None:
                value={'data':[{'id':'fake','context_length':64000,'max_output_tokens':3000}]}
            else:
                body=json.loads(request.data)
                self.assertEqual(body['max_completion_tokens'],3000)
                self.assertIn('tarefa="teste_conexao"',body['messages'][0]['content'])
                self.assertEqual(body['messages'][1]['content'],'{}')
                value={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}], 'usage':{'prompt_tokens':500}}
            return io.BytesIO(json.dumps(value).encode())
        with tempfile.TemporaryDirectory() as folder:
            client=api.Client(args,Path(folder)/'cache')
            with patch('urllib.request.urlopen',side_effect=respond):client.start()
            self.assertEqual(client.budget.context,64000)
            self.assertEqual(client.budget.total,48000)
            self.assertTrue((Path(folder)/'verificacao_api.json').is_file())
        self.assertEqual(len(requests),2)
    def test_unknown_window_aborts_before_generation(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost/v1','--sem-chave'])
        with tempfile.TemporaryDirectory() as folder:
            client=api.Client(args,folder)
            with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"data":[{"id":"fake"}]}')) as call:
                with self.assertRaisesRegex(ValueError,'contexto_tokens'):client.start()
                self.assertEqual(call.call_count,1)
    def test_manual_context_fallback_and_probe_not_cached(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost/v1','--sem-chave','--contexto-tokens','64000'])
        def respond(request,**kwargs):
            data={'data':[{'id':'fake'}]} if request.data is None else {'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
            return io.BytesIO(json.dumps(data).encode())
        with tempfile.TemporaryDirectory() as folder, patch('urllib.request.urlopen',side_effect=respond) as call:
            api.Client(args,folder).start()
            api.Client(args,folder).start()
            self.assertEqual(call.call_count,4)
    def test_api_sends_the_annotation_prompt_loaded_from_file(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost/v1','--sem-chave'])
        response={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
        with tempfile.TemporaryDirectory() as folder:
            client=transport_client(args,folder)
            with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as request:
                client.ask('anotacao',{'bloco':'teste'},lambda x:None)
            sent=json.loads(request.call_args.args[0].data)['messages'][0]['content']
            self.assertIn('tarefa="anotacao"',sent)
            self.assertIn('FALA INTEGRAL',sent)
    def test_judge_interval_refers_to_source_line_not_file_order(self):
        root=Path(self.folder.name)/'originais';root.mkdir()
        for name,line in (('z',2),('a',9)):
            d=annotated();d['fonte']['linha']=line
            api.save(root/name/'debate.json',d)
        output=Path(self.folder.name)/'julgados'
        config=Path(self.folder.name)/'.config'
        with patch('sys.argv',['julgador','--config',str(config),'--entrada',str(root),'--saida',str(output),'--somente-estrutura','--inicio','2','--fim','2']):
            self.assertEqual(j.main(),0)
        results=list(output.glob('avaliacao_*.json'))
        self.assertEqual(len(results),1)
        self.assertIn('/z/',api.load(results[0])['arquivo_anotado'])

    def test_whitespace_recovery_preserves_exact_source(self):
        source='Defendo a\n\n transparência\u00a0pública.'
        value={'trecho':'Defendo a transparência pública.'}
        a.resolve_quotes(value,source,[(0,len(source))])
        self.assertEqual(value['trecho'],source)
        self.assertEqual(value['normalizacao'],'somente_espacos')
        a.evidence_valid(value,source,[(0,len(source))])
    def test_whitespace_recovery_does_not_remove_negation(self):
        source='Não defendo a transparência irrestrita.'
        with self.assertRaises(api.EvidenceError):
            a.resolve_quotes({'trecho':'Sou favorável à transparência irrestrita.'},source,[(0,len(source))])
    def test_proposal_recovery_selects_source_by_id(self):
        source='A publicidade dos dados deve ser ampliada.'
        calls=[]
        class Recover:
            def ask(self,task,data,validate):
                calls.append(task)
                evidence=({'trecho':'Devemos publicar todos os dados.'} if task=='propostas'
                          else {'id_trecho':data['trechos_disponiveis'][0]['id']})
                result={'propostas':[{'enunciado':'A publicidade dos dados deve ser ampliada.','evidencia':evidence}]}
                validate(result);return result
        def validate(result):
            for item in result['propostas']:
                a.resolve_quotes(item['evidencia'],source,[(0,len(source))])
                a.evidence_valid(item['evidencia'],source,[(0,len(source))])
        result=a.ask_grounded(Recover(),'propostas',{'bloco':{'inicio':0,'fim':len(source),'texto':source}},validate,source,[(0,len(source))])
        self.assertEqual(calls,['propostas'])
        self.assertEqual(result['propostas'][0]['evidencia']['status'],'pendente_revisao_humana')
    def test_evidence_with_unselected_label_becomes_serializable_pending(self):
        source='Sou favorável à transparência irrestrita.'
        class Mislabeled:
            def ask(self,task,data,validate):
                result={'indicadores':['Favorável'],'justificativa':'Teste.','pendencias':[],
                        'evidencias':[{'indicador':'Favor','trecho':source}]}
                validate(result);return result
        result=a.ask_grounded(Mislabeled(),'anotacao',{},lambda value:json.dumps(value),source,[(0,len(source))])
        self.assertEqual(result['evidencias'][0]['status'],'pendente_revisao_humana')
        self.assertEqual(result['evidencias'][0]['recebida']['indicador'],'Favor')
    def test_pending_objects_are_accepted_as_text(self):
        source='Sou favorável à transparência irrestrita.'
        dimension=next(d for d in a.TAXONOMY if d['id']==4)
        class Wrapped:
            def ask(self,task,data,validate):
                result={'indicadores':[],'justificativa':'Teste.','evidencias':[],
                        'pendencias':[{'tipo':'contexto','descricao':'Conferir o tema.'},{'texto':'Fala breve.'}]}
                validate(result);return result
        validate=lambda value:a.validate_annotation(value,dimension,source,[(0,len(source))])
        result=a.ask_grounded(Wrapped(),'anotacao',{},validate,source,[(0,len(source))])
        self.assertEqual(result['pendencias'],['contexto: Conferir o tema.','Fala breve.'])
        with self.assertRaises(ValueError):
            a.ask_grounded(type('Empty',(),{'ask':lambda self,t,d,v:v({'indicadores':[],'justificativa':'Teste.',
                'evidencias':[],'pendencias':[{'tipo':''}]})})(),'anotacao',{},validate,source,[(0,len(source))])
    def test_quote_in_extra_field_is_format_error_not_evidence_failure(self):
        class Extra(Fake):
            def ask(self,task,data,validate):
                if task!='opiniao':
                    return super().ask(task,data,validate)
                result={'resumo':'Opinião.','evidencias':[],'evidencia_adicional':{'trecho':'Frase que não está na fala.'}}
                validate(result);return result
        d=doc();a.group_speeches(d,Fake())
        with self.assertRaises(ValueError) as caught:
            a.annotate_speech(d,d['falas'][0],Extra())
        self.assertNotIsInstance(caught.exception,a.EvidenceError)
        self.assertIn('Campos de opinião',str(caught.exception))
    def test_model_output_failure_becomes_pending_without_stopping_debate(self):
        from formato import readable
        class Looping(Fake):
            def ask(self,task,data,validate):
                if task=='anotacao' and data['dimensao']['id']==2:
                    raise api.ModelOutputError('Resposta truncada/recusada (finish_reason=length).')
                if task=='resumo_fala':
                    raise api.ModelOutputError('Falha após 4 tentativas: Resposta inválida: Campo do resumo da fala inválido.')
                return super().ask(task,data,validate)
        d=doc();a.group_speeches(d,Fake())
        for s in d['falas']:
            s['anotacao']=a.annotate_speech(d,s,Looping())
        d['resumo_por_fala']=True
        self.assertEqual(a.structural_errors(d),[])
        first=d['falas'][0]['anotacao']
        self.assertEqual(first['dimensoes']['Postura do Orador']['indicadores'],[])
        self.assertEqual([f['dimensao'] for f in first['falhas_modelo']],['Postura do Orador'])
        self.assertEqual(first['status_resumo'],'pendente_falha_modelo')
        self.assertEqual(len(a.summary_issues(d)),len(d['falas']))
        self.assertFalse(any('Falha do modelo' in i['motivo'] for i in a.evidence_issues(d)))
        pending=readable(d)['participantes'][0]['falas'][0]['pendencias_revisao']
        self.assertTrue(any('Postura' in p or 'truncada' in p for p in pending))
    def test_network_failure_still_stops_debate(self):
        class Offline(Fake):
            def ask(self,task,data,validate):
                if task=='anotacao':
                    raise RuntimeError('Falha após 4 tentativas: TimeoutError')
                return super().ask(task,data,validate)
        d=doc();a.group_speeches(d,Fake())
        with self.assertRaises(RuntimeError) as caught:
            a.annotate_speech(d,d['falas'][0],Offline())
        self.assertNotIsInstance(caught.exception,api.ModelOutputError)
    def test_annotation_recovery_selects_source_by_id(self):
        seen=[]
        class Recover(Fake):
            def ask(self,task,data,validate):
                if task not in ('anotacao','anotacao_por_ids'):
                    return super().ask(task,data,validate)
                seen.append(data['fala']['texto'])
                label=data['dimensao']['indicators'][0]
                ev=({'trecho':'Uma frase que não está no texto.'} if task=='anotacao'
                    else {'id_trecho':data['trechos_disponiveis'][0]['id']})
                result={'indicadores':[label],'justificativa':'Teste da referência por ID.',
                        'evidencias':[{'indicador':label,**ev}],'pendencias':[]}
                validate(result);return result
        d=doc();a.group_speeches(d,Fake())
        result=a.annotate_speech(d,d['falas'][0],Recover())
        for value in result['dimensoes'].values():
            self.assertEqual(value['evidencias'][0]['status'],'pendente_revisao_humana')
        self.assertTrue(all(t==d['falas'][0]['texto_titular'] for t in seen))

    def test_evidence_failure_saved_outside_valid_cache(self):
        p=argparse.ArgumentParser();api.model_arguments(p,'anotador')
        args=p.parse_args(['--modelo','fake','--base-url','http://localhost/v1','--sem-chave'])
        response={'choices':[{'finish_reason':'stop','message':{'content':'{"trecho":"inventado"}'}}]}
        with tempfile.TemporaryDirectory() as folder:
            client=transport_client(args,folder)
            def validate(value):a.resolve_quotes(value,'fonte real',[(0,10)])
            with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as call:
                with self.assertRaises(api.EvidenceError):client.ask('propostas',{},validate)
                self.assertEqual(call.call_count,1)
            self.assertFalse(list(Path(folder).glob('*.json')))
            failed=list((Path(folder)/'falhas').glob('*.json'))
            self.assertEqual(len(failed),1)
            self.assertEqual(api.load(failed[0])['resposta_rejeitada']['trecho'],'inventado')

if __name__=='__main__': unittest.main(verbosity=2)
