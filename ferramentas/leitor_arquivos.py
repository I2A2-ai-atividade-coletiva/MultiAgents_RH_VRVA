from pathlib import Path
from typing import Optional

import pandas as pd
from langchain.tools import tool
import json
import unicodedata
import os
import re

# Helpers de normalização para tolerar variações com acentos/caixa
def _norm_str(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()

def _norm_col(c: str) -> str:
    c = _norm_str(c).replace(" ", "_").replace("-", "_")
    c = re.sub(r"__+", "_", c)
    return c

def _find_file_by_keywords(base_dir: str, keywords: list[str]) -> Optional[str]:
    try:
        files = os.listdir(base_dir)
    except Exception:
        return None
    kw = [_norm_str(k) for k in keywords]
    for f in files:
        nf = _norm_str(f)
        if all(k in nf for k in kw):
            return os.path.join(base_dir, f)
    return None


@tool("ler_arquivo_excel")
def ler_arquivo_excel(caminho: str, sheet_name: Optional[str] = None) -> str:
    """
    Lê um arquivo Excel (.xlsx) e retorna o DataFrame como JSON (orient=records).
    Use sheet_name para planilhas específicas. Retorna uma string JSON.
    """
    path = Path(caminho)
    if not path.exists():
        # fallback: tentar localizar por palavras-chave (nome normalizado)
        base_dir = str(Path(caminho).resolve().parent)
        fname = Path(caminho).name
        keywords = re.split(r"[_\-\s]+", _norm_str(fname.replace(".xlsx", "")))
        alt = _find_file_by_keywords(base_dir, keywords)
        if not alt:
            raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")
        path = Path(alt)
    df = pd.read_excel(path, sheet_name=sheet_name)
    # Se múltiplas sheets foram retornadas (dict), padronizar para a primeira.
    if isinstance(df, dict):
        # pega a primeira sheet
        first_key = next(iter(df))
        df = df[first_key]
    # normaliza nomes de colunas para reduzir risco de divergência
    try:
        df.columns = [_norm_col(c) for c in df.columns]
    except Exception:
        pass
    return df.to_json(orient="records", force_ascii=False)


@tool("ler_arquivo_csv")
def ler_arquivo_csv(caminho: str, sep: str = ",", encoding: str = "utf-8") -> str:
    """
    Lê um arquivo CSV e retorna o DataFrame como JSON (orient=records).
    """
    path = Path(caminho)
    if not path.exists():
        # fallback de busca
        base_dir = str(Path(caminho).resolve().parent)
        fname = Path(caminho).name
        keywords = re.split(r"[_\-\s]+", _norm_str(fname.replace(".csv", "")))
        alt = _find_file_by_keywords(base_dir, keywords)
        if not alt:
            raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")
        path = Path(alt)
    df = pd.read_csv(path, sep=sep, encoding=encoding)
    try:
        df.columns = [_norm_col(c) for c in df.columns]
    except Exception:
        pass
    return df.to_json(orient="records", force_ascii=False)


@tool("normalizar_nomes_sindicatos")
def normalizar_nomes_sindicatos(df_json: str) -> str:
    """
    Padroniza os nomes dos sindicatos em um DataFrame usando o arquivo de aliases
    `automacao_rh_agentes/dados_entrada/mapa_sindicatos.json`.

    - Entrada: df_json (orient=records)
    - Saída: df_json com a coluna adicional 'sindicato_normalizado'
    """
    base_dir = Path(__file__).resolve().parent.parent
    mapa_path = base_dir / "dados_entrada" / "mapa_sindicatos.json"
    df = pd.read_json(df_json, orient="records")

    if not mapa_path.exists():
        # sem mapa, apenas replica coluna se existir
        col = None
        for c in df.columns:
            if "sindicat" in c.strip().lower():
                col = c
                break
        if col:
            df["sindicato_normalizado"] = df[col]
        return df.to_json(orient="records", force_ascii=False)

    try:
        with mapa_path.open("r", encoding="utf-8") as f:
            mapa = json.load(f)
    except Exception:
        return df.to_json(orient="records", force_ascii=False)

    # Inverte o mapa: alias (lower) -> canônico
    alias_map = {}
    for canonico, aliases in mapa.items():
        for a in aliases:
            alias_map[a.strip().lower()] = canonico

    # Identifica coluna do sindicato
    col_sind = None
    for c in df.columns:
        if "sindicat" in c.strip().lower():
            col_sind = c
            break
    if not col_sind:
        # nada a fazer
        return df.to_json(orient="records", force_ascii=False)

    def norm_name(x):
        if pd.isna(x):
            return None
        key = str(x).strip().lower()
        return alias_map.get(key, str(x))

    df["sindicato_normalizado"] = df[col_sind].apply(norm_name)
    return df.to_json(orient="records", force_ascii=False)
