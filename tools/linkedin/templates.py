"""Message templates stored as Markdown files.

One ``.md`` per template under ``~/.hermes/linkedin/templates/``, with
optional YAML-ish frontmatter for metadata and the body below it. Kept on
disk rather than in code so the copy can be edited without a commit, and out
of HubSpot so drafting a message never depends on the MCP being reachable.
"""

from __future__ import annotations

from typing import Optional

from tools.linkedin.paths import ensure_dirs, templates_dir

_EXAMPLE = """---
name: dm-step1
stage: message_1
lang: pt-BR
---
Oi {first_name}, tudo bem?

Vi seu perfil e o trabalho da {company} chamou minha atenção.
"""


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Separate leading ``---`` frontmatter from the body.

    Deliberately a tiny parser rather than a YAML dependency: the metadata is
    flat ``key: value`` and is informational only — nothing here changes how
    a message is sent.
    """
    if not raw.startswith("---"):
        return {}, raw.strip()

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw.strip()

    meta: dict = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, parts[2].strip()


def list_templates() -> list[dict]:
    """Every template on disk, with metadata and a short preview."""
    ensure_dirs()
    out: list[dict] = []
    for path in sorted(templates_dir().glob("*.md")):
        try:
            meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        out.append(
            {
                "name": path.stem,
                "path": str(path),
                "stage": meta.get("stage", ""),
                "lang": meta.get("lang", ""),
                "preview": body[:120],
            }
        )
    return out


def load_template(name: str) -> Optional[str]:
    """Return a template body by file stem, or None when absent."""
    ensure_dirs()
    # Resolve against the templates directory and confirm containment, so a
    # crafted name like "../../.ssh/id_rsa" can't read outside the folder.
    root = templates_dir().resolve()
    candidate = (root / f"{name}.md").resolve()
    if root not in candidate.parents:
        return None
    try:
        _, body = _split_frontmatter(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body or None


def seed_example() -> Optional[str]:
    """Create a starter template the first time the directory is empty."""
    ensure_dirs()
    path = templates_dir() / "dm-step1.md"
    if path.exists() or any(templates_dir().glob("*.md")):
        return None
    path.write_text(_EXAMPLE, encoding="utf-8")
    return str(path)
