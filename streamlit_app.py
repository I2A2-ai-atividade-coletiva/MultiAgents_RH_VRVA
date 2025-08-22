import os
import subprocess
from pathlib import Path
import pandas as pd
import streamlit as st
from ferramentas.persistencia_db import DB_PATH
import sys
import json
from ferramentas.calculadora_beneficios import calcular_financeiro_vr
from io import BytesIO

BASE_DIR = Path(__file__).resolve().parent
DADOS_DIR = BASE_DIR / "dados_entrada"
CCTS_DIR = BASE_DIR / "base_conhecimento" / "ccts_pdfs"
RELATORIOS_DIR = BASE_DIR / "relatorios_saida"
PROMPTS_DIR = BASE_DIR / "prompts"
CHROMA_DIR = BASE_DIR / "base_conhecimento" / "chromadb"

DADOS_DIR.mkdir(parents=True, exist_ok=True)
CCTS_DIR.mkdir(parents=True, exist_ok=True)
RELATORIOS_DIR.mkdir(parents=True, exist_ok=True)
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Automação RH - Multiagentes", layout="wide")
st.title("Automação RH - Multiagentes")

with st.sidebar:
    st.header("Navegação")
    page = st.radio(
        "Páginas",
        ["Importar Relatórios Base", "Importar CCTs", "Feriados", "Regras CCT", "Prompts", "Dashboard"],
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
                        salvar_dataframe_db.invoke({
                            "df_json": df.to_json(orient="records", force_ascii=False, date_format="iso"),
                            "nome_tabela": tname,
                        })
                        total_tabs += 1
                    elif suff in (".xlsx", ".xls"):
                        xls = pd.ExcelFile(fpath)
                        for sheet in xls.sheet_names:
                            df = pd.read_excel(xls, sheet_name=sheet)
                            tname = norm_name(f"{fpath.stem}_{sheet}")
                            salvar_dataframe_db.invoke({
                                "df_json": df.to_json(orient="records", force_ascii=False, date_format="iso"),
                                "nome_tabela": tname,
                            })
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
    st.markdown("### Validação rápida de VR (amostragem)")
    st.caption("Executa um cálculo rápido para sinalizar casos que podem exigir validação: origem de valor, comunicados até dia 15 e linhas sem valor.")
    from datetime import date as _date
    hoje = _date.today()
    comp_val = st.text_input("Competência p/ validação (YYYY-MM)", value=f"{hoje.year}-{hoje.month:02d}", key="comp_valid")
    if st.button("Executar validação"):
        try:
            res = json.loads(calcular_financeiro_vr.run(comp_val))
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

# Página: Regras CCT (configurar quando OCR não extraiu)
elif page == "Regras CCT":
    st.subheader("Configuração de Regras (VR/VA) por Sindicato/UF")
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    rules_index_path = CHROMA_DIR / "rules_index.json"
    overrides_path = CHROMA_DIR / "rules_overrides.json"

    # Carrega index extraído e overrides existentes
    rules_index = []
    if rules_index_path.exists():
        try:
            rules_index = json.loads(rules_index_path.read_text(encoding="utf-8"))
        except Exception as e:
            st.warning(f"Falha ao ler rules_index.json: {e}")
    else:
        st.info("Ainda não há rules_index.json. Rode a ingestão em 'Importar CCTs'.")

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
    st.markdown("### Cadastrar/Editar Regra Manualmente")
    with st.form("form_override"):
        col1, col2 = st.columns(2)
        with col1:
            uf_in = st.text_input("UF", max_chars=2).upper()
            vr_in = st.text_input("VR (ex.: R$ 25,00)")
            dias_in = st.number_input("Dias (opcional)", min_value=0, max_value=31, value=0)
        with col2:
            sind_in = st.text_input("Sindicato (nome completo)")
            va_in = st.text_input("VA (ex.: R$ 180,00)")
        notas_in = st.text_area("Notas (opcional)", height=80)
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

# Página: Dashboard
elif page == "Dashboard":
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
    st.markdown("### Gerar VR Mensal (com fallback por UF → Estado)")
    from datetime import date as _date
    hoje = _date.today()
    colc, coln = st.columns(2)
    with colc:
        competencia = st.text_input("Competência (YYYY-MM)", value=f"{hoje.year}-{hoje.month:02d}")
    with coln:
        nome_arquivo = st.text_input("Nome do arquivo de exportação", value=f"VR_MENSAL_{hoje.month:02d}.{hoje.year}.xlsx")
    if st.button("Gerar Relatório VR"):
        try:
            # LangChain Tool expects positional tool_input
            res = json.loads(calcular_financeiro_vr.run(competencia))
            total_fmt = f"R$ {res['total_geral']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            st.success(f"Linhas: {res['linhas']} | Total: {total_fmt}")
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
                    "Baixar Relatório VR",
                    f,
                    file_name=export_path.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
        except Exception as e:
            st.error(f"Falha ao gerar VR: {e}")
