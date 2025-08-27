import sqlite3
from pathlib import Path
import pandas as pd
from langchain.tools import tool
import json

# Banco de dados: preferir base_conhecimento/automacao_rh.db
BASE_DIR = Path(__file__).resolve().parent.parent
NEW_DB_PATH = BASE_DIR / "base_conhecimento" / "automacao_rh.db"
OLD_DB_PATH = BASE_DIR / "automacao_rh.db"

# Resolve DB_PATH a ser usado (preferindo o novo local)
if NEW_DB_PATH.exists():
    DB_PATH = NEW_DB_PATH
elif OLD_DB_PATH.exists():
    DB_PATH = OLD_DB_PATH
else:
    # default para o novo local
    DB_PATH = NEW_DB_PATH


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(DB_PATH))


@tool("salvar_dataframe_db")
def salvar_dataframe_db(df_json: str, nome_tabela: str) -> str:
    """
    Salva um DataFrame (JSON orient=records) em uma tabela SQLite.
    Substitui a tabela se já existir.
    """
    try:
        df = pd.read_json(df_json, orient="records")
        with _get_conn() as conn:
            df.to_sql(nome_tabela, conn, if_exists="replace", index=False)
        return f"OK: tabela '{nome_tabela}' com {len(df)} linhas salva em {DB_PATH.name}."
    except Exception as e:
        return f"ERRO ao salvar '{nome_tabela}': {e}"


@tool("carregar_dataframe_db")
def carregar_dataframe_db(nome_tabela: str) -> str:
    """
    Carrega uma tabela SQLite para JSON (orient=records).
    Retorna JSON de lista vazia [] se a tabela não existir.
    """
    try:
        with _get_conn() as conn:
            df = pd.read_sql_query(f"SELECT * FROM {nome_tabela}", conn)
        return df.to_json(orient="records", force_ascii=False)
    except Exception:
        return json.dumps([], ensure_ascii=False)


@tool("listar_tabelas_db")
def listar_tabelas_db() -> str:
    """Lista as tabelas existentes no banco SQLite, como JSON (lista de nomes)."""
    try:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
            nomes = [r[0] for r in cur.fetchall()]
        return json.dumps(nomes, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"erro": str(e)}, ensure_ascii=False)
