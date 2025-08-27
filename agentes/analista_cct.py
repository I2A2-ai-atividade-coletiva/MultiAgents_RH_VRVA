"""
Wrapper para compatibilidade: reexporta `criar_agente_cct` do módulo unificado `agentes.cct`.
"""

from .cct import criar_agente_cct  # noqa: F401

__all__ = ["criar_agente_cct"]
