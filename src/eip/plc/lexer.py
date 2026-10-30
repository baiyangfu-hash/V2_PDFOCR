from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional
import yaml

from eip.ir.schema import TypedDeviceIR


class FX5UDeviceLexer:
    """
    Deterministic Mitsubishi MELSEC iQ-F (FX5U) Device Lexer & Validator (P0-02).
    Strictly enforces:
      - Octal addressing for X / Y (rejects digits 8, 9)
      - Decimal addressing for M, L, SM, D, R, SD, T, ST, C, LC, Z, V
      - Hexadecimal addressing for B, W
      - Word bit specification D100.0 ~ D100.F (hexadecimal bit index 0..F only; disallowed on bit devices)
      - Digit specification K1X0 ~ K8M100 (K1..K8 nibbles only on bit devices)
      - Index register modification (e.g. D100Z0, X0Z1, M10V0; Z0..Z19, V0..V19)
      - Numeric constants: K (decimal), H (hexadecimal), E (float)
    """

    _CONST_K_RE = re.compile(r"^K([+-]?[0-9]+)$")
    _CONST_H_RE = re.compile(r"^H([0-9A-F]+)$")
    _CONST_E_RE = re.compile(r"^E([+-]?[0-9]+(?:\.[0-9]+)?)$")

    # Composite device pattern:
    # Optional K<digit> prefix + Device letters (SM, SD, ST, LC, X, Y, M, L, B, D, R, W, T, C, Z, V)
    # + Base address + Optional .<bit> + Optional <Z/V><index_num>
    _DEVICE_RE = re.compile(
        r"^(?:K([0-9]+))?"
        r"(SM|SD|ST|LC|X|Y|M|L|B|D|R|W|T|C|Z|V)"
        r"([0-9A-Z]+?)"
        r"(?:\.([0-9A-Z]+))?"
        r"(?:([ZV])([0-9]+))?$"
    )

    def __init__(self, profile_path: Optional[Path] = None) -> None:
        if profile_path is None:
            profile_path = (
                Path(__file__).resolve().parent
                / "profiles"
                / "mitsubishi"
                / "fx5u"
                / "devices.yaml"
            )
        with open(profile_path, "r", encoding="utf-8") as f:
            self.profile: Dict[str, Any] = yaml.safe_load(f)
        self.devices_cfg: Dict[str, Any] = self.profile["devices"]

    def parse(self, raw_token: str) -> TypedDeviceIR:
        token = raw_token.strip().upper()
        if not token:
            return self._invalid(raw_token, "Empty device token")

        # 1. Check constants (K, H, E) - Note: K followed by a device (e.g. K4M0) is NOT a constant
        if token.startswith("K") and len(token) > 1 and (token[1].isdigit() or token[1] in "+-"):
            # Check if it's purely a decimal constant like K100, K-50
            m_k = self._CONST_K_RE.match(token)
            if m_k:
                val = int(m_k.group(1), 10)
                return TypedDeviceIR(
                    raw_text=token,
                    device_type="K",
                    base_address=m_k.group(1),
                    radix=10,
                    address_int=val,
                    data_type="CONSTANT",
                    is_constant=True,
                    is_valid=True,
                )
            # If it starts with K<digit> but has non-digits and does NOT match a device prefix, reject
            if not re.match(r"^K[0-9]+(SM|SD|ST|LC|X|Y|M|L|B|D|R|W|T|C)", token):
                return self._invalid(raw_token, f"Invalid decimal constant syntax: {token}")

        if token.startswith("H"):
            m_h = self._CONST_H_RE.match(token)
            if m_h:
                val = int(m_h.group(1), 16)
                return TypedDeviceIR(
                    raw_text=token,
                    device_type="H",
                    base_address=m_h.group(1),
                    radix=16,
                    address_int=val,
                    data_type="CONSTANT",
                    is_constant=True,
                    is_valid=True,
                )
            return self._invalid(raw_token, f"Invalid hexadecimal constant syntax: {token}")

        if token.startswith("E"):
            m_e = self._CONST_E_RE.match(token)
            if m_e:
                return TypedDeviceIR(
                    raw_text=token,
                    device_type="E",
                    base_address=m_e.group(1),
                    radix=10,
                    address_int=0,
                    data_type="CONSTANT",
                    is_constant=True,
                    is_valid=True,
                )
            return self._invalid(raw_token, f"Invalid floating-point constant syntax: {token}")

        # 2. Match standard or composite FX5U device
        m = self._DEVICE_RE.match(token)
        if not m:
            return self._invalid(raw_token, f"Unrecognized FX5U device format: {token}")

        k_str, dev_type, base_addr, bit_str, idx_type, idx_num_str = m.groups()
        cfg = self.devices_cfg.get(dev_type)
        if not cfg:
            return self._invalid(raw_token, f"Unsupported FX5U device type: {dev_type}")

        radix: int = int(cfg["radix"])

        # 3. Validate base address according to device radix
        addr_int: int
        if radix == 8:
            if not re.match(r"^[0-7]+$", base_addr):
                return self._invalid(
                    raw_token,
                    f"Octal device {dev_type} contains non-octal digits (8 or 9) in '{base_addr}'",
                    dev_type=dev_type,
                    radix=8,
                )
            addr_int = int(base_addr, 8)
            max_oct = int(str(cfg["max_address_octal"]), 8)
            if addr_int > max_oct:
                return self._invalid(
                    raw_token,
                    f"Octal address {dev_type}{base_addr} exceeds FX5U maximum {cfg['max_address_octal']}",
                    dev_type=dev_type,
                    radix=8,
                )
        elif radix == 10:
            if not re.match(r"^[0-9]+$", base_addr):
                return self._invalid(
                    raw_token,
                    f"Decimal device {dev_type} contains non-decimal characters in '{base_addr}'",
                    dev_type=dev_type,
                    radix=10,
                )
            addr_int = int(base_addr, 10)
            max_dec = int(cfg["max_address_dec"])
            if addr_int > max_dec:
                return self._invalid(
                    raw_token,
                    f"Decimal address {dev_type}{base_addr} exceeds FX5U maximum {max_dec}",
                    dev_type=dev_type,
                    radix=10,
                )
        elif radix == 16:
            if not re.match(r"^[0-9A-F]+$", base_addr):
                return self._invalid(
                    raw_token,
                    f"Hex device {dev_type} contains invalid hex characters in '{base_addr}'",
                    dev_type=dev_type,
                    radix=16,
                )
            addr_int = int(base_addr, 16)
            max_hex = int(str(cfg["max_address_hex"]), 16)
            if addr_int > max_hex:
                return self._invalid(
                    raw_token,
                    f"Hex address {dev_type}{base_addr} exceeds FX5U maximum {cfg['max_address_hex']}",
                    dev_type=dev_type,
                    radix=16,
                )
        else:
            return self._invalid(raw_token, f"Invalid radix {radix}")

        # 4. Validate Digit Specification (K1..K8)
        digit_k: Optional[int] = None
        data_type: str = cfg["data_type"]
        if k_str is not None:
            if not cfg.get("allow_digit_spec", False):
                return self._invalid(
                    raw_token,
                    f"Device {dev_type} does not allow digit specification (K{k_str})",
                    dev_type=dev_type,
                    radix=radix,
                )
            digit_k = int(k_str, 10)
            if not (1 <= digit_k <= 8):
                return self._invalid(
                    raw_token,
                    f"Digit specification K{digit_k} out of valid range K1..K8",
                    dev_type=dev_type,
                    radix=radix,
                )
            data_type = "WORD" if digit_k <= 4 else "DWORD"

        # 5. Validate Word Bit Specification (.0 ~ .F)
        bit_index: Optional[int] = None
        if bit_str is not None:
            if not cfg.get("allow_bit_spec", False):
                return self._invalid(
                    raw_token,
                    f"Bit device or non-word device {dev_type} does not support bit suffix '.{bit_str}'",
                    dev_type=dev_type,
                    radix=radix,
                )
            if k_str is not None:
                return self._invalid(
                    raw_token,
                    f"Cannot combine digit specification K{k_str} with bit specification '.{bit_str}'",
                    dev_type=dev_type,
                    radix=radix,
                )
            # In FX5U, bit specification on 16-bit word registers is 1-character Hex 0..F
            if not re.match(r"^[0-9A-F]$", bit_str):
                return self._invalid(
                    raw_token,
                    f"Word bit specification '.{bit_str}' must be a single hexadecimal digit 0..F (0..15)",
                    dev_type=dev_type,
                    radix=radix,
                )
            bit_index = int(bit_str, 16)
            data_type = "BOOL"

        # 6. Validate Index Register Modification (Z0..Z19, V0..V19)
        index_reg: Optional[str] = None
        if idx_type is not None:
            if not cfg.get("allow_index_mod", False):
                return self._invalid(
                    raw_token,
                    f"Device {dev_type} does not allow index register modification ({idx_type}{idx_num_str})",
                    dev_type=dev_type,
                    radix=radix,
                )
            idx_num = int(idx_num_str, 10)
            idx_max = int(self.devices_cfg[idx_type]["max_address_dec"])
            if idx_num > idx_max:
                return self._invalid(
                    raw_token,
                    f"Index register {idx_type}{idx_num} exceeds FX5U maximum {idx_type}{idx_max}",
                    dev_type=dev_type,
                    radix=radix,
                )
            index_reg = f"{idx_type}{idx_num}"

        return TypedDeviceIR(
            raw_text=token,
            device_type=dev_type,
            base_address=base_addr,
            radix=radix,  # type: ignore[arg-type]
            address_int=addr_int,
            bit_index=bit_index,
            digit_k=digit_k,
            index_reg=index_reg,
            data_type=data_type,  # type: ignore[arg-type]
            is_constant=False,
            cpu_profile="FX5U",
            is_valid=True,
            validation_error=None,
        )

    @staticmethod
    def _invalid(
        raw_text: str,
        error_msg: str,
        dev_type: str = "UNKNOWN",
        radix: int = 10,
    ) -> TypedDeviceIR:
        return TypedDeviceIR(
            raw_text=raw_text.strip(),
            device_type=dev_type,
            base_address="",
            radix=radix,  # type: ignore[arg-type]
            address_int=0,
            data_type="BOOL",
            is_valid=False,
            validation_error=error_msg,
        )
