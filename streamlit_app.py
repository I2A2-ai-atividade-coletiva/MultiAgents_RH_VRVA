import os
import subprocess
from pathlib import Path
import pandas as pd
import streamlit as st
from ferramentas.persistencia_db import DB_PATH
from utils.calendario import preparar_feriados_para_ano
import sys
import json
import time
from ferramentas.calculadora_beneficios import calcular_financeiro_vr
from io import BytesIO
from utils.regras_resolver import resolve_cct_rules
from ferramentas.calculadora_beneficios import _find_col, _should_exclude, UF_MAP, _find_file_by_keywords

BASE_DIR = Path(__file__).resolve().parent
DADOS_DIR = BASE_DIR / "dados_entrada"
CCTS_DIR = BASE_DIR / "base_conhecimento" / "ccts_pdfs"
RELATORIOS_DIR = BASE_DIR / "relatorios_saida"
PROMPTS_DIR = BASE_DIR / "prompts"
CHROMA_DIR = BASE_DIR / "base_conhecimento" / "chromadb"
RULES_INDEX_ROOT = BASE_DIR / "base_conhecimento" / "rules_index.json"

DADOS_DIR.mkdir(parents=True, exist_ok=True)
CCTS_DIR.mkdir(parents=True, exist_ok=True)
RELATORIOS_DIR.mkdir(parents=True, exist_ok=True)
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Automação RH - Multiagentes", layout="wide")

