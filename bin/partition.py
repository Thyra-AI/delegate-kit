#!/usr/bin/env python3
"""partition.py - a cheap, deterministic *suggestion* for splitting work across subagents.

    python bin/partition.py map   --wu wus.json [--repo .] [--out partition.map.json]
    python bin/partition.py plan  --map partition.map.json [--budget 100000] [--out partition.json]
    python bin/partition.py check --plan partition.json [--after --base <sha>] [--write]
    python bin/partition.py show  <cluster-id> --plan partition.json
    python bin/partition.py auto  --wu wus.json [--repo .] [--out-dir DIR]

A planning agent runs `auto` once, reads the table, and overrides it with
judgment. Output is advisory: warnings never change the exit code, and `check`
accepts a partition.json that an agent has hand-edited (the cluster `files`
lists are the source of truth there: to merge two clusters, merge their `wus`
and `files` lists; to strike a false hit, delete its entry).

wus.json is a list of
  {"id","title","kind":"build|apply|wire|verify",
   "seeds":{"symbols":[],"patterns":[],"paths":[],"globs":[]},
   "depends_on":[],"readonly":false}

Stdlib only, Python >= 3.8. Runs git with argument lists (never a shell).
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys

VERSION = 1

# ---------------------------------------------------------------- constants

EXCLUDE_DIRS = {
    "node_modules": "vendored", "vendor": "vendored", "vendored": "vendored",
    "third_party": "vendored", "bower_components": "vendored",
    "dist": "dist", "build": "dist", "out": "dist", "target": "dist",
    ".next": "dist", ".nuxt": "dist", "coverage": "dist",
    ".git": "meta", ".hg": "meta", ".svn": "meta", ".venv": "meta",
    "venv": "meta", "__pycache__": "meta", ".tox": "meta",
    ".mypy_cache": "meta", ".pytest_cache": "meta", ".idea": "meta",
    ".vscode": "meta", ".delegate-kit": "meta", "graphify-out": "meta",
    ".serena": "meta",
}
LOCK_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "go.sum", "poetry.lock", "pipfile.lock", "gemfile.lock", "composer.lock",
    "cargo.lock", "bun.lockb", "uv.lock", "pdm.lock",
}
LOCK_EXT = (".lock", ".min.js", ".min.css", ".map")

LANG = {
    ".py": "python", ".pyi": "python", ".js": "js", ".jsx": "js", ".mjs": "js",
    ".cjs": "js", ".ts": "ts", ".tsx": "ts", ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".cs": "csharp", ".rb": "ruby",
    ".php": "php", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".hpp": "cpp", ".swift": "swift", ".scala": "scala", ".sh": "shell",
    ".bash": "shell", ".ps1": "powershell", ".sql": "sql", ".lua": "lua",
    ".md": "docs", ".rst": "docs", ".txt": "docs", ".adoc": "docs",
    ".json": "config", ".yml": "config", ".yaml": "config", ".toml": "config",
    ".ini": "config", ".cfg": "config", ".html": "web", ".css": "web",
    ".scss": "web", ".vue": "web", ".svelte": "web",
}
NON_CODE = {"docs", "config", "web", "sql"}
HASH_COMMENT = {"python", "shell", "ruby", "config", "powershell"}

TEST_RE = re.compile(
    r"(^|/)(tests?|Tests?|__tests__|specs?|e2e)(/|$)"
    r"|(^|/)(test_[^/]*|[^/]*_test\.[^/]+|[^/]*\.(test|spec)\.[^/]+|[^/]*_spec\.[^/]+"
    r"|[^/]*Tests?\.(java|kt|cs|scala|swift|php))$")
TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "e2e"}
GENERIC_STEMS = {"__init__", "index", "mod", "main", "lib"}

# Sizing model, calibrated from bin/bench.py runs (peak = max per-request input
# context): peak ~= 20k base + ~2.5k per tool call (1.7k-3.8k; new code is the high end).
#   est_hops   = hops_base + sum over WUs/files by kind:
#                  build  : per planned new file hops_new_small / hops_new / hops_new_large by the
#                           WU's optional "size" (small|medium|large, default medium);
#                           hops_edit per existing file
#                  apply/wire : hops_per_file per file + hits / hits_per_hop
#                  verify : hops_verify per verify WU (its files are not read)
#   est_tokens = overhead + est_hops * hop_tokens
DEFAULTS = {
    "budget": 100000, "overhead": 20000, "hop_tokens": 2500, "hops_base": 6,
    "hops_new_small": 8, "hops_new": 20, "hops_new_large": 30, "hops_edit": 3, "hops_per_file": 3, "hits_per_hop": 4,
    "hops_verify": 15,
    "fanout": 15, "low_util": 0.25, "single_max_files": 15,
}

# ---------------------------------------------------------------- utilities


SIZE_HOPS = {"small": "hops_new_small", "medium": "hops_new", "large": "hops_new_large"}


def nat_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def posix(p):
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def fmt_k(n):
    n = int(n)
    if n >= 1000:
        return "%dk" % (n // 1000) if n % 1000 == 0 else "%.1fk" % (n / 1000.0)
    return str(n)


def clip(s, n=170):
    return s if len(s) <= n else s[: n - 3] + "..."


def esc_ere(s):
    return re.sub(r"([.^$*+?(){}\[\]|\\])", r"\\\1", s)


def run_git(args, cwd):
    cmd = ["git", "-c", "core.quotepath=off", "--no-pager"] + list(args)
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return 127, ""
    return p.returncode, (p.stdout or "").replace("\r", "")


def find_root(repo):
    repo = os.path.abspath(repo)
    rc, out = run_git(["rev-parse", "--show-toplevel"], repo)
    if rc == 0 and out.strip():
        return os.path.normpath(out.strip().split("\n")[0]), True
    return repo, False


def git_head(root):
    rc, out = run_git(["rev-parse", "HEAD"], root)
    return out.strip() if rc == 0 else ""


def classify(path):
    """None for a normal file, else 'vendored'|'dist'|'lock'|'meta'."""
    parts = path.split("/")
    for seg in parts[:-1]:
        cat = EXCLUDE_DIRS.get(seg)
        if cat:
            return cat
    low = parts[-1].lower()
    if low in LOCK_NAMES or low.endswith(LOCK_EXT):
        return "lock"
    return None


def lang_of(path):
    return LANG.get(os.path.splitext(path)[1].lower(), "other")


def write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
        f.write("\n")


def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        die("cannot read %s %s: %s" % (what, path, e))


def read_object(path, what):
    data = read_json(path, what)
    if not isinstance(data, dict):
        die("%s must be a JSON object" % what)
    return data


def die(msg, code=2):
    sys.stderr.write("partition: %s\n" % msg)
    sys.exit(code)


# ---------------------------------------------------------------- repo access


class Repo(object):
    def __init__(self, root, is_git, skip=()):
        self.root = root
        self.is_git = is_git
        self.skip = set(skip)  # the wus file itself is input, not code
        self._files = None
        self._cache = {}

    def files(self):
        """Non-excluded existing files, POSIX paths relative to root."""
        if self._files is not None:
            return self._files
        out = []
        if self.is_git:
            rc, txt = run_git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], self.root)
            if rc == 0:
                names = [n for n in txt.split("\0") if n]
            else:
                names = self._walk()
        else:
            names = self._walk()
        for n in names:
            n = posix(n)
            if classify(n) is None and n not in self.skip and os.path.isfile(os.path.join(self.root, n)):
                out.append(n)
        self._files = sorted(set(out))
        return self._files

    def _walk(self):
        res = []
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
            for fn in fns:
                res.append(os.path.relpath(os.path.join(dp, fn), self.root))
        return res

    def _lines(self, path):
        if path in self._cache:
            return self._cache[path]
        lines = None
        try:
            full = os.path.join(self.root, path)
            if os.path.getsize(full) <= 2 * 1024 * 1024:
                with open(full, "rb") as f:
                    data = f.read()
                if b"\0" not in data[:8192]:
                    lines = data.decode("utf-8", "replace").replace("\r", "").split("\n")
        except OSError:
            pass
        self._cache[path] = lines
        return lines

    def grep(self, patterns):
        """[(path, lineno, text)] for lines matching any ERE in `patterns`."""
        if not patterns:
            return []
        if self.is_git:
            args = ["grep", "-n", "-I", "-E", "--untracked", "--no-color"]
            for p in patterns:
                args += ["-e", p]
            rc, out = run_git(args, self.root)
            if rc in (0, 1):
                res = []
                for ln in out.split("\n"):
                    m = re.match(r"^(.+?):(\d+):(.*)$", ln)
                    if m:
                        res.append((posix(m.group(1)), int(m.group(2)), m.group(3)))
                return res
        try:
            rx = re.compile("|".join("(?:%s)" % p for p in patterns))
        except re.error:
            return []
        res = []
        for path in self.files():
            lines = self._lines(path)
            if not lines:
                continue
            for i, text in enumerate(lines, 1):
                if rx.search(text):
                    res.append((path, i, text))
        return res


# ---------------------------------------------------------------- map


def norm_name(s):
    return re.sub(r"[_\-. ]", "", s.lower())


def test_base(path):
    name = os.path.splitext(posix(path).split("/")[-1])[0]
    name = re.sub(r"\.(test|spec)$", "", name)
    name = re.sub(r"^test_|_test$|_spec$", "", name)
    name = re.sub(r"Tests?$", "", name)
    return norm_name(name)


def source_stem(path):
    parts = path.split("/")
    stem = os.path.splitext(parts[-1])[0]
    if stem in GENERIC_STEMS and len(parts) > 1:
        stem = parts[-2]
    return stem


def glob_rx(g):
    g = posix(g)
    out, i = "", 0
    while i < len(g):
        c = g[i]
        if g.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if g.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile("^" + out + "$")


def is_comment(text, lang):
    t = text.strip()
    if not t:
        return False
    if t.startswith(("//", "/*", "*", "<!--", '"""', "'''", "--", ";")):
        return True
    if t.startswith("#"):
        return t[:2] == "# " or (lang in HASH_COMMENT and (len(t) == 1 or t[1] in "#!"))
    return False


