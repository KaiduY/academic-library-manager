"""Human-readable taxonomy loading, validation, classification, and drift suggestions."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True)
class Subject:
    id: str
    name: str
    parent_id: str | None
    path: str
    keywords: tuple[str, ...]
    deprecated: bool = False


@dataclass(frozen=True)
class Classification:
    subject_id: str
    document_type: str
    source: str
    confidence: float
    reason: str


class TaxonomyError(ValueError):
    pass


class Taxonomy:
    def __init__(self, raw: dict[str, Any], path: Path | None = None):
        self.path = path
        self.version = str(raw.get("version", ""))
        self.review_subject = str(raw.get("review_subject", "review.unclassified"))
        self.document_types = tuple(str(x) for x in raw.get("document_types", []))
        self.type_keywords: dict[str, tuple[str, ...]] = {
            str(k): tuple(str(x).casefold() for x in v) for k, v in raw.get("type_keywords", {}).items()
        }
        subjects = []
        for item in raw.get("subjects", []):
            subjects.append(Subject(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                parent_id=str(item.get("parent_id", "")) or None,
                path=str(item.get("path", "")),
                keywords=tuple(str(x).casefold() for x in item.get("keywords", [])),
                deprecated=bool(item.get("deprecated", False)),
            ))
        self.subjects = {s.id: s for s in subjects}

    @classmethod
    def load(cls, path: Path | str) -> "Taxonomy":
        p = Path(path)
        with p.open("rb") as handle:
            raw = tomllib.load(handle)
        taxonomy = cls(raw, p.absolute())
        errors = taxonomy.validate()
        if errors:
            raise TaxonomyError("Invalid taxonomy:\n- " + "\n- ".join(errors))
        return taxonomy

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.version:
            errors.append("taxonomy version is required")
        if not self.document_types:
            errors.append("document_types must contain at least one value")
        if len(set(self.document_types)) != len(self.document_types):
            errors.append("duplicate document type identifiers")
        if any(not item.strip() for item in self.document_types):
            errors.append("document_types contains an empty value")
        if self.review_subject not in self.subjects:
            errors.append(f"review_subject {self.review_subject!r} does not exist")
        ids = [s.id for s in self.subjects.values()]
        if not ids or any(not x for x in ids):
            errors.append("every subject needs a non-empty id")
        # Duplicate ids are lost in the dict above, so inspect raw source too when possible.
        if self.path and self.path.is_file():
            try:
                with self.path.open("rb") as handle:
                    raw_subjects = tomllib.load(handle).get("subjects", [])
                raw_ids = [str(s.get("id", "")) for s in raw_subjects]
                duplicates = sorted({x for x in raw_ids if raw_ids.count(x) > 1})
                if duplicates:
                    errors.append(f"duplicate subject IDs: {', '.join(duplicates)}")
            except (OSError, tomllib.TOMLDecodeError) as exc:
                errors.append(f"could not re-read taxonomy source: {exc}")
        paths: dict[str, str] = {}
        for subject in self.subjects.values():
            if not subject.name:
                errors.append(f"subject {subject.id!r} has no name")
            if subject.parent_id and subject.parent_id not in self.subjects:
                errors.append(f"subject {subject.id!r} has unknown parent {subject.parent_id!r}")
            if subject.parent_id == subject.id:
                errors.append(f"subject {subject.id!r} is its own parent")
            pure = PurePosixPath(subject.path)
            if not subject.path or pure.is_absolute() or any(p in ("", ".", "..") for p in pure.parts):
                errors.append(f"subject {subject.id!r} has unsafe relative path {subject.path!r}")
            if "\\" in subject.path:
                errors.append(f"subject {subject.id!r} path must use POSIX separators")
            old = paths.get(subject.path.casefold())
            if old:
                errors.append(f"subjects {old!r} and {subject.id!r} share destination path {subject.path!r}")
            paths[subject.path.casefold()] = subject.id
            for keyword in subject.keywords:
                if not keyword.strip():
                    errors.append(f"subject {subject.id!r} has an empty keyword")
        for kind, keywords in self.type_keywords.items():
            if kind not in self.document_types:
                errors.append(f"type keyword rule references invalid document type {kind!r}")
            if any(not keyword.strip() for keyword in keywords):
                errors.append(f"document type {kind!r} has an empty keyword rule")
        # Parent cycles are checked by following each chain.
        for subject in self.subjects.values():
            seen: set[str] = set()
            current: Subject | None = subject
            while current:
                if current.id in seen:
                    errors.append(f"circular subject hierarchy involving {current.id!r}")
                    break
                seen.add(current.id)
                current = self.subjects.get(current.parent_id) if current.parent_id else None
        # Hierarchy paths should reflect their parents' paths.
        for subject in self.subjects.values():
            if subject.parent_id and subject.parent_id in self.subjects:
                parent_path = self.subjects[subject.parent_id].path.rstrip("/")
                if not subject.path.startswith(parent_path + "/"):
                    errors.append(
                        f"subject {subject.id!r} path {subject.path!r} is not inside parent path {parent_path!r}"
                    )
        return sorted(set(errors))

    def subject_path(self, subject_id: str) -> str:
        return self.subjects[subject_id].path

    @staticmethod
    def _match_score(text: str, keyword: str) -> int:
        if not keyword:
            return 0
        # Phrase keywords use substring matching; single-token keywords require token boundaries.
        if " " in keyword or "-" in keyword or "." in keyword:
            return len(keyword) if keyword in text else 0
        pattern = r"(?<![\w])" + re.escape(keyword) + r"(?![\w])"
        return len(keyword) if re.search(pattern, text) else 0

    def classify(self, relative_path: str) -> Classification:
        text = relative_path.replace("_", " ").replace("-", " ").casefold()
        ranked: list[tuple[int, str, str]] = []
        for subject in self.subjects.values():
            if subject.deprecated or subject.id == self.review_subject:
                continue
            score = sum(self._match_score(text, kw) for kw in subject.keywords)
            if score:
                ranked.append((score, subject.id, ", ".join(kw for kw in subject.keywords if self._match_score(text, kw))))
        if ranked:
            matched_ids = {item[1] for item in ranked}
            def is_ancestor(ancestor_id: str, child_id: str) -> bool:
                current = self.subjects.get(child_id)
                while current and current.parent_id:
                    if current.parent_id == ancestor_id:
                        return True
                    current = self.subjects.get(current.parent_id)
                return False
            # A generic parent keyword (e.g. "Mathematics" in every path) must not defeat a
            # more specific matched child. Keep the parent only when no matched descendant exists.
            ranked = [item for item in ranked if not any(
                item[1] != other_id and is_ancestor(item[1], other_id) for other_id in matched_ids
            )]
            ranked.sort(key=lambda x: (-x[0], -self.subjects[x[1]].path.count("/"), x[1]))
            best_score, best_id, reason = ranked[0]
            tied = [x for x in ranked if x[0] == best_score]
            confidence = 0.60 if len(tied) == 1 else 0.40
            if len(tied) > 1:
                reason += " (ambiguous subject-score tie)"
            subject_id = best_id
        else:
            subject_id = self.review_subject
            confidence = 0.0
            reason = "no configured subject keyword matched the path or filename"
        type_text = text.casefold()
        matching_types: list[tuple[int, str]] = []
        for kind, keywords in self.type_keywords.items():
            for keyword in keywords:
                score = self._match_score(type_text, keyword.casefold())
                if score:
                    matching_types.append((score, kind))
        if matching_types:
            matching_types.sort(key=lambda x: (-x[0], x[1]))
            doc_type = matching_types[0][1]
            if len({score for score, _ in matching_types if score == matching_types[0][0]}) > 1:
                doc_type = "other"
        else:
            ext = PurePosixPath(relative_path).suffix.casefold()
            if ext in {".py", ".ipynb", ".csv", ".json", ".tex", ".bib"}:
                doc_type = "code_or_data"
            elif ext in {".mp4", ".mkv", ".mov", ".avi", ".ppt", ".pptx", ".doc", ".docx"}:
                doc_type = "course_material"
            elif ext in {".pdf", ".djvu", ".djv", ".epub"}:
                doc_type = "textbook"
            else:
                doc_type = "other"
        if doc_type not in self.document_types:
            doc_type = "other" if "other" in self.document_types else self.document_types[0]
        return Classification(subject_id, doc_type, "taxonomy_keyword_heuristic", confidence, reason)

    def destination_for(self, relative_path: str) -> tuple[str, Classification]:
        classification = self.classify(relative_path)
        basename = PurePosixPath(relative_path).name
        return f"{self.subject_path(classification.subject_id)}/{basename}", classification
