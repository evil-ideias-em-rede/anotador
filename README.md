# Preparação e anotação de falas completas

A organização para leitura humana é **debate → participantes → falas**. Cada fala tem seu texto integral, taxonomia, resumo, propostas e interrupções. Não existe mais resumo conjunto de todas as exposições de um participante.

A revisão é humana. Nenhuma etapa desta pipeline executa o julgador automático.

## Instalação e configuração local

Requer Python 3.9 ou superior. Os scripts usam apenas a biblioteca padrão, sem dependências de `pip`.

Os comandos deste README são executados a partir da pasta que contém `anotação`. Se você publicar somente o conteúdo desta pasta como raiz do repositório, retire o prefixo `anotação/` dos comandos.

Na primeira instalação, **se ainda não existir um `.config`**, crie a cópia local:

```sh
cp anotação/.config.example anotação/.config
```

Abra `anotação/.config` e configure:

- `base_url`: endereço da API compatível com Chat Completions.
- `modelo`: identificador do modelo disponível na sua conta. O exemplo contém uma configuração de referência, não descoberta automática do modelo mais recente.
- `token`: sua chave, exclusivamente nesse arquivo local. Como alternativa, mantenha-o vazio e defina a variável de ambiente `LLM_API_KEY`.
- `contexto_tokens`: mantenha `0` se a API publicar a janela; caso contrário, informe o limite documentado do modelo.
- `parametro_tokens`: nome aceito pelo provedor (`max_tokens` ou `max_completion_tokens`).

Não sobrescreva um `.config` já preenchido. `.config.example` é público e deve permanecer sem credenciais. `.config` é privado e está no `.gitignore`. A preparação e a anotação herdam os valores de `[comum]`, salvo opções específicas em `[anotador]`.

Para conferir a conexão:

```sh
python3 anotação/api.py
```

Esse comando faz uma chamada de teste real ao provedor e pode gerar cobrança. Os dados originais acompanham o projeto na pasta `dados originais dos debates`, dentro de `anotação`: `PublicHearingBR_LDS.jsonl` e `PublicHearingBR_NLI.jsonl`. O preparador usa o LDS dessa pasta por padrão, independentemente do diretório de execução. Para outra fonte, use `preparador.py --entrada "/caminho/PublicHearingBR_LDS.jsonl"`.

### O que publicar

Publique os scripts `.py`, a pasta `interface`, `prompts.xml`, os testes, este `README.md`, `.gitignore`, `.config.example` e a pasta `dados originais dos debates` com os dois JSONL originais. Debates preparados, resultados, cópias de transcrições geradas pela pipeline, arquivos de auditoria, caches e configurações privadas permanecem locais e são ignorados pelo Git. Esses caches podem representar chamadas já pagas; não é necessário apagá-los para publicar o código.

Se usar uma pasta de saída com nome diferente dos padrões, confira se ela está ignorada antes de adicioná-la ao Git. O `.gitignore` não retira arquivos que já estejam rastreados: confira o conteúdo do commit e, se necessário, remova-os somente do índice com `git rm --cached`, preservando as cópias locais. Ignorar um arquivo também não remove versões que já tenham sido publicadas no histórico.

## Definição de fala

Uma fala abrange tudo o que um participante disse enquanto detinha a palavra. Uma interrupção breve não encerra essa titularidade. Se ele encerrar ou ceder a palavra e depois recebê-la novamente, começa outra fala.

Interrupções são guardadas exclusivamente em `interrupcoes` da fala titular. Não recebem ID de fala, taxonomia, propostas ou resumo próprios. Um pedido breve de palavra não é automaticamente uma exposição autônoma. Da mesma forma, uma exposição curta com a palavra concedida não deve ser descartada como interrupção.

A preparação considera turnos completos e o turno seguinte como contexto auxiliar para distinguir um pedido de palavra de sua efetiva concessão. A anotação só acontece depois da formação das falas. Casos inconclusivos interrompem a preparação e ficam disponíveis para conferência; não se decide a titularidade apenas pelo comprimento do texto.

## Estrutura legível

Exemplo abreviado:

