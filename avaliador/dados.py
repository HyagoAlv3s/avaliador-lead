"""Leitura da planilha e gravação dos resultados (código puro)."""
import json
import os
from pathlib import Path

import pandas as pd

from . import config
from .estado import Lead


def ids_processados(caminho: Path) -> set[str]:
    return {r["id"] for r in ler_resultados(caminho)}


def ler_resultados(caminho: Path) -> list[dict]:
    if not caminho.exists():
        return []
    return json.loads(caminho.read_text(encoding="utf-8"))


def gravar_resultado(caminho: Path, registro: dict) -> None:
    """Substitui o registro de mesmo id (idempotente) com escrita atômica."""
    registros = [r for r in ler_resultados(caminho) if r["id"] != registro["id"]]
    registros.append(registro)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(registros, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, caminho)


def _valor(v):
    return None if pd.isna(v) else v


def carregar_planilha(planilha: Path, scores: Path, resultados: Path, n: int) -> list[Lead]:
    for arquivo in (planilha, scores):
        if not arquivo or not arquivo.exists():
            raise ValueError(f"Arquivo não encontrado: {arquivo}")
    df = pd.read_excel(planilha, sheet_name="Leads")
    faltando = [c for c in config.COLUNAS_OBRIGATORIAS if c not in df.columns]
    if faltando:
        raise ValueError(f"Colunas ausentes na planilha: {faltando}")

    # Score é fórmula viva na planilha; o valor vem do JSON validado do pipeline.
    score_por_id = {k: v[0] for k, v in json.loads(scores.read_text(encoding="utf-8")).items()}
    df["Score"] = df["ID"].map(score_por_id)

    ja_feitos = ids_processados(resultados)
    df = df[df["Site"].notna()
            & (df["Status"] == config.STATUS_NAO_CONTATADO)
            & ~df["ID"].isin(ja_feitos)]
    df = df.sort_values("Score", ascending=False).head(n)
    if df.empty:
        raise ValueError("Nenhum lead elegível")

    return [Lead(id=str(l["ID"]), empresa=l["Empresa"], score=_valor(l["Score"]),
                 site=str(l["Site"]),
                 contexto={c: _valor(l[c]) for c in config.COLUNAS_CONTEXTO if _valor(l[c]) is not None})
            for l in df.to_dict("records")]


def cache_site(cache: Path, url: str, dominio: str) -> str:
    if not cache or not cache.exists():
        return "sem dados em cache"
    dados = json.loads(cache.read_text(encoding="utf-8"))
    for chave, info in dados.items():
        if dominio and dominio in chave:
            uteis = {k: v for k, v in info.items() if k not in ("cnpjs", "whatsapp")}
            return json.dumps(uteis, ensure_ascii=False)
    return "sem dados em cache"
