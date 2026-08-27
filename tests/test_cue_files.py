"""Cue file naming, on a case-insensitive filesystem.

Windows rewrites "Calm.json" into an existing "calm.json" without renaming it.
A project that recorded the capitalised spelling then referenced a name the
listing never returned, and the cue list vanished from both the cue dropdown
and the Rapid Fire grid while its file sat on disk. These tests pin the two
halves of the fix: the server always reports the spelling it stored under, and
the front matches the project's names case-insensitively.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_NODE = shutil.which("node")
requires_node = pytest.mark.skipif(_NODE is None, reason="node not available on PATH")


def _read(rel: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def cue_dir(monkeypatch, tmp_path):
    """Point the app at a throwaway cue/ directory."""
    import app

    d = tmp_path / "cue"
    d.mkdir()
    monkeypatch.setattr(app, "CUE_DIR", str(d))
    return d


def _write_cue(cue_dir, name, sequence=("a",)):
    path = os.path.join(str(cue_dir), name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"sequence": list(sequence)}, fh)
    return path


# -----------------------------------------------------------------------------
# Server side
# -----------------------------------------------------------------------------

def test_resolve_adopts_the_spelling_on_disk(cue_dir):
    import app

    _write_cue(cue_dir, "calm.json")
    assert app.resolve_cue_filename("Calm.json") == "calm.json"
    assert app.resolve_cue_filename("CALM.JSON") == "calm.json"
    assert app.resolve_cue_filename("calm.json") == "calm.json"


def test_resolve_keeps_an_unknown_name_and_forces_the_extension(cue_dir):
    import app

    assert app.resolve_cue_filename("Brand New.json") == "Brand New.json"
    assert app.resolve_cue_filename("noext") == "noext.json"
    # A path is reduced to its basename; the route also refuses "..".
    assert app.resolve_cue_filename("sub/dir/x.json") == "x.json"
    with pytest.raises(ValueError):
        app.resolve_cue_filename("   ")


def test_saving_a_differently_cased_name_updates_the_same_file(cue_dir):
    import app

    _write_cue(cue_dir, "calm.json", sequence=["old"])
    stored = app.save_cue_file("Calm.json", {"sequence": ["new"]})

    assert stored == "calm.json", "the caller must learn the real name"
    assert os.listdir(str(cue_dir)) == ["calm.json"], "no second file may appear"
    with open(os.path.join(str(cue_dir), "calm.json"), encoding="utf-8") as fh:
        assert json.load(fh)["sequence"] == ["new"]


def test_a_genuinely_new_name_is_created_as_typed(cue_dir):
    import app

    stored = app.save_cue_file("Warriors.json", {"sequence": []})
    assert stored == "Warriors.json"
    assert os.listdir(str(cue_dir)) == ["Warriors.json"]


def test_load_finds_a_differently_cased_name(cue_dir):
    import app

    _write_cue(cue_dir, "calm.json", sequence=["x"])
    assert app.load_cue_file("Calm.json")["sequence"] == ["x"]
    with pytest.raises(FileNotFoundError):
        app.load_cue_file("Nope.json")


def test_post_hands_back_the_stored_filename(cue_dir):
    import app

    _write_cue(cue_dir, "calm.json")
    client = app.app.test_client()
    res = client.post("/api/cues/Calm.json", json={"sequence": []})

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["filename"] == "calm.json"
    # And the listing the front intersects against agrees.
    listing = client.get("/api/cue_files").get_json()["files"]
    assert listing == ["calm.json"]


# -----------------------------------------------------------------------------
# Front side: the same names, run for real in node
# -----------------------------------------------------------------------------

def _cues_source() -> str:
    """resolveProjectCueFiles + normalizeCueFilename + makeUniqueCueName."""
    src = _read("static/cues.js")
    start = src.index("async function resolveProjectCueFiles()")
    end = src.index("async function loadCueFile(")
    block = src[start:end]
    assert "function makeUniqueCueName" in block
    assert "function normalizeCueFilename" in block
    return block


_HARNESS = """
// Browser globals the lifted code expects.
var window = { projectCueFiles: [] };
var DISK = [];
function $id() { return null; }
global.fetch = async function () {
  return { json: async () => ({ files: DISK.slice() }) };
};
%(source)s

