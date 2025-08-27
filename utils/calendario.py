from __future__ import annotations
from functools import lru_cache
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
import re

try:
    import requests  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    requests = None  # runtime fallback: no web fetch
    BeautifulSoup = None

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


# --------- Integração com feriados.com.br (federal/estaduais) ---------

UF_LIST = {
    "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN","RS","RO","RR","SC","SP","SE","TO"
}

# Slugs conforme calendario2018brasil.com.br (ex.: SP -> sao-paulo)
UF_SLUG = {
    "AC": "acre",
    "AL": "alagoas",
    "AP": "amapa",
    "AM": "amazonas",
    "BA": "bahia",
    "CE": "ceara",
    "DF": "distrito-federal",
    "ES": "espirito-santo",
    "GO": "goias",
    "MA": "maranhao",
    "MT": "mato-grosso",
    "MS": "mato-grosso-do-sul",
    "MG": "minas-gerais",
    "PA": "para",
    "PB": "paraiba",
    "PR": "parana",
    "PE": "pernambuco",
    "PI": "piaui",
    "RJ": "rio-de-janeiro",
    "RN": "rio-grande-do-norte",
    "RS": "rio-grande-do-sul",
    "RO": "rondonia",
    "RR": "roraima",
    "SC": "santa-catarina",
    "SP": "sao-paulo",
    "SE": "sergipe",
    "TO": "tocantins",
}

def _fetch_feriados_web(ano: int, uf: Optional[str] = None) -> pd.DataFrame:
    """
    Busca feriados no site feriados.com.br.
    - Quando uf=None: feriados nacionais do ano.
    - Quando uf=UF: feriados do estado (UF) do ano.
    Parsing por heurística (datas DD/MM/YYYY + título da linha).
    """
    if requests is None or BeautifulSoup is None:
        return pd.DataFrame(columns=["data","uf","municipio","descricao"])
    try:
        if uf:
            uf_u = uf.upper()
            # página de feriados estaduais do ano/UF (heurístico)
            url = f"https://www.feriados.com.br/feriados-{uf_u}.php?ano={ano}"
        else:
            url = f"https://www.feriados.com.br/feriados-nacionais-{ano}.php"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; RH-Automation/1.0)"}
        resp = requests.get(url, timeout=20, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        texts = soup.get_text("\n")
        # encontra linhas com datas e alguma descrição
        rows = []
        for line in texts.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", line)
            if not m:
                continue
            dd, mm, yyyy = m.groups()
            try:
                d = pd.to_datetime(f"{yyyy}-{mm}-{dd}").date()
            except Exception:
                continue
            # descrição = linha sem a data
            descricao = line
            # remove a data da descrição
            descricao = re.sub(r"\b\d{2}/\d{2}/\d{4}\b", "", descricao).strip(" -:\t")
            rows.append({
                "data": d,
                "uf": uf.upper() if uf else None,
                "municipio": None,
                "descricao": descricao or None,
            })
        if not rows:
            return pd.DataFrame(columns=["data","uf","municipio","descricao"])
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame(columns=["data","uf","municipio","descricao"])

def _fetch_feriados_c2018(ano: int, uf: Optional[str] = None) -> pd.DataFrame:
    """
    Busca feriados no calendario2018brasil.com.br.
    - Nacional: tenta /feriados-{ano} e /feriados-nacionais-{ano}
    - Estadual: usa slug do estado e tenta /{slug}/feriados-{ano} e /feriados-{ano}-{slug}
    """
    if requests is None or BeautifulSoup is None:
        return pd.DataFrame(columns=["data","uf","municipio","descricao"])
    try:
        base = "https://calendario2018brasil.com.br"
        urls = []
        uf_u = (uf or "").strip().upper() or None
        if uf_u:
            slug = UF_SLUG.get(uf_u)
            if not slug:
                return pd.DataFrame(columns=["data","uf","municipio","descricao"])
            urls = [
                f"{base}/{slug}/feriados-{ano}",
                f"{base}/feriados-{ano}-{slug}",
            ]
        else:
            urls = [
                f"{base}/feriados-{ano}",
                f"{base}/feriados-nacionais-{ano}",
            ]

        headers = {"User-Agent": "Mozilla/5.0 (compatible; RH-Automation/1.0)"}
        rows: list[dict] = []
        for url in urls:
            try:
                resp = requests.get(url, timeout=20, headers=headers)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                texts = soup.get_text("\n")
                for line in texts.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", line)
                    if not m:
                        continue
                    dd, mm, yyyy = m.groups()
                    try:
                        d = pd.to_datetime(f"{yyyy}-{mm}-{dd}").date()
                    except Exception:
                        continue
                    descricao = re.sub(r"\b\d{2}/\d{2}/\d{4}\b", "", line).strip(" -:\t") or None
                    rows.append({
                        "data": d,
                        "uf": uf_u,
                        "municipio": None,
                        "descricao": descricao,
                    })
                if rows:
                    break
            except Exception:
                continue
        if not rows:
            return pd.DataFrame(columns=["data","uf","municipio","descricao"])
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame(columns=["data","uf","municipio","descricao"])

def _fetch_feriados_api_nacional(ano: int) -> pd.DataFrame:
    """Fallback confiável via BrasilAPI para feriados nacionais."""
    if requests is None:
        return pd.DataFrame(columns=["data","uf","municipio","descricao"])
    try:
        url = f"https://brasilapi.com.br/api/feriados/v1/{ano}"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; RH-Automation/1.0)"}
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        js = resp.json()
        rows = []
        for item in js:
            try:
                d = pd.to_datetime(item.get("date")).date()
            except Exception:
                continue
            rows.append({
                "data": d,
                "uf": None,
                "municipio": None,
                "descricao": item.get("name"),
            })
        if not rows:
            return pd.DataFrame(columns=["data","uf","municipio","descricao"])
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame(columns=["data","uf","municipio","descricao"])


