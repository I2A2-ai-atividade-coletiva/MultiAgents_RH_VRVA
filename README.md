# Automação RH - Agentes

Sistema multiagentes para cálculo de VR/VA orquestrado por um "Gerente" (orquestrador) e especialistas, com painel em Streamlit para operação e revisão.

## Estrutura
Pastas principais neste repositório:
- `dados_entrada/`: arquivos base (Excel/CSV) enviados pelo usuário
- `relatorios_saida/`: relatórios e artefatos gerados (XLSX/CSV/JSON)
- `ferramentas/`: ferramentas de cálculo e utilitários (ex.: `calculadora_beneficios.py`)
- `utils/`: helpers (calendário, carregamento de prompts, config)
- `agentes/` e `prompts/`: definição dos agentes e prompts

## Setup
1) Crie o ambiente virtual:
```
python3 -m venv .venv
source .venv/bin/activate  # Linux/Mac
```
2) Instale dependências:
```
pip install -r requirements.txt
```
3) Variáveis de ambiente e modelo (dotenv):
   - Copie `.env.example` para `.env` e preencha:
```
cp .env.example .env
```
   - Campos:
     - `GOOGLE_API_KEY`: chave da API do Google Gemini
     - `GENAI_MODEL`: ex. `gemini-2.5-pro`
     - `GENAI_TEMPERATURE`: ex. `0.2`
     - `LLM_PROVIDER`: `google` (padrão) ou `groq`
     - `GROQ_API_KEY`: chave da API da Groq (quando `LLM_PROVIDER=groq`)
     - `GROQ_MODEL`: ex. `llama-3.1-70b-versatile`

## Execução
Você pode executar por linha de comando ou via Dashboard (recomendado).

### Via linha de comando
Após preencher os prompts e configurar os agentes, rode:
```
python3 main.py
```

### Via Dashboard (Streamlit)
Inicie a aplicação web:
```
streamlit run streamlit_app.py
```
No app, utilize as páginas à esquerda:
- Importar Relatórios Base: upload para `dados_entrada/` e botão “Carregar tudo no SQLite”.
  - Inclui a seção “Validação rápida de VR (amostragem)”, que mostra:
    - Origem de valor (contagem por `origem_valor`)
    - “Zerados por comunicado<=15”
    - “Sem valor sindicato/estado” e download do CSV de erros quando houver
- Importar CCTs: envie PDFs para `base_conhecimento/ccts_pdfs/` e rode a ingestão.
- Prompts: edite os prompts `.md` dos agentes.
- Dashboard: execute a orquestração, veja status/checagens e gere o relatório VR com nome de arquivo personalizável.

### Ingestão das CCTs (preparação one-off)
1) Coloque PDFs em `base_conhecimento/ccts_pdfs/`
2) Execute:
```
python3 ingest_ccts.py
```

#### OCR automático (CCTs digitalizadas)
- O `ingest_ccts.py` usa PyMuPDF para extrair texto. Se a página tiver pouco texto, faz fallback para OCR com Tesseract.
- Pré-requisito no Linux (exemplos):
  - Ubuntu/Debian: `sudo apt-get update && sudo apt-get install -y tesseract-ocr tesseract-ocr-por`
  - Arch: `sudo pacman -S tesseract tesseract-data-por`
- Python deps já inclusas: `pytesseract` e `Pillow`.
- Metadados: o script infere a UF a partir do nome do arquivo ou do texto e salva em `metadatas["uf"]` para consultas por estado.

> Observação: a ingestão é uma etapa de preparação. Rode novamente somente quando adicionar novas CCTs.

## Componentes principais
- `utils/config.py`: carrega `.env` e fornece `get_llm()` (Gemini via LangChain ou Groq via LangChain, conforme `LLM_PROVIDER`).
- `agentes/*`: agentes usam o LLM e prompts carregados de `prompts/`.
- `ferramentas/*`: ferramentas (LangChain `@tool`) para leitura, cálculo e relatório.

## Dashboard: status, validações e geração do VR
- Tabela de status simples por agente/ação.
- “Checks de Validação da Execução” lendo `relatorios_saida/resultado_execucao.json`.
- “Gerar VR Mensal (com fallback por UF → Estado)” com:
  - Campo de competência `YYYY-MM`
  - Campo “Nome do arquivo de exportação” para escolher o nome do XLSX
  - Botão “Gerar Relatório VR” seguido de “Baixar Relatório VR”

## Saídas geradas
Arquivos em `relatorios_saida/`:
- `VR_MENSAL_mm_aaaa_CALC.xlsx` ou o nome customizado definido no Dashboard.
- `VR_MENSAL_mm_aaaa_ERROS.csv` quando houver linhas sem valor aplicado (amostra para revisão).
- `resultado_execucao.json` e `progresso_execucao.jsonl` (histórico/validações do workflow).

## Dependências
Arquivo chave:
- `requirements.txt` inclui: pandas, openpyxl, langchain, google-generativeai, langchain-google-genai, chromadb, pymupdf, python-dotenv, langchain-groq, pytesseract, Pillow, streamlit.

> Pré-requisitos de sistema (OCR): instalar Tesseract e o pacote de idioma PT-BR, conforme exemplos acima.
