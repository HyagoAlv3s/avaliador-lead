"""Montagem do grafo. Cada função corresponde a uma etapa do fluxo.md."""
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from . import config as conf, prompts
from .dados import carregar_planilha, gravar_resultado
from .estado import Estado, Veredito
from .ferramentas import ler_site


def _cfg(cfg, chave, padrao):
    return (cfg or {}).get("configurable", {}).get(chave, padrao)


def _opcional(valor) -> Path | None:
    return Path(valor) if valor else None


def parecer(estado: Estado) -> str:
    """Parecer da rodada atual: última resposta final do analista após a última mensagem humana."""
    msgs = estado.get("mensagens", [])
    ultimo_humano = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
    for m in reversed(msgs[ultimo_humano + 1:]):
        if isinstance(m, AIMessage) and not m.tool_calls:
            return m.text.strip()
    return ""


def contar_chamadas(estado: Estado) -> int:
    msgs = estado.get("mensagens", [])
    ultimo_humano = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
    return sum(isinstance(m, ToolMessage) for m in msgs[ultimo_humano + 1:])


def formatar_lead(lead) -> str:
    linhas = [f"Empresa: {lead['empresa']}", f"Site: {lead['site']}"]
    linhas += [f"{k}: {v}" for k, v in lead["contexto"].items()]
    return "Dados do lead (planilha):\n" + "\n".join(linhas)


def validar_mensagem(texto: str) -> str | None:
    if not texto:
        return "mensagem vazia"
    if len(texto) > conf.MAX_MENSAGEM:
        return f"tem {len(texto)} caracteres; o máximo é {conf.MAX_MENSAGEM}"
    if re.search(r"[\[\]{}]", texto):
        return "contém colchetes ou chaves (placeholder)"
    return None


def confianca_do_parecer(texto: str) -> str | None:
    m = re.search(r"confian[çc]a[^:\n]*:\s*\**\s*(alta|m[ée]dia|baixa)", texto, re.IGNORECASE)
    return m.group(1).lower().replace("é", "e") if m else None


def status_do_lead(estado: Estado) -> str:
    if estado.get("motivo_pulo"):
        return "pulado"
    if (estado.get("decisao_humana") or {}).get("acao") == "rejeitar":
        return "rejeitado"
    if estado.get("classificacao") and not estado["classificacao"]["cliente_provavel"]:
        return "descartado"
    if estado.get("mensagem"):
        return "aprovado"
    return "pulado"


def limite_recursao(n: int) -> int:
    return 40 * n + 20


