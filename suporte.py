"""Configuração, catálogo de prompts e persistência compartilhados."""
from __future__ import annotations

import argparse
import configparser
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

VERSION = "5.0.0"
# A versão 5 registra evidências pendentes sem bloquear a anotação.
CACHE_VERSION = "5.0.0"
BASE_DIR = Path(__file__).resolve().parent


class EvidenceError(ValueError):
    """Citação não ancorada: a pipeline deve recorrer à seleção de trechos por ID."""


class ModelOutputError(RuntimeError):
    """O modelo respondeu, mas a saída não serviu (cortada ou rejeitada em todas as tentativas).
    Diferente de falhas de rede, crédito ou autenticação, pode virar pendência de uma única tarefa."""


def read_config(path):
    config = configparser.ConfigParser(interpolation=None)
    require(Path(path).is_file(), f"Arquivo de configuração não encontrado: {path}")
    try:
        with Path(path).open(encoding="utf-8") as file:
            config.read_file(file)
    except configparser.Error:
        # Erros do ConfigParser podem incluir linhas com tokens: não reproduza a exceção.
        raise ValueError("Configuração INI inválida. Confira seções, chaves duplicadas e sintaxe.") from None
    allowed = {"comum", "anotador", "julgador", "processamento"}
    require(set(config.sections()) <= allowed and not config.defaults(), "Seção desconhecida na configuração.")
    return config


