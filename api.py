"""Comunicação com o provedor: descoberta, teste inicial, chamadas e cache."""
from __future__ import annotations
import argparse
import copy
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from suporte import (VERSION, CACHE_VERSION, EvidenceError, BASE_DIR, PromptCatalog, read_config, dumps, digest, save,
                     load, require, text_field, model_arguments)
from contexto import ContextBudget, ContextLimitError, model_limits
from progresso import Waiting, log, task_label

class Client:
    def __init__(self, args, cache, prompts=None):
        require(bool(args.modelo and args.base_url), "Informe --modelo e --base-url (ou variáveis de ambiente).")
        require(args.contexto_tokens >= 0 and 0.70 <= args.uso_contexto <= 0.80, "Contexto inválido; use 70% a 80%.")
        require(args.max_saida_tokens >= 0 and args.tentativas > 0 and args.timeout > 0,
                "Saída, tentativas e timeout devem ser positivos.")
        require(args.max_chamadas >= 0, "--max-chamadas deve ser >= 0.")
        parsed = urllib.parse.urlsplit(args.base_url)
        require(parsed.scheme in ("https", "http") and parsed.hostname and not parsed.query
                and not parsed.fragment and not parsed.username, "Base URL inválida; não inclua credenciais.")
        require(parsed.scheme == "https" or parsed.hostname in ("localhost", "127.0.0.1", "::1"),
                "Use HTTPS para provedores remotos.")
        self.args = args
        self.key = os.getenv(args.chave_env) or os.getenv("LLM_API_KEY") or getattr(args, "_config_token", None)
        require(args.sem_chave or self.key, f"Preencha token no .config ou defina {args.chave_env} / LLM_API_KEY.")
        self.role = getattr(args, "_role", "anotador")
        self.prompts = prompts or PromptCatalog(args.prompts)
        self.url = args.base_url.rstrip("/")
        if not self.url.endswith("/chat/completions"):
            self.url += "/chat/completions"
        self.cache = Path(cache)
        self.calls = 0
        self.hits = 0
        self.receipts = []
        self.budget = None

    def start(self):
        """Consulta metadados e faz uma geração de teste real antes de processar debates."""
        self._check_call_limit()
        request = self._request(self.url.removesuffix("/chat/completions") + "/models")
        metadata = {}
        try:
            self.calls += 1
            with Waiting("Consulta dos limites do modelo"):
                with urllib.request.urlopen(request, timeout=self.args.timeout) as response:
                    metadata = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise RuntimeError(f"Autenticação/permissão recusada na consulta inicial (HTTP {error.code}).") from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass  # Limite configurado é alternativa explícita, nunca inferência por tentativa.
        limits = model_limits(metadata, self.args.modelo)
        choices = [n for n in (self.args.contexto_tokens, limits["context"]) if n > 0]
        context = min(choices) if choices else 0
        source = "API e configuração (menor valor)" if all((self.args.contexto_tokens, limits["context"])) else ("API" if limits["context"] else "configuração")
        self.budget = ContextBudget(context, self.args.uso_contexto, self.args.max_saida_tokens, limits["output"], source)
        # Sem cache: verifica a credencial, o modelo e os parâmetros da execução atual.
        self.ask("teste_conexao", {}, lambda x: require(set(x) == {"ok"} and x["ok"] is True, "Esperado JSON ok=true."), cache=False)
        save(self.cache.parent / "verificacao_api.json", {"modelo": self.identity(), "status": "conexao_validada"})
        print(f"API validada: janela {context}; teto {self.budget.total}; reserva de saída {self.budget.output} tokens.", flush=True)

    def _check_call_limit(self):
        if self.args.max_chamadas and self.calls >= self.args.max_chamadas:
            raise RuntimeError("Teto de chamadas atingido. Retome com o mesmo comando e um teto suficiente para a verificação inicial.")

    def _request(self, url, body=None):
        request = urllib.request.Request(url, data=dumps(body).encode("utf-8") if body is not None else None,
                                         headers={"Content-Type": "application/json"})
        if self.key and not self.args.sem_chave:
            request.add_header("Authorization", "Bearer " + self.key)
        return request

    def identity(self):
        return {"modelo": self.args.modelo, "endpoint": self.url, "versao_codigo": VERSION,
                "prompts_sha256": self.prompts.sha256,
                "regras_anotacao": "somente_dimensao_solicitada",
                "parametros": {"temperatura": self.args.temperatura,
                               "parametro_tokens": self.args.parametro_tokens,
                               "max_saida_tokens": self.budget.output if self.budget else None,
                               "json_mode": not self.args.sem_json_mode,
                               "contexto": self.budget.report() if self.budget else None}}

    def _body(self, task, data):
        dimension_id = data.get("dimensao", {}).get("id") if task in ("anotacao", "anotacao_por_ids") else None
        system = self.prompts.render(task, self.role, dimension_id=dimension_id)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": dumps(data)}]
        body = {"model": self.args.modelo, "messages": messages,
                self.args.parametro_tokens: self.budget.output}
        if not self.args.sem_json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.args.temperatura is not None:
            body["temperature"] = self.args.temperatura
        return system, messages, body

    def has_cached(self, task, data):
        _, _, body = self._body(task, data)
        cache_id = digest({"endpoint": self.url, "body": body, "version": CACHE_VERSION})
        return (self.cache / (cache_id + ".json")).is_file()

    def ask(self, task, data, validate, cache=True):
        require(self.budget is not None, "Execute client.start() antes de chamar o modelo.")
        system, messages, body = self._body(task, data)
        label = task_label(task, data)
        try:
            self.budget.check(messages)
        except ValueError as error:
            subject = data.get("fala", {}).get("id") or data.get("candidato_id") or data.get("participante") or ""
            raise type(error)(f"{task} {subject}: {error}") from None
        cache_id = digest({"endpoint": self.url, "body": body, "version": CACHE_VERSION})
        path = self.cache / (cache_id + ".json")
        if cache and path.exists():
            stored = load(path)
            validate(stored["resposta"])
            self.hits += 1
            log(f"  {label}: reutilizado do cache, validado (acertos: {self.hits}).")
            self.receipts.append(cache_id)
            return stored["resposta"]
        last_error = ""
        feedback = None
        for attempt in range(self.args.tentativas):
            self._check_call_limit()
            outgoing = copy.deepcopy(body)
            if feedback:
                instructions = ET.fromstring(system)
                ET.SubElement(instructions, "correcao_formato").text = (
                    "A tentativa anterior não passou na validação: " + feedback +
                    " Refaça o JSON seguindo o contrato, sem repetir o erro; não invente citações.")
                corrected = ET.tostring(instructions, encoding="unicode")
                candidate_messages = [{"role": "system", "content": corrected}, messages[1]]
                if self.budget.estimate(candidate_messages) + self.budget.output <= self.budget.total:
                    outgoing["messages"] = candidate_messages
            delay = min(30, 2 ** attempt + random.random())
            request = self._request(self.url, outgoing)
            raw_result = None
            try:
                self.calls += 1
                with Waiting(f"{label} | chamada {self.calls} | tentativa {attempt + 1}/{self.args.tentativas}"):
                    with urllib.request.urlopen(request, timeout=self.args.timeout) as response:
                        envelope = json.load(response)
                usage = envelope.get("usage") or {}
                actual = usage.get("prompt_tokens")
                if type(actual) is int and actual + self.budget.output > self.budget.total:
                    raise RuntimeError("A contagem informada pelo provedor excedeu o orçamento estimado. Execução interrompida; é necessário adequar a contagem ao tokenizer desse provedor.")
                choice = envelope["choices"][0]
                if choice.get("finish_reason") != "stop" or choice.get("message", {}).get("refusal"):
                    raise RuntimeError("Resposta truncada/recusada. Não repetida automaticamente. Confira --max-saida-tokens; falas não serão divididas para contornar o limite.")
                result = json.loads(choice["message"]["content"])
                raw_result = copy.deepcopy(result)
                require(isinstance(result, dict), "A resposta deve ser um objeto JSON.")
                validate(result)
                save(path, {"resposta": result, "uso": envelope.get("usage"),
                            "modelo_retornado": envelope.get("model"),
                            "system_fingerprint": envelope.get("system_fingerprint"),
                            "identidade": self.identity(), "request_sha256": cache_id,
                            "tarefa": task, "prompt_xml": outgoing["messages"][0]["content"],
                            "tentativas_nesta_chamada": attempt + 1, "entrada": data, "parametros": {
                                k: v for k, v in body.items() if k != "messages"}})
                self.receipts.append(cache_id)
                log(f"  {label}: resposta validada e salva no cache.")
                return result
            except EvidenceError as error:
                # Repetir o mesmo pedido de cópia não resolve necessariamente paráfrases.
                # Guarda o diagnóstico fora do cache válido e devolve à pipeline para recuperação.
                save(self.cache / "falhas" / f"{cache_id}_{attempt + 1}.json", {
                    "tarefa": task, "entrada": data, "resposta_rejeitada": raw_result,
                    "erro": str(error), "prompt_xml": outgoing["messages"][0]["content"],
                    "request_sha256": cache_id, "identidade": self.identity()})
                raise
            except urllib.error.HTTPError as error:
                # Não exponha corpo da resposta: alguns provedores refletem cabeçalhos/chaves.
                last_error = f"HTTP {error.code}"
                try:
                    delay = min(30, max(0, float(error.headers.get("Retry-After", delay))))
                except (ValueError, TypeError, AttributeError):
                    pass
                if error.code not in (408, 429, 500, 502, 503, 504):
                    raise RuntimeError(last_error + ": confira endpoint, modelo e parâmetros; nenhuma entrada foi cortada.") from None
            except (ValueError, KeyError, IndexError, TypeError) as error:
                last_error = f"Resposta inválida: {error}"
                feedback = str(error)[:200]
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = type(error).__name__
            if attempt + 1 < self.args.tentativas:
                log(f"  {label}: tentativa sem sucesso; nova tentativa em {delay:.1f}s.")
                with Waiting("Intervalo antes da nova tentativa"):
                    time.sleep(delay)
        raise RuntimeError(f"Falha após {self.args.tentativas} tentativas: {last_error}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Testa o cliente configurável com uma chamada pequena à API.")
    parser.add_argument("--cache", type=Path, default=Path(__file__).parent / "cache_teste")
    try:
        model_arguments(parser, "anotador")
        args = parser.parse_args()
        client = Client(args, args.cache)
        client.start()
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, str(error) + "\n")
