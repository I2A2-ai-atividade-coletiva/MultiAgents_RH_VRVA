from __future__ import annotations
from functools import lru_cache
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
FERIADOS_CSV = BASE_DIR / "dados_entrada" / "feriados.csv"


def _daterange(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


@lru_cache(maxsize=1)
def carregar_feriados() -> pd.DataFrame:
    if FERIADOS_CSV.exists():
        try:
            df = pd.read_csv(FERIADOS_CSV)
            # normaliza colunas esperadas
            cols = {c.lower(): c for c in df.columns}
            # padroniza nomes
            rename = {}
            for k in list(cols.keys()):
                if k.startswith("data"):
                    rename[cols[k]] = "data"
                elif k in ("uf",):
                    rename[cols[k]] = "uf"
                elif "muni" in k:
                    rename[cols[k]] = "municipio"
                elif "descr" in k:
                    rename[cols[k]] = "descricao"
            if rename:
                df = df.rename(columns=rename)
            # tipos
            if "data" in df.columns:
                df["data"] = pd.to_datetime(df["data"]).dt.date
            if "uf" in df.columns:
                df["uf"] = df["uf"].astype(str).str.upper()
            if "municipio" in df.columns:
                df["municipio"] = df["municipio"].astype(str).str.upper()
            return df
        except Exception:
            pass
    # vazio
    return pd.DataFrame(columns=["data", "uf", "municipio", "descricao"])  # empty


def is_feriado(d: date, uf: Optional[str], municipio: Optional[str]) -> bool:
    df = carregar_feriados()
    if df.empty:
        return False
    uf = (uf or "").upper()
    municipio = (municipio or "").upper()
    # match por data e uf; se municipio existir no csv, prioriza match exato
    sel = df[df["data"] == d]
    if sel.empty:
        return False
    # município
    if "municipio" in sel.columns and municipio:
        sel_m = sel[(sel["municipio"].fillna("") == municipio)]
        if not sel_m.empty:
            return True
    # uf
    if "uf" in sel.columns and uf:
        sel_uf = sel[(sel["uf"].fillna("") == uf)]
        if not sel_uf.empty:
            return True
    # feriado nacional (sem uf/municipio marcados)
    if ("uf" in sel.columns and sel["uf"].isna().any()) and ("municipio" in sel.columns and sel["municipio"].isna().any()):
            return True
    return False


def dias_uteis_periodo(inicio: date, fim: date, uf: Optional[str], municipio: Optional[str]) -> int:
    dias = 0
    for d in _daterange(inicio, fim):
        if d.weekday() < 5 and not is_feriado(d, uf, municipio):
            dias += 1
    return dias
