"""Verifica o servidor restrito e as regras de revisão sem modelos externos."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from urllib.request import urlopen
from urllib.error import HTTPError

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from visualizar import Handler, ThreadingHTTPServer, guide


class InterfaceTests(unittest.TestCase):
    def test_guide_uses_current_taxonomy_and_prompts(self):
        from anotador import TAXONOMY
        data = guide()
        self.assertEqual([d['dimension'] for d in data], [d['dimension'] for d in TAXONOMY])
        for d in data:
            for label in d['indicators']:
                self.assertIn(label, d['orientacao'])

    def test_server_exposes_only_interface_and_guide(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base + '/guia.json') as response:
                self.assertEqual(len(json.load(response)), 4)
                self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])
            with urlopen(base + '/') as response:
                self.assertIn(b'Caderno de revis', response.read())
            for path in ('/.config', '/api.py', '/../.config', '/dados%20originais%20dos%20debates/PublicHearingBR_LDS.jsonl'):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base + path)
                self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    @unittest.skipUnless(shutil.which('node'), 'Node opcional: necessário apenas para os testes JavaScript.')
    def test_browser_logic(self):
        result = subprocess.run(['node', str(BASE / 'testes/test_interface.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
