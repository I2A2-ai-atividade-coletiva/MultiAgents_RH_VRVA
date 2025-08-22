from datetime import date, timedelta
from typing import Dict, Any, Optional, Tuple, List

from langchain.tools import tool
import pandas as pd
import re
import json
import unicodedata
import os
import numpy as np
from pathlib import Path
from utils.calendario import dias_uteis_periodo
from utils.regras_resolver import resolve_cct_rules

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
DADOS_DIR = BASE_DIR / "dados_entrada"

# UF helpers
UF_MAP = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapa", "AM": "Amazonas", "BA": "Bahia",
    "CE": "Ceara", "DF": "Distrito Federal", "ES": "Espirito Santo", "GO": "Goias",
    "MA": "Maranhao", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais",
    "PA": "Para", "PB": "Paraiba", "PR": "Parana", "PE": "Pernambuco", "PI": "Piaui",
    "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondonia",
    "RR": "Roraima", "SC": "Santa Catarina", "SP": "Sao Paulo", "SE": "Sergipe", "TO": "Tocantins"
}
UF_SET = set(UF_MAP.keys())

def _norm_str(s: str) -> str:
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")

def _extract_uf_from_sindicato(s: str) -> Optional[str]:
    if not isinstance(s, str):
        return None
    toks = re.findall(r"\b([A-Z]{2})\b", _norm_str(s))
    for t in toks:
        if t in UF_SET:
            return t
    return None

def _daterange(start: date, end: date):
    curr = start
    while curr <= end:
        yield curr
        curr = curr + timedelta(days=1)

def _parse_mes_ref(mes_referencia: str) -> Tuple[date, date]:
    """
    Converte 'YYYY-MM' em (data_inicial, data_final) do mês.
    """
    y, m = map(int, mes_referencia.split("-"))
    ini = date(y, m, 1)
    if m == 12:
        fim = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        fim = date(y, m + 1, 1) - timedelta(days=1)
    return ini, fim

def _should_exclude(row: pd.Series) -> bool:
    """
    Heurística para excluir colaboradores que não recebem VR:
    - Diretores, estagiários, aprendizes
    - Exterior
    - Afastados (quando houver indicação)
    """
    txt_join = " ".join([str(v) for v in row.to_dict().values() if pd.notna(v)]).lower()
    # padrões comuns em possíveis colunas de cargo/categoria/observações
    if re.search(r"diretor|diretoria", txt_join):
        return True
    if re.search(r"estagi[aá]rio|estagio", txt_join):
        return True
    if re.search(r"aprendiz", txt_join):
        return True
    if re.search(r"exterior|internacional", txt_join):
        return True
    # indícios de afastamento
    for c in row.index:
        lc = c.lower()
        if "afast" in lc:
            val = row.get(c)
            if isinstance(val, (int, float)):
                if not pd.isna(val) and float(val) != 0:
                    return True
            else:
                sval = str(val).strip().lower()
                if sval and sval not in {"nan", "", "0", "nao", "não", "false", "no"}:
                    return True
    return False

def _subtrai_periodos_uteis(inicio: date, fim: date, uf: Optional[str], municipio: Optional[str], periodos: List[Tuple[date, date]]) -> int:
    """
    Calcula dias úteis no intervalo [inicio, fim] subtraindo períodos fornecidos.
    Usa `dias_uteis_periodo` quando possível (com UF/município), com fallback Mon-Fri.
    """
    def _uteis(a: date, b: date) -> int:
        if a > b:
            return 0
        try:
            return int(dias_uteis_periodo(a, b, uf=uf, municipio=municipio))
        except Exception:
            dias = 0
            for d in _daterange(a, b):
                if d.weekday() < 5:
                    dias += 1
            return dias

    total = _uteis(inicio, fim)
    for (s, e) in periodos:
        si = max(inicio, s)
        ef = min(fim, e)
        if ef >= si:
            total -= _uteis(si, ef)
    return max(0, total)

