from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"


def carregar_prompt(nome_agente: str) -> str:
    """
    Lê o arquivo .md correspondente ao agente em prompts/ e retorna o conteúdo como string.
    Exemplo: carregar_prompt("orquestrador") -> prompts/orquestrador.md
    """
    caminho = PROMPTS_DIR / f"{nome_agente}.md"
    if not caminho.exists():
        raise FileNotFoundError(f"Prompt não encontrado: {caminho}")
    return caminho.read_text(encoding="utf-8")
