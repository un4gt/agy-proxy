"""Quota scopes: independent model buckets must not block their siblings."""

from __future__ import annotations

import json
import time

from agy_proxy_core.account import normalize_quota
from agy_proxy_core.account_pool import AccountPool
from agy_proxy_core.config import Config

ACCOUNT_ID = "a" * 32


def test_normalize_quota_keeps_the_group_description():
    groups = normalize_quota({"groups": [{
        "displayName": "Gemini Models",
        "description": "Models within this group: Gemini Flash, Gemini Pro",
        "buckets": [],
    }]})
    assert groups[0]["description"].startswith("Models within this group:")
    assert groups[0]["models"] == "Gemini Flash, Gemini Pro"


def make_pool(tmp_path) -> AccountPool:
    data = {
        "version": 1,
        "accounts": [{"id": ACCOUNT_ID, "name": "A", "credential_key": None,
                      "created_at": 0, "disabled_models": []}],
        "bindings": {},
        "exhausted": {},
        "managed": True,
    }
    (tmp_path / "accounts.json").write_text(json.dumps(data), encoding="utf-8")
    return AccountPool(Config(data_dir=str(tmp_path)))


def bucket(fraction: float) -> dict:
    return {"kind": "weekly", "label": "Weekly Limit Remaining",
            "remaining_fraction": fraction, "reset_at": time.time() + 3600}


def usage(*, gemini: float = 1.0, third: float = 1.0, argon: float = 1.0,
          hydrogen: float = 1.0, fetched_at: float | None = None) -> dict:
    return {
        "fetched_at": time.time() if fetched_at is None else fetched_at,
        "groups": [
            {
                "name": "Gemini Models",
                "description": "Models within this group: Gemini Flash, Gemini Pro",
                "models": "Gemini Flash, Gemini Pro",
                "buckets": [bucket(gemini)],
            },
            {
                "name": "Claude and GPT models",
                "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
                "models": "Claude Opus, Claude Sonnet, GPT-OSS",
                "buckets": [bucket(third)],
            },
            {
                "name": "Gemini 4 Argon",
                "description": "For paid Pro plans, Argon quota is separate.",
                "models": "For paid Pro plans, Argon quota is separate.",
                "buckets": [bucket(argon)],
            },
            {
                "name": "Gemini 4 Hydrogen",
                "description": "Hydrogen quota is separate.",
                "models": "Hydrogen quota is separate.",
                "buckets": [bucket(hydrogen)],
            },
        ],
    }


def test_argon_scope_does_not_block_flash_or_pro(tmp_path):
    pool = make_pool(tmp_path)
    pool.observe_quota(ACCOUNT_ID, usage(argon=0))

    assert pool._blocked(ACCOUNT_ID, "gemini-4-argon-high")
    assert not pool._blocked(ACCOUNT_ID, "gemini-3.8-flash-high")
    assert not pool._blocked(ACCOUNT_ID, "gemini-3.1-pro-high")
    assert not pool._blocked(ACCOUNT_ID, "claude-sonnet-5-5-low")
    assert pool.public(ACCOUNT_ID)["exhausted_scopes"] == ["gemini-4-argon"]

    record = pool.data["exhausted"][ACCOUNT_ID]["gemini-4-argon"]
    assert record["include"] == ["gemini-4-argon"]
    assert record["until"] > time.time()


def test_dedicated_near_zero_is_treated_as_exhausted(tmp_path):
    pool = make_pool(tmp_path)
    # The upstream can report a small positive remainder for a used-up
    # dedicated bucket (observed 0.55% for Gemini 4 Argon).
    pool.observe_quota(ACCOUNT_ID, usage(argon=0.0055))

    assert pool._blocked(ACCOUNT_ID, "gemini-4-argon-high")
    assert not pool._blocked(ACCOUNT_ID, "gemini-3.8-flash-high")
    assert pool.public(ACCOUNT_ID)["exhausted_scopes"] == ["gemini-4-argon"]


def test_dedicated_usable_balance_is_not_exhausted(tmp_path):
    pool = make_pool(tmp_path)
    pool.observe_quota(ACCOUNT_ID, usage(argon=0.02))

    assert not pool._blocked(ACCOUNT_ID, "gemini-4-argon-high")


