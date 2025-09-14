from __future__ import annotations

import os
from typing import Optional, Dict, Any
from pathlib import Path
import json

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


# ---------------------------------------------------------------------------
# Parametrização de regras de negócio (customizável por .env se desejar)
# ---------------------------------------------------------------------------

# Dias fixos por UF (ex.: SP=22, RJ=21). Se uma UF não estiver aqui, usa dias úteis do mês
DIAS_FIXOS_UF: Dict[str, int] = {
    "SP": int(get_env("DIAS_FIXOS_SP", "22")),
    "RJ": int(get_env("DIAS_FIXOS_RJ", "21")),
    "RS": int(get_env("DIAS_FIXOS_RS", "22")),
    "PR": int(get_env("DIAS_FIXOS_PR", "21")),
}

# Jornada padrão (horas/dia) — disponível para cálculos futuros que dependam de jornada
JORNADA_PADRAO_HORAS: int = int(get_env("JORNADA_PADRAO_HORAS", "8"))

# Valores padrão opcionais quando não houver CCT/estado (pode ser preenchido no .env ou código)
# Formato: { "UF": {"VR": 0.0, "VA": 0.0}, "SINDICATO_CANONICO": {"VR": 0.0, "VA": 0.0} }
VALOR_PADRAO: Dict[str, Dict[str, float]] = {}

# Raiz do projeto
BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_DIR = BASE_DIR / "base_conhecimento"
SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
COMPETENCIA_PATH = SETTINGS_DIR / "competencia.json"
RULES_SETTINGS_PATH = SETTINGS_DIR / "rules_settings.json"

def get_competencia() -> Dict[str, Any]:
    """Load persisted competence settings.

    Returns a dict with keys:
      - year: int (YYYY)
      - month: int (1..12)
      - start_day_prev: int (1..31)  # day in previous month
      - end_day_ref: int (1..31)     # day in reference month
    Defaults: current month/year from environment is not enforced; fallback is month/day defaults 1..end.
    """
    try:
        if COMPETENCIA_PATH.exists():
            data = json.loads(COMPETENCIA_PATH.read_text(encoding="utf-8"))
            # minimal validation and coercion
            y = int(data.get("year", 1900))
            m = int(data.get("month", 1))
            sd = int(data.get("start_day_prev", 1))
            ed = int(data.get("end_day_ref", 31))
            return {"year": y, "month": m, "start_day_prev": sd, "end_day_ref": ed}
    except Exception:
        pass
    # safe defaults: month/year 0 indicates 'unset'
    return {"year": None, "month": None, "start_day_prev": 1, "end_day_ref": 31}

def set_competencia(year: int, month: int, start_day_prev: int, end_day_ref: int) -> None:
    """Persist competence settings to JSON with basic validation and clamping.

    Validation is minimal here; UI must ensure valid days per month. We clamp values to [1,31].
    """
    y = int(year)
    m = max(1, min(12, int(month)))
    sd = max(1, min(31, int(start_day_prev)))
    ed = max(1, min(31, int(end_day_ref)))
    payload = {"year": y, "month": m, "start_day_prev": sd, "end_day_ref": ed}
    try:
        COMPETENCIA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # best-effort; ignore persistence errors
        pass

# ----------------------- Business rules settings ----------------------------
def _default_rules_settings() -> Dict[str, Any]:
    return {
        "include_adm_only": True,
        "default_sindicato": "",  # e.g., "RS"
        "valor_padrao_uf": {},      # e.g., {"SP": 37.5, "RJ": 35.0, "RS": 35.0, "PR": 35.0}
        "dias_fixos_uf": {},        # e.g., {"SP": 22, "RJ": 21, ...}
        "ferias_sinteticas": {
            "enable": False,
            "dias_col_candidates": ["dias", "dias_ferias", "qtd_dias"],
            "alocacao": "inicio"  # "inicio" | "centro"
        },
    }

def get_rules_settings() -> Dict[str, Any]:
    try:
        if RULES_SETTINGS_PATH.exists():
            data = json.loads(RULES_SETTINGS_PATH.read_text(encoding="utf-8"))
            # merge defaults
            default = _default_rules_settings()
            default.update({k: v for k, v in data.items() if v is not None})
            return default
    except Exception:
        pass
    return _default_rules_settings()

def set_rules_settings(payload: Dict[str, Any]) -> None:
    try:
        cur = get_rules_settings()
        # shallow merge
        if isinstance(payload, dict):
            cur.update(payload)
        RULES_SETTINGS_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
