"""Validação estrutural e segunda anotação independente. Não altera os resultados do anotador."""
from __future__ import annotations

import argparse
from pathlib import Path

from api import Client
from formato import load_debate
from progresso import progress, log
from suporte import (PromptCatalog, digest, load, model_arguments, require, save, text_field,
                     interval_arguments, check_interval)
from anotador import (TAXONOMY, annotate_speech, group_speeches,
                      normalized_proposal, structural_errors)


def proposal_comparison(original, independent, client):
    matches, missing = [], []
    for candidate in independent:
        equivalents = []
        for proposal in original:
            if normalized_proposal(proposal["enunciado"]) == normalized_proposal(candidate["enunciado"]):
                equivalents.append(proposal["id"])
                continue
            result = client.ask("equivalencia",
                                {"a": proposal["enunciado"], "b": candidate["enunciado"]},
                                lambda x: (require(type(x.get("equivalentes")) is bool, "Booleano obrigatório."),
                                           text_field(x.get("justificativa"), 400)))
            if result["equivalentes"]:
                equivalents.append(proposal["id"])
        if equivalents:
            matches.append({"proposta_independente": candidate["id"], "propostas_originais": equivalents})
        else:
            missing.append(candidate)
    matched_ids = {pid for match in matches for pid in match["propostas_originais"]}
    return {"correspondencias": matches, "possiveis_omissoes": missing,
            "originais_nao_reencontradas": [p for p in original if p["id"] not in matched_ids]}


def membership(document):
    mapping = {}
    for speech in document["falas"]:
        group = tuple(speech["turnos_titular"])
        for tid in group:
            mapping[tid] = {"titular": group, "papel": "titular"}
        for interruption in speech["interrupcoes"]:
            mapping[interruption["turno_id"]] = {"titular": group, "papel": "interrupcao"}
    return mapping


def compare_annotations(original, independent):
    comparisons = []
    for key, old in original["dimensoes"].items():
        new = independent["dimensoes"][key]
        left, right = set(old["indicadores"]), set(new["indicadores"])
        # Objetos distintos tornam rótulos de posicionamento incomparáveis, mesmo se iguais.
        same_target = (key != "Posicionamento" or
            (original.get("referencia_posicionamento") == independent.get("referencia_posicionamento")
             if "referencia_posicionamento" in original or "referencia_posicionamento" in independent else
             original["opiniao"].get("objeto_do_posicionamento") == independent["opiniao"].get("objeto_do_posicionamento")))
        comparisons.append({"dimensao_ou_proposta": key, "grupo": "dimensoes",
            "indicadores_anotador": sorted(left), "indicadores_julgador": sorted(right),
            "objetos_comparaveis": same_target,
            "concordancia_exata": left == right if same_target else None,
            "ambos_sem_classificacao": not left and not right,
            "jaccard": len(left & right) / len(left | right) if same_target and left | right else None,
            "revisao_necessaria": not same_target or left != right or bool(old["pendencias"] or new["pendencias"]),
            "justificativa_julgador": new["justificativa"], "evidencias_julgador": new["evidencias"]})
    return comparisons


def review_queue(document, result):
    queue = [{"prioridade": "alta", "tipo": "segmentacao_divergente", "turno_id": item["turno_id"]}
             for item in result["fronteiras"]["divergencias"]]
    for speech in result["avaliacoes_falas"]:
        for comparison in speech["comparacoes"]:
            if comparison["revisao_necessaria"]:
                queue.append({"prioridade": "media", "tipo": "classificacao_ou_objeto_pendente",
                              "fala_id": speech["fala_id"], "dimensao": comparison["dimensao_ou_proposta"]})
        props = speech["avaliacao_propostas"]
        if props["possiveis_omissoes"] or props["originais_nao_reencontradas"]:
            queue.append({"prioridade": "media", "tipo": "propostas_divergentes", "fala_id": speech["fala_id"]})
    for participant in result["avaliacoes_resumos"]:
        if not participant["fiel"]:
            queue.append({"prioridade": "media", "tipo": "resumo_inconsistente", "participante": participant["participante"]})
    return queue