```json
{
  "debate_id": 1,
  "tema": "Tema da audiência",
  "participantes": [
    {
      "nome": "Participante A",
      "falas": [
        {
          "id": "f00001",
          "ordem_no_debate": 1,
          "texto": "Exposição integral do titular.",
          "taxonomia": {
            "Alinhamento Temático": ["Focalizado"],
            "Postura do Orador": ["Técnica"],
            "Credibilidade e Validação": ["Referência Externa"],
            "Posicionamento": ["Favorável"]
          },
          "resumo": "Resumo exclusivamente desta exposição, guiado pela taxonomia.",
          "objeto_do_posicionamento": "Medida defendida nesta fala",
          "propostas": [],
          "interrupcoes": [],
          "pendencias_revisao": []
        }
      ]
    }
  ]
}
```

As falas aparecem na ordem de ocorrência dentro de cada participante. `ordem_no_debate` indica a posição do turno inicial na audiência, permitindo reconstruir a ordem global. Quando há interrupção, são informados seu autor, texto, posição após um caractere do texto titular e eventual observação sobre a retomada. O texto do interruptor não é incorporado ao texto usado para classificar o titular.

No preparo, os campos ainda não analisados são `null`. Depois de anotar, uma lista vazia de propostas significa que nenhuma foi extraída; uma lista vazia de interrupções significa que nenhuma foi identificada nessa fala.

## 1. Preparação independente

```sh
python3 anotação/preparador.py
```

Sem intervalo, prepara todo o LDS. Exemplos de seleção:

```sh
python3 anotação/preparador.py --inicio 2 --fim 2
python3 anotação/preparador.py --inicio 5 --fim 10
python3 anotação/preparador.py --inicio 5
```

Os limites são inclusivos; um fim além dos registros disponíveis termina no fim do arquivo. Cada debate preparado gera um JSON, como `anotação/preparados/debate_linha_00001.json`.

A preparação usa o modelo configurado para identificar titularidade e interrupções, mas não anota a taxonomia. Preparações concluídas da mesma fonte, modelo e regras são reutilizadas sem chamadas à API.

**Nesta atualização, os critérios de fronteira foram reforçados.** Para reavaliar uma preparação feita antes dela, use outra pasta e preserve os arquivos anteriores:

```sh
python3 anotação/preparador.py --inicio 1 --fim 2 --saida "anotação/preparados_v2"
```

A organização dos arquivos antigos pode ser convertida sem modelo, mas corrigir sua segmentação exige reavaliar as fronteiras. O programa não apresenta uma simples reorganização como nova validação semântica.

## 2. Anotação de um arquivo ou pasta

```sh
python3 anotação/anotador.py --entrada "anotação/preparados_v2/debate_linha_00001.json"
python3 anotação/anotador.py --entrada "anotação/preparados_v2"
```

Sem `--entrada`, usa `anotação/preparados`. A pasta é percorrida apenas no primeiro nível; arquivos administrativos são ignorados. A anotação carrega o preparo, sem reagrupar falas nem reler o LDS.

Para **cada fala separadamente**, sempre com seu texto integral:

1. Identifica a opinião expressa na fala.
2. Aplica cada uma das quatro dimensões da taxonomia.
3. Extrai propostas opcionais.
4. Produz o `resumo` daquela fala, orientado pelo conteúdo e pela taxonomia.

A tarefa `resumo_fala` recebe somente a fala em questão, sua classificação e suas propostas. Nenhuma requisição reúne todas as falas de um participante. O participante funciona como agrupamento para leitura do JSON.

O arquivo final, com sufixo `_anotado.json`, é salvo em `anotação/resultados`. Use `--saida` para outra pasta. Chamadas válidas ficam em cache; respostas de tarefas com os mesmos dados e prompts podem ser reutilizadas. O novo resumo por fala exige chamadas próprias quando ainda não existe em cache.

## JSON principal e auditoria

O JSON principal contém as informações para leitura e revisão. A transcrição original, turnos, evidências, justificativas detalhadas e decisões de fronteira ficam no arquivo correspondente da subpasta `auditoria`, indicado em `arquivo_auditoria`. São dados auxiliares; o revisor não precisa abrir esse arquivo para ler as falas e classificações.

