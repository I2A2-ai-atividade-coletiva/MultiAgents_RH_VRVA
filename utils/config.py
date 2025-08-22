from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq


# Carrega variáveis de ambiente do arquivo .env se existir
load_dotenv()


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(name, default)


def get_llm():
    provider = (get_env("LLM_PROVIDER", "google") or "google").lower()
    temperature = float(get_env("GENAI_TEMPERATURE", "0.2"))
    if provider == "groq":
        # Default para um modelo suportado e estável na Groq
        model = get_env("GROQ_MODEL", "llama-3.1-8b-instant")
        # langchain_groq usa GROQ_API_KEY do ambiente
        return ChatGroq(model=model, temperature=temperature)
    else:
        model = get_env("GENAI_MODEL", "gemini-1.5-pro")
        # A lib usa GOOGLE_API_KEY do ambiente
        return ChatGoogleGenerativeAI(model=model, temperature=temperature)
