#!/usr/bin/env python3
"""
Real-time Orderbook TUI - Textual-based Terminal Interface

Displays real-time orderbook data for Polymarket 15-minute markets using WebSocket.
Shows 5-level depth for both UP and DOWN tokens with live updates.

Usage:
    python strategies/orderbook_tui.py --coin BTC
    python strategies/orderbook_tui.py --coin ETH --levels 10
"""

import sys
import logging
import argparse
from pathlib import Path
from typing import Optional, Dict

# Suppress websocket logs in TUI mode
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from textual.app import App, ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import Header, Footer, Static, DataTable, Label
from textual.reactive import reactive
from textual.message import Message
from textual import work

from src.gamma_client import GammaClient
from src.websocket_client import MarketWebSocket, OrderbookSnapshot


class OrderbookTable(DataTable):
    """Custom DataTable for orderbook display."""

    def __init__(self, side: str, **kwargs):
        super().__init__(**kwargs)
        self.side = side
        self.zebra_stripes = True
        self.cursor_type = "none"


class MarketInfo(Static):
    """Display market information."""

    market_name = reactive("")
    end_time = reactive("")

    def render(self) -> str:
        return f"[bold]{self.market_name}[/bold]\nEnds: {self.end_time}"


class PriceDisplay(Static):
    """Display current price with color."""

    price = reactive(0.0)
    spread = reactive(0.0)
    side = reactive("up")

    def render(self) -> str:
        color = "green" if self.side == "up" else "red"
        return (
            f"[bold {color}]{self.side.upper()}[/bold {color}]\n"
            f"Mid: [{color}]{self.price:.4f}[/{color}]\n"
            f"Spread: {self.spread:.4f}"
        )


class ConnectionStatus(Static):
    """Display WebSocket connection status."""

    connected = reactive(False)
    msg_count = reactive(0)

    def render(self) -> str:
        if self.connected:
            return f"[green]● Connected[/green] | Messages: {self.msg_count}"
        else:
            return "[red]○ Disconnected[/red]"


