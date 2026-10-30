from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import sympy
from sympy.logic.boolalg import And, Boolean, Not, Or, Xor
from sympy.logic.inference import satisfiable

from eip.ir.schema import GuardedStatement, LadderRungIR, LogicExprNode


class EquivalenceResult:
    def __init__(
        self,
        is_equivalent: bool,
        solver_backend: str,
        counterexample: Optional[Dict[str, bool]] = None,
        details: str = "",
    ) -> None:
        self.is_equivalent = is_equivalent
        self.solver_backend = solver_backend
        self.counterexample = counterexample
        self.details = details


class LadderFormalVerifier:
    """
    P0-04: Formal Equivalence & Static Rule Verifier.
    - Supports Z3 Theorem Prover (if z3-solver is installed) and SymPy SAT (satisfiable)
    - Proves mathematical equivalence of combinational enable conditions (F_parsed != F_gt is UNSAT)
    - Generates concrete variable assignment counterexamples on logic mismatch / mutation
    - Performs Static Ladder Rule Checks (LRC):
      * Dangling / unconnected junctions ('J_UNCONNECTED')
      * Invalid FX5U device addresses
      * Double-coil (duplicate OUT_COIL target across rungs) detection
    """

    def check_enable_equivalence(
        self, expr_a: LogicExprNode, expr_b: LogicExprNode
    ) -> EquivalenceResult:
        try:
            import z3  # type: ignore

            return self._verify_with_z3(expr_a, expr_b)
        except ImportError:
            return self._verify_with_sympy_sat(expr_a, expr_b)

    def _verify_with_sympy_sat(
        self, expr_a: LogicExprNode, expr_b: LogicExprNode
    ) -> EquivalenceResult:
        sym_map: Dict[str, sympy.Symbol] = {}
        f_a = self._to_sympy(expr_a, sym_map)
        f_b = self._to_sympy(expr_b, sym_map)

        diff_formula = Xor(f_a, f_b)
        model = satisfiable(diff_formula)
        if model is False:
            return EquivalenceResult(
                is_equivalent=True,
                solver_backend="SymPy-SAT",
                counterexample=None,
                details="UNSAT: Combinational enable expressions are mathematically equivalent.",
            )
        cex = {str(k): bool(v) for k, v in model.items() if str(k) != "True"}
        return EquivalenceResult(
            is_equivalent=False,
            solver_backend="SymPy-SAT",
            counterexample=cex,
            details=f"SAT: Logic mismatch found with counterexample {cex}",
        )

    def _verify_with_z3(
        self, expr_a: LogicExprNode, expr_b: LogicExprNode
    ) -> EquivalenceResult:
        import z3  # type: ignore

        z3_vars: Dict[str, Any] = {}
        f_a = self._to_z3(expr_a, z3_vars)
        f_b = self._to_z3(expr_b, z3_vars)

        solver = z3.Solver()
        solver.add(f_a != f_b)
        chk = solver.check()
        if chk == z3.unsat:
            return EquivalenceResult(
                is_equivalent=True,
                solver_backend="Z3-SMT",
                counterexample=None,
                details="UNSAT: Combinational enable expressions are mathematically equivalent.",
            )
        m = solver.model()
        cex = {name: bool(z3.is_true(m.eval(var, model_completion=True))) for name, var in z3_vars.items()}
        return EquivalenceResult(
            is_equivalent=False,
            solver_backend="Z3-SMT",
            counterexample=cex,
            details=f"SAT: Logic mismatch found with counterexample {cex}",
        )

    def _to_sympy(
        self, node: LogicExprNode, sym_map: Dict[str, sympy.Symbol]
    ) -> Boolean:
        if node.expr_type == "TRUE":
            return sympy.true
        if node.expr_type in ("ATOM", "COMPARE"):
            name = (
                node.operator
                if node.expr_type == "COMPARE"
                else (node.device_ref.raw_text if node.device_ref else (node.operator or "UNKNOWN"))
            ) or "UNKNOWN"
            if name not in sym_map:
                sym_map[name] = sympy.Symbol(name)
            return sym_map[name]
        if node.expr_type in ("RISE_EDGE", "FALL_EDGE"):
            base = node.device_ref.raw_text if node.device_ref else "UNKNOWN"
            name = f"{node.expr_type}({base})"
            if name not in sym_map:
                sym_map[name] = sympy.Symbol(name)
            return sym_map[name]
        if node.expr_type == "NOT":
            return Not(self._to_sympy(node.children[0], sym_map))
        if node.expr_type == "AND":
            return And(*[self._to_sympy(c, sym_map) for c in node.children])
        if node.expr_type == "OR":
            return Or(*[self._to_sympy(c, sym_map) for c in node.children])
        return sympy.true

    def _to_z3(self, node: LogicExprNode, z3_vars: Dict[str, Any]) -> Any:
        import z3  # type: ignore

        if node.expr_type == "TRUE":
            return z3.BoolVal(True)
        if node.expr_type in ("ATOM", "COMPARE"):
            name = (
                node.operator
                if node.expr_type == "COMPARE"
                else (node.device_ref.raw_text if node.device_ref else (node.operator or "UNKNOWN"))
            ) or "UNKNOWN"
            if name not in z3_vars:
                z3_vars[name] = z3.Bool(name)
            return z3_vars[name]
        if node.expr_type in ("RISE_EDGE", "FALL_EDGE"):
            base = node.device_ref.raw_text if node.device_ref else "UNKNOWN"
            name = f"{node.expr_type}({base})"
            if name not in z3_vars:
                z3_vars[name] = z3.Bool(name)
            return z3_vars[name]
        if node.expr_type == "NOT":
            return z3.Not(self._to_z3(node.children[0], z3_vars))
        if node.expr_type == "AND":
            return z3.And(*[self._to_z3(c, z3_vars) for c in node.children])
        if node.expr_type == "OR":
            return z3.Or(*[self._to_z3(c, z3_vars) for c in node.children])
        return z3.BoolVal(True)

    def run_static_rules(self, rungs: List[LadderRungIR]) -> List[str]:
        """Runs deterministic static ERC/LRC checks across all parsed rungs."""
        all_diagnostics: List[str] = []
        out_coil_owners: Dict[str, List[str]] = {}

        for r in rungs:
            r_diags: List[str] = []
            for edge in r.geometry_edges:
                if edge.source_node == "J_UNCONNECTED" or edge.target_node == "J_UNCONNECTED":
                    msg = f"[{r.rung_id}] Dangling topology on symbol {edge.symbol.symbol_id} ({edge.source_node} -> {edge.target_node})"
                    r_diags.append(msg)

            for sym in r.geometry_nodes:
                if sym.device and not sym.device.is_valid:
                    msg = f"[{r.rung_id}] Invalid FX5U device on {sym.symbol_id}: {sym.device.validation_error}"
                    r_diags.append(msg)
                for op in sym.operands:
                    if hasattr(op, "is_valid") and not op.is_valid:
                        msg = f"[{r.rung_id}] Invalid operand on {sym.symbol_id}: {op.validation_error}"
                        r_diags.append(msg)

            for stmt in r.guarded_statements:
                if stmt.action_kind == "COIL_OUT" and stmt.target_device:
                    dev_name = stmt.target_device.raw_text
                    out_coil_owners.setdefault(dev_name, []).append(r.rung_id)

            r.diagnostics.extend(r_diags)
            r.verification_status = "FAILED" if r_diags else "VERIFIED"
            all_diagnostics.extend(r_diags)

        for dev_name, owner_rungs in out_coil_owners.items():
            if len(owner_rungs) > 1:
                conflict_msg = (
                    f"[DOUBLE_COIL_CONFLICT] Output coil '{dev_name}' is driven by OUT in multiple locations: {owner_rungs}"
                )
                all_diagnostics.append(conflict_msg)
                for r in rungs:
                    if r.rung_id in owner_rungs:
                        r.diagnostics.append(conflict_msg)
                        r.verification_status = "WARNING"

        return all_diagnostics
