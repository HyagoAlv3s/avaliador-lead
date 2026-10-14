"""Testes com modelos falsos: rotas, estado e arquivos são determinísticos."""
import json

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from avaliador import config, ferramentas
from avaliador.dados import carregar_planilha, gravar_resultado, ler_resultados
from avaliador.estado import Veredito
from avaliador.grafo import criar_grafo, validar_mensagem

PARECER = ("Resumo da empresa: X\nComo opera hoje: Y\nOportunidades de automação: Z\n"
           "Evidências (do site ou da planilha): W\nConfiança: {conf}")


# ---------- dados de teste ----------

@pytest.fixture
def arquivos(tmp_path):
    linhas = [
        # ID, Empresa, Site, Status
        ("L1", "Alfa", "alfa.com.br", "Novo"),
        ("L2", "Beta", "beta.com.br", "Novo"),
        ("L3", "Gama", None, "Novo"),               # sem site
        ("L4", "Delta", "delta.com.br", "Contatado"),  # já contatado
        ("L5", "Epsilon", "epsilon.com.br", "Novo"),
        ("L6", "Zeta", "zeta.com.br", "Novo"),
    ]
    df = pd.DataFrame([{"ID": i, "Empresa": e, "Site": s, "Status": st,
                        **{c: "x" for c in config.COLUNAS_CONTEXTO}, "Score": None}
                       for i, e, s, st in linhas])
    planilha = tmp_path / "leads.xlsx"
    df.to_excel(planilha, sheet_name="Leads", index=False)
    scores = tmp_path / "scores.json"
    scores.write_text(json.dumps({"L1": [50, 1], "L2": [90, 1], "L3": [99, 1],
                                  "L4": [95, 1], "L5": [70, 1], "L6": [10, 1]}))
    return {"planilha": planilha, "scores": scores, "resultados": tmp_path / "resultados.json"}


def cfg(arquivos, n=5, thread="t1"):
    return {"configurable": {"thread_id": thread, "n": n, **{k: str(v) for k, v in arquivos.items()}},
            "recursion_limit": 300}


# ---------- modelos falsos ----------

class FakeAnalista:
    """Pede o site na 1ª chamada da rodada; depois devolve o parecer."""

    def __init__(self, sempre_ferramenta=False, erro_em=()):
        self.sempre_ferramenta, self.erro_em = sempre_ferramenta, erro_em

    def bind_tools(self, _):
        def responder(msgs):
            lead = msgs[1].content
            if any(e in lead for e in self.erro_em):
                raise RuntimeError("cota excedida")
            ultimo = msgs[-1]
            if "Limite de leituras" in str(ultimo.content):
                return AIMessage(content=PARECER.format(conf="média"),
                                 tool_calls=[{"name": "ler_site", "args": {"url": "x"}, "id": "z"}])
            if self.sempre_ferramenta or not isinstance(ultimo, ToolMessage):
                site = lead.split("Site: ")[1].splitlines()[0]
                n = sum(isinstance(m, ToolMessage) for m in msgs)
                return AIMessage(content="", tool_calls=[
                    {"name": "ler_site", "args": {"url": site}, "id": f"c{n}"}])
            return AIMessage(content=PARECER.format(conf="média") + f" (rodada {len(msgs)})")
        return RunnableLambda(responder)


def classificador_por_nome(negativos=()):
    def classificar(msgs):
        texto = msgs[-1].content
        positivo = not any(f"Empresa: {n}" in texto for n in negativos)
        return Veredito(cliente_provavel=positivo, justificativa="j",
                        oportunidade_principal="agendamento online" if positivo else "",
                        confianca="alta")
    return RunnableLambda(classificar)


redator_ok = RunnableLambda(lambda _: AIMessage("Olá! Vi que vocês agendam por telefone. Já pensaram em agendamento online?"))


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    monkeypatch.setattr(ferramentas, "buscar", lambda url: f"Texto do site {url}. Agende por telefone.")


# ---------- Etapa 1 ----------

def test_carregar_filtra_ordena_e_limita(arquivos):
    leads = carregar_planilha(arquivos["planilha"], arquivos["scores"], arquivos["resultados"], 3)
    assert [l["id"] for l in leads] == ["L2", "L5", "L1"]   # sem L3 (sem site) e L4 (contatado)
    assert all(l["site"] for l in leads)


def test_carregar_pula_ids_ja_processados(arquivos):
    arquivos["resultados"].write_text(json.dumps([{"id": "L2"}, {"id": "L5"}]))
    leads = carregar_planilha(arquivos["planilha"], arquivos["scores"], arquivos["resultados"], 5)
    assert [l["id"] for l in leads] == ["L1", "L6"]


