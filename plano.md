# Plano de implementação: avaliador de leads (fatia 1)

Base: [fluxo.md](fluxo.md). Fatias verticais: cada uma termina com o grafo rodando ponta a ponta e verificado; o que ainda não existe fica como *stub* (substituto fixo, sem LLM).

## Decisões fechadas
| Decisão | Escolha |
|---|---|
| Modelo | Gemini, camada grátis (`langchain-google-genai`). Chave colocada por você no `.env`; nome do modelo configurável (`GEMINI_MODEL`) |
| Repositório | Git local + repositório privado no seu GitHub; commit e push por fatia verificada |
| Planilha | Caminho fixo em `config.py` (somente leitura) |
| Ler o site | Só a home é pedida no prompt; a ferramenta aceita qualquer página do mesmo domínio |
| Classificação | Mesmo modelo da análise |
| Gancho/O que oferecer da planilha | Não entram no prompt: o agente conclui pelo site |
| Execuções | `thread_id` novo por execução; retoma só se a última não chegou ao Fim |
| Aprovação | Flag `--auto` para pular (nasce na Fatia 3, aprovação entra na Fatia 4) |

## Ajustes na spec descobertos nos dados
- **`Status` nunca é vazio**: os 434 leads estão como `"Novo"`. Critério de "não contatado" passa a ser `Status == "Novo"`.
- **`Score` é fórmula viva** na planilha (sem valor em cache); lido como vazio. Valor vem do JSON de scores do pipeline (`SCORES_LEADS`), validado contra a planilha.
- **`texto_site` sai do estado**: o texto lido fica nas mensagens da ferramenta; não há outro consumidor.
- Elegíveis hoje: 240 leads com site.

→ Atualizar a Etapa 1 e o estado no `fluxo.md` com isso antes de codar.

## Estrutura
```
avaliador-lead/
  fluxo.md  plano.md  README.md
  .env.example  .gitignore  requirements.txt
  avaliador/
    config.py        caminhos, N, limites
    estado.py        Lead, Estado, Veredito
    dados.py         planilha, scores, resultados.json, cache de sites
    ferramentas.py   ler_site
    prompts.py       analista, classificador, redator
    grafo.py         nós das 8 etapas + montagem
  main.py            CLI (iniciar, aprovar, retomar)
  tests/
```
Fora do git: `.venv/`, `.env`, `checkpoints.db`, `resultados.json` (dados de empresas).

## Fatias

### Fatia 0: preparação
- `venv`, dependências, estrutura, `.gitignore`, `.env.example`.
- Conferir na versão instalada as APIs que a spec usa sem verificação.
- Repositório: `git init`, criar repo privado no GitHub, primeiro push.
- **Verificação:** imports funcionam; `pytest` roda.

### Fatia 1: laço sem LLM (Etapas 1, 2, 7, 8)
- Etapas reais: carregar, selecionar, salvar, fim. Stub no lugar de 3–6 (tudo "descartado").
- **Verificação:** critérios das Etapas 1, 2, 7, 8. Rodar N=5, ver `resultados.json` e resumo; rodar de novo e receber os 5 seguintes; repetir um lead sem duplicar.

### Fatia 2: análise e classificação (Etapas 3, 4)
- `ler_site`, analista com ferramenta, classificador com `with_structured_output`. Positivos vão direto para Salvar.
- **Verificação:** testes com modelo falso (rotas determinísticas); domínio recusado; página local com injeção de prompt; rodada real com 5 leads e leitura dos pareceres.

### Fatia 3: redação (Etapa 6) + modo automático
- Redator com validação (tamanho, placeholders).
- **Verificação:** critérios da Etapa 6; 5 mensagens conferidas à mão.

### Fatia 4: humano no fluxo (Etapa 5)
- `interrupt`, `SqliteSaver`, CLI com aprovar / rejeitar / feedback / sair, ciclo de feedback (máx. 2 revisões).
- **Verificação:** critérios da Etapa 5: sair e retomar, matar o processo e retomar, limite de revisões.

### Fatia 5: fechamento
- Todos os critérios do `fluxo.md`; revisão em agente separado; README de uso; registro no vault.

## O que depende de você
- Criar a chave em https://aistudio.google.com/apikey e colar em `.env` (só necessária a partir da rodada real da Fatia 2).
- No fim: rodar `python main.py` e testar.

## Fora deste plano
Avaliações do Google, envio da mensagem, paralelismo (`Send`), *time travel*.

## Estado atual (13/10/2026)
Fatias 0–4 implementadas e verificadas:
- 15 testes automáticos com modelos falsos passando (etapas 1–8, laço, limite de ferramenta, reset, aprovar/rejeitar, feedback com limite, retomada com novo processo via SQLite).
- Rodada real com Gemini (`gemini-3.5-flash-lite`, modo `--auto`, 2 leads): pareceres com evidências do site e da planilha; mensagens com 404 e 431 caracteres.
- Injeção de prompt em página local: ignorada (não seguiu instrução, não pediu outro domínio).
- CLI real: sair na pausa → retomar no mesmo lead → feedback → aprovar → rejeitar → nova execução pega os leads seguintes.

Diferenças de implementação em relação à spec:
- `ToolNode` usa `messages_key="mensagens"`; nós que leem config usam parâmetro `config: RunnableConfig` (exigência do LangGraph).
- No limite de 3 leituras, o analista é chamado mais uma vez pedindo o parecer final; pedidos de ferramenta acima do limite são descartados.
- Classificar força `confianca` para baixa se o parecer diz baixa, e zera a oportunidade quando não é cliente.

Não verificado: integridade do `resultados.json` ao matar o processo no meio da escrita (escrita atômica implementada, não testada com kill).
