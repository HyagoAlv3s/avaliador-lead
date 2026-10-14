import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")


def _caminho(variavel: str) -> Path | None:
    valor = os.getenv(variavel)
    return (RAIZ / valor).resolve() if valor else None


# Dados de entrada ficam fora do repositório; caminhos vêm do .env (ver .env.example).
PLANILHA = _caminho("PLANILHA_LEADS")
SCORES = _caminho("SCORES_LEADS")
CACHE_SITES = _caminho("CACHE_SITES")

RESULTADOS = RAIZ / "resultados.json"
CHECKPOINTS = RAIZ / "checkpoints.db"
ULTIMA_EXECUCAO = RAIZ / ".ultima_execucao"

N_PADRAO = 5
STATUS_NAO_CONTATADO = "Novo"
MAX_CHAMADAS_FERRAMENTA = 3
MAX_REVISOES = 2
MAX_TEXTO_SITE = 8000
MAX_MENSAGEM = 600
TIMEOUT_SITE = 10

COLUNAS_CONTEXTO = ["Setor", "Avaliações", "Nota", "Porte",
                    "Oportunidade digital", "Dor nas avaliações", "Evidências"]
COLUNAS_OBRIGATORIAS = ["ID", "Empresa", "Site", "Status"] + COLUNAS_CONTEXTO
