# Automação RH - Agentes

Sistema multiagentes para cálculo de VR/VA orquestrado por um "Gerente" (orquestrador) e especialistas, com painel em Streamlit para operação e revisão.

## Estrutura
Consulte as pastas criadas em `automacao_rh_agentes/` conforme o plano.

## Setup
1) Crie o ambiente virtual:
```
python -m venv .venv
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
python automacao_rh_agentes/main.py
```

### Via Dashboard (Streamlit)
Inicie a aplicação web:
```
streamlit run automacao_rh_agentes/streamlit_app.py
```
No app, utilize as páginas à esquerda:
- Importar Bases: envie os arquivos de entrada (Excel/CSV) para `dados_entrada/`.
- Importar CCTs: envie PDFs para `base_conhecimento/ccts_pdfs/` e rode a ingestão.
- Prompts: edite os prompts `.md` dos agentes.
- Dashboard: execute a orquestração, acompanhe o status simples e visualize validações.

### Ingestão das CCTs (preparação one-off)
1) Coloque PDFs em `base_conhecimento/ccts_pdfs/`
2) Execute:
```
python automacao_rh_agentes/ingest_ccts.py
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

## Dashboard: status e validações
- O orquestrador imprime mensagens de alto nível antes de cada etapa, capturadas pelo Dashboard:
  - "Iniciando o trabalho do Especialista de Dados..."
  - "Iniciando o trabalho do Analista de CCT..."
  - "Iniciando o trabalho do Especialista em Compliance..."
  - "Iniciando o trabalho do Especialista em Cálculo..."
- Ao término, o Dashboard exibe os "Checks de Validação da Execução" a partir de `relatorios_saida/resultado_execucao.json`.

## Saídas geradas
Arquivos em `automacao_rh_agentes/relatorios_saida/`:
- `VR_MENSAL_05.2025.xlsx`: relatório Excel com apenas a aba principal (sem aba "Validações").
- `resultado_execucao.json`: resumo de status e lista de validações/etapas concluídas.
- `regras.txt`: resumo textual das regras/CCT utilizadas.
- `compliance.txt`: resumo textual das checagens de compliance.

## Dependências
Arquivo chave:
- `requirements.txt` inclui: pandas, openpyxl, langchain, google-generativeai, langchain-google-genai, chromadb, pymupdf, python-dotenv, langchain-groq, pytesseract, Pillow, streamlit.

> Pré-requisitos de sistema (OCR): instalar Tesseract e o pacote de idioma PT-BR, conforme exemplos acima.
