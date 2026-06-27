"""Batch translate skill descriptions to Chinese."""
import json, os, re, sys, time
from pathlib import Path

import yaml
from deep_translator import GoogleTranslator

HOME = Path(os.path.expanduser("~"))
SKILLS_DIR = HOME / ".cc-switch" / "skills"
OUT_FILE = Path(__file__).parent / "skill_zh_map.json"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
translator = GoogleTranslator(source="en", target="zh-CN")


def has_chinese(text: str) -> bool:
    return any("一" <= c <= "鿿" for c in text)


def translate(text: str) -> str:
    try:
        return translator.translate(text[:4500]) or ""
    except Exception:
        return ""


def save(data):
    OUT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main():
    existing = {}
    if OUT_FILE.exists():
        existing = json.loads(OUT_FILE.read_text(encoding="utf-8"))

    result = dict(existing)
    total = 0
    translated = 0
    skipped = 0
    batch = 0

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        name = skill_dir.name
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        total += 1

        if name in result and result[name]:
            skipped += 1
            continue

        text = skill_md.read_text(encoding="utf-8", errors="replace")[:4096]
        m = FRONTMATTER_RE.match(text)
        if not m:
            continue
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue

        desc = meta.get("description", "")
        if not isinstance(desc, str):
            desc = str(desc)
        desc = desc.strip()

        if not desc:
            continue

        if has_chinese(desc):
            result[name] = desc[:200]
            print(f"[ZH] {name}")
        else:
            zh = translate(desc)
            if zh:
                result[name] = zh[:200]
                translated += 1
                batch += 1
                print(f"[EN->ZH] {name}")
            else:
                print(f"[FAIL] {name}")

            # 每翻译 5 个保存一次 + 暂停
            if batch % 5 == 0:
                save(result)
                time.sleep(1.5)

    save(result)
    print(f"\nDone: total={total}, translated={translated}, skipped={skipped}, saved={len(result)}")


if __name__ == "__main__":
    main()
