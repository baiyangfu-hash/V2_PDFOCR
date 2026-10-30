from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from eip.ir.schema import (
    GeometryEdge,
    GuardedStatement,
    InstructionStep,
    LadderRungIR,
    LadderSymbolNode,
    LogicExprNode,
)


class LadderStackCompiler:
    """
    P0-04: Three-Stage Ladder Compiler with Mitsubishi FX5U MPS/MRD/MPP Result Stack Semantics.
    Converts a Rung's directed Geometry Graph into:
      1. Convergent series-parallel (AND/OR) condition expressions between junctions
         (including inline COMPARE_BOX and INV_LINE '/' inversion)
      2. Explicit InstructionSequence including MPS (push), MRD (read top), MPP (pop)
         when a junction forks into multiple independent output branches (max 11 levels)
      3. Ordered GuardedStatement AST list preserving PLC top-to-bottom scan execution order
    """

    MAX_MPS_DEPTH = 11
    OUTPUT_KINDS = {"OUT_COIL", "SET_COIL", "RST_COIL", "FUNC_BLOCK"}

    def compile_rung(self, rung: LadderRungIR) -> LadderRungIR:
        adj: Dict[str, List[GeometryEdge]] = {}
        rev_adj: Dict[str, List[GeometryEdge]] = {}
        for edge in rung.geometry_edges:
            adj.setdefault(edge.source_node, []).append(edge)
            rev_adj.setdefault(edge.target_node, []).append(edge)

        for jid in adj:
            adj[jid].sort(
                key=lambda e: (round(e.symbol.left_port_coord[1], 1), e.symbol.anchor.bbox.x0)
            )

        output_edges: List[GeometryEdge] = [
            e for e in rung.geometry_edges if e.symbol.symbol_kind in self.OUTPUT_KINDS
        ]
        output_edges.sort(
            key=lambda e: (round(e.symbol.left_port_coord[1], 1), e.symbol.anchor.bbox.x0)
        )

        memo_expr: Dict[str, LogicExprNode] = {
            rung.left_rail_junction: LogicExprNode(expr_type="TRUE")
        }

        def compute_junction_expr(target_j: str, visited: Set[str]) -> LogicExprNode:
            if target_j in memo_expr:
                return memo_expr[target_j]
            if target_j in visited:
                return LogicExprNode(expr_type="TRUE")
            visited_next = set(visited) | {target_j}
            in_edges = [
                e for e in rev_adj.get(target_j, []) if e.symbol.symbol_kind not in self.OUTPUT_KINDS
            ]
            if not in_edges:
                return LogicExprNode(expr_type="TRUE")

            branch_exprs: List[LogicExprNode] = []
            for edge in in_edges:
                pred_expr = compute_junction_expr(edge.source_node, visited_next)
                if edge.symbol.symbol_kind == "INV_LINE":
                    combined = LogicExprNode(
                        expr_type="NOT",
                        children=[pred_expr],
                        symbol_id=edge.symbol.symbol_id,
                        anchor_ids=pred_expr.anchor_ids + [edge.symbol.symbol_id],
                    )
                else:
                    atom_expr = self._symbol_to_logic_expr(edge.symbol)
                    if pred_expr.expr_type == "TRUE":
                        combined = atom_expr
                    else:
                        combined = self._make_and([pred_expr, atom_expr])
                branch_exprs.append(combined)

            if len(branch_exprs) == 1:
                res = branch_exprs[0]
            else:
                res = self._make_or(branch_exprs)
            memo_expr[target_j] = res
            return res

        fork_to_outputs: Dict[str, List[GeometryEdge]] = {}
        output_to_fork_and_local: Dict[str, Tuple[str, List[LadderSymbolNode]]] = {}

        for out_e in output_edges:
            curr_j = out_e.source_node
            local_chain: List[LadderSymbolNode] = []
            while curr_j != rung.left_rail_junction:
                out_deg = len(adj.get(curr_j, []))
                in_edges = [
                    e for e in rev_adj.get(curr_j, []) if e.symbol.symbol_kind not in self.OUTPUT_KINDS
                ]
                if out_deg >= 2 or len(in_edges) != 1:
                    break
                only_in = in_edges[0]
                local_chain.insert(0, only_in.symbol)
                curr_j = only_in.source_node

            fork_j = curr_j
            fork_to_outputs.setdefault(fork_j, []).append(out_e)
            output_to_fork_and_local[out_e.edge_id] = (fork_j, local_chain)

        inst_seq: List[InstructionStep] = []
        guarded_stmts: List[GuardedStatement] = []
        step_no = 0
        emitted_forks: Set[str] = set()

        for exec_idx, out_e in enumerate(output_edges, start=1):
            fork_j, local_syms = output_to_fork_and_local[out_e.edge_id]
            siblings = fork_to_outputs.get(fork_j, [out_e])
            prefix_expr = compute_junction_expr(fork_j, set())

            if fork_j not in emitted_forks:
                emitted_forks.add(fork_j)
                prefix_steps = self._expr_to_instructions(prefix_expr, is_root=True)
                for op, ops, sid in prefix_steps:
                    step_no += 1
                    inst_seq.append(
                        InstructionStep(
                            step_no=step_no,
                            opcode=op,
                            operands=ops,
                            source_symbol_id=sid,
                            stack_depth_after=0,
                        )
                    )

            stack_depth = 0
            if len(siblings) >= 2 and fork_j != rung.left_rail_junction:
                branch_pos = siblings.index(out_e)
                if branch_pos == 0:
                    step_no += 1
                    stack_depth = 1
                    inst_seq.append(
                        InstructionStep(
                            step_no=step_no,
                            opcode="MPS",
                            operands=[],
                            stack_depth_after=stack_depth,
                        )
                    )
                elif branch_pos < len(siblings) - 1:
                    step_no += 1
                    stack_depth = 1
                    inst_seq.append(
                        InstructionStep(
                            step_no=step_no,
                            opcode="MRD",
                            operands=[],
                            stack_depth_after=stack_depth,
                        )
                    )
                else:
                    step_no += 1
                    stack_depth = 0
                    inst_seq.append(
                        InstructionStep(
                            step_no=step_no,
                            opcode="MPP",
                            operands=[],
                            stack_depth_after=stack_depth,
                        )
                    )

            # Build full enable condition and emit local branch series instructions
            curr_enable: LogicExprNode = prefix_expr
            for idx_local, sym in enumerate(local_syms):
                step_no += 1
                is_load = (
                    fork_j == rung.left_rail_junction
                    and idx_local == 0
                    and prefix_expr.expr_type == "TRUE"
                )
                if sym.symbol_kind == "INV_LINE":
                    curr_enable = LogicExprNode(
                        expr_type="NOT",
                        children=[curr_enable],
                        symbol_id=sym.symbol_id,
                        anchor_ids=curr_enable.anchor_ids + [sym.symbol_id],
                    )
                    inst_seq.append(
                        InstructionStep(
                            step_no=step_no,
                            opcode="INV",
                            operands=[],
                            source_symbol_id=sym.symbol_id,
                            stack_depth_after=stack_depth,
                        )
                    )
                elif sym.symbol_kind == "COMPARE_BOX":
                    comp_expr = self._symbol_to_logic_expr(sym)
                    curr_enable = (
                        comp_expr
                        if curr_enable.expr_type == "TRUE"
                        else self._make_and([curr_enable, comp_expr])
                    )
                    op_prefix = "LD" if is_load else "AND"
                    op_code = (
                        sym.mnemonic
                        if sym.mnemonic.startswith(("LD", "AND", "OR"))
                        else f"{op_prefix}{sym.mnemonic}"
                    )
                    ops_txt = [
                        op.raw_text if hasattr(op, "raw_text") else str(op)
                        for op in sym.operands
                    ]
                    inst_seq.append(
                        InstructionStep(
                            step_no=step_no,
                            opcode=op_code,
                            operands=ops_txt,
                            source_symbol_id=sym.symbol_id,
                            stack_depth_after=stack_depth,
                        )
                    )
                else:
                    sym_expr = self._symbol_to_logic_expr(sym)
                    curr_enable = (
                        sym_expr
                        if curr_enable.expr_type == "TRUE"
                        else self._make_and([curr_enable, sym_expr])
                    )
                    op_code = self._contact_opcode(sym, is_load=is_load)
                    dev_txt = sym.device.raw_text if sym.device else ""
                    inst_seq.append(
                        InstructionStep(
                            step_no=step_no,
                            opcode=op_code,
                            operands=[dev_txt] if dev_txt else [],
                            source_symbol_id=sym.symbol_id,
                            stack_depth_after=stack_depth,
                        )
                    )

            out_sym = out_e.symbol
            step_no += 1
            out_ops = [
                op.raw_text if hasattr(op, "raw_text") else str(op)
                for op in out_sym.operands
            ]
            inst_seq.append(
                InstructionStep(
                    step_no=step_no,
                    opcode=out_sym.mnemonic,
                    operands=out_ops,
                    source_symbol_id=out_sym.symbol_id,
                    stack_depth_after=stack_depth,
                )
            )

            if out_sym.symbol_kind == "OUT_COIL":
                act_kind = "COIL_OUT"
                is_stateful = False
            elif out_sym.symbol_kind == "SET_COIL":
                act_kind = "COIL_SET"
                is_stateful = True
            elif out_sym.symbol_kind == "RST_COIL":
                act_kind = "COIL_RST"
                is_stateful = True
            else:
                act_kind = "FUNC_EXEC"
                is_stateful = True

            guarded_stmts.append(
                GuardedStatement(
                    exec_order=exec_idx,
                    enable_condition=curr_enable,
                    action_kind=act_kind,  # type: ignore[arg-type]
                    mnemonic=out_sym.mnemonic,
                    target_device=out_sym.device,
                    operands=out_sym.operands,
                    is_stateful=is_stateful,
                    anchor=out_sym.anchor,
                )
            )

        rung.instruction_sequence = inst_seq
        rung.guarded_statements = guarded_stmts
        return rung

    @staticmethod
    def _symbol_to_logic_expr(sym: LadderSymbolNode) -> LogicExprNode:
        if sym.symbol_kind == "COMPARE_BOX":
            ops_str = ", ".join(
                op.raw_text if hasattr(op, "raw_text") else str(op)
                for op in sym.operands
            )
            return LogicExprNode(
                expr_type="COMPARE",
                operator=f"{sym.mnemonic}({ops_str})",
                device_ref=sym.device,
                symbol_id=sym.symbol_id,
                anchor_ids=[sym.symbol_id],
            )
        atom = LogicExprNode(
            expr_type="ATOM",
            operator=sym.device.raw_text if sym.device else sym.mnemonic,
            device_ref=sym.device,
            symbol_id=sym.symbol_id,
            anchor_ids=[sym.symbol_id],
        )
        if sym.symbol_kind == "NC_CONTACT":
            return LogicExprNode(
                expr_type="NOT",
                children=[atom],
                symbol_id=sym.symbol_id,
                anchor_ids=[sym.symbol_id],
            )
        if sym.symbol_kind == "RISE_CONTACT":
            return LogicExprNode(
                expr_type="RISE_EDGE",
                device_ref=sym.device,
                symbol_id=sym.symbol_id,
                anchor_ids=[sym.symbol_id],
            )
        if sym.symbol_kind == "FALL_CONTACT":
            return LogicExprNode(
                expr_type="FALL_EDGE",
                device_ref=sym.device,
                symbol_id=sym.symbol_id,
                anchor_ids=[sym.symbol_id],
            )
        return atom

    @staticmethod
    def _make_and(items: List[LogicExprNode]) -> LogicExprNode:
        flat: List[LogicExprNode] = []
        anchors: List[str] = []
        for it in items:
            if it.expr_type == "TRUE":
                continue
            if it.expr_type == "AND":
                flat.extend(it.children)
            else:
                flat.append(it)
            anchors.extend(it.anchor_ids)
        if not flat:
            return LogicExprNode(expr_type="TRUE")
        if len(flat) == 1:
            return flat[0]
        return LogicExprNode(expr_type="AND", children=flat, anchor_ids=anchors)

    @staticmethod
    def _make_or(items: List[LogicExprNode]) -> LogicExprNode:
        flat: List[LogicExprNode] = []
        anchors: List[str] = []
        for it in items:
            if it.expr_type == "OR":
                flat.extend(it.children)
            else:
                flat.append(it)
            anchors.extend(it.anchor_ids)
        if len(flat) == 1:
            return flat[0]
        return LogicExprNode(expr_type="OR", children=flat, anchor_ids=anchors)

    @staticmethod
    def _contact_opcode(sym: LadderSymbolNode, is_load: bool) -> str:
        if sym.symbol_kind == "NC_CONTACT":
            return "LDI" if is_load else "ANI"
        if sym.symbol_kind == "RISE_CONTACT":
            return "LDP" if is_load else "ANDP"
        if sym.symbol_kind == "FALL_CONTACT":
            return "LDF" if is_load else "ANDF"
        return "LD" if is_load else "AND"

    def _expr_to_instructions(
        self, expr: LogicExprNode, is_root: bool = False
    ) -> List[Tuple[str, List[str], Optional[str]]]:
        if expr.expr_type == "TRUE":
            return []
        if expr.expr_type == "ATOM":
            op = "LD" if is_root else "AND"
            dev = expr.device_ref.raw_text if expr.device_ref else ""
            return [(op, [dev], expr.symbol_id)]
        if expr.expr_type == "COMPARE":
            op = "LD_CMP" if is_root else "AND_CMP"
            return [(op, [expr.operator or ""], expr.symbol_id)]
        if expr.expr_type == "NOT" and expr.children:
            child = expr.children[0]
            if child.expr_type == "ATOM":
                op = "LDI" if is_root else "ANI"
                dev = child.device_ref.raw_text if child.device_ref else ""
                return [(op, [dev], expr.symbol_id)]
            inner_steps = self._expr_to_instructions(child, is_root=is_root)
            inner_steps.append(("INV", [], expr.symbol_id))
            return inner_steps
        if expr.expr_type == "RISE_EDGE":
            op = "LDP" if is_root else "ANDP"
            dev = expr.device_ref.raw_text if expr.device_ref else ""
            return [(op, [dev], expr.symbol_id)]
        if expr.expr_type == "FALL_EDGE":
            op = "LDF" if is_root else "ANDF"
            dev = expr.device_ref.raw_text if expr.device_ref else ""
            return [(op, [dev], expr.symbol_id)]
        if expr.expr_type == "OR":
            steps: List[Tuple[str, List[str], Optional[str]]] = []
            for idx, ch in enumerate(expr.children):
                if idx == 0:
                    steps.extend(self._expr_to_instructions(ch, is_root=is_root))
                else:
                    if ch.expr_type == "ATOM":
                        dev = ch.device_ref.raw_text if ch.device_ref else ""
                        steps.append(("OR", [dev], ch.symbol_id))
                    elif ch.expr_type == "NOT" and ch.children and ch.children[0].expr_type == "ATOM":
                        dev = ch.children[0].device_ref.raw_text if ch.children[0].device_ref else ""
                        steps.append(("ORI", [dev], ch.symbol_id))
                    else:
                        steps.extend(self._expr_to_instructions(ch, is_root=True))
                        steps.append(("ORB", [], None))
            return steps
        if expr.expr_type == "AND":
            steps = []
            for idx, ch in enumerate(expr.children):
                if idx == 0:
                    steps.extend(self._expr_to_instructions(ch, is_root=is_root))
                else:
                    if ch.expr_type == "OR":
                        steps.extend(self._expr_to_instructions(ch, is_root=True))
                        steps.append(("ANB", [], None))
                    else:
                        steps.extend(self._expr_to_instructions(ch, is_root=False))
            return steps
        return []