def criar_grafo(llm_analista, classificador, llm_redator, auto=False, checkpointer=None):
    """llm_analista: chat model com bind_tools; classificador: runnable que devolve Veredito;
    llm_redator: chat model. auto=True pula a aprovação humana."""
    analista_com_ferramentas = llm_analista.bind_tools([ler_site])
    redator = ChatPromptTemplate.from_messages([
        ("system", prompts.REDATOR),
        ("human", "Empresa: {empresa} ({setor})\nOportunidade: {oportunidade}\n"
                  "Justificativa: {justificativa}\n\nParecer:\n{parecer}\n\n{correcao}"),
    ]) | llm_redator | StrOutputParser()

    # Etapa 1
    def carregar_leads(estado: Estado, config: RunnableConfig) -> dict:
        leads = carregar_planilha(
            _opcional(_cfg(config, "planilha", conf.PLANILHA)),
            _opcional(_cfg(config, "scores", conf.SCORES)),
            Path(_cfg(config, "resultados", conf.RESULTADOS)),
            _cfg(config, "n", conf.N_PADRAO))
        return {"leads": leads, "indice_atual": 0, "resultados": []}

    # Etapa 2
    def selecionar_lead(estado: Estado) -> dict:
        i, leads = estado["indice_atual"], estado["leads"]
        if not 0 <= i < len(leads):
            raise IndexError(f"indice_atual {i} fora da lista ({len(leads)})")
        lead = leads[i]
        motivo = None
        if not lead.get("site"):
            motivo = "sem site"
        elif lead["id"] in {r["id"] for r in estado.get("resultados", [])}:
            motivo = "já processado"
        return {"lead_atual": lead, "motivo_pulo": motivo, "classificacao": None,
                "decisao_humana": None, "mensagem": None, "revisoes": 0,
                "mensagens": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]}

    def lead_valido(estado: Estado) -> str:
        return "pular" if estado["motivo_pulo"] else "analisar"

    # Etapa 3
    def agente_analista(estado: Estado) -> dict:
        msgs = [SystemMessage(prompts.ANALISTA),
                HumanMessage(formatar_lead(estado["lead_atual"]))] + estado.get("mensagens", [])
        try:
            if contar_chamadas(estado) >= conf.MAX_CHAMADAS_FERRAMENTA:
                resposta = analista_com_ferramentas.invoke(msgs + [HumanMessage(prompts.LIMITE_FERRAMENTA)])
                if resposta.tool_calls:  # descarta pedidos acima do limite
                    resposta = AIMessage(content=resposta.text, id=resposta.id)
            else:
                resposta = analista_com_ferramentas.invoke(msgs)
        except Exception as erro:
            return {"motivo_pulo": f"erro no analista: {type(erro).__name__}: {erro}"[:300]}
        return {"mensagens": [resposta]}

    def rota_analista(estado: Estado) -> str:
        if estado.get("motivo_pulo"):
            return "salvar"
        if estado["mensagens"][-1].tool_calls:
            return "ferramentas"
        return "classificar"

    # Etapa 4
    def classificar(estado: Estado) -> dict:
        texto = parecer(estado)
        if not texto:
            return {"motivo_pulo": "parecer vazio"}
        prompt = [SystemMessage(prompts.CLASSIFICADOR),
                  HumanMessage(f"Empresa: {estado['lead_atual']['empresa']}\n\nParecer:\n{texto}")]
        for _ in range(2):
            try:
                v = classificador.invoke(prompt)
                if not isinstance(v, Veredito):
                    v = Veredito.model_validate(v)
            except Exception:
                continue
            if confianca_do_parecer(texto) == "baixa" and v.confianca == "alta":
                v.confianca = "baixa"
            if not v.cliente_provavel:
                v.oportunidade_principal = ""
            return {"classificacao": v.model_dump()}
        return {"motivo_pulo": "falha ao classificar"}

    def rota_classificacao(estado: Estado) -> str:
        if estado.get("motivo_pulo") or not estado["classificacao"]["cliente_provavel"]:
            return "salvar"
        return "redigir" if auto else "aprovar"

    # Etapa 5
    def aprovacao_humana(estado: Estado) -> dict:
        lead = estado["lead_atual"]
        revisoes = estado.get("revisoes", 0)
        decisao = interrupt({
            "posicao": f"{estado['indice_atual'] + 1}/{len(estado['leads'])}",
            "empresa": lead["empresa"], "score": lead["score"], "site": lead["site"],
            "parecer": parecer(estado), "veredito": estado["classificacao"],
            "revisoes": revisoes, "pode_revisar": revisoes < conf.MAX_REVISOES,
        })
        if decisao["acao"] == "feedback" and revisoes >= conf.MAX_REVISOES:
            decisao = {"acao": "aprovar"}  # proteção caso a CLI não respeite o limite
        if decisao["acao"] == "feedback":
            return {"decisao_humana": decisao, "revisoes": revisoes + 1,
                    "mensagens": [HumanMessage(f"Feedback do revisor: {decisao['texto']}\n"
                                               "Refaça o parecer considerando isso.")]}
        return {"decisao_humana": decisao}

    def rota_aprovacao(estado: Estado) -> str:
        return {"aprovar": "redigir", "rejeitar": "salvar",
                "feedback": "reanalisar"}[estado["decisao_humana"]["acao"]]

    # Etapa 6
    def redigir_mensagem(estado: Estado) -> dict:
        lead, c = estado["lead_atual"], estado["classificacao"]
        entrada = {"empresa": lead["empresa"], "setor": lead["contexto"].get("Setor", ""),
                   "oportunidade": c["oportunidade_principal"], "justificativa": c["justificativa"],
                   "parecer": parecer(estado), "correcao": ""}
        for _ in range(2):
            try:
                texto = redator.invoke(entrada).strip()
            except Exception as erro:
                return {"motivo_pulo": f"erro no redator: {type(erro).__name__}"}
            problema = validar_mensagem(texto)
            if not problema:
                print(f"\n--- Mensagem para {lead['empresa']} ---\n{texto}\n")
                return {"mensagem": texto}
            entrada["correcao"] = f"A versão anterior foi recusada: {problema}. Corrija."
        return {"motivo_pulo": "falha ao redigir"}

    # Etapa 7
    def salvar(estado: Estado, config: RunnableConfig) -> dict:
        lead = estado["lead_atual"]
        registro = {
            "id": lead["id"], "empresa": lead["empresa"], "site": lead["site"],
            "score": lead["score"], "status": status_do_lead(estado),
            "motivo": estado.get("motivo_pulo") or (estado.get("decisao_humana") or {}).get("motivo"),
            "parecer": parecer(estado) or None, "classificacao": estado.get("classificacao"),
            "revisoes": estado.get("revisoes", 0), "mensagem": estado.get("mensagem"),
            "processado_em": datetime.now().isoformat(timespec="seconds"),
        }
        gravar_resultado(Path(_cfg(config, "resultados", conf.RESULTADOS)), registro)
        return {"resultados": [registro], "indice_atual": estado["indice_atual"] + 1}

    def ha_mais_leads(estado: Estado) -> str:
        return "sim" if estado["indice_atual"] < len(estado["leads"]) else "nao"

    # Etapa 8
    def fim(estado: Estado, config: RunnableConfig) -> dict:
        contagem = Counter(r["status"] for r in estado["resultados"])
        print(f"\nExecução concluída: {len(estado['resultados'])} leads")
        for status in ("aprovado", "descartado", "rejeitado", "pulado"):
            print(f"  {status + 's:':13}{contagem.get(status, 0)}")
        aprovados = [r for r in estado["resultados"] if r["status"] == "aprovado"]
        if aprovados:
            print("\nMensagens para enviar:")
            for r in aprovados:
                print(f"\n### {r['empresa']} ({r['site']})\n{r['mensagem']}")
        print(f"\nResultados em: {_cfg(config, 'resultados', conf.RESULTADOS)}")
        return {}

    g = StateGraph(Estado)
    g.add_node("carregar_leads", carregar_leads)
    g.add_node("selecionar_lead", selecionar_lead)
    g.add_node("agente_analista", agente_analista)
    g.add_node("ferramentas", ToolNode([ler_site], messages_key="mensagens"))
    g.add_node("classificar", classificar)
    g.add_node("aprovacao_humana", aprovacao_humana)
    g.add_node("redigir_mensagem", redigir_mensagem)
    g.add_node("salvar", salvar)
    g.add_node("fim", fim)

    g.add_edge(START, "carregar_leads")
    g.add_edge("carregar_leads", "selecionar_lead")
    g.add_conditional_edges("selecionar_lead", lead_valido,
                            {"analisar": "agente_analista", "pular": "salvar"})
    g.add_conditional_edges("agente_analista", rota_analista,
                            {"ferramentas": "ferramentas", "classificar": "classificar", "salvar": "salvar"})
    g.add_edge("ferramentas", "agente_analista")
    g.add_conditional_edges("classificar", rota_classificacao,
                            {"aprovar": "aprovacao_humana", "redigir": "redigir_mensagem", "salvar": "salvar"})
    g.add_conditional_edges("aprovacao_humana", rota_aprovacao,
                            {"redigir": "redigir_mensagem", "salvar": "salvar", "reanalisar": "agente_analista"})
    g.add_edge("redigir_mensagem", "salvar")
    g.add_conditional_edges("salvar", ha_mais_leads, {"sim": "selecionar_lead", "nao": "fim"})
    g.add_edge("fim", END)
    return g.compile(checkpointer=checkpointer)
