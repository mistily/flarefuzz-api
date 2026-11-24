import numpy as np
import pytest

from flarefuzz_api.simulation.fitting import fit_quadratic_diameter, predict_quadratic


def test_quadratic_fit_recovers_synthetic_trajectory_and_intervals_cover_fit():
    time = np.linspace(0.0, 5.0, 20)
    diameter = 0.01 + 0.002 * time - 0.0001 * time**2
    fitted = fit_quadratic_diameter(time.tolist(), diameter.tolist())
    assert fitted.coefficients == pytest.approx((0.01, 0.002, -0.0001), abs=1e-8)
    prediction = predict_quadratic(fitted, [0.0, 2.5, 5.0])
    assert prediction[1]["lower_95_m"] <= 0.01 + 0.002 * 2.5 - 0.0001 * 2.5**2
    assert prediction[1]["upper_95_m"] >= 0.01 + 0.002 * 2.5 - 0.0001 * 2.5**2


def test_fit_requires_three_distinct_times_and_positive_uncertainties():
    with pytest.raises(ValueError):
        fit_quadratic_diameter([0, 1, 1], [1, 2, 3])
    with pytest.raises(ValueError):
        fit_quadratic_diameter([0, 1, 2], [1, 2, 3], [0.1, 0.0, 0.1])