@tool("calcular_rateio_80_20")
def calcular_rateio_80_20(df_json: str) -> str:
    """
    Recebe um DataFrame em JSON (orient=records) contendo a coluna 'TOTAL'.
    Devolve o mesmo JSON com as colunas:
      - 'CUSTO EMPRESA 80%'
      - 'DESCONTO PROFISSIONAL 20%'
    Valores inexistentes de TOTAL são tratados como 0.
    """
    df = pd.read_json(df_json, orient="records")
    if "TOTAL" not in df.columns:
        df["TOTAL"] = 0.0
    df["CUSTO EMPRESA 80%"] = df["TOTAL"].fillna(0).astype(float).mul(0.80).round(2)
    df["DESCONTO PROFISSIONAL 20%"] = df["TOTAL"].fillna(0).astype(float).mul(0.20).round(2)
    return df.to_json(orient="records", force_ascii=False)

@tool("calcular_dias_uteis")
def calcular_dias_uteis(inicio: str, fim: str) -> int:
    """
    Calcula dias úteis (segunda a sexta) entre duas datas ISO (YYYY-MM-DD), inclusivas.
    Não considera feriados.
    """
    y1, m1, d1 = map(int, inicio.split("-"))
    y2, m2, d2 = map(int, fim.split("-"))
    di = date(y1, m1, d1)
    df = date(y2, m2, d2)
    dias = 0
    for d in _daterange(di, df):
        if d.weekday() < 5:  # 0-4 = seg-sex
            dias += 1
    return dias

def _find_column(ci: pd.Index, keywords: list[str]) -> Optional[str]:
    low = {c.lower(): c for c in ci}
    for k in keywords:
        for lc, orig in low.items():
            if k in lc:
                return orig
    return None

@tool("aplicar_regra_desligamento_dia_15")
def aplicar_regra_desligamento_dia_15(df_json: str, mes_referencia: str) -> str:
    """
    Regra: se comunicado de desligamento (status) contém 'OK' e a data do comunicado está no mês de referência (YYYY-MM)
    e dia <= 15, zera 'Dias'. Se > 15, 'Dias' proporcional até a data do comunicado.

    Entradas:
      - df_json: DataFrame (orient=records) com colunas incluindo status/data de comunicado (nomes flexíveis), 'Dias', 'TOTAL'.
      - mes_referencia: string no formato 'YYYY-MM'.

    Saída: df_json atualizado (orient=records).
    """
    df = pd.read_json(df_json, orient="records")
    # Detecta colunas de status/data do comunicado de forma tolerante
    col_flag = _find_column(df.columns, ["comunicado_status", "comunicado de desligamento", "comunicado"])  # status
    col_data = _find_column(df.columns, ["data_comunicado", "comunicado_data", "data de deslig", "data de demiss", "deslig", "demiss"])  # data
    if col_flag is None or col_data is None:
        # sem ambos, não aplica
        return df.to_json(orient="records", force_ascii=False)

    col_dias = None
    for c in df.columns:
        if c.strip().lower() == "dias":
            col_dias = c
            break
    if col_dias is None:
        # se não existir coluna Dias, cria
        col_dias = "Dias"
        if col_dias not in df.columns:
            df[col_dias] = 0

    # datas para proporcionalidade
    y, m = map(int, mes_referencia.split("-"))
    inicio_mes = date(y, m, 1)
    # obter fim do mês: avançar para próximo mês e voltar um dia
    if m == 12:
        fim_mes = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        fim_mes = date(y, m + 1, 1) - timedelta(days=1)

    total_uteis_mes = calcular_dias_uteis(str(inicio_mes), str(fim_mes))

    def ajustar_linha(row: pd.Series) -> pd.Series:
        flag = str(row.get(col_flag, "")).strip().upper()
        if "OK" not in flag:
            return row
        val = row.get(col_data)
        if pd.isna(val):
            return row
        try:
            d = pd.to_datetime(val).date()
        except Exception:
            return row
        # se não é do mesmo mês/ano de referência, manter
        if d.year != y or d.month != m:
            return row
        if d.day <= 15:
            row[col_dias] = 0
        else:
            dias_proporcionais = calcular_dias_uteis(str(inicio_mes), str(d))
            # limitar ao total do mês
            if total_uteis_mes > 0:
                row[col_dias] = min(int(row.get(col_dias, 0)), dias_proporcionais)
            else:
                row[col_dias] = dias_proporcionais
        return row

    df = df.apply(ajustar_linha, axis=1)
    return df.to_json(orient="records", force_ascii=False)

# ... (rest of the code remains the same)

