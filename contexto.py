"""Orçamento conservador: entrada estimada + reserva de saída <= 70–80% da janela."""
from __future__ import annotations
from suporte import dumps, require


class ContextLimitError(ValueError):
    """A entrada inteira não cabe no orçamento, antes de qualquer envio."""


def model_limits(envelope, model):
    """Somente limites explícitos do modelo exato; não confundir uso com capacidade."""
    data = envelope.get("data", envelope) if isinstance(envelope, dict) else []
    entries = data if isinstance(data, list) else [data]
    entry = next((item for item in entries if isinstance(item, dict) and item.get("id") == model), {})
    containers = [entry] + [entry.get(name, {}) for name in ("top_provider", "per_request_limits")]
    def smallest(names):
        values = [item.get(name) for item in containers if isinstance(item, dict) for name in names]
        return min((x for x in values if type(x) is int and x > 0), default=0)
    return {"context": smallest(("context_length", "context_window", "max_model_len")),
            "output": smallest(("max_completion_tokens", "max_output_tokens"))}


class ContextBudget:
    def __init__(self, context, fraction=0.75, output=0, provider_output=0, source="configuração"):
        require(type(context) is int and context > 0,
                "O provedor não informou a janela. Preencha contexto_tokens no .config com o limite documentado do modelo.")
        require(0.70 <= fraction <= 0.80, "uso_contexto deve estar entre 0.70 e 0.80.")
        require(output >= 0 and provider_output >= 0, "Limites de saída devem ser >= 0.")
        self.context, self.fraction, self.source = context, fraction, source
        self.total = int(context * fraction)
        automatic = min(4096, max(256, self.total // 5))
        self.output = min(output or automatic, provider_output or output or automatic)
        require(self.output < self.total, "Reserva de saída esgota o orçamento de contexto.")

    @staticmethod
    def estimate(messages):
        # Uma unidade por byte UTF-8, incluindo JSON, mais reserva do formato de chat.
        # Evita a aproximação otimista de quatro caracteres por token. Não é tokenizer universal.
        return len(dumps(messages).encode("utf-8")) + 1024

    def check(self, messages):
        estimated = self.estimate(messages)
        if estimated + self.output > self.total:
            raise ContextLimitError(
                "Mensagem excede o teto de contexto com a reserva de saída. Use um modelo com janela maior (ou confira o limite configurado). A fala não será dividida, truncada ou resumida previamente.")
        return estimated

    def report(self):
        return {"janela_tokens": self.context, "origem_limite": self.source,
                "fracao_maxima": self.fraction, "orcamento_total": self.total,
                "reserva_saida_tokens": self.output, "entrada_estimada_maxima": self.total - self.output,
                "contagem": "estimativa conservadora por bytes UTF-8 + 1024; não é tokenização exata"}