const out = [];
function check(name, cond, detail) {
  out.push({ name, ok: !!cond, detail: detail === undefined ? null : detail });
}

(async () => {
  // 1. the project's capitalised name resolves to the file on disk
  DISK = ["calm.json", "other.json"];
  window.projectCueFiles = ["Calm.json"];
  let files = await resolveProjectCueFiles();
  check("case-insensitive match", files.length === 1 && files[0] === "calm.json", JSON.stringify(files));
  check("project scope canonicalised",
        JSON.stringify(window.projectCueFiles) === JSON.stringify(["calm.json"]),
        JSON.stringify(window.projectCueFiles));

  // 2. a loose file on disk stays out of the project's scope
  DISK = ["calm.json", "loose.json"];
  window.projectCueFiles = ["Calm.json"];
  files = await resolveProjectCueFiles();
  check("loose file stays hidden", files.length === 1 && files[0] === "calm.json", JSON.stringify(files));

  // 3. a name the project claims but the disk lost is hidden, NOT dropped
  DISK = ["calm.json"];
  window.projectCueFiles = ["Calm.json", "Gone.json"];
  files = await resolveProjectCueFiles();
  check("missing file hidden", JSON.stringify(files) === JSON.stringify(["calm.json"]), JSON.stringify(files));
  check("missing file kept in scope",
        window.projectCueFiles.length === 2 && window.projectCueFiles.includes("Gone.json"),
        JSON.stringify(window.projectCueFiles));

  // 4. no project, no lists
  window.projectCueFiles = [];
  files = await resolveProjectCueFiles();
  check("empty scope yields nothing", files.length === 0, JSON.stringify(files));

  // 5. uniqueness is checked against the DISK, case-insensitively: this is what
  //    stopped "Save as Calm" from silently rewriting an existing calm.json
  DISK = ["calm.json"];
  check("clash with a differently cased file", (await makeUniqueCueName("Calm")) === "Calm-2.json");
  check("clash on the same spelling", (await makeUniqueCueName("calm.json")) === "calm-2.json");
  DISK = ["calm.json", "calm-2.json"];
  check("walks past taken suffixes", (await makeUniqueCueName("Calm")) === "Calm-3.json");
  DISK = ["other.json"];
  check("free name kept as typed", (await makeUniqueCueName("Calm")) === "Calm.json");
  check("extension added", (await makeUniqueCueName("Fresh")) === "Fresh.json");

  console.log(JSON.stringify(out));
})();
"""


def _run_node(source: str):
    script = _HARNESS % {"source": source}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "check.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.run([_NODE, path], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@requires_node
def test_front_matches_cue_names_case_insensitively():
    results = _run_node(_cues_source())
    assert results, "the node harness produced no results"
    failed = [r for r in results if not r["ok"]]
    assert not failed, "\n".join(f"{r['name']}: {r['detail']}" for r in failed)


def test_rapid_fire_reuses_the_one_resolver():
    """Two implementations of the scope drifted apart once; not again."""
    src = _read("static/rapidfire.js")
    start = src.index("async function listProjectCueFiles()")
    body = src[start:src.index("async function loadMeta(")]
    assert "window.resolveProjectCueFiles" in body
    assert "new Set(" not in body, "Rapid Fire must not rebuild its own matching"
    assert "/api/cue_files" not in body, "the resolver owns that fetch"


def test_get_reports_the_real_filename_in_a_header(cue_dir):
    """The dropdown selects by exact value, so the loader needs the real name."""
    import app

    _write_cue(cue_dir, "calm.json", sequence=["x"])
    res = app.app.test_client().get("/api/cues/Calm.json")

    assert res.status_code == 200
    assert res.headers["X-Cue-Filename"] == "calm.json"
    assert res.get_json()["sequence"] == ["x"]


def test_the_loader_adopts_that_header():
    """Regression guard: loadCueFile must not keep the caller's spelling."""
    src = _read("static/cues.js")
    start = src.index("async function loadCueFile(")
    body = src[start:src.index("async function saveCurrentCueFile(")]
    assert 'r.headers.get("X-Cue-Filename")' in body
    assert "sel.value = filename" in body, "the dropdown must follow the loaded file"