def executar_calculo_deterministico(df_json: str, mes_referencia: str) -> Tuple[str, str]:
    """
    Determinístico: calcula Dias úteis por colaborador no mês (com feriados, férias/afastamentos,
    admissões/desligamentos + regra do dia 15), aplica VR diário resolvido por sindicato/UF,
    calcula TOTAL e rateio 80/20. Retorna (df_json, validacoes_json).
    """
    df = pd.read_json(df_json, orient="records")
    validacoes: List[Dict[str, Any]] = []
    ini_mes, fim_mes = _parse_mes_ref(mes_referencia)

    # Carregar base de valores por estado (fallback)
    vr_estado_path = DADOS_DIR / "Base sindicato x valor.xlsx"
    df_base_estado: Optional[pd.DataFrame] = None
    if vr_estado_path.exists():
        try:
            df_base_estado = pd.read_excel(vr_estado_path)
            # detectar colunas de estado e valor de forma tolerante
            c_estado = _find_col(df_base_estado.columns, ["estado", "uf", "unidade federativa"]) or "estado"
            c_valor = _find_col(df_base_estado.columns, ["valor", "vr", "vale refeicao"]) or "valor"
            # normaliza
            df_base_estado = df_base_estado.rename(columns={c_estado: "estado", c_valor: "valor"})
            df_base_estado["estado_norm"] = df_base_estado["estado"].astype(str).str.strip().str.lower()
        except Exception:
            df_base_estado = None

    # localizar colunas chave por heurística
    col_matricula = _find_col(df.columns, ["matric"]) or "Matricula"
    col_uf = _find_col(df.columns, ["uf"]) or "UF"
    col_mun = _find_col(df.columns, ["munic"]) or "Municipio"
    col_sind = _find_col(df.columns, ["sind"])
    col_adm = _find_col(df.columns, ["admiss"])
    col_dem = _find_col(df.columns, ["demiss", "deslig"])

    # prepara colunas de saída
    if "Dias" not in df.columns:
        df["Dias"] = 0
    if "VALOR DIÁRIO VR" not in df.columns:
        df["VALOR DIÁRIO VR"] = 0.0
    if "TOTAL" not in df.columns:
        df["TOTAL"] = 0.0

    # Enriquecimento: UF inferida pelo sindicato e valor de VR por estado
    if col_sind:
        df["uf_inferida"] = df[col_sind].apply(_extract_uf_from_sindicato)
        df["estado_inferido"] = df["uf_inferida"].map(UF_MAP)
        df["estado_norm"] = df["estado_inferido"].astype(str).str.strip().str.lower()
        if df_base_estado is not None and not df_base_estado.empty:
            df = df.merge(df_base_estado[["estado_norm", "valor"]], on="estado_norm", how="left")
            df = df.rename(columns={"valor": "vr_valor_dia_estado"})
        else:
            df["vr_valor_dia_estado"] = None
    else:
        df["vr_valor_dia_estado"] = None

    # iterar linhas e computar dias
    for i, row in df.iterrows():
        # exclusões
        if _should_exclude(row):
            df.at[i, "Dias"] = 0
            validacoes.append({"matricula": row.get(col_matricula), "msg": "Excluído por regra (diretor/estagiário/aprendiz/afastado/exterior)"})
            continue

        # intervalo base considerando admissão/desligamento
        base_ini, base_fim = ini_mes, fim_mes
        # admissão
        if col_adm and pd.notna(row.get(col_adm)):
            try:
                d = pd.to_datetime(row[col_adm]).date()
                if d > base_ini:
                    base_ini = d
            except Exception:
                validacoes.append({"matricula": row.get(col_matricula), "msg": "Data de admissão inválida"})
        # desligamento (+ regra dia 15 será aplicada depois ao total de dias)
        deslig_dia_15_zero = False
        if col_dem and pd.notna(row.get(col_dem)):
            try:
                d = pd.to_datetime(row[col_dem]).date()
                if d < base_fim:
                    base_fim = d
                # regra do dia 15: se comunicado OK (não temos a flag aqui), aplicamos proporcional >15, zero <=15
                if d.year == fim_mes.year and d.month == fim_mes.month and d.day <= 15:
                    deslig_dia_15_zero = True
            except Exception:
                validacoes.append({"matricula": row.get(col_matricula), "msg": "Data de demissão inválida"})

        if base_ini > base_fim:
            df.at[i, "Dias"] = 0
            continue

        uf = str(row.get(col_uf, "")).upper() if col_uf else None
        mun = str(row.get(col_mun, "")).upper() if col_mun else None

        # períodos de férias/afastamento por heurística: busca colunas pares inicio/fim
        periodos_subtrair: List[Tuple[date, date]] = []
        for col in df.columns:
            lc = col.lower()
            if any(k in lc for k in ["f[ée]rias", "afast"]):
                val = row.get(col)
                if pd.isna(val) or not str(val).strip():
                    continue
                # formatos comuns: "2025-05-10 a 2025-05-20" ou duas colunas em pares detectadas por nomes
                m = re.match(r"\s*(\d{4}-\d{2}-\d{2})\s*[aà]\s*(\d{4}-\d{2}-\d{2})\s*", str(val))
                if m:
                    try:
                        di = pd.to_datetime(m.group(1)).date()
                        df_ = pd.to_datetime(m.group(2)).date()
                        periodos_subtrair.append((di, df_))
                    except Exception:
                        pass
        # dias úteis do intervalo base menos férias/afastamento
        dias_calc = _subtrai_periodos_uteis(base_ini, base_fim, uf, mun, periodos_subtrair)
        if deslig_dia_15_zero:
            dias_calc = 0
        df.at[i, "Dias"] = dias_calc

        # resolver VR diário via regras_resolver (CCT) e fallback por estado
        sind = str(row.get(col_sind, "")).strip() if col_sind else ""
        regras = resolve_cct_rules(uf=uf or "", sindicato=sind)
        vr_valor = regras.get("vr_valor")
        periodicidade = regras.get("periodicidade")
        dias_regra = regras.get("dias")
        vr_diario: Optional[float] = None
        try:
            if vr_valor:
                v = float(str(vr_valor).replace("R$", "").replace(" ", "").replace(".", "").replace(",", "."))
                if periodicidade == "mes" and dias_regra:
                    vr_diario = round(v / max(int(dias_regra), 1), 2)
                else:
                    vr_diario = round(v, 2)
        except Exception:
            pass
        if vr_diario is None:
            ev = row.get("vr_valor_dia_estado")
            if pd.notna(ev):
                try:
                    vr_diario = float(ev)
                except Exception:
                    vr_diario = None
        if vr_diario is None:
            validacoes.append({"matricula": row.get(col_matricula), "msg": f"Sem valor de VR (UF/sindicato/CCT). Aplicado 0."})
            vr_diario = 0.0
        df.at[i, "VALOR DIÁRIO VR"] = vr_diario
        df.at[i, "TOTAL"] = round(float(dias_calc) * float(vr_diario), 2)

    # rateio 80/20
    try:
        res = json.loads(calcular_rateio_80_20(df.to_json(orient="records", force_ascii=False)))
        df = pd.DataFrame(res)
    except Exception:
        pass

    # colunas finais adicionais
    if "Competência" not in df.columns:
        df["Competência"] = mes_referencia
    if "OBS GERAL" not in df.columns:
        df["OBS GERAL"] = None

    validacoes_json = json.dumps(validacoes, ensure_ascii=False)
    return df.to_json(orient="records", force_ascii=False), validacoes_json


