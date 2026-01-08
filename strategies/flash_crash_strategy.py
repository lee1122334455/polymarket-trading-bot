#!/usr/bin/env python3
"""
Flash Crash Strategy - 15-Minute Market Volatility Trading

This strategy monitors 15-minute Up/Down markets for sudden probability drops
and executes trades when probability crashes by 0.30+ within 10 seconds.

Strategy Logic:
1. Auto-discover current 15-minute market for selected coin
2. Monitor orderbook prices in real-time via WebSocket
3. When either "Up" or "Down" probability drops by 0.30+ in 10 seconds:
   - Market buy the crashed side (e.g., 0.50 -> 0.20 = drop of 0.30)
4. Exit conditions:
   - Take profit: +10 cents
   - Stop loss: -5 cents

Supported coins: BTC, ETH, SOL, XRP

Usage:
    python strategies/flash_crash_strategy.py --coin ETH
    python strategies/flash_crash_strategy.py --coin BTC --size 10
    python strategies/flash_crash_strategy.py --coin BTC --drop 0.25  # 0.25 drop threshold
"""

import os
import sys
import time
import asyncio
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass
from typing import Optional, Dict, Deque
from concurrent.futures import ThreadPoolExecutor

# Suppress websocket logs to avoid interfering with TUI
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)
logging.getLogger("src.bot").setLevel(logging.WARNING)

# Auto-load .env file
from dotenv import load_dotenv
load_dotenv()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.bot import TradingBot
from src.config import Config
from src.gamma_client import GammaClient
from src.websocket_client import MarketWebSocket, OrderbookSnapshot


# ANSI colors
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


@dataclass
class PricePoint:
    """A price observation at a specific time."""
    timestamp: float
    price: float
    side: str  # "up" or "down"


@dataclass
class Position:
    """Active trading position."""
    side: str  # "up" or "down"
    token_id: str
    entry_price: float
    size: float
    entry_time: float
    order_id: Optional[str] = None

    @property
    def take_profit_price(self) -> float:
        """Target price for take profit (+10 cents)."""
        return self.entry_price + 0.10

    @property
    def stop_loss_price(self) -> float:
        """Target price for stop loss (-5 cents)."""
        return self.entry_price - 0.05


@dataclass
class StrategyConfig:
    """Strategy configuration."""
    coin: str = "ETH"
    size: float = 5.0  # USDC size per trade
    drop_threshold: float = 0.30  # 30% drop
    lookback_seconds: int = 10  # Time window for detecting drop
    take_profit: float = 0.10  # +10 cents
    stop_loss: float = 0.05  # -5 cents
    poll_interval: float = 0.5  # Fallback poll interval (seconds)
    max_positions: int = 1  # Max concurrent positions
    use_websocket: bool = True  # Use WebSocket for real-time data


