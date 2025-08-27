from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Any

import pandas as pd
from langchain.tools import tool

from utils.schema_map import normalize_columns, missing_required
from utils.uf_mapping import infer_uf_from_sindicato

BASE_KEYWORDS = {
    "ativos": ["ativo"],
    "ferias": ["ferias"],
    "afast": ["afast"],
    "deslig": ["deslig"],
    "aprend": ["aprend"],
    "estag": ["estag"],
    "exterior": ["exterior"],
    "admiss": ["admiss"],
    "base_valores": ["base", "valor", "sindicato"],
}


def _detect_base_type(name: str) -> str | None:
    n = name.lower()
    for btype, kws in BASE_KEYWORDS.items():
        if all(kw in n for kw in kws) or any(kw in n for kw in kws):
            return btype
    return None


def _read_any(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".xlsx":
        return pd.read_excel(path, engine="openpyxl")
    # unsupported
    raise ValueError(f"Formato não suportado: {path.suffix}")


def _ensure_str(s):
    try:
        return str(s).strip()
    except Exception:
        return None


def _coerce_types(df: pd.DataFrame, base_type: str) -> pd.DataFrame:
    df = df.copy()
    if "matricula" in df.columns:
        df["matricula"] = df["matricula"].apply(_ensure_str)
    # common date fields
    for c in ["admissao", "data_demissao", "inicio", "fim"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def _report_for_df(df: pd.DataFrame, base_type: str, fname: str) -> Dict[str, Any]:
    issues: List[str] = []
    missing = missing_required(df, base_type)
    if missing:
        issues.append(f"Colunas obrigatórias ausentes: {missing}")
    # duplicates
    dups = None
    if "matricula" in df.columns:
        dups = int(df["matricula"].duplicated(keep=False).sum())
        if dups > 0:
            issues.append(f"Duplicidades de matricula: {dups}")
    # invalid dates
    for c in ["admissao", "data_demissao", "inicio", "fim"]:
        if c in df.columns:
            invalid = int(df[c].isna().sum())
            if invalid > 0:
                issues.append(f"Datas inválidas/ausentes em '{c}': {invalid}")
    # UF preview (when sindicato exists)
    uf_preview = None
    if "sindicato" in df.columns:
        try:
            sample = df[["sindicato"]].head(200).copy()
            sample["uf_inferida"], sample["uf_origem"] = zip(*sample["sindicato"].map(infer_uf_from_sindicato))
            uf_counts = sample["uf_inferida"].fillna("NA").value_counts().to_dict()
            uf_preview = {str(k): int(v) for k, v in uf_counts.items()}
        except Exception:
            uf_preview = None
    return {
        "arquivo": fname,
        "base_type": base_type,
        "linhas": int(len(df)),
        "colunas": list(df.columns),
        "problemas": issues,
        "uf_preview": uf_preview,
    }


@tool("validar_bases_dados")
def validar_bases_dados(dados_dir: str) -> str:
    """
    Varre dados_entrada/ e gera um relatório de qualidade por arquivo identificado.
    Saída: JSON com lista de entradas por arquivo e um resumo agregado.
    """
    d = Path(dados_dir)
    entries: List[Dict[str, Any]] = []
    if not d.exists():
        return pd.DataFrame([]).to_json(orient="records", force_ascii=False)
    ativos_df: pd.DataFrame | None = None
    cache: Dict[str, pd.DataFrame] = {}
    for p in d.glob("*.*"):
        if p.suffix.lower() not in (".csv", ".xlsx"):
            continue
        btype = _detect_base_type(p.name)
        if not btype:
            # ignore unknowns quietly, but could be listed
            continue
        try:
            df = _read_any(p)
            df = normalize_columns(df, btype)
            df = _coerce_types(df, btype)
            cache[btype] = df
            if btype == "ativos":
                ativos_df = df
            rep = _report_for_df(df, btype, p.name)
            entries.append(rep)
        except Exception as e:
            entries.append({
                "arquivo": p.name,
                "base_type": btype,
                "linhas": 0,
                "colunas": [],
                "problemas": [f"Falha na leitura/normalização: {e}"],
            })
    # Anti-join checks vs ativos
    if isinstance(ativos_df, pd.DataFrame) and "matricula" in ativos_df.columns:
        base_keys = [k for k in cache.keys() if k != "ativos" and "matricula" in cache[k].columns]
        for bk in base_keys:
            dfb = cache[bk]
            missing_in_ativos = sorted(set(dfb["matricula"]) - set(ativos_df["matricula"]))
            if missing_in_ativos:
                entries.append({
                    "arquivo": f"(anti-join) {bk}",
                    "base_type": bk,
                    "linhas": len(missing_in_ativos),
                    "colunas": ["matricula"],
                    "problemas": [f"{len(missing_in_ativos)} matricula(s) não encontradas em ATIVOS"],
                    "uf_preview": None,
                })
    # aggregate basics
    return pd.DataFrame(entries).to_json(orient="records", force_ascii=False)
