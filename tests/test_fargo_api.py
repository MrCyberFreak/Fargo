"""Tests for fargo_api parsing — in particular the empty-Robustness case.

FargoRate returns an empty string for Robustness on accounts with no games
played yet. That must parse as robustness 0 (a preliminary rating), NOT raise
a fetch failure that silently drops the player from every run. These stub the
HTTP layer (_get_json) so they need no network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fargo_api  # noqa: E402
from fargo_api import FargoApiError, _robustness  # noqa: E402


def test_robustness_empty_string_is_zero():
    assert _robustness("") == 0
    assert _robustness("   ") == 0
    assert _robustness(None) == 0


def test_robustness_numeric_passes_through():
    assert _robustness("63") == 63
    assert _robustness(200) == 200
    assert _robustness("0") == 0


def test_robustness_garbage_still_raises():
    with pytest.raises(FargoApiError):
        _robustness("abc")


def _player_payload(**overrides):
    data = {
        "Id": "1310533",
        "BBMMembershipId": "9900007849538",
        "FullName": "Test Player",
        "FargoRating": "438",
        "Robustness": "63",
        "State": "CO",
        "RowId": "some-guid",
    }
    data.update(overrides)
    return data


def test_get_player_empty_robustness_baselines_at_zero(monkeypatch):
    """A new account (Robustness '') is fetched successfully, not failed."""
    monkeypatch.setattr(
        fargo_api, "_get_json", lambda *a, **k: _player_payload(Robustness="")
    )
    rec = fargo_api.get_player(1310533, session=object())
    assert rec.robustness == 0
    assert rec.rating == 438
    assert rec.rating_quality == "preliminary"


def test_get_player_known_answer(monkeypatch):
    """Regression guard on the documented fixture (docs/api.md)."""
    monkeypatch.setattr(fargo_api, "_get_json", lambda *a, **k: _player_payload())
    rec = fargo_api.get_player(1310533, session=object())
    assert rec.player_id == 1310533
    assert rec.rating == 438
    assert rec.robustness == 63
    assert rec.membership_id == "9900007849538"
    assert rec.rating_quality == "preliminary"


def test_get_player_empty_rating_still_fails(monkeypatch):
    """Only Robustness is tolerant; an empty FargoRating is still a real error."""
    monkeypatch.setattr(
        fargo_api, "_get_json", lambda *a, **k: _player_payload(FargoRating="")
    )
    with pytest.raises(FargoApiError):
        fargo_api.get_player(1310533, session=object())
