"""Build a bounded API inventory from JAX-RS annotations in backend source.

This is a source-derived navigation artifact, not an authoritative deployed API
contract. It intentionally records source provenance and limitations.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
PATH_RE = re.compile(r"@Path\(\s*([^)]*)\s*\)")
METHOD_RE = re.compile(r"@(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b")
METHOD_NAME_RE = re.compile(
    r"\b(?:public|protected|private)\s+(?:static\s+)?[\w<>, ?\[\].]+\s+(\w+)\s*\("
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-endpoints", type=int, default=5000)
    return parser.parse_args()


def join_paths(base: str, child: str | None) -> str:
    if not child:
        return base or "/"
    return f"{base.rstrip('/')}/{child.lstrip('/')}"


def path_value(match: re.Match[str]) -> str:
    expression = match.group(1).strip()
    if len(expression) >= 2 and expression[0] == '"' and expression[-1] == '"':
        return expression[1:-1]
    return f"<unresolved:{expression}>"


def extract_file(path: Path, repo: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    class_path = ""
    class_index = next(
        (
            index
            for index, line in enumerate(lines)
            if re.search(
                r"^\s*(?:(?:public|protected|private|abstract|final|sealed)\s+)*"
                r"(?:class|interface|record)\b",
                line,
            )
        ),
        None,
    )
    if class_index is not None:
        for candidate in lines[max(0, class_index - 100) : class_index + 1]:
            path_match = PATH_RE.search(candidate)
            if path_match:
                class_path = path_value(path_match)
                break

    endpoints: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        method_match = METHOD_RE.search(line)
        if not method_match:
            continue
        method = method_match.group(1)
        method_path = None
        for candidate in lines[index + 1 : index + 9]:
            path_match = PATH_RE.search(candidate)
            if path_match:
                method_path = path_value(path_match)
                break
            if "(" in candidate or "{" in candidate:
                break
        method_name = None
        for candidate in lines[index : index + 24]:
            name_match = METHOD_NAME_RE.search(candidate)
            if name_match:
                method_name = name_match.group(1)
                break
        relative_path = path.relative_to(repo).as_posix()
        endpoints.append(
            {
                "method": method,
                "path": join_paths(class_path, method_path),
                "operation_name": method_name,
                "source_file": relative_path,
                "source_line": index + 1,
            }
        )
    return endpoints


def build_inventory(repo: Path, max_endpoints: int) -> dict[str, Any]:
    if max_endpoints < 1:
        raise ValueError("max-endpoints must be positive")
    files = sorted(
        path
        for path in repo.rglob("*ApiResource.java")
        if "fineract-client" not in path.parts
        and "src" in path.parts
        and "main" in path.parts
    )
    endpoints: list[dict[str, Any]] = []
    for path in files:
        if "/test/" in path.as_posix() or "/tests/" in path.as_posix():
            continue
        endpoints.extend(extract_file(path, repo))
        if len(endpoints) >= max_endpoints:
            break
    endpoints = sorted(
        endpoints[:max_endpoints],
        key=lambda item: (item["path"], item["method"], item["source_file"], item["source_line"]),
    )
    return {
        "contract": "derived_api_inventory.v1",
        "material_status": "derived_from_backend_source",
        "source_surface": "backend_code",
        "evidence_strength": "server_code",
        "repository_root": repo.as_posix(),
        "parser": "bounded_jax_rs_annotation_scan",
        "endpoint_count": len(endpoints),
        "endpoints": endpoints,
        "limitations": [
            "Derived from source annotations; not an official OpenAPI contract.",
            (
                "Does not prove deployed routes, gateway prefixes, runtime reachability, or "
                "authorization behavior."
            ),
            (
                "Exact request and response schemas require DTO and runtime/API "
                "documentation verification."
            ),
        ],
    }


def main() -> None:
    args = parse_args()
    repo = args.repo.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    inventory = build_inventory(repo, args.max_endpoints)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "derived_api_inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = [
        "# Derived Backend API Inventory",
        "",
        (
            "> TEST SUPPORT ARTIFACT. Derived from backend source; not an official API "
            "document and not valid for submission."
        ),
        "",
        f"- Repository: `{repo}`",
        f"- Parser: `{inventory['parser']}`",
        f"- Endpoint count: `{inventory['endpoint_count']}`",
        "- Evidence surface: `backend_code`",
        "- Evidence strength: `server_code`",
        "",
        "## Limitations",
        "",
        *[f"- {item}" for item in inventory["limitations"]],
        "",
        "## Endpoint Samples",
        "",
        "| Method | Path | Operation | Source | Line |",
        "|---|---|---|---|---:|",
    ]
    for endpoint in inventory["endpoints"][:100]:
        summary.append(
            f"| `{endpoint['method']}` | `{endpoint['path']}` | "
            f"`{endpoint['operation_name'] or '-'}` | `{endpoint['source_file']}` | "
            f"{endpoint['source_line']} |"
        )
    (output_dir / "derived_api_inventory.md").write_text(
        "\n".join(summary) + "\n", encoding="utf-8"
    )
    print(output_dir / "derived_api_inventory.json")
    print(f"endpoint_count={inventory['endpoint_count']}")


if __name__ == "__main__":
    main()
