#!/usr/bin/env python3
"""Find exact or controlled fuzzy model matches in a cookbook markdown table."""

import argparse
import json
import re
import sys
from pathlib import Path


VARIANT_TOKENS = {
    "instruct",
    "thinking",
    "base",
    "chat",
    "coder",
    "reasoning",
    "channel",
    "int8",
    "int4",
    "fp8",
    "w8a8",
    "w4a16",
    "awq",
    "gptq",
    "marlin",
    "slimquant",
    "vl",
    "omni",
    "vision",
}


def strip_markdown(value):
    value = value.strip().strip("`")
    match = re.match(r"^\[([^\]]+)\]\([^)]+\)$", value)
    if match:
        return match.group(1).strip()
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)


def tokenize_model_name(name):
    name = strip_markdown(name)
    if "/" in name:
        name = name.split("/")[-1]
    name = name.lower()
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return re.findall(r"[a-z]+[0-9]+[a-z0-9]*|[0-9]+[a-z]+[a-z0-9]*|[a-z]+|[0-9]+", name)


def is_version_token(token):
    return bool(re.match(r"^2[0-9]{3}$", token))


def variant_tokens(tokens):
    return [token for token in tokens if token in VARIANT_TOKENS or is_version_token(token)]


def base_tokens(tokens):
    variants = set(variant_tokens(tokens))
    return [token for token in tokens if token not in variants]


def split_table_row(line):
    if not line.lstrip().startswith("|"):
        return []
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    if not cells or all(re.match(r"^:?-{3,}:?$", cell) for cell in cells):
        return []
    return cells


def is_header_row(cells):
    header_text = " ".join(cells[:3]).lower()
    return ("模型" in header_text or "model" in header_text) and (
        "量化" in header_text or "quant" in header_text or "vllm" in header_text
    )


def parse_rows(cookbook_file):
    rows = []
    current_model = ""
    for lineno, line in enumerate(cookbook_file.read_text(encoding="utf-8").splitlines(), 1):
        cells = split_table_row(line)
        if not cells or is_header_row(cells):
            continue
        if len(cells) < 2:
            continue

        model_cell = strip_markdown(cells[0])
        if model_cell:
            current_model = model_cell
        elif not current_model:
            continue

        row = {
            "line": lineno,
            "model": current_model,
            "quantization": cells[1] if len(cells) > 1 else "",
            "framework_version": cells[2] if len(cells) > 2 else "",
            "hardware": cells[3] if len(cells) > 3 else "",
            "cards": cells[4] if len(cells) > 4 else "",
            "deployment": cells[5] if len(cells) > 5 else "",
            "command": " | ".join(cells[6:]) if len(cells) > 6 else "",
        }
        if row["model"] and not row["model"].startswith("-"):
            rows.append(row)
    return rows


def contains_filter(cell, expected):
    if not expected:
        return True
    normalized_cell = re.sub(r"[^a-z0-9.]+", " ", cell.lower())
    normalized_expected = re.sub(r"[^a-z0-9.]+", " ", expected.lower()).strip()
    return normalized_expected in normalized_cell


def row_passes_filters(row, args):
    checks = [
        ("framework_version", args.framework_version),
        ("hardware", args.card),
        ("cards", args.cards),
        ("deployment", args.deployment),
        ("quantization", args.quantization),
    ]
    mismatches = []
    for key, expected in checks:
        if expected and not contains_filter(row.get(key, ""), expected):
            mismatches.append("{} expected {!r}, got {!r}".format(key, expected, row.get(key, "")))
    return not mismatches, mismatches