def test_carregar_coluna_ausente(arquivos, tmp_path):
    df = pd.read_excel(arquivos["planilha"]).drop(columns=["Setor"])
    ruim = tmp_path / "ruim.xlsx"
    df.to_excel(ruim, sheet_name="Leads", index=False)
    with pytest.raises(ValueError, match="Setor"):
        carregar_planilha(ruim, arquivos["scores"], arquivos["resultados"], 5)


def test_carregar_sem_elegiveis(arquivos):
    arquivos["resultados"].write_text(json.dumps([{"id": i} for i in ["L1", "L2", "L5", "L6"]]))
    with pytest.raises(ValueError, match="Nenhum lead"):
        carregar_planilha(arquivos["planilha"], arquivos["scores"], arquivos["resultados"], 5)


# ---------- Etapa 7 ----------

def test_gravar_resultado_idempotente(tmp_path):
    arq = tmp_path / "r.json"
    gravar_resultado(arq, {"id": "A", "status": "pulado"})
    gravar_resultado(arq, {"id": "A", "status": "aprovado"})
    assert ler_resultados(arq) == [{"id": "A", "status": "aprovado"}]


# ---------- Etapa 3: ferramenta ----------

def test_ler_site_recusa_outro_dominio():
    estado = {"lead_atual": {"site": "alfa.com.br"}}
    assert ler_site_func("https://malicioso.com", estado).startswith("Recusado")
    assert "Texto do site" in ler_site_func("https://www.alfa.com.br/servicos", estado)


def test_ler_site_cai_no_cache(monkeypatch):
    def falha(url):
        raise ConnectionError("fora do ar")
    monkeypatch.setattr(ferramentas, "buscar", falha)
    texto = ler_site_func("alfa.com.br", {"lead_atual": {"site": "alfa.com.br"}})
    assert "cache" in texto


def ler_site_func(url, estado):
    return ferramentas.ler_site.func(url, estado)


# ---------- Etapa 6 ----------

def test_validar_mensagem():
    assert validar_mensagem("Olá, tudo bem?") is None
    assert "placeholder" in validar_mensagem("Olá [Nome]")
    assert "máximo" in validar_mensagem("a" * 601)


# ---------- grafo inteiro, modo automático ----------

def test_fluxo_auto_status_e_resumo(arquivos, capsys):
    grafo = criar_grafo(FakeAnalista(erro_em=("Epsilon",)), classificador_por_nome(negativos=("Alfa",)),
                        redator_ok, auto=True, checkpointer=MemorySaver())
    final = grafo.invoke({}, cfg(arquivos))
    por_id = {r["id"]: r for r in ler_resultados(arquivos["resultados"])}
    assert por_id["L2"]["status"] == "aprovado" and por_id["L2"]["mensagem"]
    assert por_id["L1"]["status"] == "descartado" and por_id["L1"]["classificacao"]["oportunidade_principal"] == ""
    assert por_id["L5"]["status"] == "pulado" and "cota" in por_id["L5"]["motivo"]
    assert len(final["resultados"]) == 4
    saida = capsys.readouterr().out
    assert "Execução concluída: 4 leads" in saida
    assert "--- Mensagem para Beta ---" in saida              # mostrada ao gerar
    assert "Mensagens para enviar:" in saida and "### Beta" in saida and "### Alfa" not in saida
    # parecer cita o site lido pela ferramenta (passou pelo laço agente ⇄ ferramenta)
    assert "Resumo da empresa" in por_id["L2"]["parecer"]


def test_segunda_execucao_pega_os_seguintes(arquivos):
    grafo = criar_grafo(FakeAnalista(), classificador_por_nome(), redator_ok, auto=True, checkpointer=MemorySaver())
    grafo.invoke({}, cfg(arquivos, n=2, thread="a"))
    grafo.invoke({}, cfg(arquivos, n=2, thread="b"))
    assert [r["id"] for r in ler_resultados(arquivos["resultados"])] == ["L2", "L5", "L1", "L6"]


def test_limite_de_chamadas_de_ferramenta(arquivos):
    grafo = criar_grafo(FakeAnalista(sempre_ferramenta=True), classificador_por_nome(), redator_ok,
                        auto=True, checkpointer=MemorySaver())
    c = cfg(arquivos, n=1)
    grafo.invoke({}, c)
    historico = [s for s in grafo.get_state_history(c)]
    max_tools = max(sum(isinstance(m, ToolMessage) for m in s.values.get("mensagens", [])) for s in historico)
    assert max_tools == config.MAX_CHAMADAS_FERRAMENTA
    assert ler_resultados(arquivos["resultados"])[0]["status"] == "aprovado"


