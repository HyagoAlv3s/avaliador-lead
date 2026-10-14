"""CLI que conduz o grafo: inicia, pausa para aprovação e retoma execuções."""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from avaliador import config
from avaliador.estado import Veredito
from avaliador.grafo import criar_grafo, limite_recursao


def criar_modelos():
    from langchain_google_genai import ChatGoogleGenerativeAI
    modelo = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    llm = ChatGoogleGenerativeAI(model=modelo)
    return llm, llm.with_structured_output(Veredito), llm


def mostrar(p: dict) -> None:
    v = p["veredito"]
    print("\n" + "=" * 70)
    print(f"Lead {p['posicao']}: {p['empresa']} (score {p['score']})"
          f"    Revisão {p['revisoes']}/{config.MAX_REVISOES}")
    print(f"Site: {p['site']}")
    print("--- Parecer ---")
    print(p["parecer"])
    print("--- Veredito ---")
    print(f"Cliente provável: {'sim' if v['cliente_provavel'] else 'não'} | Confiança: {v['confianca']}")
    print(f"Oportunidade: {v['oportunidade_principal']}")
    print(f"Justificativa: {v['justificativa']}")


def perguntar(p: dict) -> dict:
    opcoes = "[a]provar  [r]ejeitar  " + ("[f]eedback  " if p["pode_revisar"] else "") + "[s]air"
    while True:
        escolha = input(f"\n{opcoes} > ").strip().lower()[:1]
        if escolha == "a":
            return {"acao": "aprovar"}
        if escolha == "r":
            return {"acao": "rejeitar", "motivo": input("Motivo (opcional) > ").strip() or None}
        if escolha == "s":
            return {"acao": "sair"}
        if escolha == "f" and p["pode_revisar"]:
            texto = input("Feedback para o analista > ").strip()
            if texto:
                return {"acao": "feedback", "texto": texto}
            print("Feedback vazio. Tente de novo.")


def interrupcao_pendente(estado):
    for tarefa in estado.tasks:
        if tarefa.interrupts:
            return tarefa.interrupts[0].value
    return None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Avaliador de leads com LangGraph")
    parser.add_argument("--n", type=int, default=config.N_PADRAO, help="leads por execução")
    parser.add_argument("--auto", action="store_true", help="pula a aprovação humana")
    parser.add_argument("--nova", action="store_true", help="ignora execução pausada e começa outra")
    args = parser.parse_args()

    load_dotenv(config.RAIZ / ".env")
    faltando = [v for v in ("GOOGLE_API_KEY", "PLANILHA_LEADS", "SCORES_LEADS") if not os.getenv(v)]
    if faltando:
        print(f"Falta no arquivo .env: {', '.join(faltando)} (veja .env.example).")
        return 1

    conexao = sqlite3.connect(config.CHECKPOINTS, check_same_thread=False)
    grafo = criar_grafo(*criar_modelos(), auto=args.auto, checkpointer=SqliteSaver(conexao))

    # Retoma a última execução se ela não chegou ao Fim.
    thread = config.ULTIMA_EXECUCAO.read_text().strip() if config.ULTIMA_EXECUCAO.exists() else None
    retomar = False
    if thread and not args.nova:
        estado = grafo.get_state({"configurable": {"thread_id": thread}})
        retomar = bool(estado.next) and bool(estado.values.get("leads"))
    if not retomar:
        thread = "execucao-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        config.ULTIMA_EXECUCAO.write_text(thread)

    n = len(estado.values["leads"]) if retomar else args.n
    cfg = {"configurable": {"thread_id": thread, "n": n},
           "recursion_limit": limite_recursao(n)}

    entrada = {}
    if retomar:
        print(f"Retomando {thread}")
        pendente = interrupcao_pendente(estado)
        entrada = None
        if pendente:
            mostrar(pendente)
            decisao = perguntar(pendente)
            if decisao["acao"] == "sair":
                print("Pausado. Rode de novo para continuar.")
                return 0
            entrada = Command(resume=decisao)

    try:
        while True:
            resultado = grafo.invoke(entrada, cfg)
            if "__interrupt__" not in resultado:
                return 0
            pendente = resultado["__interrupt__"][0].value
            mostrar(pendente)
            decisao = perguntar(pendente)
            if decisao["acao"] == "sair":
                print("Pausado. Rode de novo para continuar.")
                return 0
            entrada = Command(resume=decisao)
    except Exception as erro:
        print(f"Erro: {type(erro).__name__}: {erro}")
        print("O progresso até o último passo concluído está salvo; rode de novo para retomar.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
