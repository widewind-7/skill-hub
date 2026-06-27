"""Merge all translated batches into skill_zh_map.json"""
import json, os
from pathlib import Path

base = Path("E:/AI/workspace/skill-hub/batches")
out_file = Path("E:/AI/workspace/skill-hub/skill_zh_map.json")

# Load existing Chinese descriptions from cc-switch
import re, yaml
HOME = Path(os.path.expanduser("~"))
SKILLS_DIR = HOME / ".cc-switch" / "skills"
FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

result = {}

# First: load skills that already have Chinese descriptions
for d in sorted(SKILLS_DIR.iterdir()):
    if not d.is_dir() or d.name.startswith("."):
        continue
    md = d / "SKILL.md"
    if not md.exists():
        continue
    text = md.read_text(encoding="utf-8", errors="replace")[:4096]
    m = FM.match(text)
    if not m:
        continue
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except:
        continue
    desc = meta.get("description", "")
    if not isinstance(desc, str):
        desc = str(desc)
    desc = desc.strip()
    if not desc:
        continue
    if any("一" <= c <= "鿿" for c in desc):
        result[d.name] = desc[:200]

print(f"From SKILL.md (Chinese): {len(result)}")

# Second: merge translated batches
merged = 0
for i in range(9):
    f = base / f"batch_{i}_zh.json"
    if not f.exists():
        print(f"Missing: {f}")
        continue
    items = json.loads(f.read_text(encoding="utf-8"))
    for item in items:
        name = item["name"]
        zh = item.get("zh", "")
        if zh and name not in result:
            result[name] = zh[:200]
            merged += 1

print(f"From translated batches: {merged}")
print(f"Total: {len(result)}")

# Save
out_file.write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(f"Saved to {out_file}")
