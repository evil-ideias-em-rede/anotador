import copy
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from suporte import PromptCatalog


class DimensionPromptTests(unittest.TestCase):
    def setUp(self):
        self.catalog = PromptCatalog(Path(__file__).resolve().parents[1] / 'prompts.xml')

    def test_each_dimension_contains_only_its_definition(self):
        for dimension, tag in enumerate(('alinhamento', 'postura', 'credibilidade', 'posicionamento'), 1):
            for role in ('anotador', 'julgador'):
                text = self.catalog.render('anotacao', role, dimension)
                rules = ET.fromstring(text).find('.//regras_dimensoes')
                self.assertEqual([child.tag for child in rules], [tag])
                self.assertIn('FALA INTEGRAL', text)
                self.assertIn('saida_json', text)
                self.assertLess(len(text), len(self.catalog.render('anotacao', role)))

    def test_rendering_does_not_mutate_the_catalog(self):
        original = self.catalog.render('anotacao', 'anotador')
        self.catalog.render('anotacao', 'anotador', 4)
        self.assertEqual(self.catalog.render('anotacao', 'anotador'), original)

    def test_topic_reference_preserved_for_stance(self):
        text = self.catalog.render('anotacao', 'anotador', 4)
        self.assertIn('Use sempre o tema recebido como referência', text)
        self.assertNotIn('<postura>', text)

    def test_other_tasks_unchanged_and_invalid_dimension_rejected(self):
        self.assertEqual(self.catalog.render('segmentacao', 'anotador'),
                         self.catalog.render('segmentacao', 'anotador', 4))
        with self.assertRaises(ValueError):
            self.catalog.render('anotacao', 'anotador', 9)
