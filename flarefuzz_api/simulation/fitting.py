"""Robust fitting entrypoint, separated from route implementation."""
from flarefuzz_api.simulation.fitting import fit_quadratic_diameter, predict_quadratic

__all__ = ["fit_quadratic_diameter", "predict_quadratic"]
