from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json

from utils.regras_resolver import resolve_cct_rules

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = BASE_DIR / "base_conhecimento" / "chromadb"
RELATORIOS_DIR = BASE_DIR / "relatorios_saida"
RULES_INDEX_ROOT = BASE_DIR / "base_conhecimento" / "rules_index.json"


def _read_json(path: Path) -> Optional[object]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _parse_brl(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        s = str(v)
        if not s:
            return None
        s = s.replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
        return float(s)
    except Exception:
        return None


def _eq_money(a: Any, b: Any) -> bool:
    va = _parse_brl(a)
    vb = _parse_brl(b)
    if va is None and vb is None:
        return True
    if va is None or vb is None:
        return False
    return abs(va - vb) < 0.005


def _eq_norm(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    return str(a).strip().lower() == str(b).strip().lower()


@dataclass
class ComplianceItem:
    uf: str
    sindicato: str
    origem_sistema: str
    vr_ocr: Any
    va_ocr: Any
    dias_ocr: Any
    periodicidade_ocr: Any
    vr_sistema: Any
    va_sistema: Any
    dias_sistema: Any
    periodicidade_sistema: Any
    status: str  # ok | mismatch | missing_system | missing_ocr
    site_check_recommended: bool
    detalhes: str


def validar_compliance_cct() -> Dict[str, Any]:
    """
    Compara regras extraídas por OCR (rules_index.json) com as regras resolvidas pelo sistema
    (overrides/SQLite/retrieval) e sinaliza divergências ou faltas. Retorna um relatório estruturado
    e salva um JSON em relatorios_saida/cct_compliance.json.
    """
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    RELATORIOS_DIR.mkdir(parents=True, exist_ok=True)

    # Unificado: usar base_conhecimento/rules_index.json
    rules_index_path = RULES_INDEX_ROOT
    rules_index = _read_json(rules_index_path) or []

    itens: List[ComplianceItem] = []
    for item in rules_index:
        uf = (item.get("uf") or "").upper() or "DESCONHECIDO"
        sind = (item.get("sindicato") or "").strip() or "DESCONHECIDO"
        ocr_vr = item.get("vr_valor")
        ocr_va = item.get("va_valor")
        ocr_dias = item.get("dias")
        ocr_per = item.get("periodicidade")

        sys = resolve_cct_rules(uf, sind)
        sys_vr = sys.get("vr_valor")
        sys_va = sys.get("va_valor")
        sys_dias = sys.get("dias")
        sys_per = sys.get("periodicidade")
        origem = sys.get("origem", "nao_encontrado")

        # Determine status
        missing_ocr = (ocr_vr is None and ocr_va is None and ocr_dias is None and ocr_per is None)
        missing_sys = (origem == "nao_encontrado") or (sys_vr is None and sys_va is None and sys_dias is None and sys_per is None)

        if missing_ocr and missing_sys:
            status = "missing_ocr"
            site_flag = True
            detalhes = "OCR não extraiu valores e o sistema não possui regra."
        elif missing_ocr and not missing_sys:
            status = "missing_ocr"
            site_flag = True
            detalhes = "OCR não extraiu valores. Sistema possui regra — revisar CCT oficial para confirmar."
        elif not missing_ocr and missing_sys:
            status = "missing_system"
            site_flag = True
            detalhes = "Sistema não possui regra para UF/Sindicato presentes no OCR."
        else:
            # Both sides have something; compare fields
            mismatches: List[str] = []
            if (ocr_vr is not None or sys_vr is not None) and not _eq_money(ocr_vr, sys_vr):
                mismatches.append(f"VR difere (OCR={ocr_vr} vs SYS={sys_vr})")
            if (ocr_va is not None or sys_va is not None) and not _eq_money(ocr_va, sys_va):
                mismatches.append(f"VA difere (OCR={ocr_va} vs SYS={sys_va})")
            if (ocr_dias is not None or sys_dias is not None) and not _eq_norm(ocr_dias, sys_dias):
                mismatches.append(f"Dias difere (OCR={ocr_dias} vs SYS={sys_dias})")
            if (ocr_per is not None or sys_per is not None) and not _eq_norm(ocr_per, sys_per):
                mismatches.append(f"Periodicidade difere (OCR={ocr_per} vs SYS={sys_per})")

            if mismatches:
                status = "mismatch"
                site_flag = True
                detalhes = "; ".join(mismatches)
            else:
                status = "ok"
                site_flag = False
                detalhes = "Regras consistentes."

        itens.append(
            ComplianceItem(
                uf=uf,
                sindicato=sind,
                origem_sistema=origem,
                vr_ocr=ocr_vr,
                va_ocr=ocr_va,
                dias_ocr=ocr_dias,
                periodicidade_ocr=ocr_per,
                vr_sistema=sys_vr,
                va_sistema=sys_va,
                dias_sistema=sys_dias,
                periodicidade_sistema=sys_per,
                status=status,
                site_check_recommended=site_flag,
                detalhes=detalhes,
            )
        )

    resumo = {
        "total": len(itens),
        "ok": sum(1 for i in itens if i.status == "ok"),
        "mismatch": sum(1 for i in itens if i.status == "mismatch"),
        "missing_system": sum(1 for i in itens if i.status == "missing_system"),
        "missing_ocr": sum(1 for i in itens if i.status == "missing_ocr"),
        "site_check_recommended": sum(1 for i in itens if i.site_check_recommended),
    }

    rel = {
        "resumo": resumo,
        "itens": [asdict(i) for i in itens],
    }

    out_path = RELATORIOS_DIR / "cct_compliance.json"
    try:
        out_path.write_text(json.dumps(rel, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return rel