def score_row(row, target_tokens, args):
    candidate_tokens = tokenize_model_name(row["model"])
    target_base = base_tokens(target_tokens)
    candidate_set = set(candidate_tokens)
    candidate_base = base_tokens(candidate_tokens)
    target_variants = set(variant_tokens(target_tokens))
    candidate_variants = set(variant_tokens(candidate_tokens))

    if not set(target_base).issubset(candidate_set):
        return None

    passes_filters, filter_mismatches = row_passes_filters(row, args)
    if filter_mismatches and not args.include_filter_mismatches:
        return None

    missing_variants = sorted(target_variants - candidate_variants)
    extra_variants = sorted(candidate_variants - target_variants)
    base_extra = sorted(set(candidate_base) - set(target_base))
    exact_name = target_tokens == candidate_tokens
    exact = exact_name and passes_filters and not missing_variants and not extra_variants and not base_extra

    score = 0
    score += 500 if set(target_base) == set(candidate_base) else 250
    score += 40 * len(target_variants & candidate_variants)
    score -= 60 * len(missing_variants)
    score -= 20 * len(extra_variants)
    score -= 30 * len(base_extra)
    if exact_name:
        score += 1000

    for key, expected, weight in [
        ("framework_version", args.framework_version, 80),
        ("hardware", args.card, 70),
        ("cards", args.cards, 40),
        ("deployment", args.deployment, 40),
        ("quantization", args.quantization, 80),
    ]:
        if expected:
            if contains_filter(row.get(key, ""), expected):
                score += weight
            else:
                score -= weight * 2

    diff = []
    if missing_variants:
        diff.append("missing explicit suffix: {}".format(", ".join(missing_variants)))
    if extra_variants:
        diff.append("extra suffix: {}".format(", ".join(extra_variants)))
    if base_extra:
        diff.append("extra base token: {}".format(", ".join(base_extra)))
    if filter_mismatches:
        diff.extend(filter_mismatches)
    if not diff:
        diff.append("none")

    risk = "exact" if exact else "confirm"
    if missing_variants or filter_mismatches:
        risk = "high-confirm"

    result = dict(row)
    result.update(
        {
            "score": score,
            "exact": exact,
            "target_tokens": target_tokens,
            "candidate_tokens": candidate_tokens,
            "missing_variants": missing_variants,
            "extra_variants": extra_variants,
            "base_extra": base_extra,
            "filter_mismatches": filter_mismatches,
            "diff": "; ".join(diff),
            "risk": risk,
        }
    )
    return result


def find_matches(rows, args):
    target_tokens = tokenize_model_name(args.model)
    top_k = getattr(args, "top_k", 3)
    scored = []
    for row in rows:
        match = score_row(row, target_tokens, args)
        if match:
            scored.append(match)
    scored.sort(key=lambda item: (item["exact"], item["score"]), reverse=True)

    exact_matches = [item for item in scored if item["exact"]]
    if exact_matches:
        return "exact", exact_matches[:top_k]
    return ("fuzzy" if scored else "no_candidates"), scored[:top_k]


def print_markdown(status, matches, args):
    print("# Cookbook model match")
    print("")
    print("- target: `{}`".format(args.model))
    print("- status: `{}`".format(status))
    print("")
    if not matches:
        print("No candidate matched the base model identity and hard filters.")
        return

    print("| rank | candidate | quantization | version | hardware | cards | deployment | score | match | diff |")
    print("|------|-----------|--------------|---------|----------|-------|------------|-------|-------|------|")
    for idx, item in enumerate(matches, 1):
        match_type = "exact" if item["exact"] else item["risk"]
        print(
            "| {rank} | {model} | {quant} | {version} | {hardware} | {cards} | {deployment} | {score} | {match} | {diff} |".format(
                rank=idx,
                model=item["model"],
                quant=item["quantization"],
                version=item["framework_version"],
                hardware=item["hardware"],
                cards=item["cards"],
                deployment=item["deployment"],
                score=item["score"],
                match=match_type,
                diff=item["diff"],
            )
        )

    if status != "exact":
        print("")
        print("Non-exact candidates require user confirmation before generating or running scripts.")


def main():
    parser = argparse.ArgumentParser(description="Match a target model against cookbook markdown table rows.")
    parser.add_argument("--cookbook-file", required=True, help="Path to one cookbook markdown file.")
    parser.add_argument("--model", required=True, help="Target model name or local directory name.")
    parser.add_argument("--framework-version", help="Hard filter, for example: 0.18")
    parser.add_argument("--card", help="Hard filter, for example: BW1000")
    parser.add_argument("--cards", help="Hard filter, for example: 1x or 2x")
    parser.add_argument("--deployment", help="Hard filter, for example: IFB or PD")
    parser.add_argument("--quantization", help="Hard filter, for example: W8A8 or FP8")
    parser.add_argument("--include-filter-mismatches", action="store_true")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of markdown.")
    args = parser.parse_args()

    cookbook_file = Path(args.cookbook_file).expanduser()
    if not cookbook_file.exists():
        print("cookbook file not found: {}".format(cookbook_file), file=sys.stderr)
        return 2

    rows = parse_rows(cookbook_file)
    status, matches = find_matches(rows, args)
    if args.json:
        print(json.dumps({"target": args.model, "status": status, "matches": matches}, ensure_ascii=False, indent=2))
    else:
        print_markdown(status, matches, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
