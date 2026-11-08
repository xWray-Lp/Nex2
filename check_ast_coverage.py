#!/usr/bin/env python3
"""AST field kapsama denetimi (ci gate).

Kök neden: tekrarlayan hata ailesi — bir AST alanı ebeveyn visitor tarafından
unutulup checker'dan sessizce geçiyor. Bilinen olaylar:
  PipeExpr.left/right   (visit_PipeExpr yoktu → @own sızıntısı)
  FStringExpr.parts     (visit_FStringExpr yoktu → undefined/own sızıntısı)
  MatchArm.guard        (visit_MatchStmt/Expr alanı hiç göndermezdi)
  Param.default         (visit_FnDecl hiç visit etmezdi)
  FieldDecl.default     (visit_ClassDecl hiç visit etmezdi)

Bu script her Node alt sınıfınıın her alanını çeker ve alan adının
type_checker.py içinde en az bir defa attribute erişimi (node.X) veya
explicit string olarak (getattr, türü kısılırken) geçip geçmediğini
doğrular. Hiç geçmiyorsa ALLOWED_MISSING whitelist'i dışında BUILD FAIL →
yeni eklenen her alan ya visitor'a girmek ya da whitelist'e gerekçeyle
girmek zorunda.

Kullanım: python3 check_ast_coverage.py  (exit 1 = kapsanmayan alan var)
"""

import ast as pyast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
AST_FILE      = REPO / "ast_nodes.py"
CHECKER_FILE  = REPO / "type_checker.py"

METADATA_FIELDS = {"line", "col"}          # konumsal öznitelik: denetlenecek değer değil
MODULE_FIELDS   = {"path", "names", "alias"}   # ImportStmt/ModuleStmt: checker kapsamı dışı modül yapısı

# Aç yeşil ışık: checker'ın bilinçli olarak işlemediği alanlar.
# Her giriş: sınıf adı -> {alan: gerekçe}. Yeni alan buraya YAZILMADAN
# eklenmemeli — hangi visitor'ın işlemesi gerektiğini tart!
ALLOWED_MISSING: dict[str, dict[str, str]] = {
    # ── FnDecl export & module yapısı: checker kapsamı dışı ──
    "FnDecl": {
        "is_export": "export semantiği (module sistemi) henüz yok (roadmap)",
    },
    # ImportStmt/ModuleStmt: modül ağacını işleyecek module sistemi v1'de yok.
    "ImportStmt": {"path": "module sistemi v1'de yok; checker import işlemez"},
    "ModuleStmt": {"path": "module sistemi v1'de yok; checker module işlemez"},
}


def collect_fields(path: Path) -> dict[str, list[str]]:
    """Node alt sınıflarını ve class-level alan adlarını döndür."""
    tree = pyast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for node in pyast.walk(tree):
        if not isinstance(node, pyast.ClassDef):
            continue
        fields = []
        for item in node.body:
            if isinstance(item, pyast.AnnAssign):
                if isinstance(item.target, pyast.Name):
                    fields.append(item.target.id)
            elif isinstance(item, pyast.Assign):
                for tg in item.targets:
                    if isinstance(tg, pyast.Name):
                        fields.append(tg.id)
        fields = [f for f in fields if f not in METADATA_FIELDS]
        out[node.name] = fields
    return out


def collect_attribute_names(path: Path) -> set[str]:
    """Dosyada attribute erişilen tüm .attr adları (+ getattr stringleri)."""
    src = path.read_text(encoding="utf-8")
    attrs: set[str] = set()
    tree = pyast.parse(src)
    for node in pyast.walk(tree):
        if isinstance(node, pyast.Attribute):
            attrs.add(node.attr)
        if isinstance(node, pyast.Call) and isinstance(node.func, pyast.Name) \
                and node.func.id == "getattr" and node.args[1:]:
            arg = node.args[1]
            if isinstance(arg, pyast.Constant):
                attrs.add(str(arg.value))
    # getattr string araması: getattr(x, 'left') şekli yukarıda yakalanır.
    return attrs


def main() -> int:
    classes = collect_fields(AST_FILE)
    checker_attrs = collect_attribute_names(CHECKER_FILE)

    covered: list[str] = []
    whitelisted: list[str] = []
    missing: list[str] = []

    for cls in sorted(classes):
        for field in classes[cls]:
            key = f"{cls}.{field}"
            if field in checker_attrs:
                covered.append(key)
            elif field in MODULE_FIELDS:
                whitelisted.append(f"{key} — module-level field, checker kapsamı dışı")
            elif cls in ALLOWED_MISSING and field in ALLOWED_MISSING[cls]:
                whitelisted.append(f"{key} — {ALLOWED_MISSING[cls][field]}")
            else:
                missing.append(key)

    print(f"AST coverage: {len(covered)} covered / "
          f"{len(whitelisted)} whitelisted / {len(missing)} uncovered "
          f"(fields={sum(len(v) for v in classes.values())}, classes={len(classes)})")

    if whitelisted:
        print("\nWhitelisted (bilinçli şartla kapsama dışı):")
        for w in whitelisted:
            print(f"  ~ {w}")

    if missing:
        print("\nUNCOVERED fields (visitor bağlantısı yok — hızlı tarama sonucu):")
        for m in missing:
            print(f"  ! {m}")
        print("\n  Ya visit_X/parent'ında alanı visit'le, ya ALLOWED_MISSING'e "
              "gerekçeyle ekle (unprotected sessiz geçişi engelle).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
