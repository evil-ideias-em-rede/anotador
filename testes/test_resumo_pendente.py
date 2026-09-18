import unittest
from test_pipeline import Fake, annotated
import anotador as a
from contexto import ContextBudget, ContextLimitError

class SummaryPendingTests(unittest.TestCase):
    def test_skip_only_overflowing_speech_summary(self):
        d=annotated(); first=d['falas'][0];second=d['falas'][1]
        class Overflow(Fake):
            def ask(self,task,data,validate):
                if task=='resumo_fala' and data['fala']['id']==first['id']:
                    raise ContextLimitError('Não cabe.')
                return super().ask(task,data,validate)
        first['anotacao']=a.annotate_speech(d,first,Overflow())
        second['anotacao']=a.annotate_speech(d,second,Overflow())
        self.assertIsNone(first['anotacao']['resumo'])
        self.assertEqual(second['anotacao']['status_resumo'],'concluido')
        self.assertEqual(len(a.summary_issues(d)),1)
        self.assertEqual(a.structural_errors(d),[])
    def test_other_errors_still_propagate(self):
        d=annotated()
        class Broken(Fake):
            def ask(self,*args,**kwargs):raise RuntimeError('Autenticação')
        with self.assertRaises(RuntimeError):
            a.summarize_speech(d,d['falas'][0],d['falas'][0]['anotacao'],Broken())
    def test_context_overflow_has_specific_exception(self):
        with self.assertRaises(ContextLimitError):
            ContextBudget(10000).check([{'role':'user','content':'x'*10000}])
    def test_pending_summary_cannot_be_passed_off_as_completed(self):
        d=annotated();d['falas'][0]['anotacao']['status_resumo']='pendente_contexto'
        d['falas'][0]['anotacao']['motivo_resumo_pendente']='Não cabe.'
        self.assertTrue(a.structural_errors(d))
