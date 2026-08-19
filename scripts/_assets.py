"""Deployment-aware asset discovery and signer configuration.

Asset IDs and precisions are scoped to a Lighter API deployment.  The live
asset catalog is cached per host, while spot markets provide user-facing
aliases such as ``SPY`` for an asset whose internal symbol is ``rhSPY``.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass

from _paths import asset_cache_path


_LIVE_CACHE = {}


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip("/")


@dataclass(frozen=True)
class AssetMetadata:
    asset_id: int
    symbol: str
    decimals: int
    min_transfer_amount: str
    min_withdrawal_amount: str
    margin_mode: str
    loan_to_value: str

    @classmethod
    def from_api(cls, asset):
        return cls(
            asset_id=asset.asset_id,
            symbol=asset.symbol,
            decimals=asset.decimals,
            min_transfer_amount=asset.min_transfer_amount,
            min_withdrawal_amount=asset.min_withdrawal_amount,
            margin_mode=asset.margin_mode,
            loan_to_value=asset.loan_to_value,
        )

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("invalid cached asset")
        asset = cls(
            asset_id=value.get("asset_id"),
            symbol=value.get("symbol"),
            decimals=value.get("decimals"),
            min_transfer_amount=value.get("min_transfer_amount"),
            min_withdrawal_amount=value.get("min_withdrawal_amount"),
            margin_mode=value.get("margin_mode"),
            loan_to_value=value.get("loan_to_value"),
        )
        if (
            not isinstance(asset.asset_id, int)
            or not isinstance(asset.symbol, str)
            or not isinstance(asset.decimals, int)
            or asset.decimals < 0
            or not isinstance(asset.min_transfer_amount, str)
            or not isinstance(asset.min_withdrawal_amount, str)
            or asset.margin_mode not in {"enabled", "disabled"}
            or not isinstance(asset.loan_to_value, str)
        ):
            raise ValueError("invalid cached asset")
        return asset


class AssetRegistry:
    def __init__(self, assets, aliases):
        self.assets_by_id = {asset.asset_id: asset for asset in assets}
        self.aliases = {alias.upper(): asset_id for alias, asset_id in aliases.items()}
        if not self.assets_by_id:
            raise ValueError("asset catalog is empty")
        if any(
            asset_id not in self.assets_by_id
            for asset_id in self.aliases.values()
        ):
            raise ValueError("asset alias points to an unknown asset")

    @classmethod
    def from_payload(cls, payload):
        assets = payload.get("assets")
        aliases = payload.get("aliases")
        if not isinstance(assets, list) or not isinstance(aliases, dict):
            raise ValueError("invalid asset cache")
        if not all(
            isinstance(key, str) and isinstance(value, int)
            for key, value in aliases.items()
        ):
            raise ValueError("invalid asset cache aliases")
        return cls([AssetMetadata.from_dict(asset) for asset in assets], aliases)

    def to_payload(self):
        return {
            "assets": [asdict(asset) for asset in self.assets_by_id.values()],
            "aliases": self.aliases,
        }

    def find(self, value: str):
        key = value.strip().upper()
        try:
            asset_id = int(key)
        except ValueError:
            asset_id = self.aliases.get(key)
        return self.assets_by_id.get(asset_id)

    def configure_signer(self, client):
        """Install this deployment's scales on one SignerClient instance."""
        scales = {
            asset.asset_id: 10**asset.decimals
            for asset in self.assets_by_id.values()
        }
        client.ASSET_TO_TICKER_SCALE = scales

    def known_aliases(self):
        return sorted(self.aliases)


def _read_disk_cache(host):
    path = asset_cache_path(host)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("host") != host:
            return None
        return AssetRegistry.from_payload(payload)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_disk_cache(host, registry):
    path = asset_cache_path(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "host": host,
        **registry.to_payload(),
    }

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(json.dumps(payload, separators=(",", ":")))
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _add_alias(aliases, ambiguous, alias, asset_id):
    alias = alias.strip().upper()
    if not alias or alias in ambiguous:
        return
    current = aliases.get(alias)
    if current is not None and current != asset_id:
        aliases.pop(alias)
        ambiguous.add(alias)
        return
    aliases[alias] = asset_id


async def _fetch_registry(api_client):
    import lighter

    order_api = lighter.OrderApi(api_client)
    details, books = await asyncio.gather(
        order_api.asset_details(),
        order_api.order_books(filter="spot"),
    )

    assets = [AssetMetadata.from_api(asset) for asset in details.asset_details]
    asset_ids = {asset.asset_id for asset in assets}
    aliases = {}
    ambiguous = set()
    for asset in assets:
        _add_alias(aliases, ambiguous, asset.symbol, asset.asset_id)

    quote_asset_ids = set()
    for book in books.order_books:
        if book.market_type != "spot":
            continue
        pair = book.symbol.split("/", 1)
        if len(pair) != 2:
            continue
        if book.base_asset_id in asset_ids:
            _add_alias(aliases, ambiguous, pair[0], book.base_asset_id)
        if book.quote_asset_id in asset_ids:
            _add_alias(aliases, ambiguous, pair[1], book.quote_asset_id)
            quote_asset_ids.add(book.quote_asset_id)

    if len(quote_asset_ids) == 1:
        aliases["COLLATERAL"] = next(iter(quote_asset_ids))

    return AssetRegistry(assets, aliases)


async def load_asset_registry(client, force_refresh=False):
    """Load persistent metadata and configure one signer for its deployment."""
    host = _normalize_host(client.url)
    registry = None if force_refresh else _LIVE_CACHE.get(host)
    if registry is None and not force_refresh:
        registry = _read_disk_cache(host)
    if registry is None or force_refresh:
        registry = await _fetch_registry(client.api_client)
        _write_disk_cache(host, registry)
    _LIVE_CACHE[host] = registry
    registry.configure_signer(client)
    return registry


async def resolve_asset(client, value: str):
    """Resolve an asset symbol/alias/ID, refreshing once when it is unknown."""
    registry = await load_asset_registry(client)
    asset = registry.find(value)
    if asset is None:
        registry = await load_asset_registry(client, force_refresh=True)
        asset = registry.find(value)
    if asset is None:
        known = ", ".join(registry.known_aliases())
        raise ValueError(f"unknown asset '{value}'; available assets: {known}")
    return asset
