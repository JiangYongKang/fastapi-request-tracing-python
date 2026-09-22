"""Sampling must be deterministic and reproducible across runs."""

from __future__ import annotations

from tracing.sampling import Sampler


def test_same_trace_id_same_decision():
    sampler = Sampler(0.5)
    decisions_a = [sampler.should_sample(f"trace-{i}") for i in range(500)]
    decisions_b = [sampler.should_sample(f"trace-{i}") for i in range(500)]
    assert decisions_a == decisions_b


def test_slow_and_failed_are_always_sampled():
    sampler = Sampler(0.0)  # base rate drops everything
    assert sampler.should_sample("any", is_slow=True)
    assert sampler.should_sample("any", is_failed=True)
    assert sampler.should_sample("any", is_slow=True, is_failed=True)


def test_rate_boundaries():
    assert not Sampler(0.0).should_sample("x")
    assert Sampler(1.0).should_sample("x")
    # out-of-range rates are clamped
    assert Sampler(-1.0).rate == 0.0
    assert Sampler(2.0).rate == 1.0


def test_bucket_is_deterministic_and_in_range():
    for i in range(200):
        bucket = Sampler.hash_bucket(f"id-{i}")
        assert 0.0 <= bucket < 1.0
        assert bucket == Sampler.hash_bucket(f"id-{i}")


def test_partial_rate_samples_some_but_not_all():
    sampler = Sampler(0.5)
    decisions = [sampler.should_sample(f"trace-{i}") for i in range(1000)]
    ratio = sum(decisions) / len(decisions)
    assert 0.35 < ratio < 0.65  # roughly half, deterministically
