"""Abre a interface local de revisão humana, sem API de modelos ou dependências extras."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import webbrowser
import xml.etree.ElementTree as ET

from anotador import TAXONOMY

BASE = Path(__file__).resolve().parent
ASSETS = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
          '/estilo.css': ('estilo.css', 'text/css')}


def guide():
    root = ET.parse(BASE / 'prompts.xml').getroot()
    rules = root.find("prompt[@id='anotacao']/regras_dimensoes")
    return [dict(item, orientacao=''.join(rules.find(tag).itertext()).strip())
            for item, tag in zip(TAXONOMY, ('alinhamento', 'postura', 'credibilidade', 'posicionamento'))]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/guia.json':
            payload = json.dumps(guide(), ensure_ascii=False).encode('utf-8')
            mime = 'application/json'
        elif self.path in ASSETS:
            filename, mime = ASSETS[self.path]
            payload = (BASE / 'interface' / filename).read_bytes()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--porta', type=int, default=8765)
    parser.add_argument('--sem-abrir', action='store_true', help='Mostra o endereço sem abrir o navegador.')
    args = parser.parse_args()
    if not 0 <= args.porta <= 65535:
        parser.error('Porta deve estar entre 0 e 65535; use 0 para escolher uma porta livre.')
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.porta), Handler)
    except OSError as error:
        parser.exit(1, f'Não foi possível iniciar: {error}. Tente --porta 0.\n')
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Revisão humana: {url}\nEscolha os JSONs ou a pasta de resultados na interface.\nNenhum modelo será chamado. Ctrl+C encerra o servidor.', flush=True)
    if not args.sem_abrir:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
