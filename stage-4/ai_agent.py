"""Morrow assistant intent parsing. Providers return proposals only; execution stays in Service."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


class IntentError(ValueError):
    pass


class DeterministicProvider:
    """Offline-safe parser. It never receives credentials or performs financial actions."""

    def propose(self, text: str, *, caller_handle: str, known_handles: set[str],
                minor_units: int, currency: str = "EUR") -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise IntentError("Enter a request of up to 1,000 characters.")
        lowered = text.lower()
        explicit_currency = None
        for pattern, code in ((r"₹|\brs\.?\b|\brupees?\b", "INR"),
                              (r"€|\beur\b", "EUR"), (r"£|\bgbp\b", "GBP"),
                              (r"\$|\busd\b", "USD"), (r"¥|\bjpy\b", "JPY"),
                              (r"\bbhd\b", "BHD")):
            if re.search(pattern, lowered):
                explicit_currency = code
                break
        if explicit_currency and explicit_currency != currency:
            raise IntentError(f"This request is in {explicit_currency}, but your Morrow account uses {currency}.")
        amount_match = re.search(r"(?:₹|rs\.?\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)", lowered)
        if not amount_match:
            raise IntentError("Include an amount, such as ₹1,500.")
        try:
            amount = Decimal(amount_match.group(1).replace(",", "")) * (10 ** minor_units)
        except InvalidOperation as exc:
            raise IntentError("That amount is not valid.") from exc
        if amount != amount.to_integral_value() or amount < 1 or amount > 1_000_000_000:
            raise IntentError("Amount must be positive and fit the currency's minor units.")
        amount_minor = int(amount)
        if "split" in lowered or "bill" in lowered or "dinner" in lowered:
            handles = []
            for handle, aliases in known_handles.items():
                if any(re.search(rf"(?<![a-z0-9_]){re.escape(alias.lower())}(?![a-z0-9_])", lowered)
                       for alias in aliases):
                    handles.append(handle)
            # Natural-language names are resolved against registered handles/display names
            # by the caller before invoking this provider.
            if caller_handle not in handles:
                handles.append(caller_handle)
            handles = list(dict.fromkeys(handles))
            if len(handles) < 2:
                raise IntentError("Name at least one other Morrow handle to split with you.")
            return {"action": "split", "amount": amount_minor,
                    "participant_handles": handles,
                    "note": "AI proposed shared expense", "provider": "deterministic_fallback"}
        if "request" in lowered:
            candidates = [h for h, aliases in known_handles.items() if h != caller_handle and
                          any(re.search(rf"(?<![a-z0-9_]){re.escape(alias.lower())}(?![a-z0-9_])", lowered)
                              for alias in aliases)]
            if len(candidates) != 1:
                raise IntentError("Name one registered handle to request money from.")
            return {"action": "request", "amount": amount_minor,
                    "payer_handle": candidates[0], "note": "AI proposed payment request",
                    "provider": "deterministic_fallback"}
        if re.search(r"\b(prepare|pay|checkout|payment)\b", lowered):
            candidates = [h for h, aliases in known_handles.items() if h != caller_handle and
                          any(re.search(rf"(?<![a-z0-9_]){re.escape(alias.lower())}(?![a-z0-9_])", lowered)
                              for alias in aliases)]
            if len(candidates) != 1:
                raise IntentError("Name one registered person the merchant checkout is for.")
            target = candidates[0]
            return {"action": "paypal_checkout", "amount": amount_minor,
                    "context_handle": target,
                    "note": f"Morrow Sandbox merchant checkout for {target}",
                    "provider": "deterministic_fallback"}
        if "owe" in lowered or "how much" in lowered:
            candidates = [h for h, aliases in known_handles.items() if h != caller_handle and
                          any(re.search(rf"(?<![a-z0-9_]){re.escape(alias.lower())}(?![a-z0-9_])", lowered)
                              for alias in aliases)]
            if len(candidates) != 1:
                raise IntentError("Name one registered handle to check.")
            return {"action": "balance_query", "other_handle": candidates[0],
                    "provider": "deterministic_fallback"}
        raise IntentError("Try splitting a bill or requesting money from a registered handle.")