@tool("extrair_valores_cct")
def extrair_valores_cct(texto_cct: str, sindicato: str = "") -> str:
    """
    Extrai valores monetários de VR (Vale Refeição) e VA (Vale Alimentação) do texto de uma CCT.
    Heurística por regex, retornando JSON com chaves: valor_vr, valor_va (números ou null).
    """
    if not isinstance(texto_cct, str):
        texto = str(texto_cct)
    else:
        texto = texto_cct

    # normalizar separadores decimais e símbolos
    txt = re.sub(r"\u00a0", " ", texto)

    # padrões de valor monetário (R$ 12,34) ou (12,34) ou (12.34)
    money_pattern = r"(?:R\$\s*)?(\d{1,3}(?:[\.,]\d{3})*(?:[\.,]\d{2})|\d+(?:[\.,]\d{2})?)"

    def find_value(keywords: list[str]) -> Optional[float]:
        # varre janelas contendo as palavras-chave e proximidade de valores
        for kw in keywords:
            for m in re.finditer(kw, txt, flags=re.IGNORECASE):
                start = max(0, m.start() - 120)
                end = min(len(txt), m.end() + 120)
                janela = txt[start:end]
                for vm in re.finditer(money_pattern, janela):
                    raw = vm.group(1)
                    # normaliza: remove milhar e usa ponto decimal
                    norm = raw.replace(".", "").replace(",", ".")
                    try:
                        val = float(norm)
                        if val > 0:
                            return round(val, 2)
                    except Exception:
                        continue
        return None

    valor_vr = find_value([r"vale refei[çc][aã]o", r"aux[íi]lio refei[çc][aã]o", r"vr\b"])  # VR
    valor_va = find_value([r"vale alimenta[çc][aã]o", r"aux[íi]lio alimenta[çc][aã]o", r"va\b"])  # VA

    return json.dumps({
        "sindicato": sindicato or None,
        "valor_vr": valor_vr,
        "valor_va": valor_va,
    }, ensure_ascii=False)


