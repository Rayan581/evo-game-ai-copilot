from evo.tools.analyze import bootstrap_diff_ci, summarize_curve

from conftest import COLLAPSE, RISING


def pts(vals):
    return [{"step": (i + 1) * 10000, "mean": v} for i, v in enumerate(vals)]


def test_collapse_detected(settings):
    s = summarize_curve(pts(COLLAPSE), k=3, target=100, a=settings["analysis"])
    assert "collapse" in s["flags"]
    assert s["collapse"]["smoothed_peak"] > 100 and s["collapse"]["lost_frac"] > 0.5


def test_healthy_curve_has_no_collapse(settings):
    s = summarize_curve(pts(RISING), k=3, target=100, a=settings["analysis"])
    assert "collapse" not in s["flags"]
    assert s["score"] == round((40 + 60 + 80) / 3, 2)


def test_plateau_detected_when_flat_below_target(settings):
    s = summarize_curve(pts([-100, -20, 10, 11, 10, 11, 10, 10]), k=3, target=100, a=settings["analysis"])
    assert "plateau" in s["flags"]


def test_score_uses_last_k(settings):
    s = summarize_curve(pts([0, 0, 0, 10, 20, 30]), k=2, target=None, a=settings["analysis"])
    assert s["score"] == 25.0


def test_overlapping_seeds_give_ci_spanning_zero():
    ci = bootstrap_diff_ci([10, 90, 40], [30, 60, 20], n_boot=2000, level=0.95, seed=1)
    assert ci[0] < 0 < ci[1]


def test_clear_difference_excludes_zero():
    ci = bootstrap_diff_ci([100, 105, 98], [10, 12, 9], n_boot=2000, level=0.95, seed=1)
    assert ci[0] > 0
