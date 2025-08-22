from pathlib import Path

import pandas as pd
from langchain.tools import tool


def salvar_planilha_final(
    df_json: str,
    caminho_saida: str,
    nome_aba_principal: str = "VR Mensal 05.2025",
    validacoes_json: str | None = None,
) -> str:
    """
    Salva um Excel com apenas a aba principal, com nome exato (default: "VR Mensal 05.2025") e
    colunas ordenadas conforme o modelo:
      [Matricula, Admissão, Sindicato do Colaborador, Competência, Dias, VALOR DIÁRIO VR, TOTAL,
       Custo empresa, Desconto profissional, OBS GERAL]

    Observação: validacoes_json é aceito apenas por compatibilidade e é ignorado.

    Parâmetros:
      - df_json: JSON (orient=records) do DataFrame principal.
      - caminho_saida: caminho do arquivo .xlsx a ser salvo.
      - nome_aba_principal: nome da aba principal.
      - validacoes_json: ignorado.
    Retorna o caminho do arquivo salvo.
    """
    df = pd.read_json(df_json, orient="records")

    # Ordem e renomeação conforme cabeçalho do modelo
    ordem = [
        "Matricula",
        "Admissão",
        "Sindicato do Colaborador",
        "Competência",
        "Dias",
        "VALOR DIÁRIO VR",
        "TOTAL",
        "Custo empresa",
        "Desconto profissional",
        "OBS GERAL",
    ]

    # Mapeamentos heurísticos para renomear colunas se necessário
    rename_map = {}
    col_lower = {c.lower(): c for c in df.columns}
    targets = {
        "matricula": "Matricula",
        "admiss": "Admissão",
        "sindicato": "Sindicato do Colaborador",
        "compet": "Competência",
        "dias": "Dias",
        "valor diário vr": "VALOR DIÁRIO VR",
        "total": "TOTAL",
        "custo empresa": "Custo empresa",
        "desconto profissional": "Desconto profissional",
        "obs": "OBS GERAL",
    }
    for key, target in targets.items():
        # procura por coluna que contenha o prefixo
        match = next((orig for low, orig in col_lower.items() if key in low), None)
        if match and match != target:
            rename_map[match] = target
    if rename_map:
        df = df.rename(columns=rename_map)

    # Garante colunas obrigatórias e ordem
    for col in ordem:
        if col not in df.columns:
            df[col] = None
    df = df[ordem]

    out_path = Path(caminho_saida)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=nome_aba_principal)
        # Aba de validações, se fornecida
        if validacoes_json:
            try:
                df_val = pd.read_json(validacoes_json, orient="records")
            except Exception:
                # tenta interpretar como lista de strings
                try:
                    import json as _json
                    data = _json.loads(validacoes_json)
                    if isinstance(data, list):
                        df_val = pd.DataFrame({"Validações": data, "Check": None})
                    else:
                        df_val = pd.DataFrame(columns=["Validações", "Check"])
                except Exception:
                    df_val = pd.DataFrame(columns=["Validações", "Check"])
            # garante colunas
            if "Validações" not in df_val.columns:
                # pegue a primeira coluna como validações
                if len(df_val.columns) > 0:
                    df_val = df_val.rename(columns={df_val.columns[0]: "Validações"})
                else:
                    df_val["Validações"] = None
            if "Check" not in df_val.columns:
                df_val["Check"] = None
            df_val[["Validações", "Check"]].to_excel(writer, index=False, sheet_name="Validações")
    return str(out_path)


@tool("salvar_planilha_final")
def salvar_planilha_final_tool(
    df_json: str,
    caminho_saida: str,
    nome_aba_principal: str = "VR Mensal 05.2025",
    validacoes_json: str | None = None,
) -> str:
    """Wrapper ferramenta para uso via LLM."""
    return salvar_planilha_final(
        df_json=df_json,
        caminho_saida=caminho_saida,
        nome_aba_principal=nome_aba_principal,
        validacoes_json=validacoes_json,
    )