def test_generic_group_keeps_the_exact_zero_rule(tmp_path):
    pool = make_pool(tmp_path)
    pool.observe_quota(ACCOUNT_ID, usage(gemini=0.005))

    assert not pool._blocked(ACCOUNT_ID, "gemini-3.8-flash-high")


def test_generic_gemini_scope_excludes_dedicated_families(tmp_path):
    pool = make_pool(tmp_path)
    pool.observe_quota(ACCOUNT_ID, usage(gemini=0))

    assert pool._blocked(ACCOUNT_ID, "gemini-3.8-flash-high")
    # Argon and Hydrogen have their own buckets and are still healthy.
    assert not pool._blocked(ACCOUNT_ID, "gemini-4-argon-high")
    assert not pool._blocked(ACCOUNT_ID, "gemini-4-hydrogen-high")
    assert not pool._blocked(ACCOUNT_ID, "claude-sonnet-5-5-low")

    record = pool.data["exhausted"][ACCOUNT_ID]["gemini"]
    assert record["include"] == ["gemini"]
    assert set(record["exclude"]) == {"gemini-4-argon", "gemini-4-hydrogen"}


def test_third_party_scope_does_not_block_gemini(tmp_path):
    pool = make_pool(tmp_path)
    pool.observe_quota(ACCOUNT_ID, usage(third=0))

    assert pool._blocked(ACCOUNT_ID, "claude-sonnet-5-5-low")
    assert pool._blocked(ACCOUNT_ID, "gpt-oss-120b-medium")
    assert not pool._blocked(ACCOUNT_ID, "gemini-3.8-flash-high")
    assert pool.public(ACCOUNT_ID)["exhausted_scopes"] == ["third-party"]


def test_new_independent_family_gets_its_own_scope(tmp_path):
    pool = make_pool(tmp_path)
    pool.observe_quota(ACCOUNT_ID, usage(hydrogen=0))

    assert pool._blocked(ACCOUNT_ID, "gemini-4-hydrogen-high")
    assert not pool._blocked(ACCOUNT_ID, "gemini-4-argon-high")
    assert not pool._blocked(ACCOUNT_ID, "gemini-3.8-flash-high")
    assert pool.public(ACCOUNT_ID)["exhausted_scopes"] == ["gemini-4-hydrogen"]


def test_family_tokens_match_versioned_model_ids(tmp_path):
    pool = make_pool(tmp_path)
    data = usage()
    data["groups"].append({
        "name": "Gemini Flash",
        "description": "Flash quota is separate.",
        "models": "Flash quota is separate.",
        "buckets": [bucket(0)],
    })
    pool.observe_quota(ACCOUNT_ID, data)

    assert pool._blocked(ACCOUNT_ID, "gemini-3.8-flash-high")
    assert not pool._blocked(ACCOUNT_ID, "gemini-2.5-pro-high")
    assert not pool._blocked(ACCOUNT_ID, "gemini-4-argon-high")


def test_legacy_broad_record_is_removed_by_a_healthy_snapshot(tmp_path):
    pool = make_pool(tmp_path)
    pool.data["exhausted"].setdefault(ACCOUNT_ID, {})["gemini"] = {
        "observed_at": time.time() - 10, "until": time.time() + 3600,
    }
    assert pool._blocked(ACCOUNT_ID, "gemini-3.8-flash-high")

    pool.observe_quota(ACCOUNT_ID, usage())

    assert "gemini" not in pool.data["exhausted"][ACCOUNT_ID]
    assert not pool._blocked(ACCOUNT_ID, "gemini-3.8-flash-high")


def test_expired_record_is_not_reported(tmp_path):
    pool = make_pool(tmp_path)
    pool.observe_quota(ACCOUNT_ID, usage(argon=0))

    pool.data["exhausted"][ACCOUNT_ID]["gemini-4-argon"]["until"] = time.time() - 1

    assert not pool._blocked(ACCOUNT_ID, "gemini-4-argon-high")
    assert pool.public(ACCOUNT_ID)["exhausted_scopes"] == []
