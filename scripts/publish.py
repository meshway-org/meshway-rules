#!/usr/bin/env python3
"""Builds the next `dist` tree from the upstreams in sources.toml.

Conversion, artifact checks, catalog assembly and signing are the
`meshway-rules` CLI's; this script fetches upstreams, runs `convert` per set,
applies the hold and retention policy, and lays out the tree that
`meshway-rules index` then signs:

    sets/<collection>/<name>.json   SetRecord written for every published set
    artifacts/sha256-<hex>.mwrs     current and retained artifacts
    history/<sequence>.json         the previous catalogs whose artifacts are kept
    status.json                     pipeline inputs, holds and skips (unsigned,
                                    for people; the signed record of a hold is
                                    the set's `held` in index.json)
    index.json                      copy of the previous catalog, so `index`
                                    continues its sequence

Usage:
    publish.py build --cli PATH --sources sources.toml --previous DIR \
        --out DIR --work DIR --summary FILE [--accept SET ...]
    publish.py notes --summary FILE [--index index.json]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

STATUS_SCHEMA = 1
# Catalogs whose artifacts stay in `dist`, the one being built included.
KEEP_CATALOGS = 7
# A set whose canonical entry count falls below this fraction of the
# previous one is held as suspicious.
MIN_ENTRY_RATIO = 0.5

COLLECTION_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9!@._+-]{0,127}")


# --- policy -----------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """`publish` the new conversion, `hold` the previous one, or `skip`."""

    action: str
    reason: str | None = None


def decide(
    previous_entries: int | None,
    entries: int | None,
    error: str | None,
    accepted: bool,
) -> Decision:
    """Hold policy for one set.

    `previous_entries` is the entry count of the set in the previous catalog
    (None when it was not published); `entries` is the new conversion's count,
    or None with `error` when the conversion failed. `accepted` releases a
    hold for an entry drop (never for a failure).
    """
    if entries is None:
        if previous_entries is None:
            return Decision("skip", f"conversion failed: {error}")
        return Decision("hold", f"conversion failed: {error}")
    if (
        previous_entries is not None
        and not accepted
        and entries < previous_entries * MIN_ENTRY_RATIO
    ):
        return Decision(
            "hold", f"entries dropped from {previous_entries} to {entries}"
        )
    return Decision("publish")


def retained_catalogs(catalogs: list[dict], keep: int = KEEP_CATALOGS) -> list[dict]:
    """The previous catalogs kept next to a new one: the newest `keep - 1`
    by sequence, one per sequence."""
    by_sequence = {c["sequence"]: c for c in catalogs}
    newest = sorted(by_sequence)[-(keep - 1) :] if keep > 1 else []
    return [by_sequence[s] for s in newest]


def referenced_digests(catalogs: list[dict]) -> set[str]:
    return {s["digest"] for c in catalogs for s in c["sets"].values()}


def artifact_file(digest: str) -> str:
    algorithm, hex_digest = digest.split(":", 1)
    if algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", hex_digest):
        raise ValueError(f"unexpected digest {digest!r}")
    return f"sha256-{hex_digest}.mwrs"


def held_record(record: dict, since: str, reason: str) -> dict:
    """The previous record of a held set, published again with its `held`.

    Its upstream names its own commit as the ref. A catalog's `upstreams`
    lists the exact (repo, ref, commit) its content came from, one commit per
    repo and ref: the branch ref names the commit this build converted, and a
    held set's content came from an earlier commit, which git addresses by
    itself. Two sets held from the same commit share that entry."""
    upstream = dict(record["upstream"])
    upstream["ref"] = upstream["commit"]
    return {**record, "upstream": upstream, "held": {"since": since, "reason": reason}}


# --- upstreams --------------------------------------------------------------


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout


def resolve(url: str, ref: str) -> str:
    out = git("ls-remote", "--exit-code", url, f"refs/heads/{ref}")
    commit = out.split()[0]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"{url} {ref}: unexpected ls-remote output {out!r}")
    return commit


def fetch(url: str, commit: str, globs: list[str], dest: Path) -> None:
    """Checks out the files matching `globs` at exactly `commit`."""
    dest.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=dest)
    git("fetch", "-q", "--depth", "1", url, commit, cwd=dest)
    head = git("rev-parse", "FETCH_HEAD", cwd=dest).strip()
    if head != commit:
        raise RuntimeError(f"{url}: fetched {head}, expected {commit}")
    git(
        "-c",
        "advice.detachedHead=false",
        "checkout",
        "-q",
        commit,
        "--",
        *[f":(glob){g}" for g in globs],
        cwd=dest,
    )


def list_sets(tree: Path, template: str) -> dict[str, str]:
    """`{name: path}` for the files in `tree` matching `template`."""
    prefix, _, suffix = template.partition("{name}")
    folder, _, stem_prefix = prefix.rpartition("/")
    directory = tree / folder
    found = {}
    if not directory.is_dir():
        return found
    for entry in directory.iterdir():
        file = entry.name
        if (
            entry.is_file()
            and file.startswith(stem_prefix)
            and file.endswith(suffix)
            and len(file) > len(stem_prefix) + len(suffix)
        ):
            name = file[len(stem_prefix) : len(file) - len(suffix)]
            found[name] = prefix + name + suffix
    return found


# --- conversion -------------------------------------------------------------


def cli_version(cli: Path) -> str:
    return subprocess.run(
        [str(cli), "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()


def cli_error(stderr: str) -> str:
    """The CLI's error from its JSON log lines on stderr."""
    for line in reversed(stderr.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        error = event.get("fields", {}).get("error")
        if error:
            return error
    return stderr.strip().splitlines()[-1] if stderr.strip() else "no error output"


def convert(cli: Path, format_: str, source: Path, output: Path) -> tuple[dict | None, str | None]:
    result = subprocess.run(
        [
            str(cli),
            "--log-format",
            "json",
            "--log-filter",
            "error",
            "convert",
            "--format",
            format_,
            str(source),
            "-o",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, cli_error(result.stderr)
    return json.loads(result.stdout), None


# --- build ------------------------------------------------------------------


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return None


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def tree_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@dataclass
class Summary:
    changed: bool
    sequence_base: int | None = None
    sets: int = 0
    published: int = 0
    held: dict[str, dict] = field(default_factory=dict)
    newly_held: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    artifacts: int = 0
    artifact_bytes: int = 0
    dist_bytes: int = 0
    seconds: float = 0.0

    def to_json(self) -> dict:
        reasons: dict[str, int] = {}
        for reason in self.skipped.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "changed": self.changed,
            "previousSequence": self.sequence_base,
            "sets": self.sets,
            "published": self.published,
            "held": self.held,
            "newlyHeld": self.newly_held,
            "skipped": self.skipped,
            "skippedByReason": reasons,
            "removed": self.removed,
            "artifacts": self.artifacts,
            "artifactBytes": self.artifact_bytes,
            "distBytes": self.dist_bytes,
            "seconds": round(self.seconds, 1),
        }


def build(
    cli: Path,
    sources_path: Path,
    previous: Path,
    out: Path,
    work: Path,
    accept: set[str],
    now: dt.datetime,
) -> Summary:
    started = time.monotonic()
    sources_bytes = sources_path.read_bytes()
    sources = tomllib.loads(sources_bytes.decode())
    upstreams = {u["id"]: u for u in sources["upstream"]}
    collections = sources["collection"]
    for c in collections:
        if not COLLECTION_RE.fullmatch(c["name"]):
            raise ValueError(f"invalid collection name {c['name']!r}")

    prev_index = read_json(previous / "index.json")
    prev_status = read_json(previous / "status.json") or {}
    # The signed catalog says which sets were held and since when.
    prev_held: dict[str, dict] = {
        key: entry["held"]
        for key, entry in (prev_index["sets"] if prev_index else {}).items()
        if entry.get("held")
    }

    commits = {i: resolve(u["url"], u["ref"]) for i, u in upstreams.items()}
    inputs = {
        "sources": hashlib.sha256(sources_bytes).hexdigest(),
        "converter": cli_version(cli),
        "upstreams": [
            {"repo": upstreams[i]["repo"], "ref": upstreams[i]["ref"], "commit": commits[i]}
            for i in sorted(upstreams)
        ],
    }
    base = prev_index["sequence"] if prev_index else None
    if prev_index and prev_status.get("inputs") == inputs and not accept:
        return Summary(changed=False, sequence_base=base, seconds=time.monotonic() - started)

    if out.exists():
        raise RuntimeError(f"{out} already exists")
    for i, u in upstreams.items():
        globs = [c["path"].replace("{name}", "*") for c in collections if c["upstream"] == i]
        fetch(u["url"], commits[i], globs, work / "upstream" / i)

    staging = work / "convert"
    staging.mkdir(parents=True, exist_ok=True)

    def staged(key: str) -> Path:
        return staging / f"{hashlib.sha256(key.encode()).hexdigest()}.mwrs"

    summary = Summary(changed=True, sequence_base=base)
    current = set()
    jobs = []
    for c in collections:
        u = upstreams[c["upstream"]]
        tree = work / "upstream" / c["upstream"]
        for name, path in sorted(list_sets(tree, c["path"]).items()):
            key = f"{c['name']}/{name}"
            current.add(key)
            summary.sets += 1
            if NAME_RE.fullmatch(name):
                jobs.append((key, u, commits[c["upstream"]], path, c["format"], tree / path))
            else:
                summary.skipped[key] = "invalid set name"

    (out / "artifacts").mkdir(parents=True)
    with concurrent.futures.ThreadPoolExecutor(os.cpu_count() or 4) as pool:
        results = list(
            pool.map(lambda j: convert(cli, j[4], j[5], staged(j[0])), jobs)
        )

    held: dict[str, dict] = {}
    for (key, u, commit, path, _, _), (record, error) in zip(jobs, results):
        prev_entry = prev_index["sets"].get(key) if prev_index else None
        decision = decide(
            prev_entry["entries"] if prev_entry else None,
            record["entries"] if record else None,
            error,
            key in accept,
        )
        collection, set_name = key.split("/", 1)
        record_path = out / "sets" / collection / f"{set_name}.json"
        if decision.action == "skip":
            summary.skipped[key] = decision.reason
        elif decision.action == "hold":
            previous_record = read_json(previous / "sets" / collection / f"{set_name}.json")
            if previous_record is None or previous_record["conversion"]["digest"] != prev_entry["digest"]:
                raise RuntimeError(f"{key}: previous record does not match the previous catalog")
            since = prev_held[key]["since"] if key in prev_held else now.isoformat().replace("+00:00", "Z")
            write_json(record_path, held_record(previous_record, since, decision.reason))
            copy_artifact(previous, out, prev_entry["digest"])
            held[key] = {"since": since, "reason": decision.reason, "digest": prev_entry["digest"]}
            if key not in prev_held:
                summary.newly_held.append(key)
        else:
            write_json(
                record_path,
                {
                    "upstream": {"repo": u["repo"], "ref": u["ref"], "commit": commit, "path": path},
                    "conversion": record,
                },
            )
            artifact = out / "artifacts" / artifact_file(record["digest"])
            if not artifact.exists():
                os.replace(staged(key), artifact)
            summary.published += 1
    summary.held = held
    if prev_index:
        summary.removed = sorted(set(prev_index["sets"]) - current)

    history = []
    for file in sorted((previous / "history").glob("*.json")):
        history.append((json.loads(file.read_bytes()), file.read_bytes()))
    if prev_index:
        history.append((prev_index, (previous / "index.json").read_bytes()))
    kept = retained_catalogs([h for h, _ in history])
    kept_sequences = {c["sequence"] for c in kept}
    (out / "history").mkdir()
    for catalog, raw in history:
        if catalog["sequence"] in kept_sequences:
            (out / "history" / f"{catalog['sequence']}.json").write_bytes(raw)
    for digest in sorted(referenced_digests(kept)):
        copy_artifact(previous, out, digest)
    if prev_index:
        shutil.copyfile(previous / "index.json", out / "index.json")

    write_json(
        out / "status.json",
        {"schema": STATUS_SCHEMA, "inputs": inputs, "held": held, "skipped": summary.skipped},
    )
    summary.artifacts = sum(1 for _ in (out / "artifacts").iterdir())
    summary.artifact_bytes = tree_bytes(out / "artifacts")
    summary.dist_bytes = tree_bytes(out)
    summary.seconds = time.monotonic() - started
    return summary


def copy_artifact(previous: Path, out: Path, digest: str) -> None:
    name = artifact_file(digest)
    target = out / "artifacts" / name
    if not target.exists():
        source = previous / "artifacts" / name
        if not source.is_file():
            raise RuntimeError(f"previous dist lacks artifact {name}")
        shutil.copyfile(source, target)


def notes(summary: dict, index: dict | None) -> str:
    """Markdown for the release, the job summary and the hold issue."""
    lines = []
    if index is None:
        lines.append(f"No new catalog: inputs unchanged since sequence {summary['previousSequence']}.")
        return "\n".join(lines) + "\n"
    lines += [
        f"Catalog sequence {index['sequence']}, generated {index['generatedAt']} by meshway-rules {index['converter']['version']} (MWRS {index['converter']['formatMajor']}.{index['converter']['formatMinor']}).",
        "",
        "| Upstream | Ref | Commit |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {u['repo']} | {u['ref']} | `{u['commit']}` |" for u in index["upstreams"]]
    lines += [
        "",
        f"- Sets: {summary['sets']} upstream, {len(index['sets'])} in the catalog ({summary['published']} converted now, {len(summary['held'])} held)",
        f"- Artifacts in `dist`: {summary['artifacts']} ({summary['artifactBytes']} bytes; tree without index.json {summary['distBytes']} bytes)",
        f"- Build time: {summary['seconds']} s",
    ]
    if summary["held"]:
        lines += ["", "Held (previous version kept):", ""]
        lines += [
            f"- `{name}` since {h['since']}: {h['reason']}" + (" **(new)**" if name in summary["newlyHeld"] else "")
            for name, h in sorted(summary["held"].items())
        ]
    if summary["skipped"]:
        lines += ["", "Not published:", ""]
        lines += [f"- `{name}`: {reason}" for name, reason in sorted(summary["skipped"].items())]
    if summary["removed"]:
        lines += ["", "Removed upstream:", ""]
        lines += [f"- `{name}`" for name in summary["removed"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--cli", type=Path, required=True)
    b.add_argument("--sources", type=Path, required=True)
    b.add_argument("--previous", type=Path, required=True, help="previous dist tree; may be empty")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--work", type=Path, required=True)
    b.add_argument("--summary", type=Path, required=True)
    b.add_argument("--accept", nargs="*", default=[], help="sets whose entry-drop hold is released")
    n = sub.add_parser("notes", help="markdown report of a build")
    n.add_argument("--summary", type=Path, required=True)
    n.add_argument("--index", type=Path, help="the signed index.json, when the build changed")
    args = parser.parse_args(argv)
    if args.command == "notes":
        index = read_json(args.index) if args.index else None
        sys.stdout.write(notes(json.loads(args.summary.read_bytes()), index))
        return 0
    summary = build(
        args.cli.resolve(),
        args.sources,
        args.previous,
        args.out,
        args.work,
        set(args.accept),
        dt.datetime.now(dt.timezone.utc).replace(microsecond=0),
    )
    write_json(args.summary, summary.to_json())
    print(json.dumps({k: v for k, v in summary.to_json().items() if k not in ("skipped", "held")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