def preparar_feriados_para_ano(ano: int, ufs: list[str] | None = None) -> None:
    """
    Garante que o CSV local contenha feriados nacionais e dos estados informados para o ano.
    Faz merge (sem duplicatas por data/uf/municipio) e atualiza cache.
    """
    ufs = [u.upper() for u in (ufs or []) if isinstance(u, str)]
    ufs = [u for u in ufs if u in UF_LIST]
    # carrega atual
    cur = carregar_feriados()
    # nacional
    need_write = False
    def _missing(df: pd.DataFrame, d: date, uf: Optional[str]) -> bool:
        if df.empty:
            return True
        sel = df[df["data"] == d]
        if uf:
            sel = sel[(sel["uf"].fillna("") == uf)]
        else:
            sel = sel[(sel["uf"].isna())]
        return sel.empty

    # Nacional: calendario2018brasil primeiro; depois feriados.com.br; fallback BrasilAPI
    nat = _fetch_feriados_c2018(ano, uf=None)
    if nat.empty:
        nat = _fetch_feriados_web(ano, uf=None)
    if nat.empty:
        nat = _fetch_feriados_api_nacional(ano)
    if not nat.empty:
        # filtra só o ano pedido
        nat = nat[nat["data"].apply(lambda d: d.year == ano)]
        if not nat.empty:
            cur = pd.concat([cur, nat], ignore_index=True).drop_duplicates(subset=["data","uf","municipio"], keep="first")
            need_write = True

    # Estaduais: calendario2018brasil primeiro; fallback feriados.com.br
    for uf in sorted(set(ufs)):
        est = _fetch_feriados_c2018(ano, uf=uf)
        if est.empty:
            est = _fetch_feriados_web(ano, uf=uf)
        if est.empty:
            continue
        est = est[est["data"].apply(lambda d: d.year == ano)]
        if est.empty:
            continue
        cur = pd.concat([cur, est], ignore_index=True).drop_duplicates(subset=["data","uf","municipio"], keep="first")
        need_write = True

    if need_write:
        # normaliza tipos
        out = cur.copy()
        out["data"] = pd.to_datetime(out["data"]).dt.strftime("%Y-%m-%d")
        out.to_csv(FERIADOS_CSV, index=False)
        # reset cache
        carregar_feriados.cache_clear()
