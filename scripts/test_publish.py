"""Tests for publish.py: hold policy, retention, and whole builds against a
local upstream repository with a stub CLI (the real CLI is exercised by the
publish workflow's `index` and `verify` steps)."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import publish  # noqa: E402

STUB_CLI = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import hashlib, json, sys
    args = sys.argv[1:]
    if args == ["--version"]:
        print("meshway-rules 0.0.0-test")
        sys.exit(0)
    source, output = args[args.index("--format") + 2], args[args.index("-o") + 1]
    lines = sorted({l.strip() for l in open(source) if l.strip()})
    if "FAIL" in lines:
        print(json.dumps({"level": "ERROR", "fields": {"message": "meshway-rules failed",
              "error": "Invalid source: stub"}}), file=sys.stderr)
        sys.exit(1)
    data = "\\n".join(lines).encode()
    open(output, "wb").write(data)
    print(json.dumps({"digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                      "bytes": len(data), "entries": len(lines)}))
    """
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class DecideTest(unittest.TestCase):
    def test_new_set(self):
        self.assertEqual(publish.decide(None, 10, None, False).action, "publish")

    def test_failure_without_previous_skips(self):
        d = publish.decide(None, None, "Invalid source: x", False)
        self.assertEqual((d.action, d.reason), ("skip", "conversion failed: Invalid source: x"))

    def test_failure_with_previous_holds(self):
        d = publish.decide(10, None, "too large", False)
        self.assertEqual((d.action, d.reason), ("hold", "conversion failed: too large"))

    def test_failure_is_not_accepted(self):
        self.assertEqual(publish.decide(10, None, "x", True).action, "hold")

    def test_drop_over_half_holds(self):
        d = publish.decide(100, 49, None, False)
        self.assertEqual((d.action, d.reason), ("hold", "entries dropped from 100 to 49"))

    def test_drop_of_exactly_half_publishes(self):
        self.assertEqual(publish.decide(100, 50, None, False).action, "publish")

    def test_growth_publishes(self):
        self.assertEqual(publish.decide(10, 1000, None, False).action, "publish")

    def test_accepted_drop_publishes(self):
        self.assertEqual(publish.decide(100, 1, None, True).action, "publish")


def catalog(sequence: int, *digests: str) -> dict:
    return {
        "sequence": sequence,
        "sets": {f"c/{i}": {"digest": d} for i, d in enumerate(digests)},
    }


class RetentionTest(unittest.TestCase):
    def test_keeps_six_previous(self):
        catalogs = [catalog(s, f"sha256:{s}") for s in range(1, 11)]
        kept = publish.retained_catalogs(catalogs)
        self.assertEqual([c["sequence"] for c in kept], [5, 6, 7, 8, 9, 10])

    def test_order_and_duplicates(self):
        catalogs = [catalog(3), catalog(1), catalog(2), catalog(3)]
        self.assertEqual([c["sequence"] for c in publish.retained_catalogs(catalogs)], [1, 2, 3])

    def test_keep_one_is_only_the_new_catalog(self):
        self.assertEqual(publish.retained_catalogs([catalog(1)], keep=1), [])

    def test_referenced(self):
        kept = [catalog(1, "a", "b"), catalog(2, "b", "c")]
        self.assertEqual(publish.referenced_digests(kept), {"a", "b", "c"})


