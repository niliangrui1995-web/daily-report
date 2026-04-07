#!/usr/bin/env python3
"""
Windows-compatible skill installer for Antigravity.
Copies SKILL.md files from claude-skills repo into .agent/skills/ directory structure.
"""

import json
import shutil
from pathlib import Path

# Paths
REPO_ROOT = Path(__file__).resolve().parent / "claude-skills"
SKILLS_TARGET = Path(__file__).resolve().parent  # .agent/skills/

# Domain folders to scan
DOMAIN_FOLDERS = [
    "engineering",
    "engineering-team",
    "product-team",
    "marketing-skill",
    "c-level-advisor",
    "project-management",
    "ra-qm-team",
    "business-growth",
    "finance",
]

def install_skills():
    """Scan claude-skills repo and copy skill folders into .agent/skills/."""
    installed = []
    skipped = []
    errors = []

    for domain in DOMAIN_FOLDERS:
        domain_path = REPO_ROOT / domain
        if not domain_path.exists():
            continue

        for skill_dir in domain_path.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            # Skip internal dirs
            if skill_dir.name.startswith("."):
                continue

            target_dir = SKILLS_TARGET / skill_dir.name

            # Skip if already installed (don't overwrite)
            if target_dir.exists() and target_dir.name != "claude-skills":
                skipped.append(skill_dir.name)
                continue

            try:
                # Copy the entire skill folder
                if target_dir.exists():
                    continue
                shutil.copytree(skill_dir, target_dir, dirs_exist_ok=True)
                installed.append(skill_dir.name)
            except Exception as e:
                errors.append(f"{skill_dir.name}: {e}")

    # Also install agent personas
    agents_path = REPO_ROOT / "agents" / "personas"
    if agents_path.exists():
        personas_target = SKILLS_TARGET / "_personas"
        if not personas_target.exists():
            try:
                shutil.copytree(agents_path, personas_target)
                installed.append("_personas")
            except Exception as e:
                errors.append(f"_personas: {e}")

    # Generate skills index
    index = generate_index(SKILLS_TARGET)

    print(f"\n{'='*50}")
    print(f"  Claude Skills Installer for Antigravity")
    print(f"{'='*50}")
    print(f"\n✅ Installed: {len(installed)} skills")
    print(f"⏭️  Skipped:   {len(skipped)} (already exist)")
    print(f"❌ Errors:    {len(errors)}")
    print(f"\n📋 Total indexed: {index['total']} skills")

    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  - {e}")

    print(f"\nSkills directory: {SKILLS_TARGET}")
    print(f"Index file: {SKILLS_TARGET / 'skills-index.json'}")
    return installed


def generate_index(skills_dir: Path) -> dict:
    """Generate a skills-index.json manifest."""
    skills = []

    for item in sorted(skills_dir.iterdir()):
        if not item.is_dir():
            continue
        if item.name in ("claude-skills", "__pycache__"):
            continue

        skill_md = item / "SKILL.md"
        if not skill_md.exists():
            continue

        # Extract description from SKILL.md frontmatter
        description = ""
        try:
            content = skill_md.read_text(encoding="utf-8")
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
            "path": str(item),
            "description": description or f"Skill: {item.name}",
        })

    index = {
        "version": "1.0.0",
        "total": len(skills),
        "skills": skills,
    }

    index_path = skills_dir / "skills-index.json"
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return index


if __name__ == "__main__":
    install_skills()
