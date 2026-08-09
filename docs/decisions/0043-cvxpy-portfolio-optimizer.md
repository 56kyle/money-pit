---
status: accepted
date: 2026-07-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Use CVXPY with Explicit CLARABEL Selection for Portfolio Optimization

## Context and Problem Statement

The former pipeline turns an individual surviving claim into an order. The portfolio-intelligence
architecture instead needs one deterministic target allocation that considers every active thesis,
the existing portfolio, risk, turnover, tax estimates, cash, and exposure limits together.

The optimizer is capital-sensitive. An LLM may supply bounded thesis judgments, but it must not
calculate weights or bypass constraints. The optimization boundary must also remain testable when
the compiled numerical dependency is unavailable.

The dependency audit found CVXPY suitable with caution at `cvxpy>=1.9.2,<2.0`. Its typed public
surface and disciplined convex programming checks fit this use, but it has a material compiled
dependency footprint. CLARABEL is available through CVXPY and supports the convex program required
here.

## Decision Drivers

- Target weights and constraint checks must remain deterministic code.
- Every instrument must be classified and every sector must have a declared cap.
- Missing policy coverage, invalid covariance, unavailable solvers, infeasibility, and invalid
  post-solve results must be distinguishable failures.
- A compiled optimizer must not become an import-time requirement for observation and report paths.
- Solver identity, version, and status must travel with every result.
- `optimal_inaccurate` must be accepted only when the versioned policy explicitly permits it.

## Considered Options

- CVXPY with explicitly selected CLARABEL
- A bespoke numerical optimizer using NumPy
- LLM-generated portfolio weights

## Decision Outcome

Use a pluggable `OptimizerBackend` protocol with an initial `ClarabelOptimizer`.

`ClarabelOptimizer` imports CVXPY only when `optimize` is called, verifies that CLARABEL is
registered, and invokes it by name. The optimization maximizes expected return less quadratic risk,
turnover, and estimated tax costs. A known sell's scalar tax coefficient is the maximum positive
unrealized-gain fraction multiplied by the applicable holding-period tax rate across its covered
lots. This deliberately upper-bounds every partial sale when a scalar objective cannot represent a
lot-order cost curve. Losses receive no assumed tax benefit. Incomplete coverage is explicitly
unknown and contributes no fabricated optimizer penalty, so an observation-only reduction remains
visible while autonomous execution rejects it.
Deterministic constraints cover required cash, individual names,
turnover, maximum position change, aggregate satellite exposure, minimum core weights, and sector
exposure.

`PortfolioPolicy` has no behavioral defaults. A caller must provide the version, objective
coefficients, every limit, the inaccurate-result policy, and the feasibility tolerance. The
optimizer rejects a universe whose sectors are not all covered or whose instruments are not all
classified as core or satellite.

Input instruments are sorted before vector construction. Covariance must be finite, symmetric, and
positive semidefinite. Only `optimal`, plus `optimal_inaccurate` when the policy permits it, is
accepted. The result is quantized and independently checked against every declared constraint
before it leaves the backend. Solver name, installed solver-package version, status, policy version,
and input snapshot identifiers are persisted in `OptimizationResult`.

The versioned policy identifier and policy fingerprint are both bound at the execution-approval
boundary. Reusing a policy version for different content is prohibited.

Determinism is scoped to identical canonical inputs, policy, Python platform, and pinned numerical
dependency versions. Cross-platform bit-for-bit equality from floating-point solvers is not
promised. The post-solve quantization and validation boundary is the stable persisted contract.

### Consequences

- Good, because the LLM cannot choose weights or relax limits.
- Good, because another backend can be substituted without changing portfolio services.
- Good, because missing compiled packages affect optimization only.
- Good, because incomplete policy coverage fails before capital-sensitive computation.
- Bad, because CVXPY and its solver stack increase installation size and platform sensitivity.
- Bad, because an inaccurate optimum is a policy decision that operators must understand and
  version.
- Neutral, because optimization uses a conservative per-weight tax estimate; the hashed plan also
  records the exact selected lots and estimated currency cost.

### Confirmation

Unit tests must pin policy completeness, instrument ordering, covariance validation, each constraint,
status handling, missing dependency behavior, solver metadata, deterministic repeated output, and
post-solve rejection. Integration tests must run the real CLARABEL backend on a small feasible and
infeasible portfolio.

## Pros and Cons of the Options

### CVXPY with explicitly selected CLARABEL

- Good, because the optimization is declarative and constraints remain inspectable.
- Good, because CVXPY verifies convex structure and CLARABEL is explicitly selected.
- Bad, because compiled transitive dependencies complicate installation and upgrades.

### Bespoke NumPy optimizer

- Good, because NumPy is already a project dependency.
- Bad, because constraint enforcement, convergence, and failure semantics would become
  project-owned numerical infrastructure.
- Bad, because independent validation would be difficult to distinguish from a duplicate solver.

### LLM-generated weights

- Good, because it requires little numerical infrastructure.
- Bad, because output is not reliably reproducible.
- Bad, because an LLM can violate or reinterpret capital constraints. This is decisive against.

## More Information

The package-audit verdict is adopt-with-caution for `cvxpy>=1.9.2,<2.0`. Dependency changes are
owned by the repository composition work, not the optimizer module.
