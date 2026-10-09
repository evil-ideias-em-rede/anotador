"""Anotação de debates já preparados, com evidências não bloqueantes e revisão humana."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from collections import Counter
import hashlib
import itertools
import json
from pathlib import Path
import re
import unicodedata

from api import Client
from contexto import ContextLimitError
from formato import load_debate, save_debate
from progresso import progress, log
from suporte import (VERSION, EvidenceError, ModelOutputError, PromptCatalog, digest, model_arguments, require, save, text_field,
                     interval_arguments, check_interval, load)

TAXONOMY = [
    {"id": 1, "dimension": "Alinhamento Temático",
     "indicators": ["Focalizado", "Periférico", "Desalinhado"],
     "classification_object": "Avalia a pertinência do conteúdo da fala inteira em relação ao tema recebido."},
    {"id": 2, "dimension": "Postura do Orador",
     "indicators": ["Agressiva", "Emocional", "Confiante", "Técnica"],
     "classification_object": "Caracteriza os modos de expressão discursiva observáveis no texto da fala inteira, sem inferir personalidade ou tom de voz."},
    {"id": 3, "dimension": "Credibilidade e Validação",
     "indicators": ["Autoridade Própria", "Referência Externa", "Recursos Retóricos"],
     "classification_object": "Identifica os recursos discursivos mobilizados para sustentar afirmações, sem certificar sua veracidade."},
    {"id": 4, "dimension": "Posicionamento",
     "indicators": ["Favorável", "Contrário", "Neutro", "Ambíguo"],
     "classification_object": "Identifica concordância, discordância, neutralidade expressa ou ambiguidade da fala inteira em relação ao conteúdo do tema recebido."},
]

# O delimitador é procurado fora de parênteses: "PSDB - RS" não encerra um cabeçalho.
HEADER = re.compile(r"(?m)^[ \t]*(?:O SR\.|A SRA\.|O SENHOR\b|A SENHORA\b)[^\n]*")


def parse_turns(transcript):
    matches = list(HEADER.finditer(transcript))
    require(matches, "Nenhum cabeçalho de orador reconhecido; adapte o parser antes de anotar este registro.")
    turns = []
    for index, match in enumerate(matches):
        line = match.group()
        depth, cut = 0, None
        for position, char in enumerate(line):
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif char in "-–—" and depth == 0:
                cut = position + 1
                break
        require(cut is not None, f"Cabeçalho sem delimitador na posição {match.start()}.")
        header = line[:cut]
        speaker = re.sub(r"^\s*(?:O SR\.|A SRA\.|O SENHOR|A SENHORA)\s*", "", header[:-1]).strip()
        role = speaker.split("(", 1)[0].strip()
        if "PRESIDENTE" in role and "(" in speaker:
            # rsplit: o nome pode ter um honorífico abreviado com ponto antes dele ("Dr. Fulano. PT-SP");
            # dividir no primeiro ponto truncava o nome no próprio honorífico.
            speaker = speaker.split("(", 1)[1].split(")", 1)[0].rsplit(".", 1)[0].strip()
        else:
            speaker = speaker.split("(", 1)[0].strip()
        require(speaker, "Orador não identificado.")
        start = match.start() + cut
        end = matches[index + 1].start() if index + 1 < len(matches) else len(transcript)
        turns.append({"id": f"t{index + 1:05d}", "ordem": index + 1,
                      "participante": speaker, "papel_no_cabecalho": role,
                      "cabecalho_inicio": match.start(), "inicio": start, "fim": end,
                      "cabecalho": transcript[match.start():start]})
    return turns


def chunks(transcript, start, end, size):
    """Catálogo de âncoras literais; nunca usado para dividir a entrada de classificação."""
    require(size >= 200, "Bloco deve ter pelo menos 200 caracteres.")
    while start < end:
        stop = min(start + size, end)
        if stop < end:
            minimum = start + size // 2
            candidates = [transcript.rfind(separator, minimum, stop) + len(separator)
                          for separator in ("\n", ". ", "; ", " ")]
            useful = [candidate for candidate in candidates if candidate > minimum]
            if useful:
                stop = max(useful)
        yield {"inicio": start, "fim": stop, "texto": transcript[start:stop]}
        start = stop


def evidence_valid(evidence, transcript, allowed):
    require(isinstance(evidence, dict), "Evidência deve ser objeto.")
    if evidence.get("status") == "pendente_revisao_humana":
        require("inicio" not in evidence and "fim" not in evidence, "Evidência pendente não pode ter localização validada.")
        text_field(evidence.get("motivo"), 500)
        return
    start, end = evidence.get("inicio"), evidence.get("fim")
    quote = evidence.get("trecho")
    require(type(start) is int and type(end) is int and 0 <= start < end <= len(transcript),
            "Offsets de evidência inválidos.")
    text_field(quote, max(1, len(quote)) if isinstance(quote, str) else 1)
    require(transcript[start:end] == quote, "Citação não coincide exatamente com a transcrição.")
    require(any(a <= start and end <= b for a, b in allowed), "Evidência fora dos trechos disponíveis do titular.")


def resolve_quotes(value, transcript, allowed):
    """Modelos devolvem citações; offsets são calculados pelo código, não estimados pelo LLM."""
    if isinstance(value, dict):
        if value.get("status") == "pendente_revisao_humana":
            return
        if "trecho" in value:
            quote = text_field(value["trecho"], max(1, len(value["trecho"])) if isinstance(value["trecho"], str) else 1)
            positions = []
            for a, b in allowed:
                cursor = a
                while cursor < b:
                    found = transcript.find(quote, cursor, b)
                    if found < 0:
                        break
                    positions.append((found, found + len(quote)))
                    cursor = found + 1
            positions = sorted(set(positions))
            if not positions:
                # Aceita somente diferenças de espaços/quebras, nunca paráfrases ou similaridade.
                pattern = re.compile(r"\s+".join(re.escape(part) for part in re.split(r"\s+", quote.strip())))
                positions = sorted({(m.start(), m.end()) for a, b in allowed
                                    for m in pattern.finditer(transcript, a, b)})
                if positions:
                    value["trecho_recebido"] = quote
                    value["normalizacao"] = "somente_espacos"
                    value["trecho"] = transcript[positions[0][0]:positions[0][1]]
                    positions = [(a, b) for a, b in positions if transcript[a:b] == value["trecho"]]
            if not positions:
                raise EvidenceError("Citação não localizada no contexto fornecido.")
            # Ocorrências repetidas são registradas; a primeira é apenas uma âncora determinística.
            hint = (value.get("inicio"), value.get("fim"))
            value["inicio"], value["fim"] = hint if hint in positions else positions[0]
            value["ocorrencias"] = [{"inicio": a, "fim": b} for a, b in positions]
        for key, child in list(value.items()):
            if key != "ocorrencias":
                resolve_quotes(child, transcript, allowed)
    elif isinstance(value, list):
        for child in value:
            resolve_quotes(child, transcript, allowed)


def pending_evidence(raw, reason, indicator=None):
    result = {"status": "pendente_revisao_humana", "motivo": reason, "recebida": raw}
    if indicator is not None:
        result["indicador"] = indicator
    return result


def check_evidence(raw, transcript, allowed):
    if (isinstance(raw, dict) and raw.get("status") == "pendente_revisao_humana"
            and isinstance(raw.get("motivo"), str) and raw["motivo"].strip()
            and len(raw["motivo"]) <= 500 and "inicio" not in raw and "fim" not in raw):
        return raw
    evidence = copy.deepcopy(raw)
    try:
        require(isinstance(evidence, dict), "Evidência ausente ou em formato inválido.")
        require(isinstance(evidence.get("trecho"), str) and bool(evidence["trecho"].strip()),
                "Citação ausente, vazia ou não textual.")
        resolve_quotes(evidence, transcript, allowed)
        evidence_valid(evidence, transcript, allowed)
        evidence["status"] = "correspondencia_textual_verificada"
        return evidence
    except (ValueError, KeyError, TypeError):
        return pending_evidence(raw, "Citação ausente, inválida ou não localizada nas palavras do titular; conferir manualmente.",
                                raw.get("indicador") if isinstance(raw, dict) else None)


def pending_text(item):
    """Alguns modelos embrulham a pendência num objeto ({"descricao": ...}); o conteúdo é o mesmo."""
    if not isinstance(item, dict):
        return item
    parts = [v.strip() for v in item.values() if isinstance(v, str) and v.strip()]
    return ": ".join(parts) if parts else item


def ask_grounded(client, task, data, validate, transcript, allowed):
    """Falhas apenas de evidência viram pendências; nunca exigem outra chamada."""
    def nonblocking_validate(value):
        if task == "propostas":
            for proposal in value.get("propostas", []) if isinstance(value.get("propostas"), list) else []:
                if isinstance(proposal, dict):
                    proposal["evidencia"] = check_evidence(proposal.get("evidencia"), transcript, allowed)
        else:
            raw = value.get("evidencias")
            items = raw if isinstance(raw, list) else [raw]
            value["evidencias"] = [check_evidence(item, transcript, allowed) for item in items]
            if task == "anotacao" and isinstance(value.get("pendencias"), list):
                value["pendencias"] = [pending_text(item) for item in value["pendencias"]]
            if task == "anotacao" and isinstance(value.get("indicadores"), list):
                labels = value["indicadores"]
                for item in value["evidencias"]:
                    if item.get("indicador") not in labels and item.get("status") != "pendente_revisao_humana":
                        # Cópia: o item é esvaziado e reescrito abaixo; guardá-lo por referência criaria um ciclo.
                        replacement = pending_evidence(copy.deepcopy(item), "Evidência sem associação válida a um indicador; conferir manualmente.")
                        item.clear(); item.update(replacement)
                for label in labels:
                    if not any(e.get("indicador") == label for e in value["evidencias"]):
                        value["evidencias"].append(pending_evidence(None, "Indicador sem citação utilizável; conferir a fala inteira.", label))
            if task == "opiniao" and value.get("objeto_do_posicionamento") is not None and not value["evidencias"]:
                value["evidencias"].append(pending_evidence(None, "Objeto do posicionamento sem citação; conferir a fala inteira."))
        validate(value)
    return client.ask(task, data, nonblocking_validate)


def evidence_issues(document):
    issues = []
    def visit(value, path):
        if isinstance(value, dict):
            if value.get("status") == "pendente_revisao_humana":
                issues.append({"campo": path, "motivo": value["motivo"]})
                return
            for key, child in value.items():
                if key != "falhas_modelo":  # Contadas à parte, em revisao_humana.falhas_modelo.
                    visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
    for speech in document["falas"]:
        visit(speech.get("anotacao", {}), speech["id"])
    return issues


def full_speech(document, speech):
    """Os intervalos localizam a fonte; o modelo recebe todo o texto de uma vez."""
    turns = {t["id"]: t for t in document["turnos"]}
    return {"id": speech["id"], "participante": speech["participante"],
            "texto": speech["texto_titular"]}, [
        (turns[tid]["inicio"], turns[tid]["fim"]) for tid in speech["turnos_titular"]]


def boundary_window(transcript, turns):
    return [{"id": t["id"], "participante": t["participante"],
             "inicio": t["inicio"], "fim": t["fim"],
             "texto": transcript[t["inicio"]:t["fim"]], "contexto_parcial": False}
            for t in turns]


def group_speeches(document, client):
    turns, transcript = document["turnos"], document["transcricao"]
    speeches, decisions = [], []
    i = 0
    progress("Formação de falas — turnos", 0, len(turns))
    while i < len(turns):
        own = [turns[i]["id"]]
        interruptions = []
        current = i
        while current + 1 < len(turns):
            candidate = turns[current + 1]
            # Examine a próxima contribuição inteira. Não é necessário enviar todas as
            # falas de terceiros até uma eventual nova rodada do titular.
            window = boundary_window(transcript, turns[i:current + 2])

            def validate(value):
                require(set(value) == {"decisao", "justificativa", "mudanca_de_opiniao", "observacao_retomada"}, "Campos da decisão de fronteira inválidos.")
                require(value.get("decisao") in ("mesma_fala", "nova_fala", "incerto"), "Decisão inválida.")
                # Metadado de auditoria: um excesso pontual de tamanho não deve derrubar o debate
                # inteiro. A decisão em si (já validada acima) é o que forma as falas.
                require(isinstance(value.get("justificativa"), str) and value["justificativa"].strip(), "Justificativa ausente.")
                require(isinstance(value.get("observacao_retomada"), str) and value["observacao_retomada"].strip(), "Observação ausente.")
                if len(value["justificativa"]) > 600:
                    value["justificativa"] = value["justificativa"][:597] + "..."
                if len(value["observacao_retomada"]) > 600:
                    value["observacao_retomada"] = value["observacao_retomada"][:597] + "..."
                require(value.get("mudanca_de_opiniao") is None or type(value["mudanca_de_opiniao"]) is bool,
                        "Mudança deve ser booleano ou null.")

            if client is None:
                decision = {"decisao": "incerto", "justificativa": "Preparação sem modelo; limite de fala ainda não avaliado."}
            else:
                decision = client.ask("segmentacao", {"titular": turns[i]["participante"],
                    "turnos": window, "candidato_id": candidate["id"],
                    "turno_seguinte_contexto": boundary_window(transcript, turns[current + 2:current + 3])}, validate)
            decisions.append({"turno_anterior": turns[current]["id"], "retomada": candidate["id"],
                              "janela": window, **decision})
            document["decisoes_de_fronteira"] = decisions
            if client and decision["decisao"] == "incerto":
                raise ValueError(f"Fronteira inconclusiva entre {turns[current]['id']} e {candidate['id']}. Revisão necessária; a fala não será dividida automaticamente.")
            if decision["decisao"] != "mesma_fala":
                break
            if candidate["participante"] == turns[i]["participante"]:
                own.append(candidate["id"])
                for item in interruptions:
                    if item["retomada_turno"] is None:
                        item.update(retomada_turno=candidate["id"],
                                    mudanca_de_opiniao=decision["mudanca_de_opiniao"],
                                    observacao_retomada=decision["observacao_retomada"])
            else:
                interruptions.append({"turno_id": candidate["id"], "participante": candidate["participante"],
                    "texto": transcript[candidate["inicio"]:candidate["fim"]],
                    "inicio": candidate["inicio"], "fim": candidate["fim"],
                    "apos_turno": own[-1], "retomada_turno": None,
                    "mudanca_de_opiniao": None, "observacao_retomada": "Retomada ainda não observada.",
                    "alcance_da_avaliacao": "turnos completos da fala; sujeito a revisão"})
            current += 1
        by_id = {turn["id"]: turn for turn in turns}
        speeches.append({"id": f"f{len(speeches) + 1:05d}", "participante": turns[i]["participante"],
                         "ordem_inicio": turns[i]["ordem"], "turnos_titular": own,
                         "texto_titular": "\n\n".join(transcript[by_id[tid]["inicio"]:by_id[tid]["fim"]] for tid in own),
                         "interrupcoes": interruptions})
        i = current + 1
        progress("Formação de falas — turnos", i, len(turns))
    document["falas"] = speeches
    document["participantes"] = [{"nome": name, "falas": [s["id"] for s in speeches if s["participante"] == name]}
                                 for name in dict.fromkeys(s["participante"] for s in speeches)]
    document["decisoes_de_fronteira"] = decisions




def normalized_proposal(text):
    """Só normaliza caixa, Unicode e espaços; preserva negações, números e condições."""
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


# Os limites dos prompts orientam a concisão, mas modelos não contam caracteres com precisão.
# Rejeitar um excesso pequeno só gera novas chamadas pagas; o teto real pega respostas que desandam.
FOLGA_TAMANHO = 3


def model_text(value, limit, campo):
    return text_field(value, limit * FOLGA_TAMANHO, campo)


ANNOTATION_FIELDS = {"indicadores", "justificativa", "evidencias", "pendencias"}
OPINION_FIELDS = ({"resumo", "evidencias"}, {"resumo", "objeto_do_posicionamento", "evidencias"})


def validate_annotation(value, dimension, transcript, allowed):
    require(set(value) == ANNOTATION_FIELDS, "Campos da anotação inválidos.")
    labels = value.get("indicadores")
    require(isinstance(labels, list) and all(isinstance(x, str) for x in labels)
            and len(labels) == len(set(labels)) and set(labels) <= set(dimension["indicators"]), "Rótulos inválidos.")
    if dimension["id"] in (1, 4):
        require(len(labels) <= 1, "Dimensão admite no máximo um indicador.")
    model_text(value.get("justificativa"), 700, "justificativa")
    require(isinstance(value.get("evidencias"), list), "Evidências inválidas.")
    for item in value["evidencias"]:
        require(item.get("indicador") in labels or item.get("status") == "pendente_revisao_humana", "Evidência sem indicador correspondente.")
        evidence_valid(item, transcript, allowed)
    require(set(labels) <= {x.get("indicador") for x in value["evidencias"] if isinstance(x.get("indicador"), str)}, "Cada indicador precisa de evidência.")
    require(isinstance(value.get("pendencias"), list) and len(value["pendencias"]) <= 3, "Pendências inválidas.")
    for item in value["pendencias"]:
        model_text(item, 180, "pendencias")


def validate_opinion(value, transcript, allowed):
    require(set(value) in OPINION_FIELDS, "Campos de opinião inválidos.")
    model_text(value["resumo"], 1200, "resumo")
    target = value.get("objeto_do_posicionamento")
    require(target is None or isinstance(target, str), "Objeto deve ser texto ou null.")
    if target is not None:
        model_text(target, 500, "objeto_do_posicionamento")
    require(isinstance(value["evidencias"], list), "Evidências de opinião inválidas.")
    require(target is None or value["evidencias"], "Objeto do posicionamento sem evidência.")
    for evidence in value["evidencias"]:
        evidence_valid(evidence, transcript, allowed)


def validate_proposals(value, transcript, allowed):
    require(set(value) == {"propostas"} and isinstance(value["propostas"], list), "Lista de propostas inválida.")
    for proposal in value["propostas"]:
        require(set(proposal) == {"enunciado", "evidencia"}, "Campos de proposta inválidos.")
        model_text(proposal["enunciado"], 350, "enunciado")
        evidence_valid(proposal["evidencia"], transcript, allowed)


def model_failure(task, error, dimension=None):
    """Registro de uma tarefa que o modelo não conseguiu cumprir; aparece nas pendências de revisão."""
    record = {"status": "pendente_revisao_humana", "tarefa": task,
              "motivo": f"Falha do modelo nesta tarefa ({error}); preencher manualmente."[:500]}
    if dimension is not None:
        record["dimensao"] = dimension
    return record


def annotate_speech(document, speech, client, judge=False):
    fala, allowed = full_speech(document, speech)
    source = document["transcricao"]
    data = {"tema": document["tema"], "fala": fala}
    failures = []

    def grounded(task, payload, validator, fallback, dimension=None):
        # Uma saída inutilizável do modelo vira pendência desta tarefa, sem interromper o debate.
        # Rede, crédito e autenticação continuam interrompendo: seriam falhas em massa, não da fala.
        try:
            return ask_grounded(client, task, payload, validator, source, allowed)
        except ModelOutputError as error:
            failures.append(model_failure(task, error, dimension))
            log(f"Pendência por falha do modelo: {speech['id']} {dimension or task}.")
            return fallback

    # Os campos são conferidos antes das citações: uma citação num campo extra inventado pelo modelo
    # é erro de formato (nova tentativa), não falha de evidência que interrompe o debate.
    def opinion_validator(value):
        require(set(value) in OPINION_FIELDS, "Campos de opinião inválidos.")
        resolve_quotes(value, source, allowed)
        validate_opinion(value, source, allowed)

    opinion = grounded("opiniao", data, opinion_validator,
                       {"resumo": "Opinião não identificada por falha do modelo; ver pendências.", "evidencias": []})
    output = {"unidade": "fala_integral", "referencia_posicionamento": document["tema"], "opiniao": opinion, "dimensoes": {}}
    for dimension in TAXONOMY:
        def validate(value):
            require(set(value) == ANNOTATION_FIELDS, "Campos da anotação inválidos.")
            resolve_quotes(value, source, allowed)
            validate_annotation(value, dimension, source, allowed)
        # Cada dimensão recebe a MESMA fala inteira; nunca uma parte ou um resumo substituto.
        output["dimensoes"][dimension["dimension"]] = grounded(
            "anotacao", {**data, "dimensao": dimension}, validate,
            {"indicadores": [], "justificativa": "Não classificada por falha do modelo; ver pendências.",
             "evidencias": [], "pendencias": []}, dimension["dimension"])

    def proposals_validator(value):
        require(set(value) == {"propostas"} and isinstance(value["propostas"], list)
                and all(isinstance(p, dict) and set(p) == {"enunciado", "evidencia"} for p in value["propostas"]),
                "Campos de proposta inválidos.")
        resolve_quotes(value, source, allowed)
        validate_proposals(value, source, allowed)
    output["propostas"] = grounded("propostas", data, proposals_validator, {"propostas": []})["propostas"]
    summarize_speech(document, speech, output, client)
    if failures:
        # Sem esta lista, "sem rótulo" ou "sem propostas" seriam indistinguíveis de uma falha.
        output["falhas_modelo"] = failures
    return output


def summarize_speech(document, speech, annotation, client):
    """Somente esta fala: nunca reúne as exposições de um participante."""
    def validate(value):
        require(set(value) == {"resumo"}, "Campo do resumo da fala inválido.")
        model_text(value["resumo"], 1800, "resumo")
    try:
        result = client.ask("resumo_fala", {"tema": document["tema"],
            "fala": full_speech(document, speech)[0],
            "opiniao": annotation["opiniao"]["resumo"],
            "taxonomia": {k: {"indicadores": v["indicadores"], "justificativa": v["justificativa"]}
                          for k, v in annotation["dimensoes"].items()},
            "propostas": [p["enunciado"] for p in annotation["propostas"]]}, validate)
        annotation["resumo"] = result["resumo"]
        annotation["status_resumo"] = "concluido"
        annotation.pop("motivo_resumo_pendente", None)
    except ContextLimitError:
        annotation["resumo"] = None
        annotation["status_resumo"] = "pendente_contexto"
        annotation["motivo_resumo_pendente"] = "Contexto insuficiente para o resumo desta fala inteira; revisar manualmente."
        log(f"Resumo pendente: {speech['id']}. Taxonomia e texto integral preservados.")
    except ModelOutputError as error:
        annotation["resumo"] = None
        annotation["status_resumo"] = "pendente_falha_modelo"
        annotation["motivo_resumo_pendente"] = f"Falha do modelo no resumo ({error}); revisar manualmente."[:600]
        log(f"Resumo pendente por falha do modelo: {speech['id']}.")


RESUMO_PENDENTE = ("pendente_contexto", "pendente_falha_modelo")


def summary_issues(document):
    return [{"fala_id": s["id"], "participante": s["participante"],
             "motivo": s["anotacao"]["motivo_resumo_pendente"]}
            for s in document["falas"] if s.get("anotacao", {}).get("status_resumo") in RESUMO_PENDENTE]


def prepare(record, source, line, size=None):
    require(isinstance(record, dict) and "id" in record, "Registro LDS sem id.")
    transcript = record.get("transcricao")
    require(isinstance(transcript, str) and bool(transcript.strip()), "Registro sem transcricao válida.")
    metadata = record.get("metadados")
    require(isinstance(metadata, dict), "Registro LDS sem objeto metadados.")
    theme = metadata.get("assunto", "")
    text_field(theme, 1500)
    turns = parse_turns(transcript)
    return {"versao": VERSION, "debate_id": record["id"], "tema": theme,
            "origem_tema": "metadados.assunto; título contextual, não adesão dos participantes",
            "fonte": {"arquivo": str(Path(source).resolve()), "linha": line, "campo": "transcricao",
                      "sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest()},
            "transcricao": transcript, "preambulo": {"inicio": 0, "fim": turns[0]["cabecalho_inicio"]},
            "taxonomia_original": TAXONOMY, "turnos": turns,
            "configuracao": {"unidade_analise": "fala_integral", "divisao_interna": False}, "status": "preparado_sem_anotacao"}


def structural_errors(document, annotated=True):
    errors = []
    try:
        if annotated:
            require(document.get("versao") == VERSION, "Formato antigo: execute novamente o anotador para gerar falas integrais.")
        transcript = document["transcricao"]
        require(hashlib.sha256(transcript.encode("utf-8")).hexdigest() == document["fonte"]["sha256"], "Hash da fonte divergente.")
        require(document["taxonomia_original"] == TAXONOMY, "Taxonomia alterada.")
        turns = document["turnos"]
        require(turns and len({t["id"] for t in turns}) == len(turns), "IDs de turnos duplicados/ausentes.")
        require(document["preambulo"] == {"inicio": 0, "fim": turns[0]["cabecalho_inicio"]}, "Preâmbulo inválido.")
        previous = turns[0]["cabecalho_inicio"]
        for turn in turns:
            require(turn["cabecalho_inicio"] == previous and previous <= turn["inicio"] <= turn["fim"] <= len(transcript), "Cobertura de turnos inválida.")
            require(transcript[previous:turn["inicio"]] == turn["cabecalho"], "Cabeçalho divergente.")
            previous = turn["fim"]
        require(previous == len(transcript), "Final da transcrição descoberto.")
        ids = [tid for speech in document["falas"] for tid in speech["turnos_titular"]]
        ids += [item["turno_id"] for speech in document["falas"] for item in speech["interrupcoes"]]
        require(Counter(ids) == Counter(t["id"] for t in turns), "Turnos perdidos, duplicados ou inventados na formação de falas.")
        by_id = {t["id"]: t for t in turns}
        require(len({s["id"] for s in document["falas"]}) == len(document["falas"]), "IDs de falas duplicados.")
        if annotated:
            require(document.get("versao") == VERSION, "Formato antigo: execute novamente o anotador para gerar falas integrais.")
            require(not any(d["decisao"] == "incerto" for d in document["decisoes_de_fronteira"]), "Fronteiras inconclusivas não podem ser classificadas.")
            expected_names = list(dict.fromkeys(s["participante"] for s in document["falas"]))
            require([p["nome"] for p in document["participantes"]] == expected_names, "Índice de participantes divergente.")
            for participant in document["participantes"]:
                require(participant["falas"] == [s["id"] for s in document["falas"] if s["participante"] == participant["nome"]], "Falas do participante divergentes.")
        prior_order = 0
        for speech in document["falas"]:
            orders = sorted(by_id[tid]["ordem"] for tid in speech["turnos_titular"] + [x["turno_id"] for x in speech["interrupcoes"]])
            require(orders == list(range(prior_order + 1, prior_order + len(orders) + 1)), "Falas não contíguas ou fora de ordem.")
            prior_order = orders[-1]
            own = [by_id[tid] for tid in speech["turnos_titular"]]
            require(own and all(t["participante"] == speech["participante"] for t in own), "Titular incorreto.")
            require([t["ordem"] for t in own] == sorted(t["ordem"] for t in own), "Turnos fora de ordem.")
            require(speech["texto_titular"] == "\n\n".join(transcript[t["inicio"]:t["fim"]] for t in own), "Texto titular divergente.")
            for interruption in speech["interrupcoes"]:
                require(interruption["participante"] != speech["participante"], "Titular não pode ser seu próprio interruptor.")
                require(interruption["apos_turno"] in speech["turnos_titular"], "Interrupção sem vínculo com o titular.")
                turn = by_id[interruption["turno_id"]]
                require(interruption["participante"] == turn["participante"] and interruption["texto"] == transcript[turn["inicio"]:turn["fim"]], "Interrupção divergente da fonte.")
            if not annotated:
                continue
            annotation = speech["anotacao"]
            if document.get("resumo_por_fala"):
                require("resumo" in annotation, "Fala sem resumo ou pendência explícita.")
            if annotation.get("status_resumo") in RESUMO_PENDENTE:
                require(annotation.get("resumo") is None, "Resumo pendente não pode ser apresentado como concluído.")
                text_field(annotation.get("motivo_resumo_pendente"), 600)
            elif "resumo" in annotation:
                model_text(annotation["resumo"], 1800, "resumo")
            allowed = [(t["inicio"], t["fim"]) for t in own]
            require(annotation["unidade"] == "fala_integral", "A anotação deve usar a fala integral.")
            require(set(annotation["dimensoes"]) == {d["dimension"] for d in TAXONOMY}, "Dimensões ausentes/extras.")
            require(not {"blocos", "estados_parciais", "posicionamentos"} & annotation.keys(), "Formato fragmentado não permitido.")
            validate_opinion(annotation["opiniao"], transcript, allowed)
            validate_proposals({"propostas": annotation["propostas"]}, transcript, allowed)
            for dimension in TAXONOMY:
                validate_annotation(annotation["dimensoes"][dimension["dimension"]], dimension, transcript, allowed)
            if "referencia_posicionamento" in annotation:
                require(annotation["referencia_posicionamento"] == document["tema"], "Referência do posicionamento deve ser o tema original.")
    except (ValueError, KeyError, TypeError, IndexError) as error:
        errors.append(str(error))
    return errors


def prepared_errors(document):
    errors = structural_errors(document, annotated=False)
    if document.get("status") != "preparado_para_anotacao":
        errors.append("Arquivo não é um debate preparado. Execute preparador.py primeiro.")
    if any(d.get("decisao") == "incerto" for d in document.get("decisoes_de_fronteira", [])):
        errors.append("Há fronteiras de fala inconclusivas; finalize a preparação.")
    expected = [{"nome": name, "falas": [s["id"] for s in document.get("falas", []) if s["participante"] == name]}
                for name in dict.fromkeys(s["participante"] for s in document.get("falas", []))]
    if document.get("participantes") != expected:
        errors.append("Índice de participantes não corresponde às falas preparadas.")
    if not document.get("falas"):
        errors.append("Preparação sem falas.")
    return errors


def main():
    parser = argparse.ArgumentParser(description="Anota debates já preparados: um arquivo JSON ou uma pasta.")
    parser.add_argument("--entrada", type=Path, default=Path(__file__).parent / "preparados")
    parser.add_argument("--saida", type=Path, default=Path(__file__).parent / "resultados")
    parser.add_argument("--paralelo", type=int, default=1,
                        help="Falas anotadas simultaneamente em cada debate. Padrão: 1 (sequencial).")
    model_arguments(parser, "anotador")
    args = parser.parse_args()
    require(args.paralelo >= 1, "--paralelo deve ser >= 1.")
    require(args.entrada.exists(), "Entrada não encontrada. Execute preparador.py primeiro.")
    paths = [args.entrada] if args.entrada.is_file() else sorted(args.entrada.glob("*.json"))
    prompts = PromptCatalog(args.prompts)
    settings = {key: getattr(args, key, None) for key in
                ("modelo", "base_url", "temperatura", "contexto_tokens", "uso_contexto", "max_saida_tokens", "parametro_tokens", "sem_json_mode")}
    # Só entra na identidade quando usado, preservando as execuções anteriores sem esse parâmetro.
    for key in ("esforco_raciocinio", "raciocinio_por_tarefa"):
        if getattr(args, key, None):
            settings[key] = getattr(args, key)
    client, processed, reused = None, 0, 0
    failures = []
    for path in paths:
        try:
            document = load_debate(path)
            if not args.entrada.is_file() and document.get("status") != "preparado_para_anotacao":
                log(f"Ignorado (não é debate preparado): {path.name}")
                continue
            errors = prepared_errors(document)
            require(not errors, "; ".join(errors))
            run = digest({"preparado": document, "modelo": settings, "prompts": prompts.sha256, "versao": VERSION, "formato": "por_participante_resumo_por_fala", "prompt_por_dimensao": True})[:12]
            destination = args.saida / f"{path.stem}_{run}_anotado.json"
            require(destination.resolve() != path.resolve(), "A saída não pode sobrescrever a preparação.")
            if destination.exists():
                previous = load_debate(destination)
                require(previous.get("status") == "anotado_aguardando_revisao_humana" and not structural_errors(previous),
                        "Resultado existente inválido; escolha outra pasta de saída para preservá-lo.")
                log(f"Já anotado, reutilizado: {destination.name}"); reused += 1
                continue
            if client is None:
                client = Client(args, args.saida / "cache_anotador", prompts=prompts); client.start()
            document["versao"] = VERSION
            document["arquivo_preparado"] = str(path.resolve())
            document["status"] = "em_anotacao"
            document["resumo_por_fala"] = True
            checkpoint = args.saida / "andamento" / destination.name
            log(f"Anotando {path.name}: {len(document['falas'])} falas integrais (preparação reutilizada).")
            progress("Anotação — falas", 0, len(document["falas"]))
            # As tarefas só leem o documento; cada resultado é gravado aqui, na thread principal.
            pool = ThreadPoolExecutor(max_workers=args.paralelo)
            try:
                futures = {pool.submit(annotate_speech, document, speech, client): speech for speech in document["falas"]}
                for number, future in enumerate(as_completed(futures), 1):
                    futures[future]["anotacao"] = future.result()
                    save(checkpoint, document)
                    progress("Anotação — falas", number, len(document["falas"]))
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
            document["status"] = "anotado_aguardando_revisao_humana"
            document["modelo_anotador"] = client.identity()
            document["recibos_cache"] = sorted(set(client.receipts))
            model_failures = [{"fala_id": sp["id"], **{k: v for k, v in f.items() if k != "status"}}
                              for sp in document["falas"] for f in sp["anotacao"].get("falhas_modelo", [])]
            document["revisao_humana"] = {"necessaria": True, "pendencias_evidencias": evidence_issues(document),
                "falhas_modelo": model_failures,
                "resumos_pendentes": summary_issues(document),
                "observacao": "Correspondência textual não comprova adequação semântica. Nenhum julgador automático foi executado."}
            errors = structural_errors(document)
            require(not errors, "; ".join(errors))
            save_debate(destination, document); processed += 1
            log(f"Resumos por fala: {len(document['falas']) - len(summary_issues(document))} concluídos, {len(summary_issues(document))} pendentes por contexto.")
            log(f"Salvo: {destination} | {len(document['revisao_humana']['pendencias_evidencias'])} pendência(s) de evidência, "
                f"{len(model_failures)} tarefa(s) pendente(s) por falha do modelo.")
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
            failures.append({"arquivo": str(path), "erro": str(error)})
            log(f"Interrompida: {error}")
            break
    save(args.saida / "ultima_execucao.json", {"concluidos": processed, "reutilizados": reused, "falhas": failures})
    log(f"Anotação encerrada: {processed} concluído(s), {reused} reutilizado(s), {len(failures)} falha(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
