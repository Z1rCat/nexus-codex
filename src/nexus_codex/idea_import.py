from __future__ import annotations

import importlib.util
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


@dataclass(frozen=True)
class ImportedIdea:
    project_root: Path
    copied_sources: tuple[Path, ...]
    source_text_path: Path
    raw_markdown_path: Path
    idea_markdown_path: Path
    extracted_title: str | None
    warnings: tuple[str, ...] = ()


def import_idea_bundle(
    project: str | Path,
    *,
    docx_path: str | Path | None = None,
    pdf_path: str | Path | None = None,
) -> ImportedIdea:
    root = Path(project).resolve()
    ideas_dir = root / "ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)

    docx_source = Path(docx_path).resolve() if docx_path else None
    pdf_source = Path(pdf_path).resolve() if pdf_path else None
    if docx_source is None and pdf_source is None:
        raise ValueError("at least one of docx_path or pdf_path is required")

    copied_sources: list[Path] = []
    warnings: list[str] = []
    extracted_chunks: list[str] = []

    if docx_source is not None:
        if not docx_source.exists():
            raise FileNotFoundError(docx_source)
        copied_docx = ideas_dir / "idea.source.docx"
        shutil.copyfile(docx_source, copied_docx)
        copied_sources.append(copied_docx)
        extracted_chunks.append(_extract_docx_text(docx_source))

    if pdf_source is not None:
        if not pdf_source.exists():
            raise FileNotFoundError(pdf_source)
        copied_pdf = ideas_dir / "idea.source.pdf"
        shutil.copyfile(pdf_source, copied_pdf)
        copied_sources.append(copied_pdf)
        pdf_text, pdf_warning = _extract_pdf_text(pdf_source)
        if pdf_text.strip():
            extracted_chunks.append(pdf_text)
        if pdf_warning:
            warnings.append(pdf_warning)

    extracted_text = "\n\n".join(chunk.strip() for chunk in extracted_chunks if chunk.strip()).strip()
    title = _guess_title(extracted_text)

    source_text_path = ideas_dir / "idea.source.txt"
    raw_markdown_path = ideas_dir / "idea.raw.md"
    idea_markdown_path = ideas_dir / "idea.md"

    source_text_path.write_text(extracted_text + ("\n" if extracted_text else ""), encoding="utf-8")
    raw_markdown_path.write_text(
        _render_raw_markdown(
            copied_sources=copied_sources,
            extracted_text=extracted_text,
            warnings=warnings,
        ),
        encoding="utf-8",
    )
    idea_markdown_path.write_text(
        _render_structured_idea_markdown(
            copied_sources=copied_sources,
            extracted_text=extracted_text,
            title=title,
            warnings=warnings,
        ),
        encoding="utf-8",
    )

    return ImportedIdea(
        project_root=root,
        copied_sources=tuple(copied_sources),
        source_text_path=source_text_path,
        raw_markdown_path=raw_markdown_path,
        idea_markdown_path=idea_markdown_path,
        extracted_title=title,
        warnings=tuple(warnings),
    )


def _extract_docx_text(path: Path) -> str:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        xml_bytes = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml_bytes)
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        parts: list[str] = []
        for node in paragraph.iter():
            tag = node.tag
            if tag == f"{{{namespace['w']}}}t":
                parts.append(node.text or "")
            elif tag == f"{{{namespace['w']}}}tab":
                parts.append("\t")
            elif tag == f"{{{namespace['w']}}}br":
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _extract_pdf_text(path: Path) -> tuple[str, str | None]:
    if importlib.util.find_spec("pypdf") is None:
        return "", "PDF text extraction skipped because `pypdf` is not installed."
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        parts = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(part.strip() for part in parts if part.strip()), None
    except Exception as exc:  # pragma: no cover - depends on local pypdf/pdf state
        return "", f"PDF text extraction failed: {exc}"


def _guess_title(extracted_text: str) -> str | None:
    for line in extracted_text.splitlines():
        candidate = line.strip()
        if len(candidate) >= 8:
            return candidate[:160]
    return None


def _render_raw_markdown(
    *,
    copied_sources: list[Path],
    extracted_text: str,
    warnings: list[str],
) -> str:
    lines = [
        "# Imported Idea Raw Notes",
        "",
        "## Sources",
    ]
    lines.extend(f"- {path.name}" for path in copied_sources)
    if warnings:
        lines.extend(["", "## Warnings"])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(
        [
            "",
            "## Extracted Text",
            "",
            extracted_text or "_No text could be extracted automatically._",
            "",
        ]
    )
    return "\n".join(lines)


def _render_structured_idea_markdown(
    *,
    copied_sources: list[Path],
    extracted_text: str,
    title: str | None,
    warnings: list[str],
) -> str:
    preview = _preview_paragraphs(extracted_text, limit=6)
    lines = [
        f"# {title or 'Imported RL Idea'}",
        "",
        "## Source Overview",
        "",
        f"- source files: {', '.join(path.name for path in copied_sources)}",
        "- raw text: `ideas/idea.raw.md`",
        "- extracted source text: `ideas/idea.source.txt`",
    ]
    if warnings:
        lines.append(f"- warnings: {' | '.join(warnings)}")
    lines.extend(
        [
            "",
            "## Problem",
            "",
            "Fill in the research problem in one short paragraph.",
            "",
            "## Environment",
            "",
            "- environment name:",
            "- observation / state:",
            "- action space:",
            "- episode termination:",
            "",
            "## Reward",
            "",
            "Describe the reward and any reward shaping.",
            "",
            "## Core Hypothesis",
            "",
            "State the algorithmic idea as one or two precise claims.",
            "",
            "## Baselines",
            "",
            "- simplest baseline:",
            "- stronger baseline:",
            "",
            "## Minimal Prototype",
            "",
            "Describe the smallest implementation that should run locally first.",
            "",
            "## Local Verification",
            "",
            "- smoke test command:",
            "- expected output files:",
            "- pass condition:",
            "",
            "## Risks",
            "",
            "- likely failure mode 1:",
            "- likely failure mode 2:",
            "",
            "## Source Highlights",
            "",
        ]
    )
    if preview:
        lines.extend(f"- {paragraph}" for paragraph in preview)
    else:
        lines.append("- No text was extracted automatically. Populate this file manually.")
    lines.append("")
    return "\n".join(lines)


def _preview_paragraphs(extracted_text: str, *, limit: int) -> list[str]:
    paragraphs = [paragraph.strip() for paragraph in extracted_text.split("\n\n") if paragraph.strip()]
    return [paragraph[:240] for paragraph in paragraphs[:limit]]
