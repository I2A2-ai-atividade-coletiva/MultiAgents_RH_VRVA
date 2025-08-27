from __future__ import annotations

from typing import Optional, Tuple
import re

# Simple UF inference from sindicato names with confidence
# Extend with real aliases as needed

UF_ALIASES = {
    "SP": [" sp", "-sp", "sao paulo", "são paulo"],
    "RJ": [" rj", "-rj", "rio de janeiro"],
    "MG": [" mg", "-mg", "minas gerais"],
    "RS": [" rs", "-rs", "rio grande do sul"],
    "PR": [" pr", "-pr", "parana", "paraná"],
    "SC": [" sc", "-sc", "santa catarina"],
    "ES": [" es", "-es", "espirito santo", "espírito santo"],
    "BA": [" ba", "-ba", "bahia"],
    "PE": [" pe", "-pe", "pernambuco"],
}

UF_SET = set(UF_ALIASES.keys())


def infer_uf_from_sindicato(name: Optional[str]) -> Tuple[Optional[str], str]:
    """
    Returns (uf, origem): origem in {inferida, regex, none}
    """
    if not name:
        return None, "none"
    s = str(name).lower()
    # direct UF tokens in parentheses or end
    m = re.search(r"\b([a-z]{2})\b", s)
    if m:
        cand = m.group(1).upper()
        if cand in UF_SET:
            return cand, "regex"
    # aliases
    for uf, patterns in UF_ALIASES.items():
        for p in patterns:
            if p in s:
                return uf, "inferida"
    return None, "none"
