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
from utils.config import get_competencia, set_competencia
from utils.config import get_llm
from utils.prompt_loader import carregar_prompt

# Chat com CCTs 
try:
    from langchain_community.document_loaders.pdf import PyPDFLoader  # novo caminho
except Exception:
    try:
        from langchain_community.document_loaders import PyPDFLoader  # caminho alternativo
    except Exception:
        PyPDFLoader = None
try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except Exception:
    RecursiveCharacterTextSplitter = None
try:
    from langchain_community.vectorstores.faiss import FAISS
except Exception:
    FAISS = None
try:
    from langchain_openai import OpenAIEmbeddings
except Exception:
    OpenAIEmbeddings = None
try:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
except Exception:
    GoogleGenerativeAIEmbeddings = None
try:
    from langchain_community.embeddings.huggingface import HuggingFaceEmbeddings
except Exception:
    HuggingFaceEmbeddings = None
try:
    from langchain.schema import HumanMessage
except Exception:
    HumanMessage = None

def _format_history_for_prompt(items):
    """Serialize Q/A history to a plain text conversation (oldest -> newest)."""
    if not items:
        return ""
    lines = []
    for it in reversed(items):  # oldest first for model context
        q = (it.get('q') or '').strip()
        a = (it.get('a') or '').strip()
        if q:
            lines.append(f"Human: {q}")
        if a:
            lines.append(f"AI: {a}")
    return "\n".join(lines)

BASE_DIR = Path(__file__).resolve().parent
DADOS_DIR = BASE_DIR / "dados_entrada"
CCTS_DIR = BASE_DIR / "base_conhecimento" / "ccts_pdfs"
RELATORIOS_DIR = BASE_DIR / "relatorios_saida"
PROMPTS_DIR = BASE_DIR / "prompts"
CHROMA_DIR = BASE_DIR / "base_conhecimento" / "chromadb"
FAISS_CCTS_DIR = BASE_DIR / "base_conhecimento" / "faiss_ccts"
RULES_INDEX_ROOT = BASE_DIR / "base_conhecimento" / "rules_index.json"
MODELS_DIR = BASE_DIR / "models"
FAISS_TABELAS_DIR = BASE_DIR / "base_conhecimento" / "faiss_tabelas"

DADOS_DIR.mkdir(parents=True, exist_ok=True)
CCTS_DIR.mkdir(parents=True, exist_ok=True)
RELATORIOS_DIR.mkdir(parents=True, exist_ok=True)
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Automação RH - Multiagentes", layout="wide")

# Reduce noisy logs (PyTorch/Transformers) seen as 'Examining the path of torch.classes...'
import logging, warnings
for _lg in ["torch", "transformers", "sentence_transformers", "faiss", "langchain"]:
    try:
        logging.getLogger(_lg).setLevel(logging.ERROR)
    except Exception:
        pass
try:
    warnings.filterwarnings("ignore", message=r"Examining the path of torch\.classes.*")
except Exception:
    pass

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
    p { font-feature-settings: "ss01" on; }
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
            "0-Mês Competência",
            "1-Importar Relatórios Base",
            "2-Importar CCTs",
            "3-Validação de Regras CCT",
            "4-Cadastro de Feriados",
            "5-Prompts",
            "6-Notificações",
            "7-Dados Finais",
        ],
        index=0,
    )
