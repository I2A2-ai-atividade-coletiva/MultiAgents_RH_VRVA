from typing import Callable

from utils.prompt_loader import carregar_prompt
from agentes.especialista_dados import criar_agente_dados
from agentes.analista_cct import criar_agente_cct
from agentes.especialista_compliance import criar_agente_compliance
from agentes.coletor_cct import criar_agente_coletor_cct
from agentes.especialista_calculo import criar_agente_calculo
from agentes.especialista_vrva import criar_agente_vrva
from ferramentas.gerador_relatorio import salvar_planilha_final
from ferramentas.calculadora_beneficios import executar_calculo_deterministico, _find_col, _find_file_by_keywords
from datetime import date as _date
import pandas as pd
import json
import re
from pathlib import Path
from ferramentas.persistencia_db import carregar_dataframe_db, listar_tabelas_db, salvar_dataframe_db, DB_PATH
from ferramentas.leitor_arquivos import normalizar_nomes_sindicatos


def criar_agente_orquestrador() -> Callable[[str], str]:
    """
    Cria o agente "Gerente/Orquestrador": delega sequencialmente aos especialistas.
    """
    prompt = carregar_prompt("orquestrador")

    # Instancia especialistas (versões simples com LLM já integrados)
    agente_dados = criar_agente_dados()
    agente_coletor_cct = criar_agente_coletor_cct()
    agente_vrva = criar_agente_vrva()
    agente_compliance = criar_agente_compliance()
    agente_calculo = criar_agente_calculo()

    def executar(tarefa: str) -> str:
        # Infra de progresso: arquivo jsonl e prints marcados para o Dashboard
        pkg_root = Path(__file__).resolve().parent.parent
        outdir = pkg_root / "relatorios_saida"
        outdir.mkdir(parents=True, exist_ok=True)
        progress_path = outdir / "progresso_execucao.jsonl"
        status_path = outdir / "status.log"

        def write_status(msg: str):
            try:
                outdir.mkdir(parents=True, exist_ok=True)
                with status_path.open("a", encoding="utf-8") as fp:
                    fp.write(msg.strip() + "\n")
            except Exception:
                pass

        def emit_progress(agent: str, action: str, status: str, info: str | None = None):
            rec = {"agent": agent, "action": action, "status": status}
            if info:
                rec["info"] = info
            try:
                with progress_path.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass
            # Linha padronizada para parsing em tempo real no Streamlit
            print(f"::STEP::{agent}::{action}::{status}:: {info or ''}")
        cabecalho = [
            "[Orquestrador] Tarefa recebida.",
            "Sequência: Dados -> Coletor CCT -> VR/VA -> CCT -> Compliance -> Cálculo.",
        ]
        # Coleta de validações para exibição no Dashboard
        validacoes_execucao: list[str] = []

        def _primeiro_json_array(texto: str):
            try:
                # tentativa simples: achar primeiro '[' e último ']' subsequente
                s = str(texto)
                i = s.find('[')
                j = s.rfind(']')
                if i != -1 and j != -1 and j > i:
                    arr = json.loads(s[i:j+1])
                    return arr if isinstance(arr, list) else None
            except Exception:
                return None
            return None

        # Status inicial
        write_status("Iniciando o processo...")
        emit_progress("Especialista de Dados", "Consolidação de bases", "START")
        write_status("Agente em ação: Especialista de Dados - Consolidação de bases")
        print("Iniciando o trabalho do Especialista de Dados...")
        try:
            saida_dados = agente_dados(
                "Carregue e consolide as bases de entrada em um único DataFrame. Documente suposições."
            )
        except Exception as e:
            saida_dados = f"[Erro Dados] {e}"
            emit_progress("Especialista de Dados", "Consolidação de bases", "ERROR", str(e))
            write_status(f"Especialista de Dados - ERRO: {e}")
        else:
            validacoes_execucao.append("Dados ingeridos e consolidados")
            emit_progress("Especialista de Dados", "Consolidação de bases", "DONE")
            write_status("Especialista de Dados - Concluído")
        # Persistência tentativa: dados_consolidados
        try:
            arr = _primeiro_json_array(saida_dados)
            if arr is not None:
                dfjson = json.dumps(arr, ensure_ascii=False)
                salvar_dataframe_db(dfjson, "dados_consolidados")
                # Normaliza nomes de sindicatos e salva tabela normalizada
                try:
                    emit_progress("Especialista de Dados", "Normalizar sindicatos", "START")
                    dfjson_norm = normalizar_nomes_sindicatos(dfjson)
                    salvar_dataframe_db(dfjson_norm, "dados_consolidados_norm")
                    emit_progress("Especialista de Dados", "Normalizar sindicatos", "DONE")
                except Exception:
                    emit_progress("Especialista de Dados", "Normalizar sindicatos", "SKIP")
                    pass
        except Exception:
            pass

        # Detecta UFs presentes na base consolidada para filtrar CCT por estado
        ufs = {"AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN","RS","RO","RR","SC","SP","SE","TO"}
        def extrai_ufs(texto: str):
            encontrados = set()
            for m in re.finditer(r"\b([A-Z]{2})\b", texto.upper()):
                sigla = m.group(1)
                if sigla in ufs:
                    encontrados.add(sigla)
            return sorted(encontrados)

        # Coletor CCT: consolida rules_index em um resumo estruturado por (UF, Sindicato)
        emit_progress("Coletor CCT", "Consolidação CCT index", "START")
        write_status("Agente em ação: Coletor CCT - Consolidação CCT index")
        print("Iniciando o trabalho do Coletor CCT...")
        try:
            instr = "Consolide rules_index por UF/Sindicato e marque pendências. Retorne apenas um array JSON."
            saida_coletor = agente_coletor_cct(instr)
        except Exception as e:
            saida_coletor = f"[Erro Coletor CCT] {e}"
            emit_progress("Coletor CCT", "Consolidação CCT index", "ERROR", str(e))
            write_status(f"Coletor CCT - ERRO: {e}")
        else:
            validacoes_execucao.append("Resumo CCT consolidado")
            emit_progress("Coletor CCT", "Consolidação CCT index", "DONE")
            write_status("Coletor CCT - Concluído")
        # Persistência tentativa: regras_cct_resumo
        try:
            arr = _primeiro_json_array(saida_coletor)
            if arr is not None:
                dfjson = json.dumps(arr, ensure_ascii=False)
                salvar_dataframe_db(dfjson, "regras_cct_resumo")
        except Exception:
            pass

        # Especialista VR/VA: consolidação e resolução de conflitos após ingestão Docling
        emit_progress("Especialista VR/VA", "Resolver VR/VA por UF/Sindicato", "START")
        write_status("Agente em ação: Especialista VR/VA - Resolução de VR/VA")
        try:
            instr_vrva = "Consolide e resolva VR/VA com regras e confiança. Retorne apenas um array JSON."
            saida_vrva = agente_vrva(instr_vrva)
            emit_progress("Especialista VR/VA", "Resolver VR/VA por UF/Sindicato", "DONE")
            write_status("Especialista VR/VA - Concluído")
            validacoes_execucao.append("VR/VA resolvidos por UF/Sindicato")
            # Persistência tentativa: regras_cct_vrva_resolvidas_json (para Dashboard)
            try:
                arr = _primeiro_json_array(saida_vrva)
                if arr is not None:
                    dfjson = json.dumps(arr, ensure_ascii=False)
                    salvar_dataframe_db(dfjson, "regras_cct_vrva_resolvidas_json")
            except Exception:
                pass
        except Exception as e:
            saida_vrva = f"[Erro VR/VA] {e}"
            emit_progress("Especialista VR/VA", "Resolver VR/VA por UF/Sindicato", "ERROR", str(e))
            write_status(f"Especialista VR/VA - ERRO: {e}")

        emit_progress("Analista de CCT", "Consulta às CCTs", "START")
        write_status("Agente em ação: Analista de CCT - Consulta às CCTs")
        print("Iniciando o trabalho do Analista de CCT...")
        try:
            ufs_detectadas = extrai_ufs(str(saida_dados))
            agente_cct_local = criar_agente_cct(ufs=ufs_detectadas or None)
            consulta_cct = "Quais regras de VR/VA e proporcionais se aplicam no contexto informado?"
            saida_cct = agente_cct_local(consulta_cct)
        except Exception as e:
            saida_cct = f"[Erro CCT] {e}"
            emit_progress("Analista de CCT", "Consulta às CCTs", "ERROR", str(e))
            write_status(f"Analista de CCT - ERRO: {e}")
        else:
            validacoes_execucao.append("Regras CCT analisadas")
            emit_progress("Analista de CCT", "Consulta às CCTs", "DONE")
            write_status("Analista de CCT - Concluído")

        instr_compliance = (
            "Verifique aderência às políticas internas para elegibilidade e limites; liste pendências."
        )
        emit_progress("Especialista em Compliance", "Aplicar regras internas", "START")
        write_status("Agente em ação: Especialista em Compliance - Aplicar regras internas")
        print("Iniciando o trabalho do Especialista em Compliance...")
        try:
            saida_compliance = agente_compliance(instr_compliance)
        except Exception as e:
            saida_compliance = f"[Erro Compliance] {e}"
            emit_progress("Especialista em Compliance", "Aplicar regras internas", "ERROR", str(e))
            write_status(f"Especialista em Compliance - ERRO: {e}")
        else:
            validacoes_execucao.append("Compliance aplicado")
            emit_progress("Especialista em Compliance", "Aplicar regras internas", "DONE")
            write_status("Especialista em Compliance - Concluído")
        # Persistência tentativa: dados_compliance_ok
        try:
            arr = _primeiro_json_array(saida_compliance)
            if arr is not None:
                dfjson = json.dumps(arr, ensure_ascii=False)
                salvar_dataframe_db.invoke({
                    "df_json": dfjson,
                    "nome_tabela": "dados_compliance_ok",
                })
        except Exception:
            pass

        instr_calculo = (
            "Com base nos dados consolidados e nas regras das CCTs, calcule VR/VA finais e ressalte regras aplicadas."
        )
        emit_progress("Especialista em Cálculo", "Cálculo de VR/VA", "START")
        write_status("Agente em ação: Especialista em Cálculo - Cálculo de VR/VA")
        df_base_json = None
        try:
            # prioridade de base: compliance_ok -> consolidado_norm -> consolidado
            for key_tbl in ("dados_compliance_ok", "dados_consolidados_norm", "dados_consolidados"):
                try:
                    tmp = carregar_dataframe_db(key_tbl)
                    if tmp:
                        df_base_json = tmp
                        break
                except Exception:
                    pass
            # determinar competência (YYYY-MM) a partir da tarefa ou mês atual
            tarefa_upper = str(tarefa)
            import re as _re
            m = _re.search(r"\b(20\d{2})[-/\. ]?(0[1-9]|1[0-2])\b", tarefa_upper)
            if m:
                mes_ref = f"{m.group(1)}-{m.group(2)}"
            else:
                today = _date.today()
                mes_ref = f"{today.year}-{today.month:02d}"
            if not df_base_json:
                df_base_json = "[]"
            # se vazio, tenta fallback direto da planilha ATIVOS.xlsx
            try:
                _inp = json.loads(df_base_json)
            except Exception:
                _inp = []
            if isinstance(_inp, list) and len(_inp) == 0:
                try:
                    base_dir = Path(__file__).resolve().parent.parent
                    ativos_path = base_dir / "dados_entrada" / "ATIVOS.xlsx"
                    if ativos_path.exists():
                        df_fallback = pd.read_excel(ativos_path)
                        df_base_json = df_fallback.to_json(orient="records", force_ascii=False)
                        emit_progress("Cálculo Determinístico", "Fallback base ATIVOS.xlsx", "INFO", str(ativos_path.name))
                        _inp = json.loads(df_base_json)
                except Exception as _e:
                    emit_progress("Cálculo Determinístico", "Fallback base ATIVOS.xlsx", "ERROR", str(_e))
            # log quantidade de linhas de entrada
            try:
                emit_progress("Cálculo Determinístico", "Linhas de entrada", "INFO", f"{len(_inp)}")
            except Exception:
                pass
            # Enriquecimento pré-cálculo: incluir "somente Admissões (coluna D vazia)" como ativos
            try:
                base_dir = Path(__file__).resolve().parent.parent
                dados_dir = base_dir / "dados_entrada"
                # localizar arquivos relevantes
                f_ativos   = _find_file_by_keywords(str(dados_dir), ["ativos"]) 
                f_aprendiz = _find_file_by_keywords(str(dados_dir), ["aprend"]) 
                f_estagio  = _find_file_by_keywords(str(dados_dir), ["estag"]) 
                f_exterior = _find_file_by_keywords(str(dados_dir), ["exterior"]) 
                f_ferias   = _find_file_by_keywords(str(dados_dir), ["ferias"]) 
                f_afast    = _find_file_by_keywords(str(dados_dir), ["afast"]) 
                f_deslig   = _find_file_by_keywords(str(dados_dir), ["deslig"]) 
                f_adm      = _find_file_by_keywords(str(dados_dir), ["admiss"]) 

                def _read_any(p: str) -> pd.DataFrame:
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

                ativos   = _read_any(f_ativos)
                aprendiz = _read_any(f_aprendiz)
                estagio  = _read_any(f_estagio)
                exterior = _read_any(f_exterior)
                ferias   = _read_any(f_ferias)
                afast    = _read_any(f_afast)
                deslig   = _read_any(f_deslig)
                admis    = _read_any(f_adm)

                def _ids(df: pd.DataFrame) -> set[str]:
                    try:
                        if df is None or df.empty:
                            return set()
                        return set(map(str, df[df.columns[0]].astype(str).tolist()))
                    except Exception:
                        return set()

                ids_outros = set()
                for _df in (ativos, aprendiz, estagio, exterior, ferias, afast, deslig):
                    ids_outros |= _ids(_df)

                to_add: list[dict] = []
                if admis is not None and not admis.empty and len(admis.columns) >= 4:
                    id_adm_col = admis.columns[0]
                    col_d = admis.columns[3]  # quarta coluna (coluna D)
                    try:
                        s = admis[col_d].astype(str).str.strip().replace({"nan": "", "None": ""})
                    except Exception:
                        s = pd.Series(["" for _ in range(len(admis))])
                    ids_colD_blank = set(map(str, admis.loc[s == "", id_adm_col].astype(str).tolist()))
                    ids_adm = set(map(str, admis[id_adm_col].astype(str).tolist()))
                    only_in_adm = {mid for mid in ids_adm if mid not in ids_outros}
                    target_ids = sorted(list(only_in_adm & ids_colD_blank))
                    if target_ids:
                        nome_adm_col = _find_col(admis.columns, ["nome","colaborador","funcionario"]) 
                        sind_adm_col = _find_col(admis.columns, ["sindicato","sind"]) 
                        uf_adm_col   = _find_col(admis.columns, ["uf","estado","unidade_federativa"]) 
                        # mapa Estado->UF
                        from ferramentas.calculadora_beneficios import UF_MAP as _UF_MAP
                        rev_map = {v.lower(): k for k, v in _UF_MAP.items()}
                        for mid in target_ids:
                            rec = {"matricula": mid, "origem_base": "admiss"}
                            try:
                                if nome_adm_col:
                                    rec["nome"] = admis.loc[admis[id_adm_col].astype(str) == mid, nome_adm_col].iloc[0]
                            except Exception:
                                pass
                            try:
                                if sind_adm_col:
                                    rec["sindicato"] = admis.loc[admis[id_adm_col].astype(str) == mid, sind_adm_col].iloc[0]
                            except Exception:
                                pass
                            try:
                                if uf_adm_col:
                                    raw = str(admis.loc[admis[id_adm_col].astype(str) == mid, uf_adm_col].iloc[0])
                                    raw_u = raw.strip().upper()
                                    if len(raw_u) == 2:
                                        rec["UF"] = raw_u
                                    else:
                                        rec["UF"] = rev_map.get(raw.strip().lower())
                            except Exception:
                                pass
                            to_add.append(rec)

                # anexar ao df_base_json evitando duplicatas
                try:
                    base_df = pd.read_json(df_base_json or "[]", orient="records")
                except Exception:
                    base_df = pd.DataFrame()
                if to_add:
                    add_df = pd.DataFrame(to_add)
                    # evitar duplicidades: mantém quem já existe
                    if "matricula" in base_df.columns:
                        add_df = add_df[~add_df["matricula"].astype(str).isin(base_df["matricula"].astype(str))]
                    # alinhar colunas
                    for c in add_df.columns:
                        if c not in base_df.columns:
                            base_df[c] = None
                    base_df = pd.concat([base_df, add_df[base_df.columns]], ignore_index=True)
                    df_base_json = base_df.to_json(orient="records", force_ascii=False)
                    emit_progress("Cálculo Determinístico", "Inclusão ADM-only", "INFO", f"Linhas adicionadas: {len(add_df)}")
            except Exception as _e:
                # não impede o cálculo
                emit_progress("Cálculo Determinístico", "Inclusão ADM-only", "SKIP", str(_e))

            df_calc_json, validacoes_json = executar_calculo_deterministico(df_base_json, mes_ref)
            # log quantidade de linhas de saída
            try:
                _outp = json.loads(df_calc_json)
                emit_progress("Cálculo Determinístico", "Linhas de saída", "INFO", f"{len(_outp)}")
            except Exception:
                pass
            # Persiste resultado determinístico para geração do Excel
            try:
                salvar_dataframe_db(df_calc_json, "dados_calculo_final")
            except Exception:
                pass
            emit_progress("Cálculo Determinístico", "Processar base e aplicar regras", "DONE", f"Competência={mes_ref}")
            write_status("Cálculo Determinístico - Concluído")

            pkg_root = Path(__file__).resolve().parent.parent
            # Gera nome dinâmico com base em mes_ref (YYYY-MM -> MM.YYYY)
            try:
                _ano, _mes = mes_ref.split("-")
                nome_rel = f"VR_MENSAL_{_mes}.{_ano}.xlsx"
                nome_aba = f"VR Mensal {_mes}.{_ano}"
            except Exception:
                nome_rel = "VR_MENSAL.xlsx"
                nome_aba = "VR Mensal"
            out_path = str((pkg_root / "relatorios_saida" / nome_rel).as_posix())
            salvar_planilha_final(
                df_json=df_calc_json,
                caminho_saida=out_path,
                nome_aba_principal=nome_aba,
                validacoes_json=validacoes_json,
            )
            partes.append(f"\n[Relatório] Gerado em: {Path(out_path).resolve()}\n(Relativo: {out_path})")
            validacoes_execucao.append("Relatório Excel gerado")
        except Exception as e:
            # Fallback: tenta criar um Excel mínimo direto
            try:
                cols = [
                    "Matricula","Admissão","Sindicato do Colaborador","Competência","Dias",
                    "VALOR DIÁRIO VR","TOTAL","Custo empresa","Desconto profissional","OBS GERAL",
                ]
                df_min = pd.DataFrame(columns=cols)
                pkg_root = Path(__file__).resolve().parent.parent
                # Fallback mantém nome dinâmico também
                try:
                    _ano, _mes = mes_ref.split("-")
                    nome_rel = f"VR_MENSAL_{_mes}.{_ano}.xlsx"
                    nome_aba = f"VR Mensal {_mes}.{_ano}"
                except Exception:
                    nome_rel = "VR_MENSAL.xlsx"
                    nome_aba = "VR Mensal"
                out_path = pkg_root / "relatorios_saida" / nome_rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
                    df_min.to_excel(writer, index=False, sheet_name=nome_aba)
                partes.append(f"\n[Relatório] Gerado via fallback em: {out_path.resolve()}")
                validacoes_execucao.append("Relatório Excel gerado (fallback)")
            except Exception as e2:
                partes.append(f"\n[Relatório] Falha ao gerar (principal): {e}; Fallback também falhou: {e2}")

        # Persiste um resumo estruturado para o Dashboard
        try:
            pkg_root = Path(__file__).resolve().parent.parent
            outdir = pkg_root / "relatorios_saida"
            outdir.mkdir(parents=True, exist_ok=True)
            resumo = {
                "status": "sucesso",
                "validacoes": validacoes_execucao,
                "relatorio": str((pkg_root / "relatorios_saida" / nome_rel).resolve()),
            }
            (outdir / "resultado_execucao.json").write_text(
                json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            emit_progress("Orquestrador", "Finalização", "DONE")
            write_status("Processo concluído com sucesso.")
        except Exception:
            pass

        return f"PROMPT_ORQUESTRADOR:\n{prompt}\n\nTAREFA:\n{tarefa}\n\nRESULTADO:\n" + "\n\n".join(partes)

    return executar
