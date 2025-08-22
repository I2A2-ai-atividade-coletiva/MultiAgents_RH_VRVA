from agentes.orquestrador import criar_agente_orquestrador
import os


def main():
    # Tarefa de alto nível (exemplo) - pode ser sobrescrita pela variável de ambiente ORQ_TAREFA
    tarefa = os.getenv(
        "ORQ_TAREFA",
        "Calcular VR/VA para o mês de Maio de 2025 usando arquivos em dados_entrada/, validando compliance e CCTs.",
    )

    agente = criar_agente_orquestrador()
    resultado = agente(tarefa)
    print("\n===== RESULTADO FINAL =====\n")
    print(resultado)


if __name__ == "__main__":
    main()