def test_campos_por_lead_resetam(arquivos):
    grafo = criar_grafo(FakeAnalista(), classificador_por_nome(), redator_ok, auto=True, checkpointer=MemorySaver())
    c = cfg(arquivos, n=2)
    grafo.invoke({}, c)
    for s in grafo.get_state_history(c):
        if s.next == ("agente_analista",) and s.values.get("indice_atual") == 1 and not s.values["mensagens"]:
            assert s.values["mensagem"] is None and s.values["classificacao"] is None
            return
    pytest.fail("estado após selecionar o 2º lead não encontrado")


# ---------- Etapa 5: humano no fluxo ----------

def novo_grafo_humano(negativos=()):
    return criar_grafo(FakeAnalista(), classificador_por_nome(negativos), redator_ok,
                       auto=False, checkpointer=MemorySaver())


def test_aprovar_rejeitar(arquivos):
    grafo = novo_grafo_humano(negativos=("Alfa",))
    c = cfg(arquivos, n=3)
    r = grafo.invoke({}, c)
    assert r["__interrupt__"][0].value["empresa"] == "Beta"
    r = grafo.invoke(Command(resume={"acao": "aprovar"}), c)
    assert r["__interrupt__"][0].value["empresa"] == "Epsilon"   # Alfa (negativo) não pausa
    r = grafo.invoke(Command(resume={"acao": "rejeitar", "motivo": "longe"}), c)
    assert "__interrupt__" not in r
    por_id = {x["id"]: x for x in ler_resultados(arquivos["resultados"])}
    assert por_id["L2"]["status"] == "aprovado"
    assert por_id["L5"]["status"] == "rejeitado" and por_id["L5"]["motivo"] == "longe"
    assert por_id["L1"]["status"] == "descartado"


def test_feedback_reanalisa_e_respeita_limite(arquivos):
    grafo = novo_grafo_humano()
    c = cfg(arquivos, n=1)
    p1 = grafo.invoke({}, c)["__interrupt__"][0].value
    p2 = grafo.invoke(Command(resume={"acao": "feedback", "texto": "olhe o delivery"}), c)["__interrupt__"][0].value
    assert p2["revisoes"] == 1 and p2["parecer"] != p1["parecer"]
    msgs = grafo.get_state(c).values["mensagens"]
    assert any("olhe o delivery" in m.content for m in msgs)
    # leu o site de novo na 2ª rodada (limite é por rodada)
    assert sum(isinstance(m, ToolMessage) for m in msgs) == 2
    p3 = grafo.invoke(Command(resume={"acao": "feedback", "texto": "mais um"}), c)["__interrupt__"][0].value
    assert p3["revisoes"] == 2 and p3["pode_revisar"] is False
    grafo.invoke(Command(resume={"acao": "aprovar"}), c)
    reg = ler_resultados(arquivos["resultados"])[0]
    assert reg["status"] == "aprovado" and reg["revisoes"] == 2


def test_retomada_com_novo_processo(arquivos, tmp_path):
    """Simula fechar o terminal: novo grafo, mesmo SQLite, mesmo thread_id."""
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = tmp_path / "ck.db"
    c = cfg(arquivos, n=2)
    g1 = criar_grafo(FakeAnalista(), classificador_por_nome(), redator_ok,
                     checkpointer=SqliteSaver(sqlite3.connect(db, check_same_thread=False)))
    assert g1.invoke({}, c)["__interrupt__"][0].value["empresa"] == "Beta"
    del g1

    g2 = criar_grafo(FakeAnalista(), classificador_por_nome(), redator_ok,
                     checkpointer=SqliteSaver(sqlite3.connect(db, check_same_thread=False)))
    estado = g2.get_state(c)
    assert estado.next == ("aprovacao_humana",)
    r = g2.invoke(Command(resume={"acao": "aprovar"}), c)
    assert r["__interrupt__"][0].value["empresa"] == "Epsilon"


def test_parecer_ignora_rodada_anterior():
    from langchain_core.messages import HumanMessage
    from avaliador.grafo import parecer
    antigo = AIMessage(content="parecer antigo")
    estado = {"mensagens": [antigo, HumanMessage("Feedback do revisor: x"),
                            AIMessage(content="", tool_calls=[{"name": "ler_site", "args": {}, "id": "a"}])]}
    assert parecer(estado) == ""
    estado["mensagens"].append(AIMessage(content="parecer novo"))
    assert parecer(estado) == "parecer novo"
