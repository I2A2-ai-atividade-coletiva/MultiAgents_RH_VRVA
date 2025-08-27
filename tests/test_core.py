import json
from datetime import date

import pandas as pd

from ferramentas.calculadora_beneficios import _parse_mes_ref
from utils.schema_map import normalize_columns
from utils.uf_mapping import infer_uf_from_sindicato
from utils.config import DIAS_FIXOS_UF, VALOR_PADRAO


def test_parse_mes_ref_basic():
    ini, fim = _parse_mes_ref("2025-05")
    assert isinstance(ini, date) and isinstance(fim, date)
    assert ini == date(2025, 5, 1)
    assert fim == date(2025, 5, 31)


def test_schema_normalize_desligamento_cols():
    df = pd.DataFrame(columns=["MATRICULA ", "DATA DEMISSÃO"])  # typical messy headers
    df2 = normalize_columns(df, base_type="deslig")
    assert "matricula" in df2.columns
    assert "data_demissao" in df2.columns


def test_infer_uf_from_sindicato_aliases():
    uf, origem = infer_uf_from_sindicato("Sindicato dos Comerciários - SP")
    assert uf == "SP"
    assert origem in {"inferida", "regex"}

    uf2, origem2 = infer_uf_from_sindicato("SIND. METALÚRGICOS RJ")
    assert uf2 == "RJ"
    assert origem2 in {"inferida", "regex"}


def test_config_dias_fixos_and_valor_padrao_present():
    # defaults defined in utils.config
    assert isinstance(DIAS_FIXOS_UF, dict)
    # presence and type
    if "SP" in DIAS_FIXOS_UF:
        assert isinstance(DIAS_FIXOS_UF["SP"], int)
    if "RJ" in DIAS_FIXOS_UF:
        assert isinstance(DIAS_FIXOS_UF["RJ"], int)

    assert isinstance(VALOR_PADRAO, dict)