Os dois arquivos devem ser mantidos juntos para reutilização pela pipeline. O carregamento verifica que o JSON legível corresponde à auditoria, evitando ignorar silenciosamente edições manuais. Para fazer correções ou notas de revisão, preserve uma cópia; esta versão ainda não importa alterações humanas automaticamente.

A representação técnica interna mantém referências para conferência de integridade, mas a saída principal agrupa efetivamente os objetos completos de fala dentro de seus participantes.

## Reorganizar resultados anteriores sem API

```sh
python3 anotação/formato.py --entrada "anotação/resultados" --saida "anotação/resultados/organizados"
```

Também é possível fornecer um único arquivo em `--entrada`. Os originais são preservados. Na conversão, cada resumo é montado literalmente com a opinião e os rótulos já existentes **da mesma fala**, sem inferir novas informações ou reunir falas diferentes. Isso fica documentado na auditoria. Os agrupamentos anteriores são preservados e devem ser conferidos; nenhuma nova análise pelo modelo ocorre nessa conversão.

## Evidências e limites

Evidências não têm limite rígido de 240 caracteres. Falhas apenas na citação viram pendências para revisão humana, sem descartar a classificação, bloquear o debate ou chamar um julgador. O conteúdo recebido é preservado na auditoria. Correspondência textual não garante relevância da evidência nem correção da classificação.

O tema vem dos metadados e não implica consenso. Favorável e Contrário têm como referência comum o conteúdo do tema recebido. Ausência de proposta não impede a taxonomia; ausência de posicionamento não equivale automaticamente a neutralidade.

O orçamento de contexto continua entre 70% e 80% da janela, com padrão de 75%, incluindo a reserva de saída. Uma fala nunca é dividida, truncada ou resumida previamente para caber no modelo. Se somente a tarefa de resumo exceder o contexto, o resumo **daquela fala** fica pendente e a análise prossegue; texto, taxonomia e propostas permanecem preservados.

Erros de fonte, API, JSON ou taxonomia continuam sendo sinalizados. As configurações e o token permanecem em `.config`. As instruções ficam em `prompts.xml`. O antigo `julgador.py` continua separado e não faz parte do fluxo de revisão humana.

## Verificação

```sh
python3 -B -m unittest discover -s anotação/testes -v
```

Os 68 testes passaram, sem chamadas reais à API. Cobrem agrupamento por participante, várias falas independentes da mesma pessoa, envio da fala inteira, resumo exclusivo de cada fala, interrupções sem duplicação como falas, preparação reutilizável, evidências não bloqueantes e carregamento dos arquivos principal/auditoria.

Também foi conferida uma cópia organizada do resultado existente: 17 participantes, 55 falas e 5 interrupções, com textos preservados. Esses números refletem a segmentação anterior, não uma nova avaliação de sua correção semântica.

### Posicionamento diretamente em relação ao tema

A classificação recebe o tema original e a fala inteira, sem um objeto inferido da opinião. Favorável concorda ou sustenta o conteúdo do tema; Contrário o contesta; Neutro se abstém explicitamente ou suspende o julgamento; Ambíguo apresenta posição incerta ou não unívoca. Confirmar uma acusação não significa aprovar a prática acusada. Falas procedimentais ou sem posição identificável ficam sem rótulo, com justificativa. Se o tema não permitir uma direção de concordância, não se inventa uma tese. O campo de compatibilidade `objeto_do_posicionamento` no JSON legível contém agora o próprio tema. Resultados anteriores não são corrigidos automaticamente: é preciso anotar novamente.

### Prompts específicos por dimensão