def judge_document(document, client, checkpoint=None):
    errors = structural_errors(document)
    result = {"debate_id": document.get("debate_id"), "sha256_documento_anotado": digest(document),
              "validacao_estrutural": {"valido": not errors, "erros": errors},
              "status": "estrutura_invalida" if errors else "em_avaliacao", "avaliacoes_falas": []}
    if errors:
        return result
    require(document.get("status") in ("anotado_aguardando_julgamento", "anotado_aguardando_revisao_humana"), "Selecione debate.json concluído pelo anotador.")
    if client is None:
        result["status"] = "estrutura_valida_sem_avaliacao_semantica"
        return result
    result["modelo_julgador"] = client.identity()
    result["modelo_anotador"] = document.get("modelo_anotador")
    result["mesmo_modelo_e_endpoint"] = (
        document.get("modelo_anotador", {}).get("modelo") == client.identity()["modelo"] and
        document.get("modelo_anotador", {}).get("endpoint") == client.identity()["endpoint"])
    # Não forneça os agrupamentos nem propostas originais ao julgador nesta fase.
    independent = {key: document[key] for key in ("transcricao", "turnos", "tema")}
    group_speeches(independent, client)
    left, right = membership(document), membership(independent)
    result["fronteiras"] = {
        "divergencias": [{"turno_id": tid, "anotador": left[tid], "julgador": right[tid]}
                         for tid in left if left[tid] != right[tid]],
        "decisoes_independentes": independent["decisoes_de_fronteira"]}
    if checkpoint:
        save(checkpoint, result)
    progress("Julgamento — falas", 0, len(document["falas"]))
    for i, speech in enumerate(document["falas"], 1):
        print(f"Julgador: fala {i}/{len(document['falas'])}…", flush=True)
        # Reanotação dos mesmos objetos permite comparação; discordâncias de segmentação ficam separadas.
        annotation = annotate_speech(document, speech, client, judge=True)
        result["avaliacoes_falas"].append({"fala_id": speech["id"],
                                           "comparacoes": compare_annotations(speech["anotacao"], annotation),
                                           "anotacao_independente": annotation,
                                           "avaliacao_propostas": proposal_comparison(
                                               [{"id": f"p{k+1}", **p} for k, p in enumerate(speech["anotacao"]["propostas"])],
                                               [{"id": f"p{k+1}", **p} for k, p in enumerate(annotation["propostas"])], client)})
        if checkpoint:
            save(checkpoint, result)
        progress("Julgamento — falas", i, len(document["falas"]))
    result["avaliacoes_resumos"] = []
    by_id = {s["id"]: s for s in document["falas"]}
    progress("Julgamento — resumos das falas", 0, len(document["falas"]))
    for number, speech in enumerate(document["falas"], 1):
        def validate(value):
            require(set(value) == {"fiel", "justificativa", "problemas"}, "Campos de avaliação do resumo inválidos.")
            require(type(value["fiel"]) is bool and isinstance(value["problemas"], list), "Avaliação de resumo inválida.")
            text_field(value["justificativa"], 700)
            for problem in value["problemas"]:
                text_field(problem, 500)
            require(value["fiel"] == (not value["problemas"]), "Fidelidade incompatível com problemas relatados.")
        if not speech["anotacao"].get("resumo"):
            evaluation = {"fiel": False, "justificativa": "Resumo da fala ainda pendente.", "problemas": ["Resumo ausente."]}
        else:
            evaluation = client.ask("avaliar_resumo", {"participante": speech["participante"],
                "fala": speech["texto_titular"], "resumo": speech["anotacao"]["resumo"],
                "anotacao": speech["anotacao"]}, validate)
        result["avaliacoes_resumos"].append({"participante": speech["participante"], "fala_id": speech["id"], **evaluation})
        if checkpoint:
            save(checkpoint, result)
        progress("Julgamento — resumos das falas", number, len(document["falas"]))
    comparisons = [c for speech in result["avaliacoes_falas"] for c in speech["comparacoes"]]
    nonempty = [c for c in comparisons if c["objetos_comparaveis"] and not c["ambos_sem_classificacao"]]
    grouped = {}
    for dimension in TAXONOMY:
        name = dimension["dimension"]
        items = [c for c in comparisons if
                 c["dimensao_ou_proposta"] == name]
        eligible = [c for c in items if c["objetos_comparaveis"] and not c["ambos_sem_classificacao"]]
        grouped[name] = {"comparacoes": len(items), "ambos_sem_classificacao": sum(c["ambos_sem_classificacao"] for c in items),
                         "objetos_distintos": sum(not c["objetos_comparaveis"] for c in items),
                         "concordancia_exata_com_alguma_classificacao":
                             sum(c["concordancia_exata"] for c in eligible) / len(eligible) if eligible else None}
    result["metricas"] = {
        "comparacoes": len(comparisons), "ambos_sem_classificacao": sum(c["ambos_sem_classificacao"] for c in comparisons),
        "objetos_distintos": sum(not c["objetos_comparaveis"] for c in comparisons),
        "concordancia_exata_com_alguma_classificacao":
            sum(c["concordancia_exata"] for c in nonempty) / len(nonempty) if nonempty else None,
        "divergencias": sum(c["concordancia_exata"] is False for c in comparisons), "por_dimensao": grouped,
        "interpretacao": "Concordância entre modelos; não é acurácia, F1 nem validação com especialistas."}
    result["status"] = "avaliado_aguardando_revisao_humana"
    result["fila_revisao"] = review_queue(document, result)
    result["recibos_cache"] = sorted(set(client.receipts))
    result["limitacoes"] = [
        "A reanotação recebe as mesmas falas integrais, sem os rótulos originais. Objetos de posicionamento textualmente distintos exigem revisão e são excluídos da concordância.",
        "A formação de falas é refeita; propostas opcionais são reextraídas por fala. Resumos são conferidos contra as análises originais, cuja correção é julgada separadamente.",
        "Concordância não comprova correção; modelos e critérios podem compartilhar vieses.",
        "A validade externa da taxonomia exige protocolo de anotação e avaliação humana.",
        "Nenhuma classificação é alterada automaticamente. Alegações dos oradores não são verificadas como fatos externos."]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entrada", type=Path, default=Path(__file__).parent / "resultados")
    parser.add_argument("--saida", type=Path, default=Path(__file__).parent / "avaliacoes")
    parser.add_argument("--somente-estrutura", action="store_true", help="Não chama API; não avalia significado.")
    interval_arguments(parser)
    model_arguments(parser, "julgador")
    args = parser.parse_args()
    check_interval(args)
    paths = [args.entrada] if args.entrada.is_file() else sorted(set(args.entrada.glob("*_anotado.json")) | set(args.entrada.rglob("debate.json")))
    require(paths, "Nenhum debate.json encontrado. Execute o anotador primeiro.")
    client = None
    failures, summaries = [], []
    for path in paths:
        try:
            document = load_debate(path)
            line_number = document.get("fonte", {}).get("linha")
            require(type(line_number) is int and line_number >= 1, "Resultado sem número de linha de origem válido.")
            if line_number < args.inicio or (args.fim is not None and line_number > args.fim):
                continue
            if not args.somente_estrutura and client is None:
                prompts = PromptCatalog(args.prompts)
                client = Client(args, args.saida / "cache_julgador", prompts=prompts)
                client.start()
            run = digest({"documento": document, "julgador": client.identity() if client else None})[:16]
            destination = args.saida / ("avaliacao_" + run + ".json")
            require(destination.resolve() != path.resolve(), "A avaliação não pode sobrescrever a anotação.")
            result = judge_document(document, client,
                                    args.saida / ("andamento_" + run + ".json"))
            result["arquivo_anotado"] = str(path.resolve())
            save(destination, result)
            summaries.append({"arquivo": str(destination), "status": result["status"], "metricas": result.get("metricas")})
            if result["status"] == "estrutura_invalida":
                failures.append({"arquivo": str(path), "erros": result["validacao_estrutural"]["erros"]})
            print(f"Salvo: {destination}", flush=True)
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
            failures.append({"arquivo": str(path), "erro": str(error)})
            break
    save(args.saida / "ultima_execucao.json", {"avaliacoes": summaries, "falhas": failures,
                                                "chamadas": client.calls if client else 0,
                                                "acertos_cache": client.hits if client else 0})
    log(f"Julgamento encerrado: {len(summaries)} relatório(s), {len(failures)} falha(s); "
        f"{client.calls if client else 0} requisições HTTP, {client.hits if client else 0} acertos de cache.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
