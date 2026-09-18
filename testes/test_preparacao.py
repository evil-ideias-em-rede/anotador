import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import unittest
import test_pipeline as legacy
from test_pipeline import Fake, TEXT, doc, annotated
import anotador as a
import preparador as p
from suporte import save, load
from formato import load_debate


class SplitTests(unittest.TestCase):
    def setUp(self):
        legacy.Tests.setUp(self)
        self.config = Path(self.folder.name) / '.config'

    def cli(self, module, arguments, client=None):
        with patch('sys.argv', [module.__name__, '--config', str(self.config)] + arguments):
            if client is None:
                return module.main()
            with patch.object(module, 'Client', client):
                return module.main()

    def test_prepare_all_range_and_reuse_without_api(self):
        source = Path(self.folder.name) / 'source.jsonl'
        source.write_text(''.join(json.dumps({'id': i, 'transcricao': TEXT,
            'metadados': {'assunto': 'Transparência'}})+'\n' for i in (1, 2, 3)))
        class Client(Fake):
            def __init__(self,*args,**kwargs):super().__init__()
            def start(self):pass
        for options, expected in (([], [1,2,3]), (['--inicio','2','--fim','99'],[2,3])):
            output = Path(self.folder.name) / ('prep'+str(len(expected)))
            args=['--entrada',str(source),'--saida',str(output)]+options
            self.assertEqual(self.cli(p,args,Client),0)
            paths=sorted(output.glob('debate_*.json'))
            self.assertEqual([load_debate(x)['fonte']['linha'] for x in paths],expected)
            for path in paths:
                d=load_debate(path)
                self.assertEqual(a.prepared_errors(d),[])
                self.assertNotIn('anotacao',d['falas'][0])
            with patch.object(p,'Client',side_effect=AssertionError('Não repetir preparação')):
                self.assertEqual(self.cli(p,args),0)
            self.assertEqual(load(output/'ultima_preparacao.json')['reutilizados'],len(expected))

    def test_file_and_folder_annotation_never_regroup_or_judge(self):
        d=doc();a.group_speeches(d,Fake());d['status']='preparado_para_anotacao'
        folder=Path(self.folder.name)/'preparados';source=folder/'debate.json';save(source,d)
        save(folder/'ultima_preparacao.json',{'concluidos':1})
        original=source.read_bytes()
        class Client(Fake):
            def __init__(self,*args,**kwargs):super().__init__()
            def start(self):pass
            def ask(self,task,data,validate):
                assert task not in ('segmentacao','avaliar_resumo','equivalencia')
                return super().ask(task,data,validate)
        for source_arg in (source,folder):
            output=Path(self.folder.name)/('out-file' if source_arg.is_file() else 'out-dir')
            args=['--entrada',str(source_arg),'--saida',str(output)]
            with patch.object(a,'group_speeches',side_effect=AssertionError('Não agrupar')):
                self.assertEqual(self.cli(a,args,Client),0)
            results=list(output.glob('*_anotado.json'));self.assertEqual(len(results),1)
            result=load_debate(results[0]);self.assertEqual(result['status'],'anotado_aguardando_revisao_humana')
            self.assertEqual(a.structural_errors(result),[])
            self.assertEqual(source.read_bytes(),original)
            with patch.object(a,'Client',side_effect=AssertionError('Não repetir anotação')):
                self.assertEqual(self.cli(a,args),0)

    def test_all_evidence_failures_continue_and_are_idempotent(self):
        for raw in (None, [], 'texto solto', [{'trecho':None}], [{'trecho':'inexistente'}], [{'trecho':'a'*10000}]):
            class Invalid(Fake):
                def ask(self,task,data,validate):
                    if task=='anotacao':
                        value={'indicadores':[data['dimensao']['indicators'][0]],'justificativa':'Teste.',
                               'evidencias':copy.deepcopy(raw),'pendencias':[]}
                    elif task=='opiniao':
                        value={'resumo':'Opinião.', 'objeto_do_posicionamento':'Transparência','evidencias':copy.deepcopy(raw)}
                    elif task=='propostas':
                        value={'propostas':[{'enunciado':'Ampliar transparência.','evidencia':copy.deepcopy(raw)}]}
                    else:return super().ask(task,data,validate)
                    validate(value)
                    first=copy.deepcopy(value);validate(value)
                    self_outer.assertEqual(first,value)
                    return value
            self_outer=self
            d=doc();c=Invalid();a.group_speeches(d,c)
            for speech in d['falas']:speech['anotacao']=a.annotate_speech(d,speech,c)
            self.assertEqual(a.structural_errors(d),[])
            self.assertTrue(a.evidence_issues(d))
            evidence=d['falas'][0]['anotacao']['propostas'][0]['evidencia']
            self.assertEqual(evidence['recebida'],raw)
            self.assertNotIn('inicio',evidence)

    def test_long_literal_is_accepted_without_truncating(self):
        quote='Evidência literal extensa. '*100
        evidence=a.check_evidence({'trecho':quote},quote,[(0,len(quote))])
        self.assertEqual(evidence['status'],'correspondencia_textual_verificada')
        self.assertEqual(evidence['trecho'],quote)

    def test_changed_source_does_not_overwrite_preparation(self):
        source=Path(self.folder.name)/'source.jsonl';output=Path(self.folder.name)/'prep'
        record={'id':1,'transcricao':TEXT,'metadados':{'assunto':'Tema'}}
        source.write_text(json.dumps(record)+'\n')
        class Client(Fake):
            def __init__(self,*args,**kwargs):super().__init__()
            def start(self):pass
        args=['--entrada',str(source),'--saida',str(output)]
        self.assertEqual(self.cli(p,args,Client),0)
        original=(output/'debate_linha_00001.json').read_bytes()
        record['transcricao']+=' Alteração.';source.write_text(json.dumps(record)+'\n')
        with patch.object(p,'Client',side_effect=AssertionError('Sem chamada')):
            self.assertEqual(self.cli(p,args),1)
        self.assertEqual((output/'debate_linha_00001.json').read_bytes(),original)
