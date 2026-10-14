import operator
from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class Lead(TypedDict):
    id: str
    empresa: str
    score: float
    site: str
    contexto: dict


class Estado(TypedDict, total=False):
    leads: list[Lead]
    indice_atual: int
    resultados: Annotated[list[dict], operator.add]
    # por lead
    lead_atual: Lead | None
    motivo_pulo: str | None
    classificacao: dict | None
    decisao_humana: dict | None
    mensagem: str | None
    revisoes: int
    mensagens: Annotated[list, add_messages]


class Veredito(BaseModel):
    cliente_provavel: bool
    justificativa: str = Field(description="1 a 3 frases, baseadas só no parecer")
    oportunidade_principal: str = Field(description="vazio se cliente_provavel=False")
    confianca: Literal["alta", "media", "baixa"]
