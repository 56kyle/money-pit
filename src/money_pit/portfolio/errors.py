"""Module containing explicit portfolio-construction failures."""


class PortfolioError(Exception):
    """Base class for portfolio-construction failures."""


class IncompletePortfolioPolicyError(PortfolioError):
    """Raised when a policy does not cover every optimization input."""


class InvalidOptimizationInputError(PortfolioError):
    """Raised when optimization inputs are internally inconsistent."""


class OptimizerUnavailableError(PortfolioError):
    """Raised when the configured optimizer dependency or solver is unavailable."""


class OptimizationFailedError(PortfolioError):
    """Raised when the solver fails or returns an unacceptable status."""


class OptimizationResultError(PortfolioError):
    """Raised when a solver result violates the declared policy."""