# ---------------- Sidebar: LLM & API Keys ----------------
with st.sidebar:
    st.markdown("### Configuração de LLM & APIs")
    # Provider
    prov_default = os.environ.get("LLM_PROVIDER", "google").lower()
    provider = st.selectbox("Provedor LLM", ["google", "groq"], index=0 if prov_default=="google" else 1)
    # Modelos e temperatura
    if provider == "google":
        model_default = os.environ.get("GENAI_MODEL", "gemini-1.5-pro")
        model = st.text_input("Modelo (Google)", value=model_default)
        api_key_default = os.environ.get("GOOGLE_API_KEY", "")
        api_key = st.text_input("GOOGLE_API_KEY", value=api_key_default, type="password")
    else:
        model_default = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
        model = st.text_input("Modelo (Groq)", value=model_default)
        api_key_default = os.environ.get("GROQ_API_KEY", "")
        api_key = st.text_input("GROQ_API_KEY", value=api_key_default, type="password")
    temperature = st.slider("Temperatura", min_value=0.0, max_value=1.0, value=float(os.environ.get("GENAI_TEMPERATURE", "0.2")), step=0.05)
    st.caption("Ajuste de sessão: os valores serão aplicados imediatamente nesta execução.")
    if st.button("Aplicar configurações"):
        # Provider
        os.environ["LLM_PROVIDER"] = provider
        if provider == "google":
            os.environ["GENAI_MODEL"] = model
            if api_key:
                os.environ["GOOGLE_API_KEY"] = api_key
        else:
            os.environ["GROQ_MODEL"] = model
            if api_key:
                os.environ["GROQ_API_KEY"] = api_key
        os.environ["GENAI_TEMPERATURE"] = str(temperature)
        st.success("Configurações aplicadas nesta sessão.")


# Página: Mês Competência (global)
if page == "0-Mês Competência":
    st.subheader("0.1 Definir mês/ano e janela de competência")
    st.caption("A janela de competência será usada em todo o cálculo: de {dia_início} do mês anterior até {dia_fim} do mês de referência.")

    cfg = get_competencia() or {}
    import calendar as _cal
    from datetime import date as _date
    today = _date.today()
    cur_year = int(cfg.get("year") or today.year)
    cur_month = int(cfg.get("month") or today.month)
    start_prev = int(cfg.get("start_day_prev") or 1)
    end_ref = int(cfg.get("end_day_ref") or _cal.monthrange(cur_year, cur_month)[1])

    col1, col2 = st.columns(2)
    with col1:
        year = st.number_input("Ano (YYYY)", min_value=2000, max_value=2100, value=cur_year)
    with col2:
        month = st.number_input("Mês (1-12)", min_value=1, max_value=12, value=cur_month)

    # Validar dias disponíveis para meses anterior e de referência
    if month == 1:
        py, pm = int(year) - 1, 12
    else:
        py, pm = int(year), int(month) - 1
    max_prev = _cal.monthrange(py, pm)[1]
    max_ref = _cal.monthrange(int(year), int(month))[1]

    c3, c4 = st.columns(2)
    with c3:
        start_day_prev = st.number_input(f"Dia início no mês anterior (1..{max_prev})", min_value=1, max_value=max_prev, value=min(start_prev, max_prev))
    with c4:
        end_day_ref = st.number_input(f"Dia fim no mês de referência (1..{max_ref})", min_value=1, max_value=max_ref, value=min(end_ref, max_ref))

    # Mostrar janela resultante
    try:
        start_date = _date(py, pm, int(start_day_prev))
        end_date = _date(int(year), int(month), int(end_day_ref))
        st.info(f"Janela configurada: {start_date} → {end_date}")
    except Exception as e:
        st.error(f"Janela inválida: {e}")

    if st.button("Salvar competência"):
        try:
            set_competencia(int(year), int(month), int(start_day_prev), int(end_day_ref))
            st.success("Competência salva com sucesso. Essa configuração será usada em todo o cálculo determinístico.")
        except Exception as e:
            st.error(f"Falha ao salvar competência: {e}")

    st.divider()
    st.markdown("### 0.2 Configuração atual")
    st.code(json.dumps(get_competencia(), ensure_ascii=False, indent=2))