Inspirado na organização das perguntas de classificação do [HuNeBR](https://github.com/llm-pt-ibm/brazilian_northeast_humor_benchmark/blob/main/llm_prompt_manager.py), o cliente envia somente a definição da dimensão solicitada. Regras comuns, saída estruturada, tema e fala integral permanecem presentes. O catálogo original não é alterado ao gerar cada mensagem. Essa redução de instruções foi testada estruturalmente; sua influência na qualidade semântica depende de comparação com revisão humana.

O número de chamadas por fala não aumentou. Prompts de classificação diferentes geram novas chaves de cache e uma nova identificação de execução; resultados anteriores são preservados. Nenhum julgador automático foi adicionado. Exemplos de anotação e métricas contra um gabarito humano continuam sendo melhorias a avaliar, não resultados já demonstrados.


### Critérios operacionais dos indicadores

As definições completas enviadas ao modelo estão em `prompts.xml`, em `anotacao/regras_dimensoes`; as descrições de `TAXONOMY` em `anotador.py` seguem o mesmo escopo. Os nomes dos quatro grupos e dos indicadores foram preservados. A classificação continua sendo da fala inteira, com uma dimensão por chamada.

- **Alinhamento:** Focalizado desenvolve o tema; Periférico desenvolve principalmente assuntos correlatos com ligação superficial; Desalinhado trata de outro assunto sem conexão substantiva. Discordância pode ser focalizada. Procedimentos sem conteúdo avaliável não recebem rótulo. Não se infere intenção de fuga.
- **Postura:** Agressiva exige hostilidade ou ataque textual; Emocional exige expressão ou apelo afetivo; Confiante exige segurança discursiva identificável; Técnica exige articulação de conteúdo especializado. Não se inferem voz ou psicologia. Os indicadores podem coexistir.
- **Credibilidade:** a experiência do titular, a fonte externa ou o recurso retórico devem funcionar como sustentação, não apenas ser mencionados. Recursos retóricos podem acompanhar dados e autoridade própria. A classificação não certifica veracidade.
- **Posicionamento:** concordância e discordância se referem ao conteúdo do tema, não ao interlocutor nem a uma proposta descoberta na fala. Neutralidade exige manifestação textual de suspensão ou abstenção; ausência de posição fica sem rótulo. Ambiguidade exige posição não unívoca, não mera dificuldade do anotador. Ressalvas e posições sobre aspectos diferentes não implicam contradição. Confirmar uma acusação de censura e justificar a censura continua confirmando a acusação.

Temas que não oferecem uma direção de concordância não são convertidos automaticamente em teses. Nesses casos, Posicionamento fica sem indicador, com justificativa. Os exemplos internos são orientações ilustrativas, não um gabarito humano de avaliação.

Essa operacionalização é uma proposta do projeto, ainda sujeita à validação humana por amostragem. As referências abaixo oferecem aproximações conceituais; a consulta não estabelece que os autores tenham proposto estes rótulos ou validado esta combinação:

- [Van Dijk, Macrostructures (1980)](https://discourses.org/wp-content/uploads/2022/06/Teun-A.-van-Dijk-1980-Macrostructures.-An-Interdisciplinary-Study-Of-Global-Structures-In-Discourse-Interaction-And-Cognition.pdf): temas e organização global do discurso como aproximação para a pertinência temática. Os três graus usados aqui são decisões operacionais do projeto.
- [Charaudeau, De l’argumentation entre les visées d’influence de la situation de communication](https://www.patrick-charaudeau.com/De-l-argumentation-entre-les-visees-d-influence-de-la-situation-de.html): construção discursiva da imagem de si e relação afetiva com o auditório. Não se atribui a essa fonte uma escala pronta de quatro posturas, nem a nomenclatura “ethos anunciado/mostrado” sem verificação específica.
- [Perelman e Olbrechts-Tyteca, Traité de l’argumentation](https://www.editions-ulb.be/en/book/?GCOI=74530100633530): recursos discursivos para obter adesão. A separação entre três indicadores não é apresentada como reprodução da obra.
- [Bardin, L’analyse de contenu](https://shs.cairn.info/l-analyse-de-contenu--9782130627906): referência metodológica para análise de comunicações; não comprova, por si só, a autoria ou validade desta escala de posicionamento.

A melhoria semântica deve ser medida em uma amostra humana representativa, separada dos exemplos usados para ajustar as regras. Uma estimativa de qualidade do corpus não transforma cada fala não revisada em uma anotação individualmente validada.

Para aplicar estas definições, execute novamente a anotação sobre os arquivos preparados. A preparação e suas regras de segmentação não mudaram. A alteração dos prompts gera nova identificação de execução e novas entradas de cache para as classificações alteradas; resultados antigos permanecem preservados. Não é necessário apagar caches nem preparar o corpus novamente.


## Interface de revisão humana

Na pasta principal do projeto, execute:

```sh
python3 anotação/visualizar.py
```

Se estiver dentro de `anotação`, execute `python3 visualizar.py`. Não é necessário instalar pacotes nem configurar token para a interface. O comando abre o navegador em `http://127.0.0.1:8765`; mantenha o terminal aberto. Para encerrar, use Ctrl+C. Se a porta estiver ocupada, use `--porta 0` e abra o endereço mostrado. `--sem-abrir` inicia sem abrir o navegador automaticamente.

1. Clique em **Abrir JSONs** e escolha resultados, ou em **Abrir pasta** e escolha a pasta de resultados. A seleção de pasta permite associar cada JSON à sua subpasta `auditoria`. Arquivos de cache, andamento e arquivos auxiliares não viram debates. Os formatos por participante e interno anterior são aceitos para leitura, sem converter os originais.
2. Informe o nome ou identificador do validador. Navegue por debate e participante; as falas de cada participante aparecem separadamente. A busca consulta nome e texto integral, e os filtros mostram registros não revisados, decisões humanas, dúvidas ou pendências do modelo.
3. Leia a **fala integral**, com interrupções destacadas em sua posição quando disponível. A aba **Justificativas e evidências** mostra os detalhes da auditoria. Quando uma auditoria estiver ausente ou incompatível, a interface informa a limitação e mantém a revisão disponível.
4. Confira os indicadores, o resumo e as propostas; registre problemas de titularidade/interrupção nas observações. A interface não refaz segmentação nem altera os textos. As definições em **Consultar critérios atuais** vêm diretamente do código e de `prompts.xml`, não de uma cópia manual. São os critérios atuais: resultados históricos podem ter usado outras instruções.
5. Escolha **Confirmar anotação original**, **Registrar correções** ou **Inconclusiva**, e clique em **Registrar revisão desta fala**. Alterações permanecem rascunhos até esse registro. Para Alinhamento e Posicionamento, só é permitido um indicador; desmarcar todos representa ausência de rótulo. Postura e Credibilidade aceitam múltiplos indicadores.
6. Use **Exportar revisão** para baixar um JSON separado, com autoria por fala, decisões, anotações humanas, observações, datas, critérios atuais e hash SHA-256 dos arquivos-fonte. Para continuar em outra sessão ou computador, abra os mesmos arquivos originais e use **Importar revisão**. Uma revisão de outra versão do arquivo-fonte é recusada; conflitos locais exigem escolha explícita antes da substituição.

Os rascunhos ficam no armazenamento local do navegador quando disponível. Esse armazenamento depende do navegador e do endereço/porta e pode ser apagado; a exportação é a cópia durável. Nenhum arquivo original é sobrescrito. Os JSONs exportados podem ser guardados em `anotação/revisoes/`, ignorada pelo Git.

A interface não chama modelos nem envia transcrições para serviços externos. O servidor atende apenas em `127.0.0.1` e disponibiliza a interface e o guia, sem expor `.config`, resultados ou a pasta inteira. Os JSONs selecionados são lidos no próprio navegador.

A revisão pode abranger somente a amostra definida pelo pesquisador. A interface não sorteia uma amostra representativa automaticamente: essa seleção depende do protocolo do estudo. O contador informa cobertura, não acurácia; falas ausentes do arquivo de revisão não foram revisadas, rascunhos não são decisões, e decisões inconclusivas não confirmam a anotação. O aplicativo não generaliza métricas nem transforma o restante do corpus em registros individualmente validados.

Os testes adicionais verificam acesso restrito do servidor, correspondência do guia com os prompts, compatibilidade dos formatos, preservação de falas/interrupções e consistência das decisões. O teste JavaScript usa Node quando disponível; Node não é necessário para executar a interface.
