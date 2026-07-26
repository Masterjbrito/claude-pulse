"""Testes do Claude Pulse: parser, precos, agregacao, forecast e export."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import claude_pulse as cp


# ---------- precos e modelos ----------

def test_price_for_known_models():
    assert cp.price_for("claude-opus-4-8") == cp.PRICING["opus"]
    assert cp.price_for("claude-fable-5") == cp.PRICING["fable"]
    assert cp.price_for("claude-sonnet-5") == cp.PRICING["sonnet"]
    assert cp.price_for("claude-haiku-4-5") == cp.PRICING["haiku"]


def test_price_for_unknown_falls_back_to_sonnet():
    assert cp.price_for("claude-mystery-9") == cp.PRICING["sonnet"]


def test_short_model():
    assert cp.short_model("claude-opus-4-8") == "Opus"
    assert cp.short_model("claude-fable-5") == "Fable"


def test_cost_of():
    r = {"model": "claude-opus-4-8", "in": 1_000_000, "out": 0, "cw": 0, "cr": 0}
    assert cp.cost_of(r) == pytest.approx(15.0)
    r = {"model": "claude-opus-4-8", "in": 0, "out": 1_000_000, "cw": 0, "cr": 0}
    assert cp.cost_of(r) == pytest.approx(75.0)


# ---------- parser JSONL ----------

def _entry(ts="2026-07-20T10:00:00.000Z", model="claude-opus-4-8",
           msg_id="m1", req_id="r1", **usage):
    u = {"input_tokens": 10, "output_tokens": 5,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    u.update(usage)
    return json.dumps({"type": "assistant", "timestamp": ts, "requestId": req_id,
                       "message": {"id": msg_id, "model": model, "usage": u}})


@pytest.fixture
def store_with_file(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    proj_dir = projects / "c--Users-alice-Documents-my-app"
    proj_dir.mkdir(parents=True)
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    return cp.UsageStore(), proj_dir


def test_parse_basic(store_with_file):
    store, proj_dir = store_with_file
    (proj_dir / "s1.jsonl").write_text(_entry() + "\n" + _entry(msg_id="m2", req_id="r2"),
                                       encoding="utf-8")
    recs = store.records()
    assert len(recs) == 2
    assert recs[0]["proj"] == "my-app"
    assert recs[0]["in"] == 10


def test_parse_dedupes_same_message(store_with_file):
    store, proj_dir = store_with_file
    (proj_dir / "s1.jsonl").write_text(_entry() + "\n" + _entry(), encoding="utf-8")
    assert len(store.records()) == 1


def test_parse_skips_synthetic_and_garbage(store_with_file):
    store, proj_dir = store_with_file
    lines = [_entry(model="<synthetic>"), "not json {{{", '{"type":"user"}', _entry(msg_id="ok")]
    (proj_dir / "s1.jsonl").write_text("\n".join(lines), encoding="utf-8")
    recs = store.records()
    assert len(recs) == 1


def test_parse_skips_bad_timestamp(store_with_file):
    store, proj_dir = store_with_file
    (proj_dir / "s1.jsonl").write_text(_entry(ts="not-a-date"), encoding="utf-8")
    assert store.records() == []


def test_incremental_cache_reuses_unchanged_files(store_with_file):
    store, proj_dir = store_with_file
    f = proj_dir / "s1.jsonl"
    f.write_text(_entry(), encoding="utf-8")
    assert len(store.records()) == 1
    # mesmo mtime/size -> nao rele; conteudo cacheado mantem-se
    assert len(store.records()) == 1


def test_removed_file_drops_records(store_with_file):
    store, proj_dir = store_with_file
    f = proj_dir / "s1.jsonl"
    f.write_text(_entry(), encoding="utf-8")
    assert len(store.records()) == 1
    f.unlink()
    assert store.records() == []


# ---------- nome de projeto ----------

@pytest.mark.parametrize("folder,expected", [
    ("c--Users-alice-Documents-my-app", "my-app"),
    ("c--Users-bob-Desktop-tool", "tool"),
    ("d--Users-x-Projects-site", "site"),
])
def test_proj_name(tmp_path, monkeypatch, folder, expected):
    projects = tmp_path / "projects"
    (projects / folder).mkdir(parents=True)
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    assert cp.UsageStore._proj_name(projects / folder / "s.jsonl") == expected


# ---------- forecast ----------

def _weekly(pct, days_to_reset=3.0):
    resets = datetime.now(timezone.utc) + timedelta(days=days_to_reset)
    return [{"kind": "weekly_all", "pct": pct, "resets_at": resets.isoformat()}]


def test_forecast_predicts_depletion_when_burning_fast():
    # 4 dias decorridos (reset a 3 dias), 90% usado -> esgota antes do reset
    eta = cp.forecast_depletion(_weekly(90.0))
    assert eta is not None
    assert datetime.fromisoformat(eta) < datetime.now(timezone.utc) + timedelta(days=3)


def test_forecast_none_when_pace_is_safe():
    assert cp.forecast_depletion(_weekly(10.0)) is None


def test_forecast_none_on_missing_or_bad_data():
    assert cp.forecast_depletion(None) is None
    assert cp.forecast_depletion([]) is None
    assert cp.forecast_depletion([{"kind": "weekly_all", "pct": "x", "resets_at": "bad"}]) is None


# ---------- limites (parse da resposta da API) ----------

def test_fetch_limits_parses_limits_array(monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}), encoding="utf-8")
    monkeypatch.setattr(cp, "CREDS_FILE", creds)

    class FakeResp:
        status_code = 200
        def json(self):
            return {"limits": [
                {"kind": "session", "percent": 21, "resets_at": "2026-07-24T19:00:00Z", "scope": None},
                {"kind": "weekly_scoped", "percent": 23, "resets_at": "2026-07-28T22:00:00Z",
                 "scope": {"model": {"display_name": "Fable"}}},
            ]}
    monkeypatch.setattr(cp.requests, "get", lambda *a, **k: FakeResp())
    lims = cp.fetch_limits()
    assert lims[0]["kind"] == "session" and lims[0]["pct"] == 21.0
    assert lims[1]["model"] == "Fable"


def test_fetch_limits_returns_none_on_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cp, "CREDS_FILE", tmp_path / "missing.json")
    assert cp.fetch_limits() is None


# ---------- get_stats + export (integracao) ----------

@pytest.fixture
def api(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    proj = projects / "c--Users-alice-Documents-app-x"
    proj.mkdir(parents=True)
    today = datetime.now().astimezone().strftime("%Y-%m-%dT12:00:00.000Z")
    (proj / "s.jsonl").write_text(
        _entry(ts=today, input_tokens=1_000_000, output_tokens=0) + "\n"
        + _entry(ts=today, msg_id="m2", req_id="r2", model="claude-sonnet-5"),
        encoding="utf-8")
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp, "LIMITS_CACHE", tmp_path / "limits_cache.json")
    monkeypatch.setattr(cp.Api, "_check_update", lambda self: None)
    monkeypatch.setattr(cp, "fetch_limits", lambda: None)
    return cp.Api()


def test_get_stats_aggregates(api):
    s = api.get_stats()
    assert s["today"] >= 15.0            # 1M input opus
    assert {m["name"] for m in s["models"]} == {"Opus", "Sonnet"}
    assert s["projects"][0]["name"] == "app-x"
    assert len(s["days"]) >= 1 and len(s["weeks"]) >= 1
    assert s["version"] == cp.VERSION


def test_export_data(api, tmp_path, monkeypatch):
    monkeypatch.setattr(cp.Path, "home", classmethod(lambda c: tmp_path))
    dest = api.export_data()
    files = list(Path(dest).glob("claude-pulse-export-*"))
    assert any(f.suffix == ".csv" for f in files)
    assert any(f.suffix == ".json" for f in files)
    data = json.loads(next(f for f in files if f.suffix == ".json").read_text(encoding="utf-8"))
    assert data and {"day", "model", "project", "cost"} <= set(data[0])


# ---------- limites cache: formato retroativo ----------

def test_limits_cache_backward_compat(tmp_path, monkeypatch):
    # escreve um ficheiro LIMITS_CACHE em formato antigo (lista JSON)
    cache_file = tmp_path / "limits_cache.json"
    cache_file.write_text(
        json.dumps([{"kind": "weekly_all", "pct": 50, "resets_at": "2026-07-28T22:00:00Z"}]),
        encoding="utf-8")

    projects = tmp_path / "projects"
    projects.mkdir(parents=True)

    monkeypatch.setattr(cp, "LIMITS_CACHE", cache_file)
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp.Api, "_check_update", lambda self: None)

    api = cp.Api()
    assert api._limits is not None
    assert api._limits[0]["kind"] == "weekly_all"
    assert api._limits_at == 0.0


def test_limits_cache_new_format(tmp_path, monkeypatch):
    # escreve um ficheiro LIMITS_CACHE em formato novo (dict com fetched_at e limits)
    cache_file = tmp_path / "limits_cache.json"
    cache_file.write_text(
        json.dumps({"fetched_at": 123.0, "limits": [{"kind": "session", "pct": 5, "resets_at": "2026-07-24T19:00:00Z"}]}),
        encoding="utf-8")

    projects = tmp_path / "projects"
    projects.mkdir(parents=True)

    monkeypatch.setattr(cp, "LIMITS_CACHE", cache_file)
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp.Api, "_check_update", lambda self: None)

    api = cp.Api()
    assert api._limits is not None
    assert api._limits[0]["kind"] == "session"
    assert api._limits_at == 123.0
