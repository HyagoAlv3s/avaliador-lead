from typing import Annotated
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from . import config
from .dados import cache_site


def normalizar_url(url: str) -> str:
    url = url.strip()
    return url if "://" in url else "https://" + url


def dominio(url: str) -> str:
    host = urlparse(normalizar_url(url)).hostname or ""
    return host.removeprefix("www.").lower()


def extrair_texto(html: str) -> str:
    sopa = BeautifulSoup(html, "html.parser")
    for tag in sopa(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return sopa.get_text(" ", strip=True)


def buscar(url: str) -> str:
    cabecalho = {"User-Agent": "Mozilla/5.0 (avaliador-lead; projeto de estudo)"}
    r = httpx.get(normalizar_url(url), timeout=config.TIMEOUT_SITE,
                  follow_redirects=True, headers=cabecalho)
    r.raise_for_status()
    return extrair_texto(r.text)


@tool
def ler_site(url: str, estado: Annotated[dict, InjectedState]) -> str:
    """Lê o texto visível de uma página do site do lead atual. Só aceita o domínio do lead."""
    site_lead = estado["lead_atual"]["site"]
    if dominio(url) != dominio(site_lead):
        return f"Recusado: só é permitido o site do lead atual ({dominio(site_lead)})."
    try:
        texto = buscar(url)
        if not texto:
            raise ValueError("página sem texto")
        return texto[: config.MAX_TEXTO_SITE]
    except Exception as erro:
        cache = cache_site(config.CACHE_SITES, url, dominio(site_lead))
        return f"Site indisponível ao vivo ({type(erro).__name__}). Dados do cache (indicadores, não texto): {cache}"
