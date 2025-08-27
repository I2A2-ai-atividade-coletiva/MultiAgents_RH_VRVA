from __future__ import annotations

from typing import Dict, List
import pandas as pd
import re
import unicodedata

# Central column normalization utilities and mappings per base type
# base_type keys suggested: 'ativos','ferias','afast','deslig','aprend','estag','exterior','admiss','base_valores'


def _norm_str(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


def _norm_col(c: str) -> str:
    c = _norm_str(c)
    c = c.replace(" ", "_").replace("-", "_")
    c = re.sub(r"__+", "_", c)
    return c


# Variants to canonical per base_type
COLUMN_MAP: Dict[str, Dict[str, str]] = {
    "common": {
        "matricula": "matricula",
        "matricula_": "matricula",
        "id_colaborador": "matricula",
        "nome": "nome",
        "funcionario": "nome",
        "colaborador": "nome",
        "cpf": "cpf",
    },
    "ativos": {
        "data_admissao": "admissao",
        "admissao": "admissao",
        "admissao_data": "admissao",
        "sindicato": "sindicato",
        "sindicato_do_colaborador": "sindicato",
    },
    "deslig": {
        "data_demissao": "data_demissao",
        "data_demissao_": "data_demissao",
        "demissao": "data_demissao",
    },
    "ferias": {
        "inicio_ferias": "inicio",
        "fim_ferias": "fim",
        "data_inicio": "inicio",
        "data_fim": "fim",
    },
    "afast": {
        "inicio_afast": "inicio",
        "fim_afast": "fim",
        "data_inicio": "inicio",
        "data_fim": "fim",
    },
    "aprend": {},
    "estag": {},
    "exterior": {},
    "admiss": {
        "admissao": "admissao",
        "data_admissao": "admissao",
    },
    "base_valores": {
        "uf": "uf",
        "estado": "uf",
        "valor_vr": "valor_vr",
        "valor_va": "valor_va",
        "sindicato": "sindicato",
    },
}

# Required columns per base_type (post-normalization)
REQUIRED_COLS: Dict[str, List[str]] = {
    "ativos": ["matricula", "nome"],
    "deslig": ["matricula", "data_demissao"],
    "ferias": ["matricula", "inicio", "fim"],
    "afast": ["matricula", "inicio", "fim"],
    "admiss": ["matricula", "admissao"],
}


def normalize_columns(df: pd.DataFrame, base_type: str | None = None) -> pd.DataFrame:
    # coarse normalization
    try:
        df = df.copy()
        df.columns = [_norm_col(c) for c in df.columns]
    except Exception:
        return df
    # mapping
    mapping = dict(COLUMN_MAP.get("common", {}))
    if base_type:
        mapping.update(COLUMN_MAP.get(base_type, {}))
    new_cols = []
    for c in df.columns:
        new_cols.append(mapping.get(c, c))
    df.columns = new_cols
    return df


def missing_required(df: pd.DataFrame, base_type: str) -> List[str]:
    req = REQUIRED_COLS.get(base_type, [])
    return [c for c in req if c not in df.columns]