# -----------------------------------------------------------------------------
# Saving a cue list must survive loading the project again
# -----------------------------------------------------------------------------

def _project_sync_source() -> str:
    src = _read("static/project.js")
    start = src.index("async function projectSyncCueList(")
    return src[start:src.index("async function projectLoadByName(")]


@requires_node
def test_saving_a_cue_list_reaches_the_project_copy():
    """A project embeds a copy of each list and writes it back when loaded.

    Without this, every edit was undone the next time the project was opened:
    the file on disk had the new version, the project's copy the old one, and
    the load overwrote the file.
    """
    harness = """
var currentProjectFile = "MyShow.ddmxproj";
const PROJECT = {
  name: "MyShow",
  cue_lists: { "A.json": { sequence: [{ name: "old" }] }, "B.json": { sequence: [] } },
  active_cue_list: "B.json",
};
const calls = [];
global.fetch = async function (url, opts) {
  calls.push({ url: String(url), method: (opts && opts.method) || "GET", body: opts && opts.body });
  if (!opts || opts.method !== "POST") {
    return { ok: true, json: async () => JSON.parse(JSON.stringify(PROJECT)) };
  }
  return { ok: true, status: 200, json: async () => ({ ok: true }) };
};
%(source)s

const out = [];
function check(name, cond, detail) {
  out.push({ name, ok: !!cond, detail: detail === undefined ? null : detail });
}

(async () => {
  const ok = await projectSyncCueList("A.json", { sequence: [{ name: "new" }] });
  check("it reports success", ok === true);
  const post = calls.find((c) => c.method === "POST");
  check("the project is written", Boolean(post) && post.url.includes("MyShow.ddmxproj"),
        JSON.stringify(calls.map((c) => c.method + " " + c.url)));
  const written = post ? JSON.parse(post.body) : {};
  check("the list is replaced", written.cue_lists["A.json"].sequence[0].name === "new",
        JSON.stringify(written.cue_lists));
  check("the other lists survive", "B.json" in written.cue_lists, JSON.stringify(Object.keys(written.cue_lists)));
  check("the active list is untouched", written.active_cue_list === "B.json", String(written.active_cue_list));

  // No project open: nothing to sync, and no request either.
  currentProjectFile = null;
  calls.length = 0;
  const none = await projectSyncCueList("A.json", { sequence: [] });
  check("no project, no write", none === false && calls.length === 0, JSON.stringify(calls));

  console.log(JSON.stringify(out));
})();
""" % {"source": _project_sync_source()}

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "check.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(harness)
        proc = subprocess.run([_NODE, path], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    failed = [r for r in results if not r["ok"]]
    assert not failed, "\n".join(f"{r['name']}: {r['detail']}" for r in failed)


def test_both_save_paths_go_through_the_project():
    src = _read("static/cues.js")
    for fn in ("async function saveCurrentCueFile", "async function saveCueFileAs"):
        start = src.index(fn)
        body = src[start:start + 1800]
        assert "window.projectSyncCueList" in body, f"{fn} does not update the project"
        assert "if (!res.ok) throw" in body, f"{fn} reports success even on a refused save"


def test_the_properties_panel_never_zeroes_a_missing_field():
    """A stale template must not be able to wipe a cue's timing."""
    src = _read("static/cues.js")
    start = src.index("function applyCueProps()")
    body = src[start:start + 900]
    assert 'if (fadeEl) step.fade' in body
    assert 'if (durationEl) step.duration' in body