class OrderbookApp(App):
    """Real-time Orderbook TUI Application."""

    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 3;
        grid-rows: auto 1fr auto;
    }

    #header-container {
        column-span: 2;
        height: auto;
        padding: 1;
        background: $surface;
    }

    #market-info {
        dock: left;
        width: 50%;
    }

    #connection-status {
        dock: right;
        width: 50%;
        text-align: right;
    }

    .orderbook-container {
        padding: 1;
        border: solid green;
        height: 100%;
    }

    #up-container {
        border: solid green;
    }

    #down-container {
        border: solid red;
    }

    .orderbook-container > Label {
        text-align: center;
        text-style: bold;
        width: 100%;
        padding: 1;
    }

    #up-container > Label {
        color: green;
    }

    #down-container > Label {
        color: red;
    }

    DataTable {
        height: 1fr;
    }

    .price-display {
        text-align: center;
        padding: 1;
        height: auto;
    }

    #footer-container {
        column-span: 2;
        height: auto;
        padding: 1;
        background: $surface;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("d", "toggle_dark", "Toggle Dark"),
    ]

    # Custom messages for WebSocket events
    class Connected(Message):
        """WebSocket connected message."""
        pass

    class Disconnected(Message):
        """WebSocket disconnected message."""
        pass

    def __init__(self, coin: str = "BTC", levels: int = 5):
        super().__init__()
        self.coin = coin.upper()
        self.levels = levels
        self.ws: Optional[MarketWebSocket] = None
        self.token_ids: Dict[str, str] = {}
        self.market_info: Dict = {}
        self.msg_count = 0

    def compose(self) -> ComposeResult:
        yield Header()

        with Container(id="header-container"):
            yield MarketInfo(id="market-info")
            yield ConnectionStatus(id="connection-status")

        with Vertical(id="up-container", classes="orderbook-container"):
            yield Label(f"UP Orderbook")
            yield OrderbookTable("up", id="up-table")
            yield PriceDisplay(id="up-price", classes="price-display")

        with Vertical(id="down-container", classes="orderbook-container"):
            yield Label(f"DOWN Orderbook")
            yield OrderbookTable("down", id="down-table")
            yield PriceDisplay(id="down-price", classes="price-display")

        yield Footer()

    async def on_mount(self) -> None:
        """Initialize on mount."""
        # Setup tables
        for side in ["up", "down"]:
            table = self.query_one(f"#{side}-table", DataTable)
            table.add_column("Level", key="level", width=6)
            table.add_column("Bid", key="bid", width=10)
            table.add_column("Bid Size", key="bid_size", width=10)
            table.add_column("Ask", key="ask", width=10)
            table.add_column("Ask Size", key="ask_size", width=10)

            # Add empty rows
            for i in range(self.levels):
                table.add_row(str(i + 1), "-", "-", "-", "-", key=f"level_{i}")

        # Start market discovery and WebSocket
        self.discover_market()

    @work(thread=True)
    def discover_market(self) -> None:
        """Discover market in background thread."""
        gamma = GammaClient()
        market = gamma.get_market_info(self.coin)

        if market:
            self.market_info = market
            self.token_ids = {
                "up": market["token_ids"].get("up", ""),
                "down": market["token_ids"].get("down", ""),
            }

            # Update UI from thread
            self.call_from_thread(self._update_market_info, market)
            self.call_from_thread(self._start_websocket)
        else:
            self.call_from_thread(
                self.notify,
                f"No active {self.coin} market found",
                severity="error"
            )

    def _update_market_info(self, market: Dict) -> None:
        """Update market info display."""
        info = self.query_one("#market-info", MarketInfo)
        info.market_name = market.get("question", "Unknown Market")
        info.end_time = market.get("end_date", "Unknown")

    def _start_websocket(self) -> None:
        """Start WebSocket connection."""
        # @work decorator transforms this into a worker, not a coroutine
        self.run_websocket()  # pyright: ignore[reportUnusedCoroutine]

    @work(exclusive=True)
    async def run_websocket(self) -> None:
        """Run WebSocket in background."""
        self.ws = MarketWebSocket()

        # Setup callbacks - use assert to help type checker
        assert self.ws is not None

        @self.ws.on_book
        async def on_book(snapshot: OrderbookSnapshot):  # pyright: ignore[reportUnusedFunction]
            self.msg_count += 1
            # For async workers, directly update UI (same event loop)
            self._update_orderbook(snapshot)

        @self.ws.on_connect
        def on_connect():  # pyright: ignore[reportUnusedFunction]
            # Post message to update UI from sync callback
            self.post_message(self.Connected())

        @self.ws.on_disconnect
        def on_disconnect():  # pyright: ignore[reportUnusedFunction]
            self.post_message(self.Disconnected())

        # Subscribe
        token_list = [t for t in self.token_ids.values() if t]
        if token_list:
            await self.ws.subscribe(token_list)

        # Run WebSocket
        await self.ws.run(auto_reconnect=True)

    def _set_connected(self, connected: bool) -> None:
        """Update connection status."""
        status = self.query_one("#connection-status", ConnectionStatus)
        status.connected = connected
        if connected:
            self.notify("WebSocket connected", severity="information")

    def on_orderbook_app_connected(self, message: "OrderbookApp.Connected") -> None:
        """Handle WebSocket connected message."""
        self._set_connected(True)

    def on_orderbook_app_disconnected(self, message: "OrderbookApp.Disconnected") -> None:
        """Handle WebSocket disconnected message."""
        self._set_connected(False)

    def _update_orderbook(self, snapshot: OrderbookSnapshot) -> None:
        """Update orderbook table with new data."""
        # Find which side this token belongs to
        side = None
        for s, token_id in self.token_ids.items():
            if token_id == snapshot.asset_id:
                side = s
                break

        if not side:
            return

        # Update connection status message count
        status = self.query_one("#connection-status", ConnectionStatus)
        status.msg_count = self.msg_count

        # Update table
        table = self.query_one(f"#{side}-table", DataTable)

        for i in range(self.levels):
            bid_price = snapshot.bids[i].price if i < len(snapshot.bids) else None
            bid_size = snapshot.bids[i].size if i < len(snapshot.bids) else None
            ask_price = snapshot.asks[i].price if i < len(snapshot.asks) else None
            ask_size = snapshot.asks[i].size if i < len(snapshot.asks) else None

            # Update cells
            table.update_cell(f"level_{i}", "bid", f"{bid_price:.4f}" if bid_price else "-")
            table.update_cell(f"level_{i}", "bid_size", f"{bid_size:.1f}" if bid_size else "-")
            table.update_cell(f"level_{i}", "ask", f"{ask_price:.4f}" if ask_price else "-")
            table.update_cell(f"level_{i}", "ask_size", f"{ask_size:.1f}" if ask_size else "-")

        # Update price display
        price_display = self.query_one(f"#{side}-price", PriceDisplay)
        price_display.side = side
        price_display.price = snapshot.mid_price
        price_display.spread = snapshot.best_ask - snapshot.best_bid if snapshot.best_bid > 0 else 0

    def action_refresh(self) -> None:
        """Refresh market data."""
        self.notify("Refreshing...")
        self.discover_market()

    def action_toggle_dark(self) -> None:
        """Toggle dark mode."""
        self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"


def main():
    parser = argparse.ArgumentParser(description="Real-time Orderbook TUI")
    parser.add_argument("--coin", default="BTC", help="Coin symbol (BTC, ETH, SOL, XRP)")
    parser.add_argument("--levels", type=int, default=5, help="Number of orderbook levels to show")
    args = parser.parse_args()

    app = OrderbookApp(coin=args.coin, levels=args.levels)
    app.run()


if __name__ == "__main__":
    main()