def build_wu_hits(repo, wu, warns):
    seeds = wu["seeds"]
    hits, excluded, planned = {}, {"hits": 0, "files": set()}, []
    matchers, gpats = [], []
    for s in seeds["symbols"]:
        matchers.append(re.compile(r"(?<!\w)%s(?!\w)" % re.escape(s)))
        gpats.append(esc_ere(s))
    for p in seeds["patterns"]:
        try:
            matchers.append(re.compile(p))
            gpats.append(p)
        except re.error as e:
            warns.append({"code": "INPUT", "cluster": "", "msg": clip("%s: bad pattern %r skipped (%s)" % (wu["id"], p, e))})
    if matchers:
        for path, ln, text in repo.grep(gpats):
            cat = classify(path)
            if cat == "meta" or path in repo.skip:
                continue
            if not any(m.search(text) for m in matchers):
                continue
            if cat:
                excluded["hits"] += 1
                excluded["files"].add(path)
                continue
            h = hits.setdefault(path, {"hits": 0, "hit_lines": [], "all_lines": [], "comment_hits": 0, "via": set()})
            h["hits"] += 1
            h["all_lines"].append(ln)
            if len(h["hit_lines"]) < 5:
                h["hit_lines"].append(ln)
            if is_comment(text, lang_of(path)):
                h["comment_hits"] += 1
            h["via"].add("grep")
    files = repo.files()
    fset = set(files)
    for p in seeds["paths"]:
        p = posix(p).rstrip("/")
        if not p:
            continue
        if p in fset:
            found = [p]
        else:
            found = [f for f in files if f.startswith(p + "/")]
            if not found and os.path.isfile(os.path.join(repo.root, p)) and classify(p) is None:
                found = [p]
        if not found:
            planned.append(p)
        for f in found:
            h = hits.setdefault(f, {"hits": 0, "hit_lines": [], "all_lines": [], "comment_hits": 0, "via": set()})
            h["via"].add("path")
    for g in seeds["globs"]:
        rx = glob_rx(g)
        found = [f for f in files if rx.match(f)]
        if not found:
            planned.append(posix(g))
        for f in found:
            h = hits.setdefault(f, {"hits": 0, "hit_lines": [], "all_lines": [], "comment_hits": 0, "via": set()})
            h["via"].add("glob")
    return hits, excluded, sorted(set(planned))