class PromptCatalog:
    def __init__(self, path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        require(b"<!DOCTYPE" not in raw and b"<!ENTITY" not in raw, "Não use DTD ou entidades no XML de prompts.")
        try:
            self.root = ET.fromstring(raw)
        except ET.ParseError:
            raise ValueError("XML de prompts inválido.") from None
        require(self.root.tag == "prompts" and self.root.find("comum") is not None, "Catálogo de prompts incompleto.")
        self.items = {node.get("id"): node for node in self.root.findall("prompt")}
        require(len(self.items) == len(self.root.findall("prompt")) and None not in self.items, "IDs de prompt duplicados/ausentes.")
        required = {"segmentacao", "propostas", "equivalencia", "anotacao", "teste_conexao",
                    "propostas_por_ids", "anotacao_por_ids", "opiniao", "opiniao_por_ids",
                    "resumo_fala", "avaliar_resumo"}
        require(required <= self.items.keys(), "Faltam prompts obrigatórios no catálogo.")
        require(self.root.find("papel_julgador") is not None, "Papel do julgador ausente.")
        self.sha256 = hashlib.sha256(raw).hexdigest()

    def render(self, name, role, dimension_id=None):
        require(name in self.items, f"Prompt não encontrado: {name}")
        root = ET.Element("instrucoes", versao=self.root.get("versao", ""), tarefa=name, papel=role)
        root.append(copy.deepcopy(self.root.find("comum")))
        inherited = self.items[name].get("herda")
        if inherited:
            require(inherited in self.items, "Prompt-base não encontrado.")
            base = copy.deepcopy(self.items[inherited])
            for element in list(base):
                if element.tag in ("saida_json", "saida_divisao", "evidencias"):
                    base.remove(element)
            root.append(base)
        root.append(copy.deepcopy(self.items[name]))
        if name in ("anotacao", "anotacao_por_ids") and dimension_id is not None:
            tags = {1: "alinhamento", 2: "postura", 3: "credibilidade", 4: "posicionamento"}
            require(type(dimension_id) is int and dimension_id in tags, "Dimensão desconhecida para o prompt.")
            for rules in root.iter("regras_dimensoes"):
                for rule in list(rules):
                    if rule.tag != tags[dimension_id]:
                        rules.remove(rule)
        if role == "julgador":
            root.append(copy.deepcopy(self.root.find("papel_julgador")))
        return ET.tostring(root, encoding="unicode")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def save(path, value):
    """Substituição atômica: uma interrupção não deixa JSON parcialmente escrito."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".parcial-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text_field(value, maximum=1200, campo=None):
    require(isinstance(value, str) and bool(value.strip()), f"Texto obrigatório{f' em {campo!r}' if campo else ''}.")
    # A mensagem volta ao modelo na nova tentativa: nomear o campo e o tamanho recebido permite corrigir.
    require(len(value) <= maximum, f"Texto obrigatório com até {maximum} caracteres"
            + (f" no campo {campo!r} (recebido: {len(value)}); reescreva esse campo mais curto." if campo else "."))
    return value


def model_arguments(parser, role):
    prefix = "ANNOTATOR" if role == "anotador" else "JUDGE"
    parser.add_argument("--modelo", default=os.getenv(prefix + "_MODEL", os.getenv("LLM_MODEL")))
    parser.add_argument("--base-url", default=os.getenv(prefix + "_BASE_URL", os.getenv("LLM_BASE_URL")))
    parser.add_argument("--chave-env", default=prefix + "_API_KEY",
                        help="Nome da variável com a chave; fallback: LLM_API_KEY.")
    parser.add_argument("--sem-chave", action="store_true", help="Servidor local sem autenticação.")
    parser.add_argument("--contexto-tokens", type=int, default=0, help="0 = descobrir na API; informe se o provedor não publicar o limite.")
    parser.add_argument("--uso-contexto", type=float, default=0.75, help="Teto entre 0.70 e 0.80, incluindo saída.")
    parser.add_argument("--max-saida-tokens", type=int, default=0, help="0 = derivar automaticamente do orçamento.")
    parser.add_argument("--parametro-tokens", choices=["max_tokens", "max_completion_tokens"],
                        default="max_completion_tokens")
    parser.add_argument("--sem-json-mode", action="store_true")
    parser.add_argument("--temperatura", type=float, default=None)
    parser.add_argument("--esforco-raciocinio", choices=["none", "low", "medium", "high"], default=None,
                        help="Enviado como reasoning_effort; omitido quando vazio.")
    parser.add_argument("--raciocinio-por-tarefa", default=None,
                        help="Exceções por tarefa, ex.: 'anotacao:padrao,resumo_fala:none'. 'padrao' omite o parâmetro.")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--tentativas", type=int, default=4)
    parser.add_argument("--max-chamadas", type=int, default=0,
                        help="Teto de requisições novas por execução; 0 = sem teto. Cache não conta.")
    parser.add_argument("--config", type=Path, default=BASE_DIR / ".config")
    parser.add_argument("--prompts", type=Path, default=BASE_DIR / "prompts.xml")
    parser.add_argument("--json-mode", dest="sem_json_mode", action="store_false", default=argparse.SUPPRESS)
    parser.add_argument("--autenticado", dest="sem_chave", action="store_false", default=argparse.SUPPRESS)
    # Somente localizar o arquivo nesta primeira passagem. O parse final aplica CLI sobre defaults.
    preliminary, _ = parser.parse_known_args()
    config = read_config(preliminary.config)
    common = dict(config.items("comum")) if config.has_section("comum") else {}
    role_values = dict(config.items(role)) if config.has_section(role) else {}
    process = dict(config.items("processamento")) if config.has_section("processamento") else {}
    connection_fields = {"modelo", "base_url", "token", "chave_env", "sem_chave", "prompts",
                         "contexto_tokens", "uso_contexto", "max_saida_tokens", "parametro_tokens", "sem_json_mode",
                         "temperatura", "esforco_raciocinio", "raciocinio_por_tarefa", "timeout", "tentativas", "max_chamadas"}
    # Aceitas por compatibilidade do .config anterior; não controlam nem dividem falas.
    process_fields = {"bloco_caracteres", "max_interrupcao_caracteres", "janela_turnos"}
    require(set(common) <= connection_fields and set(role_values) <= connection_fields
            and set(process) <= process_fields, "Opção desconhecida na configuração.")
    settings = {**common, **{k: v for k, v in role_values.items() if v.strip()}, **process}
    actions = {action.dest: action for action in parser._actions}
    defaults = {"_role": role, "_config_token": role_values.get("token") or common.get("token")}
    for name, value in settings.items():
        if name == "token" or not value.strip() or name not in actions:
            continue
        action = actions[name]
        try:
            if name in ("sem_chave", "sem_json_mode"):
                require(value.lower() in configparser.ConfigParser.BOOLEAN_STATES, "Booleano inválido.")
                converted = configparser.ConfigParser.BOOLEAN_STATES[value.lower()]
            else:
                converted = action.type(value) if action.type else value
            if action.choices:
                require(converted in action.choices, "Valor não permitido.")
        except (ValueError, TypeError):
            raise ValueError(f"Valor inválido na opção '{name}' da configuração.") from None
        if name == "prompts" and not converted.is_absolute():
            converted = preliminary.config.resolve().parent / converted
        defaults[name] = converted
    for dest, suffix in (("modelo", "MODEL"), ("base_url", "BASE_URL")):
        env = os.getenv(prefix + "_" + suffix) or os.getenv("LLM_" + suffix)
        if env:
            defaults[dest] = env
    parser.set_defaults(**defaults)


def interval_arguments(parser):
    parser.add_argument("--inicio", type=int, default=1, help="Primeira linha da fonte (inclusiva). Padrão: 1.")
    parser.add_argument("--fim", type=int, default=None, help="Última linha da fonte (inclusiva). Padrão: até o fim do arquivo.")


def check_interval(args):
    require(args.inicio >= 1 and (args.fim is None or args.fim >= args.inicio),
            "Use inicio >= 1 e fim >= inicio; omita fim para percorrer o restante.")