# Página: Importar Relatórios Base
elif page == "1-Importar Relatórios Base":
    st.subheader(" 1.1 Importação de Relatórios Base")
    st.caption("Arquivos base (Excel/CSV) — faça o upload para a pasta dados_entrada/.")

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

    st.markdown("### 1.2 Carregar TODOS os arquivos de dados_entrada/ para o Banco (uma tabela por arquivo/aba)")
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

    st.divider()
    st.markdown("### 1.3 Validação de Qualidade de Dados")
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
    st.markdown("### 1.4 Validação rápida inserção de dados no banco de dados")
    st.caption("Executa um cálculo rápido para sinalizar casos que podem exigir validação: origem de valor, comunicados até dia 15 e linhas sem valor.")
    from datetime import date as _date
    hoje = _date.today()
    # Seletor de competência via calendário (usa ano-mês do valor selecionado)
    _cfg = get_competencia() or {}
    if _cfg.get("year") and _cfg.get("month"):
        _default_comp = _date(int(_cfg["year"]), int(_cfg["month"]), 1)
    else:
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
elif page == "2-Importar CCTs":
    st.subheader("2.1 Importação de CCTs (PDF)")
    cct_files = st.file_uploader(
        "CCTs (PDF)", accept_multiple_files=True, type=["pdf"], key="pdfs"
    )
    if st.button("Salvar CCT PDFs"):
        for f in cct_files or []:
            out_path = CCTS_DIR / f.name
            with open(out_path, "wb") as out:
                out.write(f.read())
        st.success(f"Salvos em {CCTS_DIR}")
    if st.button("Rodar ingestão de dados CCT"):
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


    st.divider()
    st.subheader("2.2 Chat com CCTs (consulta assistida)")
    st.caption("Converse com o conteúdo das CCTs em base_conhecimento/ccts_pdfs/. O índice local é salvo em base_conhecimento/faiss_ccts.")

    def _ccts_list():
        return sorted([p.name for p in CCTS_DIR.glob("*.pdf")])

    def _get_embeddings():
        # Usa embeddings locais do diretório models/ (SentenceTransformers via HuggingFaceEmbeddings)
        if HuggingFaceEmbeddings is None:
            st.error("Pacote de embeddings locale (HuggingFaceEmbeddings) não disponível. Instale langchain-community e sentence-transformers.")
            st.stop()
        model_id = os.getenv("HUGGINGFACE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        local_path = MODELS_DIR / model_id
        if not local_path.exists():
            st.warning(f"Modelo de embeddings não encontrado em '{local_path}'.")
            if st.button("Baixar modelo de embeddings agora"):
                try:
                    proc = subprocess.Popen(
                        [sys.executable, str(BASE_DIR / "download_model.py")],
                        cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
                    )
                    log = st.empty()
                    lines = []
                    for line in proc.stdout:
                        lines.append(line.rstrip())
                        log.code("\n".join(lines))
                    proc.wait()
                    if proc.returncode == 0 and local_path.exists():
                        st.success("Modelo baixado com sucesso. Prosseguindo...")
                    else:
                        st.error("Falha ao baixar o modelo. Verifique o console acima.")
                        st.stop()
                except Exception as e:
                    st.error(f"Erro ao executar download_model.py: {e}")
                    st.stop()
            else:
                st.stop()
        try:
            return HuggingFaceEmbeddings(model_name=str(local_path), model_kwargs={"device": "cpu"})
        except Exception as e:
            st.error(f"Falha ao carregar embeddings locais: {e}")
            st.stop()

    def _build_or_load_faiss(force=False):
        FAISS_CCTS_DIR.mkdir(parents=True, exist_ok=True)
        emb = _get_embeddings()
        index_path = FAISS_CCTS_DIR / "index"
        if not force and index_path.exists():
            try:
                return FAISS.load_local(str(index_path), embeddings=emb, allow_dangerous_deserialization=True)
            except Exception:
                pass
        # construir
        docs_all = []
        for pdf in CCTS_DIR.glob("*.pdf"):
            try:
                loader = PyPDFLoader(str(pdf))
                docs = loader.load()
                splitter = RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=200)
                chunks = splitter.split_documents(docs)
                for i, d in enumerate(chunks):
                    d.metadata["source"] = pdf.name
                    d.metadata["doc_id"] = i
                docs_all.extend(chunks)
            except Exception as e:
                st.warning(f"Falha ao ler {pdf.name}: {e}")
        if not docs_all:
            st.info("Nenhum PDF em ccts_pdfs/.")
            st.stop()
        vs = FAISS.from_documents(docs_all, embedding=emb)
        vs.save_local(str(index_path))
        return vs

    c1, c2 = st.columns([1,1])
    with c1:
        if st.button("Indexar/Atualizar CCTs"):
            try:
                _build_or_load_faiss(force=True)
                st.success("Índice atualizado com sucesso.")
            except Exception as e:
                st.error(f"Falha ao indexar: {e}")
    with c2:
        arquivos = _ccts_list()
        st.write("Arquivos em CCTs:", arquivos or "(vazio)")

    # chat simples
    try:
        vs = _build_or_load_faiss(force=False)
        retriever = vs.as_retriever(search_kwargs={"k": 4})
    except Exception as e:
        st.error(f"Falha ao carregar índice: {e}")
        retriever = None

    if retriever is not None:
        # Histórico acima do input (ordem decrescente)
        st.markdown("**Histórico**")
        c_hist = st.session_state.setdefault('cct_chat_history', [])  # lista de dicts {q,a,srcs}
        if c_hist:
            for item in c_hist:
                st.markdown(f"- Pergunta: {item.get('q','')}")
                st.markdown(f"- Resposta: {item.get('a','')}")
                srcs = item.get('srcs') or []
                if srcs:
                    st.caption("Fontes: " + ", ".join(srcs))
            st.divider()

        with st.form("cct_chat_form"):
            pergunta = st.text_input("Pergunte algo sobre as CCTs (ex.: qual o VR diário para RJ?)", value="")
            enviar = st.form_submit_button("Enviar")
        if enviar and pergunta.strip():
            try:
                docs = retriever.get_relevant_documents(pergunta)
                contexto = "\n\n".join([d.page_content for d in docs])
                fontes = sorted({(d.metadata or {}).get("source", "?") for d in docs})
                llm = get_llm()
                _tmpl_cct = carregar_prompt("chat_cct")
                prompt = _tmpl_cct.format(
                    context=contexto,
                    chat_history=_format_history_for_prompt(c_hist),
                    question=pergunta,
                )
                resp = llm.invoke([HumanMessage(content=prompt)])
                answer = getattr(resp, "content", str(resp))
                # Prepend no histórico (mais recente no topo)
                c_hist.insert(0, {"q": pergunta, "a": answer, "srcs": list(fontes)})
                st.session_state['cct_chat_history'] = c_hist
                st.experimental_rerun()
            except Exception as e:
                st.error(f"Falha na conversa: {e}")

