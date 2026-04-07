#!/usr/bin/env python3
"""
Merge AI_Skills into skills with deduplication.
Only copies skills that don't already exist in the target.
"""

import shutil
import json
from pathlib import Path

SKILLS_DIR = Path(r"d:\vcp_hunter\.agent\skills")
AI_SKILLS_DIR = Path(r"d:\vcp_hunter\.agent\AI_Skills")

SKIP_NAMES = {"claude-skills", "__pycache__", ".codex", ".claude-plugin"}
SKIP_FILES = {"install_skills.py", "merge_skills.py", "skills-index.json"}


def merge():
    existing = {d.name for d in SKILLS_DIR.iterdir() if d.is_dir() and d.name not in SKIP_NAMES}
    print(f"当前 skills/ 已有: {len(existing)} 个技能")

    merged = 0
    skipped_dup = 0
    skipped_no_skill = 0
    errors = 0

    for item in sorted(AI_SKILLS_DIR.iterdir()):
        if not item.is_dir():
            continue
        if item.name in SKIP_NAMES or item.name.startswith("."):
            continue

        # Check for SKILL.md
        has_skill_md = (item / "SKILL.md").exists()
        if not has_skill_md:
            skipped_no_skill += 1
            continue

        # Skip duplicates
        if item.name in existing:
            skipped_dup += 1
            continue

        # Copy
        target = SKILLS_DIR / item.name
        try:
            shutil.copytree(item, target)
            merged += 1
        except Exception as e:
            errors += 1
            if merged < 3:  # Only print first few errors
                print(f"  Error: {item.name}: {e}")

    print(f"\n{'='*50}")
    print(f"  合并结果")
    print(f"{'='*50}")
    print(f"✅ 新增合并: {merged}")
    print(f"⏭️  重复跳过: {skipped_dup}")
    print(f"🚫 无 SKILL.md 跳过: {skipped_no_skill}")
    print(f"❌ 错误: {errors}")
    print(f"📋 合并后总计: {len(existing) + merged}")

    # Generate updated index
    generate_index()

    return merged


def generate_index():
    skills = []
    for item in sorted(SKILLS_DIR.iterdir()):
        if not item.is_dir() or item.name in SKIP_NAMES:
            continue
        skill_md = item / "SKILL.md"
        if not skill_md.exists():
            continue

        description = ""
        try:
            content = skill_md.read_text(encoding="utf-8", errors="ignore")
            if content.startswith("---"):
                end_idx = content.find("---", 3)
                if end_idx != -1:
                    for line in content[3:end_idx].split("\n"):
                        line = line.strip()
                        if line.startswith("description:"):
                            description = line[len("description:"):].strip().strip("\"'")
                            break
        except Exception:
            pass

        skills.append({
            "name": item.name,
            "description": description or f"Skill: {item.name}",
        })

    index = {"version": "1.0.0", "total": len(skills), "skills": skills}
    index_path = SKILLS_DIR / "skills-index.json"
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n📋 索引已更新: {len(skills)} 个技能")


if __name__ == "__main__":
    merge()
