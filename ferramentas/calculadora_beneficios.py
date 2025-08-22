from datetime import date, timedelta
from typing import Dict, Any, Optional, Tuple, List

from langchain.tools import tool
import pandas as pd
import re
import json
from utils.calendario import dias_uteis_periodo
from utils.regras_resolver import resolve_cct_rules


def _daterange(start: date, end: date):
    curr = start
    while curr <= end:
        yield curr
        curr = curr + timedelta(days=1)


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
    Regra: se 'COMUNICADO DE DESLIGAMENTO' == 'OK' e a 'data de demissão' <= dia 15 do mês de referência (YYYY-MM),
    então zere a coluna 'Dias'. Se > 15, o valor de 'Dias' deve ser proporcional até a data de demissão.

    Entradas:
      - df_json: DataFrame (orient=records) com colunas incluindo 'COMUNICADO DE DESLIGAMENTO', 'Dias', 'TOTAL' e uma coluna de data de demissão.
      - mes_referencia: string no formato 'YYYY-MM'.

    Saída: df_json atualizado (orient=records).
    """
    df = pd.read_json(df_json, orient="records")

    col_flag = None
    for c in df.columns:
        if c.strip().lower() == "comunicado de desligamento":
            col_flag = c
            break
    if col_flag is None:
        # se não existir, nada a aplicar
        return df.to_json(orient="records", force_ascii=False)

    # Encontrar coluna de data de demissão por heurística
    col_demissao = _find_column(df.columns, ["demiss", "deslig"])
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
        if flag != "OK":
            return row
        if not col_demissao:
            # sem data, não conseguimos aplicar -> manter
            return row
        val = row.get(col_demissao)
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


@tool("calcular_rateio_80_20")
def calcular_rateio_80_20(df_json: str) -> str:
    """
    Cria as colunas 'Custo empresa' (80%) e 'Desconto profissional' (20%) a partir da coluna 'TOTAL'.
    Se 'TOTAL' não existir, tenta calcular como 'Dias' * 'VALOR DIÁRIO VR'.
    Retorna df_json (orient=records).
    """
    df = pd.read_json(df_json, orient="records")
    total_col = None
    for c in df.columns:
        if c.strip().upper() == "TOTAL":
            total_col = c
            break
    if total_col is None:
        # tentar derivar
        col_dias = next((c for c in df.columns if c.strip().lower() == "dias"), None)
        col_valor = next((c for c in df.columns if c.strip().lower() == "valor diário vr"), None)
        if col_dias and col_valor:
            total_col = "TOTAL"
            df[total_col] = df[col_dias].astype(float) * df[col_valor].astype(float)
        else:
            # nada a fazer
            return df.to_json(orient="records", force_ascii=False)

    df["Custo empresa"] = (df[total_col].astype(float) * 0.80).round(2)
    df["Desconto profissional"] = (df[total_col].astype(float) * 0.20).round(2)
    return df.to_json(orient="records", force_ascii=False)


@tool("executar_calculo_completo")
def executar_calculo_completo(df_json: str, mes_referencia: str) -> str:
    """
    Orquestra as regras de cálculo sobre o DataFrame:
      1) Aplicar regra de desligamento do dia 15 (zera dias <= 15 com desligamento OK; proporcional > 15)
      2) Garantir/Calcular TOTAL (Dias * VALOR DIÁRIO VR) se ausente
      3) Aplicar rateio 80/20

    Retorna df_json (orient=records) atualizado.
    """
    df = pd.read_json(df_json, orient="records")

    # 1) Regra dia 15
    df_json = aplicar_regra_desligamento_dia_15(df.to_json(orient="records", force_ascii=False), mes_referencia)
    df = pd.read_json(df_json, orient="records")

    # 2) Garantir TOTAL
    total_col = next((c for c in df.columns if c.strip().upper() == "TOTAL"), None)
    if total_col is None:
        col_dias = next((c for c in df.columns if c.strip().lower() == "dias"), None)
        col_valor = next((c for c in df.columns if c.strip().lower() == "valor diário vr"), None)
        if col_dias and col_valor:
            df["TOTAL"] = df[col_dias].astype(float) * df[col_valor].astype(float)
    # 3) Rateio 80/20
    df_json = calcular_rateio_80_20(df.to_json(orient="records", force_ascii=False))
    return df_json


def _parse_mes_ref(mes_referencia: str) -> Tuple[date, date]:
    y, m = map(int, mes_referencia.split("-"))
    inicio = date(y, m, 1)
    fim = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return inicio, fim


def _find_col(ci: pd.Index, keys: List[str]) -> Optional[str]:
    low = {c.lower(): c for c in ci}
    for k in keys:
        for lc, orig in low.items():
            if k in lc:
                return orig
    return None


def _should_exclude(row: pd.Series) -> bool:
    # Heurística de exclusões por cargo/categoria/flags
    text_fields = []
    for col in row.index:
        lc = col.lower()
        if any(x in lc for x in ["cargo", "categoria", "funcao", "função", "tipo", "perfil", "situacao", "situação", "status"]):
            val = str(row.get(col, "")).lower()
            text_fields.append(val)
    blob = " ".join(text_fields)
    excl = ["diretor", "diretoria", "estagi", "aprendiz", "exterior", "fora do brasil"]
    if any(k in blob for k in excl):
        return True
    # afastados
    if "afast" in blob or "licença" in blob or "licenca" in blob:
        return True
    return False


def _intervalo_intersec(a_ini: date, a_fim: date, b_ini: date, b_fim: date) -> Optional[Tuple[date, date]]:
    ini = max(a_ini, b_ini)
    fim = min(a_fim, b_fim)
    if ini <= fim:
        return ini, fim
    return None


def _subtrai_periodos_uteis(base_ini: date, base_fim: date, uf: Optional[str], municipio: Optional[str], periodos: List[Tuple[date, date]]) -> int:
    # Calcula dias úteis do período base menos os períodos (férias/afastamento)
    total = dias_uteis_periodo(base_ini, base_fim, uf, municipio)
    for (pi, pf) in periodos:
        inter = _intervalo_intersec(base_ini, base_fim, pi, pf)
        if inter:
            di, df = inter
            total -= dias_uteis_periodo(di, df, uf, municipio)
    return max(total, 0)


def executar_calculo_deterministico(df_json: str, mes_referencia: str) -> Tuple[str, str]:
    """
    Determinístico: calcula Dias úteis por colaborador no mês (com feriados, férias/afastamentos,
    admissões/desligamentos + regra do dia 15), aplica VR diário resolvido por sindicato/UF,
    calcula TOTAL e rateio 80/20. Retorna (df_json, validacoes_json).
    """
    df = pd.read_json(df_json, orient="records")
    validacoes: List[Dict[str, Any]] = []
    ini_mes, fim_mes = _parse_mes_ref(mes_referencia)

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

        # resolver VR diário via regras_resolver
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
            validacoes.append({"matricula": row.get(col_matricula), "msg": f"VR não encontrado para UF={uf}, Sindicato='{sind}'"})
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