def _find_file_by_keywords(base_dir: str, keywords: list[str]) -> str | None:
    files = os.listdir(base_dir)
    kw = [_norm_str(k).lower() for k in keywords]
    for f in files:
        nf = _norm_str(f).lower()
        if all(k in nf for k in kw):
            return str(Path(base_dir) / f)
    return None

def _find_col(cols, keys):
    low = {c.lower(): c for c in cols}
    for k in keys:
        for lc, orig in low.items():
            if k in lc:
                return orig
    return None

@tool("calcular_financeiro_vr")
def calcular_financeiro_vr(mes_referencia: str = "2025-05") -> str:
    """
    Consolida dados em dados_entrada/, aplica regras (exclusões, férias, afastamentos, admissão/desligamento, comunicado<=15),
    resolve valor VR (CCT/sindicato/estado) e gera planilha final em relatorios_saida/VR_MENSAL_XX_YYYY_CALC.xlsx.
    Retorna um JSON com {"saida_xlsx": <path>, "linhas": N, "total_empresa": ..., "total_profissional": ...}
    """
    base = Path(__file__).resolve().parent.parent
    dados = base / "dados_entrada"
    saida_dir = base / "relatorios_saida"
    saida_dir.mkdir(parents=True, exist_ok=True)

    # localizar arquivos
    f_ativos    = _find_file_by_keywords(str(dados), ["ativos"])
    f_ferias    = _find_file_by_keywords(str(dados), ["ferias"])
    f_afast     = _find_file_by_keywords(str(dados), ["afast"])
    f_deslig    = _find_file_by_keywords(str(dados), ["deslig"])
    f_aprendiz  = _find_file_by_keywords(str(dados), ["aprend"])
    f_estagio   = _find_file_by_keywords(str(dados), ["estag"])
    f_exterior  = _find_file_by_keywords(str(dados), ["exterior"])
    f_dias      = _find_file_by_keywords(str(dados), ["base","dias","uteis"])
    f_valor     = _find_file_by_keywords(str(dados), ["base","sindicato","valor"])
    f_adm       = _find_file_by_keywords(str(dados), ["admiss"])

    def _read_xlsx(p):
        if not p:
            return pd.DataFrame()
        path = str(p)
        suf = Path(path).suffix.lower()
        try:
            if suf == ".csv":
                df = pd.read_csv(path)
            elif suf == ".xlsx":
                df = pd.read_excel(path, engine="openpyxl")
            elif suf == ".xls":
                # Não suportamos .xls por padrão (xlrd não está no requirements). Solicitar conversão para .xlsx.
                raise ValueError(f"Arquivo .xls não suportado: {path}. Converta para .xlsx.")
            else:
                # fallback tenta como excel
                df = pd.read_excel(path, engine="openpyxl")
        except Exception as e:
            raise RuntimeError(f"Falha ao ler arquivo '{path}': {e}")
        df.columns = [_norm_str(c).strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]
        return df

    ativos   = _read_xlsx(f_ativos)
    ferias   = _read_xlsx(f_ferias)
    afast    = _read_xlsx(f_afast)
    deslig   = _read_xlsx(f_deslig)
    aprendiz = _read_xlsx(f_aprendiz)
    estagio  = _read_xlsx(f_estagio)
    exterior = _read_xlsx(f_exterior)
    diasut   = _read_xlsx(f_dias)
    vr_est   = _read_xlsx(f_valor)
    admis    = _read_xlsx(f_adm)

    # ids
    def _idcol(df):
        return df.columns[0] if df is not None and len(df.columns) > 0 else None

    id_ativos = _idcol(ativos)
    if id_ativos is None or ativos.empty:
        return json.dumps({"erro":"Base ATIVOS não encontrada ou vazia"})

    # competência
    y, m = map(int, mes_referencia.split("-"))
    ini_mes = date(y, m, 1)
    fim_mes = (date(y + (m//12), ((m%12)+1), 1) - timedelta(days=1))

    # mapas auxiliares
    def _intervalos(df, start_hints, end_hints, id_col):
        out = {}
        if df is None or df.empty or not id_col: return out
        sc = _find_col(df.columns, start_hints)
        ec = _find_col(df.columns, end_hints)
        if not sc or not ec: return out
        for _, r in df.iterrows():
            try:
                s = pd.to_datetime(r[sc]).date()
                e = pd.to_datetime(r[ec]).date()
            except Exception:
                continue
            s = max(s, ini_mes); e = min(e, fim_mes)
            if e >= s:
                out.setdefault(str(r[id_col]), []).append((s, e))
        return out
    fer_int = _intervalos(ferias, ["inicio","inicio_ferias","data_inicio"], ["fim","fim_ferias","data_fim"], _idcol(ferias) if not ferias.empty else None)
    afa_int = _intervalos(afast,  ["inicio","data_inicio"], ["fim","data_fim"], _idcol(afast) if not afast.empty else None)

    # admissão
    adm_col = _find_col(admis.columns if admis is not None else [], ["data_admissao","admissao"]) 
    adm_map = {}
    if adm_col:
        for _, r in admis.iterrows():
            try:
                adm_map[str(r[_idcol(admis)])] = pd.to_datetime(r[adm_col]).date()
            except Exception:
                pass

    # desligamento - prioriza colunas de DATA específicas para evitar confundir com 'comunicado_de_desligamento'
    def _pick_col_exact(df, candidates_contains: list[str]) -> str | None:
        if df is None or df.empty:
            return None
        cols = [c for c in df.columns]
        for c in cols:
            lc = str(c).lower()
            if any(k in lc for k in candidates_contains):
                return c
        return None

    dcol = _pick_col_exact(deslig, ["data_demissao", "data_desligamento", "demissao"])  # data de demissão
    # status do comunicado (texto tipo 'OK')
    scol = None
    if not deslig.empty:
        for c in deslig.columns:
            lc = str(c).lower()
            if "comunicado" in lc and ("status" in lc or "desligamento" in lc):
                scol = c
                break
    # data do comunicado
    ccol = _pick_col_exact(deslig, ["data_comunicado", "comunicado_data"])
    dmap = {}
    for _, r in deslig.iterrows():
        rid = str(r[_idcol(deslig)])
        dd = pd.to_datetime(r[dcol]).date() if dcol and pd.notna(r.get(dcol)) else None
        st = str(r.get(scol,"")).strip().upper() if scol else None
        dc = pd.to_datetime(r[ccol]).date() if ccol and pd.notna(r.get(ccol)) else None
        dmap[rid] = {"deslig": dd, "status": st, "com_data": dc}

    # work base
    nome_col = _find_col(ativos.columns, ["nome","colaborador","funcionario"])
    sind_col = _find_col(ativos.columns, ["sindicato","sind"])
    work = ativos[[id_ativos] + ([nome_col] if nome_col else []) + ([sind_col] if sind_col else [])].copy()
    work.columns = ["matricula"] + (["nome"] if nome_col else []) + (["sindicato"] if sind_col else [])
    work["matricula"] = work["matricula"].astype(str)

    # exclusões
    ids_ap = set(map(str, (aprendiz[_idcol(aprendiz)] if not aprendiz.empty else [])))
    ids_es = set(map(str, (estagio[_idcol(estagio)] if not estagio.empty else [])))
    ids_ex = set(map(str, (exterior[_idcol(exterior)] if not exterior.empty else [])))
    work = work[~work["matricula"].isin(ids_ap | ids_es | ids_ex)].copy()

    # dias uteis base
    def _map_du(df, val_hints):
        out = {}
        if df is None or df.empty: return out
        idc = df.columns[0]
        cm = _find_col(df.columns, val_hints)
        if not cm: return out
        for _, r in df.iterrows():
            try:
                out[str(r[idc])] = int(float(r[cm]))
            except Exception:
                pass
        return out

    du_colab = _map_du(diasut, ["dias_uteis_mes_colaborador","dias_uteis_colaborador","dias_uteis"])
    du_sind  = _map_du(diasut, ["dias_uteis_sindicato_mes","dias_uteis_sindicato","dias_sindicato","dias_uteis_mes"])

    # fallback Mon-Fri do mês
    month_business_days = len(pd.bdate_range(ini_mes, fim_mes))

    # valor por estado
    if not vr_est.empty:
        ev_col = _find_col(vr_est.columns, ["estado","uf","unidade_federativa"]) or "estado"
        vv_col = _find_col(vr_est.columns, ["valor","vr","vale_refeicao"]) or "valor"
        vr_est["estado_norm"] = vr_est[ev_col].astype(str).str.strip().str.lower()
    else:
        vr_est = pd.DataFrame(columns=["estado_norm","valor"])

    records = []
    for _, r in work.iterrows():
        mid = r["matricula"]
        sind = r.get("sindicato","NA")
        # janela
        w_start = ini_mes
        w_end   = fim_mes
        if mid in adm_map and adm_map[mid] and adm_map[mid] > w_start:
            w_start = adm_map[mid]
        if mid in dmap and dmap[mid]["deslig"] and dmap[mid]["deslig"] < w_end:
            w_end = dmap[mid]["deslig"]

        # comunicado <=15
        zerar = False
        info = dmap.get(mid)
        if info and info.get("status") and "OK" in info["status"]:
            dc = info.get("com_data")
            if dc and dc.year == y and dc.month == m and dc.day <= 15:
                zerar = True

        if w_end < w_start or zerar:
            dias_pagos = 0
        else:
            dias_trab = len(pd.bdate_range(w_start, w_end))
            # descontar ferias
            df_fer = 0
            if mid in fer_int:
                for (s,e) in fer_int[mid]:
                    s2 = max(w_start, s); e2 = min(w_end, e)
                    if e2 >= s2: df_fer += len(pd.bdate_range(s2, e2))
            # afast
            df_af = 0
            if mid in afa_int:
                for (s,e) in afa_int[mid]:
                    s2 = max(w_start, s); e2 = min(w_end, e)
                    if e2 >= s2: df_af += len(pd.bdate_range(s2, e2))
            dias_liq = max(0, dias_trab - df_fer - df_af)
            dias_mes_sind = du_sind.get(mid, month_business_days)
            dias_base_col = du_colab.get(mid, dias_mes_sind)
            prop = dias_liq / month_business_days if month_business_days>0 else 0
            dias_pagos = int(round(prop * dias_mes_sind))
            dias_pagos = max(0, min(dias_pagos, dias_base_col, dias_mes_sind))

        # valor por estado
        uf = _extract_uf_from_sindicato(sind) if isinstance(sind, str) else None
        est = UF_MAP.get(uf) if uf else None
        estado_norm = str(est).strip().lower() if est else None
        valor_dia = np.nan
        origem = "NA"
        if estado_norm and not vr_est.empty:
            rowm = vr_est[vr_est["estado_norm"] == estado_norm]
            if not rowm.empty:
                valor_dia = float(rowm.iloc[0][vv_col])
                origem = "ESTADO"

        total = 0.0 if np.isnan(valor_dia) else round(dias_pagos * valor_dia, 2)
        empresa = round(total * 0.80, 2)
        prof = round(total * 0.20, 2)
        obs = []
        if zerar: obs.append("COMUNICADO<=15")
        records.append([mid, r.get("nome",""), sind, uf, mes_referencia, None if np.isnan(valor_dia) else valor_dia, dias_pagos, total, empresa, prof, ";".join(obs) if obs else "OK", origem])

    cols = [
        "matricula","nome","sindicato","uf_inferida","ano_mes",
        "vr_valor_dia_aplicado","dias_vr_pagos","vr_total_colaborador",
        "custo_empresa_80","desconto_profissional_20","observacoes","origem_valor"
    ]
    df_out = pd.DataFrame(records, columns=cols)

    # Preparar exportação com colunas renomeadas e formatadas
    try:
        # Matricula numérica
        df_out["matricula_num"] = pd.to_numeric(df_out["matricula"], errors="coerce")
        # Admissão formatada dd/mm/aaaa a partir do mapa de admissão original (se existir)
        def _fmt_date(d):
            try:
                return d.strftime("%d/%m/%Y") if d is not None else ""
            except Exception:
                return ""
        df_out["admissao_fmt"] = df_out["matricula"].map(lambda x: adm_map.get(x) if x in adm_map else None).apply(_fmt_date)
        # Competência mm/aaaa
        df_out["competencia_fmt"] = f"{m:02d}/{y}"
        # Seleção e renomeação
        export_cols = [
            ("matricula_num", "Matricula"),
            ("admissao_fmt", "Admissão"),
            ("sindicato", "Sindicato do Colaborador"),
            ("competencia_fmt", "Competência"),
            ("dias_vr_pagos", "Dias"),
            ("vr_valor_dia_aplicado", "VALOR DIÁRIO VR"),
            ("vr_total_colaborador", "TOTAL"),
            ("custo_empresa_80", "Custo empresa"),
            ("desconto_profissional_20", "Desconto profissional"),
        ]
        df_exp = df_out[[c for c, _ in export_cols]].copy()
        df_exp.columns = [n for _, n in export_cols]
    except Exception:
        # fallback: mantém layout antigo se algo falhar
        df_exp = df_out.copy()

    out_path = str(saida_dir / f"VR_MENSAL_{m:02d}_{y}_CALC.xlsx")
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        df_exp.to_excel(w, sheet_name="VR_MENSAL", index=False)

    # Sanity checks e export de erros
    try:
        assert float(df_out["vr_total_colaborador"].sum()) > 0, "Total geral deu 0 — verifique merge de valor (estado/sindicato) e comunicado<=15."
    except AssertionError as e:
        print(str(e))

    print("Origem de valor (amostragem):")
    origem_counts = {}
    zerados_por_comunicado = 0
    sem_valor_count = 0
    try:
        vc = df_out["origem_valor"].value_counts(dropna=False)
        # normaliza chaves, tratando NaN como 'NA'
        origem_counts = { ("NA" if (k!=k) else str(k)): int(v) for k, v in vc.to_dict().items() }
        print(vc.head())
    except Exception:
        pass
    try:
        zerados_por_comunicado = int((df_out["observacoes"].str.contains("COMUNICADO<=15", na=False)).sum())
        print("Zerados por comunicado<=15:", zerados_por_comunicado)
    except Exception:
        pass
    try:
        sem_valor_count = int((df_out["vr_valor_dia_aplicado"].isna()).sum())
        print("Sem valor sindicato/estado:", sem_valor_count)
    except Exception:
        pass

    # Gera arquivo de casos de erro (sem valor aplicado)
    err_path = None
    try:
        errs = df_out[df_out["vr_valor_dia_aplicado"].isna()].copy()
        if not errs.empty:
            def _motivo(row):
                if not row.get("uf_inferida"):
                    return "UF não reconhecida no sindicato"
                return "Estado sem valor na planilha Base sindicato x valor.xlsx"
            errs["motivo_erro"] = errs.apply(_motivo, axis=1)
            err_path = str(saida_dir / f"VR_MENSAL_{m:02d}_{y}_ERROS.csv")
            errs[["matricula","nome","sindicato","uf_inferida","ano_mes","motivo_erro"]].to_csv(err_path, index=False, encoding="utf-8")
    except Exception:
        pass

    return json.dumps({
        "saida_xlsx": out_path,
        "linhas": len(df_out),
        "total_empresa": float(df_out["custo_empresa_80"].sum()),
        "total_profissional": float(df_out["desconto_profissional_20"].sum()),
        "total_geral": float(df_out["vr_total_colaborador"].sum()),
        # Metricas de validação para UI
        "origem_valor_counts": origem_counts,
        "zerados_por_comunicado": zerados_por_comunicado,
        "sem_valor_count": sem_valor_count,
        "erros_csv": err_path,
    })