# Global font standardization
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    html, body, [class*="css"], .stApp {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, 'Noto Sans', 'Ubuntu', sans-serif !important;
    }
    h1, h2, h3, h4, h5, h6,
    .stMarkdown, .stText, .stTextInput, .stButton>button,
    .stDataFrame, .stTable, .stMetric, .stSelectbox, .stRadio, .stTabs,
    .stDownloadButton>button, .stFileUploader, .stAlert, .stCaption {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, 'Noto Sans', 'Ubuntu', sans-serif !important;
    }
    /* Optional: slightly tighten headings */
    h1 { font-weight: 700; }
    h2 { font-weight: 600; }
    h3, h4, h5, h6 { font-weight: 600; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Automação RH - Multiagentes")

with st.sidebar:
    st.header("Navegação")
    page = st.radio(
        "Páginas",
        [
            "Importar Relatórios Base",
            "Importar CCTs",
            "Regras CCT",
            "Feriados",
            "Prompts",
            "Notificações",
            "Dados Finais",
        ],
        index=0,
    )

# Página: Importar Relatórios Base
if page == "Importar Relatórios Base":
    st.subheader("Importação de Relatórios Base")
    base_files = st.file_uploader(
        "Arquivos base (Excel/CSV)", accept_multiple_files=True,
        type=["xlsx", "csv"], key="bases"
    )
    if st.button("Salvar arquivos base"):
        erros = []
        ok = 0
        for f in base_files or []:
            try:
                name = f.name
                data = f.getbuffer()
                suffix = Path(name).suffix.lower()
                # valida excel (.xlsx)
                if suffix == ".xlsx":
                    bio = BytesIO(bytes(data))
                    # tentar ler primeira aba para validar
                    _ = pd.read_excel(bio, engine="openpyxl")
                elif suffix == ".xls":
                    raise ValueError("Formato .xls não suportado. Converta para .xlsx antes de enviar.")
                # csv: leitura opcional rápida (não falha upload)
                out_path = DADOS_DIR / name
                with open(out_path, "wb") as out:
                    out.write(data)
                ok += 1
            except Exception as e:
                erros.append(f"{f.name}: {e}")
        if ok:
            st.success(f"{ok} arquivo(s) salvo(s) em {DADOS_DIR}")
        if erros:
            st.error("Falha ao validar/salvar alguns arquivos:\n" + "\n".join(erros))

    st.divider()
    st.markdown("### Carregar TODOS os arquivos de dados_entrada/ para o Banco (uma tabela por arquivo/aba)")
    st.caption("Cria uma tabela para cada arquivo CSV e uma tabela por aba em Excel. Nomes de tabela são normalizados.")
    if st.button("Carregar tudo no SQLite"):
        try:
            from ferramentas.persistencia_db import salvar_dataframe_db
            total_tabs = 0
            erros = []

            def norm_name(name: str) -> str:
                import re as _re
                base = name.strip().lower()
                base = _re.sub(r"[^a-z0-9_]+", "_", base)
                base = base.strip("_")
                return base or "tabela"

            for fpath in sorted(DADOS_DIR.glob("*.*")):
                suff = fpath.suffix.lower()
                try:
                    if suff == ".csv":
                        df = pd.read_csv(fpath)
                        tname = norm_name(fpath.stem)
                        salvar_dataframe_db(
                            df.to_json(orient="records", force_ascii=False, date_format="iso"),
                            tname,
                        )
                        total_tabs += 1
                    elif suff in (".xlsx", ".xls"):
                        xls = pd.ExcelFile(fpath)
                        for sheet in xls.sheet_names:
                            df = pd.read_excel(xls, sheet_name=sheet)
                            tname = norm_name(f"{fpath.stem}_{sheet}")
                            salvar_dataframe_db(
                                df.to_json(orient="records", force_ascii=False, date_format="iso"),
                                tname,
                            )
                            total_tabs += 1
                    else:
                        continue
                except Exception as e:
                    erros.append(f"{fpath.name}: {e}")
            if erros:
                st.warning("Ocorreram erros em alguns arquivos:\n- " + "\n- ".join(erros))
            st.success(f"Carregadas {total_tabs} tabela(s) no SQLite.")
        except Exception as e:
            st.error(f"Falha no carregamento em massa: {e}")

    # (Seção removida: geração de tabela final e carga individual para dados_consolidados)

    st.divider()
    st.markdown("### Validação de Qualidade de Dados")
    st.caption("Executa checagens de qualidade (colunas, tipos, duplicidades, datas) em dados_entrada/ antes do cálculo.")
    if st.button("Executar validação de dados"):
        try:
            from ferramentas.validacao_dados import validar_bases_dados
            rep_json = validar_bases_dados.run(str(DADOS_DIR))
            try:
                rep = json.loads(rep_json)
            except Exception:
                st.error("Relatório DQ retornou formato inesperado.")
                st.code(str(rep_json))
                st.stop()
            if not rep:
                st.info("Nenhum arquivo reconhecido em dados_entrada/ para validação.")
            else:
                df_rep = pd.DataFrame(rep)
                st.dataframe(df_rep, use_container_width=True, hide_index=True)
                # Downloads
                c1, c2 = st.columns(2)
                with c1:
                    st.download_button(
                        label="Baixar relatório (CSV)",
                        data=df_rep.to_csv(index=False).encode("utf-8"),
                        file_name="relatorio_dq.csv",
                        mime="text/csv",
                    )
                with c2:
                    st.download_button(
                        label="Baixar relatório (JSON)",
                        data=json.dumps(rep, ensure_ascii=False, indent=2).encode("utf-8"),
                        file_name="relatorio_dq.json",
                        mime="application/json",
                    )
        except Exception as e:
            st.error(f"Falha na validação de dados: {e}")

    st.divider()
    st.markdown("### Validação rápida inserção de dados no banco de dados")
    st.caption("Executa um cálculo rápido para sinalizar casos que podem exigir validação: origem de valor, comunicados até dia 15 e linhas sem valor.")
    from datetime import date as _date
    hoje = _date.today()
    # Seletor de competência via calendário (usa ano-mês do valor selecionado)
    _default_comp = _date(hoje.year, hoje.month, 1)
    _comp_date = st.date_input("Competência p/ validação", value=_default_comp, format="YYYY-MM-DD", help="Selecione a competência no calendário", key="comp_valid_date")
    comp_val = f"{_comp_date.year:04d}-{_comp_date.month:02d}"
    if st.button("Executar validação"):
        try:
            # Pré-checagem rápida: precisa ao menos de um arquivo com 'ativos' no nome em dados_entrada/
            missing = []
            try:
                files = [p.name.lower() for p in DADOS_DIR.glob("*.*")]
                if not any("ativos" in n for n in files):
                    missing.append("Ativos (arquivo cujo nome contenha 'ativos')")
            except Exception:
                pass
            # Mostrar diagnóstico das bases detectadas
            with st.expander("Diagnóstico de bases detectadas"):
                try:
                    found = sorted([p.name for p in DADOS_DIR.glob("*.*")])
                    st.write("Arquivos em dados_entrada/:", found or "(vazio)")
                    # Amostras e contagem de linhas (quando possível)
                    import pandas as _pd
                    def _peek(path: Path):
                        try:
                            if path.suffix.lower() == ".csv":
                                df = _pd.read_csv(path)
                            elif path.suffix.lower() in (".xlsx",):
                                df = _pd.read_excel(path, engine="openpyxl")
                            else:
                                return None
                            return len(df)
                        except Exception:
                            return None
                    infos = []
                    for p in DADOS_DIR.glob("*.*"):
                        if any(k in p.name.lower() for k in ["ativos","ferias","afast","deslig","aprend","estag","exterior","base","dias","uteis","admiss"]):
                            infos.append((p.name, _peek(p)))
                    if infos:
                        df_info = _pd.DataFrame(infos, columns=["arquivo","linhas"])
                        st.dataframe(df_info, use_container_width=True, hide_index=True)
                except Exception:
                    st.write("(não foi possível inspecionar arquivos)")
            if missing:
                st.error("Arquivos obrigatórios ausentes: " + ", ".join(missing))
                st.info(f"Coloque os arquivos em {DADOS_DIR} e tente novamente.")
                st.stop()

            # Força execução em modo VR
            raw = calcular_financeiro_vr.run(f"{comp_val}|VR")
            try:
                res = json.loads(raw)
            except Exception:
                st.error("Retorno inesperado do cálculo (não-JSON).")
                st.code(str(raw))
                st.stop()
            # Se o cálculo reportar erro (ex.: base ATIVOS ausente), informar e abortar a seção
            if isinstance(res, dict) and res.get("erro"):
                st.error(f"Falha no cálculo: {res.get('erro')}")
                st.info("Verifique se os arquivos base estão presentes em dados_entrada/ (ex.: 'ativos', 'ferias', 'afast', 'deslig', etc.).")
                st.stop()
            # Origem de valor
            counts = res.get("origem_valor_counts", {}) or {}
            if counts:
                df_counts = pd.DataFrame(
                    sorted(((k, v) for k, v in counts.items()), key=lambda x: (-x[1], x[0])),
                    columns=["origem_valor", "count"],
                )
                st.markdown("**Origem de valor (amostragem):**")
                st.dataframe(df_counts, use_container_width=True, hide_index=True)
            else:
                st.info("Sem dados de origem de valor.")

            # Métricas principais
            colA, colB = st.columns(2)
            with colA:
                st.metric("Zerados por comunicado<=15", value=int(res.get("zerados_por_comunicado", 0)))
            with colB:
                st.metric("Sem valor sindicato/estado", value=int(res.get("sem_valor_count", 0)))

            # Erros CSV
            err_csv = res.get("erros_csv")
            sem_val = int(res.get("sem_valor_count", 0))
            if sem_val > 0 and err_csv and Path(err_csv).exists():
                st.warning(f"Há {sem_val} linha(s) sem valor aplicado. Baixe a amostra para revisar.")
                with open(err_csv, "rb") as fp:
                    st.download_button(
                        "Baixar CSV de erros",
                        fp.read(),
                        file_name=Path(err_csv).name,
                        mime="text/csv",
                    )
            elif sem_val > 0:
                st.warning("Há linhas sem valor aplicado, mas o CSV de erros não foi encontrado.")
        except Exception as e:
            st.error(f"Falha na validação: {e}")

# Página: Importar CCTs
elif page == "Importar CCTs":
    st.subheader("Importação de CCTs (PDF)")
    cct_files = st.file_uploader(
        "CCTs (PDF)", accept_multiple_files=True, type=["pdf"], key="pdfs"
    )
    if st.button("Salvar CCT PDFs"):
        for f in cct_files or []:
            out_path = CCTS_DIR / f.name
            with open(out_path, "wb") as out:
                out.write(f.read())
        st.success(f"Salvos em {CCTS_DIR}")

    st.divider()
    st.subheader("Ingestão das CCTs")
    if st.button("Rodar ingest_ccts.py"):
        with st.spinner("Ingerindo CCTs..."):
            proc = subprocess.Popen(
                [sys.executable, str(BASE_DIR / "ingest_ccts.py")],
                cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            log = st.empty()
            lines = []
            for line in proc.stdout:
                lines.append(line.rstrip())
                log.code("\n".join(lines))
            proc.wait()
            st.success(f"Ingestão finalizada (exit={proc.returncode}).")

# Página: Feriados
elif page == "Feriados":
    st.subheader("Cadastro de Feriados (para cálculo de dias úteis)")
    path_csv = DADOS_DIR / "feriados.csv"

    def carregar_df():
        if path_csv.exists():
            try:
                df = pd.read_csv(path_csv)
            except Exception:
                df = pd.DataFrame(columns=["data", "uf", "municipio", "descricao"])
        else:
            df = pd.DataFrame(columns=["data", "uf", "municipio", "descricao"])
        # normaliza colunas
        rename = {}
        for c in df.columns:
            lc = c.lower()
            if lc.startswith("data"):
                rename[c] = "data"
            elif lc == "uf":
                rename[c] = "uf"
            elif "muni" in lc:
                rename[c] = "municipio"
            elif "descr" in lc:
                rename[c] = "descricao"
        if rename:
            df = df.rename(columns=rename)
        for col in ["data", "uf", "municipio", "descricao"]:
            if col not in df.columns:
                df[col] = None
        # formata
        df["data"] = df["data"].astype(str)
        df["uf"] = df["uf"].astype(str).str.upper().replace({"NAN": ""})
        df["municipio"] = df["municipio"].astype(str).str.upper().replace({"NAN": ""})
        df["descricao"] = df["descricao"].astype(str).replace({"nan": ""})
        return df[["data", "uf", "municipio", "descricao"]]

    df = carregar_df()

    st.caption("Preencha a data no formato YYYY-MM-DD. Deixe UF/Município vazios para feriados nacionais.")

    # Formulário para adicionar novo
    with st.form("novo_feriado"):
        c1, c2, c3 = st.columns((1, 1, 2))
        with c1:
            data_in = st.text_input("Data (YYYY-MM-DD)")
        with c2:
            uf_in = st.text_input("UF (opcional)", max_chars=2).upper()
            mun_in = st.text_input("Município (opcional)").upper()
        with c3:
            desc_in = st.text_input("Descrição")
        add_ok = st.form_submit_button("Adicionar")
        if add_ok:
            # valida data
            try:
                pd.to_datetime(data_in).date()
                df = pd.concat([
                    df,
                    pd.DataFrame([{ "data": data_in, "uf": uf_in or None, "municipio": mun_in or None, "descricao": desc_in }])
                ], ignore_index=True)
                st.success("Feriado adicionado na grade abaixo. Clique em 'Salvar alterações' para persistir.")
            except Exception:
                st.error("Data inválida. Use o formato YYYY-MM-DD.")

    st.markdown("### Lista de Feriados")
    df_edit = st.data_editor(
        df,
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "data": st.column_config.TextColumn("Data (YYYY-MM-DD)", width=140),
            "uf": st.column_config.TextColumn("UF", width=80),
            "municipio": st.column_config.TextColumn("Município"),
            "descricao": st.column_config.TextColumn("Descrição"),
        },
        hide_index=True,
    )

    colA, colB, colC = st.columns(3)
    with colA:
        if st.button("Salvar alterações"):
            # limpa linhas vazias e valida datas
            df_sav = df_edit.fillna("")
            # filtra linhas sem data
            df_sav = df_sav[df_sav["data"].astype(str).str.strip() != ""]
            # valida datas
            bad = []
            for i, v in df_sav["data"].items():
                try:
                    pd.to_datetime(v).date()
                except Exception:
                    bad.append((i, v))
            if bad:
                st.error(f"Datas inválidas nas linhas: {[i for i, _ in bad]}")
            else:
                # normaliza UF/municipio
                df_sav["uf"] = df_sav["uf"].astype(str).str.upper().replace({"NAN": ""})
                df_sav["municipio"] = df_sav["municipio"].astype(str).str.upper().replace({"NAN": ""})
                df_sav.to_csv(path_csv, index=False)
                st.success(f"Salvo em {path_csv}")
    with colB:
        up = st.file_uploader("Importar CSV", type=["csv"], key="fercsv")
        if up is not None:
            try:
                df_imp = pd.read_csv(up)
                df = pd.concat([df, df_imp], ignore_index=True)
                st.success("CSV importado na grade. Revise e clique em 'Salvar alterações'.")
            except Exception as e:
                st.error(f"Falha ao importar CSV: {e}")
    with colC:
        if st.button("Baixar CSV atual"):
            if not df_edit.empty:
                csv = df_edit.to_csv(index=False).encode("utf-8")
                st.download_button("Download feriados.csv", data=csv, file_name="feriados.csv", mime="text/csv")
            else:
                st.info("Não há dados para baixar.")

    st.divider()
    st.markdown("### Atualizar feriados automaticamente (feriados.com.br)")
    from datetime import date as _date
    _today = _date.today()
    c1, c2 = st.columns([1,2])
    with c1:
        ano_in = st.number_input("Ano", min_value=2000, max_value=2100, value=int(_today.year))
    with c2:
        ufs_in = st.text_input("UFs (separadas por vírgula, ex.: SP,RJ,MG)")
    if st.button("Buscar e atualizar feriados"):
        try:
            prev = carregar_df()
            prev_len = len(prev)
            ufs_list = [u.strip().upper() for u in (ufs_in or "").split(",") if u.strip()]
            preparar_feriados_para_ano(int(ano_in), ufs_list)
            df = carregar_df()
            added = max(0, len(df) - prev_len)
            st.success(f"Feriados atualizados e cacheados. Novas linhas adicionadas: {added}.")
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Falha ao atualizar feriados: {e}")

# Página: Regras CCT (configurar quando OCR não extraiu)
elif page == "Regras CCT":
    st.subheader("Configuração de Regras (VR/VA) por Sindicato/UF")
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    rules_index_path = RULES_INDEX_ROOT
    overrides_path = CHROMA_DIR / "rules_overrides.json"

    # Carrega index extraído e overrides existentes
    rules_index = []
    if rules_index_path.exists():
        try:
            rules_index = json.loads(rules_index_path.read_text(encoding="utf-8"))
        except Exception as e:
            st.warning(f"Falha ao ler rules_index.json: {e}")
    else:
        st.info("Ainda não há base_conhecimento/rules_index.json. Rode a ingestão em 'Importar CCTs'.")

    overrides = {}
    if overrides_path.exists():
        try:
            overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        except Exception as e:
            st.warning(f"Falha ao ler rules_overrides.json: {e}")

    # Index por (UF, Sindicato)
    def key(uf: str, sind: str) -> str:
        return f"{uf}::{sind}"

    # Construção de pendências (sem VR ou VA)
    pendentes = []
    for item in rules_index:
        uf = item.get("uf", "DESCONHECIDO")
        sind = item.get("sindicato", "DESCONHECIDO")
        k = key(uf, sind)
        # Se override já cobre, pula
        if k in overrides:
            continue
        vr_ok = bool(item.get("vr_valor"))
        va_ok = bool(item.get("va_valor"))
        if not (vr_ok and va_ok):
            pendentes.append((uf, sind))

    colA, colB = st.columns(2)
    with colA:
        st.markdown("**Pendências (OCR não completou VR/VA):**")
        if pendentes:
            df_pend = pd.DataFrame(sorted(set(pendentes)), columns=["UF", "Sindicato"])
            st.dataframe(df_pend, use_container_width=True, hide_index=True)
        else:
            st.success("Sem pendências detectadas ou já cobertas por overrides.")

    with colB:
        st.markdown("**Overrides existentes:**")
        if overrides:
            df_over = pd.DataFrame([
                {"UF": k.split("::",1)[0], "Sindicato": k.split("::",1)[1], **v}
                for k, v in overrides.items()
            ])
            st.dataframe(df_over, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum override salvo ainda.")

    st.divider()
    st.markdown("### Regras importadas (OCR/index)")
    st.caption("Lista completa de CCTs importadas do OCR/index, incluindo casos com UF=DESCONHECIDO. Use para verificar presença de RJ/PR.")
    try:
        if rules_index:
            cols = [
                "arquivo","uf","sindicato","vr_valor","va_valor","dias","periodicidade","origem","tem_clausula_vr","tem_clausula_va"
            ]
            # normaliza e seleciona colunas quando existirem
            df_rules = pd.DataFrame(rules_index)
            for c in cols:
                if c not in df_rules.columns:
                    df_rules[c] = None
            st.dataframe(
                df_rules[cols].sort_values(["uf","sindicato","arquivo"]).reset_index(drop=True),
                use_container_width=True,
                hide_index=True,
            )
            # Destaque de pendências de cláusulas/dias
            try:
                missing_mask = (
                    (df_rules.get("vr_valor").isna() | (df_rules.get("vr_valor")=="")) |
                    (df_rules.get("va_valor").isna() | (df_rules.get("va_valor")=="")) |
                    (df_rules.get("dias").isna()) |
                    (df_rules.get("tem_clausula_vr")==False) |
                    (df_rules.get("tem_clausula_va")==False)
                )
                df_missing = df_rules.loc[missing_mask, ["arquivo","uf","sindicato","vr_valor","va_valor","dias","tem_clausula_vr","tem_clausula_va"]]
                st.markdown("#### Itens com pendências (sem cláusula VR/VA explícita e/ou sem 'dias')")
                if not df_missing.empty:
                    st.warning("Algumas CCTs não trazem cláusula explícita ou não informam dias. Será necessário cadastrar override ou usar Base Manual/Estado.")
                    st.dataframe(df_missing.sort_values(["uf","sindicato","arquivo"]).reset_index(drop=True), use_container_width=True, hide_index=True)
                else:
                    st.success("Sem pendências de cláusula/dias detectadas.")
            except Exception:
                pass
        else:
            st.info("Nenhuma regra importada disponível.")
    except Exception as e:
        st.warning(f"Falha ao exibir regras importadas: {e}")

    st.divider()
    st.markdown("### Importação planilha")
    st.caption("Envie um CSV/XLSX com colunas ESTADO (ou UF) e VALOR. Usado quando a CCT não definir VR/VA/dias.")
    up_ev = st.file_uploader("Planilha Estado/Valor", type=["csv","xlsx"], key="estado_valor_upload")
    if up_ev is not None:
        try:
            name = up_ev.name
            data = up_ev.read()
            out = DADOS_DIR / name
            with open(out, "wb") as fp:
                fp.write(data)
            st.success(f"Arquivo salvo em {out}")
        except Exception as e:
            st.error(f"Falha ao salvar arquivo: {e}")

    st.divider()
    st.markdown("### Cadastro manual")
    # Permitir selecionar um override existente para edição rápida
    override_keys = [f"{k.split('::',1)[0]} :: {k.split('::',1)[1]}" for k in sorted(overrides.keys())]
    sel_label = "Selecionar override existente (opcional)"
    sel_options = ["(novo)"] + override_keys
    sel_choice = st.selectbox(sel_label, sel_options, index=0, key="sel_override_existente")

    # Definir valores padrão do formulário com base na seleção
    uf_def = ""
    sind_def = ""
    vr_def = ""
    va_def = ""
    dias_def = 0
    notas_def = ""
    if sel_choice != "(novo)":
        try:
            uf_def, sind_def = [p.strip() for p in sel_choice.split("::", 1)]
            k_int = f"{uf_def}::{sind_def}"
            ov = overrides.get(k_int, {})
            vr_def = str(ov.get("vr_valor") or "")
            va_def = str(ov.get("va_valor") or "")
            dias_def = int(ov.get("dias") or 0)
            notas_def = str(ov.get("notas") or "")
        except Exception:
            pass

    with st.form("form_override"):
        col1, col2 = st.columns(2)
        with col1:
            uf_in = st.text_input("UF", value=uf_def, max_chars=2).upper()
            vr_in = st.text_input("VR (ex.: R$ 25,00)", value=vr_def)
            dias_in = st.number_input("Dias (opcional)", min_value=0, max_value=31, value=int(dias_def))
        with col2:
            sind_in = st.text_input("Sindicato (nome completo)", value=sind_def)
            va_in = st.text_input("VA (ex.: R$ 180,00)", value=va_def)
        notas_in = st.text_area("Notas (opcional)", value=notas_def, height=80)
        submitted = st.form_submit_button("Salvar Override")
        if submitted:
            if not uf_in or not sind_in:
                st.error("Informe UF e Sindicato.")
            else:
                k = key(uf_in, sind_in)
                overrides[k] = {
                    "vr_valor": vr_in.strip() or None,
                    "va_valor": va_in.strip() or None,
                    "dias": int(dias_in) if dias_in else None,
                    "notas": notas_in.strip() or None,
                    "fonte": "override_manual",
                }
                try:
                    overrides_path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
                    st.success("Override salvo com sucesso.")
                except Exception as e:
                    st.error(f"Falha ao salvar override: {e}")

    st.divider()
    st.markdown("### Importar Overrides de CSV (opcional)")
    st.caption("Colunas esperadas: UF, Sindicato, vr_valor, va_valor, dias, notas")
    up = st.file_uploader("CSV de overrides", type=["csv"], key="overcsv")
    if up is not None:
        try:
            df_csv = pd.read_csv(up)
            add = 0
            for _, row in df_csv.iterrows():
                uf = str(row.get("UF", "")).upper()
                sind = str(row.get("Sindicato", ""))
                if not uf or not sind:
                    continue
                k = key(uf, sind)
                overrides[k] = {
                    "vr_valor": row.get("vr_valor"),
                    "va_valor": row.get("va_valor"),
                    "dias": int(row.get("dias")) if pd.notna(row.get("dias")) else None,
                    "notas": row.get("notas"),
                    "fonte": "override_csv",
                }
                add += 1
            overrides_path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
            st.success(f"{add} overrides importados.")
        except Exception as e:
            st.error(f"Falha ao importar CSV: {e}")

    st.divider()
    st.markdown("### Validação de Compliance (OCR x Sistema)")
    st.caption("Compara regras extraídas por OCR (rules_index.json) com as regras resolvidas pelo sistema (overrides/SQLite/Chroma).")
    if st.button("Executar validação de compliance"):
        try:
            from ferramentas.validador_cct import validar_compliance_cct
            rel = validar_compliance_cct()
            resumo = rel.get("resumo", {})
            itens = rel.get("itens", [])
            col1, col2, col3, col4, col5, col6 = st.columns(6)
            with col1:
                st.metric("Total", int(resumo.get("total", 0)))
            with col2:
                st.metric("OK", int(resumo.get("ok", 0)))
            with col3:
                st.metric("Mismatch", int(resumo.get("mismatch", 0)))
            with col4:
                st.metric("Missing Sistema", int(resumo.get("missing_system", 0)))
            with col5:
                st.metric("Missing OCR", int(resumo.get("missing_ocr", 0)))
            with col6:
                st.metric("Recomenda site", int(resumo.get("site_check_recommended", 0)))

            if itens:
                df_it = pd.DataFrame(itens)
                # Mostrar apenas problemas por padrão
                problemas = df_it[df_it["status"] != "ok"].copy()
                if not problemas.empty:
                    st.markdown("**Pendências/Discrepâncias detectadas:**")
                    st.dataframe(
                        problemas[[
                            "uf","sindicato","status","origem_sistema","vr_ocr","vr_sistema","va_ocr","va_sistema","dias_ocr","dias_sistema","periodicidade_ocr","periodicidade_sistema","site_check_recommended","detalhes"
                        ]],
                        use_container_width=True,
                        hide_index=True,
                    )
                else:
                    st.success("Nenhuma discrepância encontrada.")

            # Download do JSON gerado
            try:
                outp = (RELATORIOS_DIR / "cct_compliance.json")
                if outp.exists():
                    st.download_button(
                        label="Baixar relatório de compliance (JSON)",
                        data=outp.read_bytes(),
                        file_name=outp.name,
                        mime="application/json",
                    )
            except Exception:
                pass
        except Exception as e:
            st.error(f"Falha na validação de compliance: {e}")

    st.divider()
    st.markdown("### Resumo consolidado (rules_index.json consolidado)")
    st.caption("Consolidação por UF/Sindicato priorizando valores > cláusulas > dias, preferindo periodicidade diária em caso de empate.")
    try:
        from agentes.cct import criar_agente_coletor_cct
        agente = criar_agente_coletor_cct()
        raw = agente("")
        data = json.loads(raw) if raw else []
        df_consol = pd.DataFrame(data)
        # Normaliza colunas esperadas
        cols = [
            "uf","sindicato","vr_valor","va_valor","dias","periodicidade",
            "tem_clausula_vr","tem_clausula_va","origem",
            "pendencia_valores","pendencia_dias","pendencia_clausulas",
        ]
        for c in cols:
            if c not in df_consol.columns:
                df_consol[c] = None

        # Filtros
        ufs = sorted([u for u in df_consol["uf"].dropna().unique().tolist() if u])
        sel_ufs = st.multiselect("Filtrar UF", ufs, default=[])
        filtro_sind = st.text_input("Filtrar por substring de Sindicato", "")
        only_pend = st.checkbox("Mostrar apenas pendências", value=False)
        prefer_diario = st.checkbox("Preferir diário na ordenação", value=True)

        dfv = df_consol.copy()
        if sel_ufs:
            dfv = dfv[dfv["uf"].isin(sel_ufs)]
        if filtro_sind.strip():
            q = filtro_sind.strip().lower()
            dfv = dfv[dfv["sindicato"].astype(str).str.lower().str.contains(q)]
        if only_pend:
            pend = (dfv["pendencia_valores"].astype(bool)) | (dfv["pendencia_dias"].astype(bool)) | (dfv["pendencia_clausulas"].astype(bool))
            dfv = dfv[pend]

        # Ordenação: UF, Sindicato, e opcionalmente trazer 'diário' primeiro
        if prefer_diario and "periodicidade" in dfv.columns:
            per = dfv["periodicidade"].astype(str).str.lower().fillna("")
            dfv = dfv.assign(_pref_diario=per.str.contains("diar").astype(int))
            dfv = dfv.sort_values(["uf","_pref_diario","sindicato"], ascending=[True, False, True]).drop(columns=["_pref_diario"])
        else:
            dfv = dfv.sort_values(["uf","sindicato"]) 

        st.dataframe(
            dfv[cols],
            use_container_width=True,
            hide_index=True,
        )
    except Exception as e:
        st.warning(f"Falha ao gerar resumo consolidado: {e}")

# Página: Prompts
elif page == "Prompts":
    st.subheader("Prompts dos Agentes")
    prompt_files = sorted(PROMPTS_DIR.glob("*.md"))
    if not prompt_files:
        st.info("Nenhum arquivo de prompt .md encontrado em prompts/.")
    else:
        names = [p.name for p in prompt_files]
        sel = st.selectbox("Selecione um prompt para editar", names)
        sel_path = PROMPTS_DIR / sel
        content = sel_path.read_text(encoding="utf-8")
        new_content = st.text_area("Conteúdo do prompt (Markdown)", value=content, height=400)
        colA, colB = st.columns(2)
        with colA:
            if st.button("Salvar alterações"):
                # backup simples
                backup_path = sel_path.with_suffix(sel_path.suffix + ".bak")
                try:
                    if not backup_path.exists():
                        backup_path.write_text(content, encoding="utf-8")
                except Exception:
                    pass
                sel_path.write_text(new_content, encoding="utf-8")
                st.success(f"{sel} salvo com sucesso.")
        with colB:
            st.download_button(
                label="Baixar prompt", data=content, file_name=sel, mime="text/markdown"
            )

# Página: Notificações
elif page == "Notificações":
    st.subheader("Notificações e Sinalizações")
    st.caption("Diferenças entre valor VR do relatório base (estado) e CCT, matrículas sem admissão, e exclusões por regra.")

    from datetime import date as _date
    hoje = _date.today()
    # Calendar popover for competência (use the picked date's year-month)
    default_comp = _date(hoje.year, hoje.month, 1)
    comp_date = st.date_input("Competência", value=default_comp, format="YYYY-MM-DD", help="Selecione a competência no calendário")
    # Normalize to YYYY-MM
    y, m = comp_date.year, comp_date.month
    comp = f"{y:04d}-{m:02d}"

    # Helpers locais
    def _read_sheet(p: str) -> pd.DataFrame:
        if not p:
            return pd.DataFrame()
        suf = Path(p).suffix.lower()
        if suf == ".csv":
            return pd.read_csv(p)
        elif suf in (".xlsx",):
            return pd.read_excel(p, engine="openpyxl")
        elif suf == ".xls":
            raise ValueError("Formato .xls não suportado; converta para .xlsx.")
        else:
            return pd.read_excel(p, engine="openpyxl")

    try:
        # Localizar arquivos como no cálculo
        base_dir = str(DADOS_DIR)
        f_ativos   = _find_file_by_keywords(base_dir, ["ativos"]) 
        f_aprendiz = _find_file_by_keywords(base_dir, ["aprend"]) 
        f_estagio  = _find_file_by_keywords(base_dir, ["estag"]) 
        f_exterior = _find_file_by_keywords(base_dir, ["exterior"]) 
        f_adm      = _find_file_by_keywords(base_dir, ["admiss"]) 
        f_valor    = _find_file_by_keywords(base_dir, ["base","sindicato","valor"]) 
        f_ferias   = _find_file_by_keywords(base_dir, ["ferias"]) 
        f_afast    = _find_file_by_keywords(base_dir, ["afast"]) 
        f_deslig   = _find_file_by_keywords(base_dir, ["deslig"]) 
        f_atend    = _find_file_by_keywords(base_dir, ["atend"]) or _find_file_by_keywords(base_dir, ["obs"]) 

        if not f_ativos:
            st.error("Base ATIVOS não encontrada em dados_entrada/.")
            st.stop()

        ativos   = _read_sheet(f_ativos)
        aprendiz = _read_sheet(f_aprendiz)
        estagio  = _read_sheet(f_estagio)
        exterior = _read_sheet(f_exterior)
        admis    = _read_sheet(f_adm)
        vr_est   = _read_sheet(f_valor)
        ferias   = _read_sheet(f_ferias)
        afast    = _read_sheet(f_afast)
        deslig   = _read_sheet(f_deslig)
        atend    = _read_sheet(f_atend)

        # Normalização mínima de colunas
        ativos.columns = [str(c).strip() for c in ativos.columns]
        # Identifica colunas chave
        id_ativos = ativos.columns[0]
        nome_col = _find_col(ativos.columns, ["nome","colaborador","funcionario"]) 
        sind_col = _find_col(ativos.columns, ["sindicato","sind"]) 

        work = ativos[[id_ativos] + ([nome_col] if nome_col else []) + ([sind_col] if sind_col else [])].copy()
        work.columns = ["matricula"] + (["nome"] if nome_col else []) + (["sindicato"] if sind_col else [])
        work["matricula"] = work["matricula"].astype(str)

        # Mapas de admissão
        adm_map = {}
        # Detecta coluna de admissão de forma tolerante (captura 'Admissão', 'Data de Admissão', etc.)
        adm_col = _find_col(admis.columns if admis is not None else [], ["data_admiss","admiss"]) 
        if adm_col and not admis.empty:
            for _, r in admis.iterrows():
                try:
                    adm_map[str(r[admis.columns[0]])] = pd.to_datetime(r[adm_col], dayfirst=True).date()
                except Exception:
                    pass

        # Exclusões por listas
        def _ids(df: pd.DataFrame) -> set[str]:
            if df is None or df.empty:
                return set()
            return set(map(str, df[df.columns[0]].astype(str).tolist()))
        ids_ap = _ids(aprendiz)
        ids_es = _ids(estagio)
        ids_ex = _ids(exterior)

        # Exclusões heurísticas no ATIVOS (sem duplicar as listas dedicadas)
        excl_heur = set()
        try:
            for _, r in ativos.iterrows():
                mid = str(r[id_ativos])
                if mid in ids_ap or mid in ids_es or mid in ids_ex:
                    continue
                if _should_exclude(r):
                    excl_heur.add(mid)
        except Exception:
            pass

        # Contagens de notificações
        sem_adm = int(sum(1 for mid in work["matricula"].tolist() if mid not in adm_map))
        total_ativos = int(len(ativos))
        qtd_aprendiz = len(ids_ap)
        qtd_estagio = len(ids_es)
        qtd_exterior = len(ids_ex)

        # Afastados/Licenças: contar IDs únicos na base de afastamentos
        def _uniq_count(df: pd.DataFrame) -> int:
            if df is None or df.empty:
                return 0
            return int(df[df.columns[0]].astype(str).nunique())
        qtd_afast = _uniq_count(afast)

        # Férias: contar IDs únicos
        qtd_ferias = _uniq_count(ferias)

        # Desligados geral e por faixas
        qtd_deslig_geral = 0
        qtd_deslig_ate15_ok = 0
        qtd_deslig_ate15_sem_ok = 0
        qtd_deslig_16a_fim = 0
        if deslig is not None and not deslig.empty:
            # detectar colunas: data, status
            def _find_col_any(cols, keys):
                low = {str(c).lower(): c for c in cols}
                for k in keys:
                    for lc, orig in low.items():
                        if k in lc:
                            return orig
                return None
            c_data = _find_col_any(deslig.columns, ["data_demissao","data_deslig","demiss","deslig"])
            c_stat = _find_col_any(deslig.columns, ["status","comunicado"])  # flag OK
            qtd_deslig_geral = int(deslig[deslig.columns[0]].astype(str).nunique())
            for _, r in deslig.iterrows():
                try:
                    d = pd.to_datetime(r[c_data]).date() if c_data else None
                except Exception:
                    d = None
                if d and d.year == y and d.month == m:
                    if d.day <= 15:
                        flag_ok = str(r.get(c_stat, "")).strip().upper().find("OK") >= 0 if c_stat else False
                        if flag_ok:
                            qtd_deslig_ate15_ok += 1
                        else:
                            qtd_deslig_ate15_sem_ok += 1
                    else:
                        qtd_deslig_16a_fim += 1

        # Admitidos mês atual e mês anterior (mês anterior cheio) — robusto
        qtd_adm_mes = 0
        qtd_adm_mes_prev = 0
        try:
            from datetime import date as _d
            from calendar import monthrange as _monthrange
            prev_y = y if m > 1 else y - 1
            prev_m = m - 1 if m > 1 else 12
            prev_start = _d(prev_y, prev_m, 1)
            prev_end = _d(prev_y, prev_m, _monthrange(prev_y, prev_m)[1])
            curr_start = _d(y, m, 1)
            curr_end = _d(y, m, _monthrange(y, m)[1])

            if admis is not None and not admis.empty:
                # Garante nomes de colunas como string
                admis.columns = [str(c) for c in admis.columns]
                # 1) Usa coluna detectada por chave ('admiss')
                _adm_col = adm_col
                # 2) Se não achou, tenta heurística: coluna com 'admis' ou 'data' que tenha muitas datas válidas
                if not _adm_col:
                    candidates = [c for c in admis.columns if any(k in c.lower() for k in ["admis", "data"]) ]
                    best_col, best_non_na = None, -1
                    for c in candidates or admis.columns:
                        s = pd.to_datetime(admis[c], errors="coerce")
                        nn = s.notna().sum()
                        if nn > best_non_na:
                            best_col, best_non_na = c, nn
                    _adm_col = best_col
                if _adm_col:
                    dts = pd.to_datetime(admis[_adm_col], errors="coerce", dayfirst=True)
                    mask_curr = (dts >= pd.Timestamp(curr_start)) & (dts <= pd.Timestamp(curr_end))
                    mask_prev = (dts >= pd.Timestamp(prev_start)) & (dts <= pd.Timestamp(prev_end))
                    qtd_adm_mes = int(mask_curr.sum())
                    qtd_adm_mes_prev = int(mask_prev.sum())
                    # Debug: estatísticas da coluna de admissão
                    with st.expander("Diagnóstico de Admissões", expanded=False):
                        st.write({
                            "coluna_detectada": _adm_col,
                            "total_linhas": int(len(admis)),
                            "datas_validas": int(dts.notna().sum()),
                            "primeira_data": str(pd.to_datetime(dts.min()).date()) if pd.notna(dts.min()) else None,
                            "ultima_data": str(pd.to_datetime(dts.max()).date()) if pd.notna(dts.max()) else None,
                            "intervalo_atual": [str(curr_start), str(curr_end)],
                            "intervalo_anterior": [str(prev_start), str(prev_end)],
                            "admitidos_atual": qtd_adm_mes,
                            "admitidos_anterior": qtd_adm_mes_prev,
                        })
        except Exception:
            pass

        # Atendimentos/OBS: apenas contar linhas se arquivo existir
        qtd_atend = int(len(atend)) if atend is not None and not atend.empty else 0

        # Exibir métricas
        st.metric("Ativos", value=total_ativos)
        cA, cB, cC, cD = st.columns(4)
        with cA:
            st.metric("Aprendiz", value=qtd_aprendiz)
        with cB:
            st.metric("Estagiário", value=qtd_estagio)
        with cC:
            st.metric("Exterior", value=qtd_exterior)
        with cD:
            st.metric("Férias", value=qtd_ferias)
        cE, cF, cG = st.columns(3)
        with cE:
            st.metric("Afastados/Licenças", value=qtd_afast)
        with cF:
            st.metric("Desligados Geral", value=qtd_deslig_geral)
        with cG:
            st.metric("Matrículas sem admissão", value=sem_adm)
        cH, cI = st.columns(2)
        with cH:
            st.metric("Admitidos mês", value=qtd_adm_mes)
        with cI:
            st.metric("Admitidos mês anterior", value=qtd_adm_mes_prev)

        cJ, cK, cL = st.columns(3)
        with cJ:
            st.metric("Desligados até 15 (OK)", value=qtd_deslig_ate15_ok, help="Excluir da compra")
        with cK:
            st.metric("Desligados até 15 (sem OK)", value=qtd_deslig_ate15_sem_ok, help="Comprar integral")
        with cL:
            st.metric("Desligados 16..fim", value=qtd_deslig_16a_fim, help="Recarga cheia; desconto proporcional em rescisão")

        st.metric("Atendimentos/OBS", value=qtd_atend)

        st.divider()
        st.markdown("### Diferenças de VR — Base (Estado) x CCT")
        # Preparar mapa Estado->valor
        diff_rows = []
        estado_val_map = {}
        if vr_est is not None and not vr_est.empty:
            ev_col = _find_col(vr_est.columns, ["estado","uf","unidade_federativa"]) or vr_est.columns[0]
            vv_col = _find_col(vr_est.columns, ["valor","vr","vale_refeicao"]) or vr_est.columns[-1]
            vr_est = vr_est.rename(columns={ev_col: "estado", vv_col: "valor"})
            vr_est["estado_norm"] = vr_est["estado"].astype(str).str.strip().str.lower()
            for _, r in vr_est.iterrows():
                estado_val_map[str(r["estado_norm"])]= r["valor"]

        # Agrupar por sindicato (para obter UF inferida) e comparar valores
        def _uf_from_sind(s: str) -> str | None:
            import re as _re
            if not isinstance(s, str):
                return None
            toks = _re.findall(r"\b([A-Z]{2})\b", s)
            for t in toks:
                if t in UF_MAP:
                    return t
            return None

        seen = set()
        for _, r in work.iterrows():
            sind = str(r.get("sindicato",""))
            uf = _uf_from_sind(sind)
            if not uf:
                continue
            key = (uf, sind)
            if key in seen:
                continue
            seen.add(key)
            # Base (estado)
            est_nome = UF_MAP.get(uf)
            base_val = None
            if est_nome:
                en = str(est_nome).strip().lower()
                base_val = estado_val_map.get(en)
            # CCT
            try:
                regra = resolve_cct_rules(uf=uf, sindicato=sind)
            except Exception:
                regra = {}
            vr_val = (regra or {}).get("vr_valor")
            per = (regra or {}).get("periodicidade")
            dias = (regra or {}).get("dias")
            cct_val = None
            try:
                if vr_val:
                    v = float(
                        str(vr_val)
                        .replace("R$", "")
                        .replace(" ", "")
                        .replace(".", "")
                        .replace(",", ".")
                    )
                    if (per or "dia").lower().startswith("mes") and dias:
                        cct_val = round(v / max(int(dias), 1), 2)
                    else:
                        cct_val = round(v, 2)
            except Exception:
                cct_val = None
            # Diferença quando ambos existem
            if base_val is not None and cct_val is not None:
                try:
                    b = float(base_val)
                    c = float(cct_val)
                    if abs(b - c) > 0.005:
                        diff_rows.append({
                            "UF": uf,
                            "Sindicato": sind,
                            "VR Base (Estado)": round(b,2),
                            "VR CCT (diário)": round(c,2),
                            "Diferença": round(c - b, 2),
                        })
                except Exception:
                    pass

        if diff_rows:
            df_diff = pd.DataFrame(diff_rows)
            st.dataframe(df_diff.sort_values(["UF","Sindicato"]).reset_index(drop=True), use_container_width=True, hide_index=True)
            st.download_button(
                "Baixar discrepâncias (CSV)",
                data=pd.DataFrame(diff_rows).to_csv(index=False).encode("utf-8"),
                file_name="discrepancias_vr_base_vs_cct.csv",
                mime="text/csv",
            )
        else:
            st.success("Nenhuma discrepância relevante encontrada entre Base (Estado) e CCT para VR.")

        st.divider()
        st.markdown("### Sindicatos x Valor (VR diário resolvido)")
        sind_rows = []
        seen_sv = set()
        for _, r in work.iterrows():
            sind = str(r.get("sindicato", "")).strip()
            if not sind:
                continue
            if sind in seen_sv:
                continue
            seen_sv.add(sind)
            uf = None
            try:
                import re as _re
                toks = _re.findall(r"\b([A-Z]{2})\b", sind)
                for t in toks:
                    if t in UF_MAP:
                        uf = t
                        break
            except Exception:
                uf = None
            regra = {}
            try:
                regra = resolve_cct_rules(uf=uf or "", sindicato=sind)
            except Exception:
                regra = {}
            vr_val = (regra or {}).get("vr_valor")
            per = (regra or {}).get("periodicidade")
            dias = (regra or {}).get("dias")
            vr_d = None
            try:
                if vr_val:
                    v = float(str(vr_val).replace("R$", "").replace(" ", "").replace(".", "").replace(",", "."))
                    if (per or "dia").lower().startswith("mes") and dias:
                        vr_d = round(v / max(int(dias), 1), 2)
                    else:
                        vr_d = round(v, 2)
            except Exception:
                vr_d = None
            sind_rows.append({"Sindicato": sind, "UF": uf or "?", "VR diário": vr_d})
        if sind_rows:
            st.dataframe(pd.DataFrame(sind_rows).sort_values(["UF","Sindicato"]).reset_index(drop=True), use_container_width=True, hide_index=True)

    except Exception as e:
        st.error(f"Falha ao gerar notificações: {e}")

# Página: Dashboard
elif page == "Dados Finais":
    st.subheader("Execução e Revisão")
    tarefastr = st.text_input(
        "Tarefa", value=(
            "Calcular VR/VA para o mês de Maio de 2025 usando arquivos em dados_entrada/, validando compliance e CCTs."
        )
    )
    if st.button("Executar Orquestração"):
        status_placeholder = st.empty()
        status_placeholder.info("Aguardando o início do processo...")
        # Infra: limpar arquivo de progresso antes de iniciar
        prog_file = RELATORIOS_DIR / "progresso_execucao.jsonl"
        try:
            if prog_file.exists():
                prog_file.unlink()
        except Exception:
            pass
        # Placeholders de workflow ao vivo
        current_placeholder = st.empty()
        steps_placeholder = st.empty()
        env = os.environ.copy()
        env["ORQ_TAREFA"] = tarefastr
        with st.spinner("Executando orquestração..."):
            status_placeholder.info("Executando orquestração...")
            proc = subprocess.Popen(
                [sys.executable, str(BASE_DIR / "main.py")],
                cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env
            )
            # Não exibimos log bruto; apenas status tabular
            steps = []  # lista de dicts {agent, action, status, info}
            for line in proc.stdout:
                ln = line.rstrip()
                # Captura e exibe workflow se vier marcador ::STEP::
                if ln.startswith("::STEP::"):
                    try:
                        _pfx, agent, action, status, rest = ln.split("::", 4)
                        info = rest.strip()
                    except ValueError:
                        agent = action = status = ""
                        info = ln
                    steps.append({
                        "Agente": agent,
                        "Ação": action,
                        "Status": status,
                        "Info": info,
                    })
                    # Atualiza UI
                    if agent:
                        current_placeholder.markdown(f"**Agente atual:** {agent}  ")
                        current_placeholder.markdown(f"Ação: {action} — Status: `{status}`")
                    if steps:
                        import pandas as _pd
                        steps_df = _pd.DataFrame(steps)
                        steps_placeholder.dataframe(steps_df, use_container_width=True, hide_index=True)
            proc.wait()
            status_placeholder.success("Processo concluído.")
            st.success(f"Orquestração finalizada (exit={proc.returncode}).")

        # Após execução, tenta exibir relatório de validações do orquestrador
        try:
            import json
            resultado_json = RELATORIOS_DIR / "resultado_execucao.json"
            if resultado_json.exists():
                dados = json.loads(resultado_json.read_text(encoding="utf-8"))
                st.subheader("Checks de Validação da Execução")
                validacoes = dados.get("validacoes", [])
                if validacoes:
                    for v in validacoes:
                        st.markdown(f"- ✅ {v}")
                else:
                    st.info("Nenhuma validação registrada.")
        except Exception as e:
            st.warning(f"Não foi possível carregar o relatório de validações: {e}")

        # Histórico de execução (arquivo jsonl)
        st.divider()
        st.markdown("### Histórico de Execução (Workflow)")
        try:
            if prog_file.exists():
                rows = []
                for ln in prog_file.read_text(encoding="utf-8").splitlines():
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
                if rows:
                    df_hist = pd.DataFrame(rows)
                    # Renomeia colunas para PT
                    df_hist = df_hist.rename(columns={
                        "agent": "Agente",
                        "action": "Ação",
                        "status": "Status",
                        "info": "Info",
                    })
                    st.dataframe(df_hist, use_container_width=True, hide_index=True)
                else:
                    st.info("Sem passos registrados no progresso.")
            else:
                st.info("Arquivo de progresso não encontrado.")
        except Exception as e:
            st.warning(f"Falha ao carregar histórico: {e}")

    # (Seções removidas: Regras (CCT) e Compliance)

    st.divider()
    st.subheader("Banco de Dados (SQLite)")
    if DB_PATH.exists():
        st.caption(f"Arquivo: {DB_PATH}")
        # Botão de smoke test: cria uma tabela simples no DB
        if st.button("Criar tabela de teste no DB"):
            try:
                import sqlite3
                df_test = pd.DataFrame([
                    {"Validações": "Smoke", "Check": "OK"},
                    {"Validações": "Paths", "Check": str(DB_PATH)},
                ])
                with sqlite3.connect(str(DB_PATH)) as conn:
                    df_test.to_sql("smoke_test", conn, if_exists="replace", index=False)
                st.success("Tabela 'smoke_test' criada com sucesso.")
            except Exception as e:
                st.error(f"Falha ao criar tabela de teste: {e}")
        try:
            import sqlite3
            with sqlite3.connect(str(DB_PATH)) as conn:
                tbls = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;", conn)
            if tbls.empty:
                st.info("Nenhuma tabela encontrada no banco ainda.")
            else:
                tnames = tbls["name"].tolist()
                tsel = st.selectbox("Tabela para visualizar", tnames)
                with sqlite3.connect(str(DB_PATH)) as conn:
                    df_tbl = pd.read_sql_query(f"SELECT * FROM {tsel} LIMIT 500", conn)
                st.dataframe(df_tbl, use_container_width=True)
        except Exception as e:
            st.error(f"Erro ao ler o banco: {e}")
    else:
        st.info("Banco ainda não criado.")

    # (Seção removida: Arquivos gerados)

    st.divider()
    st.markdown("### Gerar Benefícios Mensais (VR / VA / Consolidado)")
    from datetime import date as _date
    hoje = _date.today()
    colc, colp, coln = st.columns([1,1,2])
    with colc:
        competencia = st.text_input("Competência (YYYY-MM)", value=f"{hoje.year}-{hoje.month:02d}")
    with colp:
        produto = st.selectbox("Produto", ["VR", "VA", "CONSOLIDADO"], index=0)
    with coln:
        default_prefix = "VR_MENSAL" if produto=="VR" else ("VA_MENSAL" if produto=="VA" else "BENEFICIOS_MENSAL_CONSOLIDADO")
        nome_arquivo = st.text_input("Nome do arquivo de exportação", value=f"{default_prefix}_{hoje.month:02d}.{hoje.year}.xlsx")
    st.markdown("#### Base de valores para VR/VA")
    base_opt = st.radio(
        "Selecione a base a utilizar",
        ["CCT Padrão", "Importação planilha"],
        index=0,
        help="CCT Padrão usa regras resolvidas (inclui cadastro manual/overrides). Importação planilha usa arquivo Estado/Valor em dados_entrada/.")
    if st.button("Gerar Relatório"):
        try:
            # Validar seleção e setar modo via variável de ambiente para o cálculo
            mode = "CCT" if base_opt.startswith("CCT") else "MANUAL"
            os.environ["VRVA_VAL_BASE"] = mode
            # Quando em modo Manual/Estado, validar presença de base ESTADO/VALOR em dados_entrada/
            if mode == "MANUAL":
                # localizar arquivo e validar colunas
                base_file = None
                for p in sorted(DADOS_DIR.glob("*.*")):
                    if any(k in p.name.lower() for k in ["base","valor","estado","uf","sindicato"]):
                        base_file = p
                        break
                if not base_file:
                    st.error("Modo 'Importação planilha' selecionado, mas nenhuma planilha de Estado/Valor foi encontrada em dados_entrada/.")
                    st.info("Envie um CSV/XLSX com colunas ESTADO (ou UF) e VALOR na seção 'Importação planilha'.")
                    up2 = st.file_uploader("Enviar agora (Estado/Valor)", type=["csv","xlsx"], key="estado_valor_upload_run")
                    st.stop()
                # valida colunas
                try:
                    if base_file.suffix.lower() == ".csv":
                        df_ev = pd.read_csv(base_file)
                    else:
                        df_ev = pd.read_excel(base_file, engine="openpyxl")
                    cols_low = [str(c).strip().lower() for c in df_ev.columns]
                    has_estado = any(c in cols_low for c in ["estado","uf"]) 
                    has_valor = any("valor" == c or c.endswith("valor") for c in cols_low)
                    if not (has_estado and has_valor):
                        st.error(f"Arquivo detectado '{base_file.name}' não possui colunas necessárias: ESTADO/UF e VALOR.")
                        st.stop()
                except Exception as e:
                    st.error(f"Falha ao ler/validar planilha Estado/Valor: {e}")
                    st.stop()
            # LangChain Tool expects positional tool_input
            res = json.loads(calcular_financeiro_vr.run(f"{competencia}|{produto}"))
            total_fmt = f"R$ {res['total_geral']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            st.success(f"Produto: {res.get('produto', produto)} | Linhas: {res['linhas']} | Total: {total_fmt}")
            # preparar arquivo de exportação com nome customizado
            export_path = Path(res["saida_xlsx"])  # default
            try:
                desired = (nome_arquivo or "").strip()
                if desired:
                    if not desired.lower().endswith(".xlsx"):
                        desired += ".xlsx"
                    custom_path = RELATORIOS_DIR / desired
                    # copia bytes do arquivo gerado para o nome desejado
                    with open(res["saida_xlsx"], "rb") as src, open(custom_path, "wb") as dst:
                        dst.write(src.read())
                    export_path = custom_path
            except Exception as _:
                pass
            with open(export_path, "rb") as f:
                st.download_button(
                    "Baixar Relatório",
                    f,
                    file_name=export_path.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

            # Downloads auxiliares: erros e pendências CCT
            colE, colP = st.columns(2)
            with colE:
                err_csv = res.get("erros_csv")
                if err_csv and Path(err_csv).exists():
                    with open(err_csv, "rb") as fp:
                        st.download_button(
                            "Baixar CSV de Erros",
                            fp.read(),
                            file_name=Path(err_csv).name,
                            mime="text/csv",
                        )
            with colP:
                pend_csv = res.get("pendencias_cct_csv")
                if pend_csv and Path(pend_csv).exists():
                    with open(pend_csv, "rb") as fp:
                        st.download_button(
                            "Baixar Pendências CCT",
                            fp.read(),
                            file_name=Path(pend_csv).name,
                            mime="text/csv",
                        )
        except Exception as e:
            st.error(f"Falha ao gerar relatório: {e}")