class FlashCrashStrategy:
    """
    Flash Crash Trading Strategy.

    Monitors 15-minute markets for sudden price drops and trades
    the volatility with defined take-profit and stop-loss levels.
    """

    def __init__(
        self,
        bot: TradingBot,
        config: StrategyConfig,
    ):
        """
        Initialize strategy.

        Args:
            bot: TradingBot instance
            config: Strategy configuration
        """
        self.bot = bot
        self.config = config
        self.gamma = GammaClient()

        # State
        self.running = False
        self.current_market: Optional[Dict] = None
        self.token_ids: Dict[str, str] = {}

        # WebSocket client for real-time data
        self.ws: Optional[MarketWebSocket] = None
        self._ws_connected = False
        self._ws_task: Optional[asyncio.Task] = None

        # Price history (last N seconds)
        self.price_history: Dict[str, Deque[PricePoint]] = {
            "up": deque(maxlen=100),
            "down": deque(maxlen=100),
        }

        # Recent log messages for display
        self._log_messages: Deque[str] = deque(maxlen=3)
        self._status_mode = False  # When True, log to buffer instead of printing

        # Active positions
        self.positions: Dict[str, Position] = {}

        # Cached open orders (refreshed periodically)
        self._cached_orders: list = []
        self._last_order_check: float = 0
        self._order_refresh_executor = ThreadPoolExecutor(max_workers=1)

        # Stats
        self.trades_executed = 0
        self.total_pnl = 0.0

        # Track previous market for change detection
        self._previous_market_slug: Optional[str] = None

    def _get_countdown(self) -> str:
        """Get countdown string until market ends."""
        if not self.current_market:
            return "--:--"

        end_date_str = self.current_market.get("end_date", "")
        if not end_date_str:
            return "--:--"

        try:
            # Parse ISO format: "2024-01-15T12:30:00Z" or "2024-01-15T12:30:00.000Z"
            end_date_str = end_date_str.replace("Z", "+00:00")
            end_time = datetime.fromisoformat(end_date_str)
            now = datetime.now(timezone.utc)
            remaining = end_time - now

            if remaining.total_seconds() <= 0:
                return f"{Colors.RED}ENDED{Colors.RESET}"

            total_secs = int(remaining.total_seconds())
            mins = total_secs // 60
            secs = total_secs % 60

            # Color based on time remaining
            if total_secs <= 60:
                color = Colors.RED
            elif total_secs <= 180:
                color = Colors.YELLOW
            else:
                color = Colors.GREEN

            return f"{color}{mins:02d}:{secs:02d}{Colors.RESET}"
        except Exception:
            return "--:--"

    def log(self, msg: str, level: str = "info") -> None:
        """Log message with timestamp."""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        color = {
            "info": Colors.BLUE,
            "success": Colors.GREEN,
            "warning": Colors.YELLOW,
            "error": Colors.RED,
            "trade": Colors.MAGENTA,
        }.get(level, "")
        symbol = {
            "info": "ℹ",
            "success": "✓",
            "warning": "⚠",
            "error": "✗",
            "trade": "💰",
        }.get(level, "")
        formatted = f"[{ts}] {symbol} {msg}"

        if self._status_mode:
            # Store for display in status
            self._log_messages.append(f"{color}{formatted}{Colors.RESET}")
        else:
            # Print directly
            print(f"{Colors.CYAN}[{ts}]{Colors.RESET} {color}{symbol}{Colors.RESET} {msg}")

    async def discover_market(self) -> bool:
        """
        Discover and switch to current 15-minute market.

        Returns:
            True if market found and active
        """
        self.log(f"Searching for {self.config.coin} 15-minute market...")

        market_info = self.gamma.get_market_info(self.config.coin)

        if not market_info:
            self.log(f"No active {self.config.coin} 15-minute market found", "warning")
            return False

        if not market_info["accepting_orders"]:
            self.log(f"Market not accepting orders: {market_info['slug']}", "warning")
            return False

        new_slug = market_info.get("slug", "")

        # Check if market changed
        if self._previous_market_slug and self._previous_market_slug != new_slug:
            self.log(f"MARKET CHANGED: {self._previous_market_slug} -> {new_slug}", "warning")

        self._previous_market_slug = new_slug
        self.current_market = market_info
        self.token_ids = market_info["token_ids"]

        self.log(f"Found market: {market_info['question']}", "success")
        self.log(f"  Slug: {market_info['slug']}")
        self.log(f"  End: {market_info['end_date']}")
        self.log(f"  Up token: {self.token_ids.get('up', 'N/A')}")
        self.log(f"  Down token: {self.token_ids.get('down', 'N/A')}")

        # Clear price history for new market
        self.price_history["up"].clear()
        self.price_history["down"].clear()

        return True

    async def fetch_prices(self) -> Dict[str, float]:
        """
        Fetch current prices from orderbook.

        Returns:
            Dictionary with "up" and "down" prices
        """
        prices = {}

        for side in ["up", "down"]:
            token_id = self.token_ids.get(side)
            if not token_id:
                continue

            try:
                orderbook = await self.bot.get_order_book(token_id)
                if orderbook:
                    # Use best bid as current price (what we could sell at)
                    bids = orderbook.get("bids", [])
                    asks = orderbook.get("asks", [])

                    if bids:
                        best_bid = float(bids[0].get("price", 0))
                    else:
                        best_bid = 0

                    if asks:
                        best_ask = float(asks[0].get("price", 0))
                    else:
                        best_ask = 1

                    # Mid price
                    if best_bid > 0 and best_ask < 1:
                        prices[side] = (best_bid + best_ask) / 2
                    elif best_bid > 0:
                        prices[side] = best_bid
                    elif best_ask < 1:
                        prices[side] = best_ask

            except Exception as e:
                self.log(f"Error fetching {side} orderbook: {e}", "error")

        return prices

    def record_prices(self, prices: Dict[str, float]) -> None:
        """Record price points in history."""
        now = time.time()

        for side, price in prices.items():
            if price > 0:
                self.price_history[side].append(
                    PricePoint(timestamp=now, price=price, side=side)
                )

    def detect_flash_crash(self) -> Optional[str]:
        """
        Detect if a flash crash occurred.

        A flash crash is when the probability drops by more than the threshold
        (e.g., 0.30 means price drops from 0.5 to 0.2 or lower).

        Returns:
            Side that crashed ("up" or "down") or None
        """
        now = time.time()
        lookback = self.config.lookback_seconds

        for side in ["up", "down"]:
            history = self.price_history[side]
            if len(history) < 2:
                continue

            # Get price from lookback_seconds ago
            old_price = None
            current_price = history[-1].price

            for point in history:
                if now - point.timestamp <= lookback:
                    old_price = point.price
                    break

            if old_price is None:
                continue

            # Calculate absolute probability drop (not percentage)
            # e.g., 0.5 -> 0.2 = drop of 0.3
            drop = old_price - current_price

            if drop >= self.config.drop_threshold:
                self.log(
                    f"FLASH CRASH detected on {side.upper()}! "
                    f"Drop: {drop:.2f} ({old_price:.2f} -> {current_price:.2f})",
                    "trade"
                )
                return side

        return None

    async def execute_buy(self, side: str, current_price: float) -> bool:
        """
        Execute market buy order.

        Args:
            side: "up" or "down"
            current_price: Current market price

        Returns:
            True if order placed successfully
        """
        token_id = self.token_ids.get(side)
        if not token_id:
            self.log(f"No token ID for {side}", "error")
            return False

        # Calculate size based on price
        size = self.config.size / current_price

        self.log(f"Placing BUY order: {side.upper()} @ {current_price:.2f}, size={size:.2f}")

        # Place order slightly above market for faster fill
        buy_price = min(current_price + 0.02, 0.99)

        result = await self.bot.place_order(
            token_id=token_id,
            price=buy_price,
            size=size,
            side="BUY"
        )

        if result.success:
            self.log(f"Order placed! ID: {result.order_id}", "success")

            # Track position
            self.positions[side] = Position(
                side=side,
                token_id=token_id,
                entry_price=current_price,
                size=size,
                entry_time=time.time(),
                order_id=result.order_id
            )
            self.trades_executed += 1
            return True
        else:
            self.log(f"Order failed: {result.message}", "error")
            return False

    async def check_exit_conditions(self, prices: Dict[str, float]) -> None:
        """
        Check and execute exits for open positions.

        Args:
            prices: Current prices
        """
        for side, position in list(self.positions.items()):
            current_price = prices.get(side, 0)
            if current_price == 0:
                continue

            pnl = (current_price - position.entry_price) * position.size

            # Check take profit
            if current_price >= position.take_profit_price:
                self.log(
                    f"TAKE PROFIT: {side.upper()} @ {current_price:.2f} "
                    f"(entry: {position.entry_price:.2f}, PnL: +${pnl:.2f})",
                    "success"
                )
                await self.execute_sell(side, current_price, position)
                self.total_pnl += pnl
                continue

            # Check stop loss
            if current_price <= position.stop_loss_price:
                self.log(
                    f"STOP LOSS: {side.upper()} @ {current_price:.2f} "
                    f"(entry: {position.entry_price:.2f}, PnL: -${abs(pnl):.2f})",
                    "warning"
                )
                await self.execute_sell(side, current_price, position)
                self.total_pnl += pnl
                continue

            # Log position status periodically
            hold_time = time.time() - position.entry_time
            if hold_time > 0 and int(hold_time) % 10 == 0:
                self.log(
                    f"Position: {side.upper()} @ {current_price:.2f} "
                    f"(entry: {position.entry_price:.2f}, PnL: ${pnl:+.2f})"
                )

    async def execute_sell(self, side: str, price: float, position: Position) -> bool:
        """
        Execute sell order to close position.

        Args:
            side: Position side
            price: Current price
            position: Position to close

        Returns:
            True if order placed
        """
        # Calculate PnL before closing
        pnl = (price - position.entry_price) * position.size

        # Place sell order slightly below market for faster fill
        sell_price = max(price - 0.02, 0.01)

        result = await self.bot.place_order(
            token_id=position.token_id,
            price=sell_price,
            size=position.size,
            side="SELL"
        )

        if result.success:
            # Update stats
            self.trades_executed += 1
            self.total_pnl += pnl
            self.log(f"Sell order placed! ID: {result.order_id} PnL: ${pnl:+.2f}", "success")
            del self.positions[side]
            return True
        else:
            self.log(f"Sell order failed: {result.message}", "error")
            return False

    def print_status(self, prices: Dict[str, float], open_orders: Optional[list] = None) -> None:
        """Print current status with 5-level orderbook depth (in-place update)."""
        # Build output lines
        lines = []

        ws_status = f"{Colors.GREEN}WS{Colors.RESET}" if self._ws_connected else f"{Colors.RED}REST{Colors.RESET}"
        countdown = self._get_countdown()

        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")
        lines.append(f"{Colors.CYAN}[{self.config.coin}]{Colors.RESET} [{ws_status}] Ends: {countdown} | Trades: {self.trades_executed} | PnL: ${self.total_pnl:+.2f}")
        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")

        # Get orderbook snapshots
        up_ob = None
        down_ob = None
        if self.ws:
            up_token = self.token_ids.get("up")
            down_token = self.token_ids.get("down")
            if up_token:
                up_ob = self.ws.get_orderbook(up_token)
            if down_token:
                down_ob = self.ws.get_orderbook(down_token)

        # Orderbook header - fixed width columns
        lines.append(f"{Colors.GREEN}{'UP':^39}{Colors.RESET}|{Colors.RED}{'DOWN':^39}{Colors.RESET}")
        lines.append(f"{'Bid':>9} {'Size':>9} | {'Ask':>9} {'Size':>9}|{'Bid':>9} {'Size':>9} | {'Ask':>9} {'Size':>9}")
        lines.append("-" * 80)

        # Get up to 5 levels
        up_bids = up_ob.bids[:5] if up_ob else []
        up_asks = up_ob.asks[:5] if up_ob else []
        down_bids = down_ob.bids[:5] if down_ob else []
        down_asks = down_ob.asks[:5] if down_ob else []

        # 5 levels of depth with fixed width
        for i in range(5):
            up_bid = f"{up_bids[i].price:>9.4f} {up_bids[i].size:>9.1f}" if i < len(up_bids) else f"{'--':>9} {'--':>9}"
            up_ask = f"{up_asks[i].price:>9.4f} {up_asks[i].size:>9.1f}" if i < len(up_asks) else f"{'--':>9} {'--':>9}"
            down_bid = f"{down_bids[i].price:>9.4f} {down_bids[i].size:>9.1f}" if i < len(down_bids) else f"{'--':>9} {'--':>9}"
            down_ask = f"{down_asks[i].price:>9.4f} {down_asks[i].size:>9.1f}" if i < len(down_asks) else f"{'--':>9} {'--':>9}"
            lines.append(f"{up_bid} | {up_ask}|{down_bid} | {down_ask}")

        lines.append("-" * 80)

        # Summary prices
        up_mid = up_ob.mid_price if up_ob else prices.get("up", 0)
        down_mid = down_ob.mid_price if down_ob else prices.get("down", 0)
        up_spread = (up_ob.best_ask - up_ob.best_bid) if up_ob and up_ob.best_bid > 0 else 0
        down_spread = (down_ob.best_ask - down_ob.best_bid) if down_ob and down_ob.best_bid > 0 else 0

        lines.append(
            f"Mid: {Colors.GREEN}{up_mid:.4f}{Colors.RESET}  Spread: {up_spread:.4f}           |"
            f"Mid: {Colors.RED}{down_mid:.4f}{Colors.RESET}  Spread: {down_spread:.4f}"
        )

        # History and detection status
        up_history = len(self.price_history.get("up", []))
        down_history = len(self.price_history.get("down", []))
        lines.append(f"History: UP={up_history}/100 DOWN={down_history}/100 | Drop threshold: {self.config.drop_threshold:.2f} in {self.config.lookback_seconds}s")

        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")

        # Open orders section
        lines.append(f"{Colors.BOLD}Open Orders:{Colors.RESET}")
        if open_orders:
            for order in open_orders[:5]:  # Show max 5 orders
                side = order.get("side", "?")
                price = float(order.get("price", 0))
                size = float(order.get("original_size", order.get("size", 0)))
                filled = float(order.get("size_matched", 0))
                order_id = order.get("id", "")[:8]
                token = order.get("asset_id", "")
                # Determine if UP or DOWN
                token_side = "UP" if token == self.token_ids.get("up") else "DOWN" if token == self.token_ids.get("down") else "?"
                color = Colors.GREEN if side == "BUY" else Colors.RED
                lines.append(f"  {color}{side:4}{Colors.RESET} {token_side:4} @ {price:.4f} Size: {size:.1f} Filled: {filled:.1f} ID: {order_id}...")
        else:
            lines.append(f"  {Colors.CYAN}(no open orders){Colors.RESET}")

        # Positions section
        lines.append(f"{Colors.BOLD}Positions:{Colors.RESET}")
        if self.positions:
            for side, pos in self.positions.items():
                current = prices.get(side, 0)
                pnl = (current - pos.entry_price) * pos.size
                pnl_pct = ((current - pos.entry_price) / pos.entry_price * 100) if pos.entry_price > 0 else 0
                hold_time = time.time() - pos.entry_time
                color = Colors.GREEN if pnl >= 0 else Colors.RED
                lines.append(
                    f"  {Colors.BOLD}{side.upper():4}{Colors.RESET} "
                    f"Entry: {pos.entry_price:.4f} | Current: {current:.4f} | "
                    f"Size: ${pos.size:.2f} | PnL: {color}${pnl:+.2f} ({pnl_pct:+.1f}%){Colors.RESET} | "
                    f"Hold: {hold_time:.0f}s"
                )
                lines.append(
                    f"       TP: {pos.take_profit_price:.4f} (+${self.config.take_profit:.2f}) | "
                    f"SL: {pos.stop_loss_price:.4f} (-${self.config.stop_loss:.2f})"
                )
        else:
            lines.append(f"  {Colors.CYAN}(no open positions){Colors.RESET}")

        # Recent log messages
        if self._log_messages:
            lines.append("-" * 80)
            lines.append(f"{Colors.BOLD}Recent Events:{Colors.RESET}")
            for msg in self._log_messages:
                lines.append(f"  {msg}")

        # Move cursor to home position and print all lines
        # \033[H moves to top-left, \033[J clears from cursor to end
        output = "\033[H\033[J" + "\n".join(lines)
        print(output, flush=True)

    async def _setup_websocket(self) -> bool:
        """Setup WebSocket connection and subscribe to market data."""
        if not self.config.use_websocket:
            return False

        try:
            self.ws = MarketWebSocket()

            # Setup callbacks - use assert to help type checker
            assert self.ws is not None

            @self.ws.on_book
            async def on_book_update(snapshot: OrderbookSnapshot):  # pyright: ignore[reportUnusedFunction]
                """Handle orderbook snapshot from WebSocket."""
                # Determine which side this token belongs to
                side = None
                for s, token_id in self.token_ids.items():
                    if token_id == snapshot.asset_id:
                        side = s
                        break

                if side:
                    # Record price
                    now = time.time()
                    self.price_history[side].append(
                        PricePoint(timestamp=now, price=snapshot.mid_price, side=side)
                    )

            @self.ws.on_connect
            def on_connect():  # pyright: ignore[reportUnusedFunction]
                self._ws_connected = True
                self.log("WebSocket connected", "success")

            @self.ws.on_disconnect
            def on_disconnect():  # pyright: ignore[reportUnusedFunction]
                self._ws_connected = False
                self.log("WebSocket disconnected", "warning")

            @self.ws.on_error
            def on_error(e: Exception):  # pyright: ignore[reportUnusedFunction]
                self.log(f"WebSocket error: {e}", "error")

            # Subscribe to token IDs
            token_ids = list(self.token_ids.values())
            if token_ids:
                await self.ws.subscribe(token_ids)
                self.log(f"WebSocket subscribed to {len(token_ids)} tokens")

            # Start WebSocket in background
            self._ws_task = asyncio.create_task(self.ws.run(auto_reconnect=True))
            return True

        except Exception as e:
            self.log(f"Failed to setup WebSocket: {e}", "error")
            return False

    def _refresh_orders_sync(self) -> None:
        """Refresh open orders synchronously (runs in thread pool)."""
        try:
            # Run async method in new event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                self._cached_orders = loop.run_until_complete(self.bot.get_open_orders())
            finally:
                loop.close()
        except Exception:
            pass

    def _schedule_order_refresh(self) -> None:
        """Schedule order refresh in background thread."""
        self._order_refresh_executor.submit(self._refresh_orders_sync)

    async def _get_prices_from_ws(self) -> Dict[str, float]:
        """Get current prices from WebSocket cache."""
        prices = {}
        now = time.time()
        for side, token_id in self.token_ids.items():
            ob = self.ws.get_orderbook(token_id) if self.ws else None
            if ob and ob.mid_price > 0:
                prices[side] = ob.mid_price
                # Also record to price history for flash crash detection
                self.price_history[side].append(
                    PricePoint(timestamp=now, price=ob.mid_price, side=side)
                )
        return prices

    async def run(self) -> None:
        """Main strategy loop with WebSocket support."""
        self.running = True

        print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
        print(f"{Colors.BOLD}  Flash Crash Strategy - {self.config.coin} 15-Minute Markets{Colors.RESET}")
        print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n")

        self.log(f"Configuration:")
        self.log(f"  Coin: {self.config.coin}")
        self.log(f"  Size: ${self.config.size:.2f}")
        self.log(f"  Drop threshold: {self.config.drop_threshold:.2f}")
        self.log(f"  Lookback: {self.config.lookback_seconds}s")
        self.log(f"  Take profit: +${self.config.take_profit:.2f}")
        self.log(f"  Stop loss: -${self.config.stop_loss:.2f}")
        self.log(f"  Mode: {'WebSocket (real-time)' if self.config.use_websocket else 'REST (polling)'}")
        print()

        last_market_check = 0
        market_check_interval = 60  # Check for new market every 60s
        ws_initialized = False

        try:
            while self.running:
                now = time.time()

                # Periodically check for new market
                if now - last_market_check > market_check_interval:
                    old_tokens = set(self.token_ids.values()) if self.token_ids else set()

                    if not await self.discover_market():
                        self.log("Waiting for market...", "warning")
                        await asyncio.sleep(5)
                        last_market_check = now
                        continue
                    last_market_check = now

                    new_tokens = set(self.token_ids.values())

                    # Setup WebSocket after discovering market
                    if self.config.use_websocket and not ws_initialized:
                        ws_initialized = await self._setup_websocket()
                        if ws_initialized:
                            # Give WebSocket time to connect and receive initial snapshots
                            self.log("Waiting for WebSocket data...")
                            for _ in range(30):  # Wait up to 3 seconds
                                await asyncio.sleep(0.1)
                                if self._ws_connected:
                                    prices = await self._get_prices_from_ws()
                                    if prices:
                                        self.log(f"WebSocket data received: Up={prices.get('up', 0):.2f} Down={prices.get('down', 0):.2f}", "success")
                                        # Switch to status mode after initial setup
                                        self._status_mode = True
                                        break
                    # If market changed, resubscribe
                    elif ws_initialized and self.ws and new_tokens != old_tokens:
                        self.log("Market changed, resubscribing to WebSocket...")
                        await self.ws.subscribe(list(new_tokens))

                # Get prices (from WebSocket or REST)
                if self._ws_connected and self.ws:
                    prices = await self._get_prices_from_ws()
                    # Don't fallback to REST if WebSocket is connected - just use cached data
                else:
                    prices = await self.fetch_prices()

                # Always print status even without prices
                if not prices:
                    prices = {"up": 0.5, "down": 0.5}  # Default placeholder

                # Record prices (for REST mode or when WS doesn't auto-record)
                if not self._ws_connected:
                    self.record_prices(prices)

                # Get open orders in background (every 30 seconds, non-blocking via thread pool)
                # TODO: Debug - temporarily disabled to confirm this is the issue
                # if now - self._last_order_check > 30:
                #     self._last_order_check = now
                #     self._schedule_order_refresh()

                # Print status
                self.print_status(prices, self._cached_orders)

                # Check exit conditions for open positions
                await self.check_exit_conditions(prices)

                # Check for flash crash (only if we can open new positions)
                if len(self.positions) < self.config.max_positions:
                    crashed_side = self.detect_flash_crash()
                    if crashed_side:
                        current_price = prices.get(crashed_side, 0)
                        if current_price > 0:
                            print()  # New line after status
                            await self.execute_buy(crashed_side, current_price)

                # Sleep interval - shorter for WebSocket since we have real-time data
                interval = 0.1 if self._ws_connected else self.config.poll_interval
                await asyncio.sleep(interval)

        except KeyboardInterrupt:
            print()
            self.log("Strategy stopped by user")
        finally:
            self.running = False
            # Cleanup WebSocket
            if self._ws_task:
                self._ws_task.cancel()
                try:
                    await self._ws_task
                except asyncio.CancelledError:
                    pass
            if self.ws:
                await self.ws.disconnect()
            # Cleanup thread pool
            self._order_refresh_executor.shutdown(wait=False)
            print()
            self.log(f"Session stats:")
            self.log(f"  Trades executed: {self.trades_executed}")
            self.log(f"  Total PnL: ${self.total_pnl:+.2f}")

    def stop(self) -> None:
        """Stop the strategy."""
        self.running = False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Flash Crash Strategy for Polymarket 15-minute markets"
    )
    parser.add_argument(
        "--coin",
        type=str,
        default="ETH",
        choices=["BTC", "ETH", "SOL", "XRP"],
        help="Coin to trade (default: ETH)"
    )
    parser.add_argument(
        "--size",
        type=float,
        default=5.0,
        help="Trade size in USDC (default: 5.0)"
    )
    parser.add_argument(
        "--drop",
        type=float,
        default=0.30,
        help="Drop threshold as absolute probability change (default: 0.30)"
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=10,
        help="Lookback window in seconds (default: 10)"
    )
    parser.add_argument(
        "--take-profit",
        type=float,
        default=0.10,
        help="Take profit in dollars (default: 0.10)"
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=0.05,
        help="Stop loss in dollars (default: 0.05)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--no-ws",
        action="store_true",
        help="Disable WebSocket (use REST polling)"
    )

    args = parser.parse_args()

    # Enable debug logging if requested
    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("src.websocket_client").setLevel(logging.DEBUG)

    # Check environment
    private_key = os.environ.get("POLY_PRIVATE_KEY")
    safe_address = os.environ.get("POLY_SAFE_ADDRESS")

    if not private_key or not safe_address:
        print(f"{Colors.RED}Error: POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS must be set{Colors.RESET}")
        print("Set them in .env file or export as environment variables")
        sys.exit(1)

    # Create bot
    config = Config.from_env()
    bot = TradingBot(config=config, private_key=private_key)

    if not bot.is_initialized():
        print(f"{Colors.RED}Error: Failed to initialize bot{Colors.RESET}")
        sys.exit(1)

    # Create strategy config
    strategy_config = StrategyConfig(
        coin=args.coin.upper(),
        size=args.size,
        drop_threshold=args.drop,
        lookback_seconds=args.lookback,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        use_websocket=not args.no_ws,
    )

    # Create and run strategy
    strategy = FlashCrashStrategy(bot=bot, config=strategy_config)

    try:
        asyncio.run(strategy.run())
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