# Página: Feriados
elif page == "4-Cadastro de Feriados":
    st.subheader("4.1 Cadastro de Feriados (para cálculo de dias úteis)")
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

    st.markdown("### 4.2 Lista de Feriados")
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
    st.markdown("### 4.3 Atualizar feriados automaticamente (feriados.com.br)")
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
elif page == "3-Validação de Regras CCT":
    st.subheader("3.1 Informações de Regras CCT")
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
        st.markdown("**Regras manuais - Overrides (importação ou cadastro):**")
        if overrides:
            df_over = pd.DataFrame([
                {"UF": k.split("::",1)[0], "Sindicato": k.split("::",1)[1], **v}
                for k, v in overrides.items()
            ])
            st.dataframe(df_over, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum override salvo ainda.")

    st.divider()
    st.markdown("### 3.2 Regras importadas (OCR/index)")
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
    st.markdown("### 3.3 Importação planilha")
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
    st.markdown("### 3.3 Cadastro manual")
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
    st.markdown("### 3.4 Importar Overrides de CSV (opcional)")
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
    st.markdown("### 3.5 Validação de Compliance (OCR x Sistema)")
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
    st.markdown("### 3.6 Resumo consolidado (rules_index.json consolidado)")
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
elif page == "5-Prompts":
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
elif page == "6-Notificações":
    st.subheader("6.1 Notificações e Sinalizações")
    st.caption("Diferenças entre valor VR do relatório base (estado) e CCT, matrículas sem admissão, e exclusões por regra.")

    from datetime import date as _date
    hoje = _date.today()
    # Calendar popover for competência (use the picked date's year-month)
    _cfg2 = get_competencia() or {}
    if _cfg2.get("year") and _cfg2.get("month"):
        default_comp = _date(int(_cfg2["year"]), int(_cfg2["month"]), 1)
    else:
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

        # New metric computation: Admissions-only entries with blank column D
        try:
            admissao_so_na_planilha_sem_obs = 0
            if admis is not None and not admis.empty:
                # Union of IDs from other bases
                ids_outros = set()
                try:
                    ids_outros |= _ids(ativos)
                except Exception:
                    pass
                try:
                    ids_outros |= ids_ap
                    ids_outros |= ids_es
                    ids_outros |= ids_ex
                except Exception:
                    pass
                try:
                    ids_outros |= _ids(ferias)
                except Exception:
                    pass
                try:
                    ids_outros |= _ids(afast)
                except Exception:
                    pass
                try:
                    ids_outros |= _ids(deslig)
                except Exception:
                    pass

                # Determine Admissions IDs and check column D (4th column) blank
                admis_ids = _ids(admis)
                if len(admis.columns) >= 4:
                    col_d = admis.columns[3]
                    s = admis[col_d].astype(str).str.strip().replace({"nan": "", "None": ""})
                    ids_colD_blank = set(map(str, admis.loc[s == "", admis.columns[0]].astype(str).tolist()))
                else:
                    ids_colD_blank = set()

                only_in_adm = {mid for mid in admis_ids if mid not in ids_outros}
                target_ids = only_in_adm & ids_colD_blank
                admissao_so_na_planilha_sem_obs = int(len(target_ids))
        except Exception:
            admissao_so_na_planilha_sem_obs = 0

        # Grouped metrics layout
        st.markdown("#### Funcionários")
        popA, popB, popC, popD = st.columns(4)
        with popA:
            st.metric("Ativos", value=total_ativos)
        with popB:
            st.metric("Aprendiz", value=qtd_aprendiz)
        with popC:
            st.metric("Estagiário", value=qtd_estagio)
        with popD:
            st.metric("Exterior", value=qtd_exterior)

        st.markdown("#### Ocorrências")
        occA, occB, occC = st.columns(3)
        with occA:
            st.metric("Férias", value=qtd_ferias)
        with occB:
            st.metric("Afastados/Licenças", value=qtd_afast)
        with occC:
            st.metric("Atendimentos/OBS", value=qtd_atend)

        st.markdown("#### Admissões")
        admA, admB, admC, admD = st.columns(4)
        with admA:
            st.metric("Matrículas sem admissão", value=sem_adm)
        with admB:
            st.metric("Admitidos mês", value=qtd_adm_mes)
        with admC:
            st.metric("Admitidos mês anterior", value=qtd_adm_mes_prev)
        with admD:
            st.metric(
                "Só na Admissão (col. D vazia)",
                value=admissao_so_na_planilha_sem_obs,
                help="Quantidade presente apenas na planilha de admissão e com coluna D (4ª coluna) em branco"
            )

        st.markdown("#### Desligamentos")
        desA, desB, desC, desD = st.columns(4)
        with desA:
            st.metric("Desligados Geral", value=qtd_deslig_geral)
        with desB:
            st.metric("Até 15 (OK)", value=qtd_deslig_ate15_ok, help="Excluir da compra")
        with desC:
            st.metric("Até 15 (sem OK)", value=qtd_deslig_ate15_sem_ok, help="Comprar integral")
        with desD:
            st.metric("16..fim", value=qtd_deslig_16a_fim, help="Recarga cheia; desconto proporcional em rescisão")

        # Compact summary table with download
        with st.expander("Resumo em tabela", expanded=False):
            _summary = [
                {"Métrica": "Ativos", "Valor": total_ativos},
                {"Métrica": "Aprendiz", "Valor": qtd_aprendiz},
                {"Métrica": "Estagiário", "Valor": qtd_estagio},
                {"Métrica": "Exterior", "Valor": qtd_exterior},
                {"Métrica": "Férias", "Valor": qtd_ferias},
                {"Métrica": "Afastados/Licenças", "Valor": qtd_afast},
                {"Métrica": "Atendimentos/OBS", "Valor": qtd_atend},
                {"Métrica": "Matrículas sem admissão", "Valor": sem_adm},
                {"Métrica": "Admitidos mês", "Valor": qtd_adm_mes},
                {"Métrica": "Admitidos mês anterior", "Valor": qtd_adm_mes_prev},
                {"Métrica": "Só na Admissão (col. D vazia)", "Valor": admissao_so_na_planilha_sem_obs},
                {"Métrica": "Desligados Geral", "Valor": qtd_deslig_geral},
                {"Métrica": "Desligados até 15 (OK)", "Valor": qtd_deslig_ate15_ok},
                {"Métrica": "Desligados até 15 (sem OK)", "Valor": qtd_deslig_ate15_sem_ok},
                {"Métrica": "Desligados 16..fim", "Valor": qtd_deslig_16a_fim},
            ]
            _df_sum = pd.DataFrame(_summary)
            st.dataframe(_df_sum, use_container_width=True, hide_index=True)
            st.download_button(
                "Baixar resumo (CSV)",
                data=_df_sum.to_csv(index=False).encode("utf-8"),
                file_name="notificacoes_resumo.csv",
                mime="text/csv",
            )

        st.divider()
        st.markdown("### 5.2 Diferenças de VR — Base (Estado) x CCT")
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
elif page == "7-Dados Finais":
    # 7.0 Pipeline & Agentes (visão e artefatos)
    st.subheader("7.0 Pipeline & Agentes")
    st.markdown(
        """
        - [1] Agente de Dados (Ingest/Qualidade): atua na página "1-Importar Relatórios Base" (validação 1.3) e ao preparar as bases para o cálculo.
        - [2] Agente CCT (OCR/Index): atua nas páginas "2-Importar CCTs" e "3-Validação de Regras CCT" para extrair e cadastrar regras. No cálculo, quando não há Override, `resolve_cct_rules` consulta OCR/index.
        - [3] Agente VR/VA Resolver: coordena a resolução de VR/VA (Overrides → OCR/Index → Retrieval) durante o cálculo determinístico.
        - [4] Agente Compliance: gera verificações e observações (exibidas em artefatos abaixo quando disponíveis).
        - [5] Cálculo Determinístico: aplica janela de competência, exclusões e proporcionalidades para gerar dias e totais.
        """
    )
    with st.expander("Ver artefatos recentes dos agentes (se disponíveis)"):
        try:
            regras_txt = RELATORIOS_DIR / "regras.txt"
            if regras_txt.exists():
                st.markdown("**Regras CCT (resumo do agente):**")
                st.code(regras_txt.read_text(encoding="utf-8"), language="markdown")
            else:
                st.info("Artefato 'regras.txt' não encontrado.")
        except Exception:
            st.warning("Falha ao carregar 'regras.txt'.")
        try:
            compliance_txt = RELATORIOS_DIR / "compliance.txt"
            if compliance_txt.exists():
                st.markdown("**Compliance (observações do agente):**")
                st.code(compliance_txt.read_text(encoding="utf-8"), language="markdown")
            else:
                st.info("Artefato 'compliance.txt' não encontrado.")
        except Exception:
            st.info("Banco ainda não criado.")

    # 7.1 Gerar Benefícios Mensais (unificado)
    st.divider()
    st.subheader("7.1 Gerar Benefícios Mensais (VR / VA / Consolidado)")
    from datetime import date as _date
    hoje = _date.today()
    colc, colp, coln = st.columns([1,1,2])
    with colc:
        _cfg3 = get_competencia() or {}
        if _cfg3.get("year") and _cfg3.get("month"):
            _yy, _mm = int(_cfg3["year"]), int(_cfg3["month"]) 
        else:
            _yy, _mm = hoje.year, hoje.month
        if "compet_input" not in st.session_state:
            st.session_state["compet_input"] = f"{_yy}-{_mm:02d}"
        competencia = st.text_input("Competência (YYYY-MM)", key="compet_input")
        def _apply_global_comp():
            _cfg4 = get_competencia() or {}
            if _cfg4.get("year") and _cfg4.get("month"):
                st.session_state["compet_input"] = f"{int(_cfg4['year'])}-{int(_cfg4['month']):02d}"
            else:
                st.session_state["compet_input"] = f"{hoje.year}-{hoje.month:02d}"
        st.button("Aplicar global", help="Usar competência definida em 0-Mês Competência", on_click=_apply_global_comp)
    with colp:
        produto = st.selectbox("Produto", ["VR", "VA", "CONSOLIDADO"], index=0)
    with coln:
        default_prefix = "VR_MENSAL" if produto=="VR" else ("VA_MENSAL" if produto=="VA" else "BENEFICIOS_MENSAL_CONSOLIDADO")
        try:
            _yyi, _mmi = competencia.split("-")
            _yyi = int(_yyi); _mmi = int(_mmi)
        except Exception:
            _yyi, _mmi = _yy, _mm
        nome_arquivo = st.text_input("Nome do arquivo de exportação", value=f"{default_prefix}_{_mmi:02d}.{_yyi}.xlsx")
    st.markdown("#### Base de valores para VR/VA")
    base_opt = st.radio(
        "Selecione a base a utilizar",
        ["CCT Padrão", "Importação planilha"],
        index=0,
        help="CCT Padrão usa regras resolvidas (inclui cadastro manual/overrides). Importação planilha usa arquivo Estado/Valor em dados_entrada/.")
    if st.button("Executar e Gerar Relatório"):
        try:
            # Validar seleção e setar modo via variável de ambiente para o cálculo
            mode = "CCT" if base_opt.startswith("CCT") else "MANUAL"
            os.environ["VRVA_VAL_BASE"] = mode
            if mode == "MANUAL":
                # localizar arquivo e validar colunas
                base_file = None
                for p in sorted(DADOS_DIR.glob("*.*")):
                    if any(k in p.name.lower() for k in ["base","valor","estado","uf","sindicato"]):
                        base_file = p
                        break
                if not base_file:
                    st.error("Modo 'Importação planilha' selecionado, mas nenhuma planilha de Estado/Valor foi encontrada em dados_entrada/.")
                    st.stop()
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
            # Chamada unificada do cálculo determinístico
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
                    with open(res["saida_xlsx"], "rb") as src, open(custom_path, "wb") as dst:
                        dst.write(src.read())
                    export_path = custom_path
            except Exception:
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

    # 7.2 Banco de Dados (SQLite)
    st.divider()
    st.subheader("7.2 Banco de Dados (SQLite)")
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

    # 7.3 Chat com Dados Importados (tabelas)
    st.divider()
    st.subheader("7.3 Chat com Dados Importados (tabelas)")
    st.caption("Converse com o conteúdo dos arquivos em dados_entrada/ (CSV/XLSX). Um índice FAISS é criado em base_conhecimento/faiss_tabelas.")

    def _build_or_load_faiss_tables(force: bool=False):
        FAISS_TABELAS_DIR.mkdir(parents=True, exist_ok=True)
        emb = None
        try:
            # Reaproveita a mesma estratégia local de embeddings (força CPU)
            emb = HuggingFaceEmbeddings(
                model_name=str((MODELS_DIR / os.getenv("HUGGINGFACE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")).resolve()),
                model_kwargs={"device": "cpu"}
            )
        except Exception as e:
            st.error(f"Falha ao carregar embeddings locais: {e}")
            st.stop()
        index_path = FAISS_TABELAS_DIR / "index"
        # Detectar mudanças no conjunto de arquivos e forçar rebuild quando necessário
        current_files = sorted([p.name for p in DADOS_DIR.glob("*.*") if p.suffix.lower() in (".csv", ".xlsx")])
        if not force and index_path.exists():
            if st.session_state.get('tabelas_index_files') != current_files:
                force = True
            try:
                return FAISS.load_local(str(index_path), embeddings=emb, allow_dangerous_deserialization=True)
            except Exception:
                pass
        # Construir a partir de dados_entrada/
        docs_all = []
        splitter = RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=200) if RecursiveCharacterTextSplitter else None
        for f in sorted(DADOS_DIR.glob("*.*")):
            suff = f.suffix.lower()
            if suff not in (".csv", ".xlsx"):
                continue
            try:
                if suff == ".csv":
                    # Ler como texto para preservar zeros à esquerda e formatos
                    df_map = {"__DEFAULT__": pd.read_csv(f, dtype=str)}
                else:
                    # Ler TODAS as abas da planilha
                    df_map = pd.read_excel(f, sheet_name=None, engine="openpyxl", dtype=str)
            except Exception as e:
                st.warning(f"Falha ao ler {f.name}: {e}")
                continue
            # converter cada linha em texto auditável (por aba)
            for sheet_name, df_ in df_map.items():
                if df_ is None:
                    continue
                # Normalizar colunas
                try:
                    df_ = df_.fillna("")
                except Exception:
                    pass
                for idx, row in df_.iterrows():
                    try:
                        parts = []
                        for c in df_.columns:
                            val = row.get(c)
                            if val is None or (isinstance(val, float) and pd.isna(val)):
                                continue
                            sval = str(val).strip()
                            if not sval:
                                continue
                            parts.append(f"{c}: {sval}")
                        text = f"Fonte: {f.name}{'' if sheet_name=='__DEFAULT__' else f'#{sheet_name}'} | Linha: {idx}\n" + "\n".join(parts)
                        if not parts:
                            continue
                        # dividir se necessário
                        if splitter:
                            chunks = splitter.split_text(text)
                            for j, ch in enumerate(chunks):
                                docs_all.append({"page_content": ch, "metadata": {"source": f"{f.name}{'' if sheet_name=='__DEFAULT__' else f'#{sheet_name}'}", "row": idx, "chunk": j}})
                        else:
                            docs_all.append({"page_content": text, "metadata": {"source": f"{f.name}{'' if sheet_name=='__DEFAULT__' else f'#{sheet_name}'}", "row": idx}})
                    except Exception:
                        continue
        if not docs_all:
            st.info("Nenhum CSV/XLSX válido em dados_entrada/.")
            st.stop()
        # Converter dicts em Document-like para FAISS
        from types import SimpleNamespace
        docs_lang = [SimpleNamespace(page_content=d["page_content"], metadata=d["metadata"]) for d in docs_all]
        vs = FAISS.from_documents(docs_lang, embedding=emb)
        vs.save_local(str(index_path))
        # Persistir arquivos indexados na sessão para detectar mudanças
        st.session_state['tabelas_index_files'] = current_files
        return vs

    c1, c2 = st.columns([1,1])
    with c1:
        if st.button("Indexar/Atualizar Dados Importados"):
            try:
                _build_or_load_faiss_tables(force=True)
                st.success("Índice de dados recriado/atualizado.")
            except Exception as e:
                st.error(f"Falha ao indexar dados: {e}")
    with c2:
        files = [p.name for p in DADOS_DIR.glob("*.*") if p.suffix.lower() in (".csv", ".xlsx")]
        st.write("Arquivos detectados:", files or "(vazio)")

    try:
        vs_tbl = _build_or_load_faiss_tables(force=False)
        retriever_tbl = vs_tbl.as_retriever(search_kwargs={"k": 12})
    except Exception as e:
        st.error(f"Falha ao carregar índice de dados: {e}")
        retriever_tbl = None

    if retriever_tbl is not None:
        # Histórico acima do input (ordem decrescente)
        st.markdown("**Histórico**")
        d_hist = st.session_state.setdefault('data_chat_history', [])
        if d_hist:
            for item in d_hist:
                st.markdown(f"- Pergunta: {item.get('q','')}")
                st.markdown(f"- Resposta: {item.get('a','')}")
                srcs = item.get('srcs') or []
                if srcs:
                    st.caption("Fontes: " + ", ".join(srcs))
            st.divider()

        with st.form("dados_chat_form"):
            q = st.text_input("Pergunte algo sobre os dados importados (ex.: quantos desligados com OK até dia 15?)", value="")
            sb = st.form_submit_button("Perguntar")
        if sb and q.strip():
            try:
                docs = retriever_tbl.get_relevant_documents(q)
                contexto = "\n\n".join([d.page_content for d in docs])
                fontes = sorted({(d.metadata or {}).get("source", "?") for d in docs})
                llm = get_llm()
                _tmpl_dados = carregar_prompt("chat_dados")
                prompt = _tmpl_dados.format(
                    context=contexto,
                    chat_history=_format_history_for_prompt(d_hist),
                    question=q,
                )
                if HumanMessage:
                    resp = llm.invoke([HumanMessage(content=prompt)])
                    answer = getattr(resp, "content", str(resp))
                else:
                    answer = str(llm.invoke(prompt))
                # Prepend no histórico
                d_hist.insert(0, {"q": q, "a": answer, "srcs": list(fontes)})
                st.session_state['data_chat_history'] = d_hist
                st.experimental_rerun()
            except Exception as e:
                st.error(f"Falha ao responder: {e}")
