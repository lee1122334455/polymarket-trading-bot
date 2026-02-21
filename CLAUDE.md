# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A beginner-friendly Python trading bot for Polymarket with gasless transactions via Builder Program. Features real-time WebSocket data, 15-minute crypto market support (BTC/ETH/SOL/XRP), and a strategy framework for building custom trading strategies.

## Common Commands

```bash
# Setup
pip install -r requirements.txt
export POLY_PRIVATE_KEY=your_key
export POLY_SAFE_ADDRESS=0xYourSafeAddress

# Run strategies
python apps/run_flash_crash.py --coin BTC           # Flash crash strategy
python apps/orderbook_tui.py --coin ETH --levels 5  # Real-time orderbook display

# Run examples
python examples/quickstart.py

# Testing
pytest tests/ -v                        # Run all tests
pytest tests/test_bot.py -v             # Single test file
pytest tests/test_bot.py::test_name -v  # Single test
```

## Architecture

The codebase has three layers:

```
┌─────────────────────────────────────────────────────────────┐
│                      strategies/                            │
│   BaseStrategy → FlashCrashStrategy (your strategies)       │
│   - on_book_update(), on_tick(), render_status()            │
└─────────────────────────────┬───────────────────────────────┘
                              │
┌─────────────────────────────┼───────────────────────────────┐
│                          lib/                               │
│  MarketManager    PriceTracker    PositionManager           │
│  (market + WS)    (price history) (TP/SL tracking)          │
└─────────────────────────────┬───────────────────────────────┘
                              │
┌─────────────────────────────┼───────────────────────────────┐
│                          src/                               │
│  TradingBot ← ClobClient ← OrderSigner ← KeyManager         │
│  GammaClient (15m market discovery)                         │
│  MarketWebSocket (real-time orderbook)                      │
└─────────────────────────────────────────────────────────────┘
```

### Key Modules

| Location | Module | Purpose |
|----------|--------|---------|
| `src/` | `bot.py` | TradingBot - main order interface (async) |
| `src/` | `websocket_client.py` | MarketWebSocket - real-time orderbook data |
| `src/` | `gamma_client.py` | GammaClient - 15-minute market discovery |
| `src/` | `signer.py` | EIP-712 order signing |
| `lib/` | `market_manager.py` | MarketManager - WebSocket + market auto-switching |
| `lib/` | `price_tracker.py` | PriceTracker - flash crash detection |
| `lib/` | `position_manager.py` | PositionManager - TP/SL tracking |
| `strategies/` | `base.py` | BaseStrategy - strategy lifecycle |
| `strategies/` | `flash_crash.py` | FlashCrashStrategy - example strategy |

### Strategy Data Flow

1. `MarketManager.start()` discovers 15-minute market via GammaClient
2. `MarketWebSocket` subscribes to token IDs, receives orderbook updates
3. `BaseStrategy.on_tick()` called each loop with current prices
4. Strategy calls `execute_buy(side, price)` or `bot.place_order()`
5. `PositionManager` tracks positions and checks TP/SL exits

### Order Flow

1. `TradingBot.place_order()` creates an `Order` dataclass
2. `OrderSigner.sign_order()` produces EIP-712 signature
3. `ClobClient.post_order()` submits to CLOB with Builder HMAC auth
4. If gasless enabled, `RelayerClient` handles Safe deployment/approvals

## Key Patterns

- **Async methods**: All trading operations (`place_order`, `cancel_order`, `get_trades`) are async
- **WebSocket callbacks**: Use decorators `@ws.on_book`, `@manager.on_book_update`
- **Strategy subclassing**: Extend `BaseStrategy`, implement `on_tick()`, `on_book_update()`, `render_status()`
- **Config precedence**: Environment vars > YAML file > defaults
- **Builder HMAC auth**: Timestamp + method + path + body signed with api_secret

## Building Strategies

See `docs/strategy_guide.md` for complete tutorial. Minimal template:

```python
from strategies.base import BaseStrategy, StrategyConfig
from src.websocket_client import OrderbookSnapshot

class MyStrategy(BaseStrategy):
    async def on_book_update(self, snapshot: OrderbookSnapshot) -> None:
        pass  # React to orderbook changes

    async def on_tick(self, prices: Dict[str, float]) -> None:
        # Main logic - check signals, execute trades
        if self.positions.can_open_position and prices.get("up", 0) < 0.40:
            await self.execute_buy("up", prices["up"])

    def render_status(self, prices: Dict[str, float]) -> None:
        print(f"up={prices.get('up', 0):.4f}")
```

## Testing Notes

- Tests use `pytest` with `pytest-asyncio` for async
- Mock external API calls; never hit real Polymarket APIs in tests
- Test private key: `"0x" + "a" * 64`
- Test safe address: `"0x" + "b" * 40`
- YAML config values starting with `0x` must be quoted

## Polymarket API Context

- CLOB API: `https://clob.polymarket.com` - order submission/cancellation
- Relayer API: `https://relayer-v2.polymarket.com` - gasless transactions
- Gamma API: `https://gamma-api.polymarket.com` - market discovery
- WebSocket: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Token IDs are ERC-1155 identifiers for market outcomes
- Prices are 0-1 (probability percentages)

**Important**: The `docs/` directory contains official Polymarket documentation:
- `docs/developers/CLOB/` - CLOB API endpoints, authentication, orders
- `docs/developers/builders/` - Builder Program, Relayer, gasless transactions
- `docs/developers/CLOB/websocket/` - WebSocket message formats
