"""Official store verification adapters. This module never grants entitlements.

Call blocking methods in asyncio.to_thread. Persist grants transactionally before
finalize_purchase, and retry finalization after a crash using the same token.
No credential, purchase token, or raw callback should be written to logs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

PRODUCTS = {"coins_500": "consumable", "coins_1500": "consumable", "supporter_coffee": "nonconsumable"}
KEY_URL = "https://www.gstatic.com/admob/reward/verifier-keys.json"


class VerificationError(Exception):
    """Safe, non-secret error code suitable for a client response."""


class ReceiptCipher:
    """Encrypt only pending-finalization receipts under a separate server secret.

    Keep PLAY_RECEIPT_ENCRYPTION_KEY outside the database and client. Retain old
    keys until every pending receipt is finalized before rotating the key.
    """
    def __init__(self, key: str):
        try:
            from cryptography.fernet import Fernet
            self._fernet = Fernet(key.encode("ascii"))
        except Exception:
            raise VerificationError("receipt_storage_invalid_key") from None

    @classmethod
    def from_environment(cls):
        key = os.getenv("PLAY_RECEIPT_ENCRYPTION_KEY", "")
        return cls(key) if key else None

    def encrypt(self, token: str) -> str:
        if not isinstance(token, str) or not 10 <= len(token) <= 4096:
            raise VerificationError("purchase_invalid")
        return self._fernet.encrypt(token.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            if not isinstance(ciphertext, str) or not 1 <= len(ciphertext) <= 10000:
                raise ValueError("invalid receipt size")
            token = self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
            if not 10 <= len(token) <= 4096: raise ValueError("invalid receipt size")
            return token
        except Exception:
            raise VerificationError("receipt_storage_decryption_failed") from None


@dataclass(frozen=True)
class StoreConfig:
    package_name: str = "org.virel.pongbreaker"
    billing_enabled: bool = False
    credentials_file: str = ""
    allow_test_purchases: bool = False
    ads_enabled: bool = False
    ad_unit: str = ""
    reward_amount: int = 50
    reward_item: str = "coins"


class StoreVerifier:
    def __init__(self, config: StoreConfig):
        self.config = config
        self._session = None
        self._keys: dict[int, object] = {}
        self._keys_at = 0.0
        self._key_lock = threading.Lock()
        self._billing_lock = threading.Lock()

    @classmethod
    def from_environment(cls):
        return cls(StoreConfig(
            package_name=os.getenv("PLAY_PACKAGE_NAME", "org.virel.pongbreaker"),
            billing_enabled=os.getenv("PLAY_BILLING_ENABLED") == "1",
            credentials_file=os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
            allow_test_purchases=os.getenv("PLAY_ALLOW_TEST_PURCHASES") == "1",
            ads_enabled=os.getenv("ADMOB_SSV_ENABLED") == "1",
            ad_unit=os.getenv("ADMOB_REWARDED_UNIT_ID", ""),
            reward_amount=int(os.getenv("ADMOB_REWARD_AMOUNT", "50")),
            reward_item=os.getenv("ADMOB_REWARD_ITEM", "coins"),
        ))

    def capabilities(self) -> dict:
        try:
            import google.auth  # noqa: F401
            import google.auth.transport.requests  # noqa: F401
            billing_dependency = True
        except ImportError:
            billing_dependency = False
        try:
            import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
            ads_dependency = True
        except ImportError:
            ads_dependency = False
        c = self.config
        return {
            "billing": bool(c.billing_enabled and billing_dependency and c.credentials_file and os.path.isfile(c.credentials_file)),
            "rewarded_ads": bool(c.ads_enabled and ads_dependency and re.fullmatch(r"ca-app-pub-\d{16}/\d{10}", c.ad_unit) and "3940256099942544" not in c.ad_unit and c.reward_amount > 0),
        }

    def _request(self, method: str, path: str) -> dict:
        if not self.capabilities()["billing"]:
            raise VerificationError("billing_not_configured")
        # AuthorizedSession and credential refresh are protected across workers.
        with self._billing_lock:
            try:
                if self._session is None:
                    from google.oauth2 import service_account
                    from google.auth.transport.requests import AuthorizedSession
                    credentials = service_account.Credentials.from_service_account_file(
                        self.config.credentials_file,
                        scopes=["https://www.googleapis.com/auth/androidpublisher"],
                    )
                    self._session = AuthorizedSession(credentials)
                response = self._session.request(method, "https://androidpublisher.googleapis.com/androidpublisher/v3/" + path, timeout=15)
                if response.status_code not in (200, 204):
                    # Never include Google's body (may contain receipt details).
                    if response.status_code in (400, 404, 410):
                        raise VerificationError("purchase_invalid")
                    raise VerificationError("store_verification_unavailable")
                return response.json() if response.content else {}
            except VerificationError:
                raise
            except Exception:
                raise VerificationError("store_verification_unavailable") from None

    def _path(self, product_id: str, purchase_token: str) -> str:
        if product_id not in PRODUCTS or not isinstance(purchase_token, str) or not (10 <= len(purchase_token) <= 4096):
            raise VerificationError("purchase_invalid")
        quote = lambda value: urllib.parse.quote(value, safe="")
        return f"applications/{quote(self.config.package_name)}/purchases/products/{quote(product_id)}/tokens/{quote(purchase_token)}"

    def verify_purchase(self, product_id: str, purchase_token: str, billing_account_id: str) -> dict:
        data = self._request("GET", self._path(product_id, purchase_token))
        if data.get("purchaseState") == 2:
            raise VerificationError("purchase_pending")
        if data.get("purchaseState") != 0:
            raise VerificationError("purchase_not_completed")
        if "purchaseType" in data and data["purchaseType"] == 0 and not self.config.allow_test_purchases:
            raise VerificationError("test_purchase_not_enabled")
        actual = data.get("obfuscatedExternalAccountId", "")
        if not isinstance(billing_account_id, str) or len(billing_account_id) != 64 or not isinstance(actual, str) or not hmac.compare_digest(actual, billing_account_id):
            raise VerificationError("purchase_account_mismatch")
        quantity = data.get("quantity", 1)
        # Console products must disable multi-quantity. Never trust client quantity.
        if type(quantity) is not int or quantity != 1:
            raise VerificationError("unsupported_purchase_quantity")
        return {
            "token_hash": hashlib.sha256(purchase_token.encode()).hexdigest(),
            "product_id": product_id,
            "quantity": quantity,
            "order_id": str(data.get("orderId", "")),
            "purchased_at_ms": int(data.get("purchaseTimeMillis", 0)),
            "consumed": data.get("consumptionState") == 1,
            "acknowledged": data.get("acknowledgementState") == 1,
            "test_purchase": data.get("purchaseType") == 0,
        }

    def finalize_purchase(self, product_id: str, purchase_token: str) -> None:
        path = self._path(product_id, purchase_token)
        current = self._request("GET", path)
        if current.get("purchaseState") != 0:
            raise VerificationError("purchase_not_completed")
        if PRODUCTS[product_id] == "consumable":
            if current.get("consumptionState") != 1:
                self._request("POST", path + ":consume")
        elif current.get("acknowledgementState") != 1:
            self._request("POST", path + ":acknowledge")

    def purchase_entitlement_status(self, product_id: str, purchase_token: str, billing_account_id: str) -> dict:
        """Recheck an already recorded receipt, including revocation/pending.

        This does not grant or revoke anything. The durable ledger chooses its
        policy; account binding must still match before inspecting entitlements.
        """
        data = self._request("GET", self._path(product_id, purchase_token))
        actual = data.get("obfuscatedExternalAccountId", "")
        if not isinstance(billing_account_id, str) or len(billing_account_id) != 64 or not isinstance(actual, str) or not hmac.compare_digest(actual, billing_account_id):
            raise VerificationError("purchase_account_mismatch")
        if data.get("purchaseState") not in (0, 1, 2):
            raise VerificationError("purchase_invalid")
        return {"token_hash": hashlib.sha256(purchase_token.encode()).hexdigest(), "product_id": product_id,
                "state": {0: "purchased", 1: "cancelled", 2: "pending"}[data["purchaseState"]],
                "consumed": data.get("consumptionState") == 1, "acknowledged": data.get("acknowledgementState") == 1}

    def list_voided_purchases(self, start_time_ms: int, end_time_ms: int | None = None, page_token: str = "") -> dict:
        """Poll one page of Google's authoritative void/refund records.

        The operator must poll regularly: Google retains this feed for 30 days.
        The ledger must apply each token_hash idempotently before advancing its
        durable cursor. The opaque pagination token is not a purchase receipt.
        """
        now_ms = int(time.time() * 1000)
        if type(start_time_ms) is not int or start_time_ms < now_ms - 30 * 86400_000 or start_time_ms > now_ms:
            raise VerificationError("refund_window_invalid")
        if end_time_ms is None: end_time_ms = now_ms
        if type(end_time_ms) is not int or not start_time_ms <= end_time_ms <= now_ms:
            raise VerificationError("refund_window_invalid")
        if not isinstance(page_token, str) or len(page_token) > 4096:
            raise VerificationError("refund_page_invalid")
        query = {"startTime": start_time_ms, "endTime": end_time_ms, "type": 0, "maxResults": 1000, "includeQuantityBasedPartialRefund": "true"}
        if page_token: query["token"] = page_token
        path = "applications/" + urllib.parse.quote(self.config.package_name, safe="") + "/purchases/voidedpurchases?" + urllib.parse.urlencode(query)
        data = self._request("GET", path)
        rows = []
        try:
            for item in data.get("voidedPurchases", []):
                token = item["purchaseToken"]
                if not isinstance(token, str) or not token: raise ValueError("missing token")
                rows.append({"token_hash": hashlib.sha256(token.encode()).hexdigest(), "order_id": str(item.get("orderId", "")),
                             "voided_at_ms": int(item["voidedTimeMillis"]), "reason": int(item.get("voidedReason", -1)),
                             "source": int(item.get("voidedSource", -1)), "voided_quantity": int(item.get("voidedQuantity", 1))})
            return {"purchases": rows, "next_page_token": data.get("tokenPagination", {}).get("nextPageToken", "")}
        except (TypeError, ValueError, KeyError, AttributeError):
            raise VerificationError("refund_response_invalid") from None

    def _get_key(self, key_id: int):
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        with self._key_lock:
            elapsed = time.monotonic() - self._keys_at
            # Unknown key IDs cannot force an unbounded public-key fetch storm.
            if not self._keys or elapsed > 3600 or (key_id not in self._keys and elapsed > 300):
                try:
                    with urllib.request.urlopen(KEY_URL, timeout=10) as response:
                        payload = response.read(65537)
                    if len(payload) > 65536:
                        raise ValueError("key response too large")
                    keys = json.loads(payload)["keys"]
                    self._keys = {int(key["keyId"]): load_pem_public_key(key["pem"].encode()) for key in keys}
                    self._keys_at = time.monotonic()
                except Exception:
                    raise VerificationError("ad_verification_unavailable") from None
            if key_id not in self._keys:
                raise VerificationError("ad_unknown_key")
            return self._keys[key_id]

    def verify_ad_callback(self, raw_query: str) -> dict:
        if not self.capabilities()["rewarded_ads"]:
            raise VerificationError("ads_not_configured")
        if not isinstance(raw_query, str) or len(raw_query) > 16384:
            raise VerificationError("ad_callback_invalid")
        try:
            # Google signs the exact encoded substring before '&signature='.
            # Decoding/re-encoding or sorting before verification is incorrect.
            signed, suffix = raw_query.rsplit("&signature=", 1)
            signature, key_text = suffix.split("&key_id=", 1)
            if "&" in key_text or not key_text.isdigit():
                raise ValueError("unexpected signature fields")
            pairs = urllib.parse.parse_qsl(signed, keep_blank_values=True, strict_parsing=True)
            values = dict(pairs)
            if len(pairs) != len(values) or "signature" in values or "key_id" in values:
                raise ValueError("duplicate fields")
            decoded_signature = base64.urlsafe_b64decode(urllib.parse.unquote(signature) + "=" * (-len(urllib.parse.unquote(signature)) % 4))
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives import hashes
            self._get_key(int(key_text)).verify(decoded_signature, signed.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
            c = self.config
            amount = int(values["reward_amount"])
            timestamp = int(values["timestamp"])
            now_ms = int(time.time() * 1000)
            # Retry callbacks can arrive later. Economy checks the signed watch
            # timestamp against the server-issued reservation, not request time.
            if timestamp > now_ms + 300_000 or timestamp < now_ms - 7 * 86400_000:
                raise ValueError("expired callback")
            # SSV carries the numeric unit suffix, not the SDK's publisher prefix.
            if values.get("ad_unit") != c.ad_unit.rsplit("/", 1)[-1] or amount != c.reward_amount or values.get("reward_item") != c.reward_item:
                raise ValueError("wrong reward")
            if not (1 <= len(values.get("transaction_id", "")) <= 256 and 1 <= len(values.get("custom_data", "")) <= 512 and 1 <= len(values.get("user_id", "")) <= 128):
                raise ValueError("missing binding")
            return {"transaction_id": values["transaction_id"], "custom_data": values["custom_data"], "user_id": values["user_id"], "reward_amount": amount, "reward_item": c.reward_item, "timestamp": timestamp}
        except VerificationError:
            raise
        except Exception:
            raise VerificationError("ad_callback_invalid") from None