class HelpersTest(unittest.TestCase):
    def test_pinned(self):
        record = {"upstream": {"repo": "r", "ref": "meta", "commit": "abc", "path": "p"}, "conversion": {}}
        self.assertEqual(publish.pinned(record)["upstream"]["ref"], "abc")
        self.assertEqual(record["upstream"]["ref"], "meta")

    def test_artifact_file(self):
        self.assertEqual(publish.artifact_file("sha256:" + "a" * 64), "sha256-" + "a" * 64 + ".mwrs")
        with self.assertRaises(ValueError):
            publish.artifact_file("sha256:../x")

    def test_list_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "geo" / "classical"
            d.mkdir(parents=True)
            for f in ["a.list", "b@cn.list", "c.yaml", ".list"]:
                (d / f).write_text("")
            (d / "sub.list").mkdir()
            found = publish.list_sets(Path(tmp), "geo/classical/{name}.list")
            self.assertEqual(found, {"a": "geo/classical/a.list", "b@cn": "geo/classical/b@cn.list"})


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.cli = root / "cli"
        self.cli.write_text(STUB_CLI)
        self.cli.chmod(0o755)
        self.upstream = root / "upstream"
        self.upstream.mkdir()
        git(self.upstream, "init", "-q", "-b", "meta")
        self.sources = root / "sources.toml"
        self.sources.write_text(
            textwrap.dedent(
                f"""\
                [[upstream]]
                id = "u"
                repo = "Example/rules"
                ref = "meta"
                url = "{self.upstream}"

                [[collection]]
                name = "site"
                upstream = "u"
                path = "geo/{{name}}.list"
                format = "classical"
                """
            )
        )
        self.dist = root / "dist-0"
        self.dist.mkdir()
        self.run_count = 0

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, files: dict[str, str | None]) -> str:
        for name, content in files.items():
            path = self.upstream / "geo" / f"{name}.list"
            path.parent.mkdir(exist_ok=True)
            if content is None:
                path.unlink()
            else:
                path.write_text(content)
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-q", "--allow-empty", "-m", "c")
        return git(self.upstream, "rev-parse", "HEAD").strip()

    def build(self, accept=()) -> publish.Summary:
        """One run; on change, plays `meshway-rules index` by writing the
        next sequence, and makes the result the previous dist."""
        self.run_count += 1
        out = self.root / f"dist-{self.run_count}"
        summary = publish.build(
            self.cli,
            self.sources,
            self.dist,
            out,
            self.root / f"work-{self.run_count}",
            set(accept),
            dt.datetime(2026, 10, self.run_count, tzinfo=dt.timezone.utc),
        )
        if summary.changed:
            previous = publish.read_json(out / "index.json")
            sets = {}
            for f in (out / "sets").rglob("*.json"):
                record = json.loads(f.read_text())
                sets[f"{f.parent.name}/{f.stem}"] = {
                    "digest": record["conversion"]["digest"],
                    "entries": record["conversion"]["entries"],
                }
            index = {"sequence": previous["sequence"] + 1 if previous else 1, "sets": sets}
            (out / "index.json").write_text(json.dumps(index))
            self.dist = out
        return summary

    def record(self, name: str) -> dict:
        return json.loads((self.dist / "sets" / "site" / f"{name}.json").read_text())

    def artifacts(self) -> set[str]:
        return {f.name for f in (self.dist / "artifacts").iterdir()}

    def test_publish_hold_accept_and_skip(self):
        first = self.commit({"a": "x\ny\nz\nw\n", "b": "FAIL\n", "Bad": "x\n"})
        s = self.build()
        self.assertEqual((s.sets, s.published), (3, 1))
        self.assertEqual(s.skipped, {"site/b": "conversion failed: Invalid source: stub", "site/Bad": "invalid set name"})
        self.assertEqual(self.record("a")["upstream"], {"repo": "Example/rules", "ref": "meta", "commit": first, "path": "geo/a.list"})
        a1 = self.record("a")["conversion"]["digest"]

        self.assertFalse(self.build().changed, "unchanged inputs publish nothing")

        self.commit({"a": "FAIL\n"})
        s = self.build()
        self.assertEqual(s.newly_held, ["site/a"])
        self.assertEqual(s.held["site/a"], {"since": "2026-10-03T00:00:00Z", "reason": "conversion failed: Invalid source: stub", "digest": a1})
        self.assertEqual(self.record("a")["upstream"]["ref"], first, "held record pins its commit")
        self.assertIn(publish.artifact_file(a1), self.artifacts())

        self.commit({"a": "x\n"})
        s = self.build()
        self.assertEqual(s.newly_held, [], "still held, not newly")
        self.assertEqual(s.held["site/a"]["since"], "2026-10-03T00:00:00Z")
        self.assertEqual(s.held["site/a"]["reason"], "entries dropped from 4 to 1")
        status = json.loads((self.dist / "status.json").read_text())
        self.assertEqual(status["held"], s.held)

        s = self.build(accept=["site/a"])
        self.assertEqual(s.held, {})
        self.assertEqual(self.record("a")["conversion"]["entries"], 1)
        self.assertEqual(self.record("a")["upstream"]["ref"], "meta")

        self.commit({"a": None})
        s = self.build()
        self.assertEqual(s.removed, ["site/a"])

    def test_retains_artifacts_of_seven_catalogs(self):
        digests = []
        for i in range(10):
            self.commit({"a": f"v{i}\n"})
            self.build()
            digests.append(self.record("a")["conversion"]["digest"])
        self.assertEqual(self.artifacts(), {publish.artifact_file(d) for d in digests[-7:]})
        self.assertEqual(sorted(int(f.stem) for f in (self.dist / "history").iterdir()), [4, 5, 6, 7, 8, 9])


if __name__ == "__main__":
    unittest.main()
