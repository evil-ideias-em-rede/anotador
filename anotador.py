"""Anotação de debates já preparados, com evidências não bloqueantes e revisão humana."""
from __future__ import annotations

import argparse
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
from suporte import (VERSION, EvidenceError, PromptCatalog, digest, model_arguments, require, save, text_field,
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
            speaker = speaker.split("(", 1)[1].split(".", 1)[0].split(")", 1)[0].strip()
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
            if task == "anotacao" and isinstance(value.get("indicadores"), list):
                labels = value["indicadores"]
                for item in value["evidencias"]:
                    if item.get("indicador") not in labels and item.get("status") != "pendente_revisao_humana":
                        replacement = pending_evidence(item, "Evidência sem associação válida a um indicador; conferir manualmente.")
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
                text_field(value.get("justificativa"), 600)
                text_field(value.get("observacao_retomada"), 600)
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


def validate_annotation(value, dimension, transcript, allowed):
    require(set(value) == {"indicadores", "justificativa", "evidencias", "pendencias"}, "Campos da anotação inválidos.")
    labels = value.get("indicadores")
    require(isinstance(labels, list) and all(isinstance(x, str) for x in labels)
            and len(labels) == len(set(labels)) and set(labels) <= set(dimension["indicators"]), "Rótulos inválidos.")
    if dimension["id"] in (1, 4):
        require(len(labels) <= 1, "Dimensão admite no máximo um indicador.")
    text_field(value.get("justificativa"), 700)
    require(isinstance(value.get("evidencias"), list), "Evidências inválidas.")
    for item in value["evidencias"]:
        require(item.get("indicador") in labels or item.get("status") == "pendente_revisao_humana", "Evidência sem indicador correspondente.")
        evidence_valid(item, transcript, allowed)
    require(set(labels) <= {x.get("indicador") for x in value["evidencias"] if isinstance(x.get("indicador"), str)}, "Cada indicador precisa de evidência.")
    require(isinstance(value.get("pendencias"), list) and len(value["pendencias"]) <= 3, "Pendências inválidas.")
    for item in value["pendencias"]:
        text_field(item, 180)


def validate_opinion(value, transcript, allowed):
    require(set(value) in ({"resumo", "evidencias"}, {"resumo", "objeto_do_posicionamento", "evidencias"}), "Campos de opinião inválidos.")
    text_field(value["resumo"], 1200)
    target = value.get("objeto_do_posicionamento")
    require(target is None or isinstance(target, str), "Objeto deve ser texto ou null.")
    if target is not None:
        text_field(target, 500)
    require(isinstance(value["evidencias"], list), "Evidências de opinião inválidas.")
    require(target is None or value["evidencias"], "Objeto do posicionamento sem evidência.")
    for evidence in value["evidencias"]:
        evidence_valid(evidence, transcript, allowed)


def validate_proposals(value, transcript, allowed):
    require(set(value) == {"propostas"} and isinstance(value["propostas"], list), "Lista de propostas inválida.")
    for proposal in value["propostas"]:
        require(set(proposal) == {"enunciado", "evidencia"}, "Campos de proposta inválidos.")
        text_field(proposal["enunciado"], 350)
        evidence_valid(proposal["evidencia"], transcript, allowed)


def annotate_speech(document, speech, client, judge=False):
    fala, allowed = full_speech(document, speech)
    source = document["transcricao"]
    data = {"tema": document["tema"], "fala": fala}

    def opinion_validator(value):
        resolve_quotes(value, source, allowed)
        validate_opinion(value, source, allowed)

    opinion = ask_grounded(client, "opiniao", data, opinion_validator, source, allowed)
    output = {"unidade": "fala_integral", "referencia_posicionamento": document["tema"], "opiniao": opinion, "dimensoes": {}}
    for dimension in TAXONOMY:
        def validate(value):
            resolve_quotes(value, source, allowed)
            validate_annotation(value, dimension, source, allowed)
        # Cada dimensão recebe a MESMA fala inteira; nunca uma parte ou um resumo substituto.
        output["dimensoes"][dimension["dimension"]] = ask_grounded(
            client, "anotacao", {**data, "dimensao": dimension},
            validate, source, allowed)

    def proposals_validator(value):
        resolve_quotes(value, source, allowed)
        validate_proposals(value, source, allowed)
    output["propostas"] = ask_grounded(client, "propostas", data, proposals_validator, source, allowed)["propostas"]
    summarize_speech(document, speech, output, client)
    return output


def summarize_speech(document, speech, annotation, client):
    """Somente esta fala: nunca reúne as exposições de um participante."""
    def validate(value):
        require(set(value) == {"resumo"}, "Campo do resumo da fala inválido.")
        text_field(value["resumo"], 1800)
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


def summary_issues(document):
    return [{"fala_id": s["id"], "participante": s["participante"],
             "motivo": s["anotacao"]["motivo_resumo_pendente"]}
            for s in document["falas"] if s.get("anotacao", {}).get("status_resumo") == "pendente_contexto"]


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
            if annotation.get("status_resumo") == "pendente_contexto":
                require(annotation.get("resumo") is None, "Resumo pendente não pode ser apresentado como concluído.")
                text_field(annotation.get("motivo_resumo_pendente"), 600)
            elif "resumo" in annotation:
                text_field(annotation["resumo"], 1800)
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
    model_arguments(parser, "anotador")
    args = parser.parse_args()
    require(args.entrada.exists(), "Entrada não encontrada. Execute preparador.py primeiro.")
    paths = [args.entrada] if args.entrada.is_file() else sorted(args.entrada.glob("*.json"))
    prompts = PromptCatalog(args.prompts)
    settings = {key: getattr(args, key, None) for key in
                ("modelo", "base_url", "temperatura", "contexto_tokens", "uso_contexto", "max_saida_tokens", "parametro_tokens", "sem_json_mode")}
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
            for number, speech in enumerate(document["falas"], 1):
                speech["anotacao"] = annotate_speech(document, speech, client)
                save(checkpoint, document)
                progress("Anotação — falas", number, len(document["falas"]))
            document["status"] = "anotado_aguardando_revisao_humana"
            document["modelo_anotador"] = client.identity()
            document["recibos_cache"] = sorted(set(client.receipts))
            document["revisao_humana"] = {"necessaria": True, "pendencias_evidencias": evidence_issues(document),
                "resumos_pendentes": summary_issues(document),
                "observacao": "Correspondência textual não comprova adequação semântica. Nenhum julgador automático foi executado."}
            errors = structural_errors(document)
            require(not errors, "; ".join(errors))
            save_debate(destination, document); processed += 1
            log(f"Resumos por fala: {len(document['falas']) - len(summary_issues(document))} concluídos, {len(summary_issues(document))} pendentes por contexto.")
            log(f"Salvo: {destination} | {len(document['revisao_humana']['pendencias_evidencias'])} pendência(s) de evidência.")
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