def file_static(repo, path, all_lines, bpt, window=15):
    full = os.path.join(repo.root, path)
    try:
        size = os.path.getsize(full)
        if size > 8 * 1024 * 1024:
            lines = max(1, size // 40)
        else:
            with open(full, "rb") as f:
                data = f.read()
            lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    except OSError:
        return None
    tok = int(-(-size // bpt))
    narrow = tok
    if all_lines and lines:
        covered = set()
        for ln in all_lines:
            covered.update(range(max(1, ln - window), min(lines, ln + window) + 1))
        narrow = min(tok, int(len(covered) * (float(size) / lines) / bpt) + 1)
    return {"lines": lines, "bytes": size, "tokens_est": tok, "tokens_narrow": narrow, "lang": lang_of(path)}


# A line only counts as an import when it starts (after whitespace) with an import construct.
IMPORT_LINE = (r"^\s*(?:import\b|from\b|require\s*\(|(?:const|let|var)\b[^=\n]*=\s*require\s*\("
               r"|use\b|#\s*include\b|@import\b)")


def find_importers(repo, finfo, warns, cap):
    targets = {}
    cands = sorted(p for p, i in finfo.items()
                   if not i["is_test"] and i["lang"] not in NON_CODE and i["lines"] > 0)
    if len(cands) > cap:
        warns.append({"code": "INPUT", "cluster": "", "msg": "importer scan limited to %d of %d files (--max-importer-files)" % (cap, len(cands))})
        cands = cands[:cap]
    for p in cands:
        stem = source_stem(p)
        if len(stem) >= 3:
            targets.setdefault(stem, []).append(p)
    stems = sorted(targets)
    imp = collections.defaultdict(set)
    for k in range(0, len(stems), 40):
        chunk = stems[k:k + 40]
        pat = "(import|require|from|use|include).*(%s)" % "|".join(esc_ere(s) for s in chunk)
        rxs = [(s, re.compile(IMPORT_LINE + r".*(?<!\w)%s(?!\w)" % re.escape(s))) for s in chunk]
        for path, ln, text in repo.grep([pat]):
            if classify(path) is not None or len(text) > 600:
                continue
            for s, rx in rxs:
                if rx.search(text):
                    for tgt in targets[s]:
                        if tgt != path:
                            imp[tgt].add(path)
    for p in cands:
        lst = sorted(imp.get(p, ()))
        finfo[p]["importers_n"] = len(lst)
        finfo[p]["importers"] = lst[:20]


def map_tests(repo, finfo):
    allf = repo.files()
    tests = [f for f in allf if TEST_RE.search(f)]
    tset = set(tests)
    by_base = collections.defaultdict(list)
    for t in tests:
        by_base[test_base(t)].append(t)

    def strip_dir(d):
        segs = [s for s in d.split("/") if s]
        if segs and segs[0] in ("src", "lib", "app"):
            segs = segs[1:]
        return [s for s in segs if s not in TEST_DIRS]

    by_dir = collections.defaultdict(list)
    for t in tests:
        by_dir["/".join(strip_dir(os.path.dirname(t)))].append(t)
    pending = {}
    for p, info in sorted(finfo.items()):
        if p in tset:
            info["is_test"] = True
            info["tests"] = [p]
            continue
        info["is_test"] = False
        if info["lang"] in NON_CODE or info["lang"] == "other":
            info["tests"] = []
            continue
        found = sorted(by_base.get(norm_name(source_stem(p)), []))[:6]
        if not found:
            d = "/".join(strip_dir(os.path.dirname(p)))
            if d:
                found = sorted(by_dir.get(d, []))[:6]
        info["tests"] = found
        if not found:
            pending[p] = source_stem(p)
    # tier 3: a test file that mentions the module name
    stems = sorted(set(s for s in pending.values() if len(s) >= 3))[:200]
    hitmap = collections.defaultdict(collections.Counter)
    for k in range(0, len(stems), 40):
        chunk = stems[k:k + 40]
        rxs = [(s, re.compile(r"(?<!\w)%s(?!\w)" % re.escape(s))) for s in chunk]
        for path, ln, text in repo.grep(["|".join(esc_ere(s) for s in chunk)]):
            if path in tset and len(text) < 600:
                for s, rx in rxs:
                    if rx.search(text):
                        hitmap[s][path] += 1
    for p, s in pending.items():
        c = hitmap.get(s)
        if c:
            finfo[p]["tests"] = [t for t, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:4]]


def load_graph(root, head):
    """Digest of graphify-out/graph.json or None. Skipped silently on any problem."""
    gp = os.path.join(root, "graphify-out", "graph.json")
    if not os.path.isfile(gp):
        return None
    try:
        size = os.path.getsize(gp)
        with open(gp, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        mm = re.search(r'"built_at_commit"\s*:\s*"([^"]*)"', raw)
        commit = mm.group(1) if mm else ""
        cpath = None
        if commit:
            cpath = os.path.join(root, ".delegate-kit", "cache", "graphify-%s.json" % re.sub(r"[^0-9A-Za-z]", "", commit)[:40])
            if os.path.isfile(cpath):
                try:
                    with open(cpath, encoding="utf-8") as f:
                        dg = json.load(f)
                    if dg.get("src_size") == size and dg.get("version") == VERSION:
                        return finish_graph(dg, commit, head)
                except (OSError, ValueError):
                    pass
        data = json.loads(raw)
        del raw
        dg = graph_digest(data, root, commit, size)
        if cpath:
            try:
                write_json(cpath, dg)
            except OSError:
                pass
        return finish_graph(dg, commit, head)
    except Exception:
        return None


def graph_digest(data, root, commit, size):
    node_file, votes = {}, collections.defaultdict(collections.Counter)
    for n in data.get("nodes", []):
        sf = n.get("source_file")
        if not sf or not isinstance(sf, str):
            continue
        sf = posix(sf)
        if os.path.isabs(sf):
            try:
                sf = posix(os.path.relpath(sf, root))
            except ValueError:
                continue
        node_file[n.get("id")] = sf
        if n.get("community") is not None:
            votes[sf][n["community"]] += 1
    community = {}
    for f, c in votes.items():
        best = sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]
        community[f] = best
    coupling = collections.Counter()
    for l in data.get("links") or data.get("edges") or []:
        a, b = node_file.get(l.get("source")), node_file.get(l.get("target"))
        if a and b and a != b:
            try:
                w = float(l.get("weight", 1))
            except (TypeError, ValueError):
                w = 1.0
            coupling[tuple(sorted((a, b)))] += w
    top = sorted(coupling.items(), key=lambda kv: (-kv[1], kv[0]))[:20000]
    return {"version": VERSION, "built_at_commit": commit, "src_size": size, "community": community,
            "coupling": [[a, b, round(w, 2)] for (a, b), w in top]}


def finish_graph(dg, commit, head):
    stale = True
    if commit and head:
        stale = not (head.startswith(commit) or commit.startswith(head))
    return {"community": dg["community"], "coupling": dg["coupling"], "built_at_commit": commit, "stale": stale}


def build_map(wus, repo, args):
    warns = []
    head = git_head(repo.root) if repo.is_git else ""
    wu_out, finfo, lines_by_file = [], {}, collections.defaultdict(set)
    for wu in wus:
        hits, excluded, planned = build_wu_hits(repo, wu, warns)
        for p, h in hits.items():
            lines_by_file[p].update(h["all_lines"])
        if not hits and not planned:
            warns.append({"code": "INPUT", "cluster": "", "msg": "%s: seeds matched no files" % wu["id"]})
        if wu.get("size_bad") is not None:
            warns.append({"code": "INPUT", "cluster": "", "msg": clip("%s: invalid size %r (want small|medium|large), using medium" % (wu["id"], wu["size_bad"]))})
        wu_out.append({
            "id": wu["id"], "title": wu["title"], "kind": wu["kind"], "size": wu.get("size", "medium"),
            "readonly": wu["readonly"],
            "depends_on": wu["depends_on"], "seeds": wu["seeds"], "planned": planned,
            "excluded": {"hits": excluded["hits"], "files": len(excluded["files"])},
            "hits": {p: {"hits": h["hits"], "hit_lines": h["hit_lines"], "comment_hits": h["comment_hits"],
                         "via": sorted(h["via"])} for p, h in sorted(hits.items())},
        })
    for p in sorted(lines_by_file.keys() | set(p for w in wu_out for p in w["hits"])):
        st = file_static(repo, p, sorted(lines_by_file.get(p, ())), args.bpt)
        if st:
            finfo[p] = st
    for w in wu_out:
        for p in [p for p in w["hits"] if p not in finfo]:
            del w["hits"][p]
    map_tests(repo, finfo)
    find_importers(repo, finfo, warns, args.max_importer_files)
    graph = {"used": False}
    coupling = []
    dg = load_graph(repo.root, head)
    if dg:
        graph = {"used": True, "built_at_commit": dg["built_at_commit"], "stale": dg["stale"]}
        for p in finfo:
            if p in dg["community"]:
                finfo[p]["community"] = dg["community"][p]
        coupling = [c for c in dg["coupling"] if c[0] in finfo and c[1] in finfo]
    return {"version": VERSION, "root": posix(repo.root), "git": repo.is_git, "head": head,
            "bytes_per_token": args.bpt, "graph": graph, "coupling": coupling,
            "wus": wu_out, "files": finfo, "input_warnings": warns}


def load_wus(path):
    data = read_json(path, "wus file")
    if isinstance(data, dict):
        data = data.get("wus", [])
    if not isinstance(data, list) or not data:
        die("%s must be a non-empty JSON list of work units" % path)
    out, seen = [], set()
    for i, w in enumerate(data):
        if not isinstance(w, dict) or not w.get("id"):
            die("work unit #%d has no id" % (i + 1))
        wid = str(w["id"])
        if wid in seen:
            die("duplicate work unit id %s" % wid)
        seen.add(wid)
        seeds = w.get("seeds") or {}
        kind = str(w.get("kind") or "apply").lower()
        size = w.get("size")
        size_bad = None
        if size is None:
            size = "medium"
        else:
            size = str(size).strip().lower()
            if size not in SIZE_HOPS:
                size_bad, size = str(w.get("size")), "medium"
        out.append({
            "id": wid, "title": str(w.get("title") or wid), "kind": kind,
            "size": size, "size_bad": size_bad,
            "readonly": bool(w.get("readonly", False)),
            "depends_on": [str(d) for d in (w.get("depends_on") or [])],
            "seeds": {k: [str(x) for x in (seeds.get(k) or [])] for k in ("symbols", "patterns", "paths", "globs")},
        })
    return out


# ---------------------------------------------------------------- plan


def estimate(files, params, wu_ids=(), kinds=None, sizes=None):
    """Sizing numbers only, from WU kinds (see DEFAULTS). `files` entries carry "wus", "hits",
    "planned"; `kinds` maps WU id -> kind (unknown or missing ids count as apply); `sizes` maps
    WU id -> small|medium|large for build WUs (missing or unknown count as medium)."""
    kinds = kinds or {}
    sizes = sizes or {}
    hops = float(params["hops_base"])
    for w in wu_ids:
        if kinds.get(w) == "verify":
            hops += params["hops_verify"]
    for f in files:
        best = 0.0
        for w in f.get("wus") or [None]:
            k = kinds.get(w, "apply")
            if k == "verify":
                continue
            if k == "build":
                h = params[SIZE_HOPS.get(sizes.get(w), "hops_new")] if f.get("planned") else params["hops_edit"]
            else:
                h = params["hops_per_file"] + f.get("hits", 0) / float(params["hits_per_hop"])
            best = max(best, h)
        hops += best
    return int(params["overhead"] + hops * params["hop_tokens"]), int(round(hops))


def reaches(deps, a, b, seen=None):
    """True if a depends (transitively) on b."""
    seen = seen if seen is not None else set()
    for d in deps.get(a, ()):
        if d == b:
            return True
        if d not in seen:
            seen.add(d)
            if reaches(deps, d, b, seen):
                return True
    return False


def compute_layers(ids, deps, warns):
    state, lay = {}, {}

    def visit(w):
        if w in lay:
            return lay[w]
        state[w] = 1
        best = 0
        for d in deps.get(w, ()):
            if state.get(d) == 1:
                warns.append({"code": "DEP_CYCLE", "cluster": "", "msg": "dependency cycle %s -> %s ignored" % (w, d)})
                continue
            best = max(best, visit(d) + 1)
        state[w] = 2
        lay[w] = best
        return best

    for w in ids:
        visit(w)
    return lay


def build_plan(m, p):
    params = {k: p[k] for k in DEFAULTS}
    warns = list(m.get("input_warnings", []))
    wus = m["wus"]
    byid = {w["id"]: w for w in wus}
    ids = sorted(byid, key=nat_key)
    finfo = m["files"]

    def writes(w):
        return not (w["readonly"] or w["kind"] == "verify")

    fileset = {}
    for w in wus:
        s = set(w["hits"].keys())
        s.update(w["planned"])
        fileset[w["id"]] = s
    wset = {w["id"]: (fileset[w["id"]] if writes(w) else set()) for w in wus}

    deps = {}
    for w in wus:
        ds = []
        for d in w["depends_on"]:
            if d not in byid:
                warns.append({"code": "INPUT", "cluster": "", "msg": "%s depends_on unknown id %s (ignored)" % (w["id"], d)})
            elif d != w["id"] and d not in ds:
                ds.append(d)
        deps[w["id"]] = ds
    declared = {k: list(v) for k, v in deps.items()}

    holders = collections.defaultdict(set)
    for wid, s in fileset.items():
        for f in s:
            holders[f].add(wid)
    interfaces = []
    for f in sorted(holders):
        hs = holders[f]
        builders = sorted((w for w in hs if byid[w]["kind"] == "build"), key=nat_key)
        if len(hs) >= 2 and builders:
            interfaces.append({"path": f, "wus": sorted(hs, key=nat_key), "builders": builders})
    inferred = []

    def add_edge(consumer, builder, why):
        if builder in deps[consumer] or consumer == builder:
            return
        if reaches(deps, builder, consumer):
            warns.append({"code": "INTERFACE_NOT_FIRST", "cluster": "",
                          "msg": clip("%s shares %s with %s but %s depends on %s; ordering left to you" % (consumer, why, builder, builder, consumer))})
            return
        deps[consumer].append(builder)
        inferred.append([consumer, builder])

    for itf in interfaces:
        for c in itf["wus"]:
            if byid[c]["kind"] != "build":
                for b in itf["builders"]:
                    add_edge(c, b, itf["path"])
    for w in ids:
        if byid[w]["kind"] == "verify" and not declared[w]:
            for o in ids:
                if byid[o]["kind"] != "verify" and o != w:
                    add_edge(w, o, "(verify runs last)")
    lay = compute_layers(ids, deps, warns)

    parent = {w: w for w in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in ids:
        for b in ids:
            if a < b and lay[a] == lay[b] and wset[a] & wset[b]:
                parent[find(b)] = find(a)
    groups = collections.defaultdict(list)
    for w in ids:
        groups[(lay[w], find(w))].append(w)
    ordered = sorted(groups.values(), key=lambda g: (lay[g[0]], nat_key(g[0])))
    nlayers = (max(lay.values()) + 1) if lay else 0
    layers = [[] for _ in range(nlayers)]
    clusters = []
    for i, g in enumerate(ordered, 1):
        g = sorted(g, key=nat_key)
        cid = "C%d" % i
        layers[lay[g[0]]].append(cid)
        paths = set()
        for w in g:
            paths |= fileset[w]
        wpaths = set()
        for w in g:
            wpaths |= wset[w]
        ents = []
        for f in paths:
            info = finfo.get(f)
            e = {"path": f, "hits": 0, "hit_lines": [], "lines": 0, "tokens_est": 0, "w": f in wpaths}
            if info is None:
                e["planned"] = True
            else:
                e.update({"lines": info["lines"], "bytes": info["bytes"], "tokens_est": info["tokens_est"],
                          "tokens_narrow": info["tokens_narrow"], "lang": info["lang"]})
                if info.get("importers_n"):
                    e["importers_n"] = info["importers_n"]
                    e["importers"] = info["importers"]
                if "community" in info:
                    e["community"] = info["community"]
            hl, comment, wl = set(), 0, []
            for w in g:
                h = byid[w]["hits"].get(f)
                if h:
                    e["hits"] += h["hits"]
                    comment += h["comment_hits"]
                    hl.update(h["hit_lines"])
                    wl.append(w)
            e["hit_lines"] = sorted(hl)[:5]
            e["wus"] = wl or [w for w in g if f in fileset[w]]
            if e["hits"] and info is not None:
                if info["lang"] == "docs":
                    e["suspect"] = "docs"
                elif comment == e["hits"]:
                    e["suspect"] = "comments"
            ents.append(e)
        ents.sort(key=lambda e: (-e["hits"], e["path"]))
        tests = set()
        for f in paths:
            info = finfo.get(f)
            if info:
                tests.update(info.get("tests", []))
        tests = sorted(tests)
        est, hops = estimate(ents, params, g, {w: byid[w]["kind"] for w in g}, {w: byid[w].get("size") for w in g})
        titles = [byid[w]["title"] for w in g]
        clusters.append({
            "id": cid, "title": titles[0] if len(g) == 1 else "%s (+%d)" % (titles[0], len(g) - 1),
            "wus": g, "commit_tag": "[%s]" % g[0], "layer": lay[g[0]], "files": ents, "tests": tests,
            "est_tokens": est, "est_hops": hops, "siblings": [],
            "readonly": not any(writes(byid[w]) for w in g),
            "verify": all(byid[w]["kind"] == "verify" for w in g),
        })
    for c in clusters:
        c["siblings"] = [o for o in layers[c["layer"]] if o != c["id"]]

    overlap, counts = {}, {}
    for a in ids:
        for b in ids:
            if a < b and not (byid[a]["readonly"] and byid[b]["readonly"]):
                sh = sorted(fileset[a] & fileset[b])
                if sh:
                    overlap.setdefault(a, {})[b] = sh[:20]
                    counts.setdefault(a, {})[b] = len(sh)
    coup = {}
    if m.get("coupling"):
        owner = collections.defaultdict(set)
        for wid, s in fileset.items():
            for f in s:
                owner[f].add(wid)
        for a, b, wt in m["coupling"]:
            for x in owner.get(a, ()):
                for y in owner.get(b, ()):
                    if x != y:
                        k1, k2 = sorted((x, y), key=nat_key)
                        coup.setdefault(k1, {})[k2] = round(coup.get(k1, {}).get(k2, 0) + wt, 1)
    allp = set()
    for wid, s in fileset.items():
        if byid[wid]["kind"] != "verify":
            allp |= s
    alle = []
    for f in sorted(allp):
        e = {"path": f, "planned": f not in finfo, "wus": [w for w in ids if f in fileset[w]],
             "hits": sum(byid[w]["hits"].get(f, {}).get("hits", 0) for w in ids)}
        alle.append(e)
    sest, shops = estimate(alle, params, ids, {w: byid[w]["kind"] for w in ids}, {w: byid[w].get("size") for w in ids})
    plan = {
        "version": VERSION, "base_sha": m.get("head", ""), "budget": params["budget"], "params": params,
        "layers": layers, "clusters": clusters, "overlap": overlap, "overlap_counts": counts,
        "interfaces": interfaces, "inferred_deps": inferred, "coupling": coup,
        "single_agent_est": {"tokens": sest, "hops": shops, "files": len(allp)},
        "graph": m.get("graph", {"used": False}),
        "wus": {w["id"]: {"title": w["title"], "kind": w["kind"], "size": w.get("size", "medium"),
                          "readonly": w["readonly"],
                          "depends_on": declared[w["id"]], "planned": w["planned"],
                          "excluded_hits": w["excluded"]["hits"],
                          "files": sorted(fileset[w["id"]])} for w in wus},
        "input_warnings": warns, "warnings": [],
    }
    plan["warnings"] = compute_warnings(plan)
    return plan


# ---------------------------------------------------------------- check


def normalize(plan):
    """Re-derive layer/siblings/estimates from a (possibly hand-edited) plan."""
    params = dict(DEFAULTS)
    params.update(plan.get("params") or {})
    plan["params"] = params
    plan["budget"] = plan.get("budget") or params["budget"]
    clusters = plan.get("clusters") or []
    layers = plan.get("layers")
    if not layers:
        by = collections.defaultdict(list)
        for c in clusters:
            by[c.get("layer", 0)].append(c["id"])
        layers = [by[k] for k in sorted(by)]
        plan["layers"] = layers
    layer_of = {cid: i for i, l in enumerate(layers) for cid in l}
    for c in clusters:
        c["layer"] = layer_of.get(c["id"], c.get("layer", 0))
        c.setdefault("wus", [])
        c.setdefault("files", [])
        c.setdefault("tests", [])
        c["commit_tag"] = c.get("commit_tag") or ("[%s]" % sorted(c["wus"], key=nat_key)[0] if c["wus"] else "")
        c["siblings"] = [o for o in (layers[c["layer"]] if c["layer"] < len(layers) else []) if o != c["id"]]
        ver = c.get("verify")
        if plan.get("wus") and c["wus"]:
            ver = all((plan["wus"].get(w) or {}).get("kind") == "verify" for w in c["wus"])
        c["verify"] = bool(ver)
        kinds = {w: (plan.get("wus") or {}).get(w, {}).get("kind") or ("verify" if c["verify"] else "apply")
                 for w in c["wus"]}
        sizes = {w: (plan.get("wus") or {}).get(w, {}).get("size") for w in c["wus"]}
        c["est_tokens"], c["est_hops"] = estimate(c["files"], params, c["wus"], kinds, sizes)
    return plan


def compute_warnings(plan):
    normalize(plan)
    P = plan["params"]
    budget = plan["budget"]
    clusters = plan["clusters"]
    byc = {c["id"]: c for c in clusters}
    wus = plan.get("wus") or {}
    out = [dict(w) for w in plan.get("input_warnings", [])]

    def warn(code, cid, msg):
        out.append({"code": code, "cluster": cid, "msg": clip(msg)})

    def wpaths(c):
        return set(f["path"] for f in c["files"] if f.get("w", True))

    for c in clusters:
        cid = c["id"]
        if c["est_tokens"] > budget:
            warn("OVERSIZE", cid, "%s est %s tok > budget %s (%d files, %d hops); split it" % (
                cid, fmt_k(c["est_tokens"]), fmt_k(budget), len(c["files"]), c["est_hops"]))
        for f in c["files"]:
            if f.get("tokens_est", 0) > budget:
                warn("MONOLITH", cid, "%s: %s is ~%s tok (> budget); read by hit ranges only" % (
                    cid, f["path"], fmt_k(f["tokens_est"])))
        if not c.get("readonly") and not c["tests"] and not c.get("verify"):
            code_files = [f for f in c["files"] if f.get("w", True) and not f.get("planned")
                          and f.get("lang") not in NON_CODE and f.get("lang") not in (None, "other")]
            if code_files:
                warn("NO_TESTS", cid, "%s: no tests mapped for %s" % (cid, ", ".join(f["path"] for f in code_files[:3])))
        fan = sorted((f for f in c["files"] if f.get("w", True) and f.get("importers_n", 0) >= P["fanout"]),
                     key=lambda f: -f["importers_n"])
        if fan:
            warn("HIGH_FANOUT", cid, "%s: %s" % (cid, ", ".join("%s (%d importers)" % (f["path"], f["importers_n"]) for f in fan[:3])))
        sus = [f for f in c["files"] if f.get("suspect")]
        if sus:
            warn("SUSPECT_HITS", cid, "%s: hits only in docs/comments: %s%s" % (
                cid, ", ".join("%s (%s)" % (f["path"], f["suspect"]) for f in sus[:3]), " +%d more" % (len(sus) - 3) if len(sus) > 3 else ""))
        exc = sum((wus.get(w) or {}).get("excluded_hits", 0) for w in c["wus"])
        inc = sum(f.get("hits", 0) for f in c["files"])
        if exc and exc > inc:
            warn("SUSPECT_HITS", cid, "%s: seeds mostly hit vendored/dist/lock files (%d ignored vs %d kept); seeds too generic?" % (cid, exc, inc))
        if len(clusters) > 1 and not c.get("readonly") and c["est_tokens"] < P["low_util"] * budget:
            warn("LOW_UTIL", cid, "%s est %s tok is only %d%% of budget; consider merging" % (
                cid, fmt_k(c["est_tokens"]), int(100.0 * c["est_tokens"] / budget)))
        for wid in c["wus"]:
            w = wus.get(wid) or {}
            if w.get("planned") and w.get("kind") != "build":
                warn("PLANNED_PATH_MISSING", cid, "%s: %s seeds path(s) that do not exist: %s" % (
                    cid, wid, ", ".join(w["planned"][:3])))

    flagged = set()
    for layer in plan["layers"]:
        for i, a in enumerate(layer):
            for b in layer[i + 1:]:
                ca, cb = byc.get(a), byc.get(b)
                if not ca or not cb:
                    continue
                sh = sorted(wpaths(ca) & wpaths(cb))
                if sh:
                    flagged.add((a, b))
                    warn("PARALLEL_OVERLAP", a, "%s and %s run in parallel (L%d) but both write: %s%s" % (
                        a, b, ca["layer"], ", ".join(sh[:3]), " +%d" % (len(sh) - 3) if len(sh) > 3 else ""))
                ts = sorted(set(ca["tests"]) & set(cb["tests"]))
                if ts:
                    warn("TEST_SHARED", a, "%s and %s (parallel) share tests: %s%s" % (
                        a, b, ", ".join(ts[:3]), " +%d" % (len(ts) - 3) if len(ts) > 3 else ""))

    itf_flagged = set()
    for itf in plan.get("interfaces") or []:
        holders = [c for c in clusters if any(f["path"] == itf["path"] for f in c["files"])]
        bcl = [c for c in clusters if set(c["wus"]) & set(itf["builders"])]
        if not bcl:
            continue
        bl = min(c["layer"] for c in bcl)
        for c in holders:
            if c in bcl:
                continue
            pair = tuple(sorted((c["id"], bcl[0]["id"])))
            if c["layer"] <= bl and not (c["layer"] == bl and pair in flagged):
                itf_flagged.add((c["id"], bcl[0]["id"]))
                warn("INTERFACE_NOT_FIRST", c["id"], "%s uses interface %s built in %s (L%d) but runs at L%d" % (
                    c["id"], itf["path"], bcl[0]["id"], bl, c["layer"]))
    if wus:
        file_cl = collections.defaultdict(list)
        for c in clusters:
            for f in c["files"]:
                file_cl[f["path"]].append(c["id"])
        agg = collections.defaultdict(list)
        for c in clusters:
            if not any((wus.get(w) or {}).get("kind") == "build" for w in c["wus"]):
                continue
            for f in c["files"]:
                if not f.get("w", True):
                    continue
                for imp in f.get("importers", []):
                    for qid in file_cl.get(imp, ()):
                        q = byc[qid]
                        if qid != c["id"] and (qid, c["id"]) not in itf_flagged and q["layer"] <= c["layer"] and not (q["layer"] == c["layer"] and (min(qid, c["id"]), max(qid, c["id"])) in flagged):
                            agg[(qid, c["id"])].append(f["path"])
        for (qid, cid), fl in sorted(agg.items()):
            warn("INTERFACE_NOT_FIRST", qid, "%s imports %s built in %s (L%d) but runs at L%d" % (
                qid, ", ".join(sorted(set(fl))[:2]), cid, byc[cid]["layer"], byc[qid]["layer"]))

    g = plan.get("graph") or {}
    if g.get("used") and g.get("stale"):
        warn("GRAPH_STALE", "", "graphify graph built at %s, HEAD differs; community hints may be stale" % (
            (g.get("built_at_commit") or "unknown")[:8]))
    s = plan.get("single_agent_est") or {}
    if s and s.get("tokens", 1 << 60) <= budget and s.get("files", 0) <= P["single_max_files"]:
        warn("SINGLE_AGENT_OK", "", "whole job est %s tok / %d hops, %d files: one executer is enough" % (
            fmt_k(s["tokens"]), s.get("hops", 0), s.get("files", 0)))
    return out


def check_after(plan, base, root):
    out = []
    rc, txt = run_git(["log", "--name-only", "--format=%x1e%H%x1f%s", "%s..HEAD" % base], root)
    if rc != 0:
        return [{"code": "INPUT", "cluster": "", "msg": "cannot walk %s..HEAD (bad base?)" % base[:12]}], 0, 0
    clusters = plan["clusters"]
    tag_cluster = {}
    for c in clusters:
        tag_cluster[c["commit_tag"].strip("[]")] = c["id"]
    for c in clusters:
        for w in c["wus"]:
            tag_cluster[w] = c["id"]
    byc = {c["id"]: c for c in clusters}
    planned = set()
    for c in clusters:
        planned.update(f["path"] for f in c["files"])
        planned.update(c["tests"])
    touched = collections.defaultdict(lambda: collections.defaultdict(list))
    untagged, unplanned = [], collections.defaultdict(set)
    ncommits = ntagged = 0
    for rec in txt.split("\x1e")[1:]:
        head, _, rest = rec.partition("\n")
        sha, _, subj = head.partition("\x1f")
        files = [l.strip() for l in rest.split("\n") if l.strip()]
        ncommits += 1
        m = re.match(r"^\[([^\]\s]+)\]", subj)
        tag = m.group(1) if m else None
        if not tag:
            untagged.append("%s %s" % (sha[:7], subj[:50]))
        elif tag not in tag_cluster:
            untagged.append("%s %s (tag [%s] not in plan)" % (sha[:7], subj[:40], tag))
        else:
            ntagged += 1
        for f in files:
            f = posix(f)
            if classify(f) == "meta":
                continue
            if f not in planned:
                unplanned[tag or "-"].add(f)
            if tag:
                touched[f][tag].append(sha[:7])
    if untagged:
        out.append({"code": "UNTAGGED", "cluster": "", "msg": clip("%d commit(s) without a plan tag: %s%s" % (
            len(untagged), "; ".join(untagged[:3]), " +%d" % (len(untagged) - 3) if len(untagged) > 3 else ""))})
    for tag, fs in sorted(unplanned.items()):
        fs = sorted(fs)
        out.append({"code": "UNPLANNED", "cluster": tag_cluster.get(tag, ""), "msg": clip("[%s] touched %d file(s) outside every cluster: %s%s" % (
            tag, len(fs), ", ".join(fs[:3]), " +%d" % (len(fs) - 3) if len(fs) > 3 else ""))})
    pairs = collections.defaultdict(list)
    for f, tags in sorted(touched.items()):
        ts = sorted(tags)
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                ca, cb = tag_cluster.get(a), tag_cluster.get(b)
                if ca and cb and ca != cb and byc[ca]["layer"] == byc[cb]["layer"]:
                    pairs[(a, b)].append(f)
    for (a, b), fs in sorted(pairs.items()):
        out.append({"code": "COLLISION", "cluster": tag_cluster[a], "msg": clip("[%s] and [%s] (same layer) both touched: %s%s" % (
            a, b, ", ".join(fs[:3]), " +%d" % (len(fs) - 3) if len(fs) > 3 else ""))})
    return out, ncommits, ntagged


# ---------------------------------------------------------------- output


def render(plan, warnings):
    lines = []
    wn = len(plan.get("wus") or {})
    s = plan.get("single_agent_est") or {}
    lines.append("base %s budget %s | %d WUs -> %d clusters in %d layers | single-agent ~%s tok / %s hops" % (
        (plan.get("base_sha") or "none")[:8], fmt_k(plan["budget"]), wn, len(plan["clusters"]),
        len(plan["layers"]), fmt_k(s.get("tokens", 0)), s.get("hops", "?")))
    for c in plan["clusters"]:
        lines.append("L%d %s %s %s  %df est %s/%dh tests:%d%s%s  %s" % (
            c["layer"], c["id"], c["commit_tag"], ",".join(c["wus"]), len(c["files"]),
            fmt_k(c["est_tokens"]), c["est_hops"], len(c["tests"]),
            "  sib:" + ",".join(c["siblings"]) if c["siblings"] else "",
            "  (read-only)" if c.get("readonly") else "", clip(c.get("title", ""), 40)))
    for w in warnings:
        lines.append("WARN %s %s" % (w["code"], w["msg"]))
    return "\n".join(lines)


def cmd_show(a):
    plan = normalize(read_object(a.plan, "plan"))
    key = a.cluster.lower()
    c = next((c for c in plan["clusters"] if c["id"].lower() == key), None)
    if c is None:
        c = next((c for c in plan["clusters"] if a.cluster in c["wus"]), None)
    if c is None:
        die("no cluster %s in %s (have: %s)" % (a.cluster, a.plan, ", ".join(x["id"] for x in plan["clusters"])))
    tag = c["commit_tag"]
    print("%s %s layer %d | WUs %s | est %s tok / %d hops%s" % (
        c["id"], tag, c["layer"], ",".join(c["wus"]), fmt_k(c["est_tokens"]), c["est_hops"],
        " | read-only" if c.get("readonly") else ""))
    print("title: %s" % c.get("title", ""))
    byc = {x["id"]: x for x in plan["clusters"]}
    if c["siblings"]:
        print("siblings (parallel, do not edit their files; note needs in your handback): " + ", ".join(
            "%s(%s)" % (s, byc[s].get("title", "")[:24]) if s in byc else s for s in c["siblings"]))
    print("files (path  hits  lines  @hit-lines):")
    limit = 40
    for f in c["files"][:limit]:
        if f.get("planned"):
            print("  %s  (new)" % f["path"])
        else:
            print("  %s  hits=%d lines=%d%s%s" % (
                f["path"], f.get("hits", 0), f.get("lines", 0),
                " @" + ",".join(str(x) for x in f.get("hit_lines", [])) if f.get("hit_lines") else "",
                "  [read-only]" if not f.get("w", True) else ""))
    if len(c["files"]) > limit:
        print("  ... +%d more in the plan" % (len(c["files"]) - limit))
    print("tests: " + (" ".join(c["tests"][:12]) if c["tests"] else "none mapped") + (" ..." if len(c["tests"]) > 12 else ""))
    print("commit: git add <your paths> && git commit -m \"%s <what>\"" % tag)


def dump_warnings_only(ws):
    return "\n".join("WARN %s %s" % (w["code"], w["msg"]) for w in ws)


# ---------------------------------------------------------------- commands


def add_map_args(p):
    p.add_argument("--wu", required=True, help="wus.json")
    p.add_argument("--repo", default=".", help="repository directory (default .)")
    p.add_argument("--bpt", type=int, default=4, help="bytes per token for estimates (default 4)")
    p.add_argument("--max-importer-files", type=int, default=150, dest="max_importer_files")


def add_plan_args(p):
    p.add_argument("--budget", type=int, default=DEFAULTS["budget"], help="per-agent token budget (sizing input, default 100000)")
    p.add_argument("--overhead", type=int, default=DEFAULTS["overhead"],
                   help="base context per agent in tokens (default 20000); est_tokens = overhead + est_hops * hop_tokens")
    p.add_argument("--hop-tokens", type=int, default=DEFAULTS["hop_tokens"], dest="hop_tokens",
                   help="context growth per tool call (default 2500; measured range 1700-3800)")
    p.add_argument("--hops-base", type=float, default=DEFAULTS["hops_base"], dest="hops_base",
                   help="fixed tool calls per cluster (default 6)")
    p.add_argument("--hops-new-small", type=float, default=DEFAULTS["hops_new_small"], dest="hops_new_small",
                   help="build WU with \"size\": \"small\": tool calls per planned new file (default 8)")
    p.add_argument("--hops-new", type=float, default=DEFAULTS["hops_new"], dest="hops_new",
                   help="build WU, \"size\": \"medium\" or unset: tool calls per planned new file (default 20)")
    p.add_argument("--hops-new-large", type=float, default=DEFAULTS["hops_new_large"], dest="hops_new_large",
                   help="build WU with \"size\": \"large\": tool calls per planned new file (default 30)")
    p.add_argument("--hops-edit", type=float, default=DEFAULTS["hops_edit"], dest="hops_edit",
                   help="build WU: tool calls per existing file edited (default 3)")
    p.add_argument("--hops-per-file", type=float, default=DEFAULTS["hops_per_file"], dest="hops_per_file",
                   help="apply/wire WU: tool calls per file, plus hits/--hits-per-hop (default 3)")
    p.add_argument("--hits-per-hop", type=float, default=DEFAULTS["hits_per_hop"], dest="hits_per_hop",
                   help="apply/wire WU: seed hits handled per extra tool call (default 4)")
    p.add_argument("--hops-verify", type=float, default=DEFAULTS["hops_verify"], dest="hops_verify",
                   help="verify WU: tool calls per verify WU (default 15)")
    p.add_argument("--fanout", type=int, default=DEFAULTS["fanout"], help="importer count that triggers HIGH_FANOUT")
    p.add_argument("--low-util", type=float, default=DEFAULTS["low_util"], dest="low_util",
                   help="LOW_UTIL below this fraction of budget")
    p.add_argument("--single-max-files", type=int, default=DEFAULTS["single_max_files"], dest="single_max_files")


def plan_params(a):
    return {k: getattr(a, k) for k in DEFAULTS}


def make_repo(a):
    root, isgit = find_root(a.repo)
    skip = []
    try:
        rel = posix(os.path.relpath(os.path.abspath(a.wu), root))
        if not rel.startswith(".."):
            skip.append(rel)
    except ValueError:
        pass
    return root, isgit, Repo(root, isgit, skip)


def cmd_map(a):
    wus = load_wus(a.wu)
    root, isgit, repo = make_repo(a)
    m = build_map(wus, repo, a)
    write_json(a.out, m)
    nplanned = sum(len(w["planned"]) for w in m["wus"])
    print("map: %d WUs, %d files, %d planned paths, git=%s, graph=%s -> %s" % (
        len(wus), len(m["files"]), nplanned, "yes" if isgit else "no",
        "stale" if m["graph"].get("stale") else "yes" if m["graph"]["used"] else "no", a.out))
    for w in m["input_warnings"]:
        print("WARN %s %s" % (w["code"], w["msg"]))


def cmd_plan(a):
    m = read_object(a.map, "map")
    plan = build_plan(m, plan_params(a))
    write_json(a.out, plan)
    print(render(plan, plan["warnings"]))
    print("-> %s" % a.out)


def cmd_check(a):
    plan = read_object(a.plan, "plan")
    ws = compute_warnings(plan)
    if a.write:
        plan["warnings"] = ws
        write_json(a.plan, plan)
    if not a.after:
        print(render(plan, ws))
    else:
        root, isgit = find_root(".")
        base = a.base or plan.get("base_sha") or ""
        if not isgit:
            print("after: not a git repository, skipped")
        elif not base:
            print("after: no --base and plan has no base_sha, skipped")
        else:
            aw, n, t = check_after(plan, base, root)
            print("after: %d commit(s) since %s, %d tagged" % (n, base[:8], t))
            print(dump_warnings_only(aw) if aw else "after: no collisions, unplanned files or untagged commits")


def cmd_auto(a):
    wus = load_wus(a.wu)
    root, isgit, repo = make_repo(a)
    out_dir = a.out_dir or os.path.join(root, ".delegate-kit", "partition")
    m = build_map(wus, repo, a)
    plan = build_plan(m, plan_params(a))
    mp, pp = os.path.join(out_dir, "partition.map.json"), os.path.join(out_dir, "partition.json")
    write_json(mp, m)
    write_json(pp, plan)
    print(render(plan, plan["warnings"]))
    print("-> %s (+ partition.map.json)" % posix(pp))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="partition.py", description="Advisory work partitioner for subagent planning.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("map", help="grep seeds, collect per-file numbers, importers, tests")
    add_map_args(p)
    p.add_argument("--out", default="partition.map.json")
    p.set_defaults(fn=cmd_map)
    p = sub.add_parser("plan", help="layer, cluster by write-set overlap, estimate")
    p.add_argument("--map", required=True)
    p.add_argument("--out", default="partition.json")
    add_plan_args(p)
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("check", help="warnings for a plan (hand-edits OK); --after audits commits")
    p.add_argument("--plan", required=True)
    p.add_argument("--after", action="store_true", help="audit commits in base..HEAD by [Wn] tag")
    p.add_argument("--base", default="", help="base sha for --after (default: plan base_sha)")
    p.add_argument("--write", action="store_true", help="store recomputed estimates/warnings back into the plan")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("show", help="compact view of one cluster (what a subagent reads)")
    p.add_argument("cluster")
    p.add_argument("--plan", required=True)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("auto", help="map + plan + check; writes partition.map.json and partition.json")
    add_map_args(p)
    add_plan_args(p)
    p.add_argument("--out-dir", default="", dest="out_dir", help="default: <repo>/.delegate-kit/partition")
    p.set_defaults(fn=cmd_auto)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        a.fn(a)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
